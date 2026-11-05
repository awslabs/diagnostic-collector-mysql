# diagnostic_collector_mysql/utils/

Utility scripts for development and CI.

## Modules

### validate_format_sync.py

Checks that the four report formats (HTML, Markdown, Text and JSON) generated from the same collection agree with each other:
- Section count, section names, and subsection count per section
- The expected structure of each format
- Presence of the Health Overview and the database information header
- Absence of rendering error messages

Generate one report in each format, then pass all four files:
```bash
uv run python -m diagnostic_collector_mysql.utils.validate_format_sync \
  report.html report.md report.txt report.json
```

Exit codes: `0` when every check passes, `1` when a check fails, `2` for invalid arguments or a missing file. `scripts/validate_formats.sh` wraps this for the directory layout the collector writes.

Cross-references between `report_format.json` and `queries.json` are checked separately, at startup, by `parsing/input_validator.py`.
