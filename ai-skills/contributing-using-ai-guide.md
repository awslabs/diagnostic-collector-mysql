---
name: contributing-using-ai
description: Guide for contributing to Diagnostic Collector for MySQL with an AI coding assistant. Covers repository layout, the queries.json and report_format.json data model, test and lint commands, code conventions, and how to add a query, a report section, or a CLI option. Use when working on diagnostic_collector_mysql code, adding queries, modifying report sections, fixing bugs, running tests, understanding the architecture, or adding CLI options.
---

# Contributing to Diagnostic Collector for MySQL Using AI

A development guide for AI coding assistants (and humans) contributing to this
repository. Load this file into your assistant's context, or point a
skill-compatible agent at it, to get oriented before making changes.

## Overview

Diagnostic Collector for MySQL is a Python database health check and reporting tool for MySQL 8.0+, Aurora MySQL 8.0+, and MariaDB 11.4+. It connects to a database, executes diagnostic queries, and generates health reports in HTML, JSON, Markdown, or Text format.

This guide covers developing, testing, and maintaining the diagnostic_collector_mysql codebase.

## Project Structure

```
diagnostic_collector_mysql/
├── __init__.py                  # Package exports
├── __main__.py                  # Main entry point (orchestrates the full flow)
├── cli.py                       # CLI argument parsing and validation
├── models.py                    # All data classes and enums (EngineType, ExitCode, QueryDefinition, etc.)
├── core/
│   ├── README.md                # Module documentation
│   ├── connection_manager.py    # Database connection, SSL, engine detection
│   ├── execution_logger.py      # Main execution log file writer
│   └── query_executor.py        # Query execution, back-off checks, filtering, log writing
├── data/
│   ├── README.md                # JSON schema documentation (scaling factors, requires, computed columns)
│   ├── queries.json             # All diagnostic SQL queries (the source of truth)
│   └── report_format.json       # Report structure definition (sections, subsections, columns)
├── output/
│   ├── README.md                # Module documentation
│   └── output_manager.py        # Output directory creation and file path management
├── parsing/
│   ├── README.md                # Module documentation
│   ├── innodb_status_parser.py  # Parses SHOW ENGINE INNODB STATUS output
│   └── input_validator.py       # Validates queries.json and report_format.json before execution
├── reporting/
│   ├── README.md                # Module documentation
│   └── report_generator.py      # HTML, JSON, Markdown, Text report generators
├── utils/
│   ├── README.md                # Module documentation
│   └── validate_format_sync.py  # Validates queries.json ↔ report_format.json sync
├── scripts/
│   └── validate_formats.sh      # Shell script for format validation
└── tests/                       # Test suite (pytest + hypothesis)
```

For detailed documentation on each submodule, see the README.md in each directory. The most important one is `data/README.md` which documents the full JSON schemas for queries and report format.

## Build, Test, and Lint Commands

Always use `uv` to run commands — the project uses `uv` for dependency management with a `pyproject.toml`.

```bash
# Install/sync dependencies
uv sync

# Run the full test suite
uv run pytest diagnostic_collector_mysql/tests/ -v

# Run a specific test file
uv run pytest diagnostic_collector_mysql/tests/test_query_executor.py -v

# Lint (check for relative imports and style violations)
uv run ruff check diagnostic_collector_mysql/

# Auto-fix lint issues
uv run ruff check diagnostic_collector_mysql/ --fix

# Run the collector itself (dry-run mode for testing)
DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD=test uv run python -m diagnostic_collector_mysql --user test --host localhost --dry-run --progress
```

After any code change, always run the test suite and linter to verify:
```bash
uv run pytest diagnostic_collector_mysql/tests/ -v
uv run ruff check diagnostic_collector_mysql/
```

## Architecture & Execution Flow

The main execution flow in `__main__.py` follows these steps:

1. Parse and validate CLI arguments (`cli.py`)
2. Validate input files — `queries.json` and `report_format.json` (`input_validator.py`)
3. Create output directory (`output_manager.py`)
4. If `--dry-run`, display execution plan and exit
5. Initialize execution logger (`execution_logger.py`)
6. Establish database connection and detect engine type (`connection_manager.py`)
7. Execute queries with back-off checks and filtering (`query_executor.py`)
8. Generate report in the requested format (`report_generator.py`)
9. Handle interrupts gracefully — generate partial reports if possible

Exit codes are defined in `models.py::ExitCode`:
- 0: Success
- 1: Invalid arguments
- 2: Connection failure
- 3: Input file error
- 4: Output directory error
- 5: Back-off threshold exceeded
- 6: Partial completion / interrupted

## Key Design Decisions

