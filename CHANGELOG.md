# Changelog

All notable changes to Diagnostic Collector for MySQL are documented in this file.

## 1.1.0

Initial public release.

- Read-only diagnostic collection for MySQL 8.0+, Aurora MySQL 3.0+ and MariaDB 11.4+, with engine and version detection that selects the matching variant of each query.
- 15 report sections covering server information, non-default configuration, replication, performance, memory, InnoDB, tables, indexes, security, connections, locks, I/O, database objects, SQL performance and user activity.
- Health Overview that classifies findings as critical, warning or OK and links each one to its section.
- Report output as HTML, JSON, Markdown or plain text.
- Section flags to scope a run to the areas under investigation.
- `--extended-checks` to opt in to the heavier transaction, lock and `SHOW ENGINE INNODB STATUS` checks.
- `--connection-back-off`, `--connection-usage-back-off` and `--objects-back-off` thresholds that limit the collector's impact on busy, nearly full or very large instances.
- TLS attempted on every connection, `--use-encrypted-connection` to require it, and `--ssl-ca` for certificate verification.
- Ctrl-C cancels the running statement and writes a partial report.
- `--dry-run` to validate configuration and show the execution plan without connecting.
