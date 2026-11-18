# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Diagnostic Collector for MySQL - Database Health Check and Reporting Tool

A pure Python tool that connects to MySQL/Aurora MySQL/MariaDB databases,
executes diagnostic queries, and generates formatted reports in multiple
output formats (HTML, Markdown, Text, JSON).

Design Principles:
- Minimize external library dependencies
- Use Python standard library where possible
- Keep code efficient and avoid excessive abstraction
- Support Python 3.10 and later
"""

from diagnostic_collector_mysql.models import (
    # Enums
    EngineType,
    # Input Validation Models
    QueryDefinition,
    ValidationError,
    ValidationResult,
    ComputedColumn,
    ReportSubsection,
    ReportSection,
    ReportFormat,
    # Connection Models
    ConnectionConfig,
    # Query Execution Models
    QueryResult,
    ExecutionResult,
    BackOffError,
)
from diagnostic_collector_mysql.core.execution_logger import ExecutionLogger

__version__ = "1.1.0"
__all__ = [
    # Version
    "__version__",
    # Enums
    "EngineType",
    # Input Validation Models
    "QueryDefinition",
    "ValidationError",
    "ValidationResult",
    "ComputedColumn",
    "ReportSubsection",
    "ReportSection",
    "ReportFormat",
    # Connection Models
    "ConnectionConfig",
    # Query Execution Models
    "QueryResult",
    "ExecutionResult",
    "BackOffError",
    # Logging
    "ExecutionLogger",
]
