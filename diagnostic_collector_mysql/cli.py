#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Command Line Interface for Diagnostic Collector for MySQL.

This module handles argument parsing and validation for the Diagnostic Collector for MySQL tool.
It defines every command-line option and validates the combinations.
"""

import argparse
import os
import sys
from typing import List, Optional, Tuple

from diagnostic_collector_mysql import __version__
from diagnostic_collector_mysql.models import ExitCode

# Environment variable for password (security best practice)
PASSWORD_ENV_VAR = 'DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD'

# Section check flags mapping to section names
SECTION_CHECK_FLAGS = {
    'replication_check': 'replication',
    'performance_check': 'performance',
    'memory_check': 'memory',
    'innodb_check': 'innodb',
    'tables_check': 'tables',
    'indexes_check': 'indexes',
    'security_check': 'security',
    'connections_check': 'connections',
    'locks_check': 'locks',
    'io_check': 'io',
    'objects_check': 'objects',
    'sql_performance_check': 'sql-performance',
    'user_activity_check': 'user-activity',
}

# Minimum database privileges required
MINIMUM_PRIVILEGES = """
Minimum Database Privileges Required:
=====================================
The following privileges are required to run Diagnostic Collector for MySQL:

Basic Operation:
  - SELECT on *.*                      - Table, index, object and account metadata
  - SELECT on performance_schema.*     - Performance metrics and wait events
  - SELECT on mysql.*                  - User accounts and privileges
  - SELECT, EXECUTE on sys.*           - Formatted data and sys formatter functions
  - PROCESS                            - For processlist and connection info
  - REPLICATION CLIENT                 - For replication status
  - TRIGGER                            - For the trigger inventory
  - EVENT                              - For the scheduled event inventory

SELECT must be global, not only per schema. information_schema returns only the
objects an account holds a privilege on and does not raise when rows are filtered
out, and its views differ in which privilege they need: TABLES and STATISTICS
appear with any table privilege, while COLUMNS and KEY_COLUMN_USAGE need SELECT.
Without the global grant, Auto-Increment reports OK on a column near overflow and
Tables Without Primary Key is inflated. TRIGGER and EVENT behave the same way.

The per-schema SELECT lines are subsumed by the global grant. They are listed
because they document which system schemas the tool reads.

Example GRANT statements for minimum privileges (scope the host to the address you
run the collector from, or the account becomes a finding in its own report):
  CREATE USER 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0' IDENTIFIED BY 'your_password';
  GRANT SELECT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  GRANT SELECT ON performance_schema.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  GRANT SELECT ON mysql.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  GRANT SELECT, EXECUTE ON sys.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  GRANT PROCESS ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  GRANT REPLICATION CLIENT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  GRANT TRIGGER ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  GRANT EVENT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
  FLUSH PRIVILEGES;

There is deliberately no GRANT on information_schema. That statement fails with
ERROR 1044 for every account including root and the RDS or Aurora master user.

On MariaDB 10.5.9 and newer, add REPLICA MONITOR on *.* for replication status.
"""


class CustomHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Custom formatter that shows default values and preserves formatting."""
    
    def _get_help_string(self, action):
        help_text = action.help or ''
        if action.default is not None and action.default != argparse.SUPPRESS:
            if action.default is not False:
                if '%(default)' not in help_text:
                    help_text += ' (default: %(default)s)'
        return help_text


