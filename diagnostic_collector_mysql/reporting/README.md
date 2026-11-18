# diagnostic_collector_mysql/reporting/

Report generation from query log files.

## Modules

### report_generator.py

Generates formatted reports by reading the consolidated `queries-output.log` and mapping results to the structure defined in `report_format.json`.

**Generators** (one per output format):
- `HTMLReportGenerator` — full HTML report with navigation sidebar, collapsible sections, health overview dashboard, and styled tables
- `JSONReportGenerator` — structured JSON output
- `MarkdownReportGenerator` — GitHub-flavored Markdown
- `TextReportGenerator` — plain text with ASCII tables

**Partial generators** (for interrupted runs):
- `PartialHTMLReportGenerator`, `PartialJSONReportGenerator`, `PartialMarkdownReportGenerator`, `PartialTextReportGenerator`
- Include a banner indicating the report is incomplete and which sections were collected

**Supporting classes:**
- `LogFileParser` — parses the consolidated `queries-output.log` file back into structured data (column names + rows) keyed by query ID

The report pipeline: `queries-output.log` → `LogFileParser` → parsed logs dict → report generator → output file.