- **No relative imports** — enforced by ruff rule `TID252`. Always use absolute imports like `from diagnostic_collector_mysql.models import ...`
- **Minimal external dependencies** — only `mysql-connector-python` at runtime. Dev deps: `pytest`, `hypothesis`, `ruff`
- **Python 3.10+** required (per pyproject.toml)
- **Data-driven queries** — all SQL lives in `queries.json`, not in Python code
- **Data-driven report structure** — report layout defined in `report_format.json`, not hardcoded
- **Consolidated query log** — all query output goes to a single `queries-output.log` file
- **Secure file permissions** — output files get `0600` permissions (sensitive data)

## Working with queries.json and report_format.json

For the complete schema documentation including all fields, scaling factors, requires conditions, version filtering, computed columns, fallback sources, and examples, read `diagnostic_collector_mysql/data/README.md`.

The raw data files themselves are `diagnostic_collector_mysql/data/queries.json` and `diagnostic_collector_mysql/data/report_format.json`.

Key concepts at a glance:

- **201 queries** across 16 sections, **181 unique IDs** (duplicates are engine/version variants)
- **Scaling factors** document query cost: `constant` (safe), `tables`, `threads`, `indexes`, `locks`, `transactions` (progressively heavier)
- **Requires conditions** gate queries on runtime MySQL variables (e.g., skip when `performance_schema=0`)
- **Extended checks** gate queries that may cause contention behind `--extended-checks`
- **Computed columns** in report_format.json derive values from query results (e.g., `{"name": "uptime_days", "expr": "Value / 86400"}`)
- **Fallback sources** provide alternative queries when the primary source has no data (e.g., PFS → information_schema)

## Adding a New Diagnostic Section

To add a new diagnostic section:

1. Add SQL queries to `queries.json` with a new section name
2. Add the section and subsections to `report_format.json`
3. Run format validation: `uv run python -m diagnostic_collector_mysql.utils.validate_format_sync`
4. Run tests: `uv run pytest diagnostic_collector_mysql/tests/ -v`
5. Run linter: `uv run ruff check diagnostic_collector_mysql/`

## Adding a New CLI Option

1. Add the argument in `cli.py::create_argument_parser()`
2. Add validation in `cli.py::validate_arguments()` if needed
3. Wire it through `__main__.py::main()` to the appropriate component
4. Add tests in `diagnostic_collector_mysql/tests/test_cli.py`

## Engine Type Detection

The `ConnectionManager` detects the engine type after connecting:
- **Aurora MySQL**: `aurora_version` variable exists
- **MariaDB**: `VERSION()` contains "MariaDB"
- **MySQL**: Default fallback

Minimum supported versions: MySQL 8.0, Aurora MySQL 3.0, MariaDB 11.4.

## Query Filtering Logic

The `QueryExecutor.filter_queries()` method filters queries by:
1. **Section** — matches `--replication-check`, `--performance-check`, etc. When any section flag is set, `server-info` and `custom-config` are always included.
2. **Engine type** — matches the `condition.engine` array against the detected engine
3. **Engine version** — checks `min_engine_version` / `max_engine_version` ranges
4. **Runtime requires** — evaluates MySQL variable conditions at runtime
5. **Extended checks** — skips `requires_extended_checks: true` queries unless `--extended-checks` is set
6. **Objects back-off** — skips `objects` section queries when table count exceeds threshold

## Report Generation

Four report generators exist, each with a partial variant for interrupted runs:
- `HTMLReportGenerator` / `PartialHTMLReportGenerator`
- `JSONReportGenerator` / `PartialJSONReportGenerator`
- `MarkdownReportGenerator` / `PartialMarkdownReportGenerator`
- `TextReportGenerator` / `PartialTextReportGenerator`

Reports include a Health Overview dashboard with automated health checks (redo log occupancy, tables without PK, empty passwords, connection usage, history list length, buffer pool hit rate, replication lag, etc.).

## Testing Guidelines

- Tests live in `diagnostic_collector_mysql/tests/`
- Use `pytest` for all tests
- Property-based tests use `hypothesis` (see `test_config_warnings_pbt.py`, and `test_package_properties.py`, which checks package structure: public exports, absolute imports, sub-package imports, and data file path resolution)
- Tests mock database connections — no real database needed
- Run the full suite before committing: `uv run pytest diagnostic_collector_mysql/tests/ -v`

## Common Patterns

### Reading data files
```python
from diagnostic_collector_mysql.data import get_data_path
path = get_data_path("queries.json")
```

### Creating models
```python
from diagnostic_collector_mysql.models import QueryDefinition, ConnectionConfig, EngineType
```

### Handling errors
Follow the exit code pattern — raise specific exceptions that map to exit codes, catch them in `__main__.py::main()`.