def create_argument_parser() -> argparse.ArgumentParser:
    """
    Create and configure the argument parser with all CLI options.
    
    Returns:
        argparse.ArgumentParser: Configured argument parser
    
    """
    parser = argparse.ArgumentParser(
        prog='diagnostic_collector_mysql',
        description='Diagnostic Collector for MySQL - Database Health Check and Reporting Tool\n\n'
                    'Connects to MySQL/Aurora MySQL/MariaDB databases, executes diagnostic\n'
                    'queries, and generates formatted reports.',
        epilog=MINIMUM_PRIVILEGES,
        formatter_class=CustomHelpFormatter
    )
    
    # ==========================================================================
    # Connection Options (Required)
    # ==========================================================================
    connection_group = parser.add_argument_group('Connection Options (Required)')
    
    # --user argument for database username (REQUIRED)
    connection_group.add_argument(
        '--user',
        required=True,
        metavar='USERNAME',
        help='Database username'
    )
    
    # --password argument for database password
    # Password can be provided via CLI argument or DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD environment variable
    connection_group.add_argument(
        '--password',
        required=False,
        metavar='PASSWORD',
        help=f'Database password (can also be set via {PASSWORD_ENV_VAR} environment variable for security)'
    )
    
    # --host argument for database hostname (REQUIRED)
    connection_group.add_argument(
        '--host',
        required=True,
        metavar='HOSTNAME',
        help='Database hostname'
    )
    
    # --port argument for database port (default: 3306)
    connection_group.add_argument(
        '--port',
        type=int,
        default=3306,
        metavar='PORT',
        help='Database port'
    )
    
    # ==========================================================================
    # SSL/TLS Options
    # ==========================================================================
    ssl_group = parser.add_argument_group('SSL/TLS Options')
    
    # SSL/TLS is attempted by default (secure-by-default).
    # --use-encrypted-connection flag upgraded to mean "require TLS"
    ssl_group.add_argument(
        '--use-encrypted-connection',
        action='store_true',
        default=False,
        help='Require SSL/TLS: fail the connection if the server does not '
             'support it. TLS is attempted by default even without this flag.'
    )
    
    # --ssl-ca argument for SSL CA certificate file path
    ssl_group.add_argument(
        '--ssl-ca',
        metavar='FILE',
        help='Path to SSL CA certificate file. When provided, the server '
             'certificate is verified against this CA (recommended for '
             'remote connections).'
    )
    
    # ==========================================================================
    # Timeout Options
    # ==========================================================================
    timeout_group = parser.add_argument_group('Timeout Options')
    
    # --connect-timeout argument (default: 10)
    timeout_group.add_argument(
        '--connect-timeout',
        type=int,
        default=10,
        metavar='SECONDS',
        help='Database connection timeout in seconds'
    )
    
    # --query-timeout argument (default: 30)
    timeout_group.add_argument(
        '--query-timeout',
        type=int,
        default=30,
        metavar='SECONDS',
        help='Query execution timeout in seconds'
    )
    
    # ==========================================================================
    # Output Options
    # ==========================================================================
    output_group = parser.add_argument_group('Output Options')
    
    # --format argument (default: html)
    output_group.add_argument(
        '--format',
        choices=['html', 'json', 'markdown', 'text'],
        default='html',
        metavar='FORMAT',
        help='Output format: html, json, markdown, text'
    )
    
    # --output-dir argument (default: current directory)
    output_group.add_argument(
        '--output-dir',
        default='.',
        metavar='PATH',
        help='Base output directory path'
    )
    
    # --collection-limit argument (default: 20)
    output_group.add_argument(
        '--collection-limit',
        type=int,
        default=20,
        metavar='N',
        help='Maximum rows displayed per subsection'
    )
    
    # ==========================================================================
    # Section Check Flags
    # ==========================================================================
    section_group = parser.add_argument_group(
        'Section Check Flags',
        'Specify which sections to include in the report. When any flag is specified,\n'
        'Server Information and Custom Configuration sections are always included.\n'
        'Multiple flags use OR logic (all specified sections are included).'
    )
    
    # --replication-check flag
    section_group.add_argument(
        '--replication-check',
        action='store_true',
        default=False,
        help='Enable only replication status section checks'
    )
    
    # --performance-check flag
    section_group.add_argument(
        '--performance-check',
        action='store_true',
        default=False,
        help='Enable only performance metrics section checks'
    )
    
    # --memory-check flag
    section_group.add_argument(
        '--memory-check',
        action='store_true',
        default=False,
        help='Enable only memory configuration section checks'
    )
    
    # --innodb-check flag
    section_group.add_argument(
        '--innodb-check',
        action='store_true',
        default=False,
        help='Enable only storage engine status section checks'
    )
    
    # --tables-check flag
    section_group.add_argument(
        '--tables-check',
        action='store_true',
        default=False,
        help='Enable only tables section checks'
    )
    
    # --indexes-check flag
    section_group.add_argument(
        '--indexes-check',
        action='store_true',
        default=False,
        help='Enable only index analysis section checks'
    )
    
    # --security-check flag
    section_group.add_argument(
        '--security-check',
        action='store_true',
        default=False,
        help='Enable only security section checks'
    )
    
    # --connections-check flag
    section_group.add_argument(
        '--connections-check',
        action='store_true',
        default=False,
        help='Enable only connections section checks'
    )
    
    # --locks-check flag
    section_group.add_argument(
        '--locks-check',
        action='store_true',
        default=False,
        help='Enable only lock contention analysis section checks'
    )
    
    # --io-check flag
    section_group.add_argument(
        '--io-check',
        action='store_true',
        default=False,
        help='Enable only I/O analysis section checks'
    )
    
    # --objects-check flag
    section_group.add_argument(
        '--objects-check',
        action='store_true',
        default=False,
        help='Enable only database objects section checks'
    )
    
    # --sql-performance-check flag
    section_group.add_argument(
        '--sql-performance-check',
        action='store_true',
        default=False,
        help='Enable only query analysis section checks'
    )
    
    # --user-activity-check flag
    section_group.add_argument(
        '--user-activity-check',
        action='store_true',
        default=False,
        help='Enable only database user activity section checks'
    )
    
    # ==========================================================================
    # Back-Off Options
    # ==========================================================================
    backoff_group = parser.add_argument_group('Back-Off Safety Options')
    
    # --connection-back-off argument (default: 1000)
    backoff_group.add_argument(
        '--connection-back-off',
        type=int,
        default=1000,
        metavar='N',
        help='Back off if more than N connections are executing a statement. '
             'Sleeping connections and the collector\'s own session are not '
             'counted, so this measures concurrency rather than slot use. '
             'For slot use see --connection-usage-back-off'
    )
    
    # Connection slot pressure, as a percentage of max_connections. Expressed as a
    # proportion because an absolute count has to be tuned per instance, and a
    # count high enough to be safe on a large instance can never trip on a small one.
    #
    # On by default because running out of slots also disables the interrupt path:
    # cancelling a run sends KILL QUERY over a second connection, and for an
    # account without CONNECTION_ADMIN that connection is refused with "Too many
    # connections" once the slots are gone.
    backoff_group.add_argument(
        '--connection-usage-back-off',
        type=int,
        default=90,
        metavar='PCT',
        help='Back off if more than PCT percent of max_connections are in use, '
             'counting sleeping connections, which hold a slot without executing '
             'anything. Guards the interrupt path too, because cancelling a run '
             'needs a free slot to send KILL QUERY. Use 0 to disable this check'
    )
    
    # --objects-back-off argument (default: 10000)
    backoff_group.add_argument(
        '--objects-back-off',
        type=int,
        default=10000,
        metavar='N',
        help='Maximum allowed table count before backing off'
    )
    
    # --extended-checks flag for queries that may cause contention on production
    backoff_group.add_argument(
        '--extended-checks',
        action='store_true',
        default=False,
        help='Enable extended checks (e.g., data locks). May cause contention on production systems'
    )
    
    # ==========================================================================
    # Execution Options
    # ==========================================================================
    exec_group = parser.add_argument_group('Execution Options')
    
    # --dry-run flag
    exec_group.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Validate input files and display execution plan without connecting'
    )
    
    # ==========================================================================
    # Logging Options
    # ==========================================================================
    logging_group = parser.add_argument_group('Logging Options')
    
    # --verbose flag
    logging_group.add_argument(
        '--verbose',
        action='store_true',
        default=False,
        help='Enable detailed logging output'
    )
    
    # --progress flag to show INFO messages to stdout
    logging_group.add_argument(
        '--progress',
        action='store_true',
        default=False,
        help='Show progress messages to stdout during execution'
    )
    
    # ==========================================================================
    # Version
    # ==========================================================================
    # --version flag
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}',
        help='Display version and exit'
    )
    
    return parser


