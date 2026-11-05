#!/usr/bin/env python3
"""
Diagnostic Collector for MySQL - Database Health Check and Reporting Tool

A pure Python tool that connects to MySQL/Aurora MySQL/MariaDB databases,
executes diagnostic queries, and generates formatted reports.

Usage:
    python diagnostic_collector_mysql.py --user USER --password PASSWORD --host HOST [options]

For full usage information:
    python diagnostic_collector_mysql.py --help
"""

import logging
import os
import signal
import sys
import threading
from typing import Callable, Optional, Set

from diagnostic_collector_mysql.cli import main_cli, get_enabled_sections, __version__
from diagnostic_collector_mysql.core.connection_manager import ConnectionManager, ConnectionError, UnsupportedVersionError
from diagnostic_collector_mysql.core.execution_logger import ExecutionLogger
from diagnostic_collector_mysql.parsing.input_validator import InputValidator
from diagnostic_collector_mysql.models import (
    BackOffError,
    ConnectionConfig,
    ExitCode,
    QueryDefinition,
    ReportFormat,
)
from diagnostic_collector_mysql.output.output_manager import OutputManager, OutputDirectoryError
from diagnostic_collector_mysql.core.query_executor import QueryExecutor, format_backoff_message
from diagnostic_collector_mysql.reporting.report_generator import (
    HTMLReportGenerator,
    JSONReportGenerator,
    LogFileParser,
    MarkdownReportGenerator,
    PartialHTMLReportGenerator,
    PartialJSONReportGenerator,
    PartialMarkdownReportGenerator,
    PartialTextReportGenerator,
    TextReportGenerator,
)


# Global flag for interrupt handling
_interrupted = False

# Callable that cancels the running query server-side; registered once a
# database connection exists, deregistered when it closes.
_kill_query_callback: Optional[Callable[[], bool]] = None

# Callable that forcibly aborts the connection socket, so an immediate
# exit cannot block in cleanup waiting on an in-flight statement.
_abort_connection_callback: Optional[Callable[[], bool]] = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _signal_handler(signum: int, frame) -> None:
    """
    Signal handler for SIGINT and SIGTERM.
    
    First signal: sets the global interrupted flag, and cancels the
    statement currently running on the collector's connection with a
    server-side KILL QUERY issued from a helper thread, the same
    mechanism the mysql client uses on Ctrl-C. The query executor then
    stops before the next query, and main() writes a partial report if any
    results were collected. Before a connection exists there is nothing to
    cancel, and the message says so instead.
    
    Second signal: aborts the connection socket and exits with the
    partial-completion code. When the socket cannot be aborted (some
    connector builds do not expose it), cleanup could block on the
    statement being escaped, so the process exits without cleanup
    instead. Either way the exit is immediate and the code is 6.
    
    Exit with code 6 when interrupted.
    """
    global _interrupted
    if _interrupted:
        # Second signal: stop waiting and unwind now. Abort the socket
        # first, or cleanup blocks on the same in-flight statement this
        # is trying to escape (close() sends COM_QUIT and waits for the
        # server). SystemExit(6) then rides through main()'s except and
        # finally blocks (cleanup still runs) and reaches the
        # interpreter as the documented interrupt code without a
        # traceback, which a raised KeyboardInterrupt would not (it would
        # escape the partial-report handler and exit 1).
        sys.stderr.write(
            "\nSecond interrupt: exiting immediately.\n"
        )
        # Any further signal exits immediately with the same code. Both
        # SIGINT and SIGTERM are re-registered, because setup_signal_handlers
        # wires both here; leaving SIGTERM pointed at this handler would let
        # a second SIGTERM re-enter the whole branch. Restoring Python's
        # default handler instead would raise KeyboardInterrupt during the
        # unwind and surface a traceback with exit 1.
        _hard_exit = lambda *_: os._exit(ExitCode.PARTIAL_COMPLETION)  # noqa: E731
        signal.signal(signal.SIGINT, _hard_exit)
        signal.signal(signal.SIGTERM, _hard_exit)
        aborted = False
        if _abort_connection_callback is not None:
            aborted = bool(_abort_connection_callback())
        if not aborted:
            # The socket could not be aborted, so orderly cleanup may block
            # on the very statement this is escaping. The user asked to
            # exit now: flush what we have and go, skipping cleanup.
            sys.stderr.flush()
            sys.stdout.flush()
            os._exit(ExitCode.PARTIAL_COMPLETION)
        raise SystemExit(ExitCode.PARTIAL_COMPLETION)
    
    _interrupted = True
    if _kill_query_callback is not None:
        # State an attempt, not an outcome: the daemon thread has not run
        # yet, and kill_current_query can legitimately fail (unknown
        # connection id, side-connection refused, expired credentials). It
        # logs the real result at INFO or WARNING.
        sys.stderr.write(
            "\nInterrupt received: attempting to cancel the running query "
            "server-side and stopping the collection. Press Ctrl-C again "
            "to exit immediately.\n"
        )
        # Network I/O does not belong inside a signal handler frame; a
        # daemon thread issues the KILL QUERY while the main thread
        # returns to (and is unblocked from) the interrupted call.
        threading.Thread(target=_kill_query_callback, daemon=True).start()
    else:
        # Interrupted before a connection existed (argument parsing, input
        # validation, or the connection attempt itself). There is no
        # statement to cancel and no collected result to report on, so
        # promise neither.
        sys.stderr.write(
            "\nInterrupt received: stopping. Press Ctrl-C again to exit "
            "immediately.\n"
        )


