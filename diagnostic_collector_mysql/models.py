# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Data models for Diagnostic Collector for MySQL.

This module defines all data classes used throughout the application.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


class EngineType(Enum):
    """Database engine type enumeration."""
    MYSQL = "mysql"
    AURORA_MYSQL = "aurora-mysql"
    MARIADB = "mariadb"


class ExitCode:
    """
    Exit codes for Diagnostic Collector for MySQL.

    - Exit 0: Successful completion
    - Exit 1: Invalid command line arguments or conflicting options
    - Exit 2: Database connection failures (including authentication errors)
    - Exit 3: Input file errors (missing, malformed, or invalid queries.json or report_format.json)
    - Exit 4: Output directory errors (cannot create, permission denied)
    - Exit 5: Back-off threshold exceeded (connection count or table count)
    - Exit 6: Partial completion (some queries failed but report was generated) or interrupted
    """
    SUCCESS = 0
    INVALID_ARGUMENTS = 1
    CONNECTION_FAILURE = 2
    INPUT_FILE_ERROR = 3
    OUTPUT_DIRECTORY_ERROR = 4
    BACKOFF_EXCEEDED = 5
    PARTIAL_COMPLETION = 6


# =============================================================================
# Constants
# =============================================================================

# Valid operators for requires condition
VALID_OPERATORS = {'eq', 'ne', 'gt', 'gte', 'lt', 'lte'}

# Query IDs in the health-checks section that the Health Overview evaluator reads
# directly by ID. Every one of these MUST exist in queries.json.
#
# This is enforced at startup by InputValidator.validate_health_check_references,
# because a referenced ID that does not exist fails silently: the evaluator reads
# it through get_val, gets None, and its bare guard skips the whole branch, so the
# check disappears from the report with no error and its absence reads as health.
#
# Health checks that read a non-health query (for example Replication Lag, which
# reads replica_status) are deliberately not listed here. Those queries are
# cross-referenced by report_format.json validation already.
HEALTH_CHECK_QUERY_IDS = frozenset({
    'health_redo_log_occupancy',
    'health_tables_without_pk',
    'health_empty_passwords',
    'health_connection_usage',
    'health_buffer_pool_hit_rate',
    'health_auto_increment_critical',
    'health_replication_errors',
})








# =============================================================================
# Input Validation Models
# =============================================================================

@dataclass
class RequiresCondition:
    """
    Represents a runtime variable condition for query execution.
    
    Attributes:
        variable: MySQL variable name to check (e.g., "performance_schema")
        operator: Comparison operator (eq, ne, gt, gte, lt, lte)
        value: Expected value to compare against
    """
    variable: str
    operator: str = "eq"  # Default: "eq"
    value: str = ""


@dataclass
class QueryDefinition:
    """
    Represents a query definition from queries.json.
    
    Attributes:
        id: Unique identifier used for log file naming and report references
        section: Logical grouping for filtering (replication, performance, etc.)
        command: SQL statement to execute
        condition: Optional engine-specific conditions (e.g., {"engine": ["mysql", "aurora-mysql"]})
        min_engine_version: Optional minimum engine version (e.g., "8.0"). Empty string means no limit.
        max_engine_version: Optional maximum engine version (e.g., "8.4"). Empty string means no limit.
        requires: Optional runtime variable condition for conditional query execution
        requires_extended_checks: Whether this query requires --extended-checks flag to execute
    """
    id: str
    section: str
    command: str
    condition: Optional[Dict[str, List[str]]] = None
    min_engine_version: Optional[str] = None
    max_engine_version: Optional[str] = None
    requires: Optional[RequiresCondition] = None
    requires_extended_checks: bool = False


@dataclass
class ValidationError:
    """
    Represents a validation error found during input file validation.
    
    Attributes:
        file: Name of the file containing the error
        location: Specific location within the file (e.g., "query[3].command")
        message: Human-readable error description
    """
    file: str
    location: str
    message: str


@dataclass
class ComputedColumn:
    """
    Represents a computed column definition in report_format.json.
    
    Attributes:
        name: Display name for the computed column
        expr: Mathematical expression using column names and operators (+, -, *, /)
    """
    name: str
    expr: str


