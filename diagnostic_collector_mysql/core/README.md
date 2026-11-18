# diagnostic_collector_mysql/core/

Database connection, query execution, and execution logging: the runtime engine of the collector.

## Modules

### connection_manager.py

Manages the database connection lifecycle:
- Establishes connections with SSL/TLS support and authentication plugin handling
- Detects the engine type after connecting (Aurora MySQL, MariaDB, or MySQL) by checking `aurora_version` variable and `VERSION()` string
- Validates minimum supported versions: MySQL 8.0, Aurora MySQL 3.0, MariaDB 11.4
- Provides descriptive error messages for common failure modes (auth errors, SSL issues, unreachable hosts)

### query_executor.py

Executes diagnostic queries and writes results to a consolidated log file (`queries-output.log`):
- Performs back-off threshold checks (connection count, table count) before execution
- Filters queries by engine type, version range, section flags, `requires` conditions, and `--extended-checks`
- Handles query timeouts via `max_execution_time` session variable
- Caches runtime variable lookups for `requires` condition evaluation
- Supports boolean normalization (`ON`/`YES`/`1` → `1`) for condition comparisons

### execution_logger.py

Writes the main execution log file (`diagnostic_collector_mysql_YYYYMMDD_HHMMSS.log`):
- Timestamped entries for connection status, query start/completion, warnings, and errors
- Warns when queries exceed 50% of the configured timeout
- Records total execution duration on completion
- Logs back-off events with current values and thresholds