def register_interrupt_kill_callback(
    callback: Optional[Callable[[], bool]],
    abort_callback: Optional[Callable[[], bool]] = None
) -> None:
    """
    Register the callables the signal handler uses on interrupt: one to
    cancel the running query server-side (first signal), one to abort the
    connection socket so an immediate exit cannot block (second signal).
    Pass None to deregister both (e.g. after the connection is closed,
    when neither can help).
    
    Both callables must return a bool reporting whether they succeeded.
    The handler treats a false abort as "cleanup may block" and exits
    without it, so a callback that returns None would force that path
    even when the socket was aborted.
    """
    global _kill_query_callback, _abort_connection_callback
    _kill_query_callback = callback
    _abort_connection_callback = abort_callback


def setup_signal_handlers() -> None:
    """Register signal handlers for graceful shutdown."""
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


def is_interrupted() -> bool:
    """Check if an interrupt signal has been received."""
    return _interrupted


def run_dry_run(
    args,
    queries: list,
    report_format: ReportFormat,
    enabled_sections: Optional[Set[str]]
) -> int:
    """
    Execute dry-run mode: validate inputs and display execution plan.
    
    Args:
        args: Parsed CLI arguments
        queries: List of QueryDefinition objects
        report_format: Parsed ReportFormat object
        enabled_sections: Set of enabled section names, or None for all
    
    Returns:
        Exit code (0 for success)
    
    """
    if args.progress:
        print("\n" + "=" * 60)
        print("DRY-RUN MODE - Execution Plan")
        print("=" * 60)
        
        # Display connection info (without connecting)
        print(f"\nTarget Database:")
        print(f"  Host: {args.host}:{args.port}")
        print(f"  User: {args.user}")
        if args.use_encrypted_connection:
            ssl_mode = 'Required'
        else:
            ssl_mode = 'Preferred (default)'
        print(f"  SSL:  {ssl_mode}")
        
        # Display output settings
        print(f"\nOutput Settings:")
        print(f"  Format: {args.format}")
        print(f"  Output Directory: {args.output_dir}")
        print(f"  Collection Limit: {args.collection_limit}")
        
        # Display back-off thresholds
        print(f"\nBack-Off Thresholds:")
        print(f"  Connection Back-Off: {args.connection_back_off}")
        print(f"  Objects Back-Off: {args.objects_back_off}")
        
        # Display sections to run
        print(f"\nSections to Execute:")
        if enabled_sections:
            for section in sorted(enabled_sections):
                print(f"  - {section}")
        else:
            print("  - All sections")
        
        # Display query count by section
        print(f"\nQueries by Section:")
        section_counts = {}
        for query in queries:
            section_counts[query.section] = section_counts.get(query.section, 0) + 1
        for section, count in sorted(section_counts.items()):
            marker = "*" if enabled_sections and section in enabled_sections else " "
            print(f"  {marker} {section}: {count} queries")
        
        # Display report sections
        print(f"\nReport Sections ({len(report_format.sections)} total):")
        for section in report_format.sections:
            print(f"  - {section.name} ({len(section.subsections)} subsections)")
        
        print("\n" + "=" * 60)
        print("Dry-run complete. No database connection was made.")
        print("=" * 60)
    
    return ExitCode.SUCCESS


