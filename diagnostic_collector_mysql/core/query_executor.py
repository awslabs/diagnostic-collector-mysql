# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Query Executor for Diagnostic Collector for MySQL.

This module handles query execution, back-off threshold checks, query filtering,
and writing query results to log files.

Responsibilities:
- Execute each query from queries.json exactly once per run
- When --query-timeout is specified, cancel queries that exceed the timeout duration
- When --query-timeout is not specified, use a default timeout of 30 seconds
- When a query fails, log the error and continue with remaining queries
- Capture query results including column names and row data
- Execute queries without row limits and store full results for logging
- Check current active connection count from processlist (single check only)
- Exit with code 5 when active connections exceed --connection-back-off threshold
- Check total table count in the instance (single check only)
- Exit with code 5 when table count exceeds --objects-back-off threshold
- Display clear message explaining reason, current value, threshold, and how to adjust
- Log all back-off events to main log file
- Do NOT retry or wait when back-off threshold is exceeded
- When --replication-check is specified, execute only queries in the replication section
- When --performance-check is specified, execute only queries in the performance section
- When multiple section-specific check flags are specified, execute queries for all specified sections (OR logic)
- When no section-specific check flag is specified, execute all applicable queries
- Filter queries based on the detected database engine type
- Support queries that apply to all engine types (using engine: "all")
"""

import logging
import math
import os
import time
import warnings
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import mysql.connector

from diagnostic_collector_mysql.models import (
    BackOffError,
    EngineType,
    ExecutionResult,
    QueryDefinition,
    QueryResult,
    RequiresCondition,
)


logger = logging.getLogger(__name__)

# MySQL error raised on a statement cancelled by KILL QUERY. A collection
# interrupt cancels the in-flight statement deliberately, so this is a
# cancellation rather than a query defect.
ER_QUERY_INTERRUPTED = 1317


def normalize_version(version: str) -> Tuple[int, int, int]:
    """
    Normalize a version string to a comparable tuple.
    
    Handles both x.y and x.y.z formats by normalizing x.y to x.y.0.
    Also handles MariaDB version strings like '11.4.8-MariaDB'.
    
    Args:
        version: Version string in x.y or x.y.z format, optionally with suffix
        
    Returns:
        Tuple of (major, minor, patch) integers for comparison
        
    Examples:
        normalize_version("8.0") -> (8, 0, 0)
        normalize_version("8.0.29") -> (8, 0, 29)
        normalize_version("8.4.0") -> (8, 4, 0)
        normalize_version("11.4.8-MariaDB") -> (11, 4, 8)
    """
    # Handle MariaDB and other version strings with suffixes (e.g., "11.4.8-MariaDB")
    # Strip any non-numeric suffix from the version string
    clean_version = version.split('-')[0]  # Remove -MariaDB or similar suffixes
    
    parts = clean_version.split('.')
    major = int(parts[0])
    minor = int(parts[1])
    patch = int(parts[2]) if len(parts) > 2 else 0
    return (major, minor, patch)


# Boolean-like value normalization
TRUTHY_VALUES = {'1', 'ON', 'YES'}
FALSY_VALUES = {'0', 'OFF', 'NO'}


def _normalize_boolean(value: str) -> Optional[str]:
    """
    Normalize boolean-like values to '1' or '0'.
    
    Args:
        value: The value to normalize
        
    Returns:
        '1' for truthy values, '0' for falsy values, None if not boolean-like
    """
    upper = value.upper()
    if upper in TRUTHY_VALUES:
        return '1'
    if upper in FALSY_VALUES:
        return '0'
    return None


def _to_int(value: Any) -> int:
    """
    Coerce a value returned by the connector to an int, defaulting to 0.
    
    Status variable values arrive as strings, and some drivers return bytes or
    Decimal for aggregate columns, so callers cannot assume int.
    
    Args:
        value: Value from a query result row
        
    Returns:
        The value as an int, or 0 if it is None or not numeric
    """
    if value is None:
        return 0
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class QueryExecutor:
    """
    Executes queries and writes results to a consolidated log file.
    
    Performs back-off threshold checks before execution to protect
    the database from additional load when it's already under stress.
    
    Attributes:
        connection: Active MySQL database connection
        output_dir: Directory path for writing query log files
        timestamp: Timestamp string for file naming (YYYYMMDD_HHMMSS format)
        query_timeout: Timeout in seconds for query execution
        connection_threshold: Maximum allowed active connections before back-off
        objects_threshold: Maximum allowed table count before back-off
        logger: Logger instance for recording execution events
        skip_objects_section: Flag indicating objects section should be skipped
        objects_backoff_warning: Warning message when objects threshold exceeded
        extended_checks: Whether extended checks are enabled
    """
    
    # Consolidated log file name
    QUERIES_OUTPUT_FILE = "queries-output.log"
    
    # Connection back-off measurement. One statement covers both guards where
    # performance_schema is available.
    #
    # Two different numbers are read, because the two guards protect against two
    # different things and one count cannot express both:
    #
    #   active_connections  foreground threads currently executing a statement.
    #                       Sleeping sessions are excluded, so this is concurrency.
    #   threads_connected   every client connection, sleeping or not. A parked
    #                       connection still holds a slot, so this is occupancy.
    #
    # active_connections is NOT the figure the report publishes as Current
    # Connections. That comes from connection_memory_overhead in the Memory
    # section and counts all foreground non-daemon threads including sleeping
    # ones, which on a standalone server equals Threads_connected. The report
    # figure that matches this slot guard is Connection Usage in Connection
    # Management, which computes the same ratio.
    #
    # performance_schema.threads is preferred over information_schema.processlist
    # for three reasons: it does not take the global mutex that processlist
    # requires, it exposes the same thread attributes, and it is readable by a user
    # holding only SELECT. MySQL restricts information_schema.processlist to the
    # caller's own threads when the PROCESS privilege is absent, without raising,
    # so that view can report a count of 1 on a fully loaded server.
    #
    # Replication threads are FOREGROUND with a command that is neither Sleep nor
    # Daemon, so they are counted. Verified on 8.0.40: an idle source contributes
    # its Binlog Dump thread and an idle replica its replica_io thread. An idle
    # standalone server reads 0; an idle replica does not.
    #
    # PROCESSLIST_ID != CONNECTION_ID() excludes the collector's own session, which
    # is executing this very statement and would otherwise occupy one unit of the
    # operator's threshold.
    CONNECTION_CHECK_SQL = (
        "SELECT @@performance_schema AS pfs_enabled, "
        "@@max_connections AS max_connections, "
        "(SELECT VARIABLE_VALUE FROM performance_schema.global_status "
        "WHERE VARIABLE_NAME = 'Threads_connected') AS threads_connected, "
        "(SELECT COUNT(*) FROM performance_schema.threads "
        "WHERE TYPE = 'FOREGROUND' "
        "AND PROCESSLIST_COMMAND NOT IN ('Sleep', 'Daemon') "
        "AND PROCESSLIST_ID != CONNECTION_ID()) AS active_connections"
    )
    
    # Used only where performance_schema is disabled, in which case
    # performance_schema.threads returns no rows. This path takes the processlist
    # mutex and under-reports without the PROCESS privilege, so it is a last resort.
    CONNECTION_CHECK_FALLBACK_SQL = (
        "SELECT COUNT(*) FROM information_schema.processlist "
        "WHERE command NOT IN ('Sleep', 'Daemon') AND id != CONNECTION_ID()"
    )
    
    # Read when the combined statement above cannot run at all, which happens for
    # an account with no grant on performance_schema. @@max_connections is always
    # readable, and setting pfs_enabled to 0 routes the rest of the check through
    # the processlist and SHOW GLOBAL STATUS fallbacks, so such an account keeps
    # the concurrency guard it had before performance_schema became the primary
    # source instead of losing both guards.
    CONNECTION_CHECK_DEGRADED_SQL = (
        "SELECT 0 AS pfs_enabled, @@max_connections AS max_connections, "
        "NULL AS threads_connected, 0 AS active_connections"
    )
    
    # Unfiltered processlist row count, used only to decide whether this account
    # can see other sessions at all. MySQL shows an account without PROCESS only
    # its own threads, and raises nothing, so comparing this against
    # Threads_connected is what separates a blind account from a working one.
    # Verified on 8.0.40 with performance_schema off: an account holding only
    # SELECT saw 1 row against Threads_connected 8, while one holding PROCESS saw
    # 9 against 8. The two values are sampled a moment apart, so the comparison
    # has to be "fewer than", never equality.
    PROCESSLIST_VISIBLE_ROWS_SQL = (
        "SELECT COUNT(*) FROM information_schema.processlist"
    )
    
    # Portable read of Threads_connected, for when the subquery above yields
    # nothing. MariaDB ships performance_schema off and then leaves
    # performance_schema.global_status empty, where MySQL keeps it readable:
    # verified on MariaDB 11.4.9 with 26 connections open, the table had 0 rows
    # and the subquery returned NULL, so the slot guard would never have run.
    # SHOW GLOBAL STATUS works on both engines with instrumentation either way.
    # information_schema.global_status would also answer on MariaDB but does not
    # exist on MySQL 8.0, so it is not the portable choice.
    THREADS_CONNECTED_FALLBACK_SQL = (
        "SHOW GLOBAL STATUS LIKE 'Threads_connected'"
    )
    
    def __init__(
        self,
        connection: mysql.connector.MySQLConnection,
        output_dir: str,
        timestamp: str,
        query_timeout: int,
        connection_threshold: int,
        objects_threshold: int,
        logger: logging.Logger,
        extended_checks: bool = False,
        connection_usage_threshold: int = 90,
        interrupt_check: Optional[Callable[[], bool]] = None
    ):
        """
        Initialize QueryExecutor with connection and configuration.
        
        Args:
            connection: Active MySQL database connection
            output_dir: Directory path for writing query log files
            timestamp: Timestamp string for file naming (YYYYMMDD_HHMMSS format)
            query_timeout: Timeout in seconds for query execution (default: 30)
            connection_threshold: Maximum allowed active connections before back-off (default: 1000)
            objects_threshold: Maximum allowed table count before back-off (default: 10000)
            logger: Logger instance for recording execution events
            extended_checks: Whether extended checks are enabled (default: False)
            connection_usage_threshold: Maximum percentage of max_connections in use
                before back-off, or 0 to disable that check (default: 90)
            interrupt_check: Optional callable polled between queries; when it
                returns True, execution stops early with partial results
        """
        self._connection = connection
        self._output_dir = output_dir
        self._timestamp = timestamp
        self._query_timeout = query_timeout
        self._connection_threshold = connection_threshold
        self._objects_threshold = objects_threshold
        self._connection_usage_threshold = connection_usage_threshold
        self._logger = logger
        self._extended_checks = extended_checks
        self._interrupt_check = interrupt_check
        self._interrupted_query_id: Optional[str] = None
        self._skip_objects_section = False
        self._objects_backoff_warning: Optional[str] = None
        self._consolidated_log_path = os.path.join(output_dir, self.QUERIES_OUTPUT_FILE)
        self._variable_cache: Dict[str, str] = {}
        self._skipped_by_requires: int = 0
        self._skipped_by_extended_checks: int = 0
    
    @property
    def skip_objects_section(self) -> bool:
        """Return whether objects section should be skipped due to back-off."""
        return self._skip_objects_section
    
    @property
    def objects_backoff_warning(self) -> Optional[str]:
        """Return the warning message if objects back-off was triggered."""
        return self._objects_backoff_warning
    
    def check_backoff_thresholds(self) -> None:
        """
        Check connection count and table count against configured thresholds.
        
        Performs two checks before query execution:
        1. Active connection count from processlist
        2. Total table count in the instance
        
        Each check is performed exactly once (single query) to minimize
        impact on the database.
        
        Raises:
            BackOffError: If either threshold is exceeded, containing:
                - reason: Description of which threshold was exceeded
                - current_value: The actual value that triggered back-off
                - threshold: The configured threshold value
        
        The error message includes instructions on how to adjust the
        threshold settings to proceed.
        """
        # Check active connection count
        self._check_connection_count()
        
        # Check table count
        self._check_table_count()
    
    def _check_connection_count(self) -> None:
        """
        Check connection load against the configured back-off thresholds.
        
        Two independent guards:
        
        1. Concurrency: foreground non-daemon connections currently executing,
           excluding the collector's own session, against --connection-back-off.
        2. Slot pressure: Threads_connected as a percentage of max_connections,
           against --connection-usage-back-off. Parked connections hold a slot
           without executing anything, so they are invisible to the first guard
           and are the reason this second one exists.
        
        One statement covers both where performance_schema is available. Where it
        is disabled the concurrency count falls back to
        information_schema.processlist and a second statement measures whether
        this account can see other sessions at all, and on MariaDB
        Threads_connected needs a third through SHOW GLOBAL STATUS. Checked once
        per run either way, with no retry.
        
        Raises:
            BackOffError: If either threshold is exceeded
        """
        cursor = None
        try:
            cursor = self._connection.cursor()
            try:
                cursor.execute(self.CONNECTION_CHECK_SQL)
                row = cursor.fetchone()
            except mysql.connector.Error as err:
                # An account with no grant on performance_schema cannot run the
                # combined statement at all. Reading nothing would disable both
                # guards, where the pre-change implementation queried the
                # processlist directly and so still guarded concurrency. Drop to a
                # read that cannot fail and let the fallbacks below do the work.
                self._logger.warning(
                    f"Could not read the combined connection check: {err}. "
                    f"Falling back to information_schema.processlist and "
                    f"SHOW GLOBAL STATUS, which needs no performance_schema access."
                )
                cursor.execute(self.CONNECTION_CHECK_DEGRADED_SQL)
                row = cursor.fetchone()
            
            if not row:
                self._logger.warning(
                    "Connection back-off check returned no result. "
                    "Back-off protection is DISABLED for this run."
                )
                return
            
            pfs_enabled = _to_int(row[0])
            max_connections = _to_int(row[1])
            threads_connected = _to_int(row[2])
            active_connections = _to_int(row[3])
            
            # Resolve Threads_connected before anything reads it. The subquery
            # above returns NULL on MariaDB with performance_schema off, because
            # MariaDB leaves performance_schema.global_status empty where MySQL
            # keeps it populated. A zero cannot be true while this session is
            # connected, so treat it as unread rather than as an idle server.
            if threads_connected <= 0:
                cursor.execute(self.THREADS_CONNECTED_FALLBACK_SQL)
                status_row = cursor.fetchone()
                if status_row is not None and len(status_row) > 1:
                    threads_connected = _to_int(status_row[1])
            
            if pfs_enabled != 1:
                # performance_schema.threads yields no rows when the instrumentation
                # flag is off, so the count above would read 0 regardless of load.
                cursor.execute(self.CONNECTION_CHECK_FALLBACK_SQL)
                fallback_row = cursor.fetchone()
                active_connections = _to_int(fallback_row[0]) if fallback_row else 0
                
                # Warn only when the account genuinely cannot see other sessions.
                # Warning whenever performance_schema is off fires on every MariaDB
                # run and for every account the documented minimum grants create,
                # which include PROCESS and therefore count correctly.
                cursor.execute(self.PROCESSLIST_VISIBLE_ROWS_SQL)
                visible_row = cursor.fetchone()
                visible_rows = _to_int(visible_row[0]) if visible_row else 0
                
                if threads_connected > 0 and visible_rows < threads_connected:
                    self._logger.warning(
                        f"This account can see only {visible_rows} of "
                        f"{threads_connected} connections in "
                        f"information_schema.processlist, so the active connection "
                        f"count under-reports and the concurrency guard is "
                        f"ineffective. MySQL restricts that view to the caller's "
                        f"own threads when the PROCESS privilege is absent, and "
                        f"raises nothing. Granting PROCESS fixes it from the next "
                        f"run, with no restart "
                        f"(GRANT PROCESS ON *.* TO '<user>'@'<host>'). Enabling "
                        f"performance_schema is the better steady state, but it is "
                        f"a read-only variable set at server startup, so it needs a "
                        f"server restart, or a parameter group change and reboot on "
                        f"RDS and Aurora."
                    )
                else:
                    self._logger.debug(
                        f"performance_schema is disabled, so the active connection "
                        f"count comes from information_schema.processlist. This "
                        f"account sees {visible_rows} of {threads_connected} "
                        f"connections, so the count is usable."
                    )
            
            self._logger.debug(
                f"Active connection count: {active_connections} "
                f"(threshold: {self._connection_threshold}); "
                f"connections in use: {threads_connected} of {max_connections}"
            )
            
            # Guard 1: concurrency
            if active_connections > self._connection_threshold:
                reason = "Active connection count exceeds threshold"
                self._logger.warning(
                    f"Back-off triggered: {reason}. "
                    f"Current: {active_connections}, Threshold: {self._connection_threshold}"
                )
                raise BackOffError(
                    reason=reason,
                    current_value=active_connections,
                    threshold=self._connection_threshold
                )
            
            # Guard 2: connection slot pressure. Expressed as a percentage so it is
            # meaningful without per-instance tuning, unlike an absolute count that
            # has to be set against each instance's max_connections.
            if self._connection_usage_threshold <= 0:
                slot_summary = (
                    "slot usage check disabled (--connection-usage-back-off 0)"
                )
            elif threads_connected <= 0 or max_connections <= 0:
                self._logger.warning(
                    f"Could not read connection slot usage "
                    f"(connected={threads_connected}, "
                    f"max_connections={max_connections}). Slot usage back-off "
                    f"protection is DISABLED for this run."
                )
                slot_summary = "slot usage check SKIPPED, it could not be evaluated"
            else:
                # Compare in integers rather than rounding first. Rounding let a
                # real breach pass: 904 of 1000 is 90.40% and 181 of 200 is
                # 90.50%, and both rounded to 90 and passed a threshold of 90, the
                # second because Python rounds halves to even. The help text
                # promises "more than PCT percent", so the comparison has to mean
                # exactly that. max_connections is 1000 on an Aurora db.r6g.large,
                # so the 904 case is reachable rather than contrived.
                exceeded = (
                    threads_connected * 100
                    > self._connection_usage_threshold * max_connections
                )
                usage_pct = threads_connected * 100 / max_connections
                slot_summary = (
                    f"{threads_connected} of {max_connections} slots in use "
                    f"({usage_pct:.1f}%, threshold: "
                    f"{self._connection_usage_threshold}%)"
                )
                if exceeded:
                    reason = "Connection slot usage exceeds threshold"
                    self._logger.warning(
                        f"Back-off triggered: {reason}. "
                        f"Current: {usage_pct:.1f}% ({threads_connected} of "
                        f"{max_connections}), "
                        f"Threshold: {self._connection_usage_threshold}%"
                    )
                    raise BackOffError(
                        reason=reason,
                        # The measured percentage, so the operator is told what was
                        # actually observed. format_backoff_message rounds it up
                        # only to suggest an integer threshold, which is a
                        # different job from reporting the reading.
                        current_value=usage_pct,
                        threshold=self._connection_usage_threshold
                    )
            
            self._logger.info(
                f"Connection back-off checks passed: {active_connections} active "
                f"(threshold: {self._connection_threshold}), {slot_summary}"
            )
            
        except mysql.connector.Error as err:
            # Deliberately non-fatal. A least-privilege or resource-limited account
            # is a normal way to run a read-only diagnostic, and refusing to run at
            # all would be worse than running unguarded. The trade-off is stated
            # explicitly so an operator can see that the guard did not apply.
            self._logger.warning(
                f"Could not evaluate connection back-off thresholds: {err}. "
                f"Back-off protection is DISABLED for this run; proceeding with "
                f"query execution."
            )
        finally:
            if cursor is not None:
                cursor.close()
    
    def _check_table_count(self) -> None:
        """
        Check total table count against --objects-back-off threshold.
        
        Queries information_schema.tables to get the total table count.
        This is a single check only - no retries.
        
        When threshold is exceeded, sets skip_objects_section flag and logs
        a warning instead of raising BackOffError. This allows the tool to
        continue with other sections while skipping objects-related queries.
        """
        cursor = None
        try:
            cursor = self._connection.cursor()
            
            # Query total table count from information_schema
            # Excludes system schemas for accurate user table count
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema NOT IN ('information_schema', 'mysql', "
                "'performance_schema', 'sys')"
            )
            result = cursor.fetchone()
            table_count = result[0] if result else 0
            
            self._logger.debug(
                f"Table count: {table_count} "
                f"(threshold: {self._objects_threshold})"
            )
            
            # Check against threshold
            if table_count > self._objects_threshold:
                # Set flag to skip objects section instead of raising error
                self._skip_objects_section = True
                self._objects_backoff_warning = (
                    f"Objects back-off threshold exceeded. "
                    f"Table count: {table_count}, Threshold: {self._objects_threshold}. "
                    f"Skipping 'objects' section queries. "
                    f"To include objects section, increase threshold with "
                    f"--objects-back-off={table_count + 100}"
                )
                self._logger.warning(self._objects_backoff_warning)
            else:
                self._logger.info(
                    f"Table count check passed: {table_count} tables "
                    f"(threshold: {self._objects_threshold})"
                )
            
        except mysql.connector.Error as err:
            # Log the error but don't fail - allow execution to continue
            # This handles cases where information_schema access might be restricted
            self._logger.warning(
                f"Could not check table count: {err}. "
                f"Proceeding with query execution."
            )
        finally:
            if cursor is not None:
                cursor.close()

    def _get_runtime_variable(self, variable_name: str) -> Optional[str]:
        """
        Get a MySQL runtime variable value, using cache if available.

        Args:
            variable_name: Name of the MySQL variable (without @@ prefix)

        Returns:
            Variable value as string, or None if variable cannot be queried
        """
        # Check cache first
        if variable_name in self._variable_cache:
            self._logger.debug(
                f"Using cached value for variable '{variable_name}': "
                f"{self._variable_cache[variable_name]}"
            )
            return self._variable_cache[variable_name]

        # Query the variable from MySQL
        cursor = None
        try:
            cursor = self._connection.cursor()
            cursor.execute(f"SELECT @@{variable_name}")
            result = cursor.fetchone()

            if result is not None:
                value = str(result[0])
                self._variable_cache[variable_name] = value
                self._logger.debug(
                    f"Queried variable '{variable_name}': {value}"
                )
                return value
            else:
                self._logger.warning(
                    f"Variable '{variable_name}' returned no result"
                )
                return None

        except mysql.connector.Error as err:
            self._logger.warning(
                f"Could not query variable '{variable_name}': {err}"
            )
            return None
        except Exception as err:
            self._logger.warning(
                f"Unexpected error querying variable '{variable_name}': {err}"
            )
            return None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

    def _compare_values(
        self,
        actual: str,
        operator: str,
        expected: str,
        query_id: str = ""
    ) -> bool:
        """
        Compare actual value against expected using the specified operator.

        Handles:
        - Boolean normalization for eq/ne operators
        - Numeric comparison for gt/gte/lt/lte operators
        - Case-insensitive string comparison

        Args:
            actual: The actual value from MySQL variable
            operator: Comparison operator (eq, ne, gt, gte, lt, lte)
            expected: The expected value to compare against
            query_id: Query ID for logging (optional)

        Returns:
            True if comparison passes, False otherwise
        """
        if operator == "eq":
            # Try boolean normalization first
            actual_bool = _normalize_boolean(actual)
            expected_bool = _normalize_boolean(expected)

            if actual_bool is not None and expected_bool is not None:
                # Both values normalize to boolean, compare normalized values
                return actual_bool == expected_bool

            # Otherwise, do case-insensitive string comparison
            return actual.upper() == expected.upper()

        elif operator == "ne":
            # Same as eq but return opposite result
            actual_bool = _normalize_boolean(actual)
            expected_bool = _normalize_boolean(expected)

            if actual_bool is not None and expected_bool is not None:
                # Both values normalize to boolean, compare normalized values
                return actual_bool != expected_bool

            # Otherwise, do case-insensitive string comparison
            return actual.upper() != expected.upper()

        elif operator in ("gt", "gte", "lt", "lte"):
            # Try to convert both values to float for numeric comparison
            try:
                actual_num = float(actual)
            except (ValueError, TypeError):
                self._logger.warning(
                    f"Cannot perform numeric comparison for query '{query_id}': "
                    f"actual value '{actual}' is not numeric"
                )
                return False

            try:
                expected_num = float(expected)
            except (ValueError, TypeError):
                self._logger.warning(
                    f"Cannot perform numeric comparison for query '{query_id}': "
                    f"expected value '{expected}' is not numeric"
                )
                return False

            # Perform numeric comparison
            if operator == "gt":
                return actual_num > expected_num
            elif operator == "gte":
                return actual_num >= expected_num
            elif operator == "lt":
                return actual_num < expected_num
            elif operator == "lte":
                return actual_num <= expected_num

        # Unknown operator - should not happen if validation is correct
        self._logger.warning(
            f"Unknown operator '{operator}' for query '{query_id}'"
        )
        return False

    def _check_requires_condition(
        self,
        requires: 'RequiresCondition',
        query_id: str
    ) -> bool:
        """
        Check if a requires condition is satisfied.
        
        Args:
            requires: The RequiresCondition to evaluate
            query_id: Query ID for logging
            
        Returns:
            True if condition is met, False otherwise
        """
        # Get the runtime variable value
        actual = self._get_runtime_variable(requires.variable)
        
        # If variable value is None (couldn't be queried), return False
        if actual is None:
            return False
        
        # Use default operator "eq" if not specified
        operator = requires.operator if requires.operator else "eq"
        
        # Compare using _compare_values
        result = self._compare_values(actual, operator, requires.value, query_id)
        
        # Log debug message when condition not met
        if not result:
            self._logger.debug(
                f"Query '{query_id}' requires condition not met: "
                f"variable '{requires.variable}' {operator} '{requires.value}' "
                f"(actual value: '{actual}')"
            )
        
        return result

    def execute_query(self, query: QueryDefinition) -> QueryResult:
        """
        Execute a single query and capture results.
        
        Executes the query with the configured timeout, captures column names
        and all row data without any row limits.
        
        Args:
            query: QueryDefinition object containing the query to execute
            
        Returns:
            QueryResult containing:
                - query_id: ID of the executed query
                - success: True if query executed successfully
                - columns: List of column names (None if query failed)
                - rows: List of row tuples (None if query failed)
                - error: Error message (None if query succeeded)
                - execution_time: Time taken to execute in seconds
                - log_file: Path to the consolidated log file
        
        Behavior:
            - Execute each query exactly once
            - Cancel queries that exceed timeout duration
            - Use default timeout of 30 seconds if not specified
            - Capture query results including column names and row data
            - Execute queries without row limits
        """
        start_time = time.time()
        cursor = None
        columns = None
        rows = None
        error = None
        success = False
        
        try:
            # Create cursor with timeout
            cursor = self._connection.cursor()
            
            # Set query timeout at session level
            # MySQL uses seconds for wait_timeout and max_execution_time
            try:
                cursor.execute(f"SET SESSION max_execution_time = {self._query_timeout * 1000}")
            except mysql.connector.Error:
                # max_execution_time may not be available on all MySQL versions
                # Fall back to no timeout enforcement at MySQL level
                self._logger.debug(
                    f"Could not set max_execution_time for query {query.id}, "
                    f"timeout will be enforced at connector level only"
                )
            
            self._logger.info(f"Executing query: {query.id}")
            self._logger.debug(f"Query command: {query.command}")
            
            # Execute the query
            cursor.execute(query.command)
            
            # Capture column names
            if cursor.description:
                columns = [desc[0] for desc in cursor.description]
            
            # Capture all rows without limit
            # Suppress MySQL connector UserWarnings from stdout and log them instead
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                rows = cursor.fetchall()
                
                # Log any Python warnings (e.g., deprecation warnings from mysql-connector)
                for w in caught_warnings:
                    self._logger.warning(f"MySQL warning for query {query.id}: {w.message}")
            
            # Also capture MySQL server warnings via cursor.fetchwarnings()
            mysql_warnings = cursor.fetchwarnings()
            if mysql_warnings:
                for warning in mysql_warnings:
                    # warning is a tuple: (level, code, message)
                    self._logger.warning(f"MySQL server warning for query {query.id}: {warning[2]}")
            
            success = True
            self._logger.info(
                f"Query {query.id} completed successfully: "
                f"{len(rows) if rows else 0} rows returned"
            )
            
        except mysql.connector.Error as err:
            # Capture error message
            error = f"{err.errno} ({err.sqlstate}): {err.msg}" if hasattr(err, 'errno') else str(err)
            interrupt_requested = (
                self._interrupt_check is not None and self._interrupt_check()
            )
            if (getattr(err, 'errno', None) == ER_QUERY_INTERRUPTED
                    and interrupt_requested):
                # The statement was cancelled on purpose, by the KILL QUERY
                # an interrupt issues. That is not a query defect, so it is
                # logged as a cancellation and kept out of the failure
                # accounting by execute_queries(). The interrupt check
                # matters: 1317 is also raised when another session kills
                # the statement, which is a real failure to report.
                self._interrupted_query_id = query.id
                self._logger.info(
                    f"Query {query.id} was cancelled on request: {error}"
                )
            else:
                self._logger.error(f"Query {query.id} failed: {error}")
            
        except Exception as err:
            # Capture unexpected errors
            error = str(err)
            self._logger.error(f"Query {query.id} failed with unexpected error: {error}")
            
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
        
        execution_time = time.time() - start_time
        
        # Append query output to consolidated log file. A statement cancelled
        # by our own interrupt is skipped: it is excluded from the results
        # and the counts, and the report generator parses this file
        # independently, so a block here would be re-read as a query outcome
        # the run deliberately discarded. The cancellation is recorded in the
        # execution log instead.
        if self._interrupted_query_id != query.id:
            self._append_query_log(query, columns, rows, error)
        
        return QueryResult(
            query_id=query.id,
            success=success,
            columns=columns,
            rows=rows,
            error=error,
            execution_time=execution_time,
            log_file=self._consolidated_log_path
        )
    
    def _append_query_log(
        self,
        query: QueryDefinition,
        columns: Optional[List[str]],
        rows: Optional[List[tuple]],
        error: Optional[str]
    ) -> None:
        """
        Append query output to the consolidated log file.
        
        Appends to a single queries-output.log file with the query execution 
        timestamp, command, and results or error message.
        
        Sets restrictive file permissions (0600) for security since output
        may contain sensitive database information.
        
        Args:
            query: The executed QueryDefinition
            columns: List of column names (None if query failed)
            rows: List of row tuples (None if query failed)
            error: Error message (None if query succeeded)
        """
        try:
            with open(self._consolidated_log_path, 'a', encoding='utf-8') as f:
                # Write header
                f.write("=" * 80 + "\n")
                f.write(f"Query: {query.id}\n")
                f.write(f"Executed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 80 + "\n")
                
                if error:
                    # Write error message
                    f.write(f"ERROR: {error}\n")
                else:
                    # Write SELECT NOW() timestamp
                    f.write(f"SELECT NOW(): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write("-" * 80 + "\n")
                    
                    # Write query command
                    f.write(f"{query.command}\n")
                    f.write("-" * 80 + "\n")
                    
                    # Write results
                    if columns:
                        f.write("\t".join(columns) + "\n")
                    
                    if rows:
                        for row in rows:
                            # Convert row values to strings, handling None
                            row_str = "\t".join(
                                str(val) if val is not None else "NULL"
                                for val in row
                            )
                            f.write(row_str + "\n")
                
                # Add separator between queries
                f.write("\n")
            
            # Set restrictive file permissions (0600) for security
            try:
                import stat
                os.chmod(self._consolidated_log_path, stat.S_IRUSR | stat.S_IWUSR)
            except (OSError, AttributeError):
                pass  # Best effort - may fail on Windows or if not owner
                    
            self._logger.debug(f"Query {query.id} appended to: {self._consolidated_log_path}")
            
        except IOError as err:
            self._logger.error(f"Failed to append query log for {query.id}: {err}")
    
    def execute_queries(
        self,
        queries: List[QueryDefinition],
        engine_type: EngineType,
        engine_version: Optional[str] = None,
        sections: Optional[Set[str]] = None
    ) -> ExecutionResult:
        """
        Execute all filtered queries and return aggregated results.
        
        Performs back-off threshold checks, filters queries by engine type,
        version, and sections, then executes each filtered query exactly once.
        Continues execution even if individual queries fail.
        
        Args:
            queries: List of QueryDefinition objects to potentially execute
            engine_type: The detected database engine type for filtering
            engine_version: The detected database engine version (e.g., "8.0.29")
            sections: Optional set of section names to filter by. If None or empty,
                     all sections are included.
        
        Returns:
            ExecutionResult containing:
                - total_queries: Number of queries attempted, satisfying
                  successful + failed + cancelled == total_queries
                - successful: Number of queries that succeeded
                - failed: Number of queries that failed
                - cancelled: Number cancelled by an interrupt; attempted but
                  with no outcome, so absent from query_results
                - query_results: Dictionary mapping query IDs to their results
        
        Raises:
            BackOffError: If back-off thresholds are exceeded
        
        Behavior:
            - Execute each query exactly once per run
            - When a query fails, log the error and continue with remaining queries
            - Filter queries by section
            - Filter queries by engine type
        """
        # Check back-off thresholds first
        self.check_backoff_thresholds()
        
        # Filter queries by engine type, version, and sections
        filtered_queries = self.filter_queries(queries, engine_type, engine_version, sections)
        
        self._logger.info(
            f"Starting execution of {len(filtered_queries)} queries"
        )
        
        # Reset per-run interrupt state so a reused executor cannot inherit
        # a cancellation from an earlier run.
        self._interrupted_query_id = None
        
        query_results = {}
        successful = 0
        failed = 0
        cancelled = 0
        
        # Execute each query exactly once
        for query in filtered_queries:
            # Stop early when an interrupt was requested. The
            # results gathered so far feed the partial report.
            if self._interrupt_check is not None and self._interrupt_check():
                remaining = len(filtered_queries) - len(query_results)
                self._logger.info(
                    f"Interrupt requested: stopping query execution with "
                    f"{remaining} of {len(filtered_queries)} queries not run"
                )
                break
            
            result = self.execute_query(query)
            
            if self._interrupted_query_id == query.id:
                # This statement was cancelled by the interrupt's KILL
                # QUERY, not by a defect. It was attempted, so it counts
                # toward the attempt total, but it is left out of the
                # results and of success/failure: there is no outcome to
                # report and it is not a defect. Then stop.
                cancelled += 1
                self._logger.info(
                    f"Query {query.id} cancelled by interrupt: stopping "
                    f"query execution"
                )
                break
            
            query_results[query.id] = result
            
            if result.success:
                successful += 1
            else:
                failed += 1
                # Continue with remaining queries
                self._logger.info(
                    f"Continuing with remaining queries after failure of {query.id}"
                )
        
        attempted = len(query_results) + cancelled
        self._logger.info(
            f"Query execution complete: {successful} successful, {failed} failed"
            f"{f', {cancelled} cancelled' if cancelled else ''} "
            f"out of {attempted} attempted "
            f"({len(filtered_queries)} planned)"
        )
        
        return ExecutionResult(
            total_queries=attempted,
            successful=successful,
            failed=failed,
            query_results=query_results,
            cancelled=cancelled
        )

    def filter_queries(
        self,
        queries: List[QueryDefinition],
        engine_type: EngineType,
        engine_version: Optional[str] = None,
        sections: Optional[Set[str]] = None
    ) -> List[QueryDefinition]:
        """
        Filter queries by engine type, version, and sections.
        
        Implements query filtering based on:
        1. Engine type matching - queries with condition["engine"] must include the
           detected engine type. Queries without condition run on all engines.
        2. Version filtering - queries with min_engine_version/max_engine_version
           are filtered based on the detected version. Empty string means no constraint.
        3. Section filtering - when section flags are specified, only queries
           in those sections are included (OR logic)
        4. When no sections specified, all applicable queries are returned
        5. Objects section exclusion - when objects back-off threshold is exceeded,
           queries in the "objects" section are excluded
        
        Args:
            queries: List of QueryDefinition objects to filter
            engine_type: The detected database engine type (mysql, aurora-mysql, mariadb)
            engine_version: The detected database engine version (e.g., "8.0.29")
            sections: Optional set of section names to filter by. If None or empty,
                     all sections are included.
        
        Returns:
            List of QueryDefinition objects that match the filtering criteria
        
        Examples:
            # Filter for MySQL 8.0.29, replication section only
            filtered = executor.filter_queries(queries, EngineType.MYSQL, "8.0.29", {"replication"})
            
            # Filter for Aurora MySQL 8.0, multiple sections (OR logic)
            filtered = executor.filter_queries(queries, EngineType.AURORA_MYSQL, "8.0.32",
                                               {"replication", "performance"})
            
            # Filter for MariaDB, all sections
            filtered = executor.filter_queries(queries, EngineType.MARIADB, "10.6.12", None)
        """
        filtered = []
        engine_value = engine_type.value  # Get string value from enum
        objects_skipped_count = 0
        self._skipped_by_requires = 0  # Reset counter for requires condition filtering
        
        # Normalize detected version for comparison if provided
        detected_version_tuple = None
        if engine_version:
            try:
                detected_version_tuple = normalize_version(engine_version)
            except (ValueError, IndexError):
                self._logger.warning(
                    f"Could not parse detected engine version '{engine_version}', "
                    f"version filtering will be skipped"
                )
        
        for query in queries:
            # Check engine type match using condition field
            # If no condition is specified, query runs on ALL engines (default behavior)
            engine_matches = True
            if query.condition is not None and 'engine' in query.condition:
                engine_list = query.condition['engine']
                engine_matches = engine_value in engine_list
            
            if not engine_matches:
                self._logger.debug(
                    f"Query {query.id} filtered out: engine '{engine_value}' "
                    f"not in condition engines {query.condition['engine']}"
                )
                continue
            
            # Check version constraints if detected version is available
            version_matches = True
            if detected_version_tuple is not None:
                # Check min_engine_version constraint
                if query.min_engine_version and query.min_engine_version != "":
                    try:
                        min_version_tuple = normalize_version(query.min_engine_version)
                        if detected_version_tuple < min_version_tuple:
                            version_matches = False
                            self._logger.debug(
                                f"Query {query.id} filtered out: detected version "
                                f"'{engine_version}' < min_engine_version '{query.min_engine_version}'"
                            )
                    except (ValueError, IndexError):
                        self._logger.warning(
                            f"Could not parse min_engine_version '{query.min_engine_version}' "
                            f"for query {query.id}, skipping version check"
                        )
                
                # Check max_engine_version constraint
                if version_matches and query.max_engine_version and query.max_engine_version != "":
                    try:
                        max_version_tuple = normalize_version(query.max_engine_version)
                        if detected_version_tuple > max_version_tuple:
                            version_matches = False
                            self._logger.debug(
                                f"Query {query.id} filtered out: detected version "
                                f"'{engine_version}' > max_engine_version '{query.max_engine_version}'"
                            )
                    except (ValueError, IndexError):
                        self._logger.warning(
                            f"Could not parse max_engine_version '{query.max_engine_version}' "
                            f"for query {query.id}, skipping version check"
                        )
            
            if not version_matches:
                continue
            
            # Check requires condition (if present)
            if query.requires is not None:
                if not self._check_requires_condition(query.requires, query.id):
                    self._skipped_by_requires += 1
                    continue  # Skip this query
            
            # Check if query requires extended checks and they are disabled
            if query.requires_extended_checks and not self._extended_checks:
                self._skipped_by_extended_checks += 1
                self._logger.debug(
                    f"Query {query.id} filtered out: requires extended checks "
                    f"but --extended-checks is not enabled"
                )
                continue
            
            # Check if objects section should be skipped due to back-off
            if self._skip_objects_section and query.section == "objects":
                objects_skipped_count += 1
                self._logger.debug(
                    f"Query {query.id} filtered out: objects section skipped "
                    f"due to back-off threshold"
                )
                continue
            
            # Check section match
            # If no sections specified, include all queries
            # If sections specified, query must be in one of them (OR logic)
            if sections is not None and len(sections) > 0:
                section_matches = query.section in sections
                if not section_matches:
                    self._logger.debug(
                        f"Query {query.id} filtered out: section '{query.section}' "
                        f"not in requested sections {sections}"
                    )
                    continue
            
            # Query passes all filters
            filtered.append(query)
            condition_info = f"condition={query.condition}" if query.condition else "no condition (all engines)"
            self._logger.debug(
                f"Query {query.id} included: {condition_info}, "
                f"section='{query.section}'"
            )
        
        # Log objects section skip summary if applicable
        if objects_skipped_count > 0:
            self._logger.warning(
                f"Skipped {objects_skipped_count} queries from 'objects' section "
                f"due to objects back-off threshold"
            )
        
        if self._skipped_by_requires > 0:
            self._logger.info(
                f"Skipped {self._skipped_by_requires} queries due to runtime variable conditions"
            )
        
        if self._skipped_by_extended_checks > 0:
            self._logger.info(
                f"Skipped {self._skipped_by_extended_checks} queries requiring extended checks "
                f"(use --extended-checks to enable)"
            )
        
        self._logger.info(
            f"Query filtering complete: {len(filtered)} of {len(queries)} queries "
            f"selected for engine '{engine_value}'"
            + (f" version '{engine_version}'" if engine_version else "")
            + (f" with sections {sections}" if sections else " (all sections)")
        )
        
        return filtered


def format_backoff_message(error: BackOffError) -> str:
    """
    Format a BackOffError into a user-friendly message.
    
    Creates a clear message explaining the reason for back-off,
    the current value, the threshold, and how to adjust settings
    to proceed.
    
    Args:
        error: BackOffError containing reason, current_value, and threshold
        
    Returns:
        Formatted message string suitable for display to the user
    """
    if "slot usage" in error.reason.lower():
        # Percentage-based guard. current_value carries the measured percentage, so
        # it is displayed as measured; the integer the flag needs is derived here,
        # rounded up so that raising the threshold to it actually lets the run
        # proceed. Raising past 100 disables the guard rather than relaxing it, so
        # the suggestion is capped.
        threshold_flag = "--connection-usage-back-off"
        suggested = min(math.ceil(error.current_value) + 5, 100)
        return (
            f"\n"
            f"Back-off threshold exceeded - execution aborted to protect database.\n"
            f"\n"
            f"Reason: {error.reason}\n"
            f"Current connection slot usage: {error.current_value:.1f}%\n"
            f"Configured threshold: {error.threshold}%\n"
            f"\n"
            f"To proceed, you can:\n"
            f"  1. Wait for connections to be released, or find the sessions holding\n"
            f"     slots. An application pool that never closes connections is the\n"
            f"     usual cause. Raising max_connections frees no slot, it only moves\n"
            f"     the denominator, and it raises the instance's memory ceiling\n"
            f"  2. Raise the threshold using {threshold_flag}={suggested}, or 0 to\n"
            f"     disable the guard for this run\n"
            f"\n"
            f"Example: diagnostic_collector_mysql {threshold_flag} {suggested} [other options]\n"
        )
    
    if "connection" in error.reason.lower():
        threshold_flag = "--connection-back-off"
        resource_type = "active connections"
    else:
        threshold_flag = "--objects-back-off"
        resource_type = "tables"
    
    return (
        f"\n"
        f"Back-off threshold exceeded - execution aborted to protect database.\n"
        f"\n"
        f"Reason: {error.reason}\n"
        f"Current {resource_type}: {error.current_value}\n"
        f"Configured threshold: {error.threshold}\n"
        f"\n"
        f"To proceed, you can:\n"
        f"  1. Wait for the database load to decrease\n"
        f"  2. Increase the threshold using {threshold_flag}={error.current_value + 100}\n"
        f"\n"
        f"Example: diagnostic_collector_mysql {threshold_flag} {error.current_value + 100} [other options]\n"
    )
