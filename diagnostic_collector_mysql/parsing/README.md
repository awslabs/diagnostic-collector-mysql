# diagnostic_collector_mysql/parsing/

Input validation and specialized log parsing.

## Modules

### input_validator.py

Validates `queries.json` and `report_format.json` before any database connection is attempted (fail-fast):
- Checks JSON structure, required fields, and field types
- Validates engine types against the allowed set (`mysql`, `aurora-mysql`, `mariadb`)
- Validates version format strings (`x.y` or `x.y.z`)
- Validates `requires` conditions (variable, operator, value)
- Cross-references `report_format.json` source IDs against `queries.json` query IDs
- Collects all errors (doesn't stop at the first one) for a complete validation report

### innodb_status_parser.py

Parses the raw text output of `SHOW ENGINE INNODB STATUS` into structured data:
- Splits the output into named sections (SEMAPHORES, TRANSACTIONS, LOG, BUFFER POOL, etc.)
- Extracts metrics from each section using regex patterns
- Parses deadlock information, foreign key errors, semaphore waits, transaction state, log sequence numbers, buffer pool statistics, and row operation rates
- Calculates derived metrics (purge lag, checkpoint lag, unflushed log)
- Returns an `InnoDBStatusData` dataclass with all parsed fields