def generate_report(
    args,
    output_manager: OutputManager,
    report_format: ReportFormat,
    engine_type,
    execution_logger: ExecutionLogger,
    is_partial: bool = False,
    database_info: Optional[dict] = None
) -> str:
    """
    Generate the report in the specified format.
    
    Args:
        args: Parsed CLI arguments
        output_manager: OutputManager instance
        report_format: Parsed ReportFormat object
        engine_type: Detected database engine type
        execution_logger: ExecutionLogger instance
        is_partial: Whether this is a partial report (interrupted)
        database_info: Dictionary with host, version, engine_type for report header
    
    Returns:
        Path to the generated report file
    """
    report_path = output_manager.get_report_path(args.format)
    
    # Parse log files from output directory
    log_parser = LogFileParser(output_manager.output_dir)
    parsed_logs = log_parser.parse_all_logs()
    
    execution_logger.log_info(f"Generating {args.format} report...")
    
    # Get engine type string for report
    engine_type_str = engine_type.value if engine_type else None
    
    # Build database_info if not provided
    if database_info is None:
        database_info = {}
    
    # Select appropriate generator based on format and partial status
    if is_partial:
        if args.format == 'html':
            generator = PartialHTMLReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                is_partial=True,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        elif args.format == 'markdown':
            generator = PartialMarkdownReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                is_partial=True,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        elif args.format == 'text':
            generator = PartialTextReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                is_partial=True,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        else:  # json
            generator = PartialJSONReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                is_partial=True,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        
        report_content = generator.generate(parsed_logs, engine_type_str)
    else:
        if args.format == 'html':
            generator = HTMLReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        elif args.format == 'markdown':
            generator = MarkdownReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        elif args.format == 'text':
            generator = TextReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        else:  # json
            generator = JSONReportGenerator(
                report_format=report_format,
                collection_limit=args.collection_limit,
                timestamp=output_manager.display_timestamp,
                database_info=database_info,
                extended_checks=args.extended_checks
            )
        
        report_content = generator.generate(parsed_logs, engine_type_str)
    
    # Write report content to file
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_content)
    
    # Set restrictive permissions on report file (contains sensitive data)
    OutputManager.set_secure_file_permissions(report_path)
    
    execution_logger.log_info(f"Report generated: {report_path}")
    return report_path


