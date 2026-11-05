# diagnostic_collector_mysql/data/

Bundled data files that define **what** the collector queries and **how** it presents results. These two JSON files are the source of truth: all SQL lives in `queries.json`, all report layout lives in `report_format.json`. No diagnostic SQL is hardcoded in Python.

The files are loaded at runtime via `get_data_path()` from `__init__.py`, which resolves paths using `importlib.resources`.

## queries.json

A JSON object with a single `"queries"` array. Each element defines one diagnostic query.

### Query Object Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique identifier. Used for log file naming and as the `source` reference in `report_format.json`. Duplicate IDs are allowed **only** when queries have different `condition`, version ranges, or `requires`. This supports engine-specific or version-specific SQL variants for the same logical query. |
| `section` | string | Yes | Logical grouping for CLI section filtering. Valid values: `server-info`, `custom-config`, `replication`, `performance`, `memory`, `innodb`, `tables`, `indexes`, `security`, `connections`, `locks`, `io`, `io-stats`, `objects`, `sql-performance`, `user-activity`, `health-checks`. |
| `command` | string | Yes | The SQL statement to execute. Executed exactly once per run with no row limits. |
| `description` | string | No | Human-readable explanation of what the query does and why. Documentation only, not used at runtime. |
| `scaling_factor` | object | No | Documents the query's performance characteristics. See [Scaling Factors](#scaling-factors) below. |
| `condition` | object | No | Engine filter. Format: `{"engine": ["mysql", "aurora-mysql", "mariadb"]}`. When omitted, the query runs on all engines. |
| `min_engine_version` | string | No | Minimum engine version (inclusive). Format: `"x.y"` or `"x.y.z"`. Empty string `""` means no lower bound. |
| `max_engine_version` | string | No | Maximum engine version (exclusive). Format: `"x.y"` or `"x.y.z"`. Empty string `""` means no upper bound. |
| `requires` | object | No | Runtime variable condition. The query is skipped if the condition is not met. See [Requires Conditions](#requires-conditions) below. |
| `requires_extended_checks` | boolean | No | Default `false`. When `true`, the query only executes if `--extended-checks` is passed on the CLI. Use for queries that may cause contention on production systems. |
| `name` | string | No | Optional display name override for the query (used by some subsections). |
| `note` | string | No | Optional note displayed alongside query results. |
| `static` | string | No | When `"True"`, indicates the query returns static/hardcoded content (e.g., troubleshooting tips) rather than live database data. |

### Scaling Factors

The `scaling_factor` object documents how a query's cost scales with database size. This is metadata for humans and external tooling. The collector does not use it at runtime, but it's critical for understanding which queries are safe to run on large production systems.

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | The scaling category. See table below. |
| `note` | string | Human-readable explanation of the performance impact. |

**Scaling factor types:**

| Type | Meaning | Example |
|------|---------|---------|
| `constant` | Fixed cost regardless of database size. Reads server variables, status counters, or pre-aggregated Performance Schema tables. Negligible overhead. | `SELECT @@hostname`, `SHOW GLOBAL STATUS LIKE 'Uptime'` |
| `tables` | Cost scales with the number of tables/schemas. Queries `information_schema` data dictionary views. | Largest tables, storage engine distribution, fragmented tables |
| `threads` | Cost scales with active thread count. Queries `information_schema.processlist` (holds global thread manager mutex) or Performance Schema thread tables. | Connection breakdown, long-running queries |
| `indexes` | Cost scales with total index count across all schemas. Queries `information_schema.STATISTICS` or Performance Schema I/O summary tables. | Unused indexes, duplicate indexes, index I/O stats |
| `locks` | Cost scales with active lock count. May hold InnoDB `trx_sys` mutex during scan. Typically gated behind `requires_extended_checks`. | Current data locks, lock waits |
| `transactions` | Cost scales with active transaction count. May hold InnoDB `trx_sys` mutex. Typically gated behind `requires_extended_checks`. | InnoDB transaction details |

Scaling factors are designed to be consumed by external tools that want to assess query safety before execution. For example, a tool could filter out `threads`-type queries on systems with >10,000 connections, or skip `locks`-type queries entirely on production.

### Requires Conditions

The `requires` object defines a runtime check against a MySQL variable. If the condition is not met, the query is silently skipped.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `variable` | string | Yes | MySQL variable name (without `@@` prefix). Queried via `SELECT @@variable_name`. |
| `operator` | string | No | Comparison operator. Default: `"eq"`. Valid values: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`. |
| `value` | string | Yes | Expected value. For `eq`/`ne`, boolean normalization is applied (`1`/`ON`/`YES` → `1`, `0`/`OFF`/`NO` → `0`). For `gt`/`gte`/`lt`/`lte`, numeric comparison is used. |

**Example:** Skip this query when Performance Schema is disabled:
```json
{
  "requires": {
    "variable": "performance_schema",
    "operator": "eq",
    "value": "1"
  }
}
```

### Version Filtering

Version ranges use inclusive lower bound and exclusive upper bound. The version string from the database is normalized: suffixes like `-MariaDB` are stripped, and `x.y` is treated as `x.y.0`.

Examples:
- `"min_engine_version": "8.0.23", "max_engine_version": ""`: MySQL 8.0.23 and above
- `"min_engine_version": "", "max_engine_version": "8.1"`: below MySQL 8.1
- `"min_engine_version": "8.2", "max_engine_version": "8.4"`: MySQL 8.2.x through 8.3.x

### Engine-Specific Query Variants

When the same logical query needs different SQL for different engines or versions, use the same `id` with different `condition` or version ranges:

```json
[
  {
    "id": "binary_log_status",
    "command": "SHOW MASTER STATUS",
    "condition": {"engine": ["mariadb"]}
  },
  {
    "id": "binary_log_status",
    "command": "SHOW MASTER STATUS",
    "condition": {"engine": ["aurora-mysql", "mysql"]},
    "max_engine_version": "8.1"
  },
  {
    "id": "binary_log_status",
    "command": "SHOW BINARY LOG STATUS",
    "condition": {"engine": ["aurora-mysql", "mysql"]},
    "min_engine_version": "8.2"
  }
]
```

Only one variant will match at runtime based on the detected engine and version.

### Minimal Query Example

```json
{
  "id": "server_hostname",
  "section": "server-info",
  "command": "SELECT @@hostname AS hostname"
}
```

### Full Query Example

```json
{
  "id": "current_data_locks",
  "description": "Shows currently held InnoDB row-level locks. Useful for diagnosing lock contention.",
  "scaling_factor": {
    "type": "locks",
    "note": "Pre-8.0.40: holds global InnoDB trx_sys mutex during scan. 8.0.40+: uses bucket-based iteration. Gated behind requires_extended_checks."
  },
  "section": "locks",
  "command": "SELECT * FROM performance_schema.data_locks LIMIT 100",
  "condition": {"engine": ["mysql", "aurora-mysql"]},
  "min_engine_version": "",
  "max_engine_version": "",
  "requires": {
    "variable": "performance_schema",
    "operator": "eq",
    "value": "1"
  },
  "requires_extended_checks": true
}
```

---

## report_format.json

A JSON object with a single `"sections"` array. Defines the report structure: which sections appear, what subsections they contain, and how query results map to displayed columns.

### Section Object Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name for the section heading. |
| `number` | string | No | Section number for ordering and display (e.g., `"1"`, `"1.5"`, `"2"`). |
| `short_name` | string | No | Short name for navigation sidebar (e.g., `"Server Info"`). |
| `subsections` | array | Yes | Array of subsection objects. |

### Subsection Object Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name for the subsection heading. |
| `source` | string | Yes | Query ID from `queries.json`. Links this subsection to its data source. |
| `columns` | array | Yes | Columns to display. Each element is either a string (column name from query results) or a [computed column object](#computed-columns). |
| `condition` | object | No | Engine filter, same format as queries: `{"engine": ["mysql", "aurora-mysql"]}`. |
| `min_engine_version` | string | No | Minimum engine version (inclusive). |
| `max_engine_version` | string | No | Maximum engine version (exclusive). |
| `default_note` | string | No | Message displayed when the query returns no data. Replaces the generic "No data returned" message. |
| `note` | string | No | Informational note displayed above the data table. Supports markdown-like formatting in HTML reports. |
| `warning` | string | No | Warning message displayed below the data table. |
| `result_note` | string | No | Note displayed below the data table only when results are present. |
| `requires_extended_checks` | boolean | No | When `true`, subsection only renders if `--extended-checks` was passed. |
| `extended_checks_disabled_note` | string | No | Message shown in place of data when extended checks are disabled. |
| `fallback_source` | string | No | Alternative query ID to use when the primary `source` has no data. |
| `fallback_columns` | array | No | Columns to use with `fallback_source`. |
| `fallback_requires_extended_checks` | boolean | No | Whether the fallback source requires `--extended-checks`. |
| `display_type` | string | No | Rendering hint. Default is a table. `"metric_cards"` renders as card-style metrics instead. |
| `tips` | array | No | Array of tip objects (`{"title": "...", "description": "..."}`) rendered as collapsible guidance sections. |

### Computed Columns

When a column value needs to be derived from query results, use a computed column object instead of a plain string:

```json
{"name": "uptime_days", "expr": "Value / 86400"}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Display name for the column header. |
| `expr` | string | Mathematical expression using column names from the query results and operators (`+`, `-`, `*`, `/`). |

### Cross-Reference Validation

Every `source` (and `fallback_source`) value in `report_format.json` must correspond to an `id` in `queries.json`. The `InputValidator` enforces this at startup. If a source references a nonexistent query ID, the collector exits with code 3 (input file error).

To check that the four report formats render the same sections and subsections, generate one report in each format and compare them with the format sync validator:
```bash
uv run python -m diagnostic_collector_mysql.utils.validate_format_sync \
  report.html report.md report.txt report.json
```

### Subsection Example

```json
{
  "name": "Server Uptime",
  "source": "server_uptime",
  "columns": [
    "uptime_seconds",
    {"name": "uptime_days", "expr": "Value / 86400"}
  ]
}
```

---

## Exporting Queries for External Use

The `queries.json` format is designed to be portable. External tools can consume it directly to:

- Execute the same diagnostics outside the collector
- Filter queries by `scaling_factor.type` to assess safety for a given environment
- Build custom dashboards from a subset of sections
- Generate documentation from `description` fields

The key contract: each query's `command` is a standalone SQL statement that can be executed on any compatible MySQL/Aurora/MariaDB instance with the appropriate privileges. The `condition`, version, and `requires` fields provide the metadata needed to determine applicability.
