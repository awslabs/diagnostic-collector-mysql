# Diagnostic Collector for MySQL

A Python-based database health check and reporting tool for MySQL, Aurora MySQL, and MariaDB databases.

## Overview

Diagnostic Collector for MySQL connects to your database, executes diagnostic queries, and generates comprehensive health reports in multiple formats (HTML, JSON, Markdown, Text). It's designed to help DBAs and developers quickly assess database health, identify performance issues, and troubleshoot problems.

## Features

- **Multi-Engine Support**: MySQL 8.0+, Aurora MySQL 8.0+, MariaDB 11.4+
- **Comprehensive Diagnostics**: Server info, replication, performance, memory, storage, tables, indexes, security, connections, locks, I/O, and more
- **Multiple Output Formats**: HTML, JSON, Markdown, Plain Text
- **Health Overview Dashboard**: At-a-glance health status with critical issues, warnings, and OK indicators
- **Section Filtering**: Run specific diagnostic sections or all sections
- **Back-Off Protection**: Automatically backs off when database is under heavy load
- **Graceful Interruption**:
  - The first Ctrl-C asks the server to cancel the running statement with `KILL QUERY`, the way the mysql client does.
  - The collection then stops, and a partial report is written if any results were collected.
  - A second Ctrl-C exits immediately, without waiting for the running statement.
- **Dry-Run Mode**: Validate configuration without connecting to database

## Quick Start

### Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) — fast Python package and project manager

### Install

```bash
pip install uv        # or: brew install uv
git clone https://github.com/awslabs/diagnostic-collector-mysql
cd diagnostic-collector-mysql
uv sync
```

### Run

```bash
# Supply the password through the environment so it stays out of process listings
export DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD='secret'

# Generate an HTML health report
uv run diagnostic-collector-mysql --user admin --host mydb.example.com

# JSON format
uv run diagnostic-collector-mysql --user admin --host mydb.example.com --format json

# SSL/TLS: require encryption and verify the server certificate
# TLS is always attempted; these two flags enforce it and check server identity
uv run diagnostic-collector-mysql --user admin --host mydb.example.com \
    --use-encrypted-connection --ssl-ca /path/to/global-bundle.pem

# Run only specific sections
uv run diagnostic-collector-mysql --user admin --host mydb.example.com \
    --replication-check --innodb-check

# Dry-run (validate without connecting)
uv run diagnostic-collector-mysql --user admin --host mydb.example.com --dry-run
```

### Test & Lint

```bash
uv run pytest diagnostic_collector_mysql/tests/ -v
uv run ruff check diagnostic_collector_mysql/
```

## Health Overview

Every report begins with a Health Overview section that provides an at-a-glance summary of database health. The following automated health checks are performed:

| Check | Warning Threshold | Critical Threshold |
|-------|-------------------|-------------------|
| Redo Log Occupancy | >60% | >80% |
| Tables Without Primary Key | Any | - |
| Empty Passwords | - | Any |
| Connection Usage | >75% | >90% |
| History List Length | >50,000 | >100,000 |
| Buffer Pool Hit Rate | <95% | - |
| Replication Lag | >10 seconds | >60 seconds |
| Replication Errors | - | Any |
| Auto-Increment >90% Full | - | Any |
| Server Configuration Issues | Any | - |
| Disk Tmp Table % | >50% | >80% |

A check appears in the Health Overview only when the data it reads was collected.
Section flags, engine and version conditions, and an instance that does not expose
a given metric can all leave a check out of a report.

### Health Overview in JSON output

With `--format json`, the Health Overview is under `health_overview`, and each
check is an object with three fields:

```json
{
  "health_overview": {
    "health_checks": [
      { "status": "critical", "check": "Tables Without Primary Key", "details": "10 table(s)" },
      { "status": "warning",  "check": "Server Configuration",       "details": "Issues detected" },
      { "status": "ok",       "check": "Buffer Pool Hit Rate",       "details": "99.87%" }
    ]
  }
}
```

`status` is one of `critical`, `warning` or `ok`. `check` is the check name as it
appears in the table above. `details` is a human-readable summary and its wording
is not a stable interface, so match on `status` and `check` rather than parsing it.

## InnoDB Purge Analysis

The InnoDB Purge Analysis is a composite subsection within the "InnoDB Engine & Redo Log" section that provides comprehensive visibility into InnoDB's purge operations. This feature helps identify purge-related performance issues and potential transaction blocking problems.

### Components

The InnoDB Purge Analysis includes:

1. **Understanding Purge** - An informational box explaining what InnoDB purge does and why it matters, with a link to the [AWS Database Blog article on InnoDB History List Length](https://aws.amazon.com/blogs/database/reduce-downtime-with-amazon-rds-mysql-by-avoiding-innodb-history-list-length-growth/).

2. **Purge Thread Activity** - Shows the current state of purge threads including thread ID, name, type, and state.

3. **Purge Configuration** - Displays key purge-related configuration variables:
   - `innodb_purge_threads` - Number of background threads for purge operations
   - `innodb_purge_batch_size` - Number of undo log pages purged per batch
   - `innodb_max_purge_lag` - Maximum purge lag before throttling DML
   - `innodb_max_purge_lag_delay` - Maximum delay in microseconds when purge lag exceeded

4. **Purge Performance Metrics** - Key metrics including:
   - Purge Threads count
   - Undo Tablespaces count
   - Active Undo Tablespaces count

5. **Purge Optimization Recommendations** - Actionable recommendations based on current purge state.

6. **Purge Health Status** - A health indicator based on history list length with the following thresholds:

| History List Length | Status | Message |
|---------------------|--------|---------|
| < 50,000 | OK | Purge operating normally. History list length is healthy. |
| 50,000 - 99,999 | WARNING | History list length is elevated. Monitor purge performance and check for long-running transactions. |
| ≥ 100,000 (with long-running transactions*) | CRITICAL | History list length is very high. Long-running transaction(s) detected that may be blocking purge. See Active Transactions section. |
| ≥ 100,000 (no long-running transactions) | CRITICAL | History list length is very high. Purge may not be keeping up with workload. |

*Long-running transactions are defined as transactions older than 5 minutes.

When the history list length reaches CRITICAL status and long-running transactions are detected, the report includes a hyperlink to the "Active Transactions" section for quick navigation to identify the blocking transactions.

## Long-Running Transactions Activity (Top 10 Recent Queries)

The Long-Running Transactions Activity section provides visibility into what SQL statements have been executed within long-running transactions (>5 minutes). This helps identify the root cause of transaction blocking and lock contention issues.

### Purpose

When a transaction runs for an extended period, it can:
- Block InnoDB purge operations, causing history list length growth
- Hold locks that block other transactions
- Consume resources and affect overall database performance

This section shows the 10 most recent SQL statements executed within each long-running transaction, helping you understand:
- What operations the transaction has performed
- Whether the transaction is actively working or idle
- The efficiency of queries within the transaction

### Columns

| Column | Description |
|--------|-------------|
| `mysql_pid` | Process ID - use to identify the connection and correlate with application logs |
| `trx_duration` | Total time the transaction has been active |
| `trx_state` | Current transaction state (ACTIVE, COMMITTED, ROLLED BACK) |
| `sql_text` | The SQL statement that was executed |
| `stmt_execution_time` | How long this specific statement took to execute |
| `rows_sent` | Number of rows returned to the client |
| `rows_modified` | Number of rows affected (inserted, updated, deleted) by this statement |
| `rows_examined` | Number of rows scanned - high values may indicate missing indexes |
| `tmp_disk_tables` | Disk temporary tables created - high values indicate inefficient queries |
| `error_info` | Shows if the statement encountered errors |

### What to Look For

- **High `rows_modified`**: Statements modifying many rows may be causing lock contention
- **High `rows_examined` vs `rows_sent` ratio**: Indicates inefficient queries that scan many rows but return few
- **High `tmp_disk_tables`**: Suggests queries need optimization (consider adding indexes or rewriting queries)
- **Multiple statements per PID**: Shows the transaction's activity history
- **Error information**: Identifies statements that failed within the transaction

### Requirements

This section requires `performance_schema` to be enabled and the following consumers to be active:
- `events_transactions_current`
- `events_statements_history`

### Example Use Case

If you see a transaction with:
- `trx_duration`: 15 min
- Multiple SELECT statements with high `rows_examined`
- Low `rows_sent` values

This indicates the transaction is running inefficient queries that are scanning large portions of tables. Consider:
1. Adding appropriate indexes
2. Optimizing the query WHERE clauses
3. Breaking the transaction into smaller batches

## Parallel Replication (MTS) Analysis

The Parallel Replication (MTS) Details section provides analysis of multi-threaded replication worker efficiency. This section only appears when:
- The instance is a replica (has active replication connections)
- `replica_parallel_workers` (or `slave_parallel_workers`) > 1

### Worker Efficiency Metrics

The analysis calculates:
- **Distribution Spread**: Difference between the highest and lowest transaction percentage across workers
- **Active Workers**: Workers handling more than 5% of total transactions

### Distribution Status Thresholds

| Status | Spread | Active Workers | Meaning |
|--------|--------|----------------|---------|
| ✅ Good | < 20% | ≥ 80% of configured | Work evenly distributed, good parallelism |
| ⚠️ Uneven | > 50% | < 50% of configured | Poor parallelism, recommendations provided |
| ℹ️ Moderate | 20-50% | 50-80% of configured | Acceptable but could be improved |

### Understanding Spread

**Lower spread = better distribution = better parallelism**

| Spread Value | Interpretation |
|--------------|----------------|
| 2% | All workers handle similar % → Excellent parallelism |
| 25% | Some variance but acceptable → Moderate parallelism |
| 77% | One worker doing most work → Poor parallelism |

### Examples

**Example 1: Good Distribution ✅**

4 parallel workers, all working efficiently:

| Worker | Transactions | % of Total |
|--------|-------------|------------|
| Worker 1 | 2,500 | 25% |
| Worker 2 | 2,400 | 24% |
| Worker 3 | 2,600 | 26% |
| Worker 4 | 2,500 | 25% |

- Spread = 26% - 24% = **2%** (< 20% ✅)
- Active workers = 4 of 4 (all > 5%) = **100%** (≥ 80% ✅)
- **Result**: Good Distribution

**Example 2: Uneven Distribution ⚠️**

4 parallel workers, work concentrated on 1 worker:

| Worker | Transactions | % of Total |
|--------|-------------|------------|
| Worker 1 | 8,500 | 85% |
| Worker 2 | 500 | 5% |
| Worker 3 | 700 | 7% |
| Worker 4 | 300 | 3% |

- Spread = 85% - 3% = **82%** (> 50% ⚠️)
- Active workers = 3 of 4 (Workers 1,2,3 > 5%) = **75%**
- **Result**: Uneven Distribution - recommendations provided

**Example 3: Too Few Active Workers ⚠️**

8 parallel workers configured, only 3 doing real work:

| Worker | Transactions | % of Total |
|--------|-------------|------------|
| Worker 1 | 4,000 | 40% |
| Worker 2 | 3,500 | 35% |
| Worker 3 | 2,000 | 20% |
| Worker 4 | 200 | 2% |
| Worker 5 | 150 | 1.5% |
| Worker 6 | 100 | 1% |
| Worker 7 | 30 | 0.3% |
| Worker 8 | 20 | 0.2% |

- Spread = 40% - 0.2% = **39.8%** (< 50% ✅)
- Active workers = 3 of 8 (only Workers 1,2,3 > 5%) = **37.5%** (< 50% ⚠️)
- **Result**: Uneven Distribution - 5 workers are essentially idle

### Configuration Checks

The analysis also validates optimal configuration:

| Parameter | Optimal Value | Notes |
|-----------|---------------|-------|
| `replica_parallel_type` | `LOGICAL_CLOCK` | Allows transactions from same group commit to run in parallel |
| `binlog_transaction_dependency_tracking` | `WRITESET` or `WRITESET_SESSION` | Only checked for MySQL < 8.1; must be set on SOURCE server |

### Recommendations

When uneven distribution is detected, the report provides actionable recommendations:

1. **Set `replica_parallel_type` to `LOGICAL_CLOCK`** (if not already set)
   ```sql
   -- Stop replication first
   STOP REPLICA;
   SET GLOBAL replica_parallel_type = 'LOGICAL_CLOCK';
   START REPLICA;
   ```

2. **Enable WRITESET on SOURCE server** (MySQL < 8.1)
   ```sql
   -- Run on SOURCE server, not replica
   SET GLOBAL binlog_transaction_dependency_tracking = 'WRITESET';
   SET GLOBAL transaction_write_set_extraction = 'XXHASH64';
   ```

3. **Review workload for hot spots** - Uneven distribution may indicate:
   - Many transactions modifying the same rows/tables
   - Large transactions that cannot be parallelized
   - Sequential dependencies in application logic

4. **Monitor after changes** - Reset statistics and re-run collector:
   ```sql
   TRUNCATE performance_schema.events_transactions_summary_by_thread_by_event_name;
   ```

## Report Sections

The generated report includes the following diagnostic sections with detailed subsections:

### 1. Server Information & Version Details (13 subsections)

Basic server identification and configuration status.

| Subsection | Description |
|------------|-------------|
| Engine Version | MySQL/MariaDB version string |
| Aurora Version | Aurora-specific version (Aurora only) |
| Server Hostname | Database server hostname |
| Server UUID | Unique server identifier (MySQL/Aurora only) |
| Server Port | Listening port number |
| Data Directory | Path to MySQL data files |
| Character Set | Default character set and collation |
| Time Zone | System and session time zone settings |
| Server Uptime | Time since last restart in seconds and days |
| Read Only Status | Whether server is in read-only mode |
| Performance Schema Status | Whether Performance Schema is enabled |
| Performance Schema Consumers | Status of Performance Schema event consumers |
| Performance Schema Instruments | Count of enabled vs total instruments by class |

### 2. Non-Default Configuration Variables (2 subsections)

Server configuration analysis and potential issues.

| Subsection | Description |
|------------|-------------|
| Variables Changed from Default | All variables that differ from compiled defaults, showing current value, source (config file, command line, etc.), and configuration path |
| Configuration Warnings | Identifies 9 potentially problematic settings: `sync_binlog`, `innodb_flush_log_at_trx_commit`, `foreign_key_checks`, `innodb_stats_persistent`, `innodb_stats_auto_recalc`, `binlog_row_image`, `default_storage_engine`, `log_output`, `max_connect_errors` |

### 3. Replication Topology & Status (10 subsections)

Complete replication configuration and health status.

| Subsection | Description |
|------------|-------------|
| Replication Role | Whether instance is a source, replica, or standalone |
| Binary Log Status | Current binary log file and position, GTID set |
| GTID Configuration | GTID mode and consistency enforcement settings |
| Replica Status | IO/SQL thread status, lag, and errors (for replicas) |
| Connected Replicas | List of replicas connected to this source |
| Aurora Replica Host Status | Aurora Global Database replica lag (Aurora only) |
| Replication Filters | Active replication filters (replicate-do-db, etc.) |
| Parallel Replication (MTS) Details | Multi-threaded replication worker statistics with distribution analysis |

### 4. Performance Metrics (6 subsections)

Key performance indicators and bottleneck identification.

| Subsection | Description |
|------------|-------------|
| Key Performance Counters | QPS, total queries, slow queries, threads connected/running, aborted connections/clients |
| Temporary Table Usage | Created tmp tables vs disk tmp tables with health percentage |
| Wait Events by Latency | Top wait events consuming time, identifies I/O, lock, or sync bottlenecks |
| Stale Statistics | Tables with index statistics older than 7 days |
| Query Type Distribution | Breakdown of SELECT, INSERT, UPDATE, DELETE operations |
| Table Cache Statistics | Table cache hit rate and opened tables metrics |

### 5. Memory Configuration & Usage (15 subsections)

Comprehensive memory analysis and troubleshooting.

| Subsection | Description |
|------------|-------------|
| Global Memory Buffers | InnoDB buffer pool, key buffer, query cache sizes |
| Per-Connection Memory Buffers | sort_buffer, join_buffer, read_buffer with max connections estimate |
| Global Memory Usage - Top Components | Actual memory by MySQL component from Performance Schema |
| Memory Usage by Thread | Memory consumption per connection/background thread |
| Memory Usage by User Account | Memory grouped by database user |
| Memory Events - Prepared Statements | Memory for prepared statements and connections |
| Connection Memory Overhead | Average memory per connection calculation |
| Temporary Table Memory Usage | tmp_table_size, temptable_max_ram settings |
| TempTable Engine Memory | TempTable engine allocation (MySQL 8.0+) |
| Sort & Join Buffer Memory | Sort/join buffer usage and sort merge passes |
| InnoDB Buffer Pool Statistics | Hit rate, dirty pages, free pages |
| Queries Using Most Memory | Top queries by MAX_TOTAL_MEMORY (MySQL 8.0.16+) |
| Performance Schema Memory | Memory consumed by Performance Schema |
| Global Memory Total | Total tracked memory from sys schema |
| Memory Troubleshooting Tips | Actionable guidance for memory issues |

### 6. InnoDB Engine & Redo Log (14 subsections)

Storage engine health and transaction management.

| Subsection | Description |
|------------|-------------|
| Redo Log Status | Redo log configuration and current state |
| Redo Log Occupancy | Percentage of redo log space used (critical for write stalls) |
| Transaction Summary | Active transactions, oldest age, rows locked/modified |
| InnoDB Configuration | Key InnoDB parameters with recommendations |
| InnoDB Row Operations | Rows read, inserted, updated, deleted counts |
| Buffer Pool Hit Rate | Cache efficiency metric (>99% ideal) |
| InnoDB Purge & Transaction Status | History list length and purge metrics |
| Long-Running Transactions | Composite view of idle and active long transactions with blocking analysis |
| Long-Running Transactions Activity | Top 10 recent queries per long-running transaction (>5 min) |
| All Current Transactions Summary | Overview of all active transactions |
| History List Length | Purge lag indicator (high values cause bloat) |
| InnoDB Change Buffer | Change buffer status and efficiency |
| InnoDB Purge Analysis | Comprehensive purge thread activity, configuration, and health status |
| InnoDB Engine Status | Detailed diagnostics from SHOW ENGINE INNODB STATUS |

### 7. Table Analysis (9 subsections)

Table structure, storage, and potential issues.

| Subsection | Description |
|------------|-------------|
| Largest Tables | Top tables by total size (data + indexes) |
| Storage Engine Distribution | Table count and size by engine (InnoDB, MyISAM, etc.) |
| InnoDB Row Format Distribution | Tables by row format (DYNAMIC, COMPRESSED, etc.) |
| InnoDB Tables Compact/Redundant Problematic Indexes | Indexes exceeding 767-byte prefix limit on Compact/Redundant tables (MySQL/Aurora only) |
| Tables Without Primary Key | Critical: tables missing PKs break replication |
| Fragmented Tables | Tables with >20% fragmentation wasting disk space |
| Auto Increment Usage (>50%) | Tables approaching auto-increment limits |
| User Tables Not Using InnoDB | Non-InnoDB tables that may have issues |
| Instant DDL Column Tracking | Tables with instant DDL row versions approaching limits |

### 8. Index Analysis (5 subsections)

Index optimization opportunities.

| Subsection | Description |
|------------|-------------|
| Unused Indexes | Indexes never used since restart (candidates for removal) |
| Duplicate Indexes | Indexes with identical leading columns |
| Redundant Indexes | Indexes covered by other indexes (from sys schema) |
| Foreign Keys Without Indexes | FK columns missing indexes (causes table locks) |
| Index I/O Statistics | Read/write activity per index |

### 9. Security Configuration (14 subsections)

Security audit and vulnerability identification.

| Subsection | Description |
|------------|-------------|
| Locked Accounts | Accounts that are locked |
| Accounts with Expired Passwords | Accounts requiring password reset |
| Accounts with Empty Passwords | Critical: accounts without passwords |
| Accounts with Wildcard Hosts | Accounts allowing connections from any IP |
| Users with Elevated Privileges | Users with SUPER, ALL, or admin privileges |
| SSL Configuration | SSL/TLS settings and enforcement |
| SSL Certificate Expiry | Certificate validity dates |
| Current SSL Connection Status | Count of SSL vs non-SSL connections |
| SSL Connection Details per Thread | Per-connection SSL version details |
| Accounts with Weak Authentication Plugins | Accounts using mysql_native_password |
| Users with GRANT OPTION | Users who can delegate privileges |
| Anonymous Accounts | Critical: accounts with empty username |
| Database-Level Privileges to Sensitive Schemas | Access to mysql, performance_schema, sys (includes users with global privileges) |
| Users with Password Older than 6 Months | Stale credentials needing rotation |

### 10. Connection Management (10 subsections)

Connection health and resource usage.

| Subsection | Description |
|------------|-------------|
| Connection Statistics | max_connections, current connections, errors |
| Connections by User | Connection count per user account |
| Connection Breakdown by State | What connections are doing (Sending data, Sorting, etc.) |
| Connection Breakdown by Command | Activity types (Query, Sleep, etc.) |
| Connection Breakdown by Time | Connection age distribution |
| Connection Attributes | Client application details (name, version, OS) |
| Long-Running Idle Connections | Idle connections >5 minutes (uses PFS, falls back to information_schema with --extended-checks) |
| Long Running Queries | Queries executing >60 seconds (uses PFS, falls back to information_schema with --extended-checks) |
| Long Running Transactions | Transactions >10 minutes (uses PFS, falls back to information_schema with --extended-checks) |
| Connection Usage | Percentage of max_connections in use |

### 11. Lock Analysis (6 subsections)

Lock contention and deadlock analysis.

| Subsection | Description |
|------------|-------------|
| Lock Configuration | innodb_lock_wait_timeout, deadlock detection settings |
| InnoDB Lock Statistics | Cumulative row lock waits, time, and detailed explanations |
| Deadlock Count | Total deadlocks since server start (MariaDB) |
| Current Data Locks | Active row-level locks (requires --extended-checks) |
| Lock Waits | Transactions waiting for locks (requires --extended-checks) |
| Metadata Locks | DDL-blocking metadata locks on user objects |

### 12. I/O Analysis (7 subsections)

Storage performance and hot spots.

| Subsection | Description |
|------------|-------------|
| Table I/O Statistics | Read/write operations and latency per table |
| File I/O Statistics | I/O at the file level (data files, redo logs) |
| High Read Tables | Tables with most read operations |
| High Write Tables | Tables with most write operations |
| High Latency Tables | Tables with highest I/O latency |
| I/O by Event Type | I/O distribution by event (data, redo, temp) |
| I/O by Index | Index-level I/O activity patterns |

*Note: Aurora MySQL does not instrument physical I/O the same way due to its distributed storage architecture. Logical I/O (table-level operations) is fully available.*

### 13. Database Objects (8 subsections)

Database object inventory.

| Subsection | Description |
|------------|-------------|
| Schema Object Overview | Summary of all objects by schema and type |
| Databases | List of user databases (excludes system DBs) |
| Triggers | Triggers with event type and timing |
| Events | Scheduled events with status and schedule |
| Stored Procedures | User-defined stored procedures |
| Functions | User-defined functions |
| Foreign Key Constraints | FK relationships with CASCADE rules |
| Partitioned Tables | Partitioned tables with partition details |

### 14. SQL Performance (13 subsections)

Query optimization opportunities.

| Subsection | Description |
|------------|-------------|
| Top Queries by Execution Time | Queries consuming most total time |
| Query Latency Percentiles | Min/avg/max latency distribution |
| Query Latency P95 | 95th percentile latency (MySQL/Aurora) |
| Query Latency P99 | 99th percentile latency (MySQL/Aurora) |
| Queries with Errors | Queries generating errors or warnings |
| Full Table Scans | Queries not using indexes effectively |
| Queries Creating Temp Tables on Disk | Queries spilling to disk temp tables |
| Statement Analysis | Comprehensive query analysis from sys schema |
| Full Table Scans (sys schema) | Table scan queries with latency |
| Tables with Full Table Scans | Tables being fully scanned |
| Statements with Sorting | Queries requiring sort operations |
| Statements with Temp Tables | Queries creating temporary tables |
| Tables Without Primary Key Count | Count for health overview |

### 15. Database User Activity (4 subsections)

User behavior and connection patterns.

| Subsection | Description |
|------------|-------------|
| Connection Errors by Host | Hosts with connection failures |
| Statement Errors by User | Users generating query errors |
| Accounts Not Closing Connections | Connection leak detection |
| Accounts Never Connected Since Restart | Potentially unused accounts |

## Installation

For detailed installation instructions, see [Quick Start](#quick-start) above.

## Usage

### Engine-Specific Examples

```bash
# Supply the password through the environment so it stays out of process listings
export DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD='your_password'

# MySQL 8.0 - HTML report
uv run diagnostic-collector-mysql \
    --host your-mysql-host.example.com \
    --user your_user --port 3306 \
    --format html

# MySQL 8.0 Read Replica - require TLS and verify the server certificate
uv run diagnostic-collector-mysql \
    --host your-mysql-replica.example.com \
    --user your_user --port 3306 \
    --format html --use-encrypted-connection --ssl-ca /path/to/global-bundle.pem

# MariaDB - require TLS and verify the server certificate
uv run diagnostic-collector-mysql \
    --host your-mariadb-host.example.com \
    --user your_user --port 3306 \
    --format html --use-encrypted-connection --ssl-ca /path/to/global-bundle.pem

# Aurora MySQL - JSON format with extended checks
uv run diagnostic-collector-mysql \
    --host your-aurora-cluster.cluster-xxxxx.region.rds.amazonaws.com \
    --user your_user --port 3306 \
    --format json --extended-checks

# Verbose output for debugging
uv run diagnostic-collector-mysql \
    --host your-mysql-host.example.com \
    --user your_user --port 3306 \
    --format html --verbose
```

## AI-Assisted Development

This project includes a contributing guide for AI coding assistants at [`ai-skills/contributing-using-ai-guide.md`](ai-skills/contributing-using-ai-guide.md). It covers the project architecture, test and lint commands, JSON schema conventions, and common development workflows like adding queries or CLI options.

The guide is tool-agnostic plain markdown with YAML frontmatter, so a skill-compatible agent can load it automatically, and any other assistant can take it as context. It also reads well on its own as an architectural overview.

### Subdirectory READMEs

Each subdirectory has a README documenting its modules:

| Directory | README covers |
|-----------|---------------|
| `diagnostic_collector_mysql/data/` | Full JSON schemas for `queries.json` and `report_format.json` — scaling factors, requires conditions, version filtering, computed columns, fallback sources |
| `diagnostic_collector_mysql/core/` | Connection manager, query executor, execution logger |
| `diagnostic_collector_mysql/parsing/` | Input validator, InnoDB status parser |
| `diagnostic_collector_mysql/reporting/` | Report generators (HTML, JSON, Markdown, Text) and log file parser |
| `diagnostic_collector_mysql/output/` | Output directory management and file permissions |
| `diagnostic_collector_mysql/utils/` | Format sync validator |

## CLI Options

### Connection Options (Required)

| Option | Description |
|--------|-------------|
| `--user USERNAME` | Database username |
| `--password PASSWORD` | Database password. Prefer the `DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD` environment variable (see [Secure Credential Handling](#secure-credential-handling)) |
| `--host HOSTNAME` | Database hostname |
| `--port PORT` | Database port (default: 3306) |

### SSL/TLS Options

TLS is always attempted. The tool has no plaintext mode by design: an
unencrypted session happens only when the server itself declines TLS, and the
collector logs a warning when that happens.

Verification and enforcement are separate concerns with separate flags:

| Option | Description |
|--------|-------------|
| `--ssl-ca FILE` | **Verification.** Verify the server certificate against this CA. Without it the session is still encrypted, but the server's identity is unchecked and the connection is open to man-in-the-middle. |
| `--use-encrypted-connection` | **Enforcement.** Fail the connection if the session is not encrypted, rather than falling back to plaintext with a warning. Also fails closed if the encryption state cannot be determined. |

Passing `--ssl-ca` does not imply `--use-encrypted-connection`. For a remote
production database, use both:

```bash
uv run diagnostic-collector-mysql --user admin --host mydb.example.com \
    --use-encrypted-connection --ssl-ca /path/to/global-bundle.pem
```

#### Getting a CA bundle

For Amazon RDS and Aurora, download the global trust bundle, which covers all
regions:

```bash
curl -O https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
```

See the RDS and Aurora documentation on using SSL/TLS to encrypt a connection
for per-region bundles and certificate rotation schedules.

For self-managed MySQL or MariaDB, use the CA that signed the server
certificate. On a server using auto-generated certificates that is the
`ca.pem` in the data directory.

### Timeout Options

| Option | Description |
|--------|-------------|
| `--connect-timeout SECONDS` | Connection timeout (default: 10) |
| `--query-timeout SECONDS` | Query execution timeout (default: 30) |

### Output Options

| Option | Description |
|--------|-------------|
| `--format FORMAT` | Output format: html, json, markdown, text (default: html) |
| `--output-dir PATH` | Base output directory (default: current directory) |
| `--collection-limit N` | Maximum rows per subsection (default: 20) |

### Section Check Flags

When any section flag is specified, Server Information and Custom Configuration are always included.

| Option | Description |
|--------|-------------|
| `--replication-check` | Replication status section |
| `--performance-check` | Performance metrics section |
| `--memory-check` | Memory configuration section |
| `--innodb-check` | Storage engine status section |
| `--tables-check` | Tables section |
| `--indexes-check` | Index analysis section |
| `--security-check` | Security section |
| `--connections-check` | Connections section |
| `--locks-check` | Lock contention analysis section |
| `--io-check` | I/O analysis section |
| `--objects-check` | Database objects section |
| `--sql-performance-check` | Query analysis section |
| `--user-activity-check` | Database user activity section |

### Back-Off Safety Options

Back-off thresholds put a ceiling on the collector's impact. They are evaluated
once, before the first collection query, and exceeding either connection
threshold aborts the run with exit code 5.

| Option | Description |
|--------|-------------|
| `--connection-back-off N` | Back off if more than N connections are executing a statement (default: 1000) |
| `--connection-usage-back-off PCT` | Back off if more than PCT percent of `max_connections` are in use (default: 90, use 0 to disable) |
| `--objects-back-off N` | Skip the object-heavy sections if the table count exceeds N (default: 10000) |

#### The two connection guards measure different things

`--connection-back-off` measures concurrency. It counts foreground connections
currently executing a statement, and excludes sleeping sessions, daemon threads,
and the collector's own session. This is the number that tells you whether the
server is busy right now.

Replication threads are counted. They are foreground threads with a command that
is neither `Sleep` nor `Daemon`, so an idle source contributes its `Binlog Dump`
thread and an idle replica its receiver and applier threads. Only a standalone
server reads 0 when idle.

`--connection-usage-back-off` measures slot occupancy, as a percentage of
`max_connections`. A connection parked in `Sleep` holds a slot without executing
anything, so it is invisible to the concurrency guard while still bringing the
instance closer to refusing new connections. A percentage is used rather than an
absolute count because a count that is safe on a large instance can never trip on
a small one.

Both numbers are logged together, so you can reconcile them. They are emitted at
debug level, so pass `--verbose` to see them.

#### Which report figure matches which guard

For slot usage, read **Connection Usage** in the Connection Management section. It
is `Threads_connected` over `max_connections`, the same ratio this guard computes,
so the two agree.

The **Current Connections** figure is not that number. It lives in the Memory
section's Connection Memory Overhead subsection and counts all foreground
non-daemon threads including sleeping ones. On a standalone server that equals
`Threads_connected`; on a replica it is higher, because replication threads are
foreground threads without being client connections. Neither figure matches the
concurrency guard, which excludes sleeping sessions, so on an instance with many
idle application connections the concurrency number is far lower than both.

#### Where the counts come from, and the performance_schema fallback

One statement reads both figures where `performance_schema` is available. The
concurrency count comes from `performance_schema.threads`, which does not acquire
the global mutex that `information_schema.processlist` requires, and slot usage
comes from `Threads_connected` in `performance_schema.global_status`.

When `performance_schema` is disabled, `performance_schema.threads` returns no
rows, so the concurrency count falls back to `information_schema.processlist`.

Slot usage needs a fallback too, on MariaDB. MySQL keeps
`performance_schema.global_status` readable with instrumentation off, but MariaDB
leaves it empty, and MariaDB ships `performance_schema` off by default. The
collector therefore reads `Threads_connected` through `SHOW GLOBAL STATUS` when
the `performance_schema` table yields nothing, which works on both engines with
instrumentation either way.

The processlist fallback carries one caveat worth understanding. MySQL restricts
`information_schema.processlist` to the calling account's own threads when the
`PROCESS` privilege is absent, and it does so without raising an error. Because
the collector also excludes its own session from the count, such an account sees
**0** connections on a fully loaded server, and the concurrency guard silently
stops protecting anything.

The collector detects this rather than assuming it: it compares the number of
processlist rows the account can see against `Threads_connected`, and warns only
when the account really is blind. An account created with the grants under
Minimum Database Privileges includes `PROCESS` and counts correctly, so it gets
no warning.

Two remedies, in the order you can apply them:

1. **Grant `PROCESS`** to the collector's account. A grant reaches new
   connections only, and the collector connects once per run, so this takes
   effect on the next run. No restart is needed. It is already listed under
   Minimum Database Privileges.

   ```sql
   GRANT PROCESS ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
   ```

2. **Enable `performance_schema`** for the better steady state, which avoids the
   processlist mutex entirely. Note that `performance_schema` is a read-only
   variable set at server startup: `SET GLOBAL performance_schema = 1` fails with
   ERROR 1238. Enabling it requires a server restart, or a parameter group change
   and a reboot on Amazon RDS and Aurora.

If the thresholds cannot be evaluated at all, for example because a resource limit
on the account makes the query fail, the run continues and the log states that
back-off protection is disabled for that run. Running a read-only diagnostic from
a least-privilege account is a normal thing to do, so refusing to run would be
worse than running unguarded, but the log says plainly which happened.

### Execution Options

| Option | Description |
|--------|-------------|
| `--dry-run` | Validate inputs and display plan without connecting |
| `--extended-checks` | Enable extended checks that may cause contention (e.g., data locks, lock waits) |
| `--verbose` | Enable detailed logging output |
| `--progress` | Show progress messages to stdout during execution |
| `--version` | Display version and exit |

## Output Files

Diagnostic Collector for MySQL creates a timestamped output directory containing:

```
diagnostic_collector_mysql_data_YYYYMMDD_HHMMSS/
├── diagnostic_collector_mysql_YYYYMMDD_HHMMSS.log    # Main execution log
├── queries-output.log                      # Consolidated query output file
└── report_YYYYMMDD_HHMMSS.html            # Report file (format varies)
```

## Exit Codes

| Code | Description |
|------|-------------|
| 0 | Successful completion |
| 1 | Invalid command line arguments |
| 2 | Database connection failure |
| 3 | Input file error (queries.json or report_format.json) |
| 4 | Output directory error |
| 5 | Back-off threshold exceeded |
| 6 | Partial completion or interrupted |

## Minimum Database Privileges

Create a dedicated user with the privileges below. Scope the host to the address the
collector runs from rather than `'%'`, or the account becomes a finding in the
Security section of its own report.

### MySQL 8.0+ / Aurora MySQL

```sql
-- Create user (replace the host with the address you run the collector from)
CREATE USER 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0' IDENTIFIED BY 'your_secure_password';

-- Grant required privileges
GRANT SELECT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT SELECT ON performance_schema.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT SELECT ON mysql.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT SELECT, EXECUTE ON sys.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT PROCESS ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT REPLICATION CLIENT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT TRIGGER ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT EVENT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';

-- Apply changes
FLUSH PRIVILEGES;
```

### MariaDB 11.4+

MariaDB sys schema views use `SQL SECURITY INVOKER`, so `EXECUTE ON sys.*` is required
there for the same reason it is required on MySQL. From MariaDB 10.5 the
`REPLICATION CLIENT` privilege was renamed `BINLOG MONITOR` and no longer covers
`SHOW REPLICA STATUS`, so `REPLICA MONITOR` is required on every supported version.

```sql
-- Create user (replace the host with the address you run the collector from)
CREATE USER 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0' IDENTIFIED BY 'your_secure_password';

-- Grant required privileges
GRANT SELECT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT SELECT ON performance_schema.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT SELECT ON mysql.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT SELECT, EXECUTE ON sys.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT PROCESS ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT REPLICATION CLIENT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT TRIGGER ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';
GRANT EVENT ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';

-- Required for replication status from MariaDB 10.5.9 onward
GRANT REPLICA MONITOR ON *.* TO 'diagnostic_collector_mysql'@'10.0.0.0/255.255.0.0';

-- Apply changes
FLUSH PRIVILEGES;
```

### Privilege Details

| Privilege | Purpose | Notes |
|-----------|---------|-------|
| `SELECT ON *.*` | Table, index, object and account metadata | Global scope is required, see below |
| `SELECT ON performance_schema.*` | Performance metrics, wait events, memory usage | Subsumed by the global grant; kept explicit |
| `SELECT ON mysql.*` | User accounts and privileges | Subsumed by the global grant; kept explicit |
| `SELECT ON sys.*` | Formatted performance data | Subsumed by the global grant; kept explicit |
| `EXECUTE ON sys.*` | sys schema formatter functions | Required on MySQL and MariaDB |
| `PROCESS` | Process list and connection information | Without it the process list returns only the account's own threads |
| `REPLICATION CLIENT` | Replication status information | |
| `TRIGGER` | Trigger inventory | Without it `information_schema.triggers` returns no rows and raises no error |
| `EVENT` | Scheduled event inventory | Without it `information_schema.events` returns no rows and raises no error |
| `REPLICA MONITOR` | Replication monitoring | MariaDB 10.5.9+ |

**`SELECT` must be granted globally, not only per schema.** `information_schema`
returns only the objects the account holds a privilege on, and it does not raise when
rows are filtered out. The views also differ in which privilege they require:
`TABLES`, `STATISTICS` and `PARTITIONS` appear with any table privilege, while
`COLUMNS` and `KEY_COLUMN_USAGE` need `SELECT` specifically. An account without global
`SELECT` therefore reports Auto-Increment as OK on an instance with a column near
overflow, and inflates Tables Without Primary Key, because the primary-key join finds
nothing. The per-schema lines above are redundant once `SELECT ON *.*` is granted;
they are kept because they are harmless, they document which system schemas the tool
reads, and they still apply if the global grant is ever narrowed.

`TRIGGER` and `EVENT` behave the same way as the visibility filtering above: without
them the Database Objects section reports no triggers and no scheduled events on a
schema that has both, and `sys.schema_object_overview` omits those object types from
its census.

## Security Best Practices

### Secure Credential Handling

Diagnostic Collector for MySQL supports multiple methods for providing database credentials, listed in order of security preference:

#### 1. Environment Variable (Recommended)

Set the password via environment variable to avoid exposing it in process listings:

```bash
export DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD='your_secure_password'
uv run diagnostic-collector-mysql --user admin --host mydb.example.com
```

Or inline for a single command:

```bash
DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD='your_secure_password' uv run diagnostic-collector-mysql --user admin --host mydb.example.com
```

#### 2. Command Line Argument (Less Secure)

The `--password` option is available but exposes the password in process listings:

```bash
# Warning: Password visible in 'ps' output
uv run diagnostic-collector-mysql --user admin --password secret --host mydb.example.com
```

**Note**: When both environment variable and `--password` are provided, the command line argument takes precedence.

### Output File Security

Diagnostic Collector for MySQL automatically sets restrictive permissions (0600) on all output files:

- **Report files** (HTML, JSON, Markdown, Text)
- **Query output logs**
- **Execution logs**
- **Output directories** (0700)

This ensures only the file owner can read sensitive information such as:
- Database user accounts and privileges
- Connection details and hostnames
- Security configurations and password ages
- Performance data that could reveal system architecture

### Additional Security Recommendations

1. **Require encryption and verify server certificates** when connecting over
   networks (TLS is always attempted; `--use-encrypted-connection` enforces it and
   `--ssl-ca` verifies the server):
   ```bash
   uv run diagnostic-collector-mysql --user admin --host mydb.example.com \
       --use-encrypted-connection --ssl-ca /path/to/global-bundle.pem
   ```

2. **Create a dedicated read-only user** with minimum required privileges (see "Minimum Database Privileges" section)

3. **Store reports securely** - Reports contain sensitive database information and should be treated as confidential

4. **Clean up old reports** - Remove reports after analysis to minimize exposure of sensitive data

5. **Avoid running as root** - Run the collector as a non-privileged user

## Security Advisory

### Report Data Sensitivity Classification

Diagnostic Collector for MySQL reports contain sensitive information that requires appropriate handling:

| Data Category | Sensitivity Level | Examples |
|---------------|-------------------|----------|
| **User Accounts** | HIGH | Usernames, hosts, privileges, password ages |
| **Connection Details** | HIGH | Hostnames, IP addresses, ports, client info |
| **Security Configuration** | HIGH | SSL settings, authentication plugins, locked accounts |
| **Performance Data** | MEDIUM | Query patterns, wait events, resource usage |
| **Schema Information** | MEDIUM | Table names, index structures, object counts |
| **Server Configuration** | MEDIUM | Variable settings, version info, uptime |

### Data Handling Guidelines

#### Storage
- Store reports in encrypted storage when possible
- Limit access to reports on a need-to-know basis
- Use secure file transfer methods (SFTP, SCP) when sharing reports

#### Retention
- Define a retention policy for reports (recommended: 30-90 days)
- Securely delete reports after analysis using `shred` or secure delete tools
- Do not store reports in version control systems or shared drives

#### Sharing
- Redact sensitive hostnames and IP addresses before sharing externally
- Consider using the JSON format for programmatic redaction
- Never share reports via unencrypted email or public channels

### What Diagnostic Collector for MySQL Does NOT Collect

For transparency, Diagnostic Collector for MySQL explicitly avoids collecting:

- ❌ Actual passwords or authentication strings
- ❌ Table data or row contents
- ❌ Query results containing application data
- ❌ Encryption keys or certificates
- ❌ AWS credentials or IAM information

### Network Security

When running Diagnostic Collector for MySQL across networks:

1. **Require encryption and verify the certificate** for remote connections, using
   `--use-encrypted-connection --ssl-ca <file>` together. TLS is always attempted,
   but only `--use-encrypted-connection` prevents a silent plaintext fallback, and
   only `--ssl-ca` checks the server's identity
2. **Use private networks** or VPN when possible
3. **Restrict collector host** - Run from a bastion host or management network

### Compliance Considerations

If your organization is subject to compliance requirements:

| Framework | Considerations |
|-----------|----------------|
| **PCI-DSS** | Reports may contain cardholder data environment details; handle as sensitive |
| **HIPAA** | Database metadata could reveal PHI system architecture |
| **SOC 2** | Include report handling in your security policies |
| **GDPR** | User account information may constitute personal data |

### Reporting Security Issues

If you discover a security vulnerability in Diagnostic Collector for MySQL:

1. **Do not** open a public GitHub issue.
2. Report it through the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/), as described in [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications).
3. Include detailed reproduction steps.

## Input File Formats

For the complete JSON schema documentation — including all fields, scaling factors, requires conditions, version filtering, computed columns, fallback sources, display types, and worked examples — see [`diagnostic_collector_mysql/data/README.md`](diagnostic_collector_mysql/data/README.md).

Each subdirectory also has a README documenting its modules:
- [`diagnostic_collector_mysql/core/README.md`](diagnostic_collector_mysql/core/README.md) — connection, query execution, logging
- [`diagnostic_collector_mysql/parsing/README.md`](diagnostic_collector_mysql/parsing/README.md) — input validation, InnoDB status parsing
- [`diagnostic_collector_mysql/reporting/README.md`](diagnostic_collector_mysql/reporting/README.md) — report generation pipeline
- [`diagnostic_collector_mysql/output/README.md`](diagnostic_collector_mysql/output/README.md) — output directory management
- [`diagnostic_collector_mysql/utils/README.md`](diagnostic_collector_mysql/utils/README.md) — development utilities

### queries.json

Defines the diagnostic queries to execute:

```json
{
  "queries": [
    {
      "id": "server_version",
      "section": "server-info",
      "command": "SELECT VERSION() AS version"
    },
    {
      "id": "aurora_version",
      "section": "server-info",
      "command": "SELECT aurora_version() AS aurora_version",
      "condition": {"engine": ["aurora-mysql"]}
    },
    {
      "id": "binary_log_status_84",
      "section": "replication",
      "command": "SHOW BINARY LOG STATUS",
      "condition": {"engine": ["mysql", "aurora-mysql"]},
      "min_engine_version": "8.2",
      "max_engine_version": ""
    },
    {
      "id": "perf_schema_waits",
      "section": "performance",
      "command": "SELECT * FROM performance_schema.events_waits_summary_global_by_event_name LIMIT 10",
      "requires": {"variable": "performance_schema", "operator": "eq", "value": "1"}
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier for the query (required) |
| `section` | Logical grouping for filtering (required) |
| `command` | SQL statement to execute (required) |
| `condition` | Optional engine filter: `{"engine": ["mysql", "aurora-mysql", "mariadb"]}` |
| `min_engine_version` | Optional minimum version (e.g., "8.0" or "8.0.29"). Empty string means no limit |
| `max_engine_version` | Optional maximum version (e.g., "8.4" or "8.4.0"). Empty string means no limit |
| `requires` | Optional runtime condition: `{"variable": "performance_schema", "operator": "eq", "value": "1"}` |

**Default Behavior**: When no `condition`, `min_engine_version`, or `max_engine_version` fields are present, the query runs on ALL engines and ALL versions.

#### Runtime Conditional Queries

Use `requires` to conditionally execute queries based on MySQL runtime variables:

```json
{
  "id": "perf_schema_waits",
  "section": "performance",
  "command": "SELECT * FROM performance_schema.events_waits_summary_global_by_event_name",
  "requires": {"variable": "performance_schema", "operator": "eq", "value": "1"}
}
```

| Field | Description |
|-------|-------------|
| `variable` | MySQL variable name to check (required) |
| `operator` | Comparison operator: eq, ne, gt, gte, lt, lte (default: eq) |
| `value` | Expected value to compare against (required) |

This enables fallback queries when a preferred data source is unavailable (e.g., when `performance_schema` is disabled).

### report_format.json

Defines the report structure:

```json
{
  "sections": [
    {
      "name": "Server Information",
      "subsections": [
        {
          "name": "Engine Version",
          "source": "server_version",
          "columns": ["version"]
        },
        {
          "name": "Server Uptime",
          "source": "server_uptime",
          "columns": [
            "uptime_seconds",
            {"name": "uptime_days", "expr": "uptime_seconds / 86400"}
          ]
        },
        {
          "name": "Replica Status",
          "source": "replica_status",
          "columns": ["Source_Host", "Replica_IO_Running"],
          "default_note": "No replication configured"
        },
        {
          "name": "InnoDB Purge Status",
          "source": "innodb_purge_status",
          "columns": ["VARIABLE_NAME", "VARIABLE_VALUE"],
          "min_engine_version": "8.0",
          "max_engine_version": ""
        }
      ]
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `name` | Display name for section/subsection |
| `source` | Query ID from queries.json |
| `columns` | Column names or computed columns |
| `condition` | Optional engine-specific conditions |
| `min_engine_version` | Optional minimum engine version (e.g., "8.4"). Empty string means no limit |
| `max_engine_version` | Optional maximum engine version (e.g., "8.0"). Empty string means no limit |
| `default_note` | Optional note to display when query returns no data |

#### Computed Columns

Computed columns allow mathematical expressions:

```json
{"name": "uptime_days", "expr": "uptime_seconds / 86400"}
{"name": "memory_mb", "expr": "memory_bytes / 1048576"}
{"name": "ratio_pct", "expr": "(value_a / value_b) * 100"}
```

Supported operators: `+`, `-`, `*`, `/`, parentheses

#### Version Filtering

Use `min_engine_version` and `max_engine_version` to show subsections only for specific database versions:

```json
{
  "name": "InnoDB Purge Status",
  "source": "innodb_purge_status",
  "columns": ["VARIABLE_NAME", "VARIABLE_VALUE"],
  "min_engine_version": "8.0",
  "max_engine_version": ""
}
```

- Both fields must be present together, or neither
- Empty string (`""`) means no limit
- Example: `"min_engine_version": "8.4", "max_engine_version": ""` means version >= 8.4
- Example: `"min_engine_version": "", "max_engine_version": "8.0"` means version <= 8.0

#### Default Notes

Use `default_note` to display a custom message when a query returns no data:

```json
{
  "name": "Replica Status",
  "source": "replica_status",
  "columns": ["Source_Host", "Replica_IO_Running"],
  "default_note": "No replication configured"
}
```

If not specified, "No data returned" is displayed.

### Query Usage Architecture

Not all queries defined in `queries.json` have a direct `"source"` reference in `report_format.json`. Queries are consumed in two ways:

**1. Simple Subsections (Direct Reference)**

Most subsections directly reference a query via the `"source"` field:
```
queries.json (query) → report_format.json (source) → rendered in report
```

**2. Composite/Health Subsections (Programmatic Reference)**

Some queries are consumed programmatically by the report generator for:

- **Health Overview calculations** - Queries like `health_empty_passwords`, `innodb_history_length` are used to compute health indicators
- **Composite subsections** - The InnoDB Purge Analysis section combines data from multiple queries (`innodb_transactions`, `long_running_idle_trx`, `long_running_active_trx`, `purge_thread_activity`, etc.)
- **Fallback queries** - Some queries serve as fallbacks when preferred data sources are unavailable

These queries don't appear as `"source"` values in `report_format.json` but are referenced directly in `report_generator.py`.

**Example**: The `innodb_transactions` query (which queries `information_schema.innodb_trx`) is used as a fallback in the InnoDB Purge Analysis composite section when the more detailed `long_running_idle_trx` or `long_running_active_trx` queries aren't available.

## Troubleshooting

### Connection Issues

**Error: Authentication failed**
- Verify username and password are correct
- Check if user has permission to connect from your host

**Error: Connection timeout**
- Verify the host is reachable
- Check firewall rules
- Increase `--connect-timeout` value

**Error: SSL/TLS connection failed**
- Verify the CA certificate path is correct
- Ensure the server supports SSL connections
- Confirm the bundle matches the server. For RDS and Aurora, use the global
  bundle (see "Getting a CA bundle")

**Warning: "the server certificate is NOT verified"**
- The session is encrypted, but the server's identity was never checked, so the
  connection is not protected against man-in-the-middle
- Pass `--ssl-ca <file>` to verify it

**Warning: "Connection to ... is NOT encrypted"**
- The server did not negotiate TLS, so the collector fell back to plaintext.
  Credentials and query results transited unencrypted
- Enable TLS on the server, or pass `--use-encrypted-connection` to fail the
  run instead of falling back

### Back-Off Triggered

**Error: Active connection count exceeds threshold**
- More connections are executing a statement than `--connection-back-off` allows
- Wait for load to decrease, or raise `--connection-back-off`

**Error: Connection slot usage exceeds threshold**
- More than `--connection-usage-back-off` percent of `max_connections` are in use,
  counting sleeping connections, which hold a slot without executing anything
- Wait for connections to be released, or find the sessions holding slots. An
  application pool that never closes connections is the usual cause
- To proceed anyway, raise `--connection-usage-back-off`, or set it to 0 to
  disable that guard for the run. Raising `max_connections` lowers the percentage
  without freeing a slot and raises the instance's memory ceiling, so it is not a
  fix for this warning; on RDS and Aurora it also means overriding a parameter
  group formula

**Warning: This account can see only N of M connections in information_schema.processlist**
- `performance_schema` is off, so the concurrency count came from the processlist
  view, and this account cannot see other sessions. MySQL restricts that view to
  the caller's own threads without the `PROCESS` privilege and raises nothing, so
  the count under-reports and the guard is ineffective
- Grant `PROCESS` to the collector's account, which takes effect on the next run
  with no restart, or enable `performance_schema`, which needs one. See Back-Off
  Safety Options

**Warning: Back-off protection is DISABLED for this run**
- The thresholds could not be evaluated, so the run proceeded unguarded
- The accompanying error names the cause, commonly a resource limit on the account

**Warning: Objects back-off threshold exceeded**
- The user table count is above `--objects-back-off`, so the object-heavy sections
  were skipped. The rest of the report is generated normally and the exit code is
  unaffected, unlike the two connection guards, which abort with exit code 5
- To include those sections, raise `--objects-back-off`. The warning message
  suggests a value

### Input File Errors

**Error: File not found**
- Ensure `queries.json` and `report_format.json` exist in the `diagnostic_collector_mysql/data/` directory

**Error: Invalid JSON**
- Check JSON syntax in the input files

**Error: Source query ID not found**
- Ensure all `source` references in report_format.json match query IDs in queries.json


## Acknowledgments

Diagnostic Collector for MySQL is built on the diagnostic infrastructure that the MySQL and MariaDB communities have developed over many years. The depth of what this tool can surface is a direct function of what [performance_schema](https://dev.mysql.com/doc/refman/8.0/en/performance-schema.html) and [information_schema](https://dev.mysql.com/doc/refman/8.0/en/information-schema.html) expose — interfaces that both communities have invested in continuously.

Database connectivity is provided by [mysql-connector-python](https://github.com/mysql/mysql-connector-python), Oracle's open source Python connector, available under the GPL v2 / Commercial license.

We are grateful to the MySQL and MariaDB communities for building and maintaining the engines this tool runs against. Their work makes tools like this one possible.

## Maintainers

Diagnostic Collector for MySQL is maintained by Shiv Bhatt and Chelluru V N S S Vidyadhar. See [MAINTAINERS.md](MAINTAINERS.md).

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