@dataclass
class ReportSubsection:
    """
    Represents a subsection within a report section.
    
    Attributes:
        name: Display name for the subsection
        source: Query ID from queries.json to extract data from
        columns: List of column names or ComputedColumn objects to display
        condition: Optional engine-specific conditions (e.g., {"engine": ["mysql"]})
        min_engine_version: Optional minimum engine version (e.g., "8.4"). Empty string means no limit.
        max_engine_version: Optional maximum engine version (e.g., "8.0"). Empty string means no limit.
        default_note: Optional note to display when query returns no data (instead of "No data returned")
        note: Optional note to display above the data table
        warning: Optional warning to display below the data table
        result_note: Optional note to display below the data table when results are present
        requires_extended_checks: Whether this subsection requires --extended-checks flag
        extended_checks_disabled_note: Note to display when extended checks are disabled
        fallback_source: Optional fallback query ID to use when primary source is not available
        fallback_columns: Optional columns to use with fallback_source
        fallback_requires_extended_checks: Whether the fallback source requires --extended-checks flag
    """
    name: str
    source: str
    columns: List[Union[str, ComputedColumn]]
    condition: Optional[Dict[str, List[str]]] = None
    min_engine_version: Optional[str] = None
    max_engine_version: Optional[str] = None
    default_note: Optional[str] = None
    note: Optional[str] = None
    warning: Optional[str] = None
    result_note: Optional[str] = None
    requires_extended_checks: bool = False
    extended_checks_disabled_note: Optional[str] = None
    fallback_source: Optional[str] = None
    fallback_columns: Optional[List[str]] = None
    fallback_requires_extended_checks: bool = False


@dataclass
class ReportSection:
    """
    Represents a top-level section in the report.
    
    Attributes:
        name: Display name for the section
        subsections: List of subsections within this section
        number: Section number for ordering and display (e.g., "1", "1.5", "2")
        short_name: Short display name for navigation (e.g., "Server Info")
    """
    name: str
    subsections: List[ReportSubsection]
    number: str = ""
    short_name: str = ""


@dataclass
class ReportFormat:
    """
    Represents the complete report format definition from report_format.json.
    
    Attributes:
        sections: List of report sections defining the report structure
    """
    sections: List[ReportSection]


@dataclass
class ValidationResult:
    """
    Result of input file validation.
    
    Attributes:
        success: True if validation passed with no errors
        errors: List of validation errors found
        queries: Parsed query definitions (empty if validation failed)
        report_format: Parsed report format (None if validation failed)
    """
    success: bool
    errors: List[ValidationError]
    queries: List[QueryDefinition] = field(default_factory=list)
    report_format: Optional[ReportFormat] = None


# =============================================================================
# Connection Models
# =============================================================================

@dataclass
class ConnectionConfig:
    """
    Configuration for database connection.
    
    Attributes:
        host: Database hostname
        port: Database port (default: 3306)
        user: Database username
        password: Database password
        require_ssl: Whether to fail the connection if the server does not
                     support SSL/TLS (default: False). Set via
                     --use-encrypted-connection. TLS is always attempted
                     regardless; there is no plaintext opt-out.
        ssl_ca: Path to SSL CA certificate file (optional). When provided,
                the server certificate is verified against this CA.
        connect_timeout: Connection timeout in seconds (default: 10)
        get_warnings: Whether to retrieve warnings from MySQL (default: False)
    """
    host: str
    port: int
    user: str
    password: str
    require_ssl: bool = False
    ssl_ca: Optional[str] = None
    connect_timeout: int = 10
    get_warnings: bool = False


# =============================================================================
# Query Execution Models
# =============================================================================

@dataclass
class QueryResult:
    """
    Result of executing a single query.
    
    Attributes:
        query_id: ID of the executed query
        success: True if query executed successfully
        columns: List of column names (None if query failed)
        rows: List of row tuples (None if query failed)
        error: Error message (None if query succeeded)
        execution_time: Time taken to execute the query in seconds
        log_file: Path to the log file containing the query output
    """
    query_id: str
    success: bool
    columns: Optional[List[str]]
    rows: Optional[List[Tuple[Any, ...]]]
    error: Optional[str]
    execution_time: float
    log_file: str


@dataclass
class ExecutionResult:
    """
    Result of executing all queries.
    
    Attributes:
        total_queries: Total number of queries attempted, satisfying
            successful + failed + cancelled == total_queries
        successful: Number of queries that succeeded
        failed: Number of queries that failed
        cancelled: Number of queries cancelled by an interrupt. Such a query
            was attempted but has no outcome to report, so it is counted
            here rather than as a failure, and is absent from query_results
        query_results: Dictionary mapping query IDs to their results
    """
    total_queries: int
    successful: int
    failed: int
    query_results: Dict[str, QueryResult]
    cancelled: int = 0


class BackOffError(Exception):
    """
    Exception raised when back-off threshold is exceeded.
    
    Attributes:
        reason: Description of why back-off was triggered
        current_value: The current value that triggered the back-off
        threshold: The configured threshold that was exceeded
    """
    
    def __init__(self, reason: str, current_value: int, threshold: int):
        self.reason = reason
        self.current_value = current_value
        self.threshold = threshold
        super().__init__(
            f"{reason}: current value {current_value} exceeds threshold {threshold}"
        )
