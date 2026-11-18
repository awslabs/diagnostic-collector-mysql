# diagnostic_collector_mysql/output/

Output directory management.

## Modules

### output_manager.py

Creates and manages the timestamped output directory (`diagnostic_collector_mysql_data_YYYYMMDD_HHMMSS/`):
- Creates the directory under the base path specified by `--output-dir`
- Provides path helpers for the main log file, query output log, and report file
- Sets restrictive file permissions (`0600`) on output files since they may contain sensitive database information
- Raises `OutputDirectoryError` (exit code 4) if the directory cannot be created