def main() -> int:
    """
    Main entry point for Diagnostic Collector for MySQL.
    
    Returns:
        int: Exit code as defined in ExitCode class:
            - 0: Successful completion
            - 1: Invalid command line arguments
            - 2: Database connection failures
            - 3: Input file errors
            - 4: Output directory errors
            - 5: Back-off threshold exceeded
            - 6: Partial completion or interrupted
    """
    # Set up signal handlers for graceful shutdown
    setup_signal_handlers()
    
    # Parse and validate CLI arguments
    # Returns exit code 1 for invalid arguments
    args, exit_code = main_cli()
    
    if args is None:
        # Argument parsing or validation failed
        return exit_code if exit_code != 0 else ExitCode.INVALID_ARGUMENTS
    
    # Configure logging level based on verbosity and progress flags
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.progress:
        logging.getLogger().setLevel(logging.INFO)
    else:
        # Default: suppress INFO messages, only show warnings and errors
        logging.getLogger().setLevel(logging.WARNING)
    
    # Get enabled sections based on section check flags
    enabled_sections = get_enabled_sections(args)
    enabled_sections_set = set(enabled_sections) if enabled_sections else None
    
    if args.progress:
        print(f"Diagnostic Collector for MySQL v{__version__}")
        if args.dry_run:
            print("Running in dry-run mode...")
        if enabled_sections:
            print(f"Enabled sections: {', '.join(enabled_sections)}")
        else:
            print("Running all sections...")
    
    # Determine script directory for input files
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # ==========================================================================
    # Step 1: Validate input files
    # ==========================================================================
    if args.progress:
        print("Validating input files...")
    
    validator = InputValidator()
    
    # Validate queries.json
    query_errors, queries = validator.validate_queries_json()
    if query_errors:
        for error in query_errors:
            print(f"Error in {error.file} at {error.location}: {error.message}", 
                  file=sys.stderr)
        return ExitCode.INPUT_FILE_ERROR
    
    # Get query IDs for cross-reference validation
    query_ids = {q.id for q in queries}
    
    # Verify the Health Overview does not reference query IDs that do not exist.
    # A missing ID silently omits the check from every report, so this fails fast.
    health_ref_errors = validator.validate_health_check_references(query_ids)
    if health_ref_errors:
        for error in health_ref_errors:
            print(f"Error in {error.file} at {error.location}: {error.message}",
                  file=sys.stderr)
        return ExitCode.INPUT_FILE_ERROR
    
    # Validate report_format.json
    format_errors, report_format = validator.validate_report_format_json(query_ids)
    if format_errors:
        for error in format_errors:
            print(f"Error in {error.file} at {error.location}: {error.message}",
                  file=sys.stderr)
        return ExitCode.INPUT_FILE_ERROR
    
    if args.progress:
        print(f"  Loaded {len(queries)} queries from queries.json")
        print(f"  Loaded {len(report_format.sections)} sections from report_format.json")
    
    # ==========================================================================
    # Step 2: Create output directory
    # Must be done BEFORE the database connection
    # ==========================================================================
    if args.progress:
        print("Creating output directory...")
    
    try:
        output_manager = OutputManager(base_dir=args.output_dir)
        output_dir = output_manager.create_output_directory()
        if args.progress:
            print(f"  Output directory: {output_dir}")
    except OutputDirectoryError as err:
        print(f"Error: {err}", file=sys.stderr)
        return ExitCode.OUTPUT_DIRECTORY_ERROR
    
    # ==========================================================================
    # Dry-run mode: validate and display plan, then exit
    # ==========================================================================
    if args.dry_run:
        return run_dry_run(args, queries, report_format, enabled_sections_set)
    
    # ==========================================================================
    # Step 3: Initialize execution logger
    # ==========================================================================
    execution_logger = ExecutionLogger(
        log_file_path=output_manager.get_main_log_path(),
        query_timeout=args.query_timeout,
        verbose=args.verbose
    )
    execution_logger.start()
    
    # Track state for cleanup
    connection_manager = None
    has_query_results = False
    partial_completion = False
    engine_type = None
    database_info = {}  # Store host, version, engine for report header
    
    try:
        # ==================================================================
        # Step 4: Establish database connection
        # ==================================================================
        if args.progress:
            print(f"Connecting to {args.host}:{args.port}...")
        
        connection_config = ConnectionConfig(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            require_ssl=args.use_encrypted_connection,
            ssl_ca=args.ssl_ca,
            connect_timeout=args.connect_timeout,
            get_warnings=args.progress
        )
        
        connection_manager = ConnectionManager(connection_config)
        
        try:
            connection_manager.connect()
            engine_type = connection_manager.get_engine_type()
            
            # Build database_info for report header
            database_info = {
                'host': args.host,
                'port': args.port,
                'version': connection_manager.get_version_string() or 'Unknown',
                'engine_type': engine_type.value if engine_type else 'Unknown'
            }
            
            execution_logger.log_connection_status(
                host=args.host,
                port=args.port,
                user=args.user,
                success=True,
                engine_type=engine_type
            )
            
            if args.progress:
                print(f"  Connected successfully")
                print(f"  Detected engine: {engine_type.value}")
                
        except UnsupportedVersionError as err:
            execution_logger.log_connection_status(
                host=args.host,
                port=args.port,
                user=args.user,
                success=False,
                error_message=str(err)
            )
            print(f"Unsupported version: {err}", file=sys.stderr)
            return ExitCode.CONNECTION_FAILURE
                
        except ConnectionError as err:
            execution_logger.log_connection_status(
                host=args.host,
                port=args.port,
                user=args.user,
                success=False,
                error_message=str(err)
            )
            print(f"Connection failed: {err}", file=sys.stderr)
            return ExitCode.CONNECTION_FAILURE
        
        # Check for interruption
        if is_interrupted():
            partial_completion = True
            raise KeyboardInterrupt("Interrupted after connection")
        
        # From here on, an interrupt can cancel the in-flight statement
        # server-side
        register_interrupt_kill_callback(
            connection_manager.kill_current_query,
            connection_manager.abort_connection
        )
        
        # ==================================================================
        # Step 5: Execute queries
        # ==================================================================
        if args.progress:
            print("Executing queries...")
        
        query_executor = QueryExecutor(
            connection=connection_manager.get_connection(),
            output_dir=output_manager.output_dir,
            timestamp=output_manager.display_timestamp,
            query_timeout=args.query_timeout,
            connection_threshold=args.connection_back_off,
            objects_threshold=args.objects_back_off,
            connection_usage_threshold=args.connection_usage_back_off,
            logger=logger,
            extended_checks=args.extended_checks,
            interrupt_check=is_interrupted
        )
        
        try:
            # Execute queries with back-off checks
            execution_result = query_executor.execute_queries(
                queries=queries,
                engine_type=engine_type,
                engine_version=connection_manager.get_version_string(),
                sections=enabled_sections_set
            )
            
            has_query_results = True
            
            # Display objects back-off warning if triggered
            if query_executor.objects_backoff_warning:
                if args.progress:
                    print(f"\nWARNING: {query_executor.objects_backoff_warning}\n")
                execution_logger.log_warning(query_executor.objects_backoff_warning)
            
            # Log execution summary
            execution_logger.log_execution_summary(
                total_queries=execution_result.total_queries,
                successful=execution_result.successful,
                failed=execution_result.failed,
                cancelled=execution_result.cancelled
            )
            
            if args.progress:
                print(f"  Executed {execution_result.total_queries} queries")
                print(f"  Successful: {execution_result.successful}")
                print(f"  Failed: {execution_result.failed}")
                if execution_result.cancelled:
                    print(f"  Cancelled: {execution_result.cancelled}")
            
            # Check if any queries failed (partial completion)
            if execution_result.failed > 0:
                partial_completion = True
                
        except BackOffError as err:
            execution_logger.log_backoff_triggered(
                reason=err.reason,
                current_value=err.current_value,
                threshold=err.threshold
            )
            print(format_backoff_message(err), file=sys.stderr)
            return ExitCode.BACKOFF_EXCEEDED
        
        # Check for interruption
        if is_interrupted():
            partial_completion = True
            raise KeyboardInterrupt("Interrupted during query execution")
        
        # ==================================================================
        # Step 6: Generate report
        # ==================================================================
        if args.progress:
            print("Generating report...")
        
        report_path = generate_report(
            args=args,
            output_manager=output_manager,
            report_format=report_format,
            engine_type=engine_type,
            execution_logger=execution_logger,
            is_partial=partial_completion,
            database_info=database_info
        )
        
        if args.progress:
            print(f"  Report: {report_path}")
        
    except KeyboardInterrupt:
        # Handle interruption - generate partial report if we have results
        if args.progress:
            print("\nInterrupted. Generating partial report...")
        
        if has_query_results and engine_type is not None:
            try:
                report_path = generate_report(
                    args=args,
                    output_manager=output_manager,
                    report_format=report_format,
                    engine_type=engine_type,
                    execution_logger=execution_logger,
                    is_partial=True,
                    database_info=database_info
                )
                if args.progress:
                    print(f"  Partial report: {report_path}")
            except Exception as report_err:
                execution_logger.log_error(f"Failed to generate partial report: {report_err}")
        
        partial_completion = True
        
    finally:
        # Clean up resources. Drop the kill callback first: once the
        # connection is closing, a KILL QUERY can no longer help. Keep the
        # abort callback through close(), because close() is the call that
        # can block on an in-flight statement, so a second interrupt during
        # it must still be able to abort the socket and let cleanup finish
        # rather than skipping it.
        #
        # The nested try/finally matters: on that path the handler's
        # SystemExit is raised from inside close(), so without it the
        # remaining statements here would be skipped and the execution log
        # left unflushed in exactly the case this was built for.
        try:
            if connection_manager is not None:
                register_interrupt_kill_callback(
                    None, connection_manager.abort_connection
                )
                connection_manager.close()
        finally:
            register_interrupt_kill_callback(None)
            execution_logger.stop()
    
    # ==================================================================
    # Final status
    # ==================================================================
    if args.progress:
        print("\n" + "=" * 60)
        if partial_completion:
            print("Execution completed with partial results.")
        else:
            print("Execution completed successfully.")
        print(f"Output directory: {output_manager.output_dir}")
        print("=" * 60)
    
    # Return appropriate exit code
    if partial_completion:
        return ExitCode.PARTIAL_COMPLETION
    
    return ExitCode.SUCCESS


if __name__ == "__main__":
    sys.exit(main())