def get_enabled_sections(args: argparse.Namespace) -> Optional[List[str]]:
    """
    Get the list of enabled sections based on section check flags.
    
    When any section-specific check flag is specified, Server Information
    and Custom Configuration sections are always included.
    Multiple flags use OR logic.
    
    Args:
        args: Parsed command line arguments
        
    Returns:
        List of section names to include, or None if all sections should run
    """
    enabled_sections = []
    
    for flag_name, section_name in SECTION_CHECK_FLAGS.items():
        if getattr(args, flag_name, False):
            enabled_sections.append(section_name)
    
    if enabled_sections:
        # Always include Server Information and Custom Configuration
        # These are represented as 'server-info' and 'custom-config' sections
        if 'server-info' not in enabled_sections:
            enabled_sections.insert(0, 'server-info')
        if 'custom-config' not in enabled_sections:
            enabled_sections.insert(1, 'custom-config')
        return enabled_sections
    
    # No section flags specified - run all sections
    return None


def parse_arguments(args: Optional[List[str]] = None) -> Tuple[argparse.Namespace, argparse.ArgumentParser]:
    """
    Parse command line arguments.
    
    Args:
        args: List of arguments to parse (defaults to sys.argv[1:])
        
    Returns:
        Tuple of (parsed arguments namespace, parser instance)
    """
    parser = create_argument_parser()
    parsed_args = parser.parse_args(args)
    return parsed_args, parser


