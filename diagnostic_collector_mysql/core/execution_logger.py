#!/usr/bin/env python3
"""
Execution Logger for Diagnostic Collector for MySQL.

This module handles the main execution log file that captures the overall
execution flow including connection status, query progress, warnings, errors,
and total execution duration.

Responsibilities:
- Write main execution log to diagnostic_collector_mysql_YYYYMMDD_HHMMSS.log
- Log connection establishment status and engine detection result
- Log each query execution start and completion with timing
- Log warnings for queries that exceed 50% of timeout
- Log all errors with descriptive messages
- Log total execution duration at completion
- Include timestamp prefix on all log entries
"""

import logging
import time
from datetime import datetime
from typing import Optional

from diagnostic_collector_mysql.models import EngineType


class ExecutionLogger:
    """
    Manages the main execution log file for Diagnostic Collector for MySQL.
    
    This class handles writing to the main execution log file
    (diagnostic_collector_mysql_YYYYMMDD_HHMMSS.log) with timestamped entries
    for all significant events during execution.
    
    Attributes:
        log_file_path: Path to the main execution log file
        query_timeout: Query timeout in seconds (used for warning threshold)
        verbose: Whether to include verbose/debug information
    """
    
    # Warning threshold: 50% of query timeout
    WARNING_THRESHOLD_PERCENT = 0.5
    
    # Timestamp format for log entries
    LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
    
    def __init__(
        self,
        log_file_path: str,
        query_timeout: int = 30,
        verbose: bool = False
    ):
        """
        Initialize ExecutionLogger.
        
        Args:
            log_file_path: Path to the main execution log file
            query_timeout: Query timeout in seconds (default: 30)
            verbose: Whether to include verbose/debug information (default: False)
        """
        self._log_file_path = log_file_path
        self._query_timeout = query_timeout
        self._verbose = verbose
        self._start_time: Optional[float] = None
        self._file_handle = None
        self._logger = logging.getLogger(__name__)
    
    @property
    def log_file_path(self) -> str:
        """Get the path to the main execution log file."""
        return self._log_file_path
    
    def _get_timestamp(self) -> str:
        """
        Get current timestamp formatted for log entries.
        
        Returns:
            Timestamp string in YYYY-MM-DD HH:MM:SS format
        """
        return datetime.now().strftime(self.LOG_TIMESTAMP_FORMAT)
    
    def _write_entry(self, level: str, message: str) -> None:
        """
        Write a timestamped entry to the log file.
        
        Args:
            level: Log level (INFO, WARNING, ERROR, DEBUG)
            message: Log message to write
        
        Include timestamp prefix on all log entries
        """
        timestamp = self._get_timestamp()
        entry = f"[{timestamp}] [{level}] {message}\n"
        
        try:
            if self._file_handle is not None:
                self._file_handle.write(entry)
                self._file_handle.flush()
            else:
                # Append mode if file handle not open
                with open(self._log_file_path, 'a', encoding='utf-8') as f:
                    f.write(entry)
        except IOError as err:
            self._logger.error(f"Failed to write to execution log: {err}")
    
    def start(self) -> None:
        """
        Start the execution log and record start time.
        
        Opens the log file and writes the header with start timestamp.
        """
        self._start_time = time.time()
        
        try:
            self._file_handle = open(self._log_file_path, 'w', encoding='utf-8')
            
            # Write header
            self._file_handle.write("=" * 80 + "\n")
            self._file_handle.write("Diagnostic Collector for MySQL Execution Log\n")
            self._file_handle.write(f"Started: {self._get_timestamp()}\n")
            self._file_handle.write("=" * 80 + "\n\n")
            self._file_handle.flush()
            
            self._write_entry("INFO", "Execution started")
            
        except IOError as err:
            self._logger.error(f"Failed to create execution log file: {err}")
            raise
    
    def stop(self) -> None:
        """
        Stop the execution log and record total duration.
        
        Writes the total execution duration and closes the log file.
        Log total execution duration at completion
        """
        if self._start_time is not None:
            total_duration = time.time() - self._start_time
            self.log_total_duration(total_duration)
        
        if self._file_handle is not None:
            try:
                self._file_handle.write("\n" + "=" * 80 + "\n")
                self._file_handle.write(f"Completed: {self._get_timestamp()}\n")
                self._file_handle.write("=" * 80 + "\n")
                self._file_handle.close()
            except IOError as err:
                self._logger.error(f"Failed to close execution log: {err}")
            finally:
                self._file_handle = None
    
    def log_connection_status(
        self,
        host: str,
        port: int,
        user: str,
        success: bool,
        engine_type: Optional[EngineType] = None,
        error_message: Optional[str] = None
    ) -> None:
        """
        Log connection establishment status and engine detection result.
        
        Args:
            host: Database host
            port: Database port
            user: Database user
            success: Whether connection was successful
            engine_type: Detected engine type (if successful)
            error_message: Error message (if failed)
        
        Log connection establishment status and engine detection result
        """
        if success:
            self._write_entry(
                "INFO",
                f"Connected to {host}:{port} as user '{user}'"
            )
            if engine_type is not None:
                self._write_entry(
                    "INFO",
                    f"Detected database engine: {engine_type.value}"
                )
        else:
            self._write_entry(
                "ERROR",
                f"Failed to connect to {host}:{port} as user '{user}'"
            )
            if error_message:
                self._write_entry("ERROR", f"Connection error: {error_message}")
    
    def log_query_start(self, query_id: str, section: str) -> None:
        """
        Log the start of a query execution.
        
        Args:
            query_id: ID of the query being executed
            section: Section the query belongs to
        
        Log each query execution start
        """
        self._write_entry(
            "INFO",
            f"Starting query: {query_id} (section: {section})"
        )
    
    def log_query_complete(
        self,
        query_id: str,
        success: bool,
        execution_time: float,
        row_count: Optional[int] = None,
        error_message: Optional[str] = None
    ) -> None:
        """
        Log the completion of a query execution with timing.
        
        Args:
            query_id: ID of the executed query
            success: Whether query executed successfully
            execution_time: Time taken to execute in seconds
            row_count: Number of rows returned (if successful)
            error_message: Error message (if failed)
        
        Implements:
            - Log each query execution completion with timing
            - Log warnings for queries that exceed 50% of timeout
            - Log all errors with descriptive messages
        """
        if success:
            # Log successful completion
            row_info = f", {row_count} rows" if row_count is not None else ""
            self._write_entry(
                "INFO",
                f"Completed query: {query_id} ({execution_time:.3f}s{row_info})"
            )
            
            # Check for slow query warning
            warning_threshold = self._query_timeout * self.WARNING_THRESHOLD_PERCENT
            if execution_time > warning_threshold:
                self._write_entry(
                    "WARNING",
                    f"Query {query_id} took {execution_time:.3f}s, "
                    f"exceeding 50% of timeout ({warning_threshold:.1f}s)"
                )
        else:
            # Log error
            self._write_entry(
                "ERROR",
                f"Query {query_id} failed after {execution_time:.3f}s"
            )
            if error_message:
                self._write_entry("ERROR", f"Error: {error_message}")
    
    def log_warning(self, message: str) -> None:
        """
        Log a warning message.
        
        Args:
            message: Warning message to log
        
        Log warnings
        """
        self._write_entry("WARNING", message)
    
    def log_error(self, message: str) -> None:
        """
        Log an error message.
        
        Args:
            message: Error message to log
        
        Log all errors with descriptive messages
        """
        self._write_entry("ERROR", message)
    
    def log_info(self, message: str) -> None:
        """
        Log an informational message.
        
        Args:
            message: Info message to log
        """
        self._write_entry("INFO", message)
    
    def log_debug(self, message: str) -> None:
        """
        Log a debug message (only if verbose mode is enabled).
        
        Args:
            message: Debug message to log
        
        In verbose mode, write additional debug information
        """
        if self._verbose:
            self._write_entry("DEBUG", message)
    
    def log_total_duration(self, duration: float) -> None:
        """
        Log the total execution duration.
        
        Args:
            duration: Total execution time in seconds
        
        Log total execution duration at completion
        """
        # Format duration in human-readable format
        hours, remainder = divmod(int(duration), 3600)
        minutes, seconds = divmod(remainder, 60)
        
        if hours > 0:
            duration_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            duration_str = f"{minutes}m {seconds}s"
        else:
            duration_str = f"{duration:.3f}s"
        
        self._write_entry(
            "INFO",
            f"Total execution duration: {duration_str}"
        )
    
    def log_backoff_triggered(
        self,
        reason: str,
        current_value: int,
        threshold: int
    ) -> None:
        """
        Log a back-off event.
        
        Args:
            reason: Reason for back-off
            current_value: Current value that triggered back-off
            threshold: Configured threshold
        
        Log all back-off events to main log file
        """
        self._write_entry(
            "WARNING",
            f"Back-off triggered: {reason}"
        )
        self._write_entry(
            "WARNING",
            f"Current value: {current_value}, Threshold: {threshold}"
        )
    
    def log_execution_summary(
        self,
        total_queries: int,
        successful: int,
        failed: int,
        cancelled: int = 0
    ) -> None:
        """
        Log execution summary.
        
        Args:
            total_queries: Total number of queries attempted
            successful: Number of successful queries
            failed: Number of failed queries
            cancelled: Number cancelled by an interrupt. Emitted only when
                non-zero, so an uninterrupted run's summary is unchanged.
                Without it the counts would not add up to total_queries on
                an interrupted run, and this log is the artifact that ships
                in a support bundle.
        """
        summary = (
            f"Execution summary: {successful}/{total_queries} queries "
            f"successful, {failed} failed"
        )
        if cancelled:
            summary += f", {cancelled} cancelled by interrupt"
        self._write_entry("INFO", summary)
    
    def __enter__(self) -> 'ExecutionLogger':
        """Context manager entry - start logging."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - stop logging."""
        if exc_type is not None:
            # Log any exception that occurred
            self.log_error(f"Execution terminated due to error: {exc_val}")
        self.stop()