def validate_arguments(args: argparse.Namespace) -> List[str]:
    """
    Validate parsed arguments for conflicts and constraints.
    
    Args:
        args: Parsed command line arguments
        
    Returns:
        List of validation error messages (empty if valid)
    
    """
    errors = []
    
    # Check for password from environment variable if not provided via CLI
    if not args.password:
        env_password = os.environ.get(PASSWORD_ENV_VAR)
        if env_password:
            args.password = env_password
        else:
            errors.append(
                f"Error: --password is required. You can provide it via:\n"
                f"  1. Command line: --password YOUR_PASSWORD\n"
                f"  2. Environment variable: export {PASSWORD_ENV_VAR}=YOUR_PASSWORD (recommended for security)"
            )
    
    # Validate positive integers
    if args.port <= 0 or args.port > 65535:
        errors.append(f"Error: --port must be between 1 and 65535, got {args.port}")
    
    if args.connect_timeout <= 0:
        errors.append(f"Error: --connect-timeout must be positive, got {args.connect_timeout}")
    
    if args.query_timeout <= 0:
        errors.append(f"Error: --query-timeout must be positive, got {args.query_timeout}")
    
    if args.collection_limit <= 0:
        errors.append(f"Error: --collection-limit must be positive, got {args.collection_limit}")
    
    if args.connection_back_off <= 0:
        errors.append(f"Error: --connection-back-off must be positive, got {args.connection_back_off}")
    
    # 0 disables the check; above 100 could never trigger, since usage is a
    # percentage of max_connections and cannot exceed it.
    if args.connection_usage_back_off < 0 or args.connection_usage_back_off > 100:
        errors.append(
            f"Error: --connection-usage-back-off must be between 0 and 100, "
            f"got {args.connection_usage_back_off}"
        )
    
    if args.objects_back_off <= 0:
        errors.append(f"Error: --objects-back-off must be positive, got {args.objects_back_off}")
    
    return errors


def main_cli(args: Optional[List[str]] = None) -> Tuple[Optional[argparse.Namespace], int]:
    """
    Main CLI entry point that parses and validates arguments.
    
    Args:
        args: List of arguments to parse (defaults to sys.argv[1:])
        
    Returns:
        Tuple of (parsed arguments or None on error, exit code)
        Exit code 0 means success, 1 means invalid arguments
    """
    try:
        parsed_args, parser = parse_arguments(args)
    except SystemExit as e:
        # argparse calls sys.exit on --help, --version, or missing required args
        # Exit with code 1 for invalid/missing arguments
        # --help and --version exit with 0, errors exit with non-zero
        if e.code == 0:
            return None, ExitCode.SUCCESS
        return None, ExitCode.INVALID_ARGUMENTS
    
    # Validate arguments
    errors = validate_arguments(parsed_args)
    
    if errors:
        # Display usage help and exit with code 1
        for error in errors:
            print(error, file=sys.stderr)
        parser.print_usage(sys.stderr)
        return None, ExitCode.INVALID_ARGUMENTS
    
    return parsed_args, ExitCode.SUCCESS


if __name__ == "__main__":
    # For testing the CLI directly
    parsed, exit_code = main_cli()
    if parsed:
        print("Arguments parsed successfully:")
        for key, value in vars(parsed).items():
            print(f"  {key}: {value}")
    sys.exit(exit_code)
