"""
Report Generator module for Diagnostic Collector for MySQL.

This module provides functionality to parse query log files and generate
formatted reports from the collected data.

Responsibilities:
- Parse query log files to extract column names and row data
- Handle error log files gracefully (mark as failed, include error message)
- Evaluate computed column expressions using query result data
- Support operators: +, -, *, /, parentheses
- Log warning when computed column references non-existent column
- Treat NULL values as 0 in arithmetic expressions
"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from diagnostic_collector_mysql import __version__
from diagnostic_collector_mysql.parsing.innodb_status_parser import get_innodb_status_from_parsed_logs, InnoDBStatusData


# Module logger
logger = logging.getLogger(__name__)


# Configuration warning messages for the 9 checked parameters
# Used by all report generators (HTML, JSON, Markdown, Text)
CONFIG_WARNINGS = {
    'sync_binlog': "⚠️ WARNING: sync_binlog is not set to 1 - binary log may not be synchronized to disk after each transaction, risking data loss during system crashes.",
    'innodb_flush_log_at_trx_commit': "⚠️ WARNING: innodb_flush_log_at_trx_commit is not set to 1 - transaction logs may not be flushed to disk after each commit, compromising ACID durability guarantees.",
    'foreign_key_checks': "⚠️ WARNING: foreign_key_checks is disabled - referential integrity constraints are not enforced, allowing invalid data relationships and potential impact to data integrity.",
    'innodb_stats_persistent': "⚠️ WARNING: innodb_stats_persistent is disabled - table statistics will not persist across server restarts, causing suboptimal query execution plans.",
    'innodb_stats_auto_recalc': "⚠️ WARNING: innodb_stats_auto_recalc is disabled - table statistics will not be automatically updated, potentially leading to poor query performance over time.",
    'binlog_row_image': "⚠️ WARNING: binlog_row_image is set to FULL with ROW format - this generates excessive binary log data and may impact replication performance.",
    'default_storage_engine': "⚠️ WARNING: default_storage_engine is not InnoDB - using non-transactional storage engines compromises ACID compliance and crash recovery capabilities.",
    'log_output': "⚠️ WARNING: log_output is set to TABLE - storing logs in tables can cause performance degradation and potential circular logging issues.",
    'max_connect_errors': "⚠️ WARNING: max_connect_errors configured to high value - overly permissive connection error threshold may allow brute force attacks and mask connection issues.",
}


def evaluate_config_warnings(parsed_logs: Dict[str, 'ParsedLogFile']) -> List[Tuple[str, str]]:
    """
    Evaluate configuration parameters and return warnings for problematic values.
    
    This function checks the 9 configuration parameters from the config_warnings_check
    query and returns a list of (Parameter, Message) tuples for any warnings.
    
    This is a module-level function used by all report generators (HTML, JSON, Markdown, Text).
    
    Args:
        parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
        
    Returns:
        List of (Parameter, Message) tuples for triggered warnings.
        Empty list if no warnings or query data not available.
    """
    warnings = []
    
    # Check if config_warnings_check query exists and succeeded
    if 'config_warnings_check' not in parsed_logs:
        return warnings
    
    log_data = parsed_logs['config_warnings_check']
    if not log_data.success or not log_data.rows or not log_data.columns:
        return warnings
    
    # Build column index map
    try:
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Helper to get value from row
        def get_val(col_name):
            if col_name in col_map:
                return row[col_map[col_name]]
            return None
        
        # Check 1: sync_binlog != 1
        sync_binlog = get_val('sync_binlog')
        if sync_binlog is not None:
            try:
                if int(sync_binlog) != 1:
                    warnings.append(('sync_binlog', CONFIG_WARNINGS['sync_binlog']))
            except (ValueError, TypeError):
                pass
        
        # Check 2: innodb_flush_log_at_trx_commit != 1
        innodb_flush = get_val('innodb_flush_log_at_trx_commit')
        if innodb_flush is not None:
            try:
                if int(innodb_flush) != 1:
                    warnings.append(('innodb_flush_log_at_trx_commit', CONFIG_WARNINGS['innodb_flush_log_at_trx_commit']))
            except (ValueError, TypeError):
                pass
        
        # Check 3: foreign_key_checks == 'OFF'
        fk_checks = get_val('foreign_key_checks')
        if fk_checks is not None:
            if str(fk_checks).upper() == 'OFF' or fk_checks == 0 or fk_checks == '0':
                warnings.append(('foreign_key_checks', CONFIG_WARNINGS['foreign_key_checks']))
        
        # Check 4: innodb_stats_persistent == 'OFF'
        stats_persistent = get_val('innodb_stats_persistent')
        if stats_persistent is not None:
            if str(stats_persistent).upper() == 'OFF' or stats_persistent == 0 or stats_persistent == '0':
                warnings.append(('innodb_stats_persistent', CONFIG_WARNINGS['innodb_stats_persistent']))
        
        # Check 5: innodb_stats_auto_recalc == 'OFF'
        stats_auto_recalc = get_val('innodb_stats_auto_recalc')
        if stats_auto_recalc is not None:
            if str(stats_auto_recalc).upper() == 'OFF' or stats_auto_recalc == 0 or stats_auto_recalc == '0':
                warnings.append(('innodb_stats_auto_recalc', CONFIG_WARNINGS['innodb_stats_auto_recalc']))
        
        # Check 6: binlog_row_image == 'FULL' AND binlog_format == 'ROW' AND log_bin == 'ON'
        # Only check when binary logging is enabled
        log_bin = get_val('log_bin')
        binlog_row_image = get_val('binlog_row_image')
        binlog_format = get_val('binlog_format')
        if log_bin is not None and binlog_row_image is not None and binlog_format is not None:
            # log_bin can be 'ON', '1', or 1
            log_bin_enabled = str(log_bin).upper() in ('ON', '1')
            if log_bin_enabled and str(binlog_row_image).upper() == 'FULL' and str(binlog_format).upper() == 'ROW':
                warnings.append(('binlog_row_image', CONFIG_WARNINGS['binlog_row_image']))
        
        # Check 7: default_storage_engine != 'InnoDB'
        default_engine = get_val('default_storage_engine')
        if default_engine is not None:
            if str(default_engine).upper() != 'INNODB':
                warnings.append(('default_storage_engine', CONFIG_WARNINGS['default_storage_engine']))
        
        # Check 8: log_output == 'TABLE' AND (general_log == 'ON' OR slow_query_log == 'ON')
        # Only check when at least one of the logs is enabled
        log_output = get_val('log_output')
        general_log = get_val('general_log')
        slow_query_log = get_val('slow_query_log')
        if log_output is not None:
            if str(log_output).upper() == 'TABLE':
                # Check if either general_log or slow_query_log is enabled
                general_log_enabled = general_log is not None and str(general_log).upper() in ('ON', '1')
                slow_query_log_enabled = slow_query_log is not None and str(slow_query_log).upper() in ('ON', '1')
                if general_log_enabled or slow_query_log_enabled:
                    warnings.append(('log_output', CONFIG_WARNINGS['log_output']))
        
        # Check 9: max_connect_errors > 10000
        max_connect_errors = get_val('max_connect_errors')
        if max_connect_errors is not None:
            try:
                if int(max_connect_errors) > 10000:
                    warnings.append(('max_connect_errors', CONFIG_WARNINGS['max_connect_errors']))
            except (ValueError, TypeError):
                pass
    
    except (IndexError, KeyError):
        pass
    
    return warnings


def evaluate_key_performance_counters(parsed_logs: Dict[str, 'ParsedLogFile'], max_connections: int = 0) -> List[Tuple[str, Any]]:
    """
    Transform key_performance_counters query results into column format.
    
    The query returns rows from SHOW GLOBAL STATUS, but we need to display
    as a single row with columns: Queries Per Second, Total Queries, Slow Queries,
    Threads Connected, Threads Running, Aborted Connections, Aborted Clients.
    
    Args:
        parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
        max_connections: The max_connections server variable value
        
    Returns:
        List containing a single tuple with all counter values, or empty list if no data.
    """
    if 'key_performance_counters' not in parsed_logs:
        return []
    
    log_data = parsed_logs['key_performance_counters']
    if not log_data.success or not log_data.rows or not log_data.columns:
        return []
    
    # Build a map of variable_name -> value from the rows
    values = {}
    for row in log_data.rows:
        if len(row) >= 2:
            var_name = str(row[0]).lower()
            values[var_name] = row[1]
    
    try:
        queries = int(values.get('queries', 0))
        uptime = int(values.get('uptime', 1))  # Avoid division by zero
        slow_queries = int(values.get('slow_queries', 0))
        threads_connected = int(values.get('threads_connected', 0))
        threads_running = int(values.get('threads_running', 0))
        aborted_connects = int(values.get('aborted_connects', 0))
        aborted_clients = int(values.get('aborted_clients', 0))
        
        # Calculate queries per second
        qps = round(queries / uptime, 2) if uptime > 0 else 0
        
        # Format threads connected with max_connections
        if max_connections > 0:
            threads_connected_str = f"{threads_connected} / {max_connections}"
        else:
            threads_connected_str = str(threads_connected)
        
        return [(qps, queries, slow_queries, threads_connected_str, threads_running, aborted_connects, aborted_clients)]
    
    except (ValueError, TypeError, KeyError):
        return []


def evaluate_table_cache_stats(parsed_logs: Dict[str, 'ParsedLogFile']) -> List[Tuple[str, str, str]]:
    """
    Transform table_cache_stats and table_cache_config query results into rows with Metric, Value, Explanation.
    
    Args:
        parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
        
    Returns:
        List of (Metric, Value, Explanation) tuples, or empty list if no data.
    """
    rows = []
    
    # Get config values (table_open_cache, table_open_cache_instances)
    table_open_cache = 0
    table_open_cache_instances = 0
    
    if 'table_cache_config' in parsed_logs:
        config_data = parsed_logs['table_cache_config']
        if config_data.success and config_data.rows and config_data.columns:
            col_map = {col.lower(): idx for idx, col in enumerate(config_data.columns)}
            row = config_data.rows[0]
            if 'table_open_cache' in col_map:
                try:
                    table_open_cache = int(row[col_map['table_open_cache']])
                except (ValueError, TypeError):
                    pass
            if 'table_open_cache_instances' in col_map:
                try:
                    table_open_cache_instances = int(row[col_map['table_open_cache_instances']])
                except (ValueError, TypeError):
                    pass
    
    # Get status values (Open_tables, Opened_tables)
    open_tables = 0
    opened_tables = 0
    
    if 'table_cache_stats' in parsed_logs:
        stats_data = parsed_logs['table_cache_stats']
        if stats_data.success and stats_data.rows:
            for row in stats_data.rows:
                if len(row) >= 2:
                    var_name = str(row[0]).lower()
                    try:
                        if var_name == 'open_tables':
                            open_tables = int(row[1])
                        elif var_name == 'opened_tables':
                            opened_tables = int(row[1])
                    except (ValueError, TypeError):
                        pass
    
    # If we have no data at all, return empty
    if table_open_cache == 0 and open_tables == 0 and opened_tables == 0:
        return []
    
    # Calculate hit rate
    if opened_tables > 0:
        hit_rate = round((open_tables / opened_tables) * 100, 2)
        hit_rate_str = f"{hit_rate}%"
    else:
        hit_rate_str = "N/A"
    
    # Build the rows
    rows.append(('table_open_cache', str(table_open_cache), 'Maximum number of cached table handles'))
    rows.append(('table_open_cache_instances', str(table_open_cache_instances), 'Number of cache instances (for concurrency)'))
    rows.append(('Open Tables', f"{open_tables:,}", 'Currently open tables'))
    rows.append(('Opened Tables', f"{opened_tables:,}", 'Total tables opened since startup'))
    rows.append(('Table Cache Hit Rate', hit_rate_str, 'Higher is better (>95% ideal)'))
    
    return rows


def evaluate_temp_table_usage(parsed_logs: Dict[str, 'ParsedLogFile']) -> Optional[Dict[str, Any]]:
    """
    Transform temp_table_usage query results into a dictionary with metrics.
    
    Args:
        parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
        
    Returns:
        Dictionary with created_tmp_tables, created_tmp_disk_tables, disk_tmp_table_pct,
        or None if no data.
    """
    if 'temp_table_usage' not in parsed_logs:
        return None
    
    log_data = parsed_logs['temp_table_usage']
    if not log_data.success or not log_data.rows or not log_data.columns:
        return None
    
    # Build column map
    col_map = {col.lower(): idx for idx, col in enumerate(log_data.columns)}
    row = log_data.rows[0]
    
    try:
        created_tmp_tables = 0
        created_tmp_disk_tables = 0
        disk_tmp_table_pct = 0.0
        
        if 'created_tmp_tables' in col_map:
            created_tmp_tables = int(row[col_map['created_tmp_tables']] or 0)
        if 'created_tmp_disk_tables' in col_map:
            created_tmp_disk_tables = int(row[col_map['created_tmp_disk_tables']] or 0)
        if 'disk_tmp_table_pct' in col_map:
            val = row[col_map['disk_tmp_table_pct']]
            disk_tmp_table_pct = float(val) if val is not None else 0.0
        
        return {
            'created_tmp_tables': created_tmp_tables,
            'created_tmp_disk_tables': created_tmp_disk_tables,
            'disk_tmp_table_pct': disk_tmp_table_pct
        }
    except (ValueError, TypeError, IndexError):
        return None


def _evaluate_health_checks_common(parsed_logs: Dict[str, 'ParsedLogFile'], evaluate_config_warnings_func=None) -> List[Tuple[str, str, str]]:
    """
    Shared health check evaluation logic used by all report generators.
    
    Returns list of (status, check_name, details) tuples where status is 'critical', 'warning', or 'ok'.
    """
    health_checks = []
    
    def get_val(query_id: str, col_idx: int = 0) -> Optional[str]:
        if query_id in parsed_logs:
            log = parsed_logs[query_id]
            if log.success and log.rows and len(log.rows) > 0 and len(log.rows[0]) > col_idx:
                return str(log.rows[0][col_idx]) if log.rows[0][col_idx] is not None else None
        return None
    
    def get_count(query_id: str) -> int:
        if query_id in parsed_logs:
            log = parsed_logs[query_id]
            if log.success and log.rows:
                return len(log.rows)
        return 0
    
    def get_val_by_name(query_id: str, *column_names: str) -> Optional[str]:
        """
        Read a value from the first row of a query by column name.
        
        Used where the column position is not stable across engine variants.
        Candidate names are tried in order and matched case-insensitively.
        
        Returns None if the query is absent, failed, returned no rows, carries no
        column metadata, matched none of the candidate names, or the value is NULL.
        """
        if query_id not in parsed_logs:
            return None
        log = parsed_logs[query_id]
        if not log.success or not log.rows or not log.columns:
            return None
        positions = {name.lower(): idx for idx, name in enumerate(log.columns)}
        for column_name in column_names:
            idx = positions.get(column_name.lower())
            if idx is not None and idx < len(log.rows[0]):
                value = log.rows[0][idx]
                return str(value) if value is not None else None
        return None
    
    # Check 1: Redo Log Occupancy
    redo_pct = get_val('health_redo_log_occupancy', 1)
    if redo_pct:
        try:
            pct = float(redo_pct)
            if pct > 80:
                health_checks.append(('critical', 'Redo Log Occupancy', f'{pct:.1f}% (>80% critical)'))
            elif pct > 60:
                health_checks.append(('warning', 'Redo Log Occupancy', f'{pct:.1f}% (>60% warning)'))
            else:
                health_checks.append(('ok', 'Redo Log Occupancy', f'{pct:.1f}%'))
        except (ValueError, TypeError):
            pass
    
    # Check 2: Tables Without Primary Keys
    tables_no_pk = get_val('health_tables_without_pk', 0)
    if tables_no_pk:
        try:
            count = int(tables_no_pk)
            if count > 0:
                health_checks.append(('critical', 'Tables Without Primary Key', f'{count} table(s)'))
            else:
                health_checks.append(('ok', 'Tables Without Primary Key', 'None'))
        except (ValueError, TypeError):
            pass
    
    # Check 3: Empty Passwords
    empty_pwd = get_val('health_empty_passwords', 0)
    if empty_pwd:
        try:
            count = int(empty_pwd)
            if count > 0:
                health_checks.append(('critical', 'Empty Passwords', f'{count} account(s)'))
            else:
                health_checks.append(('ok', 'Empty Passwords', 'None'))
        except (ValueError, TypeError):
            pass
    
    # Check 4: Connection Usage
    conn_pct = get_val('health_connection_usage', 0)
    if conn_pct:
        try:
            pct = float(conn_pct)
            if pct > 90:
                health_checks.append(('critical', 'Connection Usage', f'{pct:.1f}% (>90% critical)'))
            elif pct > 75:
                health_checks.append(('warning', 'Connection Usage', f'{pct:.1f}% (>75% warning)'))
            else:
                health_checks.append(('ok', 'Connection Usage', f'{pct:.1f}%'))
        except (ValueError, TypeError):
            pass
    
    # Check 5: History List Length
    hll = get_val('innodb_history_length', 0)
    if hll:
        try:
            length = int(hll)
            if length > 100000:
                health_checks.append(('critical', 'History List Length', f'{length:,} (>100K critical)'))
            elif length > 50000:
                health_checks.append(('warning', 'History List Length', f'{length:,} (>50K warning)'))
            else:
                health_checks.append(('ok', 'History List Length', f'{length:,}'))
        except (ValueError, TypeError):
            pass
    
    # Check 6: Buffer Pool Hit Rate
    hit_rate = get_val('health_buffer_pool_hit_rate', 0)
    if hit_rate:
        try:
            rate = float(hit_rate)
            if rate < 95:
                health_checks.append(('warning', 'Buffer Pool Hit Rate', f'{rate:.2f}% (<95% warning)'))
            else:
                health_checks.append(('ok', 'Buffer Pool Hit Rate', f'{rate:.2f}%'))
        except (ValueError, TypeError):
            pass
    
    # Check 7: Replication Lag
    # Read Seconds_Behind_Source from the always-collected replica_status query by
    # column name. MySQL 8.0.23+ reports Seconds_Behind_Source; SHOW SLAVE STATUS
    # on older MySQL and on MariaDB reports Seconds_Behind_Master. Position is not
    # stable across those variants, so the lookup is by name.
    # The value is NULL when the SQL thread is stopped, which surfaces as None and
    # skips the check rather than reporting it as zero lag.
    rep_lag = get_val_by_name('replica_status', 'Seconds_Behind_Source', 'Seconds_Behind_Master')
    if rep_lag is not None:
        try:
            lag = float(rep_lag)
            if lag > 60:
                health_checks.append(('critical', 'Replication Lag', f'{lag:.0f}s (>60s critical)'))
            elif lag > 10:
                health_checks.append(('warning', 'Replication Lag', f'{lag:.0f}s (>10s warning)'))
            else:
                health_checks.append(('ok', 'Replication Lag', f'{lag:.0f}s'))
        except (ValueError, TypeError):
            pass
    
    # Check 8: Replication Errors
    # health_replication_errors returns no rows on a non-replica, so the check is
    # skipped there rather than reporting OK on an instance with no applier at all.
    rep_errors = get_val('health_replication_errors', 0)
    if rep_errors is not None:
        try:
            count = int(rep_errors)
            if count > 0:
                health_checks.append(('critical', 'Replication Errors', f'{count} worker(s)'))
            else:
                health_checks.append(('ok', 'Replication Errors', 'None'))
        except (ValueError, TypeError):
            pass
    
    # Check 9: Auto-Increment Critical
    ai_critical = get_val('health_auto_increment_critical', 0)
    if ai_critical is not None:
        try:
            count = int(ai_critical)
            if count > 0:
                health_checks.append(('critical', 'Auto-Increment >90% Full', f'{count} column(s)'))
            else:
                health_checks.append(('ok', 'Auto-Increment Usage', 'All within limits'))
        except (ValueError, TypeError):
            pass
    
    # Check 10: Server Configuration
    if evaluate_config_warnings_func:
        config_warnings = evaluate_config_warnings_func(parsed_logs)
        if config_warnings:
            health_checks.append(('warning', 'Server Configuration', 'Issues detected'))
        else:
            health_checks.append(('ok', 'Server Configuration', 'OK'))
    
    # Check 11: Disk Temporary Table Usage
    temp_data = evaluate_temp_table_usage(parsed_logs)
    if temp_data:
        disk_pct = temp_data['disk_tmp_table_pct']
        if disk_pct > 80:
            health_checks.append(('critical', 'Disk Tmp Table %', f'{disk_pct:.1f}% (>80% critical)'))
        elif disk_pct > 50:
            health_checks.append(('warning', 'Disk Tmp Table %', f'{disk_pct:.1f}% (>50% warning)'))
        else:
            health_checks.append(('ok', 'Disk Tmp Table %', f'{disk_pct:.1f}%'))
    
    # Check 12: Anonymous Accounts
    anon_count = get_count('anonymous_accounts')
    if anon_count > 0:
        health_checks.append(('critical', 'Anonymous Accounts', f'{anon_count} account(s)'))
    else:
        health_checks.append(('ok', 'Anonymous Accounts', 'None'))
    
    # Check 13: Wildcard Host Accounts
    wildcard_count = get_count('wildcard_hosts')
    if wildcard_count > 0:
        health_checks.append(('warning', 'Wildcard Host Accounts', f'{wildcard_count} account(s)'))
    else:
        health_checks.append(('ok', 'Wildcard Host Accounts', 'None'))
    
    # Check 14: Long-Running Queries
    long_query_count = get_count('long_running_queries')
    if long_query_count > 50:
        health_checks.append(('warning', 'Long-Running Queries', f'{long_query_count} (>50 warning)'))
    elif long_query_count > 0:
        health_checks.append(('ok', 'Long-Running Queries', f'{long_query_count}'))
    else:
        health_checks.append(('ok', 'Long-Running Queries', 'None'))
    
    # Check 15: Long-Running Transactions
    idle_trx = get_count('long_running_idle_trx') or get_count('long_running_idle_trx_ps')
    active_trx = get_count('long_running_active_trx') or get_count('long_running_active_trx_ps')
    total_long_trx = idle_trx + active_trx
    if total_long_trx > 0:
        health_checks.append(('warning', 'Long-Running Transactions', f'{total_long_trx} (idle: {idle_trx}, active: {active_trx})'))
    else:
        health_checks.append(('ok', 'Long-Running Transactions', 'None'))
    
    # Check 16: Deadlocks
    deadlock_val = get_val('deadlock_count', 1)
    if deadlock_val:
        try:
            count = int(deadlock_val)
            if count > 0:
                health_checks.append(('warning', 'Deadlocks (since restart)', f'{count:,}'))
            else:
                health_checks.append(('ok', 'Deadlocks', 'None'))
        except (ValueError, TypeError):
            pass
    
    # Check 17: Unused Indexes
    unused_idx_count = get_count('unused_indexes')
    if unused_idx_count > 10:
        health_checks.append(('ok', 'Unused Indexes', f'{unused_idx_count} (review recommended)'))
    elif unused_idx_count > 0:
        health_checks.append(('ok', 'Unused Indexes', f'{unused_idx_count}'))
    else:
        health_checks.append(('ok', 'Unused Indexes', 'None'))
    
    # Check 18: Foreign Keys Without Indexes
    fk_no_idx_count = get_count('fk_without_indexes')
    if fk_no_idx_count > 0:
        health_checks.append(('warning', 'FK Without Indexes', f'{fk_no_idx_count} column(s)'))
    else:
        health_checks.append(('ok', 'FK Without Indexes', 'None'))
    
    # Check 19: Non-InnoDB Tables
    non_innodb_count = get_count('non_innodb_tables')
    if non_innodb_count > 0:
        health_checks.append(('warning', 'Non-InnoDB Tables', f'{non_innodb_count} table(s)'))
    else:
        health_checks.append(('ok', 'Non-InnoDB Tables', 'None'))
    
    # Check 20: Stale Statistics
    stale_stats_count = get_count('stale_statistics')
    if stale_stats_count > 0:
        health_checks.append(('warning', 'Stale Statistics', f'{stale_stats_count} table(s) >7 days old'))
    else:
        health_checks.append(('ok', 'Stale Statistics', 'None'))
    
    # Check 21: Instant DDL Row Versions
    if 'instant_ddl_tracking' in parsed_logs:
        log = parsed_logs['instant_ddl_tracking']
        if log.success and log.rows and log.columns:
            try:
                col_idx = log.columns.index('TOTAL_ROW_VERSIONS')
                critical_cnt = sum(1 for row in log.rows if row[col_idx] is not None and int(row[col_idx]) > 150)
                warning_cnt = sum(1 for row in log.rows if row[col_idx] is not None and int(row[col_idx]) > 50)
                if critical_cnt > 0:
                    health_checks.append(('critical', 'Instant DDL Row Versions', f'{critical_cnt} table(s) >150'))
                elif warning_cnt > 0:
                    health_checks.append(('warning', 'Instant DDL Row Versions', f'{warning_cnt} table(s) >50'))
                else:
                    health_checks.append(('ok', 'Instant DDL Row Versions', 'All within limits'))
            except (ValueError, IndexError):
                pass
    
    # Check 22: Elevated Privileges Users
    elevated_count = get_count('elevated_privileges_users')
    if elevated_count > 0:
        health_checks.append(('ok', 'Elevated Privileges Users', f'{elevated_count} user(s)'))
    else:
        health_checks.append(('ok', 'Elevated Privileges Users', 'None'))
    
    # Check 23: Long-Running Idle Connections
    idle_conn_count = get_count('long_running_idle_connections')
    if idle_conn_count > 100:
        health_checks.append(('warning', 'Idle Connections >5min', f'{idle_conn_count} (>100 warning)'))
    elif idle_conn_count > 0:
        health_checks.append(('ok', 'Idle Connections >5min', f'{idle_conn_count}'))
    else:
        health_checks.append(('ok', 'Idle Connections >5min', 'None'))
    
    # Check 24: Query Failures
    # health_ queries are counted here too. A health query that fails produces no
    # health line of its own, so excluding it here would leave the failure
    # unreported anywhere in the output.
    failed_queries = sum(1 for qid, log in parsed_logs.items() if not log.success)
    if failed_queries > 0:
        health_checks.append(('warning', 'Query Failures', f'{failed_queries} of {len(parsed_logs)} queries failed'))
    
    return health_checks


class ExpressionEvaluator:
    """
    Evaluates arithmetic expressions with column references.
    
    This class parses and evaluates expressions like "(col1 + col2) / col3 * 100"
    against a row of data provided as a dictionary mapping column names to values.
    
    Features:
    - Supports operators: +, -, *, / with standard precedence
    - Supports nested parentheses
    - Treats NULL values as 0 in arithmetic operations
    - Logs warning and returns None for invalid column references
    
    Responsibilities:
    - Evaluate computed column expressions using query result data
    - Support operators: +, -, *, /, parentheses
    - Log warning when computed column references non-existent column
    - Treat NULL values as 0 in arithmetic expressions
    """
    
    def __init__(self, expr: str):
        """
        Initialize the evaluator with an expression.
        
        Args:
            expr: The expression string to evaluate (e.g., "(col1 + col2) / col3 * 100")
        """
        self._expr = expr.strip()
        self._tokens: List[Tuple[str, str]] = []
        self._pos = 0
        self._tokenize()
    
    def _tokenize(self) -> None:
        """
        Tokenize the expression into a list of (token_type, token_value) tuples.
        
        Token types:
        - 'NUMBER': numeric literal (integer or decimal)
        - 'IDENTIFIER': column name
        - 'OPERATOR': +, -, *, /
        - 'LPAREN': (
        - 'RPAREN': )
        """
        self._tokens = []
        i = 0
        expr = self._expr
        
        while i < len(expr):
            char = expr[i]
            
            # Skip whitespace
            if char.isspace():
                i += 1
                continue
            
            # Operators
            if char in '+-*/':
                self._tokens.append(('OPERATOR', char))
                i += 1
                continue
            
            # Parentheses
            if char == '(':
                self._tokens.append(('LPAREN', char))
                i += 1
                continue
            
            if char == ')':
                self._tokens.append(('RPAREN', char))
                i += 1
                continue
            
            # Numbers (integer or decimal)
            if char.isdigit() or (char == '.' and i + 1 < len(expr) and expr[i + 1].isdigit()):
                start = i
                has_decimal = False
                
                while i < len(expr):
                    c = expr[i]
                    if c.isdigit():
                        i += 1
                    elif c == '.' and not has_decimal:
                        has_decimal = True
                        i += 1
                    else:
                        break
                
                number_str = expr[start:i]
                self._tokens.append(('NUMBER', number_str))
                continue
            
            # Identifiers (column names) - start with letter or underscore
            if char.isalpha() or char == '_':
                start = i
                while i < len(expr) and (expr[i].isalnum() or expr[i] == '_'):
                    i += 1
                
                identifier = expr[start:i]
                self._tokens.append(('IDENTIFIER', identifier))
                continue
            
            # Unknown character - skip (validation is done at startup)
            i += 1
    
    def evaluate(self, row: Dict[str, Any]) -> Optional[float]:
        """
        Evaluate the expression against a row of data.
        
        Args:
            row: Dictionary mapping column names to values
            
        Returns:
            The computed value as a float, or None if evaluation fails
            (e.g., invalid column reference, division by zero)
        """
        if not self._tokens:
            return None
        
        self._pos = 0
        self._row = row
        self._missing_columns: List[str] = []
        
        try:
            result = self._parse_expression()
            
            # Check if we consumed all tokens
            if self._pos < len(self._tokens):
                return None
            
            # Log warnings for missing columns
            for col in self._missing_columns:
                logger.warning(
                    f"Computed column expression '{self._expr}' references "
                    f"non-existent column '{col}'"
                )
            
            # If any columns were missing, return None
            if self._missing_columns:
                return None
            
            return result
        except (ValueError, ZeroDivisionError, IndexError):
            return None
    
    def _current_token(self) -> Optional[Tuple[str, str]]:
        """Get the current token without advancing."""
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None
    
    def _advance(self) -> Optional[Tuple[str, str]]:
        """Get the current token and advance to the next."""
        token = self._current_token()
        if token:
            self._pos += 1
        return token
    
    def _parse_expression(self) -> float:
        """
        Parse an expression (handles + and - with lowest precedence).
        
        Grammar:
        expression := term (('+' | '-') term)*
        """
        result = self._parse_term()
        
        while True:
            token = self._current_token()
            if token and token[0] == 'OPERATOR' and token[1] in '+-':
                op = self._advance()[1]
                right = self._parse_term()
                if op == '+':
                    result = result + right
                else:
                    result = result - right
            else:
                break
        
        return result
    
    def _parse_term(self) -> float:
        """
        Parse a term (handles * and / with higher precedence).
        
        Grammar:
        term := factor (('*' | '/') factor)*
        """
        result = self._parse_factor()
        
        while True:
            token = self._current_token()
            if token and token[0] == 'OPERATOR' and token[1] in '*/':
                op = self._advance()[1]
                right = self._parse_factor()
                if op == '*':
                    result = result * right
                else:
                    if right == 0:
                        raise ZeroDivisionError("Division by zero")
                    result = result / right
            else:
                break
        
        return result
    
    def _parse_factor(self) -> float:
        """
        Parse a factor (handles unary minus, parentheses, numbers, and identifiers).
        
        Grammar:
        factor := '-' factor | '(' expression ')' | NUMBER | IDENTIFIER
        """
        token = self._current_token()
        
        if not token:
            raise ValueError("Unexpected end of expression")
        
        # Handle unary minus
        if token[0] == 'OPERATOR' and token[1] == '-':
            self._advance()
            return -self._parse_factor()
        
        # Handle parentheses
        if token[0] == 'LPAREN':
            self._advance()  # consume '('
            result = self._parse_expression()
            
            # Expect closing parenthesis
            close_token = self._current_token()
            if not close_token or close_token[0] != 'RPAREN':
                raise ValueError("Expected closing parenthesis")
            self._advance()  # consume ')'
            return result
        
        # Handle numbers
        if token[0] == 'NUMBER':
            self._advance()
            return float(token[1])
        
        # Handle identifiers (column references)
        if token[0] == 'IDENTIFIER':
            self._advance()
            col_name = token[1]
            return self._get_column_value(col_name)
        
        raise ValueError(f"Unexpected token: {token}")
    
    def _get_column_value(self, col_name: str) -> float:
        """
        Get the value of a column from the row data.
        
        Args:
            col_name: The column name to look up
            
        Returns:
            The column value as a float (NULL values are treated as 0)
            
        Note:
            If the column doesn't exist, it's added to _missing_columns
            and 0 is returned to allow expression evaluation to complete.
        """
        if col_name not in self._row:
            # Track missing column for warning
            self._missing_columns.append(col_name)
            return 0.0
        
        value = self._row[col_name]
        
        # Treat NULL as 0
        if value is None:
            return 0.0
        
        # Convert to float
        try:
            return float(value)
        except (ValueError, TypeError):
            # Non-numeric value - treat as 0
            return 0.0


@dataclass
class ParsedLogFile:
    """
    Represents the parsed content of a query log file.
    
    Attributes:
        query_id: The query identifier extracted from the log file
        success: True if the query executed successfully, False if it failed
        columns: List of column names (None if query failed)
        rows: List of row data as tuples (None if query failed)
        error: Error message (None if query succeeded)
        executed_timestamp: Timestamp when the query was executed
        select_now_timestamp: Timestamp from SELECT NOW() (None if query failed)
        query_command: The SQL command that was executed (None if query failed)
    """
    query_id: str
    success: bool
    columns: Optional[List[str]]
    rows: Optional[List[Tuple[Any, ...]]]
    error: Optional[str]
    executed_timestamp: Optional[str]
    select_now_timestamp: Optional[str] = None
    query_command: Optional[str] = None


class LogFileParser:
    """
    Parser for query log files generated by QueryExecutor.
    
    This class reads the consolidated queries-output.log file and parses
    it to extract query results, column names, row data, and error messages.
    
    The consolidated log file format contains multiple queries:
    
    For successful queries:
    ```
    ================================================================================
    Query: {query_id}
    Executed: {timestamp}
    ================================================================================
    SELECT NOW(): {timestamp}
    --------------------------------------------------------------------------------
    {query_command}
    --------------------------------------------------------------------------------
    {column1}\t{column2}\t{column3}
    {value1}\t{value2}\t{value3}
    ...
    
    ```
    
    For failed queries:
    ```
    ================================================================================
    Query: {query_id}
    Executed: {timestamp}
    ================================================================================
    ERROR: {error_message}
    
    ```
    """
    
    # Consolidated log file name
    QUERIES_OUTPUT_FILE = "queries-output.log"
    
    # Pattern to match query log files (legacy support): {QUERY_ID}_YYYYMMDD_HHMMSS.log
    LOG_FILE_PATTERN = re.compile(r'^(.+)_(\d{8}_\d{6})\.log$')
    
    # Pattern to exclude main execution log
    MAIN_LOG_PATTERN = re.compile(r'^diagnostic_collector_mysql_\d{8}_\d{6}\.log$')
    
    # Pattern to match query section separator
    QUERY_SEPARATOR = "=" * 80
    
    def __init__(self, output_dir: str):
        """
        Initialize the LogFileParser.
        
        Args:
            output_dir: Path to the output directory containing query log files
        """
        self._output_dir = output_dir
    
    def _get_consolidated_log_path(self) -> str:
        """Get the path to the consolidated log file."""
        return os.path.join(self._output_dir, self.QUERIES_OUTPUT_FILE)
    
    def scan_log_files(self) -> List[str]:
        """
        Scan the output directory for query log files.
        
        Returns:
            List of paths to query log files (excludes main execution log).
            Returns consolidated log file if it exists, otherwise falls back
            to individual log files for backward compatibility.
        """
        if not os.path.isdir(self._output_dir):
            return []
        
        # Check for consolidated log file first
        consolidated_path = self._get_consolidated_log_path()
        if os.path.isfile(consolidated_path):
            return [consolidated_path]
        
        # Fall back to individual log files (legacy support)
        log_files = []
        for filename in os.listdir(self._output_dir):
            # Skip main execution log
            if self.MAIN_LOG_PATTERN.match(filename):
                continue
            
            # Match query log files
            if self.LOG_FILE_PATTERN.match(filename):
                log_files.append(os.path.join(self._output_dir, filename))
        
        return sorted(log_files)
    
    def extract_query_id_from_filename(self, filepath: str) -> Optional[str]:
        """
        Extract the query ID from a log file path.
        
        Args:
            filepath: Path to the log file
            
        Returns:
            The query ID or None if the filename doesn't match the expected pattern
        """
        filename = os.path.basename(filepath)
        match = self.LOG_FILE_PATTERN.match(filename)
        if match:
            return match.group(1)
        return None
    
    def parse_log_file(self, filepath: str) -> ParsedLogFile:
        """
        Parse a single query log file (legacy support).
        
        Args:
            filepath: Path to the log file to parse
            
        Returns:
            ParsedLogFile containing the extracted data
        """
        # Extract query ID from filename as fallback
        query_id_from_filename = self.extract_query_id_from_filename(filepath)
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except IOError as e:
            # Return error result if file cannot be read
            return ParsedLogFile(
                query_id=query_id_from_filename or "UNKNOWN",
                success=False,
                columns=None,
                rows=None,
                error=f"Failed to read log file: {e}",
                executed_timestamp=None
            )
        
        return self._parse_content(content, query_id_from_filename)
    
    def _parse_consolidated_log(self, filepath: str) -> Dict[str, ParsedLogFile]:
        """
        Parse the consolidated queries-output.log file.
        
        Args:
            filepath: Path to the consolidated log file
            
        Returns:
            Dictionary mapping query IDs to their parsed log data
        """
        results = {}
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except IOError as e:
            logger.error(f"Failed to read consolidated log file: {e}")
            return results
        
        # Split content by query separator
        # Each query block starts with "=" * 80
        query_blocks = self._split_query_blocks(content)
        
        for block in query_blocks:
            if not block.strip():
                continue
            
            parsed = self._parse_content(block, None)
            if parsed.query_id and parsed.query_id != "UNKNOWN":
                results[parsed.query_id] = parsed
        
        return results
    
    def _split_query_blocks(self, content: str) -> List[str]:
        """
        Split consolidated log content into individual query blocks.
        
        Each query block has the format:
        ================================================================================
        Query: {query_id}
        Executed: {timestamp}
        ================================================================================
        SELECT NOW(): {timestamp}
        --------------------------------------------------------------------------------
        {query_command}
        --------------------------------------------------------------------------------
        {columns}
        {data rows}
        
        Note: Each query block has TWO separator lines (=====), so we need to
        group them together properly.
        
        Args:
            content: The full content of the consolidated log file
            
        Returns:
            List of query block strings
        """
        blocks = []
        current_block = []
        lines = content.split('\n')
        separator_count = 0
        
        for line in lines:
            if line == self.QUERY_SEPARATOR:
                separator_count += 1
                current_block.append(line)
                
                # Every 2nd separator marks the end of a query header
                # The block continues until the next 1st separator (start of next query)
                # So we split when we see an odd separator (1st, 3rd, 5th, etc.)
                # after we already have content
                if separator_count % 2 == 1 and len(current_block) > 1:
                    # This is the start of a new query block
                    # Save everything before this separator as the previous block
                    prev_block = current_block[:-1]  # Exclude the current separator
                    if prev_block and any(l.strip() for l in prev_block):
                        blocks.append('\n'.join(prev_block))
                    current_block = [line]  # Start new block with this separator
            else:
                current_block.append(line)
        
        # Add the last block
        if current_block and any(l.strip() for l in current_block):
            blocks.append('\n'.join(current_block))
        
        return blocks
    
    def _parse_content(
        self,
        content: str,
        fallback_query_id: Optional[str]
    ) -> ParsedLogFile:
        """
        Parse the content of a log file.
        
        Args:
            content: The raw content of the log file
            fallback_query_id: Query ID extracted from filename (used as fallback)
            
        Returns:
            ParsedLogFile containing the extracted data
        """
        lines = content.split('\n')
        
        # Parse header section
        query_id = None
        executed_timestamp = None
        
        for line in lines:
            if line.startswith('Query: '):
                query_id = line[7:].strip()
            elif line.startswith('Executed: '):
                executed_timestamp = line[10:].strip()
            elif line.startswith('ERROR:') or line.startswith('SELECT NOW():'):
                break
        
        # Use fallback query ID if not found in content
        if not query_id:
            query_id = fallback_query_id or "UNKNOWN"
        
        # Check if this is an error log
        error_line = None
        for line in lines:
            if line.startswith('ERROR:'):
                error_line = line
                break
        
        if error_line:
            # Parse error log
            error_message = error_line[6:].strip()
            return ParsedLogFile(
                query_id=query_id,
                success=False,
                columns=None,
                rows=None,
                error=error_message,
                executed_timestamp=executed_timestamp
            )
        
        # Parse successful query log
        return self._parse_success_content(lines, query_id, executed_timestamp)
    
    def _parse_success_content(
        self,
        lines: List[str],
        query_id: str,
        executed_timestamp: Optional[str]
    ) -> ParsedLogFile:
        """
        Parse the content of a successful query log.
        
        The expected format is:
        ================================================================================
        Query: {query_id}
        Executed: {timestamp}
        ================================================================================
        SELECT NOW(): {timestamp}
        --------------------------------------------------------------------------------
        {query_command}
        --------------------------------------------------------------------------------
        {column1}\t{column2}\t...
        {value1}\t{value2}\t...
        
        Args:
            lines: Lines of the log file
            query_id: The query ID
            executed_timestamp: The execution timestamp
            
        Returns:
            ParsedLogFile containing the extracted data
        """
        select_now_timestamp = None
        query_command = None
        columns = None
        rows = []
        
        # State machine for parsing
        # States: header -> after_header_sep -> after_select_now -> query_command -> data
        state = 'header'
        header_sep_count = 0
        
        for line in lines:
            # In header state, look for the second separator line (after Query/Executed)
            if state == 'header':
                if line.startswith('=' * 10):
                    header_sep_count += 1
                    if header_sep_count >= 2:
                        state = 'after_header_sep'
                continue
            
            # After header separator, look for SELECT NOW line
            if state == 'after_header_sep':
                if line.startswith('SELECT NOW():'):
                    select_now_timestamp = line[13:].strip()
                    state = 'after_select_now'
                elif line.startswith('-' * 10):
                    # Some queries might not have SELECT NOW, go directly to query command
                    state = 'query_command'
                continue
            
            # After SELECT NOW, look for first dashed separator
            if state == 'after_select_now':
                if line.startswith('-' * 10):
                    state = 'query_command'
                continue
            
            # In query_command state, collect query until next dashed separator
            if state == 'query_command':
                if line.startswith('-' * 10):
                    state = 'data'
                else:
                    if query_command is None:
                        query_command = line
                    else:
                        query_command += '\n' + line
                continue
            
            # In data state, first line is columns, rest are rows
            if state == 'data':
                # Skip empty lines and separator lines
                if not line.strip() or line.startswith('=' * 10):
                    continue
                
                # First non-empty line after separator is column names
                if columns is None:
                    columns = line.split('\t')
                else:
                    # Parse row data
                    row_values = self._parse_row(line, len(columns) if columns else 0)
                    rows.append(tuple(row_values))
        
        return ParsedLogFile(
            query_id=query_id,
            success=True,
            columns=columns,
            rows=rows if rows else None,
            error=None,
            executed_timestamp=executed_timestamp,
            select_now_timestamp=select_now_timestamp,
            query_command=query_command.strip() if query_command else None
        )
    
    def _parse_row(self, line: str, expected_columns: int) -> List[Any]:
        """
        Parse a row of tab-separated values.
        
        Args:
            line: The line containing tab-separated values
            expected_columns: Expected number of columns
            
        Returns:
            List of parsed values (with NULL converted to None)
        """
        values = line.split('\t')
        
        # Convert NULL strings back to None
        parsed_values = []
        for val in values:
            if val == 'NULL':
                parsed_values.append(None)
            else:
                # Try to convert to appropriate type
                parsed_values.append(self._convert_value(val))
        
        return parsed_values
    
    def _convert_value(self, value: str) -> Any:
        """
        Convert a string value to its appropriate Python type.
        
        Args:
            value: The string value to convert
            
        Returns:
            The converted value (int, float, or original string)
        """
        # Try integer conversion
        try:
            return int(value)
        except ValueError:
            pass
        
        # Try float conversion
        try:
            return float(value)
        except ValueError:
            pass
        
        # Return as string
        return value
    
    def parse_all_logs(self) -> Dict[str, ParsedLogFile]:
        """
        Parse all query log files in the output directory.
        
        Supports both consolidated log file (queries-output.log) and
        individual log files (legacy format) for backward compatibility.
        
        Returns:
            Dictionary mapping query IDs to their parsed log data
        """
        # Check for consolidated log file first
        consolidated_path = self._get_consolidated_log_path()
        if os.path.isfile(consolidated_path):
            return self._parse_consolidated_log(consolidated_path)
        
        # Fall back to individual log files (legacy support)
        results = {}
        for filepath in self.scan_log_files():
            parsed = self.parse_log_file(filepath)
            results[parsed.query_id] = parsed
        
        return results


class CollectionLimitEnforcer:
    """
    Enforces collection limits on report subsections.

    This class limits the number of rows displayed per subsection to the
    configured collection limit value. When a subsection has more rows than
    the limit, only the first N rows are shown.

    Responsibilities:
    - Limit displayed rows per subsection to --collection-limit value
    - Default to 20 rows per subsection when not specified
    """

    DEFAULT_LIMIT = 20

    def __init__(self, collection_limit: Optional[int] = None):
        """
        Initialize the CollectionLimitEnforcer.

        Args:
            collection_limit: Maximum rows per subsection. Defaults to 20 if None.
        """
        self._limit = collection_limit if collection_limit is not None else self.DEFAULT_LIMIT

    @property
    def limit(self) -> int:
        """Get the current collection limit."""
        return self._limit

    def apply_limit(
        self,
        rows: Optional[List[Tuple[Any, ...]]],
        subsection_name: Optional[str] = None
    ) -> Tuple[Optional[List[Tuple[Any, ...]]], bool, int]:
        """
        Apply the collection limit to a list of rows.

        Args:
            rows: List of row tuples to limit, or None
            subsection_name: Optional name of the subsection (for logging)

        Returns:
            Tuple of:
            - Limited rows (or None if input was None)
            - Boolean indicating if rows were truncated
            - Total row count before limiting
        """
        if rows is None:
            return None, False, 0

        total_count = len(rows)

        if total_count <= self._limit:
            return rows, False, total_count

        # Truncate to limit
        limited_rows = rows[:self._limit]

        if subsection_name:
            logger.info(
                f"Subsection '{subsection_name}': Showing {self._limit} of {total_count} rows"
            )

        return limited_rows, True, total_count

    def get_truncation_message(self, total_count: int) -> str:
        """
        Generate a truncation message for display in the report.

        Args:
            total_count: Total number of rows before truncation

        Returns:
            Human-readable truncation message
        """
        return f"Showing {self._limit} of {total_count} rows"





class HTMLReportGenerator:
    """
    Generates HTML reports from parsed query log data.
    
    This class creates a complete, self-contained HTML document with:
    - Header with timestamp, database info, and table of contents
    - Health Overview section with critical issues and warnings
    - Sections organized per report_format.json
    - Each section has Section_Overview and subsections
    - Tables with column headers and data rows
    - Consistent styling with inline CSS
    
    Responsibilities:
    - Generate HTML report as default output format
    - Include Health Overview section at the top
    - Organize report by sections defined in report_format.json
    - Include Section_Overview at the start of each section
    - Use consistent color scheme for section headings
    - Include table of contents with links to sections
    - Include timestamp and database information in report header
    """
    
    # Color scheme - unique to Diagnostic Collector for MySQL
    COLORS = {
        'primary': '#2c5282',       # Deep blue for headers
        'secondary': '#4a5568',     # Gray for secondary text
        'accent': '#38a169',        # Green for success/healthy
        'warning': '#d69e2e',       # Amber for warnings
        'critical': '#c53030',      # Red for critical issues
        'background': '#f7fafc',    # Light gray background
        'card_bg': '#ffffff',       # White card background
        'border': '#e2e8f0',        # Light border color
        'table_header': '#edf2f7',  # Table header background
        'table_stripe': '#f8fafc',  # Alternating row color
        'link': '#3182ce',          # Link color
    }
    
    # Configuration warning messages for the 9 checked parameters
    CONFIG_WARNINGS = {
        'sync_binlog': "⚠️ WARNING: sync_binlog is not set to 1 - binary log may not be synchronized to disk after each transaction, risking data loss during system crashes.",
        'innodb_flush_log_at_trx_commit': "⚠️ WARNING: innodb_flush_log_at_trx_commit is not set to 1 - transaction logs may not be flushed to disk after each commit, compromising ACID durability guarantees.",
        'foreign_key_checks': "⚠️ WARNING: foreign_key_checks is disabled - referential integrity constraints are not enforced, allowing invalid data relationships and potential impact to data integrity.",
        'innodb_stats_persistent': "⚠️ WARNING: innodb_stats_persistent is disabled - table statistics will not persist across server restarts, causing suboptimal query execution plans.",
        'innodb_stats_auto_recalc': "⚠️ WARNING: innodb_stats_auto_recalc is disabled - table statistics will not be automatically updated, potentially leading to poor query performance over time.",
        'binlog_row_image': "⚠️ WARNING: binlog_row_image is set to FULL with ROW format - this generates excessive binary log data and may impact replication performance.",
        'default_storage_engine': "⚠️ WARNING: default_storage_engine is not InnoDB - using non-transactional storage engines compromises ACID compliance and crash recovery capabilities.",
        'log_output': "⚠️ WARNING: log_output is set to TABLE - storing logs in tables can cause performance degradation and potential circular logging issues.",
        'max_connect_errors': "⚠️ WARNING: max_connect_errors configured to high value - overly permissive connection error threshold may allow brute force attacks and mask connection issues.",
    }
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        extended_checks: bool = False
    ):
        """
        Initialize the HTMLReportGenerator.
        
        Args:
            report_format: ReportFormat object defining the report structure
            collection_limit: Maximum rows per subsection (default: 50)
            timestamp: Report generation timestamp (default: current time)
            database_info: Dictionary with database information (host, version, etc.)
            extended_checks: Whether extended checks are enabled (default: False)
        """
        self._report_format = report_format
        self._limit_enforcer = CollectionLimitEnforcer(collection_limit)
        self._timestamp = timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._database_info = database_info or {}
        self._expression_cache: Dict[str, ExpressionEvaluator] = {}
        self._extended_checks = extended_checks
        # Parse database version for version filtering
        self._database_version = self._parse_version(self._database_info.get('version', ''))
    
    def _parse_version(self, version_string: str) -> tuple:
        """
        Parse version string into a tuple of integers for comparison.
        
        Handles formats like:
        - "8.0.35" (MySQL)
        - "8.0.35-0ubuntu0.22.04.1" (MySQL with suffix)
        - "11.4.2-MariaDB" (MariaDB)
        - "3.05.2" (Aurora)
        
        Args:
            version_string: Version string to parse
            
        Returns:
            Tuple of (major, minor, patch) integers
        """
        if not version_string:
            return (0, 0, 0)
        
        # Extract version numbers from the beginning of the string
        match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', version_string)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0
            return (major, minor, patch)
        
        return (0, 0, 0)
    
    def _evaluate_config_warnings(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> List[Tuple[str, str]]:
        """
        Evaluate configuration parameters and return warnings for problematic values.
        
        This method delegates to the module-level evaluate_config_warnings function.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            
        Returns:
            List of (Parameter, Message) tuples for triggered warnings.
            Empty list if no warnings or query data not available.
        """
        return evaluate_config_warnings(parsed_logs)
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """
        Generate a complete HTML report from parsed log data.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            engine_type: The detected database engine type (for condition filtering)
            
        Returns:
            Complete HTML document as a string
        """
        # Anchors are unique per document, so the registry resets per report.
        self._emitted_anchor_ids = set()
        html_parts = [
            self._generate_html_header(),
            self._generate_body_start(),
            self._generate_report_header(parsed_logs, engine_type),
            self._generate_table_of_contents(),
            self._generate_health_overview(parsed_logs, engine_type),
        ]
        
        # Generate each section from report_format with section numbers
        is_first_section = True
        for section in self._report_format.sections:
            # Get section number from report_format
            section_number = 0
            try:
                section_number = int(float(section.number)) if section.number else 0
            except ValueError:
                section_number = 0
            # Use custom generator for Server Information section
            if section.name == "Server Information & Version Details":
                section_html = self._generate_server_info_section(section, parsed_logs, engine_type, section_number, is_first_section)
            elif section.name == "Non-Default Configuration Variables":
                section_html = self._generate_non_default_vars_section(section, parsed_logs, engine_type)
            elif section.name == "Replication Topology & Status":
                section_html = self._generate_replication_section(section, parsed_logs, engine_type)
            else:
                section_html = self._generate_section(section, parsed_logs, engine_type, section_number, is_first_section)
            if section_html:
                html_parts.append(section_html)
                is_first_section = False
        
        html_parts.extend([
            self._generate_footer(),
            self._generate_body_end(),
            self._generate_html_footer(),
        ])
        
        return '\n'.join(html_parts)
    
    def _generate_html_header(self) -> str:
        """Generate the HTML document header with inline CSS."""
        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Diagnostic Collector for MySQL Report - {self._timestamp}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #f5f5f5;
        }}
        
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}
        
        .header h1 {{
            margin: 0;
            font-size: 2.5em;
        }}
        
        .info-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }}
        
        .info-box {{
            background: rgba(255,255,255,0.1);
            padding: 10px;
            border-radius: 5px;
        }}
        
        .nav {{
            background: white;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            position: sticky;
            top: 10px;
            z-index: 100;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        
        .nav a {{
            color: #667eea;
            text-decoration: none;
            margin-right: 20px;
            font-weight: 500;
        }}
        
        .nav a:hover {{
            text-decoration: underline;
        }}
        
        details {{
            background: white;
            margin-bottom: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            scroll-margin-top: 90px;
        }}
        
        summary {{
            background: linear-gradient(to right, #667eea, #764ba2);
            color: white;
            padding: 15px 20px;
            cursor: pointer;
            font-size: 1.2em;
            font-weight: bold;
            user-select: none;
            border-radius: 10px 10px 0 0;
        }}
        
        summary:hover {{
            background: linear-gradient(to right, #5568d3, #6a3f8f);
        }}
        
        details[open] summary {{
            border-radius: 10px 10px 0 0;
        }}
        
        .content {{
            padding: 20px;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 10px 0;
        }}
        
        th {{
            background: #667eea;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            position: sticky;
            top: 0;
        }}
        
        td {{
            padding: 10px 12px;
            border-bottom: 1px solid #e0e0e0;
        }}
        
        tr:hover {{
            background-color: #f8f9fa;
        }}
        
        .warning {{
            background-color: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 10px;
            margin: 10px 0;
        }}
        
        .critical {{
            background-color: #f8d7da;
            border-left: 4px solid #dc3545;
            padding: 10px;
            margin: 10px 0;
        }}
        
        .success {{
            background-color: #d4edda;
            border-left: 4px solid #28a745;
            padding: 10px;
            margin: 10px 0;
        }}
        
        .info {{
            background-color: #d1ecf1;
            border-left: 4px solid #17a2b8;
            padding: 10px;
            margin: 10px 0;
        }}
        
        .metric-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 15px;
            margin: 15px 0;
        }}
        
        .metric-card {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            border-left: 4px solid #667eea;
        }}
        
        .metric-label {{
            font-size: 0.9em;
            color: #666;
            margin-bottom: 5px;
        }}
        
        .metric-value {{
            font-size: 1.5em;
            font-weight: bold;
            color: #333;
        }}
        
        code {{
            background: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: monospace;
        }}
        
        pre {{
            background: #f4f4f4;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
        }}
        
        h3 {{
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
            margin-top: 20px;
        }}
        
        .copy-btn {{
            background: #667eea;
            color: white;
            border: none;
            padding: 1px 6px;
            border-radius: 3px;
            cursor: pointer;
            font-size: 0.7em;
            margin-left: 3px;
        }}
        
        .copy-btn:hover {{
            background: #5568d3;
        }}
        
        .copy-btn:active {{
            background: #4557c2;
        }}
        
        .health-overview {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        
        .health-overview h2 {{
            margin-top: 0;
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }}
        
        .health-stats {{
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }}
        
        .health-stat {{
            flex: 1;
            min-width: 150px;
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }}
        
        .health-stat.critical {{
            background: #f8d7da;
            border: 1px solid #dc3545;
        }}
        
        .health-stat.warning {{
            background: #fff3cd;
            border: 1px solid #ffc107;
        }}
        
        .health-stat.healthy {{
            background: #d4edda;
            border: 1px solid #28a745;
        }}
        
        .health-stat .count {{
            font-size: 32px;
            font-weight: bold;
        }}
        
        .health-stat.critical .count {{
            color: #dc3545;
        }}
        
        .health-stat.warning .count {{
            color: #ffc107;
        }}
        
        .health-stat.healthy .count {{
            color: #28a745;
        }}
        
        .health-stat .label {{
            font-size: 14px;
            color: #666;
        }}
        
        .metric-cards {{
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            margin-top: 10px;
        }}
        
        .metric-card {{
            flex: 1;
            min-width: 150px;
            max-width: 200px;
            padding: 15px;
            background: #f8f9fa;
            border: 1px solid #dee2e6;
            border-radius: 8px;
            text-align: center;
        }}
        
        .metric-card .metric-value {{
            font-size: 24px;
            font-weight: bold;
            color: #333;
        }}
        
        .metric-card .metric-label {{
            font-size: 12px;
            color: #666;
            margin-top: 5px;
        }}
        
        .issues-list {{
            margin-top: 15px;
        }}
        
        .issues-list h3 {{
            font-size: 16px;
            margin-bottom: 10px;
            color: #666;
        }}
        
        .issue-item {{
            padding: 10px 15px;
            margin-bottom: 8px;
            border-radius: 4px;
            font-size: 14px;
        }}
        
        .issue-item.critical {{
            background: #f8d7da;
            border-left: 4px solid #dc3545;
        }}
        
        .issue-item.warning {{
            background: #fff3cd;
            border-left: 4px solid #ffc107;
        }}
        
        .subsection {{
            margin-bottom: 25px;
            /*
             * The navigation bar is position: sticky, so a fragment jump that lands a
             * heading at viewport top puts it underneath the bar. scroll-margin-top
             * reserves the bar's height plus its offset and a little breathing room.
             */
            scroll-margin-top: 90px;
        }}
        
        .subsection:last-child {{
            margin-bottom: 0;
        }}
        
        .subsection h3 {{
            color: #333;
            font-size: 16px;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 2px solid #667eea;
        }}
        
        .data-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        
        .data-table th {{
            background: #667eea;
            color: white;
            font-weight: 600;
            text-align: left;
            padding: 12px;
        }}
        
        .data-table td {{
            padding: 10px 12px;
            border-bottom: 1px solid #e0e0e0;
            vertical-align: top;
        }}
        
        .data-table tr:nth-child(even) {{
            background: #f8f9fa;
        }}
        
        .data-table tr:hover {{
            background: #f8f9fa;
        }}
        
        .truncation-notice {{
            font-size: 12px;
            color: #ffc107;
            font-style: italic;
            margin-top: 8px;
        }}
        
        .no-data {{
            color: #666;
            font-style: italic;
            padding: 15px;
            text-align: center;
            background: #f8f9fa;
            border-radius: 4px;
        }}
        
        .error-message {{
            color: #dc3545;
            background: #f8d7da;
            padding: 10px 15px;
            border-radius: 4px;
            font-size: 13px;
        }}
        
        .footer {{
            text-align: center;
            padding: 20px;
            color: #666;
            font-size: 12px;
        }}
        
        @media (max-width: 768px) {{
            .nav {{
                position: static;
            }}
            
            /* The bar is static at this width, so the reserved space would be a gap. */
            .subsection,
            details {{
                scroll-margin-top: 10px;
            }}
            
            .health-stats {{
                flex-direction: column;
            }}
            
            .health-stat {{
                min-width: 100%;
            }}
        }}
    </style>
    <script>
        function copyToClipboard(text) {{
            navigator.clipboard.writeText(text).then(function() {{
                alert('Copied to clipboard: ' + text);
            }}, function(err) {{
                prompt('Copy this text:', text);
            }});
        }}

        /*
         * Open the <details> ancestors of a fragment target.
         *
         * Only the first section is rendered open, so a link into any later section
         * points at an element inside a collapsed <details>, and a fragment jump to a
         * target with no layout does nothing. Section links in the navigation bar do
         * not need this, because their ids sit on the <details> elements themselves,
         * which have layout while collapsed.
         */
        function revealFragmentTarget() {{
            var id = decodeURIComponent(window.location.hash.replace(/^#/, ''));
            if (!id) {{
                return;
            }}
            var target = document.getElementById(id);
            if (!target) {{
                return;
            }}
            var opened = false;
            for (var node = target; node; node = node.parentElement) {{
                if (node.tagName === 'DETAILS' && !node.open) {{
                    node.open = true;
                    opened = true;
                }}
            }}
            /*
             * Re-scroll only when something was expanded. The browser has already
             * scrolled by this point, against a layout that did not include the
             * revealed content, so the original position is stale. Leaving an
             * already-visible target alone keeps the browser's own behavior.
             */
            if (opened) {{
                target.scrollIntoView();
            }}
        }}

        window.addEventListener('hashchange', revealFragmentTarget);
        /*
         * Also run on load, so a report opened directly at a fragment behaves the same
         * as one navigated within. hashchange does not fire for the initial URL.
         */
        window.addEventListener('DOMContentLoaded', revealFragmentTarget);
    </script>
</head>'''
    
    def _generate_body_start(self) -> str:
        """Generate the body opening tag."""
        return '''<body>
'''
    
    def _generate_report_header(self, parsed_logs: Optional[Dict[str, ParsedLogFile]] = None, engine_type: Optional[str] = None) -> str:
        """
        Generate the report header with timestamp and database info.
        
        Include timestamp and database information
        """
        host = self._database_info.get('host', 'Unknown')
        port = self._database_info.get('port', 'Unknown')
        version = self._database_info.get('version', 'Unknown')
        engine_type = engine_type or self._database_info.get('engine_type', '')
        
        # Helper to get value from parsed_logs
        def get_log_value(query_id: str, column_name: str) -> Optional[str]:
            if parsed_logs and query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return str(log_data.rows[0][col_idx])
                    except (ValueError, IndexError):
                        pass
            return None
        
        # Build info boxes
        info_boxes = f'''
        <div class="info-box"><div>Hostname</div><div style="font-size:1.2em;font-weight:bold">{self._escape_html(host)}</div></div>'''
        
        # Only show Aurora-specific fields if it's Aurora MySQL
        if engine_type == 'aurora-mysql':
            aurora_server_id = get_log_value('aurora_server_id', 'aurora_server_id')
            aurora_version = get_log_value('aurora_version', 'aurora_version')
            innodb_read_only = get_log_value('server_read_only', 'innodb_read_only')
            
            if aurora_server_id:
                info_boxes += f'''
        <div class="info-box"><div>Aurora Server ID</div><div style="font-size:1.2em;font-weight:bold">{self._escape_html(aurora_server_id)}</div></div>'''
            
            info_boxes += f'''
        <div class="info-box"><div>Port</div><div style="font-size:1.2em;font-weight:bold">{self._escape_html(str(port))}</div></div>
        <div class="info-box"><div>MySQL Version</div><div style="font-size:1.2em;font-weight:bold">{self._escape_html(version)}</div></div>'''
            
            if aurora_version:
                info_boxes += f'''
        <div class="info-box"><div>Aurora Version</div><div style="font-size:1.2em;font-weight:bold">{self._escape_html(aurora_version)}</div></div>'''
            
            # Format read-only status
            if innodb_read_only in ('1', 'ON', 'True'):
                ro_status = "Yes (Reader)"
            else:
                ro_status = "No (Writer)"
            info_boxes += f'''
        <div class="info-box"><div>Read Only (InnoDB)</div><div style="font-size:1.2em;font-weight:bold">{ro_status}</div></div>'''
        else:
            # Non-Aurora: just show Port and MySQL Version
            info_boxes += f'''
        <div class="info-box"><div>Port</div><div style="font-size:1.2em;font-weight:bold">{self._escape_html(str(port))}</div></div>
        <div class="info-box"><div>MySQL Version</div><div style="font-size:1.2em;font-weight:bold">{self._escape_html(version)}</div></div>'''
        
        info_boxes += f'''
        <div class="info-box"><div>Report Time</div><div style="font-size:1.2em;font-weight:bold">{self._timestamp}</div></div>'''
        
        return f'''
<div class="header">
    <h1>Diagnostic Collector for MySQL Report</h1>
    <div class="info-grid">{info_boxes}
    </div>
</div>
'''
    
    def _generate_table_of_contents(self) -> str:
        """
        Generate the Quick Navigation with pipe-separated links to sections.
        
        Include table of contents with links to sections
        """
        section_nav_items = []
        
        # Sort sections by number for proper ordering
        sorted_sections = sorted(
            self._report_format.sections,
            key=lambda s: (float(s.number) if s.number else 999)
        )
        
        for section in sorted_sections:
            section_id = self._make_section_id(section.name)
            section_num = section.number
            display_name = section.short_name or section.name
            if section_num:
                section_nav_items.append(f'<a href="#{section_id}">{section_num}. {display_name}</a>')
            else:
                section_nav_items.append(f'<a href="#{section_id}">{display_name}</a>')
        
        nav_links = ' | '.join(section_nav_items)
        
        return f'''
<div class="nav">
    <strong>Table of contents:</strong><br>
    {nav_links}
</div>
'''
    
    def _generate_health_overview(self, parsed_logs: Dict[str, ParsedLogFile], engine_type: Optional[str] = None) -> str:
        """
        Generate the Health Overview section with Executive Summary health checks.
        
        Include Health Overview section with critical issues and warnings
        """
        # Health check results: list of tuples (status, check_name, details)
        # status: 'critical', 'warning', 'ok'
        health_checks = []
        
        # Count failed queries, including health_ queries: a health query that
        # fails produces no health line of its own, so excluding it here would
        # leave the failure unreported anywhere in the output.
        failed_queries = sum(1 for query_id, log in parsed_logs.items()
                           if not log.success)
        total_queries = len(parsed_logs)
        
        # === Helper Functions ===
        
        def get_health_value(query_id: str, column_idx: int = 0) -> Optional[str]:
            """Get single value from health check query."""
            if query_id in parsed_logs:
                log = parsed_logs[query_id]
                if log.success and log.rows and len(log.rows) > 0:
                    row = log.rows[0]
                    if len(row) > column_idx:
                        return str(row[column_idx]) if row[column_idx] is not None else None
            return None
        
        def get_row_count(query_id: str) -> int:
            """Get row count from query results."""
            if query_id in parsed_logs:
                log = parsed_logs[query_id]
                if log.success and log.rows:
                    return len(log.rows)
            return 0
        
        def get_health_value_by_name(query_id: str, *column_names: str) -> Optional[str]:
            """
            Read a value from the first row of a query by column name.
            
            Used where the column position is not stable across engine variants.
            Candidate names are tried in order and matched case-insensitively.
            Returns None if the query is absent, failed, returned no rows, carries
            no column metadata, matched no candidate name, or the value is NULL.
            """
            if query_id not in parsed_logs:
                return None
            log = parsed_logs[query_id]
            if not log.success or not log.rows or not log.columns:
                return None
            positions = {name.lower(): idx for idx, name in enumerate(log.columns)}
            for column_name in column_names:
                idx = positions.get(column_name.lower())
                if idx is not None and idx < len(log.rows[0]):
                    value = log.rows[0][idx]
                    return str(value) if value is not None else None
            return None
        
        # === Executive Summary Health Checks ===
        
        # Check 1: Redo Log Occupancy
        redo_pct = get_health_value('health_redo_log_occupancy', 1)
        if redo_pct:
            try:
                pct = float(redo_pct)
                if pct > 80:
                    health_checks.append(('critical', 'Redo Log Occupancy', f'{pct:.1f}% (>80% critical)'))
                elif pct > 60:
                    health_checks.append(('warning', 'Redo Log Occupancy', f'{pct:.1f}% (>60% warning)'))
                else:
                    health_checks.append(('ok', 'Redo Log Occupancy', f'{pct:.1f}%'))
            except (ValueError, TypeError):
                pass
        
        # Check 2: Tables Without Primary Keys
        tables_no_pk = get_health_value('health_tables_without_pk', 0)
        if tables_no_pk:
            try:
                count = int(tables_no_pk)
                if count > 0:
                    health_checks.append(('critical', 'Tables Without Primary Key', f'{count} table(s)'))
                else:
                    health_checks.append(('ok', 'Tables Without Primary Key', 'None'))
            except (ValueError, TypeError):
                pass
        
        # Check 3: Empty Passwords
        empty_pwd = get_health_value('health_empty_passwords', 0)
        if empty_pwd:
            try:
                count = int(empty_pwd)
                if count > 0:
                    health_checks.append(('critical', 'Empty Passwords', f'{count} account(s)'))
                else:
                    health_checks.append(('ok', 'Empty Passwords', 'None'))
            except (ValueError, TypeError):
                pass
        
        # Check 4: Connection Usage
        conn_pct = get_health_value('health_connection_usage', 0)
        if conn_pct:
            try:
                pct = float(conn_pct)
                if pct > 90:
                    health_checks.append(('critical', 'Connection Usage', f'{pct:.1f}% (>90% critical)'))
                elif pct > 75:
                    health_checks.append(('warning', 'Connection Usage', f'{pct:.1f}% (>75% warning)'))
                else:
                    health_checks.append(('ok', 'Connection Usage', f'{pct:.1f}%'))
            except (ValueError, TypeError):
                pass
        
        # Check 5: History List Length
        hll = get_health_value('innodb_history_length', 0)
        if hll:
            try:
                length = int(hll)
                if length > 100000:
                    health_checks.append(('critical', 'History List Length', f'{length:,} (>100K critical)'))
                elif length > 50000:
                    health_checks.append(('warning', 'History List Length', f'{length:,} (>50K warning)'))
                else:
                    health_checks.append(('ok', 'History List Length', f'{length:,}'))
            except (ValueError, TypeError):
                pass
        
        # Check 6: Buffer Pool Hit Rate
        hit_rate = get_health_value('health_buffer_pool_hit_rate', 0)
        if hit_rate:
            try:
                rate = float(hit_rate)
                if rate < 95:
                    health_checks.append(('warning', 'Buffer Pool Hit Rate', f'{rate:.2f}% (<95% warning)'))
                else:
                    health_checks.append(('ok', 'Buffer Pool Hit Rate', f'{rate:.2f}%'))
            except (ValueError, TypeError):
                pass
        
        # Check 7: Replication Lag
        # Read Seconds_Behind_Source from the always-collected replica_status query
        # by column name. MySQL 8.0.23+ reports Seconds_Behind_Source; SHOW SLAVE
        # STATUS on older MySQL and on MariaDB reports Seconds_Behind_Master.
        # NULL (SQL thread stopped) surfaces as None and skips the check rather
        # than reporting it as zero lag.
        rep_lag = get_health_value_by_name(
            'replica_status', 'Seconds_Behind_Source', 'Seconds_Behind_Master'
        )
        if rep_lag is not None:
            try:
                lag = float(rep_lag)
                if lag > 60:
                    health_checks.append(('critical', 'Replication Lag', f'{lag:.0f}s (>60s critical)'))
                elif lag > 10:
                    health_checks.append(('warning', 'Replication Lag', f'{lag:.0f}s (>10s warning)'))
                else:
                    health_checks.append(('ok', 'Replication Lag', f'{lag:.0f}s'))
            except (ValueError, TypeError):
                pass
        
        # Check 8: Replication Errors
        # health_replication_errors returns no rows on a non-replica, so the check
        # is skipped there rather than reporting OK where there is no applier.
        rep_errors = get_health_value('health_replication_errors', 0)
        if rep_errors is not None:
            try:
                count = int(rep_errors)
                if count > 0:
                    health_checks.append(('critical', 'Replication Errors', f'{count} worker(s)'))
                else:
                    health_checks.append(('ok', 'Replication Errors', 'None'))
            except (ValueError, TypeError):
                pass
        
        # Check 9: Auto-Increment Critical (>90%)
        ai_critical = get_health_value('health_auto_increment_critical', 0)
        if ai_critical is not None:
            try:
                count = int(ai_critical)
                if count > 0:
                    health_checks.append(('critical', 'Auto-Increment >90% Full', f'{count} column(s)'))
                else:
                    health_checks.append(('ok', 'Auto-Increment Usage', 'All within limits'))
            except (ValueError, TypeError):
                pass
        
        # Check 10: Server Configuration Warnings
        config_warnings = self._evaluate_config_warnings(parsed_logs)
        if config_warnings:
            health_checks.append(('warning', 'Server Configuration', 'Issues detected'))
        else:
            health_checks.append(('ok', 'Server Configuration', 'OK'))
        
        # Check 11: Disk Temporary Table Usage
        temp_data = evaluate_temp_table_usage(parsed_logs)
        if temp_data:
            disk_pct = temp_data['disk_tmp_table_pct']
            if disk_pct > 80:
                health_checks.append(('critical', 'Disk Tmp Table %', f'{disk_pct:.1f}% (>80% critical)'))
            elif disk_pct > 50:
                health_checks.append(('warning', 'Disk Tmp Table %', f'{disk_pct:.1f}% (>50% warning)'))
            else:
                health_checks.append(('ok', 'Disk Tmp Table %', f'{disk_pct:.1f}%'))
        
        # === NEW HEALTH CHECKS ===
        
        # Check 12: Anonymous Accounts (Critical if any)
        anon_count = get_row_count('anonymous_accounts')
        if anon_count > 0:
            health_checks.append(('critical', 'Anonymous Accounts', f'{anon_count} account(s)'))
        else:
            health_checks.append(('ok', 'Anonymous Accounts', 'None'))
        
        # Check 13: Wildcard Host Accounts (Warning if any)
        wildcard_count = get_row_count('wildcard_hosts')
        if wildcard_count > 0:
            health_checks.append(('warning', 'Wildcard Host Accounts', f'{wildcard_count} account(s)'))
        else:
            health_checks.append(('ok', 'Wildcard Host Accounts', 'None'))
        
        # Check 14: Long-Running Queries (Warning if >50)
        long_query_count = get_row_count('long_running_queries')
        if long_query_count > 50:
            health_checks.append(('warning', 'Long-Running Queries', f'{long_query_count} (>50 warning)'))
        elif long_query_count > 0:
            health_checks.append(('ok', 'Long-Running Queries', f'{long_query_count}'))
        else:
            health_checks.append(('ok', 'Long-Running Queries', 'None'))
        
        # Check 15: Long-Running Transactions (Warning if any)
        idle_trx_count = get_row_count('long_running_idle_trx')
        active_trx_count = get_row_count('long_running_active_trx')
        # Also check performance_schema versions
        if idle_trx_count == 0:
            idle_trx_count = get_row_count('long_running_idle_trx_ps')
        if active_trx_count == 0:
            active_trx_count = get_row_count('long_running_active_trx_ps')
        total_long_trx = idle_trx_count + active_trx_count
        if total_long_trx > 0:
            health_checks.append(('warning', 'Long-Running Transactions', f'{total_long_trx} (idle: {idle_trx_count}, active: {active_trx_count})'))
        else:
            health_checks.append(('ok', 'Long-Running Transactions', 'None'))
        
        # Check 16: Deadlocks (Warning if >0)
        deadlock_val = get_health_value('deadlock_count', 1)  # Value column is index 1
        if deadlock_val:
            try:
                count = int(deadlock_val)
                if count > 0:
                    health_checks.append(('warning', 'Deadlocks (since restart)', f'{count:,}'))
                else:
                    health_checks.append(('ok', 'Deadlocks', 'None'))
            except (ValueError, TypeError):
                pass
        
        # Check 17: Unused Indexes (Info if >10)
        unused_idx_count = get_row_count('unused_indexes')
        if unused_idx_count > 10:
            health_checks.append(('ok', 'Unused Indexes', f'{unused_idx_count} (review recommended)'))
        elif unused_idx_count > 0:
            health_checks.append(('ok', 'Unused Indexes', f'{unused_idx_count}'))
        else:
            health_checks.append(('ok', 'Unused Indexes', 'None'))
        
        # Check 18: Foreign Keys Without Indexes (Warning if any)
        fk_no_idx_count = get_row_count('fk_without_indexes')
        if fk_no_idx_count > 0:
            health_checks.append(('warning', 'FK Without Indexes', f'{fk_no_idx_count} column(s)'))
        else:
            health_checks.append(('ok', 'FK Without Indexes', 'None'))
        
        # Check 19: Non-InnoDB Tables (Warning if any)
        non_innodb_count = get_row_count('non_innodb_tables')
        if non_innodb_count > 0:
            health_checks.append(('warning', 'Non-InnoDB Tables', f'{non_innodb_count} table(s)'))
        else:
            health_checks.append(('ok', 'Non-InnoDB Tables', 'None'))
        
        # Check 20: Stale Statistics (Warning if any)
        stale_stats_count = get_row_count('stale_statistics')
        if stale_stats_count > 0:
            health_checks.append(('warning', 'Stale Statistics', f'{stale_stats_count} table(s) >7 days old'))
        else:
            health_checks.append(('ok', 'Stale Statistics', 'None'))
        
        # Check 21: Instant DDL Row Versions (Warning >50, Critical >150)
        if 'instant_ddl_tracking' in parsed_logs:
            log = parsed_logs['instant_ddl_tracking']
            if log.success and log.rows:
                # Find max TOTAL_ROW_VERSIONS
                try:
                    col_idx = log.columns.index('TOTAL_ROW_VERSIONS')
                    max_versions = max(int(row[col_idx]) for row in log.rows if row[col_idx] is not None)
                    critical_count = sum(1 for row in log.rows if row[col_idx] is not None and int(row[col_idx]) > 150)
                    warning_count = sum(1 for row in log.rows if row[col_idx] is not None and int(row[col_idx]) > 50)
                    if critical_count > 0:
                        health_checks.append(('critical', 'Instant DDL Row Versions', f'{critical_count} table(s) >150'))
                    elif warning_count > 0:
                        health_checks.append(('warning', 'Instant DDL Row Versions', f'{warning_count} table(s) >50'))
                    else:
                        health_checks.append(('ok', 'Instant DDL Row Versions', 'All within limits'))
                except (ValueError, IndexError):
                    pass
        
        # Check 22: Elevated Privileges Users (Info)
        elevated_count = get_row_count('elevated_privileges_users')
        if elevated_count > 0:
            health_checks.append(('ok', 'Elevated Privileges Users', f'{elevated_count} user(s)'))
        else:
            health_checks.append(('ok', 'Elevated Privileges Users', 'None'))
        
        # Check 23: Long-Running Idle Connections (Warning if >100)
        idle_conn_count = get_row_count('long_running_idle_connections')
        if idle_conn_count > 100:
            health_checks.append(('warning', 'Idle Connections >5min', f'{idle_conn_count} (>100 warning)'))
        elif idle_conn_count > 0:
            health_checks.append(('ok', 'Idle Connections >5min', f'{idle_conn_count}'))
        else:
            health_checks.append(('ok', 'Idle Connections >5min', 'None'))
        
        # Add query failure warning
        if failed_queries > 0:
            health_checks.append(('warning', 'Query Failures', f'{failed_queries} of {total_queries} queries failed'))
        
        # Count by status
        critical_count = sum(1 for h in health_checks if h[0] == 'critical')
        warning_count = sum(1 for h in health_checks if h[0] == 'warning')
        
        # Build health checks table HTML with new format
        health_checks_html = '''
    <div class="health-checks">
        <h3>Summary</h3>
        <table class="data-table">
            <thead>
                <tr>
                    <th>Status</th>
                    <th>Check</th>
                    <th>Details</th>
                </tr>
            </thead>
            <tbody>'''
        
        # Add checks to table (critical first, then warning, then ok)
        for status, check_name, details in health_checks:
            if status == 'critical':
                health_checks_html += f'''
                <tr class="critical-row">
                    <td>🔴 CRITICAL</td>
                    <td>{self._get_health_check_link(check_name)}</td>
                    <td>{self._escape_html(details)}</td>
                </tr>'''
        
        for status, check_name, details in health_checks:
            if status == 'warning':
                health_checks_html += f'''
                <tr class="warning-row">
                    <td>🟡 WARNING</td>
                    <td>{self._get_health_check_link(check_name)}</td>
                    <td>{self._escape_html(details)}</td>
                </tr>'''
        
        for status, check_name, details in health_checks:
            if status == 'ok':
                health_checks_html += f'''
                <tr class="healthy-row">
                    <td>🟢 OK</td>
                    <td>{self._get_health_check_link(check_name)}</td>
                    <td>{self._escape_html(details)}</td>
                </tr>'''
        
        health_checks_html += '''
            </tbody>
        </table>
    </div>'''
        
        # Determine server type and Performance Schema status
        engine_type = engine_type or self._database_info.get('engine_type', 'mysql')
        if engine_type == 'aurora-mysql':
            server_type = 'Aurora'
        elif engine_type == 'mariadb':
            server_type = 'MariaDB'
        else:
            server_type = 'MySQL'
        
        # Get Performance Schema status
        perf_schema_status = 'Unknown'
        if 'performance_schema_status' in parsed_logs:
            log_data = parsed_logs['performance_schema_status']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    col_idx = log_data.columns.index('performance_schema_enabled')
                    enabled = log_data.rows[0][col_idx]
                    perf_schema_status = 'Enabled' if enabled in (1, '1', 'ON', True) else 'Disabled'
                except (ValueError, IndexError):
                    pass
        
        # Build Aurora Cluster info if Aurora
        aurora_cluster_html = ''
        if engine_type == 'aurora-mysql' and 'aurora_cluster_info' in parsed_logs:
            log_data = parsed_logs['aurora_cluster_info']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    writer_idx = log_data.columns.index('writer_count')
                    reader_idx = log_data.columns.index('reader_count')
                    total_idx = log_data.columns.index('total_instances')
                    
                    writer_count = log_data.rows[0][writer_idx]
                    reader_count = log_data.rows[0][reader_idx]
                    total_instances = log_data.rows[0][total_idx]
                    
                    reader_label = 'reader' if int(reader_count) == 1 else 'readers'
                    aurora_cluster_html = f'''
        <div class="health-stat healthy">
            <div class="count">{total_instances} instances</div>
            <div class="label">Aurora Cluster</div>
            <div class="sublabel">{writer_count} writer, {reader_count} {reader_label}</div>
        </div>'''
                except (ValueError, IndexError):
                    pass
        
        return f'''
<div class="health-overview" id="health-overview">
    <h2>Health Overview</h2>
    <div class="health-stats">
        <div class="health-stat critical">
            <div class="count">{critical_count}</div>
            <div class="label">Critical Issues</div>
        </div>
        <div class="health-stat warning">
            <div class="count">{warning_count}</div>
            <div class="label">Warnings</div>
        </div>
        <div class="health-stat healthy">
            <div class="count">{perf_schema_status}</div>
            <div class="label">Performance Schema</div>
        </div>
        <div class="health-stat healthy">
            <div class="count">{server_type}</div>
            <div class="label">Server Type</div>
        </div>{aurora_cluster_html}
    </div>
    {health_checks_html}
</div>'''
    
    def _generate_server_info_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str],
        section_number: int = 1,
        is_first: bool = True
    ) -> str:
        """
        Generate a custom Server Information section with consolidated table format.
        
        This creates a single table with Parameter/Value/Description columns
        matching the reference report format.
        """
        section_id = self._make_section_id(section.name)
        
        # Define server details with descriptions
        server_details = [
            ('server_version', 'MySQL Version', 'version', 'Full version string'),
            ('server_uuid', 'Server UUID', 'server_uuid', 'Unique server identifier for GTID replication'),
            ('server_hostname', 'Hostname', 'hostname', 'Server hostname'),
            ('server_port', 'Port', 'port', 'MySQL listening port'),
            ('server_datadir', 'Datadir', 'datadir', 'Data directory location'),
            ('server_charset', 'Character Set', 'character_set', 'Default character set'),
            ('server_charset', 'Collation', 'collation', 'Default collation'),
            ('server_timezone', 'Time Zone', 'system_time_zone', 'Server timezone settings'),
            ('server_read_only', 'Read Only', 'read_only', 'Prevents non-SUPER users from writes'),
            ('server_read_only', 'InnoDB Read Only', 'innodb_read_only', 'InnoDB read-only status'),
        ]
        
        # Build server details table rows
        detail_rows = []
        for source_id, param_name, column_name, description in server_details:
            if source_id in parsed_logs:
                log_data = parsed_logs[source_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        value = log_data.rows[0][col_idx]
                        # Format read_only values
                        if column_name in ('read_only', 'innodb_read_only'):
                            if value in (1, '1', 'ON', True):
                                value = '[YES] Yes'
                            else:
                                value = '[NO] No'
                        detail_rows.append(f'<tr><td>{self._escape_html(param_name)}</td><td>{self._escape_html(str(value))}</td><td>{self._escape_html(description)}</td></tr>')
                    except (ValueError, IndexError):
                        pass
        
        # Build uptime metric cards
        uptime_html = ''
        if 'server_uptime' in parsed_logs:
            log_data = parsed_logs['server_uptime']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    col_idx = log_data.columns.index('uptime_seconds')
                    uptime_seconds = int(log_data.rows[0][col_idx])
                    days = uptime_seconds // 86400
                    hours = (uptime_seconds % 86400) // 3600
                    minutes = (uptime_seconds % 3600) // 60
                    uptime_str = f"{days}d {hours}h {minutes}m"
                    uptime_html = f'''
<h3>Server Uptime</h3>
<div class="metric-grid">
    <div class="metric-card">
        <div class="metric-label">Uptime</div>
        <div class="metric-value">{uptime_str}</div>
    </div>
</div>'''
                except (ValueError, IndexError):
                    pass
        
        # Build Performance Schema status
        perf_schema_html = ''
        if 'performance_schema_status' in parsed_logs:
            log_data = parsed_logs['performance_schema_status']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    col_idx = log_data.columns.index('performance_schema_enabled')
                    enabled = log_data.rows[0][col_idx]
                    if enabled in (1, '1', 'ON', True):
                        perf_schema_html = '''
<h3>Performance Schema Status</h3>
<div class="success">Performance Schema is <strong>ENABLED</strong><br><small>Performance Schema provides detailed instrumentation for monitoring.</small></div>'''
                    else:
                        perf_schema_html = '''
<h3>Performance Schema Status</h3>
<div class="warning">Performance Schema is <strong>DISABLED</strong><br><small>Many advanced metrics will not be available.</small></div>'''
                except (ValueError, IndexError):
                    pass
        
        # Define consumer descriptions and impact
        consumer_info = {
            'thread_instrumentation': ('Enables per-thread instrumentation', 'Thread-level metrics unavailable'),
            'global_instrumentation': ('Enables global instrumentation', 'Global metrics unavailable'),
            'events_waits_current': ('Tracks current wait events', 'Wait event analysis unavailable'),
        }
        
        # Build Performance Schema Consumers table
        consumers_html = ''
        disabled_consumers = []
        if 'performance_schema_consumers' in parsed_logs:
            log_data = parsed_logs['performance_schema_consumers']
            if log_data.success and log_data.rows and log_data.columns:
                consumer_rows = []
                for row in log_data.rows:
                    try:
                        name_idx = log_data.columns.index('NAME')
                        enabled_idx = log_data.columns.index('ENABLED')
                        name = row[name_idx]
                        enabled = row[enabled_idx]
                        color = 'green' if enabled == 'YES' else 'red'
                        desc, impact = consumer_info.get(name, ('', 'Metrics may be unavailable'))
                        if enabled != 'YES':
                            disabled_consumers.append(name)
                        consumer_rows.append(f'<tr><td>{self._escape_html(str(name))}</td><td style="color:{color}">{self._escape_html(str(enabled))}</td><td>{self._escape_html(impact)}</td></tr>')
                    except (ValueError, IndexError):
                        pass
                if consumer_rows:
                    consumers_html = f'''
<h4>Performance Schema Consumers</h4>
<table class="data-table">
    <thead><tr><th>Consumer</th><th>Status</th><th>Impact if Disabled</th></tr></thead>
    <tbody>
        {chr(10).join(consumer_rows)}
    </tbody>
</table>'''
        
        # Define instrument descriptions
        instrument_info = {
            'memory': ('Memory allocation tracking', 'Memory analysis unavailable'),
            'wait': ('Wait event instrumentation', 'Wait analysis unavailable'),
        }
        
        # Build Performance Schema Instruments table
        instruments_html = ''
        disabled_instruments = []
        if 'performance_schema_instruments' in parsed_logs:
            log_data = parsed_logs['performance_schema_instruments']
            if log_data.success and log_data.rows and log_data.columns:
                instrument_rows = []
                for row in log_data.rows:
                    try:
                        class_idx = log_data.columns.index('instrument_class')
                        total_idx = log_data.columns.index('total_count')
                        enabled_idx = log_data.columns.index('enabled_count')
                        inst_class = row[class_idx]
                        total = int(row[total_idx])
                        enabled = int(row[enabled_idx])
                        color = 'green' if enabled == total else 'red'
                        desc, impact = instrument_info.get(inst_class, ('Instrumentation', 'Some metrics unavailable'))
                        if enabled < total:
                            disabled_instruments.append(f"{inst_class} ({enabled}/{total})")
                        instrument_rows.append(f'<tr><td>{self._escape_html(str(inst_class))}</td><td style="color:{color}">{enabled} of {total} enabled</td><td>{self._escape_html(impact)}</td></tr>')
                    except (ValueError, IndexError):
                        pass
                if instrument_rows:
                    instruments_html = f'''
<h4>Performance Schema Instruments</h4>
<table class="data-table">
    <thead><tr><th>Instrument Class</th><th>Status</th><th>Impact if Disabled</th></tr></thead>
    <tbody>
        {chr(10).join(instrument_rows)}
    </tbody>
</table>'''
        
        # Build warning notification if any consumers or instruments are disabled
        pfs_warning_html = ''
        if disabled_consumers or disabled_instruments:
            warning_items = []
            if disabled_consumers:
                for consumer in disabled_consumers:
                    warning_items.append(f"<code>UPDATE performance_schema.setup_consumers SET ENABLED='YES' WHERE NAME='{consumer}';</code>")
            if disabled_instruments:
                warning_items.append("<code>UPDATE performance_schema.setup_instruments SET ENABLED='YES' WHERE NAME LIKE 'memory/%';</code>")
                warning_items.append("<code>UPDATE performance_schema.setup_instruments SET ENABLED='YES' WHERE NAME LIKE 'wait/%';</code>")
            
            pfs_warning_html = f'''
<div class="warning"><strong>⚠️ To Enable Consumers/Instruments:</strong><br>
{chr(10).join(warning_items)}<br>
<small>Note: These changes take effect immediately and do not require a restart.</small></div>'''
        
        # Generate section overview
        overview_text = self._get_section_overview(section.name)
        
        # Add 'open' attribute to first section
        open_attr = ' open' if is_first else ''
        
        return f'''
<details id="{section_id}"{open_attr}><summary>{section_number}. Server Information & Version Details</summary><div class="content">

<div class="info">{self._escape_html(overview_text)}</div>

<h3>Server Details</h3>
<table><tr><th>Parameter</th><th>Value</th><th>Description</th></tr>
{chr(10).join(detail_rows)}
</table>

{uptime_html}
{perf_schema_html}
{consumers_html}
{instruments_html}
{pfs_warning_html}

</div></details>'''
    
    def _generate_non_default_vars_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> str:
        """
        Generate a custom Non-Default Configuration Variables section.
        
        This creates a formatted table showing variables that differ from their
        compiled defaults, with source type explanations.
        """
        section_id = self._make_section_id(section.name)
        
        # Build variables table
        var_rows = []
        truncation_msg = None
        if 'non_default_variables' in parsed_logs:
            log_data = parsed_logs['non_default_variables']
            if log_data.success and log_data.rows and log_data.columns:
                # Apply collection limit
                limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
                    log_data.rows, "Variables Changed from Default Values"
                )
                if was_truncated:
                    truncation_msg = self._limit_enforcer.get_truncation_message(total_count)
                for row in (limited_rows or []):
                    try:
                        name_idx = log_data.columns.index('VARIABLE_NAME')
                        value_idx = log_data.columns.index('VARIABLE_VALUE')
                        min_idx = log_data.columns.index('MIN_VALUE')
                        max_idx = log_data.columns.index('MAX_VALUE')
                        source_idx = log_data.columns.index('VARIABLE_SOURCE')
                        set_by_idx = log_data.columns.index('SET_BY')
                        set_time_idx = log_data.columns.index('SET_TIME')
                        path_idx = log_data.columns.index('CONFIG_PATH')
                        
                        var_name = row[name_idx]
                        var_value = row[value_idx]
                        min_val = row[min_idx] if row[min_idx] else '0'
                        max_val = row[max_idx] if row[max_idx] else '0'
                        source = row[source_idx]
                        set_by = row[set_by_idx] if row[set_by_idx] else 'N/A'
                        set_time = row[set_time_idx] if row[set_time_idx] else 'N/A'
                        config_path = row[path_idx] if row[path_idx] else ''
                        
                        # Truncate long values
                        display_value = str(var_value)
                        if len(display_value) > 80:
                            display_value = display_value[:77] + '...'
                        
                        var_rows.append(f'''<tr>
<td><strong>{self._escape_html(str(var_name))}</strong></td>
<td>{self._escape_html(display_value)}</td>
<td>{self._escape_html(str(min_val))}</td>
<td>{self._escape_html(str(max_val))}</td>
<td><strong>{self._escape_html(str(source))}</strong></td>
<td>{self._escape_html(str(set_by))}</td>
<td>{self._escape_html(str(set_time))}</td>
<td><small>{self._escape_html(str(config_path))}</small></td>
</tr>''')
                    except (ValueError, IndexError):
                        pass
        
        # Build the table HTML
        if var_rows:
            truncation_html = f'<p class="truncation-notice"><em>{truncation_msg}</em></p>' if truncation_msg else ''
            table_html = f'''
<h3>Variables Changed from Default Values</h3>
<p><em>Shows all variables that differ from their compiled defaults, including how they were set.</em></p>
{truncation_html}
<table class="data-table">
    <thead>
        <tr>
            <th>Variable Name</th>
            <th>Current Value</th>
            <th>Min Value</th>
            <th>Max Value</th>
            <th>Source</th>
            <th>Set By</th>
            <th>Set Time</th>
            <th>Config Path</th>
        </tr>
    </thead>
    <tbody>
        {chr(10).join(var_rows)}
    </tbody>
</table>

<p><em><strong>Note:</strong> MySQL does not store default values in Performance Schema. To see the compiled default for a variable, check the MySQL documentation or compare with a fresh installation. The Min/Max values show the valid range for numeric variables.</em></p>

<h4>Variable Source Types</h4>
<ul>
    <li><strong>COMPILED:</strong> Default value from MySQL compilation</li>
    <li><strong>PERSISTED:</strong> Set via SET PERSIST and stored in mysqld-auto.cnf</li>
    <li><strong>DYNAMIC:</strong> Changed at runtime via SET GLOBAL</li>
    <li><strong>EXPLICIT:</strong> Set in configuration file (my.cnf/my.ini)</li>
    <li><strong>COMMAND_LINE:</strong> Passed as command-line argument to mysqld</li>
    <li><strong>GLOBAL:</strong> Set via other global mechanisms</li>
</ul>'''
        else:
            table_html = '''
<h3>Variables Changed from Default Values</h3>
<div class="success">All variables are at their compiled default values.</div>'''
        
        # Generate Configuration Warnings subsection
        config_warnings = self._evaluate_config_warnings(parsed_logs)
        
        if config_warnings:
            warning_rows = []
            for param, message in config_warnings:
                warning_rows.append(f'''<tr>
<td><strong>{self._escape_html(param)}</strong></td>
<td>{self._escape_html(message)}</td>
</tr>''')
            
            config_warnings_html = f'''
<div class="subsection" id="{self._emit_subsection_anchor_id('Configuration Warnings')}">
<h3>Configuration Warnings</h3>
<p><em>Critical configuration parameters that may impact data integrity, performance, or security.</em></p>
<table class="data-table">
    <thead>
        <tr>
            <th>Parameter</th>
            <th>Message</th>
        </tr>
    </thead>
    <tbody>
        {chr(10).join(warning_rows)}
    </tbody>
</table>
</div>'''
        else:
            config_warnings_html = f'''
<div class="subsection" id="{self._emit_subsection_anchor_id('Configuration Warnings')}">
<h3>Configuration Warnings</h3>
<div class="success">No configuration warnings - all checked parameters are optimally configured</div>
</div>'''
        
        return f'''
<details id="{section_id}"><summary>1.5 Non-Default Configuration Variables</summary><div class="content">

<div class="info">Variables changed from their compiled defaults indicate intentional tuning or configuration. Understanding these changes helps troubleshoot issues, document customizations, and identify potential misconfigurations.</div>

{table_html}

{config_warnings_html}

</div></details>'''
    
    def _default_note_for_source(self, source_id: str, fallback: str) -> str:
        """
        Find the authored default_note for a query id in report_format.json.
        
        Section-level renderers have no subsection in scope, so they cannot read
        subsection.default_note directly. Looking the text up keeps one copy of it
        in report_format.json rather than duplicating it in the generator.
        
        Args:
            source_id: Query id whose subsection carries the note
            fallback: Text to use when no subsection declares a note
        
        Returns:
            The authored note, or the fallback
        """
        sections = getattr(self._report_format, 'sections', None) or []
        for section in sections:
            for subsection in getattr(section, 'subsections', None) or []:
                if getattr(subsection, 'source', None) == source_id:
                    if getattr(subsection, 'default_note', None):
                        return subsection.default_note
        return fallback
    
    def _generate_parallel_replication_section(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate the Parallel Replication (MTS) Details section.
        
        This section shows worker thread efficiency for multi-threaded replication.
        Only displayed on replica instances when replica_parallel_workers > 1.
        """
        # Helper to get value from parsed logs
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return log_data.rows[0][col_idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        # Check if this is a replica instance
        replication_role = get_value('replication_role', 'replication_role', 'STANDALONE')
        if replication_role != 'REPLICA':
            return ''
        
        # Check if parallel workers > 1
        parallel_workers = 0
        parallel_type = 'N/A'
        dep_tracking = 'N/A'
        
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    # Check replica_parallel_workers first (MySQL 8.0.26+), fallback to slave_parallel_workers
                    if 'replica_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    if parallel_workers == 0 and 'slave_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    
                    # Check replica_parallel_type first, fallback to slave_parallel_type
                    if 'replica_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    if parallel_type == 'N/A' and 'slave_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    
                    if 'binlog_transaction_dependency_tracking' in log_data.columns:
                        idx = log_data.columns.index('binlog_transaction_dependency_tracking')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            dep_tracking = str(val)
                except (ValueError, IndexError, TypeError):
                    pass
        
        # If parallel workers <= 1, don't show this section
        if parallel_workers <= 1:
            return ''
        
        # Check if we have worker stats data
        if 'parallel_replication_worker_stats' not in parsed_logs:
            return ''
        
        log_data = parsed_logs['parallel_replication_worker_stats']
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id('Parallel Replication (MTS) Details')}">
    <h3>Parallel Replication (MTS) Details</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Workers are configured but no per-worker rows came back. THREAD_ID is NULL
        # in replication_applier_status_by_worker while the applier is stopped, so
        # the join yields nothing. Emitting the note keeps "could not measure" apart
        # from "distribution is fine", which an empty section cannot express.
        #
        # This is a section-level renderer with no subsection in scope, so the
        # authored text is looked up by source id rather than duplicated here.
        if not log_data.rows or not log_data.columns:
            no_data_message = self._default_note_for_source(
                'parallel_replication_worker_stats', 'No data returned'
            )
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id('Parallel Replication (MTS) Details')}">
    <h3>Parallel Replication (MTS) Details</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build the section HTML
        html = '''
<h3>Parallel Replication (MTS) Details</h3>
<div class="info">The multi-threaded replication enables multiple threads to apply replication events in parallel. The configuration is at global level which applies to all replication channels.</div>

<h4>Replica Worker Thread Efficiency</h4>
<table class="data-table">
<thead><tr><th>Channel Name</th><th>Worker Thread ID</th><th>Transactions Executed</th><th>% of Total Transactions</th></tr></thead>
<tbody>'''
        
        try:
            channel_idx = log_data.columns.index('CHANNEL_NAME')
            thread_idx = log_data.columns.index('replica_worker_thread_id')
            trx_idx = log_data.columns.index('number_of_trx_executed')
            pct_idx = log_data.columns.index('percent_of_trx_executed_by_worker')
            
            # Track stats for interpretation
            worker_stats = []
            
            for row in log_data.rows:
                channel = row[channel_idx] if row[channel_idx] else '(default)'
                thread_id = row[thread_idx]
                trx_count = row[trx_idx]
                pct = row[pct_idx]
                
                worker_stats.append({
                    'channel': channel,
                    'thread_id': thread_id,
                    'trx_count': int(trx_count) if trx_count else 0,
                    'pct': float(pct) if pct else 0.0
                })
                
                html += f'<tr><td>{self._escape_html(str(channel))}</td>'
                html += f'<td>{self._escape_html(str(thread_id))}</td>'
                html += f'<td>{self._escape_html(str(trx_count))}</td>'
                html += f'<td>{self._escape_html(str(pct))}%</td></tr>'
            
            html += '</tbody></table>'
            
            # Generate interpretation if we have data
            if worker_stats:
                html += self._generate_mts_efficiency_interpretation(
                    worker_stats, parallel_workers, parallel_type, dep_tracking
                )
            
        except (ValueError, IndexError) as e:
            html += f'<tr><td colspan="4">Error parsing worker stats: {self._escape_html(str(e))}</td></tr>'
            html += '</tbody></table>'
        
        return html
    
    def _generate_mts_efficiency_interpretation(
        self,
        worker_stats: list,
        parallel_workers: int,
        parallel_type: str,
        dep_tracking: str
    ) -> str:
        """
        Generate interpretation of MTS worker efficiency with actionable recommendations.
        
        Analyzes the distribution of transactions across worker threads
        and provides specific action items based on configuration.
        """
        if not worker_stats:
            return ''
        
        # Group by channel
        channels = {}
        for stat in worker_stats:
            channel = stat['channel']
            if channel not in channels:
                channels[channel] = []
            channels[channel].append(stat)
        
        html = '<h4>Worker Efficiency Analysis</h4>'
        
        for channel, stats in channels.items():
            total_trx = sum(s['trx_count'] for s in stats)
            if total_trx == 0:
                html += f'<div class="info"><strong>Channel: {self._escape_html(channel)}</strong> - No transactions executed yet. Statistics will populate as replication processes transactions.</div>'
                continue
            
            # Calculate distribution metrics
            percentages = [s['pct'] for s in stats]
            max_pct = max(percentages)
            min_pct = min(percentages)
            avg_pct = sum(percentages) / len(percentages)
            spread = max_pct - min_pct
            
            # Count workers with significant work (>5% of transactions)
            active_workers = sum(1 for p in percentages if p > 5.0)
            
            html += f'<div class="subsection"><strong>Channel: {self._escape_html(channel)}</strong>'
            html += '<ul>'
            html += f'<li>Total transactions processed: {total_trx:,}</li>'
            html += f'<li>Active worker threads: {active_workers} of {len(stats)} configured</li>'
            html += f'<li>Transaction distribution spread: {spread:.1f}% (min: {min_pct:.1f}%, max: {max_pct:.1f}%, avg: {avg_pct:.1f}%)</li>'
            html += '</ul>'
            
            # Current configuration summary
            html += '<h5>Current Configuration</h5>'
            html += '<table class="data-table"><thead><tr><th>Parameter</th><th>Value</th><th>Status</th></tr></thead><tbody>'
            
            # Check parallel_type
            parallel_type_status = '✅ Optimal' if parallel_type == 'LOGICAL_CLOCK' else '⚠️ Suboptimal'
            html += f'<tr><td>replica_parallel_type</td><td>{self._escape_html(parallel_type)}</td><td>{parallel_type_status}</td></tr>'
            
            # Check dependency tracking (only for MySQL < 8.1)
            if dep_tracking != 'N/A' and self._database_version < (8, 1, 0):
                dep_status = '✅ Optimal' if dep_tracking in ('WRITESET', 'WRITESET_SESSION') else '⚠️ Suboptimal'
                html += f'<tr><td>binlog_transaction_dependency_tracking (source)</td><td>{self._escape_html(dep_tracking)}</td><td>{dep_status}</td></tr>'
            
            html += f'<tr><td>replica_parallel_workers</td><td>{parallel_workers}</td><td>✅ Enabled</td></tr>'
            html += '</tbody></table>'
            
            # Provide interpretation and action items based on distribution
            if spread < 20 and active_workers >= len(stats) * 0.8:
                html += '<div class="success"><strong>✅ Good Distribution:</strong> Transactions are well-distributed across worker threads. Multi-threaded replication is working efficiently.</div>'
            elif spread > 50 or active_workers < len(stats) * 0.5:
                html += '<div class="warning"><strong>⚠️ Uneven Distribution Detected</strong></div>'
                html += '<p>Transaction workload is concentrated on fewer workers than configured. This reduces the effectiveness of multi-threaded replication.</p>'
                
                html += '<h5>Recommended Actions</h5>'
                html += '<table class="data-table"><thead><tr><th>#</th><th>Action</th><th>Details</th></tr></thead><tbody>'
                
                action_num = 1
                
                # Check parallel_type
                if parallel_type != 'LOGICAL_CLOCK':
                    html += f'<tr><td>{action_num}</td><td><strong>Set replica_parallel_type to LOGICAL_CLOCK</strong></td>'
                    html += '<td>LOGICAL_CLOCK allows transactions from the same binary log group commit to execute in parallel. '
                    html += f'Current value: <code>{self._escape_html(parallel_type)}</code><br>'
                    html += '<code>SET GLOBAL replica_parallel_type = \'LOGICAL_CLOCK\';</code><br>'
                    html += '<em>Note: Requires stopping replication (STOP REPLICA) before changing.</em></td></tr>'
                    action_num += 1
                
                # Check dependency tracking (only for MySQL < 8.1)
                if dep_tracking != 'N/A' and self._database_version < (8, 1, 0) and dep_tracking not in ('WRITESET', 'WRITESET_SESSION'):
                    html += f'<tr><td>{action_num}</td><td><strong>Enable WRITESET dependency tracking on SOURCE server</strong></td>'
                    html += '<td>WRITESET tracking allows transactions modifying different rows to execute in parallel, even across group commits. '
                    html += f'Current value on this replica: <code>{self._escape_html(dep_tracking)}</code><br>'
                    html += '<strong>On the SOURCE server, run:</strong><br>'
                    html += '<code>SET GLOBAL binlog_transaction_dependency_tracking = \'WRITESET\';</code><br>'
                    html += '<code>SET GLOBAL transaction_write_set_extraction = \'XXHASH64\';</code><br>'
                    html += '<em>Note: This setting must be configured on the SOURCE, not the replica.</em></td></tr>'
                    action_num += 1
                
                # General recommendations
                html += f'<tr><td>{action_num}</td><td><strong>Review workload characteristics</strong></td>'
                html += '<td>Uneven distribution may indicate:<ul>'
                html += '<li>Many transactions modify the same rows/tables (hot spots)</li>'
                html += '<li>Large transactions that cannot be parallelized</li>'
                html += '<li>Sequential dependencies in the application logic</li>'
                html += '</ul>Consider reviewing application design to reduce contention on hot tables.</td></tr>'
                action_num += 1
                
                html += f'<tr><td>{action_num}</td><td><strong>Monitor after changes</strong></td>'
                html += '<td>After implementing changes:<ol>'
                html += '<li>Reset statistics: <code>TRUNCATE performance_schema.events_transactions_summary_by_thread_by_event_name;</code></li>'
                html += '<li>Allow workload to run for a representative period</li>'
                html += '<li>Re-run this collector to verify improvement</li>'
                html += '</ol></td></tr>'
                
                html += '</tbody></table>'
            else:
                html += '<div class="info"><strong>ℹ️ Moderate Distribution:</strong> Transaction distribution is acceptable but could be improved.</div>'
                
                # Still provide optimization tips if configuration is suboptimal
                if parallel_type != 'LOGICAL_CLOCK' or (dep_tracking != 'N/A' and self._database_version < (8, 1, 0) and dep_tracking not in ('WRITESET', 'WRITESET_SESSION')):
                    html += '<h5>Optimization Opportunities</h5><ul>'
                    if parallel_type != 'LOGICAL_CLOCK':
                        html += f'<li>Consider setting <code>replica_parallel_type = LOGICAL_CLOCK</code> (current: {self._escape_html(parallel_type)})</li>'
                    if dep_tracking != 'N/A' and self._database_version < (8, 1, 0) and dep_tracking not in ('WRITESET', 'WRITESET_SESSION'):
                        html += f'<li>Consider enabling <code>binlog_transaction_dependency_tracking = WRITESET</code> on the SOURCE server (current: {self._escape_html(dep_tracking)})</li>'
                    html += '</ul>'
            
            html += '</div>'
        
        return html
    
    def _generate_replication_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> str:
        """
        Generate a custom Replication Topology & Status section.
        
        This creates a comprehensive replication status report including:
        - Replication role detection (Source/Replica/Standalone)
        - Binary log configuration and status
        - Connected replicas (if source)
        - Key replication parameters with recommendations
        - Transaction dependency tracking analysis
        - Aurora MySQL replication (if applicable)
        """
        section_id = self._make_section_id(section.name)
        
        # Helper to get column value from parsed log
        def get_value(log_id: str, column: str, default=None):
            if log_id in parsed_logs:
                log_data = parsed_logs[log_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column)
                        return log_data.rows[0][col_idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        # Determine replication role
        replication_role = get_value('replication_role', 'replication_role', 'STANDALONE')
        log_bin_enabled = get_value('replication_role', 'log_bin_enabled', 0)
        
        # Build role display
        if replication_role == 'REPLICA':
            role_display = 'REPLICA (Read-only, replicating from source)'
            role_class = 'info'
        elif replication_role == 'SOURCE':
            role_display = 'SOURCE/STANDALONE (Can accept writes)'
            role_class = 'info'
        else:
            role_display = 'STANDALONE (No replication configured)'
            role_class = 'info'
        
        role_html = f'''
<h3>Replication Role</h3>
<div class="{role_class}">This server is configured as a <strong>{role_display}</strong></div>'''
        
        # Build Binary Log Replication section (2.1)
        binlog_section_html = '<h2>2.1 Binary Log Replication</h2>'
        binlog_section_html += '<div class="info">Standard MySQL replication using binary logs for data synchronization between source and replica servers.</div>'
        
        # Binary log status
        if log_bin_enabled in (1, '1', 'ON', True):
            binlog_status_html = '''
<h3>Binary Log Configuration</h3>
<p><em>Binary logs record all changes for replication and point-in-time recovery. Essential for data durability.</em></p>
<div class="success">Binary Logging: <strong>ENABLED</strong><br><small>Binary logs are enabled for replication and recovery.</small></div>'''
        else:
            binlog_status_html = '''
<h3>Binary Log Configuration</h3>
<p><em>Binary logs record all changes for replication and point-in-time recovery. Essential for data durability.</em></p>
<div class="warning">Binary Logging: <strong>DISABLED</strong><br><small>Binary logs are disabled. Replication and point-in-time recovery are not available.</small></div>'''
        
        # Connected replicas (if source)
        replicas_html = ''
        
        # Replication channel count metric (will be added to replicas section)
        channel_count = get_value('replication_channel_count', 'channel_count', 0)
        channel_metric_html = f'''
<div class="metric-card">
    <div class="metric-label">Replication Channels</div>
    <div class="metric-value">{channel_count}</div>
</div>'''
        
        if replication_role in ('SOURCE', 'PRIMARY'):
            replicas_html = '''
<h3>Replication Source Status</h3>
<p><em>Shows replicas currently connected to this server (if this is a replication source).</em></p>'''
            
            if 'connected_replicas' in parsed_logs:
                log_data = parsed_logs['connected_replicas']
                if log_data.success and log_data.rows:
                    replica_count = len(log_data.rows)
                    replicas_html += f'<div class="success"><strong>Connected Replicas:</strong> {replica_count} replica(s) currently connected</div>'
                    replicas_html += channel_metric_html  # Add channel count after connected replicas message
                    replicas_html += '<h4>Connected Replica Details</h4>'
                    replicas_html += '<table class="data-table"><thead><tr><th>Process ID</th><th>User</th><th>Host</th><th>State</th><th>Time (sec)</th></tr></thead><tbody>'
                    
                    for row in log_data.rows:
                        try:
                            pid_idx = log_data.columns.index('process_id')
                            user_idx = log_data.columns.index('USER')
                            host_idx = log_data.columns.index('HOST')
                            state_idx = log_data.columns.index('state')
                            time_idx = log_data.columns.index('time_sec')
                            
                            replicas_html += f'<tr><td>{self._escape_html(str(row[pid_idx]))}</td>'
                            replicas_html += f'<td>{self._escape_html(str(row[user_idx]))}</td>'
                            replicas_html += f'<td>{self._escape_html(str(row[host_idx]))}</td>'
                            replicas_html += f'<td>{self._escape_html(str(row[state_idx]))}</td>'
                            replicas_html += f'<td>{self._escape_html(str(row[time_idx]))}</td></tr>'
                        except (ValueError, IndexError):
                            pass
                    
                    replicas_html += '</tbody></table>'
                    replicas_html += '<p><em><strong>Note:</strong> Replica connections are identified by "Binlog Dump" processes. For detailed replica information including Server_Id and Replica_UUID, run: <code>SHOW REPLICAS;</code></em></p>'
                else:
                    replicas_html += '<div class="info">No replicas currently connected to this source.</div>'
                    replicas_html += channel_metric_html  # Add channel count even when no replicas
            else:
                replicas_html += '<div class="info">No replicas currently connected to this source.</div>'
                replicas_html += channel_metric_html  # Add channel count even when no data
        else:
            # For non-source servers, still show channel count
            replicas_html = channel_metric_html
        
        # Key Replication Parameters table
        params_html = f'''
<h3>Key Replication Parameters</h3>
<p><em>Server Role: <strong>{replication_role}</strong>. Showing {"source" if replication_role != "REPLICA" else "replica"} configuration.</em></p>'''
        
        # Get replication config values
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                row = log_data.rows[0]
                
                def get_config_val(col_name, default='N/A'):
                    try:
                        idx = log_data.columns.index(col_name)
                        val = row[idx]
                        return val if val is not None else default
                    except (ValueError, IndexError):
                        return default
                
                log_bin = get_config_val('log_bin')
                binlog_format = get_config_val('binlog_format')
                sync_binlog = get_config_val('sync_binlog')
                gtid_mode = get_config_val('gtid_mode')
                enforce_gtid = get_config_val('enforce_gtid_consistency')
                parallel_workers = get_config_val('replica_parallel_workers')
                parallel_type = get_config_val('replica_parallel_type')
                preserve_commit = get_config_val('replica_preserve_commit_order')
                dep_tracking = get_config_val('binlog_transaction_dependency_tracking')
                group_delay = get_config_val('binlog_group_commit_sync_delay')
                group_count = get_config_val('binlog_group_commit_sync_no_delay_count')
                writeset_extract = get_config_val('transaction_write_set_extraction')
                flush_log = get_config_val('innodb_flush_log_at_trx_commit')
                
                # Build parameters table with recommendations
                param_rows = [
                    ('log_bin', log_bin, 'ON for source; optional for replica'),
                    ('binlog_format', binlog_format, 'ROW recommended for consistency'),
                    ('sync_binlog', sync_binlog, '1 = safest; 0 = faster but risky'),
                    ('gtid_mode', gtid_mode, 'ON strongly recommended'),
                    ('enforce_gtid_consistency', enforce_gtid, 'Must be ON with GTID'),
                    ('replica_parallel_workers', parallel_workers, '4-16 for MTR; 0 = single-threaded'),
                    ('replica_parallel_type', parallel_type, 'LOGICAL_CLOCK for best parallelism'),
                    ('replica_preserve_commit_order', preserve_commit, 'ON for consistency with MTR'),
                    ('binlog_group_commit_sync_delay', f'{group_delay} μs' if group_delay != 'N/A' else 'N/A', 'Artificial delay for larger groups (0 = none)'),
                    ('binlog_group_commit_sync_no_delay_count', group_count, 'Commit after N trx (0 = no limit)'),
                ]
                
                # Add deprecated parameters only for MySQL < 8.1 (removed in 8.1+)
                if self._database_version < (8, 1, 0):
                    param_rows.insert(8, ('binlog_transaction_dependency_tracking', dep_tracking, 'WRITESET/WRITESET_SESSION for max parallelism'))
                    param_rows.append(('transaction_write_set_extraction', writeset_extract, 'XXHASH64 required for WRITESET'))
                
                params_html += '<table class="data-table"><thead><tr><th>Parameter</th><th>Value</th><th>Recommendation</th></tr></thead><tbody>'
                for param, value, rec in param_rows:
                    params_html += f'<tr><td>{self._escape_html(param)}</td><td>{self._escape_html(str(value))}</td><td>{self._escape_html(rec)}</td></tr>'
                params_html += '</tbody></table>'
                
                # Transaction Dependency Tracking section - only show for MySQL < 8.1
                # (binlog_transaction_dependency_tracking was removed in MySQL 8.1+)
                dep_tracking_html = ''
                if self._database_version < (8, 1, 0):
                    dep_tracking_html = '''
<h3>Transaction Dependency Tracking (Critical for Multi-Threaded Replication Performance!)</h3>
<div class="info"><strong>Understanding WRITESET:</strong> Transaction dependency tracking determines which transactions can run in parallel on replicas. This is CRITICAL for multi-threaded replication performance!</div>

<h4>Dependency Tracking Methods</h4>
<table class="data-table">
<thead><tr><th>Method</th><th>Description</th><th>Performance</th><th>Use Case</th></tr></thead>
<tbody>
<tr><td><strong>COMMIT_ORDER</strong></td><td>Uses binary log group commit. Transactions in same group commit can run in parallel.</td><td>Limited parallelism</td><td>Default, safe but slower</td></tr>
<tr><td><strong>WRITESET</strong></td><td>Tracks row hashes modified by each transaction. Transactions modifying different rows can run in parallel.</td><td>High parallelism</td><td>Recommended for most workloads</td></tr>
<tr><td><strong>WRITESET_SESSION</strong></td><td>Like WRITESET but preserves order within same session.</td><td>High parallelism</td><td>Best for applications needing session consistency</td></tr>
</tbody>
</table>

<h4>Current Configuration Analysis</h4>'''
                
                    # Add warning/success based on current setting
                    if dep_tracking == 'COMMIT_ORDER':
                        dep_tracking_html += f'''
<div class="warning"><strong>Dependency Tracking:</strong> {dep_tracking}<br><strong>[WARNING] Recommendation:</strong> Consider switching to WRITESET or WRITESET_SESSION for better replication parallelism. This can significantly reduce replication lag!<br><small>Set: binlog_transaction_dependency_tracking=WRITESET and transaction_write_set_extraction=XXHASH64</small></div>'''
                    elif dep_tracking in ('WRITESET', 'WRITESET_SESSION'):
                        dep_tracking_html += f'''
<div class="success"><strong>Dependency Tracking:</strong> {dep_tracking}<br>Good configuration for multi-threaded replication parallelism.</div>'''
                    else:
                        # N/A or unknown value - variable may not exist in this MySQL version
                        dep_tracking_html += f'''
<div class="info"><strong>Dependency Tracking:</strong> {dep_tracking}<br>The <code>binlog_transaction_dependency_tracking</code> variable is not available in this MySQL version.</div>'''
                
                    dep_tracking_html += '''
<p><em><strong>Key Insight:</strong> WRITESET tracking allows transactions that modify different rows to execute in parallel on replicas, even if they were in different group commits on the source. This dramatically improves replication throughput and reduces lag.</em></p>

<div class="warning"><strong>⚠️ Important:</strong> The <code>binlog_transaction_dependency_tracking</code> setting must be configured on the <strong>SOURCE/PRIMARY server</strong>, not on replicas. Replicas read the dependency information from the binary logs generated by the source. Setting WRITESET on the source enables better parallelism on ALL downstream replicas.</div>'''
                
                # Binary Logging Configuration Advice
                logging_advice_html = '''
<h3>Binary Logging Configuration Advice</h3>
<div class="info"><strong>Binary Logging on Replicas:</strong> Binary logs are NOT required on replicas unless the replica is also a source for another replica [chained replication] (or) binary logs data used for data streaming services (or) you need point-in-time recovery from the replica.</div>

<h4>Current Server Role and Logging Recommendation</h4>'''
                
                role_text = 'Source/Standalone' if replication_role != 'REPLICA' else 'Replica'
                binlog_text = 'Enabled' if log_bin in (1, '1', 'ON', True) else 'Disabled'
                binlog_rec = 'Binary logs should be enabled on source servers for replication and PITR.' if replication_role != 'REPLICA' else 'Binary logs optional on leaf replicas unless chaining.'
                
                logging_advice_html += f'''
<table class="data-table">
<thead><tr><th>Aspect</th><th>Status</th><th>Recommendation</th></tr></thead>
<tbody>
<tr><td>Server Role</td><td>{role_text}</td><td>This is a {role_text.lower()} server</td></tr>
<tr><td>Binary Logging</td><td>{binlog_text}</td><td>{binlog_rec}</td></tr>
<tr><td>sync_binlog</td><td>{sync_binlog}</td><td>{'Safest setting (ACID compliant). Every commit syncs binlog to disk.' if str(sync_binlog) == '1' else 'Consider sync_binlog=1 for durability.'}</td></tr>
<tr><td>innodb_flush_log_at_trx_commit</td><td>{flush_log}</td><td>{'Safest setting (ACID compliant). Every commit syncs redo log to disk.' if str(flush_log) == '1' else 'Consider innodb_flush_log_at_trx_commit=1 for durability.'}</td></tr>
</tbody>
</table>

<h4>Logging Best Practices</h4>
<ul>
<li><strong>Source Servers:</strong> Enable binary logging (log_bin=ON) with sync_binlog=1 for durability</li>
<li><strong>Leaf Replicas:</strong> Binary logging optional. Disable to save I/O unless needed for chaining or PITR</li>
<li><strong>Intermediate Replicas:</strong> Enable binary logging if this replica is a source for other replicas</li>
<li><strong>ACID Compliance:</strong> Use sync_binlog=1 AND innodb_flush_log_at_trx_commit=1 for full durability</li>
<li><strong>Performance vs Durability:</strong> Lower sync values improve performance but risk data loss on crash</li>
</ul>
'''
                
                # Check if general_log is enabled and show warning only if ON
                general_log_warning = ''
                try:
                    general_log_value = get_value('logging_status', 'general_log', 0)
                    if general_log_value in (1, '1', 'ON', True):
                        general_log_warning = '''
<div class="warning"><strong>⚠️ Performance Warning:</strong> The general query log (<code>general_log=ON</code>) logs ALL queries and can severely impact performance on busy systems. Use only for short-term debugging. The slow query log (<code>slow_query_log=ON</code>) has less impact but should still be used judiciously with appropriate <code>long_query_time</code> settings.</div>'''
                except Exception:
                    # If logging_status query failed or doesn't exist, don't show the warning
                    pass
                
                logging_advice_html += general_log_warning
            else:
                dep_tracking_html = ''
                logging_advice_html = ''
        else:
            dep_tracking_html = ''
            logging_advice_html = ''
        
        # Parallel Replication (MTS) Details section - only for MySQL and Aurora MySQL when parallel workers > 1
        parallel_replication_html = ''
        if engine_type in ('mysql', 'aurora-mysql'):
            parallel_replication_html = self._generate_parallel_replication_section(parsed_logs)
        
        # Aurora MySQL Replication section (2.2)
        aurora_html = '<h2>2.2 Aurora MySQL Replication</h2>'
        if engine_type == 'aurora-mysql':
            aurora_html += '<div class="info">Aurora MySQL uses a shared storage architecture with up to 15 read replicas.</div>'
            
            if 'aurora_global_db_status' in parsed_logs:
                log_data = parsed_logs['aurora_global_db_status']
                if log_data.success and log_data.rows:
                    aurora_html += '<h3>Aurora Replica Host Status</h3>'
                    aurora_html += '<table class="data-table"><thead><tr><th>Server ID</th><th>Session ID</th><th>Last Update</th><th>Replica Lag (ms)</th><th>Oldest Read View TRX ID</th><th>Oldest Read View LSN</th></tr></thead><tbody>'
                    
                    for row in log_data.rows:
                        try:
                            server_idx = log_data.columns.index('SERVER_ID')
                            session_idx = log_data.columns.index('SESSION_ID')
                            update_idx = log_data.columns.index('LAST_UPDATE_TIMESTAMP')
                            lag_idx = log_data.columns.index('REPLICA_LAG_IN_MILLISECONDS')
                            trx_idx = log_data.columns.index('OLDEST_READ_VIEW_TRX_ID') if 'OLDEST_READ_VIEW_TRX_ID' in log_data.columns else -1
                            lsn_idx = log_data.columns.index('OLDEST_READ_VIEW_LSN') if 'OLDEST_READ_VIEW_LSN' in log_data.columns else -1
                            
                            aurora_html += f'<tr><td>{self._escape_html(str(row[server_idx]))}</td>'
                            aurora_html += f'<td>{self._escape_html(str(row[session_idx]))}</td>'
                            aurora_html += f'<td>{self._escape_html(str(row[update_idx]))}</td>'
                            aurora_html += f'<td>{self._escape_html(str(row[lag_idx]))}</td>'
                            aurora_html += f'<td>{self._escape_html(str(row[trx_idx])) if trx_idx >= 0 else "N/A"}</td>'
                            aurora_html += f'<td>{self._escape_html(str(row[lsn_idx])) if lsn_idx >= 0 else "N/A"}</td></tr>'
                        except (ValueError, IndexError):
                            pass
                    
                    aurora_html += '</tbody></table>'
                else:
                    aurora_html += '<div class="info">Aurora Replica Host Status not available.</div>'
            else:
                aurora_html += '<div class="info">Aurora Replica Host Status not configured.</div>'
            
            # Aurora In-Memory Relay Log Status subsection
            aurora_html += self._generate_aurora_replication_subsection(
                'aurora_relay_log_status',
                'Aurora In-Memory Relay Log Status',
                'Aurora MySQL in-memory relay log status. In-memory relay log improves replica performance by avoiding disk I/O for relay log operations.',
                'Aurora in-memory relay log status not available',
                parsed_logs
            )
            
            # Aurora Replication Throughput subsection
            aurora_html += self._generate_aurora_replication_subsection(
                'aurora_repl_throughput',
                'Aurora Replication Throughput',
                'Aurora replication throughput monitoring. Aurora_repl_bytes_received tracks total bytes received from the replication source.',
                'Aurora replication throughput metrics not available',
                parsed_logs
            )
        else:
            aurora_html += '<div class="warning"><strong>Not Aurora MySQL:</strong> Aurora replication features not available. This is a standard MySQL server.</div>'
        
        # Assemble the complete section
        return f'''
<details id="{section_id}"><summary>2. Replication Topology & Status</summary><div class="content">

<div class="info">Replication is critical for high availability, read scaling, and disaster recovery. Monitoring replication lag, errors, and configuration helps prevent data loss and ensures replicas stay synchronized with the source.</div>

{role_html}
{binlog_section_html}
{binlog_status_html}
{replicas_html}
{params_html}
{dep_tracking_html}
{logging_advice_html}
{parallel_replication_html}
{aurora_html}

</div></details>'''
    
    def _generate_aurora_replication_subsection(
        self,
        source_id: str,
        title: str,
        note: str,
        default_note: str,
        parsed_logs: Dict[str, 'ParsedLogFile']
    ) -> str:
        """Generate HTML for an Aurora replication subsection with a data table."""
        # Check if data exists
        if source_id not in parsed_logs:
            return f'''
<h3>{self._escape_html(title)}</h3>
<p><em>{self._escape_html(note)}</em></p>
<div class="info">{self._escape_html(default_note)}</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<h3>{self._escape_html(title)}</h3>
<p><em>{self._escape_html(note)}</em></p>
<div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>'''
        
        # Handle no data case
        if not log_data.rows:
            return f'''
<h3>{self._escape_html(title)}</h3>
<p><em>{self._escape_html(note)}</em></p>
<div class="info">{self._escape_html(default_note)}</div>'''
        
        # Build the data table
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in log_data.columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        body_rows = []
        for row in log_data.rows:
            cells = [f'<td>{self._escape_html(str(cell) if cell is not None else "")}</td>' for cell in row]
            body_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {"".join(body_rows)}
    </tbody>
</table>'''
        
        # Add dynamic recommendations for aurora_relay_log_status
        recommendations_html = ''
        if source_id == 'aurora_relay_log_status':
            recommendations_html = self._get_aurora_relay_log_recommendations(log_data)
        
        return f'''
<h3>{self._escape_html(title)}</h3>
<p><em>{self._escape_html(note)}</em></p>
{table_html}
{recommendations_html}'''
    
    def _get_aurora_relay_log_recommendations(self, log_data: 'ParsedLogFile') -> str:
        """Generate dynamic recommendations for Aurora relay log status."""
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        recommendations = []
        
        # Check Aurora_in_memory_relaylog_status
        relay_status = values.get('Aurora_in_memory_relaylog_status', '')
        disabled_reason = values.get('Aurora_in_memory_relaylog_disabled_reason', '')
        if relay_status.upper() == 'DISABLED':
            reason_text = f' Reason: {disabled_reason}.' if disabled_reason else ''
            recommendations.append(f'⚠️ <strong>In-memory relay log disabled:</strong>{reason_text} This may impact replica performance.')
        
        # Check Aurora_in_memory_relaylog_fallback_count
        fallback_count = values.get('Aurora_in_memory_relaylog_fallback_count', '0')
        try:
            fallback_int = int(fallback_count)
            if fallback_int > 0:
                recommendations.append(f'ℹ️ <strong>Relay log fallbacks:</strong> {fallback_int} times. Large transactions caused fallback to persistent storage.')
        except (ValueError, TypeError):
            pass
        
        if not recommendations:
            return ''
        
        return f'''
<div class="warning" style="margin-top: 10px;">
    <strong>Analysis:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        {"".join(f'<li>{rec}</li>' for rec in recommendations)}
    </ul>
</div>'''
    
    def _generate_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str],
        section_number: int = 0,
        is_first: bool = False
    ) -> str:
        """
        Generate HTML for a report section using collapsible details/summary.
        
        Behavior:
        - Organize report by sections defined in report_format.json
        - Include Section_Overview at the start of each section
        - Use consistent color scheme for section headings
        """
        section_id = self._make_section_id(section.name)
        
        # Generate subsections
        subsections_html = []
        has_data = False
        
        for subsection in section.subsections:
            # Check condition (engine and version filtering)
            if not self._check_condition(
                subsection.condition, 
                engine_type,
                subsection.min_engine_version,
                subsection.max_engine_version
            ):
                continue
            
            subsection_html = self._generate_subsection(subsection, parsed_logs)
            if subsection_html:
                subsections_html.append(subsection_html)
                has_data = True
        
        # Skip section if no subsections have data
        if not has_data:
            return ''
        
        # Generate section overview
        overview_text = self._get_section_overview(section.name)
        
        # Add engine-specific notes for certain sections
        engine_note_html = ''
        if section.name == 'I/O Analysis' and engine_type == 'aurora-mysql':
            engine_note_html = '''
<div class="info" style="margin-top: 10px;"><strong>Note:</strong> Aurora MySQL does not instrument physical I/O (file-level bytes) the same way as Community MySQL due to its distributed storage architecture. However, logical I/O (table-level row operations) is fully instrumented and available.</div>'''
        
        # Determine section title with number
        section_title = section.name
        if section_number > 0:
            # Section names in report_format.json match display titles
            section_title = f"{section_number}. {section.name}"
        
        # Add 'open' attribute to first section
        open_attr = ' open' if is_first else ''
        
        return f'''
<details id="{section_id}"{open_attr}><summary>{self._escape_html(section_title)}</summary><div class="content">

<div class="info">{self._escape_html(overview_text)}</div>{engine_note_html}

{chr(10).join(subsections_html)}

</div></details>'''
    
    def _is_transaction_consumer_enabled(self, parsed_logs: Dict[str, 'ParsedLogFile']) -> bool:
        """Check if the events_transactions_current consumer is enabled in performance_schema."""
        consumer_status = parsed_logs.get('events_transactions_consumer_status')
        if consumer_status and consumer_status.success and consumer_status.rows:
            # Check if ENABLED column value is 'YES'
            if consumer_status.columns:
                try:
                    enabled_idx = consumer_status.columns.index('ENABLED')
                    return consumer_status.rows[0][enabled_idx].upper() == 'YES'
                except (ValueError, IndexError):
                    pass
        return False
    
    def _generate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for a report subsection with data table."""
        source_id = subsection.source
        
        # Check if subsection requires extended checks and they are disabled
        if subsection.requires_extended_checks and not self._extended_checks:
            # 3-tier fallback logic:
            # 1. If --extended-checks enabled -> Use innodb_trx query (handled above, we're in the disabled case)
            # 2. If fallback_source exists and transaction consumer is enabled -> Use PS query
            # 3. Else -> Display message IN PLACE OF result set
            
            if subsection.fallback_source and self._is_transaction_consumer_enabled(parsed_logs):
                # Use fallback source (performance_schema query)
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success:
                    # Use fallback columns if specified, otherwise use original columns
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    # Create a temporary subsection with fallback source
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        requires_extended_checks=False  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
            
            # No fallback available or consumer not enabled - show disabled message
            disabled_note = subsection.extended_checks_disabled_note or "Extended checks disabled. Use --extended-checks to enable."
            anchor_id = self._emit_subsection_anchor_id(subsection.name)
            # Add note above the disabled message if provided
            note_html = ''
            if subsection.note:
                note_html = f'''
<div class="info">{subsection.note}</div>'''
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="warning">{self._escape_html(disabled_note)}</div>
</div>'''
        
        # Special handling for Key Performance Counters subsection
        if source_id == 'key_performance_counters':
            return self._generate_key_performance_counters_subsection(subsection, parsed_logs)
        
        # Special handling for Transaction Summary subsection
        if source_id == 'transaction_summary':
            return self._generate_transaction_summary_subsection(subsection, parsed_logs)
        
        # Special handling for Table Cache Statistics subsection
        if source_id == 'table_cache_stats':
            return self._generate_table_cache_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Temporary Table Usage subsection
        if source_id == 'temp_table_usage':
            return self._generate_temp_table_usage_subsection(subsection, parsed_logs)
        
        # Special handling for Memory Troubleshooting Tips subsection
        if source_id == 'memory_troubleshooting_tips':
            return self._generate_memory_troubleshooting_tips_subsection(subsection)
        
        # Special handling for Aurora InnoDB Note subsection
        if source_id == 'aurora_innodb_note':
            return self._generate_aurora_innodb_note_subsection(subsection)
        
        # Special handling for InnoDB Purge Analysis subsection
        if source_id == 'innodb_purge_analysis':
            return self._generate_innodb_purge_analysis_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Engine Status Analysis subsection
        if source_id == 'innodb_engine_status_analysis':
            return self._generate_innodb_engine_status_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Analysis subsection
        if source_id == 'long_running_transactions_analysis':
            return self._generate_long_running_transactions_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Activity subsection
        if source_id == 'long_running_trx_recent_queries':
            return self._generate_long_running_trx_activity_subsection(subsection, parsed_logs)
        
        # Special handling for Tables Without Primary Key subsection
        if source_id == 'tables_without_pk':
            return self._generate_tables_without_pk_subsection(subsection, parsed_logs)
        
        # Special handling for Unused Indexes subsection
        if source_id == 'unused_indexes':
            return self._generate_unused_indexes_subsection(subsection, parsed_logs)
        
        # Special handling for Locked Accounts subsection
        if source_id == 'locked_accounts':
            return self._generate_locked_accounts_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Configuration subsection
        if source_id == 'ssl_config':
            return self._generate_ssl_config_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Connection Status subsection
        if source_id == 'ssl_connection_status':
            return self._generate_ssl_connection_status_subsection(subsection, parsed_logs)
        
        # Special handling for Connection Memory Overhead subsection
        if source_id == 'connection_memory_overhead':
            return self._generate_connection_memory_overhead_subsection(subsection, parsed_logs)
        
        # Special handling for Sort & Join Buffer Memory subsection
        if source_id == 'sort_join_buffer_memory':
            return self._generate_sort_join_buffer_memory_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Buffer Pool Statistics (Detailed) subsection
        if source_id == 'innodb_buffer_pool_stats_detailed':
            return self._generate_innodb_buffer_pool_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Per-Connection Memory Buffers subsection
        if source_id == 'per_connection_memory':
            return self._generate_per_connection_memory_subsection(subsection, parsed_logs)
        
        # Special handling for Global Memory Usage - Top Components subsection
        if source_id == 'global_memory_usage_top':
            return self._generate_global_memory_usage_top_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Row Operations subsection (metric cards)
        if source_id == 'innodb_row_operations':
            return self._generate_innodb_row_operations_subsection(subsection, parsed_logs)
        
        # Special handling for Buffer Pool Hit Rate subsection (metric cards)
        if source_id == 'health_buffer_pool_hit_rate':
            return self._generate_buffer_pool_hit_rate_subsection(subsection, parsed_logs)
        
        # Special handling for All Current Transactions Summary subsection (metric cards)
        if source_id == 'current_transactions_summary':
            return self._generate_current_transactions_summary_subsection(subsection, parsed_logs)
        
        # Special handling for History List Length subsection (metric cards)
        if source_id == 'innodb_history_length':
            return self._generate_history_list_length_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Memory Status subsection (dynamic recommendations)
        if source_id == 'aurora_memory_status':
            return self._generate_aurora_memory_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Parallel Query Status subsection (dynamic recommendations)
        if source_id == 'aurora_pq_status':
            return self._generate_aurora_pq_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Relay Log Status subsection (dynamic recommendations)
        if source_id == 'aurora_relay_log_status':
            return self._generate_aurora_relay_log_status_subsection(subsection, parsed_logs)
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            # Primary source not available - check for fallback
            if subsection.fallback_source:
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success and fallback_log.rows:
                    # Use fallback source
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        fallback_source=None  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
                elif subsection.fallback_requires_extended_checks and not self._extended_checks:
                    # Fallback requires extended checks which are disabled
                    disabled_note = subsection.extended_checks_disabled_note or "Enable --extended-checks to view this data."
                    anchor_id = self._emit_subsection_anchor_id(subsection.name)
                    note_html = ''
                    if subsection.note:
                        note_html = f'''
    <div class="info">{subsection.note}</div>'''
                    return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="warning">{self._escape_html(disabled_note)}</div>
</div>'''
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No data available</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case - use default_note if provided
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No matching columns found</div>
</div>'''
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Generate table
        table_html = self._generate_table(
            display_columns,
            limited_rows,
            column_map,
            log_data.columns
        )
        
        # Add truncation notice if needed
        truncation_html = ''
        if was_truncated:
            truncation_html = f'''
<div class="truncation-notice">
    {self._limit_enforcer.get_truncation_message(total_count)}
</div>'''
        
        # Add note above the table if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
<div class="info">{subsection.note}</div>'''
        
        # Add warning for Unused Indexes subsection when it has data
        warning_html = ''
        if subsection.name == "Unused Indexes" and limited_rows:
            warning_html = '''
<div class="warning">
<strong>⚠️ Important - Safe Index Removal Process:</strong><br>
DO NOT drop indexes immediately! Indexes may be used infrequently (daily/weekly/monthly reports, batch jobs, etc.).<br><br>
<strong>Recommended approach (MySQL 8.0+):</strong><br>
1. Make the index INVISIBLE first: <code>ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE;</code><br>
2. Monitor for several weeks/months to ensure no queries break<br>
3. If no issues, drop the index: <code>ALTER TABLE schema.table DROP INDEX index_name;</code><br>
4. If queries break, make visible again: <code>ALTER TABLE schema.table ALTER INDEX index_name VISIBLE;</code><br><br>
<strong>Why:</strong> Invisible indexes are maintained by MySQL but not used by the optimizer. This allows safe testing without the risk of losing the index structure. Available in MySQL 8.0+ and Aurora MySQL 2.x+.
</div>'''
        # Add warning from subsection config if provided
        elif subsection.warning and limited_rows:
            warning_html = f'''
<div class="warning">{self._escape_html(subsection.warning)}</div>'''

        # Add result_note after the table when there are results
        result_note_html = ''
        if subsection.result_note and limited_rows:
            result_note_html = f'''
<div class="info"><em>{self._escape_html(subsection.result_note)}</em></div>'''
        
        # Generate anchor ID for the subsection
        anchor_id = self._emit_subsection_anchor_id(subsection.name)
        
        return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
    {truncation_html}
    {result_note_html}
    {warning_html}
</div>'''
    
    def _generate_table(
        self,
        display_columns: List[Tuple[str, Optional[str]]],
        rows: Optional[List[Tuple[Any, ...]]],
        column_map: Dict[str, int],
        source_columns: List[str]
    ) -> str:
        """
        Generate an HTML table for the data.
        
        Args:
            display_columns: List of (display_name, source_column_or_expr) tuples
            rows: Data rows
            column_map: Mapping of column names to indices
            source_columns: Original column names from the query
        """
        if not rows:
            return '<div class="no-data">No data</div>'
        
        # Generate header
        header_cells = [
            f'<th>{self._escape_html(col[0])}</th>'
            for col in display_columns
        ]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        # Generate data rows
        data_rows = []
        for row in rows:
            # Convert row tuple to dict for expression evaluation
            # Guard against rows with fewer columns than expected
            row_len = len(row) if row else 0
            row_dict = {col: row[idx] for col, idx in column_map.items() if idx < row_len}
            
            cells = []
            for display_name, source in display_columns:
                value = self._get_cell_value(source, row, column_map, row_dict)
                cells.append(f'<td>{self._format_cell_value(value)}</td>')
            
            data_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        return f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {chr(10).join(data_rows)}
    </tbody>
</table>'''
    
    def _generate_key_performance_counters_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Key Performance Counters subsection with metric cards."""
        # Get max_connections for the Threads Connected display
        max_connections = 0
        if 'connection_stats' in parsed_logs:
            conn_data = parsed_logs['connection_stats']
            if conn_data.success and conn_data.rows:
                for row in conn_data.rows:
                    if len(row) >= 2 and str(row[0]).lower() == 'max_connections':
                        try:
                            max_connections = int(row[1])
                        except (ValueError, TypeError):
                            pass
        
        # Transform the data using the module-level function
        transformed_rows = evaluate_key_performance_counters(parsed_logs, max_connections)
        
        if not transformed_rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Extract values from the transformed row
        row = transformed_rows[0]
        qps = row[0]
        total_queries = f"{row[1]:,}"
        slow_queries = f"{row[2]:,}"
        threads_connected = row[3]  # Already formatted as "X / Y" or just "X"
        threads_running = row[4]
        aborted_connections = f"{row[5]:,}"
        aborted_clients = f"{row[6]:,}"
        
        # Generate metric cards HTML
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{qps}</div>
            <div class="metric-label">Queries Per Second</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{total_queries}</div>
            <div class="metric-label">Total Queries</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{slow_queries}</div>
            <div class="metric-label">Slow Queries</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{threads_connected}</div>
            <div class="metric-label">Threads Connected</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{threads_running}</div>
            <div class="metric-label">Threads Running</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{aborted_connections}</div>
            <div class="metric-label">Aborted Connections</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{aborted_clients}</div>
            <div class="metric-label">Aborted Clients</div>
        </div>
    </div>
</div>'''
    
    def _generate_transaction_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Transaction Summary subsection with metric cards."""
        source_id = subsection.source
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Extract values from the row
        # Columns: total_transactions, oldest_trx_age_seconds, total_rows_locked, total_modified_rows
        row = parsed_log.rows[0]
        total_transactions = row[0] if row[0] is not None else 0
        oldest_trx_age = row[1] if row[1] is not None else 0
        total_rows_locked = row[2] if row[2] is not None else 0
        total_modified_rows = row[3] if row[3] is not None else 0
        
        # Format values
        total_transactions_str = f"{total_transactions:,}"
        oldest_trx_age_str = f"{oldest_trx_age:,}" if oldest_trx_age else "0"
        total_rows_locked_str = f"{total_rows_locked:,}"
        total_modified_rows_str = f"{total_modified_rows:,}"
        
        # Generate metric cards HTML
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{total_transactions_str}</div>
            <div class="metric-label">Total Transactions</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{oldest_trx_age_str}</div>
            <div class="metric-label">Oldest Trx Age (seconds)</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{total_rows_locked_str}</div>
            <div class="metric-label">Total Rows Locked</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{total_modified_rows_str}</div>
            <div class="metric-label">Total Modified Rows</div>
        </div>
    </div>
</div>'''
    
    def _generate_innodb_row_operations_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for InnoDB Row Operations subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Extract values - columns: Rows Read, Rows Inserted, Rows Updated, Rows Deleted
        row = parsed_log.rows[0]
        rows_read = row[0] if row[0] is not None else 0
        rows_inserted = row[1] if row[1] is not None else 0
        rows_updated = row[2] if row[2] is not None else 0
        rows_deleted = row[3] if row[3] is not None else 0
        
        # Format values
        rows_read_str = f"{rows_read:,}"
        rows_inserted_str = f"{rows_inserted:,}"
        rows_updated_str = f"{rows_updated:,}"
        rows_deleted_str = f"{rows_deleted:,}"
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>{note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{rows_read_str}</div>
            <div class="metric-label">Rows Read</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{rows_inserted_str}</div>
            <div class="metric-label">Rows Inserted</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{rows_updated_str}</div>
            <div class="metric-label">Rows Updated</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{rows_deleted_str}</div>
            <div class="metric-label">Rows Deleted</div>
        </div>
    </div>
</div>'''
    
    def _generate_buffer_pool_hit_rate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Buffer Pool Hit Rate subsection with metric cards."""
        source_id = subsection.source
        anchor_id = self._emit_subsection_anchor_id(subsection.name)
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Extract value - column: buffer_pool_hit_rate
        row = parsed_log.rows[0]
        hit_rate = row[0] if row[0] is not None else "N/A"
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>{note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(str(hit_rate))}</div>
            <div class="metric-label">Buffer Pool Hit Rate</div>
        </div>
    </div>
</div>'''
    
    def _generate_current_transactions_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for All Current Transactions Summary subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Extract values - columns: Total Active Transactions, Oldest Transaction Age (seconds)
        row = parsed_log.rows[0]
        total_active = row[0] if row[0] is not None else 0
        oldest_age = row[1] if row[1] is not None else 0
        
        # Format values
        total_active_str = f"{total_active:,}"
        oldest_age_str = f"{oldest_age:,}"
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>{note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{total_active_str}</div>
            <div class="metric-label">Total Active Transactions</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{oldest_age_str}</div>
            <div class="metric-label">Oldest Transaction Age (seconds)</div>
        </div>
    </div>
</div>'''
    
    def _generate_history_list_length_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for History List Length subsection with metric cards."""
        source_id = subsection.source
        anchor_id = self._emit_subsection_anchor_id(subsection.name)
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Extract value - column: history_list_length
        row = parsed_log.rows[0]
        history_length = row[0] if row[0] is not None else 0
        
        # Format value
        history_length_str = f"{history_length:,}"
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>{note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{history_length_str}</div>
            <div class="metric-label">History List Length</div>
        </div>
    </div>
</div>'''
    
    def _generate_table_cache_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Table Cache Statistics subsection with transformed data."""
        # Transform the data using the module-level function
        transformed_rows = evaluate_table_cache_stats(parsed_logs)
        
        if not transformed_rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Define the columns for display
        columns = ["Metric", "Value", "Explanation"]
        
        # Generate header
        header_cells = [f'<th>{self._escape_html(col)}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        # Generate data rows
        data_rows = []
        for row in transformed_rows:
            cells = [f'<td>{self._format_cell_value(val)}</td>' for val in row]
            data_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {chr(10).join(data_rows)}
    </tbody>
</table>'''
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
</div>'''
    
    def _generate_temp_table_usage_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Temporary Table Usage subsection with metric cards."""
        # Transform the data using the module-level function
        temp_data = evaluate_temp_table_usage(parsed_logs)
        anchor_id = self._emit_subsection_anchor_id(subsection.name)
        
        if not temp_data:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Format the values
        created_tmp_tables = f"{temp_data['created_tmp_tables']:,}"
        created_tmp_disk_tables = f"{temp_data['created_tmp_disk_tables']:,}"
        disk_tmp_table_pct = f"{temp_data['disk_tmp_table_pct']:.2f}%"
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        # Generate metric cards HTML
        return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{created_tmp_tables}</div>
            <div class="metric-label">Created Tmp Tables</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{created_tmp_disk_tables}</div>
            <div class="metric-label">Created Tmp Disk Tables</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{disk_tmp_table_pct}</div>
            <div class="metric-label">Disk Tmp Table %</div>
        </div>
    </div>
</div>'''
    
    def _generate_connection_memory_overhead_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Connection Memory Overhead subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        current_conn = str(row[col_map.get('Current Connections', 0)]) if 'Current Connections' in col_map else 'N/A'
        max_conn = str(row[col_map.get('Max Connections', 1)]) if 'Max Connections' in col_map else 'N/A'
        avg_mem = str(row[col_map.get('Avg Memory per Connection', 2)]) if 'Avg Memory per Connection' in col_map else 'N/A'
        total_mem = str(row[col_map.get('Total Connection Memory', 3)]) if 'Total Connection Memory' in col_map else 'N/A'
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(current_conn)}</div>
            <div class="metric-label">Current Connections</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(max_conn)}</div>
            <div class="metric-label">Max Connections</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(avg_mem)}</div>
            <div class="metric-label">Avg Memory per Connection</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(total_mem)}</div>
            <div class="metric-label">Total Connection Memory</div>
        </div>
    </div>
</div>'''
    
    def _generate_sort_join_buffer_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Sort & Join Buffer Memory subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        sort_buffer = str(row[col_map.get('sort_buffer_size', 0)]) if 'sort_buffer_size' in col_map else 'N/A'
        join_buffer = str(row[col_map.get('join_buffer_size', 1)]) if 'join_buffer_size' in col_map else 'N/A'
        read_buffer = str(row[col_map.get('read_buffer_size', 2)]) if 'read_buffer_size' in col_map else 'N/A'
        read_rnd_buffer = str(row[col_map.get('read_rnd_buffer_size', 3)]) if 'read_rnd_buffer_size' in col_map else 'N/A'
        sort_merge = str(row[col_map.get('Sort Merge Passes', 4)]) if 'Sort Merge Passes' in col_map else 'N/A'
        sort_range = str(row[col_map.get('Sort Range', 5)]) if 'Sort Range' in col_map else 'N/A'
        sort_scan = str(row[col_map.get('Sort Scan', 6)]) if 'Sort Scan' in col_map else 'N/A'
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(sort_buffer)}</div>
            <div class="metric-label">sort_buffer_size</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(join_buffer)}</div>
            <div class="metric-label">join_buffer_size</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(read_buffer)}</div>
            <div class="metric-label">read_buffer_size</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(read_rnd_buffer)}</div>
            <div class="metric-label">read_rnd_buffer_size</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(sort_merge)}</div>
            <div class="metric-label">Sort Merge Passes</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(sort_range)}</div>
            <div class="metric-label">Sort Range</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(sort_scan)}</div>
            <div class="metric-label">Sort Scan</div>
        </div>
    </div>
</div>'''
    
    def _generate_innodb_buffer_pool_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for InnoDB Buffer Pool Statistics (Detailed) subsection with metric cards and per-instance table."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        pool_size = str(row[col_map.get('Buffer Pool Size (GB)', 0)]) if 'Buffer Pool Size (GB)' in col_map else 'N/A'
        pool_instances = str(row[col_map.get('Buffer Pool Instances', 1)]) if 'Buffer Pool Instances' in col_map else 'N/A'
        total_pages = str(row[col_map.get('Total Pages', 2)]) if 'Total Pages' in col_map else 'N/A'
        data_pages = str(row[col_map.get('Data Pages', 3)]) if 'Data Pages' in col_map else 'N/A'
        dirty_pages = str(row[col_map.get('Dirty Pages', 4)]) if 'Dirty Pages' in col_map else 'N/A'
        free_pages = str(row[col_map.get('Free Pages', 5)]) if 'Free Pages' in col_map else 'N/A'
        hit_rate = str(row[col_map.get('Buffer Pool Hit Rate', 6)]) if 'Buffer Pool Hit Rate' in col_map else 'N/A'
        dirty_pct = str(row[col_map.get('Dirty Page %', 7)]) if 'Dirty Page %' in col_map else 'N/A'
        
        # Format numeric values with commas
        try:
            total_pages = f"{int(total_pages):,}" if total_pages != 'N/A' and total_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            data_pages = f"{int(data_pages):,}" if data_pages != 'N/A' and data_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            dirty_pages = f"{int(dirty_pages):,}" if dirty_pages != 'N/A' and dirty_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            free_pages = f"{int(free_pages):,}" if free_pages != 'N/A' and free_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        # Generate buffer pool per-instance table if available
        buffer_pool_table_html = ''
        if 'buffer_pool_stats' in parsed_logs:
            bp_data = parsed_logs['buffer_pool_stats']
            if bp_data.success and bp_data.rows and bp_data.columns:
                bp_col_map = {col: idx for idx, col in enumerate(bp_data.columns)}
                display_columns = [
                    ('POOL_ID', 'POOL_ID'),
                    ('POOL_SIZE', 'POOL_SIZE'),
                    ('FREE_BUFFERS', 'FREE_BUFFERS'),
                    ('DATABASE_PAGES', 'DATABASE_PAGES'),
                    ('OLD_DATABASE_PAGES', 'OLD_DATABASE_PAGES'),
                    ('MODIFIED_DATABASE_PAGES', 'MODIFIED_DATABASE_PAGES')
                ]
                table_html = self._generate_table(
                    display_columns,
                    bp_data.rows,
                    bp_col_map,
                    bp_data.columns
                )
                buffer_pool_table_html = f'''
    <div class="info" style="margin-top: 15px;"><strong>Buffer Pool Per-Instance Statistics:</strong></div>
    {table_html}'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(pool_size)} GB</div>
            <div class="metric-label">Buffer Pool Size</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(pool_instances)}</div>
            <div class="metric-label">Buffer Pool Instances</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(total_pages)}</div>
            <div class="metric-label">Total Pages</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(data_pages)}</div>
            <div class="metric-label">Data Pages</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(dirty_pages)}</div>
            <div class="metric-label">Dirty Pages</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(free_pages)}</div>
            <div class="metric-label">Free Pages</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(hit_rate)}</div>
            <div class="metric-label">Buffer Pool Hit Rate</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{self._escape_html(dirty_pct)}</div>
            <div class="metric-label">Dirty Page %</div>
        </div>
    </div>
    {buffer_pool_table_html}
</div>'''
    
    def _generate_per_connection_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Per-Connection Memory Buffers subsection with table and estimate note."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No matching columns found</div>
</div>'''
        
        # Generate table
        table_html = self._generate_table(
            display_columns,
            log_data.rows,
            column_map,
            log_data.columns
        )
        
        # Add note above the table if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
<div class="info">{subsection.note}</div>'''
        
        # Get per_connection_memory_estimate data for the estimate note
        estimate_note_html = ''
        if 'per_connection_memory_estimate' in parsed_logs:
            estimate_data = parsed_logs['per_connection_memory_estimate']
            if estimate_data.success and estimate_data.rows and estimate_data.columns:
                est_col_map = {col: idx for idx, col in enumerate(estimate_data.columns)}
                est_row = estimate_data.rows[0]
                
                per_conn_mb = str(est_row[est_col_map.get('Per Connection (MB)', 0)]) if 'Per Connection (MB)' in est_col_map else 'N/A'
                max_conn = str(est_row[est_col_map.get('Max Connections', 1)]) if 'Max Connections' in est_col_map else 'N/A'
                est_max_mem = str(est_row[est_col_map.get('Estimated Max Connections Memory', 2)]) if 'Estimated Max Connections Memory' in est_col_map else 'N/A'
                
                estimate_note_html = f'''
<div class="info"><strong>Estimated Per-Connection Memory:</strong> ~{self._escape_html(per_conn_mb)} MB per connection. With max_connections={self._escape_html(max_conn)}, this could use up to {self._escape_html(est_max_mem)} GB if all connections are active.</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
    {estimate_note_html}
</div>'''
    
    def _generate_global_memory_usage_top_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Global Memory Usage - Top Components subsection with table and total note."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No matching columns found</div>
</div>'''
        
        # Apply collection limit to displayed rows
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Generate table with limited rows
        table_html = self._generate_table(
            display_columns,
            limited_rows,
            column_map,
            log_data.columns
        )
        
        # Add truncation notice if rows were limited
        truncation_html = ''
        if was_truncated:
            truncation_html = f'<p class="truncation-notice"><em>Showing {self._limit_enforcer.limit} of {total_count} rows</em></p>'
        
        # Add note above the table if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
<div class="info">{subsection.note}</div>'''
        
        # Get global_memory_usage_total data for the total note
        # This uses ALL rows for the total calculation, not just the displayed rows
        total_note_html = ''
        if 'global_memory_usage_total' in parsed_logs:
            total_data = parsed_logs['global_memory_usage_total']
            if total_data.success and total_data.rows and total_data.columns:
                total_col_map = {col: idx for idx, col in enumerate(total_data.columns)}
                total_row = total_data.rows[0]
                
                total_allocated = str(total_row[total_col_map.get('Total Allocated', 0)]) if 'Total Allocated' in total_col_map else 'N/A'
                high_watermark = str(total_row[total_col_map.get('High Watermark', 1)]) if 'High Watermark' in total_col_map else 'N/A'
                
                total_note_html = f'''
<div class="info"><strong>Total Tracked Memory:</strong> {self._escape_html(total_allocated)} currently allocated. High watermark: {self._escape_html(high_watermark)}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {truncation_html}
    {table_html}
    {total_note_html}
</div>'''
    
    def _generate_memory_troubleshooting_tips_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> str:
        """Generate HTML for Memory Troubleshooting Tips subsection."""
        tips = [
            ("High Memory Usage", "Check section 4.3 (Global Memory Usage) to identify which components are consuming memory"),
            ("Memory Spikes", 'Look at "High Watermark" columns - they show peak memory usage even if current usage is low'),
            ("Per-Connection Issues", "Check section 4.4 (Memory by Thread) to find memory-hungry connections"),
            ("Application Issues", "Use section 4.5 (Memory by User) to identify which applications/users consume most memory"),
            ("Prepared Statement Memory", "Check section 4.7 for prepared statement memory usage - common cause of memory issues"),
            ("Temporary Table Issues", "Section 4.9 shows if queries are creating too many disk temp tables"),
            ("Reset Statistics", "To reset high watermarks, run: <code>TRUNCATE TABLE performance_schema.memory_summary_global_by_event_name;</code>")
        ]
        
        tips_html = '\n'.join([
            f'<li><strong>{title}:</strong> {desc}</li>'
            for title, desc in tips
        ])
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="info">
        <strong>How to Use This Section for Troubleshooting:</strong>
        <ul style="margin-top: 10px; margin-bottom: 0;">
            {tips_html}
        </ul>
    </div>
</div>'''
    
    def _generate_aurora_innodb_note_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> str:
        """Generate HTML for Aurora InnoDB Note subsection (Aurora MySQL only)."""
        note_text = subsection.note if subsection.note else ""
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <div class="info">
        <strong>Note:</strong>
        <ul style="margin-top: 10px; margin-bottom: 0;">
            <li>Aurora MySQL uses a distributed storage layer that automatically manages I/O, flushing, and redo log sizing. Many traditional InnoDB tuning parameters are not applicable or modifiable in Aurora.</li>
            <li>Redo log sizing and flushing are managed automatically by Aurora's distributed storage layer. These configuration parameters do not apply to Aurora.</li>
            <li>Redo log management is handled automatically by Aurora's distributed storage layer. Redo log occupancy monitoring and sizing are not applicable to Aurora instances.</li>
        </ul>
    </div>
</div>'''
    
    def _generate_innodb_purge_analysis_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for InnoDB Purge Analysis composite subsection."""
        
        # Get history list length for health status
        history_list_length = 0
        if 'innodb_history_length' in parsed_logs:
            hll_data = parsed_logs['innodb_history_length']
            if hll_data.success and hll_data.rows and hll_data.columns:
                try:
                    history_list_length = int(hll_data.rows[0][0])
                except (ValueError, TypeError, IndexError):
                    pass
        
        # Check for long-running transactions (older than 5 minutes = 300 seconds)
        has_long_running_trx = False
        if 'long_running_idle_trx' in parsed_logs:
            trx_data = parsed_logs['long_running_idle_trx']
            if trx_data.success and trx_data.rows:
                has_long_running_trx = True
        if not has_long_running_trx and 'innodb_transactions' in parsed_logs:
            trx_data = parsed_logs['innodb_transactions']
            if trx_data.success and trx_data.rows and trx_data.columns:
                # Check if any transaction is older than 300 seconds
                try:
                    duration_idx = trx_data.columns.index('duration_seconds')
                    for row in trx_data.rows:
                        if row[duration_idx] and int(row[duration_idx]) > 300:
                            has_long_running_trx = True
                            break
                except (ValueError, IndexError):
                    pass
        
        # Build Understanding Purge section
        understanding_html = '''
<div class="info" style="margin-bottom: 15px;">
    <strong>Understanding Purge:</strong> InnoDB purge is a background process that removes old row versions (undo logs) no longer needed for MVCC or rollback. When purge cannot keep up with transaction rate, the History List Length grows, causing:
    <ul style="margin-top: 10px; margin-bottom: 10px;">
        <li>Increased disk space (undo logs)</li>
        <li>Slower queries (more versions to skip)</li>
        <li>Higher I/O consumption</li>
        <li>Potential disk log pressure</li>
    </ul>
    <strong>Reference:</strong> <a href="https://aws.amazon.com/blogs/database/achieve-a-high-speed-innodb-purge-on-amazon-rds-for-mysql-and-amazon-aurora-mysql/" target="_blank">AWS Blog: Achieve a high-speed InnoDB purge</a>
</div>'''
        
        # Build Purge Thread Activity table
        thread_activity_html = self._build_purge_thread_activity_html(parsed_logs)
        
        # Build Purge Configuration table
        purge_config_html = self._build_purge_configuration_html(parsed_logs)
        
        # Build Purge Performance Metrics
        purge_metrics_html = self._build_purge_performance_metrics_html(parsed_logs)
        
        # Build Purge Optimization Recommendations
        recommendations_html = '''
<div class="info" style="margin-top: 15px; margin-bottom: 15px;">
    <strong>Purge Optimization Recommendations:</strong>
    <p style="margin-top: 10px;"><strong>Based on AWS Best Practices:</strong></p>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        <li><strong>Monitor History List Length:</strong> Set CloudWatch alarms for values >10K (warning) and >50K (critical)</li>
        <li><strong>Avoid Long-Running Transactions:</strong> Keep transactions short; long transactions block purge for all subsequent transactions</li>
        <li><strong>Optimize Workload:</strong> Use DROP PARTITION or DROP TABLE instead of DELETE for bulk deletion use cases (age-partitioned)</li>
        <li><strong>Secondary Indexes:</strong> Purge is slower with many secondary indexes. Review unused indexes with sys.schema_unused_indexes</li>
        <li><strong>Table Sharding:</strong> Spread writes across multiple tables to increase number of purge threads for better parallelism</li>
        <li><strong>Instance Sizing:</strong> Ensure adequate CPU, memory (buffer pool), and I/O capacity for purge threads</li>
        <li><strong>Aurora-Specific:</strong> In Aurora, purge performance scales to Serverless v2 scales on replicas. Consider aurora_read_replica_read_committed=ON</li>
    </ul>
</div>'''
        
        # Build Purge Health Status
        if history_list_length < 50000:
            status_class = "success"
            status_text = f"OK - Purge operating normally. History list length ({history_list_length:,}) is healthy."
        elif history_list_length < 100000:
            status_class = "warning"
            status_text = f"WARNING - History list length ({history_list_length:,}) is elevated. Monitor purge performance and check for long-running transactions."
        else:
            status_class = "critical"
            if has_long_running_trx:
                status_text = f'CRITICAL - History list length ({history_list_length:,}) is very high. Long-running transaction(s) detected that may be blocking purge. See <a href="#active-transactions">Active Transactions</a> section.'
            else:
                status_text = f"CRITICAL - History list length ({history_list_length:,}) is very high. Purge may not be keeping up with workload."
        
        health_status_html = f'''
<div class="purge-health-status {status_class}" style="margin-top: 15px; padding: 10px; border-radius: 4px; background-color: {"#d4edda" if status_class == "success" else "#fff3cd" if status_class == "warning" else "#f8d7da"}; border: 1px solid {"#c3e6cb" if status_class == "success" else "#ffeeba" if status_class == "warning" else "#f5c6cb"};">
    <strong>Purge Health Status:</strong> {status_text}
</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <p style="color: #666; margin-bottom: 15px;">Detailed analysis of InnoDB purge operations. Purge removes old row versions (undo logs) created by MVCC. Slow purge causes History List Length growth, causing performance degradation.</p>
    {understanding_html}
    {thread_activity_html}
    {purge_config_html}
    {purge_metrics_html}
    {recommendations_html}
    {health_status_html}
</div>'''
    
    def _generate_innodb_engine_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for InnoDB Engine Status (Detailed Diagnostics) composite subsection."""
        
        # Get parsed InnoDB status data
        status_data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        if not status_data:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">InnoDB engine status not available</div>
</div>'''
        
        # Build all sub-sections
        sections_html = []
        
        # 1. Latest Detected Deadlock
        sections_html.append(self._build_deadlock_html(status_data))
        
        # 2. Latest Foreign Key Error
        sections_html.append(self._build_fk_error_html(status_data))
        
        # 3. Semaphore Contention (MySQL/MariaDB only, not Aurora)
        # This will be filtered by engine condition in report_format.json
        sections_html.append(self._build_semaphore_html(status_data))
        
        # 4. Transaction & Purge Status
        sections_html.append(self._build_trx_purge_status_html(status_data))
        
        # 5. Redo Log Checkpoint Status (MySQL/MariaDB only)
        sections_html.append(self._build_redo_log_checkpoint_html(status_data))
        
        # 6. Buffer Pool Efficiency (MySQL/MariaDB only)
        sections_html.append(self._build_buffer_pool_efficiency_html(status_data))
        
        # 7. Adaptive Hash Index Efficiency (MySQL/MariaDB only)
        sections_html.append(self._build_adaptive_hash_html(status_data))
        
        # 8. Row Operations Summary
        sections_html.append(self._build_row_operations_html(status_data))
        
        # 9. InnoDB Status Optimization Tips
        sections_html.append(self._build_innodb_status_tips_html())
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <p style="color: #666; margin-bottom: 15px;">Detailed diagnostics from SHOW ENGINE INNODB STATUS. This provides internal InnoDB metrics for advanced troubleshooting.</p>
    {''.join(sections_html)}
</div>'''
    
    def _build_deadlock_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Latest Detected Deadlock section."""
        if not data.has_deadlock:
            return '''
<div class="sub-subsection">
    <h4>Latest Detected Deadlock</h4>
    <div class="no-data">No deadlocks detected since server restart</div>
</div>'''
        
        # Build deadlock info
        trx_info = []
        for trx in data.deadlock_transactions:
            trx_html = f"<li><strong>Transaction {trx.get('number', '?')}</strong>"
            if 'trx_id' in trx:
                trx_html += f" (ID: {trx['trx_id']})"
            if 'query' in trx:
                trx_html += f"<br><code style='font-size: 0.85em;'>{self._escape_html(trx['query'][:200])}</code>"
            trx_html += "</li>"
            trx_info.append(trx_html)
        
        victim_html = ""
        if data.deadlock_victim:
            victim_html = f"<p><strong>Victim (rolled back):</strong> Transaction {data.deadlock_victim}</p>"
        
        timestamp_html = ""
        if data.deadlock_timestamp:
            timestamp_html = f"<p><strong>Timestamp:</strong> {data.deadlock_timestamp}</p>"
        
        note_html = '''
<div class="info" style="margin-top: 15px;">
    <strong>Notes:</strong>
    <ul style="margin-top: 5px; margin-bottom: 5px;">
        <li>This shows only the most recent deadlock. To log all deadlocks to the error log, enable <code>innodb_print_all_deadlocks=ON</code>.</li>
        <li>The <code>innodb_deadlock_detect</code> variable controls whether InnoDB performs deadlock detection. When enabled (default), InnoDB automatically detects deadlocks and rolls back one transaction. Disabling it (<code>innodb_deadlock_detect=OFF</code>) requires relying on <code>innodb_lock_wait_timeout</code> to handle deadlocks.</li>
    </ul>
    <strong>Best practices to avoid deadlocks:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        <li>Access tables and rows in a consistent order across all transactions</li>
        <li>Keep transactions short</li>
        <li>Add proper indexes to reduce lock scope</li>
        <li>Consider using <code>SELECT ... FOR UPDATE NOWAIT</code> or <code>SKIP LOCKED</code> for lock-free patterns</li>
    </ul>
</div>'''
        
        return f'''
<div class="sub-subsection">
    <h4>Latest Detected Deadlock</h4>
    {timestamp_html}
    <p><strong>Transactions involved:</strong></p>
    <ul>{''.join(trx_info)}</ul>
    {victim_html}
    {note_html}
</div>'''
    
    def _build_fk_error_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Latest Foreign Key Error section."""
        if not data.has_fk_error:
            return '''
<div class="sub-subsection">
    <h4>Latest Foreign Key Error</h4>
    <div class="no-data">No foreign key errors since server restart</div>
</div>'''
        
        details = []
        if data.fk_error_timestamp:
            details.append(f"<tr><td><strong>Timestamp</strong></td><td>{data.fk_error_timestamp}</td></tr>")
        if data.fk_error_constraint:
            details.append(f"<tr><td><strong>Constraint</strong></td><td>{self._escape_html(data.fk_error_constraint)}</td></tr>")
        if data.fk_error_table:
            details.append(f"<tr><td><strong>Table</strong></td><td>{self._escape_html(data.fk_error_table)}</td></tr>")
        if data.fk_error_statement:
            details.append(f"<tr><td><strong>Statement</strong></td><td><code>{self._escape_html(data.fk_error_statement[:300])}</code></td></tr>")
        
        note_html = '''
<div class="info" style="margin-top: 15px;">
    <strong>Best practices to avoid FK errors:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        <li>Ensure parent rows exist before inserting child rows</li>
        <li>Use <code>ON DELETE CASCADE</code> or <code>ON UPDATE CASCADE</code> carefully</li>
        <li>Verify data integrity before enabling FK constraints on existing data</li>
        <li>Avoid performing operations with <code>foreign_key_checks</code> disabled as this can compromise data integrity and lead to foreign key errors</li>
    </ul>
</div>'''
        
        return f'''
<div class="sub-subsection">
    <h4>Latest Foreign Key Error</h4>
    <table class="data-table">
        <tbody>{''.join(details)}</tbody>
    </table>
    {note_html}
</div>'''
    
    def _build_semaphore_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Semaphore Contention section."""
        # Build metrics table
        rows = [
            f"<tr><td>OS Waits Reservation Count</td><td>{data.os_waits_reservation_count:,}</td></tr>",
            f"<tr><td>OS Waits Signal Count</td><td>{data.os_waits_signal_count:,}</td></tr>",
            f"<tr><td>RW-shared: Spins / Rounds / OS Waits</td><td>{data.rw_shared_spins:,} / {data.rw_shared_rounds:,} / {data.rw_shared_os_waits:,}</td></tr>",
            f"<tr><td>RW-excl: Spins / Rounds / OS Waits</td><td>{data.rw_excl_spins:,} / {data.rw_excl_rounds:,} / {data.rw_excl_os_waits:,}</td></tr>",
            f"<tr><td>RW-sx: Spins / Rounds / OS Waits</td><td>{data.rw_sx_spins:,} / {data.rw_sx_rounds:,} / {data.rw_sx_os_waits:,}</td></tr>",
            f"<tr><td>Spin Rounds Per Wait (shared/excl/sx)</td><td>{data.spin_rounds_per_wait_shared:.2f} / {data.spin_rounds_per_wait_excl:.2f} / {data.spin_rounds_per_wait_sx:.2f}</td></tr>",
        ]
        
        # Calculate OS wait ratio
        if data.os_waits_reservation_count > 0:
            wait_ratio = (data.os_waits_signal_count / data.os_waits_reservation_count) * 100
            rows.append(f"<tr><td>Signal/Reservation Ratio</td><td>{wait_ratio:.1f}%</td></tr>")
        
        # Current waits
        current_waits_html = ""
        if data.current_waits:
            waits_list = "".join([f"<li><code>{self._escape_html(w[:200])}</code></li>" for w in data.current_waits[:5]])
            current_waits_html = f'''
<div class="warning" style="margin-top: 10px;">
    <strong>Current Waits (threads waiting for semaphores):</strong>
    <ul style="margin-top: 5px;">{waits_list}</ul>
</div>'''
        
        note_html = '''
<div class="info" style="margin-top: 15px;">
    <strong>Note:</strong> High spin rounds per wait values are influenced by <code>innodb_spin_wait_delay</code>, <code>innodb_spin_wait_pause_multiplier</code>, and <code>innodb_sync_spin_loops</code>. The presence of threads in the current wait list indicates active contention.
</div>'''
        
        return f'''
<div class="sub-subsection">
    <h4>Semaphore Contention</h4>
    <table class="data-table">
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    {current_waits_html}
    {note_html}
</div>'''
    
    def _build_trx_purge_status_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Transaction & Purge Status section."""
        # Determine purge health
        purge_status = "Unknown"
        purge_class = ""
        if data.purge_state:
            if "running but idle" in data.purge_state.lower():
                purge_status = "Purge is current (running but idle)"
                purge_class = "success"
            elif "running" in data.purge_state.lower():
                purge_status = "Purge is actively running"
                purge_class = "warning"
            else:
                purge_status = data.purge_state
        
        if data.purge_lag > 0:
            purge_status += f" - {data.purge_lag:,} transactions behind"
            if data.purge_lag > 1000:
                purge_class = "warning"
            if data.purge_lag > 10000:
                purge_class = "critical"
        
        status_style = ""
        if purge_class == "success":
            status_style = "background-color: #d4edda; border: 1px solid #c3e6cb; padding: 5px; border-radius: 4px;"
        elif purge_class == "warning":
            status_style = "background-color: #fff3cd; border: 1px solid #ffeeba; padding: 5px; border-radius: 4px;"
        elif purge_class == "critical":
            status_style = "background-color: #f8d7da; border: 1px solid #f5c6cb; padding: 5px; border-radius: 4px;"
        
        rows = [
            f"<tr><td>Current Transaction ID Counter</td><td>{data.trx_id_counter:,}</td></tr>",
            f"<tr><td>Purge Done For Transaction</td><td>{data.purge_done_for_trx:,}</td></tr>",
            f"<tr><td>Purge Lag (transactions behind)</td><td>{data.purge_lag:,}</td></tr>",
            f"<tr><td>History List Length</td><td>{data.history_list_length:,}</td></tr>",
            f"<tr><td>Purge State</td><td><span style='{status_style}'>{purge_status}</span></td></tr>",
        ]
        
        interpretation = ""
        if data.purge_lag > 10000:
            interpretation = '''
<div class="warning" style="margin-top: 10px;">
    <strong>Warning:</strong> Purge lag is high. Investigate long-running transactions blocking purge.
</div>'''
        
        return f'''
<div class="sub-subsection">
    <h4>Transaction &amp; Purge Status</h4>
    <table class="data-table">
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    {interpretation}
</div>'''
    
    def _build_redo_log_checkpoint_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Redo Log Checkpoint Status section."""
        rows = [
            f"<tr><td>Log Sequence Number (LSN)</td><td>{data.log_sequence_number:,}</td></tr>",
            f"<tr><td>Log Flushed Up To</td><td>{data.log_flushed_up_to:,}</td></tr>",
            f"<tr><td>Pages Flushed Up To</td><td>{data.pages_flushed_up_to:,}</td></tr>",
            f"<tr><td>Last Checkpoint At</td><td>{data.last_checkpoint_at:,}</td></tr>",
            f"<tr><td>Checkpoint Lag (bytes)</td><td>{data.checkpoint_lag:,}</td></tr>",
            f"<tr><td>Unflushed Log (bytes)</td><td>{data.unflushed_log:,}</td></tr>",
            f"<tr><td>Pending Log Flushes</td><td>{data.pending_log_flushes}</td></tr>",
            f"<tr><td>Pending Checkpoint Writes</td><td>{data.pending_chkp_writes}</td></tr>",
            f"<tr><td>Log I/Os Done</td><td>{data.log_ios_done:,}</td></tr>",
            f"<tr><td>Log I/Os Per Second</td><td>{data.log_ios_per_second:.2f}</td></tr>",
        ]
        
        note_html = ""
        if data.checkpoint_lag > 0 or data.unflushed_log > 0:
            note_html = '''
<div class="info" style="margin-top: 15px;">
    <strong>Recommendations:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        <li>Checkpoint lag indicates how much redo log data hasn't been checkpointed yet. High checkpoint lag under heavy write workloads may indicate redo log is too small or I/O is a bottleneck.</li>
        <li>Consider increasing <code>innodb_log_file_size</code> if checkpoint lag is consistently high.</li>
    </ul>
</div>'''
        
        return f'''
<div class="sub-subsection">
    <h4>Redo Log Checkpoint Status</h4>
    <table class="data-table">
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    {note_html}
</div>'''
    
    def _build_buffer_pool_efficiency_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Buffer Pool Efficiency section."""
        hit_rate_pct = (data.buffer_pool_hit_rate / data.buffer_pool_hit_rate_denominator * 100) if data.buffer_pool_hit_rate_denominator > 0 else 0
        
        rows = [
            f"<tr><td>Buffer Pool Hit Rate</td><td>{data.buffer_pool_hit_rate} / {data.buffer_pool_hit_rate_denominator} ({hit_rate_pct:.2f}%)</td></tr>",
            f"<tr><td>Pages Made Young</td><td>{data.pages_made_young:,}</td></tr>",
            f"<tr><td>Pages Made Not Young</td><td>{data.pages_made_not_young:,}</td></tr>",
            f"<tr><td>Youngs Per Second</td><td>{data.youngs_per_sec:.2f}</td></tr>",
            f"<tr><td>Non-Youngs Per Second</td><td>{data.non_youngs_per_sec:.2f}</td></tr>",
            f"<tr><td>Read Ahead Per Second</td><td>{data.read_ahead_per_sec:.2f}</td></tr>",
            f"<tr><td>Evicted Without Access Per Second</td><td>{data.evicted_without_access_per_sec:.2f}</td></tr>",
            f"<tr><td>LRU Length</td><td>{data.lru_len:,}</td></tr>",
        ]
        
        note_html = ""
        if hit_rate_pct < 99.9 or data.evicted_without_access_per_sec > 0:
            note_html = '''
<div class="info" style="margin-top: 15px;">
    <strong>Recommendations:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        <li>Buffer pool hit rate below 99.9% may indicate buffer pool is undersized for the working set.</li>
        <li>High "evicted without access" rate suggests pages are being evicted before being read, indicating memory pressure.</li>
        <li>Review queries causing full table scans that pollute the buffer pool.</li>
        <li>Check if <code>innodb_buffer_pool_size</code>, <code>innodb_old_blocks_pct</code> and <code>innodb_old_blocks_time</code> are tuned appropriately.</li>
    </ul>
</div>'''
        
        return f'''
<div class="sub-subsection">
    <h4>Buffer Pool Efficiency</h4>
    <table class="data-table">
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    {note_html}
</div>'''
    
    def _build_adaptive_hash_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Adaptive Hash Index Efficiency section."""
        total_searches = data.hash_searches_per_sec + data.non_hash_searches_per_sec
        hash_ratio = (data.hash_searches_per_sec / total_searches * 100) if total_searches > 0 else 0
        
        rows = [
            f"<tr><td>Hash Searches Per Second</td><td>{data.hash_searches_per_sec:.2f}</td></tr>",
            f"<tr><td>Non-Hash Searches Per Second</td><td>{data.non_hash_searches_per_sec:.2f}</td></tr>",
            f"<tr><td>Hash Search Ratio</td><td>{hash_ratio:.1f}%</td></tr>",
            f"<tr><td>Hash Table Size</td><td>{data.hash_table_size:,}</td></tr>",
        ]
        
        note_html = ""
        if total_searches > 0:
            note_html = '''
<div class="info" style="margin-top: 15px;">
    <strong>About Adaptive Hash Index (AHI):</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        <li>AHI builds in-memory hash indexes for frequently accessed index pages.</li>
        <li>Low hash search ratio may indicate AHI is not benefiting your workload.</li>
    </ul>
    <strong>Recommendations:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        <li>If hash searches are consistently near zero, consider disabling AHI with <code>innodb_adaptive_hash_index=OFF</code> to reduce memory overhead.</li>
        <li>AHI works best with point lookups; range scans don't benefit from it.</li>
        <li>Monitor <code>innodb_adaptive_hash_index_parts</code> for partitioning to reduce contention.</li>
    </ul>
</div>'''
        
        return f'''
<div class="sub-subsection">
    <h4>Adaptive Hash Index Efficiency</h4>
    <table class="data-table">
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    {note_html}
</div>'''
    
    def _build_row_operations_html(self, data: 'InnoDBStatusData') -> str:
        """Build HTML for Row Operations Summary section."""
        return f'''
<div class="sub-subsection">
    <h4>Row Operations Summary</h4>
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{data.queries_inside_innodb}</div>
            <div class="metric-label">Queries Inside InnoDB</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{data.queries_in_queue}</div>
            <div class="metric-label">Queries in Queue</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{data.inserts_per_sec:.2f}</div>
            <div class="metric-label">Inserts/sec</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{data.updates_per_sec:.2f}</div>
            <div class="metric-label">Updates/sec</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{data.deletes_per_sec:.2f}</div>
            <div class="metric-label">Deletes/sec</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{data.reads_per_sec_rows:.2f}</div>
            <div class="metric-label">Reads/sec</div>
        </div>
    </div>
    <p style="margin-top: 10px; color: #666;"><strong>Main Thread State:</strong> {data.main_thread_state or 'Unknown'}</p>
</div>'''
    
    def _build_innodb_status_tips_html(self) -> str:
        """Build HTML for InnoDB Status Optimization Tips section."""
        tips = [
            ("<strong>Deadlock Victim</strong>", "The transaction InnoDB chose to roll back to break the deadlock cycle. InnoDB selects the transaction that modified fewer rows as the victim to minimize rollback cost."),
            ("<strong>Purge Lag</strong>", "The number of transactions whose old row versions haven't been cleaned up yet. Growing purge lag indicates long-running transactions are blocking purge, causing undo log growth and table bloat."),
            ("<strong>History List Length</strong>", "The number of unpurged transactions in the undo log. Values consistently above 10,000 indicate purge is falling behind, leading to increased disk usage and slower queries due to MVCC overhead."),
            ("<strong>Checkpoint Lag</strong>", "The difference between the current Log Sequence Number (LSN) and the last checkpoint. Large checkpoint lag means more redo log data needs to be replayed during crash recovery, increasing recovery time."),
            ("<strong>Unflushed Log</strong>", "The amount of data (in bytes) written to the InnoDB Log Buffer but not yet flushed to redo log files on disk. If this approaches <code>innodb_log_buffer_size</code>, it may indicate I/O bottleneck or undersized log buffer."),
            ("<strong>Buffer Pool Hit Rate</strong>", "The percentage of page requests served from the buffer pool cache vs disk reads. Values below 99.9% suggest the buffer pool may be undersized for your working set, causing excessive disk I/O."),
            ("<strong>Pages Evicted Without Access</strong>", "Pages that were loaded into the buffer pool but evicted before being read. High values indicate memory pressure or queries causing unnecessary page loads (e.g., full table scans)."),
            ("<strong>Pages Made Young/Not Young</strong>", "Tracks how pages move between the 'old' and 'young' sublists in the buffer pool LRU. High 'not young' values may indicate <code>innodb_old_blocks_time</code> is filtering out scan-loaded pages effectively."),
            ("<strong>Adaptive Hash Index (AHI) Efficiency</strong>", "The ratio of hash searches to non-hash searches. Low ratio means AHI isn't benefiting your workload; consider disabling it to free memory and reduce contention."),
            ("<strong>Spin Rounds Per Wait</strong>", "The number of spin loop iterations before a thread gives up and performs an OS wait. Values are influenced by <code>innodb_spin_wait_delay</code> and <code>innodb_sync_spin_loops</code>. High values with many OS waits indicate contention."),
            ("<strong>OS Waits vs Reservation Count</strong>", "The ratio of expensive OS-level waits to total lock attempts. Lower ratio is better, meaning more locks were acquired via cheap spinning rather than costly context switches."),
            ("<strong>Queries in Queue</strong>", "The number of queries waiting to enter InnoDB. Values consistently above 0 indicate thread contention; consider tuning <code>innodb_thread_concurrency</code>."),
            ("<strong>Main Thread State</strong>", "The current state of the InnoDB master background thread (sleeping, purging, flushing). Persistent non-sleeping states may indicate heavy background activity."),
        ]
        
        tips_html = "".join([f"<li>{title} - {desc}</li>" for title, desc in tips])
        
        return f'''
<div class="sub-subsection">
    <h4>InnoDB Status Optimization Tips</h4>
    <div class="info">
        <ul style="margin: 0; padding-left: 20px;">
            {tips_html}
        </ul>
    </div>
</div>'''
    
    def _generate_long_running_transactions_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Long-Running Transactions composite subsection."""
        
        # Build Active Transactions table
        active_trx_html = self._build_active_transactions_html(parsed_logs)
        
        # Build Long-Running Idle Transactions table
        idle_trx_html = self._build_long_running_idle_trx_html(parsed_logs)
        
        # Build Long-Running Active Transactions table
        active_long_trx_html = self._build_long_running_active_trx_html(parsed_logs)
        
        # Get note from subsection
        note_html = ''
        if subsection.note:
            note_html = f'<div class="info" style="margin-bottom: 15px;">{subsection.note}</div>'
        
        # Generate anchor ID
        anchor_id = self._emit_subsection_anchor_id(subsection.name)
        
        return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {active_trx_html}
    {idle_trx_html}
    {active_long_trx_html}
</div>'''
    
    def _build_active_transactions_html(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build HTML table for Active Transactions."""
        if 'innodb_transactions' not in parsed_logs:
            return '''
<div class="sub-subsection">
    <h4>Active Transactions</h4>
    <div class="no-data">No active transactions</div>
</div>'''
        
        log_data = parsed_logs['innodb_transactions']
        
        if not log_data.success or not log_data.rows:
            return '''
<div class="sub-subsection">
    <h4>Active Transactions</h4>
    <div class="no-data">No active transactions</div>
</div>'''
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'duration_seconds', 'trx_mysql_thread_id', 'trx_query', 'trx_rows_locked', 'trx_rows_modified']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            return '''
<div class="sub-subsection">
    <h4>Active Transactions</h4>
    <div class="no-data">No matching columns found</div>
</div>'''
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_cells = [f'<th>{self._escape_html(col)}</th>' for col in display_columns]
        header_row = f'<tr>{"".join(header_cells)}</tr>'
        
        # Build data rows
        data_rows = []
        for row in log_data.rows[:20]:  # Limit to 20 rows
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                cells.append(f'<td>{self._escape_html(str(val) if val is not None else "")}</td>')
            data_rows.append(f'<tr>{"".join(cells)}</tr>')
        
        rows_html = '\n'.join(data_rows)
        
        return f'''
<div class="sub-subsection">
    <h4>Active Transactions</h4>
    <table>
        <thead>{header_row}</thead>
        <tbody>{rows_html}</tbody>
    </table>
</div>'''
    
    def _build_long_running_idle_trx_html(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build HTML table for Long-Running Idle Transactions."""
        if 'long_running_idle_trx' not in parsed_logs:
            return '''
<div class="sub-subsection">
    <h4>Long-Running Idle Transactions (&gt;5 min, no query)</h4>
    <p class="info" style="margin-bottom: 10px;">Transactions that started but have no current query running. May indicate application issues or forgotten transactions.</p>
    <div class="no-data">No idle transactions older than 5 minutes</div>
</div>'''
        
        log_data = parsed_logs['long_running_idle_trx']
        
        if not log_data.success or not log_data.rows:
            return '''
<div class="sub-subsection">
    <h4>Long-Running Idle Transactions (&gt;5 min, no query)</h4>
    <p class="info" style="margin-bottom: 10px;">Transactions that started but have no current query running. May indicate application issues or forgotten transactions.</p>
    <div class="no-data">No idle transactions older than 5 minutes</div>
</div>'''
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'trx_rows_modified']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            return '''
<div class="sub-subsection">
    <h4>Long-Running Idle Transactions (&gt;5 min, no query)</h4>
    <div class="no-data">No matching columns found</div>
</div>'''
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_cells = [f'<th>{self._escape_html(col)}</th>' for col in display_columns]
        header_row = f'<tr>{"".join(header_cells)}</tr>'
        
        # Build data rows
        data_rows = []
        for row in log_data.rows[:20]:  # Limit to 20 rows
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                cells.append(f'<td>{self._escape_html(str(val) if val is not None else "")}</td>')
            data_rows.append(f'<tr>{"".join(cells)}</tr>')
        
        rows_html = '\n'.join(data_rows)
        
        return f'''
<div class="sub-subsection">
    <h4>Long-Running Idle Transactions (&gt;5 min, no query)</h4>
    <p class="info" style="margin-bottom: 10px;">Transactions that started but have no current query running. May indicate application issues or forgotten transactions.</p>
    <table>
        <thead>{header_row}</thead>
        <tbody>{rows_html}</tbody>
    </table>
</div>'''
    
    def _build_long_running_active_trx_html(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build HTML table for Long-Running Active Transactions."""
        if 'long_running_active_trx' not in parsed_logs:
            return '''
<div class="sub-subsection">
    <h4>Long-Running Active Transactions (&gt;1 min, with query)</h4>
    <p class="info" style="margin-bottom: 10px;">Transactions with queries currently executing. May indicate slow queries or large batch operations.</p>
    <div class="no-data">No active transactions running longer than 1 minute</div>
</div>'''
        
        log_data = parsed_logs['long_running_active_trx']
        
        if not log_data.success or not log_data.rows:
            return '''
<div class="sub-subsection">
    <h4>Long-Running Active Transactions (&gt;1 min, with query)</h4>
    <p class="info" style="margin-bottom: 10px;">Transactions with queries currently executing. May indicate slow queries or large batch operations.</p>
    <div class="no-data">No active transactions running longer than 1 minute</div>
</div>'''
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'current_query']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            return '''
<div class="sub-subsection">
    <h4>Long-Running Active Transactions (&gt;1 min, with query)</h4>
    <div class="no-data">No matching columns found</div>
</div>'''
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_cells = [f'<th>{self._escape_html(col)}</th>' for col in display_columns]
        header_row = f'<tr>{"".join(header_cells)}</tr>'
        
        # Build data rows
        data_rows = []
        for row in log_data.rows[:20]:  # Limit to 20 rows
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                cells.append(f'<td>{self._escape_html(str(val) if val is not None else "")}</td>')
            data_rows.append(f'<tr>{"".join(cells)}</tr>')
        
        rows_html = '\n'.join(data_rows)
        
        return f'''
<div class="sub-subsection">
    <h4>Long-Running Active Transactions (&gt;1 min, with query)</h4>
    <p class="info" style="margin-bottom: 10px;">Transactions with queries currently executing. May indicate slow queries or large batch operations.</p>
    <table>
        <thead>{header_row}</thead>
        <tbody>{rows_html}</tbody>
    </table>
</div>'''
    
    def _generate_long_running_trx_activity_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Long-Running Transactions Activity subsection."""
        source_id = subsection.source
        anchor_id = self._emit_subsection_anchor_id(subsection.name)
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            note_html = f'<div class="info" style="margin-bottom: 10px;">{subsection.note}</div>' if subsection.note else ''
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            note_html = f'<div class="info" style="margin-bottom: 10px;">{subsection.note}</div>' if subsection.note else ''
            return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build the data table
        columns = ['mysql_pid', 'trx_duration', 'trx_state', 'sql_text', 'stmt_execution_time', 'rows_sent', 'rows_modified', 'rows_examined', 'tmp_disk_tables', 'error_info']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            display_columns = log_data.columns
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_cells = [f'<th>{self._escape_html(col)}</th>' for col in display_columns]
        header_row = f'<tr>{"".join(header_cells)}</tr>'
        
        # Build data rows
        data_rows = []
        for row in log_data.rows:
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                # Truncate long SQL text for display
                if col == 'sql_text' and val and len(str(val)) > 200:
                    val = str(val)[:200] + '...'
                cells.append(f'<td>{self._escape_html(str(val) if val is not None else "")}</td>')
            data_rows.append(f'<tr>{"".join(cells)}</tr>')
        
        rows_html = '\n'.join(data_rows)
        
        # Build note HTML
        note_html = f'<div class="info" style="margin-bottom: 10px;">{subsection.note}</div>' if subsection.note else ''
        
        # Build interpretation when results are present
        interpretation_html = '''
<div class="info" style="margin-top: 15px; background-color: #e7f3ff; border-left: 4px solid #2196F3; padding: 12px;">
    <strong>📊 Interpretation Guide:</strong><br><br>
    This section shows the <strong>10 most recent SQL statements</strong> executed within each long-running transaction (>5 minutes). Use this to understand what operations the transaction has performed.<br><br>
    <strong>Key columns:</strong>
    <ul style="margin: 8px 0; padding-left: 20px;">
        <li><strong>mysql_pid</strong>: Process ID - use to identify the connection</li>
        <li><strong>trx_duration</strong>: Total time the transaction has been active</li>
        <li><strong>sql_text</strong>: The SQL statement that was executed</li>
        <li><strong>stmt_execution_time</strong>: How long this specific statement took</li>
        <li><strong>rows_sent</strong>: Number of rows returned to the client</li>
        <li><strong>rows_modified</strong>: Number of rows affected (INSERT/UPDATE/DELETE)</li>
        <li><strong>rows_examined</strong>: Number of rows scanned (high values may indicate missing indexes)</li>
        <li><strong>tmp_disk_tables</strong>: Disk temp tables created (high values indicate inefficient queries)</li>
        <li><strong>error_info</strong>: Shows if the statement encountered errors</li>
    </ul>
    <strong>What to look for:</strong>
    <ul style="margin: 8px 0; padding-left: 20px;">
        <li>Statements with high <code>rows_modified</code> may be causing lock contention</li>
        <li>High <code>rows_examined</code> vs <code>rows_sent</code> ratio suggests missing indexes</li>
        <li>High <code>tmp_disk_tables</code> suggests queries need optimization</li>
        <li>Multiple statements per PID show the transaction's activity history</li>
        <li>Use the PID to correlate with application logs or kill the connection if needed</li>
    </ul>
</div>'''
        
        return f'''
<div class="subsection" id="{anchor_id}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <table class="data-table">
        <thead>{header_row}</thead>
        <tbody>{rows_html}</tbody>
    </table>
    {interpretation_html}
</div>'''
    
    def _generate_tables_without_pk_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Tables Without Primary Key subsection with detailed info box."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No data available</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case - all tables have PKs
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data" style="color: #155724; background-color: #d4edda; border: 1px solid #c3e6cb; padding: 10px; border-radius: 4px;">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Tables without PK found - show critical warning and info box
        table_count = len(log_data.rows)
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        data_rows = []
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                if i < len(row):
                    cells.append(f'<td>{self._format_cell_value(row[i])}</td>')
                else:
                    cells.append('<td></td>')
            data_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {chr(10).join(data_rows)}
    </tbody>
</table>'''
        
        # Build the critical warning and info box
        info_box_html = f'''
<div style="background-color: #f8d7da; border: 1px solid #f5c6cb; border-radius: 4px; padding: 15px; margin-bottom: 15px;">
    <div style="color: #721c24; font-weight: bold; margin-bottom: 10px;">⚠ CRITICAL REPLICATION IMPACT:</div>
    <ul style="color: #721c24; margin: 0 0 15px 0; padding-left: 20px;">
        <li><strong>Full Table Scans on Replicas:</strong> Every UPDATE/DELETE on these tables causes a FULL TABLE SCAN on replicas to locate the row. On large tables, this causes severe performance degradation and replication lag.</li>
        <li><strong>Row-Based Replication (RBR):</strong> With binlog_format=ROW (default in MySQL 8.0+), replicas must scan entire tables to find matching rows. This can make replicas orders of magnitude slower than the source.</li>
        <li><strong>Semi-Sync Replication:</strong> Without PKs, semi-sync replication can timeout waiting for replica acknowledgment due to slow replica processing, causing source to fall back to async mode.</li>
        <li><strong>Replication Lag:</strong> Large tables without PKs can cause hours or days of replication lag, making replicas unusable for reads.</li>
        <li><strong>Table Scan Avoidance:</strong> MySQL 8.0.18+ has optimizations (slave_rows_search_algorithms) but they are NOT sufficient - primary keys are still required!</li>
        <li><strong>Immediate Action Required:</strong> Add primary keys to ALL tables: <code style="background-color: #fff; padding: 2px 6px; border-radius: 3px;">ALTER TABLE table_name ADD PRIMARY KEY (column);</code></li>
    </ul>
    
    <div style="color: #721c24; font-weight: bold; margin-bottom: 10px;">Workaround Options:</div>
    <ul style="color: #721c24; margin: 0 0 15px 0; padding-left: 20px;">
        <li><strong>Option 1 (Best):</strong> Add a proper primary key based on business logic</li>
        <li><strong>Option 2 (Temporary):</strong> Add an invisible auto-increment column (MySQL 8.0.23+): <code style="background-color: #fff; padding: 2px 6px; border-radius: 3px;">ALTER TABLE t ADD COLUMN _pk BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE PRIMARY KEY;</code></li>
        <li><strong>Option 3 (Last Resort):</strong> Use a unique index as a substitute, though not as effective as a true PK</li>
    </ul>
    
    <div style="color: #721c24; font-weight: bold; margin-bottom: 5px;">References:</div>
    <ul style="color: #721c24; margin: 0 0 15px 0; padding-left: 20px;">
        <li><a href="https://dev.mysql.com/doc/refman/8.0/en/table-scan-avoidance.html" target="_blank" style="color: #721c24;">MySQL: Replication and Table Scan Avoidance</a></li>
        <li><a href="https://dev.mysql.com/doc/mysql-replication-excerpt/8.0/en/replication-features-row-searches.html" target="_blank" style="color: #721c24;">MySQL: Row-Based Replication Row Searches</a></li>
        <li><a href="https://jfg-mysql.blogspot.com/2017/08/danger-no-pk-with-RBR-and-mariadb-protection.html" target="_blank" style="color: #721c24;">Jean-François Gagné: Danger of No PK with RBR</a></li>
        <li><a href="https://www.percona.com/blog/attack-no-pk-replication-lag-with-mysql-percona-server-8-invisible-columns/" target="_blank" style="color: #721c24;">Percona: Attack No-PK Replication Lag with Invisible Columns</a></li>
    </ul>
    
    <div style="color: #721c24; font-weight: bold; margin-bottom: 10px;">Prevention (MySQL 8.0.13+):</div>
    <ul style="color: #721c24; margin: 0 0 15px 0; padding-left: 20px;">
        <li><strong>Require PKs:</strong> Set <code style="background-color: #fff; padding: 2px 6px; border-radius: 3px;">sql_require_primary_key=ON</code> to prevent creating tables without PKs (MySQL 8.0.13+)</li>
        <li><strong>Auto-Generate PKs:</strong> Set <code style="background-color: #fff; padding: 2px 6px; border-radius: 3px;">sql_generate_invisible_primary_key=ON</code> to automatically add invisible PKs (MySQL 8.0.30+)</li>
        <li><strong>Current Settings:</strong> sql_require_primary_key=0, sql_generate_invisible_primary_key=0</li>
    </ul>
    
    <div style="color: #721c24; font-weight: bold; margin-bottom: 5px;">References:</div>
    <ul style="color: #721c24; margin: 0; padding-left: 20px;">
        <li><a href="https://dev.mysql.com/doc/refman/8.0/en/create-table-gipks.html" target="_blank" style="color: #721c24;">MySQL: Generated Invisible Primary Keys (GIPKs)</a></li>
        <li><a href="https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_sql_generate_invisible_primary_key" target="_blank" style="color: #721c24;">MySQL: sql_generate_invisible_primary_key</a></li>
        <li><a href="https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_sql_require_primary_key" target="_blank" style="color: #721c24;">MySQL: sql_require_primary_key</a></li>
        <li><a href="https://dev.mysql.com/doc/refman/8.0/en/table-scan-avoidance.html" target="_blank" style="color: #721c24;">MySQL: Replication and Table Scan Avoidance</a></li>
        <li><a href="https://dev.mysql.com/doc/mysql-replication-excerpt/8.0/en/replication-features-row-searches.html" target="_blank" style="color: #721c24;">MySQL: Row-Based Replication Row Searches</a></li>
        <li><a href="https://jfg-mysql.blogspot.com/2017/08/danger-no-pk-with-RBR-and-mariadb-protection.html" target="_blank" style="color: #721c24;">Jean-François Gagné: Danger of No PK with RBR</a></li>
        <li><a href="https://www.percona.com/blog/attack-no-pk-replication-lag-with-mysql-percona-server-8-invisible-columns/" target="_blank" style="color: #721c24;">Percona: Attack No-PK Replication Lag with Invisible Columns</a></li>
        <li><a href="https://www.percona.com/blog/explore-the-new-feature-of-mysql-to-restrict-users-from-creating-a-table-without-a-primary-key/" target="_blank" style="color: #721c24;">Percona: Restrict Tables Without Primary Keys</a></li>
        <li><a href="https://www.percona.com/blog/tuning-innodb-primary-keys/" target="_blank" style="color: #721c24;">Percona: Tuning InnoDB Primary Keys</a></li>
        <li><a href="https://www.percona.com/blog/illustrating-primary-key-models-in-innodb-and-their-impact-on-disk-usage/" target="_blank" style="color: #721c24;">Percona: Primary Key Models and Disk Usage Impact</a></li>
    </ul>
</div>'''
        
        # Critical header
        critical_header = f'''
<div style="background-color: #721c24; color: white; padding: 10px 15px; border-radius: 4px 4px 0 0; margin-bottom: 0; font-weight: bold;">
    [CRITICAL] CRITICAL: Found {table_count} table{"s" if table_count != 1 else ""} without primary keys!
</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {critical_header}
    {info_box_html}
    {table_html}
</div>'''
    
    def _generate_unused_indexes_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Unused Indexes subsection with warning note when indexes found."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No data available</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        # Handle no data case - no unused indexes
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Unused indexes found - build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        data_rows = []
        for row in limited_rows:
            cells = []
            for i, col in enumerate(columns):
                if i < len(row):
                    cells.append(f'<td>{self._format_cell_value(row[i])}</td>')
                else:
                    cells.append('<td></td>')
            data_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {chr(10).join(data_rows)}
    </tbody>
</table>'''
        
        # Add truncation notice if needed
        truncation_html = ''
        if was_truncated:
            truncation_html = f'''
<div class="truncation-notice">
    {self._limit_enforcer.get_truncation_message(total_count)}
</div>'''
        
        # Warning note for safe index removal
        warning_html = '''
<div style="background-color: #fff3cd; border: 1px solid #ffeeba; border-radius: 4px; padding: 15px; margin-top: 15px;">
    <div style="color: #856404; font-weight: bold; margin-bottom: 10px;">⚠️ Important - Safe Index Removal Process:</div>
    <p style="color: #856404; margin: 0 0 10px 0;"><strong>DO NOT drop indexes immediately!</strong> Indexes may be used infrequently (daily/weekly/monthly reports, batch jobs, etc.).</p>
    
    <div style="color: #856404; font-weight: bold; margin-bottom: 5px;">Recommended approach (MySQL 8.0+):</div>
    <ol style="color: #856404; margin: 0 0 10px 0; padding-left: 20px;">
        <li>Make the index INVISIBLE first: <code style="background-color: #fff; padding: 2px 6px; border-radius: 3px;">ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE;</code></li>
        <li>Monitor for several weeks/months to ensure no queries break</li>
        <li>If no issues, drop the index: <code style="background-color: #fff; padding: 2px 6px; border-radius: 3px;">ALTER TABLE schema.table DROP INDEX index_name;</code></li>
        <li>If queries break, make visible again: <code style="background-color: #fff; padding: 2px 6px; border-radius: 3px;">ALTER TABLE schema.table ALTER INDEX index_name VISIBLE;</code></li>
    </ol>
    
    <p style="color: #856404; margin: 0;"><strong>Why:</strong> Invisible indexes are maintained by MySQL but not used by the optimizer. This allows safe testing without the risk of losing the index structure. Available in MySQL 8.0+ and Aurora MySQL 2.x+.</p>
</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
    {truncation_html}
    {warning_html}
</div>'''
    
    def _generate_locked_accounts_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Locked Accounts subsection with locked_connects warning."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No data available</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Add note if provided
        note_html = ''
        if subsection.note:
            note_html = f'''
    <div class="info">{subsection.note}</div>'''
        
        # Handle no data case - no locked accounts
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        data_rows = []
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                if i < len(row):
                    cells.append(f'<td>{self._format_cell_value(row[i])}</td>')
                else:
                    cells.append('<td></td>')
            data_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {chr(10).join(data_rows)}
    </tbody>
</table>'''
        
        # Check locked_connects status and add warning if > 0
        warning_html = ''
        if 'locked_connects_status' in parsed_logs:
            status_data = parsed_logs['locked_connects_status']
            if status_data.success and status_data.rows:
                for row in status_data.rows:
                    if len(row) >= 2 and str(row[0]).lower() == 'locked_connects':
                        try:
                            locked_count = int(row[1])
                            if locked_count > 0:
                                warning_html = f'''
<div style="background-color: #fff3cd; border: 1px solid #ffeeba; border-radius: 4px; padding: 10px; margin-top: 10px;">
    <span style="color: #856404;">⚠️ The number of attempts to connect to locked user accounts - <strong>{locked_count:,}</strong></span>
</div>'''
                        except (ValueError, TypeError):
                            pass
                        break
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
    {warning_html}
</div>'''
    
    def _generate_ssl_config_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for SSL Configuration subsection with require_secure_transport warning."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">No data available</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        # Check for require_secure_transport value while building rows
        require_secure_transport_off = False
        data_rows = []
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                if i < len(row):
                    cells.append(f'<td>{self._format_cell_value(row[i])}</td>')
                else:
                    cells.append('<td></td>')
            data_rows.append(f'<tr>{" ".join(cells)}</tr>')
            
            # Check if this row is require_secure_transport and if it's OFF
            if len(row) >= 2:
                var_name = str(row[0]).lower() if row[0] else ''
                var_value = str(row[1]).upper() if row[1] else ''
                if var_name == 'require_secure_transport' and var_value == 'OFF':
                    require_secure_transport_off = True
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {chr(10).join(data_rows)}
    </tbody>
</table>'''
        
        # Add warning if require_secure_transport is OFF
        warning_html = ''
        if require_secure_transport_off:
            warning_html = '''
<div style="background-color: #fff3cd; border: 1px solid #ffeeba; border-radius: 4px; padding: 10px; margin-top: 10px;">
    <span style="color: #856404;">⚠️ Enable <code>require_secure_transport=ON</code> to enforce SSL/TLS for all connections. This prevents unencrypted connections that could expose sensitive data.</span>
</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {table_html}
    {warning_html}
</div>'''
    
    def _generate_ssl_connection_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for SSL Connection Status subsection with metric cards."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Extract values from the first row
        row = log_data.rows[0]
        total_connections = row[0] if len(row) > 0 else 0
        ssl_connections = row[1] if len(row) > 1 else 0
        non_ssl_connections = row[2] if len(row) > 2 else 0
        
        # Format values
        total_str = f"{int(total_connections):,}" if total_connections is not None else "0"
        ssl_str = f"{int(ssl_connections):,}" if ssl_connections is not None else "0"
        non_ssl_str = f"{int(non_ssl_connections):,}" if non_ssl_connections is not None else "0"
        
        # Generate metric cards HTML
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="metric-cards">
        <div class="metric-card">
            <div class="metric-value">{total_str}</div>
            <div class="metric-label">Total Connections</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{ssl_str}</div>
            <div class="metric-label">SSL/TLS Connections</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{non_ssl_str}</div>
            <div class="metric-label">Non-SSL Connections</div>
        </div>
    </div>
</div>'''
    
    def _generate_aurora_memory_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Aurora Memory Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        body_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    cell_value = row[col_idx]
                    cells.append(f'<td>{self._escape_html(str(cell_value) if cell_value is not None else "")}</td>')
                else:
                    cells.append('<td></td>')
            body_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {"".join(body_rows)}
    </tbody>
</table>'''
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check aurora_oom_status
        oom_status = values.get('aurora_oom_status', '')
        if oom_status == 'RESERVED':
            recommendations.append('⚠️ <strong>Memory pressure detected:</strong> aurora_oom_status is RESERVED. OOM avoidance is active. Review long-running queries and connections. Consider scaling up instance class.')
        
        # Check Aurora_reserved_mem_exceeded_incidents
        incidents = values.get('Aurora_reserved_mem_exceeded_incidents', '0')
        try:
            incidents_count = int(incidents)
            if incidents_count > 0:
                recommendations.append(f'⚠️ <strong>Memory limits exceeded:</strong> Aurora_reserved_mem_exceeded_incidents = {incidents_count}. Review peak usage times, check for connection leaks, and consider larger instance.')
        except (ValueError, TypeError):
            pass
        
        # Check aurora_oom_avoidance_recovery_state
        recovery_state = values.get('aurora_oom_avoidance_recovery_state', '')
        if recovery_state == 'ACTIVE':
            recommendations.append('⚠️ <strong>OOM recovery active:</strong> aurora_oom_avoidance_recovery_state is ACTIVE. The system is actively recovering from memory pressure.')
        
        # Check Aurora_lockmgr_memory_used (warn if > 100MB)
        lockmgr_mem = values.get('Aurora_lockmgr_memory_used', '0')
        try:
            lockmgr_bytes = int(lockmgr_mem)
            if lockmgr_bytes > 100 * 1024 * 1024:  # 100MB
                lockmgr_mb = lockmgr_bytes / (1024 * 1024)
                recommendations.append(f'⚠️ <strong>High lock manager memory:</strong> Aurora_lockmgr_memory_used = {lockmgr_mb:.1f} MB. Check for long-running transactions or lock contention.')
        except (ValueError, TypeError):
            pass
        
        # Check aurora_temptable_ram_allocation (warn if > 1GB)
        temptable_mem = values.get('aurora_temptable_ram_allocation', '0')
        try:
            temptable_bytes = int(temptable_mem)
            if temptable_bytes > 1024 * 1024 * 1024:  # 1GB
                temptable_gb = temptable_bytes / (1024 * 1024 * 1024)
                recommendations.append(f'⚠️ <strong>High temporary table memory:</strong> aurora_temptable_ram_allocation = {temptable_gb:.2f} GB. Review queries creating temp tables and optimize or add indexes.')
        except (ValueError, TypeError):
            pass
        
        # Build recommendations HTML
        recommendations_html = ''
        if recommendations:
            recommendations_html = f'''
<div class="warning" style="margin-top: 10px;">
    <strong>Recommendations:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        {"".join(f'<li>{rec}</li>' for rec in recommendations)}
    </ul>
</div>'''
        else:
            recommendations_html = '''
<div class="success" style="margin-top: 10px;">
    ✓ All Aurora memory metrics are within normal ranges.
</div>'''
        
        # Add note
        note_html = ''
        if subsection.note:
            note_html = f'''
<div class="info">{self._escape_html(subsection.note)}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
    {recommendations_html}
</div>'''
    
    def _generate_aurora_pq_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Aurora Parallel Query Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        body_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    cell_value = row[col_idx]
                    cells.append(f'<td>{self._escape_html(str(cell_value) if cell_value is not None else "")}</td>')
                else:
                    cells.append('<td></td>')
            body_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {"".join(body_rows)}
    </tbody>
</table>'''
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check Aurora_pq_request_throttled
        throttled = values.get('Aurora_pq_request_throttled', '0')
        try:
            throttled_count = int(throttled)
            if throttled_count > 0:
                recommendations.append(f'⚠️ <strong>PQ throttling detected:</strong> Aurora_pq_request_throttled = {throttled_count}. Parallel query concurrency limit reached. Consider scaling to a larger instance class for more concurrent PQ sessions.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_failed
        failed = values.get('Aurora_pq_request_failed', '0')
        try:
            failed_count = int(failed)
            if failed_count > 0:
                recommendations.append(f'⚠️ <strong>PQ failures detected:</strong> Aurora_pq_request_failed = {failed_count}. Check storage layer health and query complexity.')
        except (ValueError, TypeError):
            pass
        
        # Calculate PQ success rate
        attempted = values.get('Aurora_pq_request_attempted', '0')
        executed = values.get('Aurora_pq_request_executed', '0')
        try:
            attempted_count = int(attempted)
            executed_count = int(executed)
            if attempted_count > 0:
                success_rate = (executed_count / attempted_count) * 100
                if success_rate < 70:
                    recommendations.append(f'⚠️ <strong>Low PQ success rate:</strong> {success_rate:.1f}% ({executed_count}/{attempted_count}). Many queries are not using parallel query. Review query patterns and table sizes.')
                elif success_rate < 90:
                    recommendations.append(f'ℹ️ <strong>PQ success rate:</strong> {success_rate:.1f}% ({executed_count}/{attempted_count}). Some queries are not using parallel query.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_not_chosen_long_trx
        long_trx = values.get('Aurora_pq_request_not_chosen_long_trx', '0')
        try:
            long_trx_count = int(long_trx)
            if long_trx_count > 0:
                recommendations.append(f'ℹ️ <strong>Long transactions affecting PQ:</strong> Aurora_pq_request_not_chosen_long_trx = {long_trx_count}. Long-running transactions prevent parallel query. Review transaction management.')
        except (ValueError, TypeError):
            pass
        
        # Build recommendations HTML
        recommendations_html = ''
        if recommendations:
            recommendations_html = f'''
<div class="warning" style="margin-top: 10px;">
    <strong>Analysis:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        {"".join(f'<li>{rec}</li>' for rec in recommendations)}
    </ul>
</div>'''
        else:
            # Check if PQ is being used at all
            try:
                attempted_count = int(values.get('Aurora_pq_request_attempted', '0'))
                if attempted_count > 0:
                    recommendations_html = '''
<div class="success" style="margin-top: 10px;">
    ✓ Aurora parallel query is operating normally with no issues detected.
</div>'''
                else:
                    recommendations_html = '''
<div class="info" style="margin-top: 10px;">
    ℹ️ No parallel query requests have been attempted. This may be normal if parallel query is disabled or workload doesn't benefit from it.
</div>'''
            except (ValueError, TypeError):
                pass
        
        # Add note
        note_html = ''
        if subsection.note:
            note_html = f'''
<div class="info">{self._escape_html(subsection.note)}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
    {recommendations_html}
</div>'''
    
    def _generate_aurora_relay_log_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate HTML for Aurora Relay Log Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="error-message">Query failed: {self._escape_html(log_data.error or "Unknown error")}</div>
</div>'''
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    <div class="no-data">{self._escape_html(no_data_message)}</div>
</div>'''
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_cells = [f'<th>{self._escape_html(str(col))}</th>' for col in columns]
        header_row = f'<tr>{" ".join(header_cells)}</tr>'
        
        body_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    cell_value = row[col_idx]
                    cells.append(f'<td>{self._escape_html(str(cell_value) if cell_value is not None else "")}</td>')
                else:
                    cells.append('<td></td>')
            body_rows.append(f'<tr>{" ".join(cells)}</tr>')
        
        table_html = f'''
<table class="data-table">
    <thead>{header_row}</thead>
    <tbody>
        {"".join(body_rows)}
    </tbody>
</table>'''
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check Aurora_in_memory_relaylog_status
        relay_status = values.get('Aurora_in_memory_relaylog_status', '')
        disabled_reason = values.get('Aurora_in_memory_relaylog_disabled_reason', '')
        if relay_status.upper() == 'DISABLED':
            reason_text = f' Reason: {disabled_reason}.' if disabled_reason else ''
            recommendations.append(f'⚠️ <strong>In-memory relay log disabled:</strong>{reason_text} This may impact replica performance. Consider enabling if supported.')
        
        # Check Aurora_in_memory_relaylog_fallback_count
        fallback_count = values.get('Aurora_in_memory_relaylog_fallback_count', '0')
        try:
            fallback_int = int(fallback_count)
            if fallback_int > 0:
                recommendations.append(f'ℹ️ <strong>Relay log fallbacks:</strong> Aurora_in_memory_relaylog_fallback_count = {fallback_int}. Large transactions caused fallback to persistent storage. Consider breaking up large transactions.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_in_memory_relaylog_recovery_count
        recovery_count = values.get('Aurora_in_memory_relaylog_recovery_count', '0')
        try:
            recovery_int = int(recovery_count)
            if recovery_int > 0:
                recommendations.append(f'ℹ️ <strong>Relay log recoveries:</strong> Aurora_in_memory_relaylog_recovery_count = {recovery_int}. Relay log recovered from fallback state {recovery_int} times.')
        except (ValueError, TypeError):
            pass
        
        # Build recommendations HTML
        recommendations_html = ''
        if recommendations:
            recommendations_html = f'''
<div class="warning" style="margin-top: 10px;">
    <strong>Analysis:</strong>
    <ul style="margin-top: 5px; margin-bottom: 0;">
        {"".join(f'<li>{rec}</li>' for rec in recommendations)}
    </ul>
</div>'''
        else:
            if relay_status.upper() == 'ENABLED':
                recommendations_html = '''
<div class="success" style="margin-top: 10px;">
    ✓ Aurora in-memory relay log is enabled and operating normally.
</div>'''
        
        # Add note
        note_html = ''
        if subsection.note:
            note_html = f'''
<div class="info">{self._escape_html(subsection.note)}</div>'''
        
        return f'''
<div class="subsection" id="{self._emit_subsection_anchor_id(subsection.name)}">
    <h3>{self._escape_html(subsection.name)}</h3>
    {note_html}
    {table_html}
    {recommendations_html}
</div>'''
    
    def _build_purge_thread_activity_html(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build HTML for Purge Thread Activity table."""
        if 'purge_thread_activity' not in parsed_logs:
            return '<h4>Purge Thread Activity</h4><div class="no-data">Purge thread information not available</div>'
        
        log_data = parsed_logs['purge_thread_activity']
        if not log_data.success or not log_data.rows or not log_data.columns:
            return '<h4>Purge Thread Activity</h4><div class="no-data">Purge thread information not available</div>'
        
        # Build table
        headers = ['Thread ID', 'Thread Name', 'Thread Type', 'State']
        header_html = ''.join(f'<th>{self._escape_html(h)}</th>' for h in headers)
        
        rows_html = ''
        for row in log_data.rows:
            cells = ''.join(f'<td>{self._escape_html(str(cell) if cell is not None else "")}</td>' for cell in row)
            rows_html += f'<tr>{cells}</tr>'
        
        return f'''
<h4>Purge Thread Activity</h4>
<p style="color: #666; font-size: 0.9em;">Shows current purge thread status from Performance Schema.</p>
<table class="data-table">
    <thead><tr>{header_html}</tr></thead>
    <tbody>{rows_html}</tbody>
</table>'''
    
    def _build_purge_configuration_html(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build HTML for Purge Configuration table."""
        if 'purge_configuration' not in parsed_logs:
            return '<h4>Purge Configuration</h4><div class="no-data">Purge configuration not available</div>'
        
        log_data = parsed_logs['purge_configuration']
        if not log_data.success or not log_data.rows or not log_data.columns:
            return '<h4>Purge Configuration</h4><div class="no-data">Purge configuration not available</div>'
        
        # Build table
        headers = ['Parameter', 'Value', 'Recommendation']
        header_html = ''.join(f'<th>{self._escape_html(h)}</th>' for h in headers)
        
        rows_html = ''
        for row in log_data.rows:
            cells = ''.join(f'<td>{self._escape_html(str(cell) if cell is not None else "")}</td>' for cell in row)
            rows_html += f'<tr>{cells}</tr>'
        
        return f'''
<h4>Purge Configuration</h4>
<p style="color: #666; font-size: 0.9em;">Current purge-related server variables.</p>
<table class="data-table">
    <thead><tr>{header_html}</tr></thead>
    <tbody>{rows_html}</tbody>
</table>'''
    
    def _build_purge_performance_metrics_html(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build HTML for Purge Performance Metrics."""
        if 'purge_performance_metrics' not in parsed_logs:
            return '<h4>Purge Performance Metrics</h4><div class="no-data">Purge performance metrics not available</div>'
        
        log_data = parsed_logs['purge_performance_metrics']
        if not log_data.success or not log_data.rows or not log_data.columns:
            return '<h4>Purge Performance Metrics</h4><div class="no-data">Purge performance metrics not available</div>'
        
        row = log_data.rows[0]
        purge_threads = row[0] if len(row) > 0 else 'N/A'
        undo_tablespaces = row[1] if len(row) > 1 else 'N/A'
        active_undo_tablespaces = row[2] if len(row) > 2 else 'N/A'
        
        return f'''
<h4>Purge Performance Metrics</h4>
<p style="color: #666; font-size: 0.9em;">Key metrics evaluating purge health and performance.</p>
<div style="display: flex; gap: 20px; margin-top: 10px;">
    <div style="text-align: center; padding: 15px; background: #f8f9fa; border-radius: 4px; min-width: 120px;">
        <div style="font-size: 24px; font-weight: bold; color: #333;">{purge_threads}</div>
        <div style="font-size: 12px; color: #666;">Purge Threads</div>
    </div>
    <div style="text-align: center; padding: 15px; background: #f8f9fa; border-radius: 4px; min-width: 120px;">
        <div style="font-size: 24px; font-weight: bold; color: #333;">{undo_tablespaces}</div>
        <div style="font-size: 12px; color: #666;">Undo Tablespaces</div>
    </div>
    <div style="text-align: center; padding: 15px; background: #f8f9fa; border-radius: 4px; min-width: 120px;">
        <div style="font-size: 24px; font-weight: bold; color: #333;">{active_undo_tablespaces}</div>
        <div style="font-size: 12px; color: #666;">Active Undo Tablespaces</div>
    </div>
</div>'''
    
    def _resolve_columns(
        self,
        columns: List[Union[str, 'ComputedColumn']],
        column_map: Dict[str, int]
    ) -> List[Tuple[str, Optional[str]]]:
        """
        Resolve column definitions to (display_name, source) tuples.
        
        Returns list of tuples where:
        - display_name: The column header to show
        - source: Either a column name or an expression string (prefixed with 'expr:')
        """
        # Import here to avoid circular imports
        from diagnostic_collector_mysql.models import ComputedColumn
        
        resolved = []
        for col in columns:
            if isinstance(col, str):
                # Simple column reference
                if col in column_map:
                    resolved.append((col, col))
            elif isinstance(col, ComputedColumn):
                # Computed column
                resolved.append((col.name, f'expr:{col.expr}'))
            elif isinstance(col, dict):
                # Dict format from JSON parsing
                if 'name' in col and 'expr' in col:
                    resolved.append((col['name'], f"expr:{col['expr']}"))
        
        return resolved
    
    def _get_cell_value(
        self,
        source: Optional[str],
        row: Tuple[Any, ...],
        column_map: Dict[str, int],
        row_dict: Dict[str, Any]
    ) -> Any:
        """Get the value for a cell, handling both direct columns and expressions."""
        if source is None:
            return None
        
        if source.startswith('expr:'):
            # Evaluate expression
            expr = source[5:]
            return self._evaluate_expression(expr, row_dict)
        else:
            # Direct column reference
            if source in column_map:
                idx = column_map[source]
                return row[idx] if idx < len(row) else None
            return None
    
    def _evaluate_expression(self, expr: str, row_dict: Dict[str, Any]) -> Optional[float]:
        """
        Evaluate a computed column expression.
        
        Uses caching to avoid re-parsing the same expression multiple times.
        """
        if expr not in self._expression_cache:
            self._expression_cache[expr] = ExpressionEvaluator(expr)
        
        return self._expression_cache[expr].evaluate(row_dict)
    
    def _check_condition(
        self,
        condition: Optional[Dict[str, List[str]]],
        engine_type: Optional[str],
        min_engine_version: Optional[str] = None,
        max_engine_version: Optional[str] = None
    ) -> bool:
        """
        Check if a subsection condition is met.

        Args:
            condition: Engine condition dict (e.g., {"engine": ["mysql"]})
            engine_type: Current database engine type
            min_engine_version: Minimum version required (empty string = no limit)
            max_engine_version: Maximum version allowed (empty string = no limit)

        Returns:
            True if all conditions are met, False otherwise
        """
        # Check engine condition
        if condition is not None:
            if 'engine' in condition and engine_type:
                allowed_engines = condition['engine']
                if engine_type not in allowed_engines:
                    return False

        # Check version conditions
        if min_engine_version is not None and max_engine_version is not None:
            # Both must be present (validated in input_validator)
            # Empty string means no limit
            if min_engine_version:
                min_version = self._parse_version(min_engine_version)
                if self._database_version < min_version:
                    return False

            if max_engine_version:
                max_version = self._parse_version(max_engine_version)
                if self._database_version > max_version:
                    return False

        return True
    
    def _get_section_overview(self, section_name: str) -> str:
        """
        Get the overview text for a section.
        
        Include Section_Overview at the start of each section
        """
        # Default overviews for common sections
        overviews = {
            'Server Information & Version Details': 'Server configuration and status information including version, uptime, and crucial configurations.',
            'Non-Default Configuration Variables': 'Variables that differ from their compiled defaults.',
            'Replication Topology & Status': 'Replication configuration, status, and lag information.',
            'Performance Metrics': 'Performance metrics reveal system load, query patterns, and potential bottlenecks. High slow query counts, connection issues, or table cache misses indicate areas needing optimization.',
            'Memory Configuration & Usage - Comprehensive Analysis': 'Memory configuration affects performance dramatically. The InnoDB buffer pool should be 70-80% of available RAM. Buffer pool hit rate >99% is ideal. Per-connection buffers multiply by max_connections, so should be configured appropriately. This section provides comprehensive memory analysis using Performance Schema instrumentation.',
            'InnoDB Engine & Redo Log': 'InnoDB redo logs record all changes for crash recovery. If redo logs completely utilized, writes operations will be stalled. History list length indicates purge lag - high values cause table bloat and slow queries. Monitor these closely!',
            'Table Analysis': 'Table statistics, sizes, and potential issues.',
            'Index Analysis': 'Index usage, redundant indexes, and optimization opportunities.',
            'Security Configuration': 'User accounts, privileges, and security configuration.',
            'Connection Management': 'Connection limits prevent new connections when reached. High connection churn indicates connection pooling issues. Monitor to prevent "Too many connections" errors.',
            'Lock Analysis': 'Lock waits, deadlocks, and contention metrics.',
            'I/O Analysis': 'I/O operations are typically the slowest part of database operations. Identifying tables and files with high I/O helps pinpoint performance bottlenecks and guides optimization efforts.',
            'Database Objects': 'Triggers, events, and stored procedures can impact performance and complicate troubleshooting. Understanding what automated processes are running helps diagnose unexpected behavior.',
            'SQL Performance': 'Query performance analysis and problematic queries.',
            'Database User Activity': 'User activity and connection patterns.',
        }
        
        return overviews.get(section_name, f'Information about {section_name}.')
    
    def _make_section_id(self, name: str) -> str:
        """Convert a section name to a short HTML ID matching reference format."""
        # Map section names to short IDs
        short_ids = {
            'Server Information & Version Details': 'server',
            'Non-Default Configuration Variables': 'non-defaults',
            'Replication Topology & Status': 'replication',
            'Performance Metrics': 'performance',
            'Memory Configuration & Usage - Comprehensive Analysis': 'memory',
            'InnoDB Engine & Redo Log': 'innodb-redo',
            'Table Analysis': 'tables',
            'Index Analysis': 'indexes',
            'Security Configuration': 'security',
            'Connection Management': 'connections',
            'Lock Analysis': 'locks',
            'I/O Analysis': 'io-analysis',
            'Database Objects': 'objects',
            'SQL Performance': 'sql-performance',
            'Database User Activity': 'user-activity',
        }
        
        if name in short_ids:
            return short_ids[name]
        
        # Fallback: Replace spaces and special chars with hyphens, lowercase
        section_id = name.lower()
        section_id = re.sub(r'[^a-z0-9]+', '-', section_id)
        section_id = section_id.strip('-')
        return section_id
    
    def _get_subsection_anchor_id(self, name: str) -> str:
        """Convert a subsection name to a valid HTML anchor ID."""
        # Map common subsection names to short IDs for cross-referencing
        short_ids = {
            'Active Transactions': 'active-transactions',
            'Long-Running Idle Transactions (>5 min, no query)': 'long-running-idle-trx',
            'Long-Running Active Transactions (>1 min, with query)': 'long-running-active-trx',
            'History List Length': 'history-list-length',
            'InnoDB Purge Analysis': 'innodb-purge-analysis',
        }
        
        if name in short_ids:
            return short_ids[name]
        
        # Fallback: Replace spaces and special chars with hyphens, lowercase
        anchor_id = name.lower()
        anchor_id = re.sub(r'[^a-z0-9]+', '-', anchor_id)
        anchor_id = anchor_id.strip('-')
        return anchor_id
    
    def _emit_subsection_anchor_id(self, name: str) -> str:
        """
        Anchor ID for a subsection about to be written into the document.

        Two distinct subsections can slug to the same anchor, because the slug
        collapses runs of non-alphanumeric characters: "Long-Running Transactions" in
        the InnoDB section and "Long Running Transactions" in Connection Management
        both yield `long-running-transactions`. Duplicate IDs are invalid HTML and a
        fragment jump silently resolves to whichever came first.

        The first emission keeps the plain slug, so a link built by
        _get_health_check_link still resolves, and later collisions are suffixed.
        """
        base = self._get_subsection_anchor_id(name)
        if not hasattr(self, '_emitted_anchor_ids'):
            self._emitted_anchor_ids = set()
        if base not in self._emitted_anchor_ids:
            self._emitted_anchor_ids.add(base)
            return base
        suffix = 2
        while f'{base}-{suffix}' in self._emitted_anchor_ids:
            suffix += 1
        unique = f'{base}-{suffix}'
        self._emitted_anchor_ids.add(unique)
        return unique
    
    def _get_health_check_link(self, check_name: str) -> str:
        """
        Get HTML for a health check name, with hyperlink to related subsection if available.
        
        Maps health check names to their corresponding report subsections.
        """
        # Mapping from health check names to subsection names
        check_to_subsection = {
            'Redo Log Occupancy': 'Redo Log Occupancy',
            'Tables Without Primary Key': 'Tables Without Primary Key',
            'Empty Passwords': 'Accounts with Empty Passwords',
            'Connection Usage': 'Connection Usage',
            'History List Length': 'History List Length',
            'Buffer Pool Hit Rate': 'Buffer Pool Hit Rate',
            'Replication Lag': 'Replica Status',
            'Replication Errors': 'Replica Status',
            'Auto-Increment >90% Full': 'Auto Increment Usage (>50%)',
            'Auto-Increment Usage': 'Auto Increment Usage (>50%)',
            'Server Configuration': 'Configuration Warnings',
            'Disk Tmp Table %': 'Temporary Table Usage',
            'Anonymous Accounts': 'Anonymous Accounts',
            'Wildcard Host Accounts': 'Accounts with Wildcard Hosts',
            'Long-Running Queries': 'Long Running Queries',
            'Long-Running Transactions': 'Long-Running Transactions',
            'Deadlocks (since restart)': 'Deadlock Count',
            'Deadlocks': 'Deadlock Count',
            'Unused Indexes': 'Unused Indexes',
            'FK Without Indexes': 'Foreign Keys Without Indexes',
            'Non-InnoDB Tables': 'User Tables Not Using InnoDB',
            'Stale Statistics': 'Stale Statistics',
            'Instant DDL Row Versions': 'Instant DDL Column Tracking',
            'Elevated Privileges Users': 'Users with Elevated Privileges',
            'Idle Connections >5min': 'Long-Running Idle Connections (>5 min)',
            'Query Failures': None,  # No specific subsection for this
        }
        
        subsection_name = check_to_subsection.get(check_name)
        if subsection_name:
            anchor_id = self._get_subsection_anchor_id(subsection_name)
            return f'<a href="#{anchor_id}">{self._escape_html(check_name)}</a>'
        else:
            return self._escape_html(check_name)
    
    def _escape_html(self, text: Any) -> str:
        """Escape HTML special characters."""
        if text is None:
            return ''
        text = str(text)
        return (
            text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#x27;')
        )
    
    def _format_cell_value(self, value: Any) -> str:
        """Format a cell value for display."""
        if value is None:
            return '<span style="color: #a0aec0; font-style: italic;">NULL</span>'
        
        if isinstance(value, float):
            # Format floats with reasonable precision
            if value == int(value):
                return self._escape_html(str(int(value)))
            return self._escape_html(f'{value:.2f}')
        
        return self._escape_html(str(value))
    
    def _generate_footer(self) -> str:
        """Generate the report footer."""
        return f'''
<div class="footer">
    <p><strong>Diagnostic Collector for MySQL Report Complete</strong></p>
    <p>Generated at: {self._timestamp}</p>
    <p><em>Review all sections carefully. Address any CRITICAL or WARNING items promptly.</em></p>
    <p>Diagnostic Collector for MySQL v{__version__} | Compatible with RDS/Aurora MySQL, Community MySQL 8.0+ and RDS MariaDB, Community MariaDB 11.4+</p>
</div>'''
    
    def _generate_body_end(self) -> str:
        """Generate the closing body tag."""
        return '''
</body>'''
    
    def _generate_html_footer(self) -> str:
        """Generate the closing HTML tag."""
        return '</html>'



class MarkdownReportGenerator:
    """
    Generates Markdown reports from parsed query log data.
    
    This class creates a GitHub-flavored Markdown (GFM) document with:
    - Header with timestamp, database info, and table of contents
    - Health Overview section with critical issues and warnings
    - Sections organized per report_format.json
    - Each section has Section_Overview and subsections
    - Tables using Markdown table syntax (| col1 | col2 |)
    - Consistent formatting compatible with GitHub rendering
    
    Responsibilities:
    - Generate Markdown report when --format markdown is specified
    - Include Health Overview section at the top
    - Organize report by sections defined in report_format.json
    - Include Section_Overview at the start of each section
    - Include table of contents with links to sections
    - Include timestamp and database information in report header
    """
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        extended_checks: bool = False
    ):
        """
        Initialize the MarkdownReportGenerator.

        Args:
            report_format: ReportFormat object defining the report structure
            collection_limit: Maximum rows per subsection (default: 50)
            timestamp: Report generation timestamp (default: current time)
            database_info: Dictionary with database information (host, version, etc.)
            extended_checks: Whether extended checks are enabled (default: False)
        """
        self._report_format = report_format
        self._limit_enforcer = CollectionLimitEnforcer(collection_limit)
        self._timestamp = timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._database_info = database_info or {}
        self._expression_cache: Dict[str, ExpressionEvaluator] = {}
        self._extended_checks = extended_checks
        # Parse database version for version filtering
        self._database_version = self._parse_version(self._database_info.get('version', ''))

    def _parse_version(self, version_string: str) -> tuple:
        """Parse version string into a tuple of integers for comparison."""
        if not version_string:
            return (0, 0, 0)
        match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', version_string)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0
            return (major, minor, patch)
        return (0, 0, 0)
    
    def _is_transaction_consumer_enabled(self, parsed_logs: Dict[str, 'ParsedLogFile']) -> bool:
        """Check if the events_transactions_current consumer is enabled in performance_schema."""
        consumer_status = parsed_logs.get('events_transactions_consumer_status')
        if consumer_status and consumer_status.success and consumer_status.rows:
            # Check if ENABLED column value is 'YES'
            if consumer_status.columns:
                try:
                    enabled_idx = consumer_status.columns.index('ENABLED')
                    return consumer_status.rows[0][enabled_idx].upper() == 'YES'
                except (ValueError, IndexError):
                    pass
        return False
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """
        Generate a complete Markdown report from parsed log data.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            engine_type: The detected database engine type (for condition filtering)
            
        Returns:
            Complete Markdown document as a string
        """
        md_parts = [
            self._generate_header(parsed_logs, engine_type),
            self._generate_table_of_contents(),
            self._generate_health_overview(parsed_logs, engine_type),
        ]
        
        # Generate each section from report_format
        for section in self._report_format.sections:
            section_md = self._generate_section(section, parsed_logs, engine_type)
            if section_md:
                md_parts.append(section_md)
        
        md_parts.append(self._generate_footer())
        
        return '\n'.join(md_parts)
    
    def _generate_header(self, parsed_logs: Optional[Dict[str, ParsedLogFile]] = None, engine_type: Optional[str] = None) -> str:
        """
        Generate the report header with timestamp and database info.
        
        Include timestamp and database information
        """
        host = self._database_info.get('host', 'Unknown')
        version = self._database_info.get('version', 'Unknown')
        engine_type = engine_type or self._database_info.get('engine_type', 'Unknown')
        
        # Helper to get value from parsed_logs
        def get_log_value(query_id: str, column_name: str) -> Optional[str]:
            if parsed_logs and query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return str(log_data.rows[0][col_idx])
                    except (ValueError, IndexError):
                        pass
            return None
        
        # Build header table rows
        rows = [
            f"| Host | {self._escape_markdown(host)} |",
            f"| Version | {self._escape_markdown(version)} |",
        ]
        
        # Add Aurora-specific fields if Aurora MySQL
        if engine_type == 'aurora-mysql':
            aurora_server_id = get_log_value('aurora_server_id', 'aurora_server_id')
            aurora_version = get_log_value('aurora_version', 'aurora_version')
            innodb_read_only = get_log_value('server_read_only', 'innodb_read_only')
            
            if aurora_server_id:
                rows.append(f"| Aurora Server ID | {self._escape_markdown(aurora_server_id)} |")
            if aurora_version:
                rows.append(f"| Aurora Version | {self._escape_markdown(aurora_version)} |")
            
            # Format read-only status
            if innodb_read_only in ('1', 'ON', 'True'):
                ro_status = "Yes (Reader)"
            else:
                ro_status = "No (Writer)"
            rows.append(f"| Read Only (InnoDB) | {ro_status} |")
        
        rows.append(f"| Engine | {self._escape_markdown(engine_type)} |")
        
        rows_str = '\n'.join(rows)
        
        return f"""# Diagnostic Collector for MySQL Health Report

**Generated:** {self._timestamp}

| Property | Value |
|----------|-------|
{rows_str}

---
"""
    
    def _generate_table_of_contents(self) -> str:
        """
        Generate the table of contents with anchor links to sections.
        
        Include table of contents with links to sections
        """
        toc_items = ['- [Health Overview](#health-overview)']
        
        for section in self._report_format.sections:
            anchor = self._make_anchor(section.name)
            toc_items.append(f'- [{self._escape_markdown(section.name)}](#{anchor})')
        
        return f"""## Table of Contents

{chr(10).join(toc_items)}

---
"""
    
    def _generate_health_overview(self, parsed_logs: Dict[str, ParsedLogFile], engine_type: Optional[str] = None) -> str:
        """
        Generate the Health Overview section.
        
        Include Health Overview section with critical issues and warnings
        """
        # Use shared health check evaluation
        health_checks = self._evaluate_all_health_checks(parsed_logs)
        
        # Count by status
        critical_count = sum(1 for h in health_checks if h[0] == 'critical')
        warning_count = sum(1 for h in health_checks if h[0] == 'warning')
        ok_count = sum(1 for h in health_checks if h[0] == 'ok')
        
        # Determine server type
        engine_type = engine_type or self._database_info.get('engine_type', 'mysql')
        if engine_type == 'aurora-mysql':
            server_type = 'Aurora'
        elif engine_type == 'mariadb':
            server_type = 'MariaDB'
        else:
            server_type = 'MySQL'
        
        # Build health stats summary
        health_stats = f"""| Metric | Count |
|--------|-------|
| 🔴 Critical Issues | {critical_count} |
| 🟡 Warnings | {warning_count} |
| 🟢 OK | {ok_count} |
| 🖥️ Server Type | {server_type} |
"""
        
        # Build health checks table
        checks_table = """### Health Checks

| Status | Check | Details |
|--------|-------|---------|
"""
        # Add critical first, then warning, then ok
        for status, check_name, details in health_checks:
            if status == 'critical':
                checks_table += f"| 🔴 CRITICAL | {self._escape_markdown(check_name)} | {self._escape_markdown(details)} |\n"
        for status, check_name, details in health_checks:
            if status == 'warning':
                checks_table += f"| 🟡 WARNING | {self._escape_markdown(check_name)} | {self._escape_markdown(details)} |\n"
        for status, check_name, details in health_checks:
            if status == 'ok':
                checks_table += f"| 🟢 OK | {self._escape_markdown(check_name)} | {self._escape_markdown(details)} |\n"
        
        return f"""## Health Overview

{health_stats}
{checks_table}
---
"""
    
    def _evaluate_config_warnings(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> List[Tuple[str, str]]:
        """
        Evaluate configuration parameters and return warnings for problematic values.
        
        This method delegates to the module-level evaluate_config_warnings function.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            
        Returns:
            List of (Parameter, Message) tuples for triggered warnings.
            Empty list if no warnings or query data not available.
        """
        return evaluate_config_warnings(parsed_logs)
    
    def _evaluate_all_health_checks(self, parsed_logs: Dict[str, ParsedLogFile]) -> List[Tuple[str, str, str]]:
        """Evaluate all health checks - shared logic for Markdown generator."""
        return _evaluate_health_checks_common(parsed_logs, self._evaluate_config_warnings)
    
    def _generate_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> str:
        """
        Generate Markdown for a report section.
        
        Behavior:
        - Organize report by sections defined in report_format.json
        - Include Section_Overview at the start of each section
        """
        # Special handling for Replication section
        if section.name == "Replication Topology & Status":
            return self._generate_replication_section(section, parsed_logs, engine_type)
        
        # Generate subsections
        subsections_md = []
        has_data = False
        
        for subsection in section.subsections:
            # Check condition (engine and version filtering)
            if not self._check_condition(
                subsection.condition, 
                engine_type,
                subsection.min_engine_version,
                subsection.max_engine_version
            ):
                continue
            
            subsection_md = self._generate_subsection(subsection, parsed_logs)
            if subsection_md:
                subsections_md.append(subsection_md)
                has_data = True
        
        # Skip section if no subsections have data
        if not has_data:
            return ''
        
        # Generate section overview
        overview_text = self._get_section_overview(section.name)
        
        # Add engine-specific notes for certain sections
        engine_note_md = ''
        if section.name == 'I/O Analysis' and engine_type == 'aurora-mysql':
            engine_note_md = '\n> **Note:** Aurora MySQL does not instrument physical I/O (file-level bytes) the same way as Community MySQL due to its distributed storage architecture. However, logical I/O (table-level row operations) is fully instrumented and available.\n'
        
        return f"""## {self._escape_markdown(section.name)}

> {self._escape_markdown(overview_text)}
{engine_note_md}
{chr(10).join(subsections_md)}

---
"""
    
    def _generate_replication_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> str:
        """
        Generate Markdown for the Replication Topology & Status section with Binary Logging Configuration Advice.
        """
        # Generate subsections first
        subsections_md = []
        has_data = False
        
        for subsection in section.subsections:
            if not self._check_condition(
                subsection.condition, 
                engine_type,
                subsection.min_engine_version,
                subsection.max_engine_version
            ):
                continue
            
            subsection_md = self._generate_subsection(subsection, parsed_logs)
            if subsection_md:
                subsections_md.append(subsection_md)
                has_data = True
        
        if not has_data:
            return ''
        
        # Generate section overview
        overview_text = self._get_section_overview(section.name)
        
        # Generate Transaction Dependency Tracking section (only for MySQL < 8.1)
        dep_tracking_md = self._generate_transaction_dependency_tracking_md(parsed_logs)
        
        # Generate Binary Logging Configuration Advice
        logging_advice_md = self._generate_binary_logging_advice(parsed_logs, engine_type)
        
        return f"""## {self._escape_markdown(section.name)}

> {self._escape_markdown(overview_text)}

{chr(10).join(subsections_md)}

{dep_tracking_md}
{logging_advice_md}

---
"""
    
    def _generate_transaction_dependency_tracking_md(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate Transaction Dependency Tracking section in Markdown format.
        
        Only shown for MySQL < 8.1 (binlog_transaction_dependency_tracking was removed in 8.1+).
        """
        # Only show for MySQL < 8.1
        if self._database_version >= (8, 1, 0):
            return ''
        
        # Helper to get value from parsed logs
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log = parsed_logs[query_id]
                if log.success and log.rows and log.columns:
                    try:
                        idx = log.columns.index(column_name)
                        return log.rows[0][idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        # Get dependency tracking value
        dep_tracking = 'N/A'
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    if 'binlog_transaction_dependency_tracking' in log_data.columns:
                        idx = log_data.columns.index('binlog_transaction_dependency_tracking')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            dep_tracking = str(val)
                except (ValueError, IndexError, TypeError):
                    pass
        
        md = """### Transaction Dependency Tracking (Critical for Multi-Threaded Replication Performance!)

> **Understanding WRITESET:** Transaction dependency tracking determines which transactions can run in parallel on replicas. This is CRITICAL for multi-threaded replication performance!

#### Dependency Tracking Methods

| Method | Description | Performance | Use Case |
|--------|-------------|-------------|----------|
| COMMIT_ORDER | Uses binary log group commit. Transactions in same group commit can run in parallel. | Limited parallelism | Default, safe but slower |
| WRITESET | Tracks row hashes modified by each transaction. Transactions modifying different rows can run in parallel. | High parallelism | Recommended for most workloads |
| WRITESET_SESSION | Like WRITESET but preserves order within same session. | High parallelism | Best for applications needing session consistency |

#### Current Configuration Analysis

"""
        
        if dep_tracking == 'COMMIT_ORDER':
            md += f"""⚠️ **Dependency Tracking:** {dep_tracking}

**[WARNING] Recommendation:** Consider switching to WRITESET or WRITESET_SESSION for better replication parallelism. This can significantly reduce replication lag!

Set: `binlog_transaction_dependency_tracking=WRITESET` and `transaction_write_set_extraction=XXHASH64`

"""
        elif dep_tracking in ('WRITESET', 'WRITESET_SESSION'):
            md += f"""✅ **Dependency Tracking:** {dep_tracking}

Good configuration for multi-threaded replication parallelism.

"""
        else:
            md += f"""ℹ️ **Dependency Tracking:** {dep_tracking}

The `binlog_transaction_dependency_tracking` variable is not available or not configured.

"""
        
        md += """> **Key Insight:** WRITESET tracking allows transactions that modify different rows to execute in parallel on replicas, even if they were in different group commits on the source. This dramatically improves replication throughput and reduces lag.

> ⚠️ **Important:** The `binlog_transaction_dependency_tracking` setting must be configured on the **SOURCE/PRIMARY server**, not on replicas. Replicas read the dependency information from the binary logs generated by the source. Setting WRITESET on the source enables better parallelism on ALL downstream replicas.

"""
        
        return md
    
    def _generate_binary_logging_advice(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> str:
        """Generate Binary Logging Configuration Advice in Markdown format."""
        # Helper to get value from parsed logs
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log = parsed_logs[query_id]
                if log.success and log.rows and log.columns:
                    try:
                        idx = log.columns.index(column_name)
                        return log.rows[0][idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        # Get replication role
        replication_role = get_value('replication_role', 'replication_role', 'UNKNOWN')
        log_bin = get_value('replication_role', 'log_bin_enabled', 0)
        
        # Get sync settings
        sync_binlog = get_value('config_warnings_check', 'sync_binlog', 'N/A')
        flush_log = get_value('config_warnings_check', 'innodb_flush_log_at_trx_commit', 'N/A')
        
        # Build the advice section
        role_text = 'Source/Standalone' if replication_role != 'REPLICA' else 'Replica'
        binlog_text = 'Enabled' if log_bin in (1, '1', 'ON', True) else 'Disabled'
        binlog_rec = 'Binary logs should be enabled on source servers for replication and PITR.' if replication_role != 'REPLICA' else 'Binary logs optional on leaf replicas unless chaining.'
        
        sync_rec = 'Safest setting (ACID compliant). Every commit syncs binlog to disk.' if str(sync_binlog) == '1' else 'Consider sync_binlog=1 for durability.'
        flush_rec = 'Safest setting (ACID compliant). Every commit syncs redo log to disk.' if str(flush_log) == '1' else 'Consider innodb_flush_log_at_trx_commit=1 for durability.'
        
        advice_md = f"""### Binary Logging Configuration Advice

> **Binary Logging on Replicas:** Binary logs are NOT required on replicas unless the replica is also a source for another replica [chained replication] (or) binary logs data used for data streaming services (or) you need point-in-time recovery from the replica.

#### Current Server Role and Logging Recommendation

| Aspect | Status | Recommendation |
|--------|--------|----------------|
| Server Role | {role_text} | This is a {role_text.lower()} server |
| Binary Logging | {binlog_text} | {binlog_rec} |
| sync_binlog | {sync_binlog} | {sync_rec} |
| innodb_flush_log_at_trx_commit | {flush_log} | {flush_rec} |

#### Logging Best Practices

- **Source Servers:** Enable binary logging (log_bin=ON) with sync_binlog=1 for durability
- **Leaf Replicas:** Binary logging optional. Disable to save I/O unless needed for chaining or PITR
- **Intermediate Replicas:** Enable binary logging if this replica is a source for other replicas
- **ACID Compliance:** Use sync_binlog=1 AND innodb_flush_log_at_trx_commit=1 for full durability
- **Performance vs Durability:** Lower sync values improve performance but risk data loss on crash
"""
        
        # Check if general_log is enabled
        try:
            general_log_value = get_value('logging_status', 'general_log', 0)
            if general_log_value in (1, '1', 'ON', True):
                advice_md += """
> ⚠️ **Performance Warning:** The general query log (`general_log=ON`) logs ALL queries and can severely impact performance on busy systems. Use only for short-term debugging. The slow query log (`slow_query_log=ON`) has less impact but should still be used judiciously with appropriate `long_query_time` settings.
"""
        except Exception:
            pass
        
        return advice_md

    def _generate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for a report subsection with data table."""
        source_id = subsection.source
        
        # Check if subsection requires extended checks and they are disabled
        if subsection.requires_extended_checks and not self._extended_checks:
            # 3-tier fallback logic:
            # 1. If --extended-checks enabled -> Use innodb_trx query (handled above, we're in the disabled case)
            # 2. If fallback_source exists and transaction consumer is enabled -> Use PS query
            # 3. Else -> Display message IN PLACE OF result set
            
            if subsection.fallback_source and self._is_transaction_consumer_enabled(parsed_logs):
                # Use fallback source (performance_schema query)
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success:
                    # Use fallback columns if specified, otherwise use original columns
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    # Create a temporary subsection with fallback source
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        requires_extended_checks=False  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
            
            # No fallback available or consumer not enabled - show disabled message
            disabled_note = subsection.extended_checks_disabled_note or "Extended checks disabled. Use --extended-checks to enable."
            # Add note above the disabled message if provided
            note_md = ''
            if subsection.note:
                note_md = f'\n> {self._escape_markdown(subsection.note)}\n'
            return f"""### {self._escape_markdown(subsection.name)}
{note_md}
⚠️ **{self._escape_markdown(disabled_note)}**
"""
        
        # Special handling for Configuration Warnings subsection
        if source_id == 'config_warnings_check':
            return self._generate_config_warnings_subsection(subsection, parsed_logs)
        
        # Special handling for Key Performance Counters subsection
        if source_id == 'key_performance_counters':
            return self._generate_key_performance_counters_subsection(subsection, parsed_logs)
        
        # Special handling for Transaction Summary subsection
        if source_id == 'transaction_summary':
            return self._generate_transaction_summary_subsection(subsection, parsed_logs)
        
        # Special handling for Table Cache Statistics subsection
        if source_id == 'table_cache_stats':
            return self._generate_table_cache_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Temporary Table Usage subsection
        if source_id == 'temp_table_usage':
            return self._generate_temp_table_usage_subsection(subsection, parsed_logs)
        
        # Special handling for Memory Troubleshooting Tips subsection
        if source_id == 'memory_troubleshooting_tips':
            return self._generate_memory_troubleshooting_tips_subsection(subsection)
        
        # Special handling for Aurora InnoDB Note subsection
        if source_id == 'aurora_innodb_note':
            return self._generate_aurora_innodb_note_subsection(subsection)
        
        # Special handling for InnoDB Purge Analysis subsection
        if source_id == 'innodb_purge_analysis':
            return self._generate_innodb_purge_analysis_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Engine Status Analysis subsection
        if source_id == 'innodb_engine_status_analysis':
            return self._generate_innodb_engine_status_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Analysis subsection
        if source_id == 'long_running_transactions_analysis':
            return self._generate_long_running_transactions_subsection(subsection, parsed_logs)
        
        # Special handling for Tables Without Primary Key subsection
        if source_id == 'tables_without_pk':
            return self._generate_tables_without_pk_subsection(subsection, parsed_logs)
        
        # Special handling for Unused Indexes subsection
        if source_id == 'unused_indexes':
            return self._generate_unused_indexes_subsection(subsection, parsed_logs)

        # Special handling for Locked Accounts subsection
        if source_id == 'locked_accounts':
            return self._generate_locked_accounts_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Configuration subsection
        if source_id == 'ssl_config':
            return self._generate_ssl_config_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Connection Status subsection
        if source_id == 'ssl_connection_status':
            return self._generate_ssl_connection_status_subsection(subsection, parsed_logs)
        
        # Special handling for Connection Memory Overhead subsection
        if source_id == 'connection_memory_overhead':
            return self._generate_connection_memory_overhead_subsection(subsection, parsed_logs)
        
        # Special handling for Sort & Join Buffer Memory subsection
        if source_id == 'sort_join_buffer_memory':
            return self._generate_sort_join_buffer_memory_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Buffer Pool Statistics (Detailed) subsection
        if source_id == 'innodb_buffer_pool_stats_detailed':
            return self._generate_innodb_buffer_pool_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Per-Connection Memory Buffers subsection
        if source_id == 'per_connection_memory':
            return self._generate_per_connection_memory_subsection(subsection, parsed_logs)
        
        # Special handling for Global Memory Usage - Top Components subsection
        if source_id == 'global_memory_usage_top':
            return self._generate_global_memory_usage_top_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Row Operations subsection (metric cards)
        if source_id == 'innodb_row_operations':
            return self._generate_innodb_row_operations_subsection(subsection, parsed_logs)
        
        # Special handling for Buffer Pool Hit Rate subsection (metric cards)
        if source_id == 'health_buffer_pool_hit_rate':
            return self._generate_buffer_pool_hit_rate_subsection(subsection, parsed_logs)
        
        # Special handling for All Current Transactions Summary subsection (metric cards)
        if source_id == 'current_transactions_summary':
            return self._generate_current_transactions_summary_subsection(subsection, parsed_logs)
        
        # Special handling for History List Length subsection (metric cards)
        if source_id == 'innodb_history_length':
            return self._generate_history_list_length_subsection(subsection, parsed_logs)
        
        # Special handling for Parallel Replication Worker Stats subsection
        if source_id == 'parallel_replication_worker_stats':
            return self._generate_parallel_replication_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Activity subsection
        if source_id == 'long_running_trx_recent_queries':
            return self._generate_long_running_trx_activity_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Memory Status subsection (dynamic recommendations)
        if source_id == 'aurora_memory_status':
            return self._generate_aurora_memory_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Parallel Query Status subsection (dynamic recommendations)
        if source_id == 'aurora_pq_status':
            return self._generate_aurora_pq_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Relay Log Status subsection (dynamic recommendations)
        if source_id == 'aurora_relay_log_status':
            return self._generate_aurora_relay_log_status_subsection(subsection, parsed_logs)
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            # Primary source not available - check for fallback
            if subsection.fallback_source:
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success and fallback_log.rows:
                    # Use fallback source
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        fallback_source=None  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
                elif subsection.fallback_requires_extended_checks and not self._extended_checks:
                    # Fallback requires extended checks which are disabled
                    disabled_note = subsection.extended_checks_disabled_note or "Enable --extended-checks to view this data."
                    note_md = f"\n> {self._escape_markdown(subsection.note)}\n" if subsection.note else ''
                    return f"""### {self._escape_markdown(subsection.name)}
{note_md}
> ⚠️ {self._escape_markdown(disabled_note)}
"""
            return f"""### {self._escape_markdown(subsection.name)}

*No data available*
"""
        
        log_data = parsed_logs[source_id]
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case - use default_note if provided
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f"""### {self._escape_markdown(subsection.name)}

*No matching columns found*
"""
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Generate table
        table_md = self._generate_table(
            display_columns,
            limited_rows,
            column_map,
            log_data.columns
        )
        
        # Add truncation notice if needed
        truncation_md = ''
        if was_truncated:
            truncation_md = f'\n*{self._limit_enforcer.get_truncation_message(total_count)}*\n'
        
        # Add note above the table if provided
        note_md = ''
        if subsection.note:
            note_md = f'> {self._escape_markdown(subsection.note)}\n\n'
        
        # Add warning for Unused Indexes subsection when it has data
        warning_md = ''
        if subsection.name == "Unused Indexes" and limited_rows:
            warning_md = '''
> ⚠️ **Important - Safe Index Removal Process:**
>
> DO NOT drop indexes immediately! Indexes may be used infrequently (daily/weekly/monthly reports, batch jobs, etc.).
>
> **Recommended approach (MySQL 8.0+):**
> 1. Make the index INVISIBLE first: `ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE;`
> 2. Monitor for several weeks/months to ensure no queries break
> 3. If no issues, drop the index: `ALTER TABLE schema.table DROP INDEX index_name;`
> 4. If queries break, make visible again: `ALTER TABLE schema.table ALTER INDEX index_name VISIBLE;`
>
> **Why:** Invisible indexes are maintained by MySQL but not used by the optimizer. This allows safe testing without the risk of losing the index structure. Available in MySQL 8.0+ and Aurora MySQL 2.x+.
'''
        # Add warning from subsection config if provided
        elif subsection.warning and limited_rows:
            warning_md = f'\n> ⚠️ {self._escape_markdown(subsection.warning)}\n'

        # Add result_note after the table when there are results
        result_note_md = ''
        if subsection.result_note and limited_rows:
            result_note_md = f'\n> *{self._escape_markdown(subsection.result_note)}*\n'
        
        return f"""### {self._escape_markdown(subsection.name)}

{note_md}{table_md}
{truncation_md}{result_note_md}{warning_md}"""
    
    def _generate_config_warnings_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate Markdown for the Configuration Warnings subsection.
        
        This method transforms the raw config_warnings_check query data into
        warning rows using the evaluate_config_warnings function.
        """
        # Evaluate config warnings using the module-level function
        warnings = evaluate_config_warnings(parsed_logs)
        
        if not warnings:
            no_data_message = subsection.default_note if subsection.default_note else "No configuration warnings"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Generate table with Parameter and Message columns
        header_row = '| Parameter | Message |'
        separator_row = '|---|---|'
        
        data_rows = []
        for param, message in warnings:
            data_rows.append(f'| {self._escape_table_cell(param)} | {self._escape_table_cell(message)} |')
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}
"""
    
    def _generate_key_performance_counters_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate Markdown for the Key Performance Counters subsection.
        
        This method transforms the raw key_performance_counters query data into
        a single row with calculated metrics.
        """
        # Transform the data using the module-level function
        transformed_rows = evaluate_key_performance_counters(parsed_logs, 0)
        
        if not transformed_rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Define the columns for display
        columns = ["Queries Per Second", "Total Queries", "Slow Queries", "Threads Connected", "Threads Running", "Aborted Connections", "Aborted Clients"]
        
        # Generate table
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        
        data_rows = []
        for row in transformed_rows:
            cells = [self._escape_table_cell(str(val)) for val in row]
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}
"""
    
    def _generate_transaction_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Transaction Summary subsection with metric table."""
        source_id = subsection.source
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Extract values from the row
        # Columns: total_transactions, oldest_trx_age_seconds, total_rows_locked, total_modified_rows
        row = parsed_log.rows[0]
        total_transactions = row[0] if row[0] is not None else 0
        oldest_trx_age = row[1] if row[1] is not None else 0
        total_rows_locked = row[2] if row[2] is not None else 0
        total_modified_rows = row[3] if row[3] is not None else 0
        
        # Format values
        total_transactions_str = f"{total_transactions:,}"
        oldest_trx_age_str = f"{oldest_trx_age:,}" if oldest_trx_age else "0"
        total_rows_locked_str = f"{total_rows_locked:,}"
        total_modified_rows_str = f"{total_modified_rows:,}"
        
        # Generate table with metrics
        columns = ["Total Transactions", "Oldest Trx Age (seconds)", "Total Rows Locked", "Total Modified Rows"]
        values = [total_transactions_str, oldest_trx_age_str, total_rows_locked_str, total_modified_rows_str]
        
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        data_row = '| ' + ' | '.join(values) + ' |'
        
        table_md = f"""{header_row}
{separator_row}
{data_row}"""
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}
"""
    
    def _generate_innodb_row_operations_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for InnoDB Row Operations subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Extract values - columns: Rows Read, Rows Inserted, Rows Updated, Rows Deleted
        row = parsed_log.rows[0]
        rows_read = row[0] if row[0] is not None else 0
        rows_inserted = row[1] if row[1] is not None else 0
        rows_updated = row[2] if row[2] is not None else 0
        rows_deleted = row[3] if row[3] is not None else 0
        
        # Format values
        rows_read_str = f"{rows_read:,}"
        rows_inserted_str = f"{rows_inserted:,}"
        rows_updated_str = f"{rows_updated:,}"
        rows_deleted_str = f"{rows_deleted:,}"
        
        # Generate table with metrics
        columns = ["Rows Read", "Rows Inserted", "Rows Updated", "Rows Deleted"]
        values = [rows_read_str, rows_inserted_str, rows_updated_str, rows_deleted_str]
        
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        data_row = '| ' + ' | '.join(values) + ' |'
        
        table_md = f"""{header_row}
{separator_row}
{data_row}"""
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n\n> {self._escape_markdown(subsection.note)}"
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}{note_md}
"""
    
    def _generate_buffer_pool_hit_rate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Buffer Pool Hit Rate subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Extract value - column: buffer_pool_hit_rate
        row = parsed_log.rows[0]
        hit_rate = row[0] if row[0] is not None else "N/A"
        
        # Generate table with metric
        columns = ["Buffer Pool Hit Rate"]
        values = [str(hit_rate)]
        
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        data_row = '| ' + ' | '.join(values) + ' |'
        
        table_md = f"""{header_row}
{separator_row}
{data_row}"""
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n\n> {self._escape_markdown(subsection.note)}"
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}{note_md}
"""
    
    def _generate_current_transactions_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for All Current Transactions Summary subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Extract values - columns: Total Active Transactions, Oldest Transaction Age (seconds)
        row = parsed_log.rows[0]
        total_active = row[0] if row[0] is not None else 0
        oldest_age = row[1] if row[1] is not None else 0
        
        # Format values
        total_active_str = f"{total_active:,}"
        oldest_age_str = f"{oldest_age:,}"
        
        # Generate table with metrics
        columns = ["Total Active Transactions", "Oldest Transaction Age (seconds)"]
        values = [total_active_str, oldest_age_str]
        
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        data_row = '| ' + ' | '.join(values) + ' |'
        
        table_md = f"""{header_row}
{separator_row}
{data_row}"""
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n\n> {self._escape_markdown(subsection.note)}"
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}{note_md}
"""
    
    def _generate_history_list_length_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for History List Length subsection with metric cards."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Extract value - column: history_list_length
        row = parsed_log.rows[0]
        history_length = row[0] if row[0] is not None else 0
        
        # Format value
        history_length_str = f"{history_length:,}"
        
        # Generate table with metric
        columns = ["History List Length"]
        values = [history_length_str]
        
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        data_row = '| ' + ' | '.join(values) + ' |'
        
        table_md = f"""{header_row}
{separator_row}
{data_row}"""
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n\n> {self._escape_markdown(subsection.note)}"
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}{note_md}
"""
    
    def _generate_parallel_replication_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Parallel Replication Worker Stats subsection with interpretation."""
        # Helper to get value from parsed logs
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return log_data.rows[0][col_idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        # Check if this is a replica instance
        replication_role = get_value('replication_role', 'replication_role', 'STANDALONE')
        if replication_role != 'REPLICA':
            return ''
        
        # Check if parallel workers > 1
        parallel_workers = 0
        parallel_type = 'N/A'
        dep_tracking = 'N/A'
        
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    # Check replica_parallel_workers first (MySQL 8.0.26+), fallback to slave_parallel_workers
                    if 'replica_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    if parallel_workers == 0 and 'slave_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    
                    # Check replica_parallel_type first, fallback to slave_parallel_type
                    if 'replica_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    if parallel_type == 'N/A' and 'slave_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    
                    if 'binlog_transaction_dependency_tracking' in log_data.columns:
                        idx = log_data.columns.index('binlog_transaction_dependency_tracking')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            dep_tracking = str(val)
                except (ValueError, IndexError, TypeError):
                    pass
        
        if parallel_workers <= 1:
            return ''
        
        source_id = subsection.source
        if source_id not in parsed_logs:
            return ''
        
        log_data = parsed_logs[source_id]
        if not log_data.success:
            return (
                f"### {self._escape_markdown(subsection.name)}\n\n"
                f"**Error:** {self._escape_markdown(log_data.error or 'Unknown error')}\n"
            )
        
        # Workers are configured but no per-worker rows came back. THREAD_ID is NULL
        # in replication_applier_status_by_worker while the applier is stopped, so
        # the join yields nothing. Emitting the note keeps "could not measure" apart
        # from "distribution is fine", which an empty section cannot express.
        if not log_data.rows or not log_data.columns:
            no_data_message = (
                subsection.default_note if subsection.default_note else "No data returned"
            )
            return (
                f"### {self._escape_markdown(subsection.name)}\n\n"
                f"*{self._escape_markdown(no_data_message)}*\n"
            )
        
        md = f"### {self._escape_markdown(subsection.name)}\n\n"
        if subsection.note:
            md += f"> {self._escape_markdown(subsection.note)}\n\n"
        
        md += "| Channel Name | Worker Thread ID | Transactions Executed | % of Total |\n"
        md += "|---|---|---|---|\n"
        
        try:
            channel_idx = log_data.columns.index('CHANNEL_NAME')
            thread_idx = log_data.columns.index('replica_worker_thread_id')
            trx_idx = log_data.columns.index('number_of_trx_executed')
            pct_idx = log_data.columns.index('percent_of_trx_executed_by_worker')
            
            worker_stats = []
            for row in log_data.rows:
                channel = row[channel_idx] if row[channel_idx] else '(default)'
                thread_id = row[thread_idx]
                trx_count = row[trx_idx]
                pct = row[pct_idx]
                worker_stats.append({'channel': channel, 'thread_id': thread_id, 'trx_count': int(trx_count) if trx_count else 0, 'pct': float(pct) if pct else 0.0})
                md += f"| {self._escape_markdown(str(channel))} | {thread_id} | {trx_count} | {pct}% |\n"
            
            if worker_stats:
                md += self._generate_mts_interpretation_md(worker_stats, parallel_workers, parallel_type, dep_tracking)
        except (ValueError, IndexError) as e:
            md += f"| Error | - | - | {self._escape_markdown(str(e))} |\n"
        
        return md + "\n"
    
    def _generate_mts_interpretation_md(self, worker_stats: list, parallel_workers: int, parallel_type: str, dep_tracking: str) -> str:
        """Generate Markdown interpretation of MTS worker efficiency."""
        if not worker_stats:
            return ''
        
        channels = {}
        for stat in worker_stats:
            channel = stat['channel']
            if channel not in channels:
                channels[channel] = []
            channels[channel].append(stat)
        
        md = "\n#### Worker Efficiency Analysis\n\n"
        
        for channel, stats in channels.items():
            total_trx = sum(s['trx_count'] for s in stats)
            if total_trx == 0:
                md += f"**Channel: {self._escape_markdown(channel)}** - No transactions executed yet.\n\n"
                continue
            
            percentages = [s['pct'] for s in stats]
            max_pct, min_pct = max(percentages), min(percentages)
            avg_pct = sum(percentages) / len(percentages)
            spread = max_pct - min_pct
            active_workers = sum(1 for p in percentages if p > 5.0)
            
            md += f"**Channel: {self._escape_markdown(channel)}**\n\n"
            md += f"- Total transactions: {total_trx:,}\n"
            md += f"- Active workers: {active_workers} of {len(stats)}\n"
            md += f"- Distribution spread: {spread:.1f}%\n\n"
            
            md += "**Configuration:**\n\n| Parameter | Value | Status |\n|---|---|---|\n"
            pt_status = '✅ Optimal' if parallel_type == 'LOGICAL_CLOCK' else '⚠️ Suboptimal'
            md += f"| replica_parallel_type | {parallel_type} | {pt_status} |\n"
            if dep_tracking != 'N/A' and self._database_version < (8, 1, 0):
                dt_status = '✅ Optimal' if dep_tracking in ('WRITESET', 'WRITESET_SESSION') else '⚠️ Suboptimal'
                md += f"| binlog_transaction_dependency_tracking | {dep_tracking} | {dt_status} |\n"
            md += f"| replica_parallel_workers | {parallel_workers} | ✅ Enabled |\n\n"
            
            if spread < 20 and active_workers >= len(stats) * 0.8:
                md += "> ✅ **Good Distribution:** Transactions well-distributed.\n\n"
            elif spread > 50 or active_workers < len(stats) * 0.5:
                md += "> ⚠️ **Uneven Distribution:** Workload concentrated on fewer workers.\n\n"
                md += "**Recommended Actions:**\n\n| # | Action | Details |\n|---|---|---|\n"
                n = 1
                if parallel_type != 'LOGICAL_CLOCK':
                    md += f"| {n} | Set replica_parallel_type=LOGICAL_CLOCK | `SET GLOBAL replica_parallel_type='LOGICAL_CLOCK';` (stop replica first) |\n"
                    n += 1
                if dep_tracking != 'N/A' and self._database_version < (8, 1, 0) and dep_tracking not in ('WRITESET', 'WRITESET_SESSION'):
                    md += f"| {n} | Enable WRITESET on SOURCE | `SET GLOBAL binlog_transaction_dependency_tracking='WRITESET';` |\n"
                    n += 1
                md += f"| {n} | Review workload | Check for hot spots or sequential dependencies |\n"
                md += f"| {n+1} | Monitor after changes | Reset stats and re-run collector |\n\n"
            else:
                md += "> ℹ️ **Moderate Distribution:** Acceptable but could be improved.\n\n"
        
        return md
    
    def _generate_table_cache_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate Markdown for the Table Cache Statistics subsection.
        
        This method transforms the raw table_cache_stats and table_cache_config
        query data into rows with Metric, Value, Explanation format.
        """
        # Transform the data using the module-level function
        transformed_rows = evaluate_table_cache_stats(parsed_logs)
        
        if not transformed_rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Define the columns for display
        columns = ["Metric", "Value", "Explanation"]
        
        # Generate table
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        
        data_rows = []
        for row in transformed_rows:
            cells = [self._escape_table_cell(str(val)) for val in row]
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}
"""
    
    def _generate_temp_table_usage_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Temporary Table Usage subsection with metric display."""
        # Transform the data using the module-level function
        temp_data = evaluate_temp_table_usage(parsed_logs)
        
        if not temp_data:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Format the values
        created_tmp_tables = f"{temp_data['created_tmp_tables']:,}"
        created_tmp_disk_tables = f"{temp_data['created_tmp_disk_tables']:,}"
        disk_tmp_table_pct = f"{temp_data['disk_tmp_table_pct']:.2f}%"
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n"
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
| Created Tmp Tables | Created Tmp Disk Tables | Disk Tmp Table % |
|:------------------:|:-----------------------:|:----------------:|
| {created_tmp_tables} | {created_tmp_disk_tables} | {disk_tmp_table_pct} |
"""
    
    def _generate_connection_memory_overhead_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Connection Memory Overhead subsection with metric display."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        current_conn = str(row[col_map.get('Current Connections', 0)]) if 'Current Connections' in col_map else 'N/A'
        max_conn = str(row[col_map.get('Max Connections', 1)]) if 'Max Connections' in col_map else 'N/A'
        avg_mem = str(row[col_map.get('Avg Memory per Connection', 2)]) if 'Avg Memory per Connection' in col_map else 'N/A'
        total_mem = str(row[col_map.get('Total Connection Memory', 3)]) if 'Total Connection Memory' in col_map else 'N/A'
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n"
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
| Current Connections | Max Connections | Avg Memory per Connection | Total Connection Memory |
|:-------------------:|:---------------:|:-------------------------:|:-----------------------:|
| {current_conn} | {max_conn} | {avg_mem} | {total_mem} |
"""
    
    def _generate_sort_join_buffer_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Sort & Join Buffer Memory subsection with metric display."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        sort_buffer = str(row[col_map.get('sort_buffer_size', 0)]) if 'sort_buffer_size' in col_map else 'N/A'
        join_buffer = str(row[col_map.get('join_buffer_size', 1)]) if 'join_buffer_size' in col_map else 'N/A'
        read_buffer = str(row[col_map.get('read_buffer_size', 2)]) if 'read_buffer_size' in col_map else 'N/A'
        read_rnd_buffer = str(row[col_map.get('read_rnd_buffer_size', 3)]) if 'read_rnd_buffer_size' in col_map else 'N/A'
        sort_merge = str(row[col_map.get('Sort Merge Passes', 4)]) if 'Sort Merge Passes' in col_map else 'N/A'
        sort_range = str(row[col_map.get('Sort Range', 5)]) if 'Sort Range' in col_map else 'N/A'
        sort_scan = str(row[col_map.get('Sort Scan', 6)]) if 'Sort Scan' in col_map else 'N/A'
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n"
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
| sort_buffer_size | join_buffer_size | read_buffer_size | read_rnd_buffer_size | Sort Merge Passes | Sort Range | Sort Scan |
|:----------------:|:----------------:|:----------------:|:--------------------:|:-----------------:|:----------:|:---------:|
| {sort_buffer} | {join_buffer} | {read_buffer} | {read_rnd_buffer} | {sort_merge} | {sort_range} | {sort_scan} |
"""
    
    def _generate_innodb_buffer_pool_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for InnoDB Buffer Pool Statistics (Detailed) subsection with metric display."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        pool_size = str(row[col_map.get('Buffer Pool Size (GB)', 0)]) if 'Buffer Pool Size (GB)' in col_map else 'N/A'
        pool_instances = str(row[col_map.get('Buffer Pool Instances', 1)]) if 'Buffer Pool Instances' in col_map else 'N/A'
        total_pages = str(row[col_map.get('Total Pages', 2)]) if 'Total Pages' in col_map else 'N/A'
        data_pages = str(row[col_map.get('Data Pages', 3)]) if 'Data Pages' in col_map else 'N/A'
        dirty_pages = str(row[col_map.get('Dirty Pages', 4)]) if 'Dirty Pages' in col_map else 'N/A'
        free_pages = str(row[col_map.get('Free Pages', 5)]) if 'Free Pages' in col_map else 'N/A'
        hit_rate = str(row[col_map.get('Buffer Pool Hit Rate', 6)]) if 'Buffer Pool Hit Rate' in col_map else 'N/A'
        dirty_pct = str(row[col_map.get('Dirty Page %', 7)]) if 'Dirty Page %' in col_map else 'N/A'
        
        # Format numeric values with commas
        try:
            total_pages = f"{int(total_pages):,}" if total_pages != 'N/A' and total_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            data_pages = f"{int(data_pages):,}" if data_pages != 'N/A' and data_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            dirty_pages = f"{int(dirty_pages):,}" if dirty_pages != 'N/A' and dirty_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            free_pages = f"{int(free_pages):,}" if free_pages != 'N/A' and free_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n"
        
        # Generate buffer pool per-instance table if available
        buffer_pool_table_md = ''
        if 'buffer_pool_stats' in parsed_logs:
            bp_data = parsed_logs['buffer_pool_stats']
            if bp_data.success and bp_data.rows and bp_data.columns:
                bp_col_map = {col: idx for idx, col in enumerate(bp_data.columns)}
                display_columns = [
                    ('POOL_ID', 'POOL_ID'),
                    ('POOL_SIZE', 'POOL_SIZE'),
                    ('FREE_BUFFERS', 'FREE_BUFFERS'),
                    ('DATABASE_PAGES', 'DATABASE_PAGES'),
                    ('OLD_DATABASE_PAGES', 'OLD_DATABASE_PAGES'),
                    ('MODIFIED_DATABASE_PAGES', 'MODIFIED_DATABASE_PAGES')
                ]
                table_md = self._generate_table(
                    display_columns,
                    bp_data.rows,
                    bp_col_map,
                    bp_data.columns
                )
                buffer_pool_table_md = f"\n> **Buffer Pool Per-Instance Statistics:**\n\n{table_md}\n"
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
| Buffer Pool Size | Buffer Pool Instances | Total Pages | Data Pages | Dirty Pages | Free Pages | Buffer Pool Hit Rate | Dirty Page % |
|:----------------:|:---------------------:|:-----------:|:----------:|:-----------:|:----------:|:--------------------:|:------------:|
| {pool_size} GB | {pool_instances} | {total_pages} | {data_pages} | {dirty_pages} | {free_pages} | {hit_rate} | {dirty_pct} |
{buffer_pool_table_md}"""
    
    def _generate_per_connection_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Per-Connection Memory Buffers subsection with table and estimate note."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f"""### {self._escape_markdown(subsection.name)}

*No matching columns found*
"""
        
        # Generate table
        table_md = self._generate_table(
            display_columns,
            log_data.rows,
            column_map,
            log_data.columns
        )
        
        # Add note above the table if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n"
        
        # Get per_connection_memory_estimate data for the estimate note
        estimate_note_md = ''
        if 'per_connection_memory_estimate' in parsed_logs:
            estimate_data = parsed_logs['per_connection_memory_estimate']
            if estimate_data.success and estimate_data.rows and estimate_data.columns:
                est_col_map = {col: idx for idx, col in enumerate(estimate_data.columns)}
                est_row = estimate_data.rows[0]
                
                per_conn_mb = str(est_row[est_col_map.get('Per Connection (MB)', 0)]) if 'Per Connection (MB)' in est_col_map else 'N/A'
                max_conn = str(est_row[est_col_map.get('Max Connections', 1)]) if 'Max Connections' in est_col_map else 'N/A'
                est_max_mem = str(est_row[est_col_map.get('Estimated Max Connections Memory', 2)]) if 'Estimated Max Connections Memory' in est_col_map else 'N/A'
                
                estimate_note_md = f"\n> **Estimated Per-Connection Memory:** ~{self._escape_markdown(per_conn_mb)} MB per connection. With max_connections={self._escape_markdown(max_conn)}, this could use up to {self._escape_markdown(est_max_mem)} GB if all connections are active.\n"
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
{table_md}
{estimate_note_md}"""
    
    def _generate_global_memory_usage_top_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Global Memory Usage - Top Components subsection with table and total note."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f"""### {self._escape_markdown(subsection.name)}

*No matching columns found*
"""
        
        # Apply collection limit to displayed rows
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Generate table with limited rows
        table_md = self._generate_table(
            display_columns,
            limited_rows,
            column_map,
            log_data.columns
        )
        
        # Add truncation notice if rows were limited
        truncation_md = ''
        if was_truncated:
            truncation_md = f"*Showing {self._limit_enforcer.limit} of {total_count} rows*\n"
        
        # Add note above the table if provided
        note_md = ''
        if subsection.note:
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n"
        
        # Get global_memory_usage_total data for the total note
        # This uses ALL rows for the total calculation, not just the displayed rows
        total_note_md = ''
        if 'global_memory_usage_total' in parsed_logs:
            total_data = parsed_logs['global_memory_usage_total']
            if total_data.success and total_data.rows and total_data.columns:
                total_col_map = {col: idx for idx, col in enumerate(total_data.columns)}
                total_row = total_data.rows[0]
                
                total_allocated = str(total_row[total_col_map.get('Total Allocated', 0)]) if 'Total Allocated' in total_col_map else 'N/A'
                high_watermark = str(total_row[total_col_map.get('High Watermark', 1)]) if 'High Watermark' in total_col_map else 'N/A'
                
                total_note_md = f"\n> **Total Tracked Memory:** {self._escape_markdown(total_allocated)} currently allocated. High watermark: {self._escape_markdown(high_watermark)}\n"
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
{truncation_md}
{table_md}
{total_note_md}"""
    
    def _generate_memory_troubleshooting_tips_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> str:
        """Generate Markdown for Memory Troubleshooting Tips subsection."""
        tips = [
            ("High Memory Usage", "Check section 4.3 (Global Memory Usage) to identify which components are consuming memory"),
            ("Memory Spikes", 'Look at "High Watermark" columns - they show peak memory usage even if current usage is low'),
            ("Per-Connection Issues", "Check section 4.4 (Memory by Thread) to find memory-hungry connections"),
            ("Application Issues", "Use section 4.5 (Memory by User) to identify which applications/users consume most memory"),
            ("Prepared Statement Memory", "Check section 4.7 for prepared statement memory usage - common cause of memory issues"),
            ("Temporary Table Issues", "Section 4.9 shows if queries are creating too many disk temp tables"),
            ("Reset Statistics", "To reset high watermarks, run: `TRUNCATE TABLE performance_schema.memory_summary_global_by_event_name;`")
        ]
        
        tips_md = '\n'.join([f"* **{title}:** {desc}" for title, desc in tips])
        
        return f"""### {self._escape_markdown(subsection.name)}

**How to Use This Section for Troubleshooting:**

{tips_md}
"""
    
    def _generate_aurora_innodb_note_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> str:
        """Generate Markdown for Aurora InnoDB Note subsection (Aurora MySQL only)."""
        return f"""### {self._escape_markdown(subsection.name)}

> **Note:**
> * Aurora MySQL uses a distributed storage layer that automatically manages I/O, flushing, and redo log sizing. Many traditional InnoDB tuning parameters are not applicable or modifiable in Aurora.
> * Redo log sizing and flushing are managed automatically by Aurora's distributed storage layer. These configuration parameters do not apply to Aurora.
> * Redo log management is handled automatically by Aurora's distributed storage layer. Redo log occupancy monitoring and sizing are not applicable to Aurora instances.
"""
    
    def _generate_innodb_purge_analysis_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for InnoDB Purge Analysis composite subsection."""
        
        # Get history list length for health status
        history_list_length = 0
        if 'innodb_history_length' in parsed_logs:
            hll_data = parsed_logs['innodb_history_length']
            if hll_data.success and hll_data.rows and hll_data.columns:
                try:
                    history_list_length = int(hll_data.rows[0][0])
                except (ValueError, TypeError, IndexError):
                    pass
        
        # Check for long-running transactions (older than 5 minutes = 300 seconds)
        has_long_running_trx = False
        if 'long_running_idle_trx' in parsed_logs:
            trx_data = parsed_logs['long_running_idle_trx']
            if trx_data.success and trx_data.rows:
                has_long_running_trx = True
        if not has_long_running_trx and 'innodb_transactions' in parsed_logs:
            trx_data = parsed_logs['innodb_transactions']
            if trx_data.success and trx_data.rows and trx_data.columns:
                try:
                    duration_idx = trx_data.columns.index('duration_seconds')
                    for row in trx_data.rows:
                        if row[duration_idx] and int(row[duration_idx]) > 300:
                            has_long_running_trx = True
                            break
                except (ValueError, IndexError):
                    pass
        
        # Build Understanding Purge section
        understanding_md = """
> **Understanding Purge:** InnoDB purge is a background process that removes old row versions (undo logs) no longer needed for MVCC or rollback. When purge cannot keep up with transaction rate, the History List Length grows, causing:
> * Increased disk space (undo logs)
> * Slower queries (more versions to skip)
> * Higher I/O consumption
> * Potential disk log pressure
>
> **Reference:** [AWS Blog: Achieve a high-speed InnoDB purge](https://aws.amazon.com/blogs/database/achieve-a-high-speed-innodb-purge-on-amazon-rds-for-mysql-and-amazon-aurora-mysql/)
"""
        
        # Build Purge Thread Activity table
        thread_activity_md = self._build_purge_thread_activity_md(parsed_logs)
        
        # Build Purge Configuration table
        purge_config_md = self._build_purge_configuration_md(parsed_logs)
        
        # Build Purge Performance Metrics
        purge_metrics_md = self._build_purge_performance_metrics_md(parsed_logs)
        
        # Build Purge Optimization Recommendations
        recommendations_md = """
> **Purge Optimization Recommendations (Based on AWS Best Practices):**
> * **Monitor History List Length:** Set CloudWatch alarms for values >10K (warning) and >50K (critical)
> * **Avoid Long-Running Transactions:** Keep transactions short; long transactions block purge for all subsequent transactions
> * **Optimize Workload:** Use DROP PARTITION or DROP TABLE instead of DELETE for bulk deletion use cases (age-partitioned)
> * **Secondary Indexes:** Purge is slower with many secondary indexes. Review unused indexes with sys.schema_unused_indexes
> * **Table Sharding:** Spread writes across multiple tables to increase number of purge threads for better parallelism
> * **Instance Sizing:** Ensure adequate CPU, memory (buffer pool), and I/O capacity for purge threads
> * **Aurora-Specific:** In Aurora, purge performance scales to Serverless v2 scales on replicas. Consider aurora_read_replica_read_committed=ON
"""
        
        # Build Purge Health Status
        if history_list_length < 50000:
            status_text = f"**Purge Health Status:** ✅ OK - Purge operating normally. History list length ({history_list_length:,}) is healthy."
        elif history_list_length < 100000:
            status_text = f"**Purge Health Status:** ⚠️ WARNING - History list length ({history_list_length:,}) is elevated. Monitor purge performance and check for long-running transactions."
        else:
            if has_long_running_trx:
                status_text = f"**Purge Health Status:** 🔴 CRITICAL - History list length ({history_list_length:,}) is very high. Long-running transaction(s) detected that may be blocking purge. See [Active Transactions](#active-transactions) section."
            else:
                status_text = f"**Purge Health Status:** 🔴 CRITICAL - History list length ({history_list_length:,}) is very high. Purge may not be keeping up with workload."
        
        return f"""### {self._escape_markdown(subsection.name)}

*Detailed analysis of InnoDB purge operations. Purge removes old row versions (undo logs) created by MVCC. Slow purge causes History List Length growth, causing performance degradation.*

{understanding_md}

{thread_activity_md}

{purge_config_md}

{purge_metrics_md}

{recommendations_md}

{status_text}
"""
    
    def _generate_innodb_engine_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for InnoDB Engine Status (Detailed Diagnostics) composite subsection."""
        
        # Get parsed InnoDB status data
        status_data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        if not status_data:
            return f"""### {self._escape_markdown(subsection.name)}

*InnoDB engine status not available*
"""
        
        # Build all sub-sections
        sections_md = []
        
        # 1. Latest Detected Deadlock
        sections_md.append(self._build_deadlock_md(status_data))
        
        # 2. Latest Foreign Key Error
        sections_md.append(self._build_fk_error_md(status_data))
        
        # 3. Semaphore Contention
        sections_md.append(self._build_semaphore_md(status_data))
        
        # 4. Transaction & Purge Status
        sections_md.append(self._build_trx_purge_status_md(status_data))
        
        # 5. Redo Log Checkpoint Status
        sections_md.append(self._build_redo_log_checkpoint_md(status_data))
        
        # 6. Buffer Pool Efficiency
        sections_md.append(self._build_buffer_pool_efficiency_md(status_data))
        
        # 7. Adaptive Hash Index Efficiency
        sections_md.append(self._build_adaptive_hash_md(status_data))
        
        # 8. Row Operations Summary
        sections_md.append(self._build_row_operations_md(status_data))
        
        # 9. InnoDB Status Optimization Tips
        sections_md.append(self._build_innodb_status_tips_md())
        
        return f"""### {self._escape_markdown(subsection.name)}

*Detailed diagnostics from SHOW ENGINE INNODB STATUS. This provides internal InnoDB metrics for advanced troubleshooting.*

{''.join(sections_md)}
"""
    
    def _build_deadlock_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Latest Detected Deadlock section."""
        if not data.has_deadlock:
            return """
#### Latest Detected Deadlock

*No deadlocks detected since server restart*
"""
        
        # Build deadlock info
        trx_info = []
        for trx in data.deadlock_transactions:
            trx_md = f"- **Transaction {trx.get('number', '?')}**"
            if 'trx_id' in trx:
                trx_md += f" (ID: {trx['trx_id']})"
            if 'query' in trx:
                trx_md += f"\n  - Query: `{self._escape_markdown(trx['query'][:200])}`"
            trx_info.append(trx_md)
        
        victim_md = ""
        if data.deadlock_victim:
            victim_md = f"\n**Victim (rolled back):** Transaction {data.deadlock_victim}\n"
        
        timestamp_md = ""
        if data.deadlock_timestamp:
            timestamp_md = f"**Timestamp:** {data.deadlock_timestamp}\n\n"
        
        note_md = """
> **Notes:**
> - This shows only the most recent deadlock. To log all deadlocks to the error log, enable `innodb_print_all_deadlocks=ON`.
> - The `innodb_deadlock_detect` variable controls whether InnoDB performs deadlock detection. When enabled (default), InnoDB automatically detects deadlocks and rolls back one transaction. Disabling it (`innodb_deadlock_detect=OFF`) requires relying on `innodb_lock_wait_timeout` to handle deadlocks.
>
> **Best practices to avoid deadlocks:**
> - Access tables and rows in a consistent order across all transactions
> - Keep transactions short
> - Add proper indexes to reduce lock scope
> - Consider using `SELECT ... FOR UPDATE NOWAIT` or `SKIP LOCKED` for lock-free patterns
"""
        
        return f"""
#### Latest Detected Deadlock

{timestamp_md}**Transactions involved:**

{chr(10).join(trx_info)}
{victim_md}
{note_md}
"""
    
    def _build_fk_error_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Latest Foreign Key Error section."""
        if not data.has_fk_error:
            return """
#### Latest Foreign Key Error

*No foreign key errors since server restart*
"""
        
        details = []
        if data.fk_error_timestamp:
            details.append(f"| Timestamp | {data.fk_error_timestamp} |")
        if data.fk_error_constraint:
            details.append(f"| Constraint | {self._escape_markdown(data.fk_error_constraint)} |")
        if data.fk_error_table:
            details.append(f"| Table | {self._escape_markdown(data.fk_error_table)} |")
        if data.fk_error_statement:
            details.append(f"| Statement | `{self._escape_markdown(data.fk_error_statement[:300])}` |")
        
        table_md = ""
        if details:
            table_md = f"""| Field | Value |
|-------|-------|
{chr(10).join(details)}"""
        
        note_md = """
> **Best practices to avoid FK errors:**
> - Ensure parent rows exist before inserting child rows
> - Use `ON DELETE CASCADE` or `ON UPDATE CASCADE` carefully
> - Verify data integrity before enabling FK constraints on existing data
> - Avoid performing operations with `foreign_key_checks` disabled as this can compromise data integrity and lead to foreign key errors
"""
        
        return f"""
#### Latest Foreign Key Error

{table_md}

{note_md}
"""
    
    def _build_semaphore_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Semaphore Contention section."""
        rows = [
            f"| OS Waits Reservation Count | {data.os_waits_reservation_count:,} |",
            f"| OS Waits Signal Count | {data.os_waits_signal_count:,} |",
            f"| RW-shared: Spins / Rounds / OS Waits | {data.rw_shared_spins:,} / {data.rw_shared_rounds:,} / {data.rw_shared_os_waits:,} |",
            f"| RW-excl: Spins / Rounds / OS Waits | {data.rw_excl_spins:,} / {data.rw_excl_rounds:,} / {data.rw_excl_os_waits:,} |",
            f"| RW-sx: Spins / Rounds / OS Waits | {data.rw_sx_spins:,} / {data.rw_sx_rounds:,} / {data.rw_sx_os_waits:,} |",
            f"| Spin Rounds Per Wait (shared/excl/sx) | {data.spin_rounds_per_wait_shared:.2f} / {data.spin_rounds_per_wait_excl:.2f} / {data.spin_rounds_per_wait_sx:.2f} |",
        ]
        
        # Calculate OS wait ratio
        if data.os_waits_reservation_count > 0:
            wait_ratio = (data.os_waits_signal_count / data.os_waits_reservation_count) * 100
            rows.append(f"| Signal/Reservation Ratio | {wait_ratio:.1f}% |")
        
        # Current waits
        current_waits_md = ""
        if data.current_waits:
            waits_list = "\n".join([f"- `{self._escape_markdown(w[:200])}`" for w in data.current_waits[:5]])
            current_waits_md = f"""
> ⚠️ **Current Waits (threads waiting for semaphores):**
{waits_list}
"""
        
        note_md = """
> **Note:** High spin rounds per wait values are influenced by `innodb_spin_wait_delay`, `innodb_spin_wait_pause_multiplier`, and `innodb_sync_spin_loops`. The presence of threads in the current wait list indicates active contention.
"""
        
        return f"""
#### Semaphore Contention

| Metric | Value |
|--------|-------|
{chr(10).join(rows)}
{current_waits_md}
{note_md}
"""
    
    def _build_trx_purge_status_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Transaction & Purge Status section."""
        # Determine purge health
        purge_status = "Unknown"
        status_icon = ""
        if data.purge_state:
            if "running but idle" in data.purge_state.lower():
                purge_status = "Purge is current (running but idle)"
                status_icon = "✅"
            elif "running" in data.purge_state.lower():
                purge_status = "Purge is actively running"
                status_icon = "⚠️"
            else:
                purge_status = data.purge_state
        
        if data.purge_lag > 0:
            purge_status += f" - {data.purge_lag:,} transactions behind"
            if data.purge_lag > 1000:
                status_icon = "⚠️"
            if data.purge_lag > 10000:
                status_icon = "🔴"
        
        rows = [
            f"| Current Transaction ID Counter | {data.trx_id_counter:,} |",
            f"| Purge Done For Transaction | {data.purge_done_for_trx:,} |",
            f"| Purge Lag (transactions behind) | {data.purge_lag:,} |",
            f"| History List Length | {data.history_list_length:,} |",
            f"| Purge State | {status_icon} {purge_status} |",
        ]
        
        interpretation = ""
        if data.purge_lag > 10000:
            interpretation = """
> ⚠️ **Warning:** Purge lag is high. Investigate long-running transactions blocking purge.
"""
        
        return f"""
#### Transaction & Purge Status

| Metric | Value |
|--------|-------|
{chr(10).join(rows)}
{interpretation}
"""
    
    def _build_redo_log_checkpoint_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Redo Log Checkpoint Status section."""
        rows = [
            f"| Log Sequence Number (LSN) | {data.log_sequence_number:,} |",
            f"| Log Flushed Up To | {data.log_flushed_up_to:,} |",
            f"| Pages Flushed Up To | {data.pages_flushed_up_to:,} |",
            f"| Last Checkpoint At | {data.last_checkpoint_at:,} |",
            f"| Checkpoint Lag (bytes) | {data.checkpoint_lag:,} |",
            f"| Unflushed Log (bytes) | {data.unflushed_log:,} |",
            f"| Pending Log Flushes | {data.pending_log_flushes} |",
            f"| Pending Checkpoint Writes | {data.pending_chkp_writes} |",
            f"| Log I/Os Done | {data.log_ios_done:,} |",
            f"| Log I/Os Per Second | {data.log_ios_per_second:.2f} |",
        ]
        
        note_md = ""
        if data.checkpoint_lag > 0 or data.unflushed_log > 0:
            note_md = """
> **Recommendations:**
> - Checkpoint lag indicates how much redo log data hasn't been checkpointed yet. High checkpoint lag under heavy write workloads may indicate redo log is too small or I/O is a bottleneck.
> - Consider increasing `innodb_log_file_size` if checkpoint lag is consistently high.
"""
        
        return f"""
#### Redo Log Checkpoint Status

| Metric | Value |
|--------|-------|
{chr(10).join(rows)}
{note_md}
"""
    
    def _build_buffer_pool_efficiency_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Buffer Pool Efficiency section."""
        hit_rate_pct = (data.buffer_pool_hit_rate / data.buffer_pool_hit_rate_denominator * 100) if data.buffer_pool_hit_rate_denominator > 0 else 0
        
        rows = [
            f"| Buffer Pool Hit Rate | {data.buffer_pool_hit_rate} / {data.buffer_pool_hit_rate_denominator} ({hit_rate_pct:.2f}%) |",
            f"| Pages Made Young | {data.pages_made_young:,} |",
            f"| Pages Made Not Young | {data.pages_made_not_young:,} |",
            f"| Youngs Per Second | {data.youngs_per_sec:.2f} |",
            f"| Non-Youngs Per Second | {data.non_youngs_per_sec:.2f} |",
            f"| Read Ahead Per Second | {data.read_ahead_per_sec:.2f} |",
            f"| Evicted Without Access Per Second | {data.evicted_without_access_per_sec:.2f} |",
            f"| LRU Length | {data.lru_len:,} |",
        ]
        
        note_md = ""
        if hit_rate_pct < 99.9 or data.evicted_without_access_per_sec > 0:
            note_md = """
> **Recommendations:**
> - Buffer pool hit rate below 99.9% may indicate buffer pool is undersized for the working set.
> - High "evicted without access" rate suggests pages are being evicted before being read, indicating memory pressure.
> - Review queries causing full table scans that pollute the buffer pool.
> - Check if `innodb_buffer_pool_size`, `innodb_old_blocks_pct` and `innodb_old_blocks_time` are tuned appropriately.
"""
        
        return f"""
#### Buffer Pool Efficiency

| Metric | Value |
|--------|-------|
{chr(10).join(rows)}
{note_md}
"""
    
    def _build_adaptive_hash_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Adaptive Hash Index Efficiency section."""
        total_searches = data.hash_searches_per_sec + data.non_hash_searches_per_sec
        hash_ratio = (data.hash_searches_per_sec / total_searches * 100) if total_searches > 0 else 0
        
        rows = [
            f"| Hash Searches Per Second | {data.hash_searches_per_sec:.2f} |",
            f"| Non-Hash Searches Per Second | {data.non_hash_searches_per_sec:.2f} |",
            f"| Hash Search Ratio | {hash_ratio:.1f}% |",
            f"| Hash Table Size | {data.hash_table_size:,} |",
        ]
        
        note_md = ""
        if total_searches > 0:
            note_md = """
> **About Adaptive Hash Index (AHI):**
> - AHI builds in-memory hash indexes for frequently accessed index pages.
> - Low hash search ratio may indicate AHI is not benefiting your workload.
>
> **Recommendations:**
> - If hash searches are consistently near zero, consider disabling AHI with `innodb_adaptive_hash_index=OFF` to reduce memory overhead.
> - AHI works best with point lookups; range scans don't benefit from it.
> - Monitor `innodb_adaptive_hash_index_parts` for partitioning to reduce contention.
"""
        
        return f"""
#### Adaptive Hash Index Efficiency

| Metric | Value |
|--------|-------|
{chr(10).join(rows)}
{note_md}
"""
    
    def _build_row_operations_md(self, data: 'InnoDBStatusData') -> str:
        """Build Markdown for Row Operations Summary section."""
        return f"""
#### Row Operations Summary

| Metric | Value |
|--------|-------|
| Queries Inside InnoDB | {data.queries_inside_innodb} |
| Queries in Queue | {data.queries_in_queue} |
| Inserts/sec | {data.inserts_per_sec:.2f} |
| Updates/sec | {data.updates_per_sec:.2f} |
| Deletes/sec | {data.deletes_per_sec:.2f} |
| Reads/sec | {data.reads_per_sec_rows:.2f} |

**Main Thread State:** {data.main_thread_state or 'Unknown'}
"""
    
    def _build_innodb_status_tips_md(self) -> str:
        """Build Markdown for InnoDB Status Optimization Tips section."""
        tips = [
            ("**Deadlock Victim**", "The transaction InnoDB chose to roll back to break the deadlock cycle. InnoDB selects the transaction that modified fewer rows as the victim to minimize rollback cost."),
            ("**Purge Lag**", "The number of transactions whose old row versions haven't been cleaned up yet. Growing purge lag indicates long-running transactions are blocking purge, causing undo log growth and table bloat."),
            ("**History List Length**", "The number of unpurged transactions in the undo log. Values consistently above 10,000 indicate purge is falling behind, leading to increased disk usage and slower queries due to MVCC overhead."),
            ("**Checkpoint Lag**", "The difference between the current Log Sequence Number (LSN) and the last checkpoint. Large checkpoint lag means more redo log data needs to be replayed during crash recovery, increasing recovery time."),
            ("**Unflushed Log**", "The amount of data (in bytes) written to the InnoDB Log Buffer but not yet flushed to redo log files on disk. If this approaches `innodb_log_buffer_size`, it may indicate I/O bottleneck or undersized log buffer."),
            ("**Buffer Pool Hit Rate**", "The percentage of page requests served from the buffer pool cache vs disk reads. Values below 99.9% suggest the buffer pool may be undersized for your working set, causing excessive disk I/O."),
            ("**Pages Evicted Without Access**", "Pages that were loaded into the buffer pool but evicted before being read. High values indicate memory pressure or queries causing unnecessary page loads (e.g., full table scans)."),
            ("**Pages Made Young/Not Young**", "Tracks how pages move between the 'old' and 'young' sublists in the buffer pool LRU. High 'not young' values may indicate `innodb_old_blocks_time` is filtering out scan-loaded pages effectively."),
            ("**Adaptive Hash Index (AHI) Efficiency**", "The ratio of hash searches to non-hash searches. Low ratio means AHI isn't benefiting your workload; consider disabling it to free memory and reduce contention."),
            ("**Spin Rounds Per Wait**", "The number of spin loop iterations before a thread gives up and performs an OS wait. Values are influenced by `innodb_spin_wait_delay` and `innodb_sync_spin_loops`. High values with many OS waits indicate contention."),
            ("**OS Waits vs Reservation Count**", "The ratio of expensive OS-level waits to total lock attempts. Lower ratio is better, meaning more locks were acquired via cheap spinning rather than costly context switches."),
            ("**Queries in Queue**", "The number of queries waiting to enter InnoDB. Values consistently above 0 indicate thread contention; consider tuning `innodb_thread_concurrency`."),
            ("**Main Thread State**", "The current state of the InnoDB master background thread (sleeping, purging, flushing). Persistent non-sleeping states may indicate heavy background activity."),
        ]
        
        tips_md = "\n".join([f"- {title} - {desc}" for title, desc in tips])
        
        return f"""
#### InnoDB Status Optimization Tips

{tips_md}
"""
    
    def _generate_long_running_transactions_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Long-Running Transactions composite subsection."""
        
        # Build Active Transactions table
        active_trx_md = self._build_active_transactions_md(parsed_logs)
        
        # Build Long-Running Idle Transactions table
        idle_trx_md = self._build_long_running_idle_trx_md(parsed_logs)
        
        # Build Long-Running Active Transactions table
        active_long_trx_md = self._build_long_running_active_trx_md(parsed_logs)
        
        # Get note from subsection
        note_md = ''
        if subsection.note:
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n"
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
{active_trx_md}

{idle_trx_md}

{active_long_trx_md}
"""
    
    def _build_active_transactions_md(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build Markdown table for Active Transactions."""
        if 'innodb_transactions' not in parsed_logs:
            return """#### Active Transactions

*No active transactions*
"""
        
        log_data = parsed_logs['innodb_transactions']
        
        if not log_data.success or not log_data.rows:
            return """#### Active Transactions

*No active transactions*
"""
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'duration_seconds', 'trx_mysql_thread_id', 'trx_query', 'trx_rows_locked', 'trx_rows_modified']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            return """#### Active Transactions

*No matching columns found*
"""
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_row = "| " + " | ".join(self._escape_markdown(col) for col in display_columns) + " |"
        separator_row = "| " + " | ".join("---" for _ in display_columns) + " |"
        
        # Build data rows (limit to 20)
        data_rows = []
        for row in log_data.rows[:20]:
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                cells.append(self._escape_markdown(str(val) if val is not None else ""))
            data_rows.append("| " + " | ".join(cells) + " |")
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        return f"""#### Active Transactions

{table_md}
"""
    
    def _build_long_running_idle_trx_md(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build Markdown table for Long-Running Idle Transactions."""
        note = "> Transactions that started but have no current query running. May indicate application issues or forgotten transactions."
        
        if 'long_running_idle_trx' not in parsed_logs:
            return f"""#### Long-Running Idle Transactions (>5 min, no query)

{note}

*No idle transactions older than 5 minutes*
"""
        
        log_data = parsed_logs['long_running_idle_trx']
        
        if not log_data.success or not log_data.rows:
            return f"""#### Long-Running Idle Transactions (>5 min, no query)

{note}

*No idle transactions older than 5 minutes*
"""
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'trx_rows_modified']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            return f"""#### Long-Running Idle Transactions (>5 min, no query)

{note}

*No matching columns found*
"""
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_row = "| " + " | ".join(self._escape_markdown(col) for col in display_columns) + " |"
        separator_row = "| " + " | ".join("---" for _ in display_columns) + " |"
        
        # Build data rows (limit to 20)
        data_rows = []
        for row in log_data.rows[:20]:
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                cells.append(self._escape_markdown(str(val) if val is not None else ""))
            data_rows.append("| " + " | ".join(cells) + " |")
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        return f"""#### Long-Running Idle Transactions (>5 min, no query)

{note}

{table_md}
"""
    
    def _build_long_running_active_trx_md(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build Markdown table for Long-Running Active Transactions."""
        note = "> Transactions with queries currently executing. May indicate slow queries or large batch operations."
        
        if 'long_running_active_trx' not in parsed_logs:
            return f"""#### Long-Running Active Transactions (>1 min, with query)

{note}

*No active transactions running longer than 1 minute*
"""
        
        log_data = parsed_logs['long_running_active_trx']
        
        if not log_data.success or not log_data.rows:
            return f"""#### Long-Running Active Transactions (>1 min, with query)

{note}

*No active transactions running longer than 1 minute*
"""
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'current_query']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            return f"""#### Long-Running Active Transactions (>1 min, with query)

{note}

*No matching columns found*
"""
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_row = "| " + " | ".join(self._escape_markdown(col) for col in display_columns) + " |"
        separator_row = "| " + " | ".join("---" for _ in display_columns) + " |"
        
        # Build data rows (limit to 20)
        data_rows = []
        for row in log_data.rows[:20]:
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                cells.append(self._escape_markdown(str(val) if val is not None else ""))
            data_rows.append("| " + " | ".join(cells) + " |")
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        return f"""#### Long-Running Active Transactions (>1 min, with query)

{note}

{table_md}
"""
    
    def _generate_long_running_trx_activity_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Long-Running Transactions Activity subsection."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n" if subsection.note else ''
            return f"""### {self._escape_markdown(subsection.name)}
{note_md}
*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            note_md = f"\n> {self._escape_markdown(subsection.note)}\n" if subsection.note else ''
            return f"""### {self._escape_markdown(subsection.name)}
{note_md}
*{self._escape_markdown(no_data_message)}*
"""
        
        # Build the data table
        columns = ['mysql_pid', 'trx_duration', 'trx_state', 'sql_text', 'stmt_execution_time', 'rows_sent', 'rows_modified', 'rows_examined', 'tmp_disk_tables', 'error_info']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            display_columns = log_data.columns
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Build header
        header_row = "| " + " | ".join(self._escape_markdown(col) for col in display_columns) + " |"
        separator_row = "| " + " | ".join("---" for _ in display_columns) + " |"
        
        # Build data rows
        data_rows = []
        for row in log_data.rows:
            cells = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else ''
                # Truncate long SQL text for display
                if col == 'sql_text' and val and len(str(val)) > 100:
                    val = str(val)[:100] + '...'
                cells.append(self._escape_markdown(str(val) if val is not None else ""))
            data_rows.append("| " + " | ".join(cells) + " |")
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        # Build note
        note_md = f"\n> {self._escape_markdown(subsection.note)}\n" if subsection.note else ''
        
        # Build interpretation
        interpretation_md = """
**📊 Interpretation Guide:**

This section shows the **10 most recent SQL statements** executed within each long-running transaction (>5 minutes). Use this to understand what operations the transaction has performed.

**Key columns:**
- **mysql_pid**: Process ID - use to identify the connection
- **trx_duration**: Total time the transaction has been active
- **sql_text**: The SQL statement that was executed
- **stmt_execution_time**: How long this specific statement took
- **rows_sent**: Number of rows returned to the client
- **rows_modified**: Number of rows affected (INSERT/UPDATE/DELETE)
- **rows_examined**: Number of rows scanned (high values may indicate missing indexes)
- **tmp_disk_tables**: Disk temp tables created (high values indicate inefficient queries)
- **error_info**: Shows if the statement encountered errors

**What to look for:**
- Statements with high `rows_modified` may be causing lock contention
- High `rows_examined` vs `rows_sent` ratio suggests missing indexes
- High `tmp_disk_tables` suggests queries need optimization
- Multiple statements per PID show the transaction's activity history
- Use the PID to correlate with application logs or kill the connection if needed
"""
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
{table_md}

{interpretation_md}
"""
    
    def _generate_tables_without_pk_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Tables Without Primary Key subsection with detailed info box."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""### {self._escape_markdown(subsection.name)}

*No data available*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case - all tables have PKs
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

✅ *{self._escape_markdown(no_data_message)}*
"""
        
        # Tables without PK found - show critical warning and info box
        table_count = len(log_data.rows)
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_row = "| " + " | ".join(str(col) for col in columns) + " |"
        separator_row = "| " + " | ".join("---" for _ in columns) + " |"
        
        data_rows = []
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                if i < len(row):
                    val = row[i]
                    cells.append(str(val) if val is not None else "")
                else:
                    cells.append("")
            data_rows.append("| " + " | ".join(cells) + " |")
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        info_box_md = f"""
> **[CRITICAL] CRITICAL: Found {table_count} table{"s" if table_count != 1 else ""} without primary keys!**

> ⚠️ **CRITICAL REPLICATION IMPACT:**
>
> - **Full Table Scans on Replicas:** Every UPDATE/DELETE on these tables causes a FULL TABLE SCAN on replicas to locate the row. On large tables, this causes severe performance degradation and replication lag.
> - **Row-Based Replication (RBR):** With binlog_format=ROW (default in MySQL 8.0+), replicas must scan entire tables to find matching rows. This can make replicas orders of magnitude slower than the source.
> - **Semi-Sync Replication:** Without PKs, semi-sync replication can timeout waiting for replica acknowledgment due to slow replica processing, causing source to fall back to async mode.
> - **Replication Lag:** Large tables without PKs can cause hours or days of replication lag, making replicas unusable for reads.
> - **Table Scan Avoidance:** MySQL 8.0.18+ has optimizations (slave_rows_search_algorithms) but they are NOT sufficient - primary keys are still required!
> - **Immediate Action Required:** Add primary keys to ALL tables: `ALTER TABLE table_name ADD PRIMARY KEY (column);`

> **Workaround Options:**
>
> - **Option 1 (Best):** Add a proper primary key based on business logic
> - **Option 2 (Temporary):** Add an invisible auto-increment column (MySQL 8.0.23+): `ALTER TABLE t ADD COLUMN _pk BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE PRIMARY KEY;`
> - **Option 3 (Last Resort):** Use a unique index as a substitute, though not as effective as a true PK

> **References:**
> - [MySQL: Replication and Table Scan Avoidance](https://dev.mysql.com/doc/refman/8.0/en/table-scan-avoidance.html)
> - [MySQL: Row-Based Replication Row Searches](https://dev.mysql.com/doc/mysql-replication-excerpt/8.0/en/replication-features-row-searches.html)
> - [Jean-François Gagné: Danger of No PK with RBR](https://jfg-mysql.blogspot.com/2017/08/danger-no-pk-with-RBR-and-mariadb-protection.html)
> - [Percona: Attack No-PK Replication Lag with Invisible Columns](https://www.percona.com/blog/attack-no-pk-replication-lag-with-mysql-percona-server-8-invisible-columns/)

> **Prevention (MySQL 8.0.13+):**
>
> - **Require PKs:** Set `sql_require_primary_key=ON` to prevent creating tables without PKs (MySQL 8.0.13+)
> - **Auto-Generate PKs:** Set `sql_generate_invisible_primary_key=ON` to automatically add invisible PKs (MySQL 8.0.30+)
> - **Current Settings:** sql_require_primary_key=0, sql_generate_invisible_primary_key=0

> **References:**
> - [MySQL: Generated Invisible Primary Keys (GIPKs)](https://dev.mysql.com/doc/refman/8.0/en/create-table-gipks.html)
> - [MySQL: sql_generate_invisible_primary_key](https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_sql_generate_invisible_primary_key)
> - [MySQL: sql_require_primary_key](https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_sql_require_primary_key)
> - [Percona: Restrict Tables Without Primary Keys](https://www.percona.com/blog/explore-the-new-feature-of-mysql-to-restrict-users-from-creating-a-table-without-a-primary-key/)
> - [Percona: Tuning InnoDB Primary Keys](https://www.percona.com/blog/tuning-innodb-primary-keys/)
> - [Percona: Primary Key Models and Disk Usage Impact](https://www.percona.com/blog/illustrating-primary-key-models-in-innodb-and-their-impact-on-disk-usage/)
"""
        
        return f"""### {self._escape_markdown(subsection.name)}

{info_box_md}

{table_md}
"""
    
    def _generate_unused_indexes_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Unused Indexes subsection with warning note when indexes found."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""### {self._escape_markdown(subsection.name)}

*No data available*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f'\n> {self._escape_markdown(subsection.note)}\n'
        
        # Handle no data case - no unused indexes
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}
{note_md}
*{self._escape_markdown(no_data_message)}*
"""
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_row = '| ' + ' | '.join(self._escape_markdown(str(col)) for col in columns) + ' |'
        separator_row = '|' + '|'.join('---' for _ in columns) + '|'
        
        data_rows = []
        for row in limited_rows:
            cells = []
            for i in range(len(columns)):
                if i < len(row):
                    cells.append(self._escape_markdown(self._format_cell_value(row[i])))
                else:
                    cells.append('')
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        table_md = f"{header_row}\n{separator_row}\n" + '\n'.join(data_rows)
        
        # Add truncation notice if needed
        truncation_md = ''
        if was_truncated:
            truncation_md = f'\n*{self._limit_enforcer.get_truncation_message(total_count)}*\n'
        
        # Warning note for safe index removal
        warning_md = """
> ⚠️ **Important - Safe Index Removal Process:**
>
> **DO NOT drop indexes immediately!** Indexes may be used infrequently (daily/weekly/monthly reports, batch jobs, etc.).
>
> **Recommended approach (MySQL 8.0+):**
>
> 1. Make the index INVISIBLE first: `ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE;`
> 2. Monitor for several weeks/months to ensure no queries break
> 3. If no issues, drop the index: `ALTER TABLE schema.table DROP INDEX index_name;`
> 4. If queries break, make visible again: `ALTER TABLE schema.table ALTER INDEX index_name VISIBLE;`
>
> **Why:** Invisible indexes are maintained by MySQL but not used by the optimizer. This allows safe testing without the risk of losing the index structure. Available in MySQL 8.0+ and Aurora MySQL 2.x+.
"""
        
        return f"""### {self._escape_markdown(subsection.name)}
{note_md}
{table_md}
{truncation_md}
{warning_md}
"""
    
    def _generate_locked_accounts_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Locked Accounts subsection with locked_connects warning."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""### {self._escape_markdown(subsection.name)}

*No data available*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Add note if provided
        note_md = ''
        if subsection.note:
            note_md = f'> {self._escape_markdown(subsection.note)}\n\n'
        
        # Handle no data case - no locked accounts
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

{note_md}*{self._escape_markdown(no_data_message)}*
"""
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Generate table
        header_row = '| ' + ' | '.join(self._escape_table_cell(str(col)) for col in columns) + ' |'
        separator_row = '|' + '|'.join('---' for _ in columns) + '|'
        
        data_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                if col in column_map and column_map[col] < len(row):
                    cells.append(self._escape_table_cell(str(row[column_map[col]]) if row[column_map[col]] is not None else ''))
                else:
                    cells.append('')
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        # Check locked_connects status and add warning if > 0
        warning_md = ''
        if 'locked_connects_status' in parsed_logs:
            status_data = parsed_logs['locked_connects_status']
            if status_data.success and status_data.rows:
                for row in status_data.rows:
                    if len(row) >= 2 and str(row[0]).lower() == 'locked_connects':
                        try:
                            locked_count = int(row[1])
                            if locked_count > 0:
                                warning_md = f'\n> ⚠️ The number of attempts to connect to locked user accounts - **{locked_count:,}**\n'
                        except (ValueError, TypeError):
                            pass
                        break
        
        return f"""### {self._escape_markdown(subsection.name)}

{note_md}{table_md}
{warning_md}"""
    
    def _generate_ssl_config_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for SSL Configuration subsection with require_secure_transport warning."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""### {self._escape_markdown(subsection.name)}

*No data available*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Generate table
        header_row = '| ' + ' | '.join(self._escape_table_cell(str(col)) for col in columns) + ' |'
        separator_row = '|' + '|'.join('---' for _ in columns) + '|'
        
        # Check for require_secure_transport value while building rows
        require_secure_transport_off = False
        data_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                if col in column_map and column_map[col] < len(row):
                    cells.append(self._escape_table_cell(str(row[column_map[col]]) if row[column_map[col]] is not None else ''))
                else:
                    cells.append('')
            data_rows.append('| ' + ' | '.join(cells) + ' |')
            
            # Check if this row is require_secure_transport and if it's OFF
            if len(row) >= 2:
                var_name = str(row[0]).lower() if row[0] else ''
                var_value = str(row[1]).upper() if row[1] else ''
                if var_name == 'require_secure_transport' and var_value == 'OFF':
                    require_secure_transport_off = True
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        # Add warning if require_secure_transport is OFF
        warning_md = ''
        if require_secure_transport_off:
            warning_md = '\n> ⚠️ Enable `require_secure_transport=ON` to enforce SSL/TLS for all connections. This prevents unencrypted connections that could expose sensitive data.\n'
        
        return f"""### {self._escape_markdown(subsection.name)}

{table_md}
{warning_md}"""
    
    def _generate_ssl_connection_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for SSL Connection Status subsection with metric display."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Extract values from the first row
        row = log_data.rows[0]
        total_connections = row[0] if len(row) > 0 else 0
        ssl_connections = row[1] if len(row) > 1 else 0
        non_ssl_connections = row[2] if len(row) > 2 else 0
        
        # Format values
        total_str = f"{int(total_connections):,}" if total_connections is not None else "0"
        ssl_str = f"{int(ssl_connections):,}" if ssl_connections is not None else "0"
        non_ssl_str = f"{int(non_ssl_connections):,}" if non_ssl_connections is not None else "0"
        
        return f"""### {self._escape_markdown(subsection.name)}

| Total Connections | SSL/TLS Connections | Non-SSL Connections |
|---|---|---|
| {total_str} | {ssl_str} | {non_ssl_str} |
"""
    
    def _generate_aurora_memory_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Aurora Memory Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        
        data_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    cell_value = row[col_idx]
                    cells.append(self._escape_table_cell(str(cell_value) if cell_value is not None else ''))
                else:
                    cells.append('')
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check aurora_oom_status
        oom_status = values.get('aurora_oom_status', '')
        if oom_status == 'RESERVED':
            recommendations.append('⚠️ **Memory pressure detected:** aurora_oom_status is RESERVED. OOM avoidance is active. Review long-running queries and connections. Consider scaling up instance class.')
        
        # Check Aurora_reserved_mem_exceeded_incidents
        incidents = values.get('Aurora_reserved_mem_exceeded_incidents', '0')
        try:
            incidents_count = int(incidents)
            if incidents_count > 0:
                recommendations.append(f'⚠️ **Memory limits exceeded:** Aurora_reserved_mem_exceeded_incidents = {incidents_count}. Review peak usage times, check for connection leaks, and consider larger instance.')
        except (ValueError, TypeError):
            pass
        
        # Check aurora_oom_avoidance_recovery_state
        recovery_state = values.get('aurora_oom_avoidance_recovery_state', '')
        if recovery_state == 'ACTIVE':
            recommendations.append('⚠️ **OOM recovery active:** aurora_oom_avoidance_recovery_state is ACTIVE. The system is actively recovering from memory pressure.')
        
        # Check Aurora_lockmgr_memory_used (warn if > 100MB)
        lockmgr_mem = values.get('Aurora_lockmgr_memory_used', '0')
        try:
            lockmgr_bytes = int(lockmgr_mem)
            if lockmgr_bytes > 100 * 1024 * 1024:  # 100MB
                lockmgr_mb = lockmgr_bytes / (1024 * 1024)
                recommendations.append(f'⚠️ **High lock manager memory:** Aurora_lockmgr_memory_used = {lockmgr_mb:.1f} MB. Check for long-running transactions or lock contention.')
        except (ValueError, TypeError):
            pass
        
        # Check aurora_temptable_ram_allocation (warn if > 1GB)
        temptable_mem = values.get('aurora_temptable_ram_allocation', '0')
        try:
            temptable_bytes = int(temptable_mem)
            if temptable_bytes > 1024 * 1024 * 1024:  # 1GB
                temptable_gb = temptable_bytes / (1024 * 1024 * 1024)
                recommendations.append(f'⚠️ **High temporary table memory:** aurora_temptable_ram_allocation = {temptable_gb:.2f} GB. Review queries creating temp tables and optimize or add indexes.')
        except (ValueError, TypeError):
            pass
        
        # Build recommendations markdown
        recommendations_md = ''
        if recommendations:
            recommendations_md = '\n**Recommendations:**\n\n' + '\n'.join(f'- {rec}' for rec in recommendations) + '\n'
        else:
            recommendations_md = '\n✓ All Aurora memory metrics are within normal ranges.\n'
        
        # Add note
        note_md = ''
        if subsection.note:
            note_md = f'> {self._escape_markdown(subsection.note)}\n\n'
        
        return f"""### {self._escape_markdown(subsection.name)}

{note_md}{table_md}
{recommendations_md}"""
    
    def _generate_aurora_pq_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Aurora Parallel Query Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        
        data_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    cell_value = row[col_idx]
                    cells.append(self._escape_table_cell(str(cell_value) if cell_value is not None else ''))
                else:
                    cells.append('')
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check Aurora_pq_request_throttled
        throttled = values.get('Aurora_pq_request_throttled', '0')
        try:
            throttled_count = int(throttled)
            if throttled_count > 0:
                recommendations.append(f'⚠️ **PQ throttling detected:** Aurora_pq_request_throttled = {throttled_count}. Parallel query concurrency limit reached. Consider scaling to a larger instance class for more concurrent PQ sessions.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_failed
        failed = values.get('Aurora_pq_request_failed', '0')
        try:
            failed_count = int(failed)
            if failed_count > 0:
                recommendations.append(f'⚠️ **PQ failures detected:** Aurora_pq_request_failed = {failed_count}. Check storage layer health and query complexity.')
        except (ValueError, TypeError):
            pass
        
        # Calculate PQ success rate
        attempted = values.get('Aurora_pq_request_attempted', '0')
        executed = values.get('Aurora_pq_request_executed', '0')
        try:
            attempted_count = int(attempted)
            executed_count = int(executed)
            if attempted_count > 0:
                success_rate = (executed_count / attempted_count) * 100
                if success_rate < 70:
                    recommendations.append(f'⚠️ **Low PQ success rate:** {success_rate:.1f}% ({executed_count}/{attempted_count}). Many queries are not using parallel query. Review query patterns and table sizes.')
                elif success_rate < 90:
                    recommendations.append(f'ℹ️ **PQ success rate:** {success_rate:.1f}% ({executed_count}/{attempted_count}). Some queries are not using parallel query.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_not_chosen_long_trx
        long_trx = values.get('Aurora_pq_request_not_chosen_long_trx', '0')
        try:
            long_trx_count = int(long_trx)
            if long_trx_count > 0:
                recommendations.append(f'ℹ️ **Long transactions affecting PQ:** Aurora_pq_request_not_chosen_long_trx = {long_trx_count}. Long-running transactions prevent parallel query. Review transaction management.')
        except (ValueError, TypeError):
            pass
        
        # Build recommendations markdown
        recommendations_md = ''
        if recommendations:
            recommendations_md = '\n**Analysis:**\n\n' + '\n'.join(f'- {rec}' for rec in recommendations) + '\n'
        else:
            # Check if PQ is being used at all
            try:
                attempted_count = int(values.get('Aurora_pq_request_attempted', '0'))
                if attempted_count > 0:
                    recommendations_md = '\n✓ Aurora parallel query is operating normally with no issues detected.\n'
                else:
                    recommendations_md = '\nℹ️ No parallel query requests have been attempted. This may be normal if parallel query is disabled or workload doesn\'t benefit from it.\n'
            except (ValueError, TypeError):
                pass
        
        # Add note
        note_md = ''
        if subsection.note:
            note_md = f'> {self._escape_markdown(subsection.note)}\n\n'
        
        return f"""### {self._escape_markdown(subsection.name)}

{note_md}{table_md}
{recommendations_md}"""
    
    def _generate_aurora_relay_log_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate Markdown for Aurora Relay Log Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""### {self._escape_markdown(subsection.name)}

**Error:** {self._escape_markdown(log_data.error or "Unknown error")}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""### {self._escape_markdown(subsection.name)}

*{self._escape_markdown(no_data_message)}*
"""
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        header_row = '| ' + ' | '.join(columns) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(columns)) + '|'
        
        data_rows = []
        for row in log_data.rows:
            cells = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    cell_value = row[col_idx]
                    cells.append(self._escape_table_cell(str(cell_value) if cell_value is not None else ''))
                else:
                    cells.append('')
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        table_md = f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check Aurora_in_memory_relaylog_status
        relay_status = values.get('Aurora_in_memory_relaylog_status', '')
        disabled_reason = values.get('Aurora_in_memory_relaylog_disabled_reason', '')
        if relay_status.upper() == 'DISABLED':
            reason_text = f' Reason: {disabled_reason}.' if disabled_reason else ''
            recommendations.append(f'⚠️ **In-memory relay log disabled:**{reason_text} This may impact replica performance. Consider enabling if supported.')
        
        # Check Aurora_in_memory_relaylog_fallback_count
        fallback_count = values.get('Aurora_in_memory_relaylog_fallback_count', '0')
        try:
            fallback_int = int(fallback_count)
            if fallback_int > 0:
                recommendations.append(f'ℹ️ **Relay log fallbacks:** Aurora_in_memory_relaylog_fallback_count = {fallback_int}. Large transactions caused fallback to persistent storage. Consider breaking up large transactions.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_in_memory_relaylog_recovery_count
        recovery_count = values.get('Aurora_in_memory_relaylog_recovery_count', '0')
        try:
            recovery_int = int(recovery_count)
            if recovery_int > 0:
                recommendations.append(f'ℹ️ **Relay log recoveries:** Aurora_in_memory_relaylog_recovery_count = {recovery_int}. Relay log recovered from fallback state {recovery_int} times.')
        except (ValueError, TypeError):
            pass
        
        # Build recommendations markdown
        recommendations_md = ''
        if recommendations:
            recommendations_md = '\n**Analysis:**\n\n' + '\n'.join(f'- {rec}' for rec in recommendations) + '\n'
        else:
            if relay_status.upper() == 'ENABLED':
                recommendations_md = '\n✓ Aurora in-memory relay log is enabled and operating normally.\n'
        
        # Add note
        note_md = ''
        if subsection.note:
            note_md = f'> {self._escape_markdown(subsection.note)}\n\n'
        
        return f"""### {self._escape_markdown(subsection.name)}

{note_md}{table_md}
{recommendations_md}"""
    
    def _build_purge_thread_activity_md(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build Markdown for Purge Thread Activity table."""
        if 'purge_thread_activity' not in parsed_logs:
            return """#### Purge Thread Activity

*Purge thread information not available*
"""
        
        log_data = parsed_logs['purge_thread_activity']
        if not log_data.success or not log_data.rows or not log_data.columns:
            return """#### Purge Thread Activity

*Purge thread information not available*
"""
        
        # Build table
        headers = ['Thread ID', 'Thread Name', 'Thread Type', 'State']
        header_row = '| ' + ' | '.join(headers) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(headers)) + '|'
        
        data_rows = []
        for row in log_data.rows:
            cells = [self._escape_table_cell(str(cell) if cell is not None else '') for cell in row]
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        return f"""#### Purge Thread Activity

*Shows current purge thread status from Performance Schema.*

{header_row}
{separator_row}
{chr(10).join(data_rows)}
"""
    
    def _build_purge_configuration_md(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build Markdown for Purge Configuration table."""
        if 'purge_configuration' not in parsed_logs:
            return """#### Purge Configuration

*Purge configuration not available*
"""
        
        log_data = parsed_logs['purge_configuration']
        if not log_data.success or not log_data.rows or not log_data.columns:
            return """#### Purge Configuration

*Purge configuration not available*
"""
        
        # Build table
        headers = ['Parameter', 'Value', 'Recommendation']
        header_row = '| ' + ' | '.join(headers) + ' |'
        separator_row = '|' + '|'.join(['---'] * len(headers)) + '|'
        
        data_rows = []
        for row in log_data.rows:
            cells = [self._escape_table_cell(str(cell) if cell is not None else '') for cell in row]
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        return f"""#### Purge Configuration

*Current purge-related server variables.*

{header_row}
{separator_row}
{chr(10).join(data_rows)}
"""
    
    def _build_purge_performance_metrics_md(self, parsed_logs: Dict[str, ParsedLogFile]) -> str:
        """Build Markdown for Purge Performance Metrics."""
        if 'purge_performance_metrics' not in parsed_logs:
            return """#### Purge Performance Metrics

*Purge performance metrics not available*
"""
        
        log_data = parsed_logs['purge_performance_metrics']
        if not log_data.success or not log_data.rows or not log_data.columns:
            return """#### Purge Performance Metrics

*Purge performance metrics not available*
"""
        
        row = log_data.rows[0]
        purge_threads = row[0] if len(row) > 0 else 'N/A'
        undo_tablespaces = row[1] if len(row) > 1 else 'N/A'
        active_undo_tablespaces = row[2] if len(row) > 2 else 'N/A'
        
        return f"""#### Purge Performance Metrics

*Key metrics evaluating purge health and performance.*

| Purge Threads | Undo Tablespaces | Active Undo Tablespaces |
|:-------------:|:----------------:|:-----------------------:|
| {purge_threads} | {undo_tablespaces} | {active_undo_tablespaces} |
"""
    
    def _generate_table(
        self,
        display_columns: List[Tuple[str, Optional[str]]],
        rows: Optional[List[Tuple[Any, ...]]],
        column_map: Dict[str, int],
        source_columns: List[str]
    ) -> str:
        """
        Generate a Markdown table for the data.
        
        Args:
            display_columns: List of (display_name, source_column_or_expr) tuples
            rows: Data rows
            column_map: Mapping of column names to indices
            source_columns: Original column names from the query
        """
        if not rows:
            return '*No data*'
        
        # Generate header row
        header_cells = [self._escape_table_cell(col[0]) for col in display_columns]
        header_row = '| ' + ' | '.join(header_cells) + ' |'
        
        # Generate separator row
        separator_row = '|' + '|'.join(['---' for _ in display_columns]) + '|'
        
        # Generate data rows
        data_rows = []
        for row in rows:
            # Convert row tuple to dict for expression evaluation
            row_len = len(row) if row else 0
            row_dict = {col: row[idx] for col, idx in column_map.items() if idx < row_len}
            
            cells = []
            for display_name, source in display_columns:
                value = self._get_cell_value(source, row, column_map, row_dict)
                cells.append(self._format_cell_value(value))
            
            data_rows.append('| ' + ' | '.join(cells) + ' |')
        
        return f"""{header_row}
{separator_row}
{chr(10).join(data_rows)}"""
    
    def _resolve_columns(
        self,
        columns: List[Union[str, 'ComputedColumn']],
        column_map: Dict[str, int]
    ) -> List[Tuple[str, Optional[str]]]:
        """
        Resolve column definitions to (display_name, source) tuples.
        
        Returns list of tuples where:
        - display_name: The column header to show
        - source: Either a column name or an expression string (prefixed with 'expr:')
        """
        # Import here to avoid circular imports
        from diagnostic_collector_mysql.models import ComputedColumn
        
        resolved = []
        for col in columns:
            if isinstance(col, str):
                # Simple column reference
                if col in column_map:
                    resolved.append((col, col))
            elif isinstance(col, ComputedColumn):
                # Computed column
                resolved.append((col.name, f'expr:{col.expr}'))
            elif isinstance(col, dict):
                # Dict format from JSON parsing
                if 'name' in col and 'expr' in col:
                    resolved.append((col['name'], f"expr:{col['expr']}"))
        
        return resolved
    
    def _get_cell_value(
        self,
        source: Optional[str],
        row: Tuple[Any, ...],
        column_map: Dict[str, int],
        row_dict: Dict[str, Any]
    ) -> Any:
        """Get the value for a cell, handling both direct columns and expressions."""
        if source is None:
            return None
        
        if source.startswith('expr:'):
            # Evaluate expression
            expr = source[5:]
            return self._evaluate_expression(expr, row_dict)
        else:
            # Direct column reference
            if source in column_map:
                idx = column_map[source]
                return row[idx] if idx < len(row) else None
            return None
    
    def _evaluate_expression(self, expr: str, row_dict: Dict[str, Any]) -> Optional[float]:
        """
        Evaluate a computed column expression.
        
        Uses caching to avoid re-parsing the same expression multiple times.
        """
        if expr not in self._expression_cache:
            self._expression_cache[expr] = ExpressionEvaluator(expr)
        
        return self._expression_cache[expr].evaluate(row_dict)
    
    def _check_condition(
        self,
        condition: Optional[Dict[str, List[str]]],
        engine_type: Optional[str],
        min_engine_version: Optional[str] = None,
        max_engine_version: Optional[str] = None
    ) -> bool:
        """Check if a subsection condition is met (engine and version filtering)."""
        # Check engine condition
        if condition is not None:
            if 'engine' in condition and engine_type:
                allowed_engines = condition['engine']
                if engine_type not in allowed_engines:
                    return False

        # Check version conditions
        if min_engine_version is not None and max_engine_version is not None:
            if min_engine_version:
                min_version = self._parse_version(min_engine_version)
                if self._database_version < min_version:
                    return False
            if max_engine_version:
                max_version = self._parse_version(max_engine_version)
                if self._database_version > max_version:
                    return False

        return True
    
    def _get_section_overview(self, section_name: str) -> str:
        """
        Get the overview text for a section.
        
        Include Section_Overview at the start of each section
        """
        # Default overviews for common sections
        overviews = {
            'Server Information & Version Details': 'Server configuration and status information including version, uptime, and crucial configurations.',
            'Non-Default Configuration Variables': 'Variables that differ from their compiled defaults.',
            'Replication Topology & Status': 'Replication configuration, status, and lag information.',
            'Performance Metrics': 'Performance metrics reveal system load, query patterns, and potential bottlenecks. High slow query counts, connection issues, or table cache misses indicate areas needing optimization.',
            'Memory Configuration & Usage - Comprehensive Analysis': 'Memory configuration affects performance dramatically. The InnoDB buffer pool should be 70-80% of available RAM. Buffer pool hit rate >99% is ideal. Per-connection buffers multiply by max_connections, so should be configured appropriately. This section provides comprehensive memory analysis using Performance Schema instrumentation.',
            'InnoDB Engine & Redo Log': 'InnoDB redo logs record all changes for crash recovery. If redo logs completely utilized, writes operations will be stalled. History list length indicates purge lag - high values cause table bloat and slow queries. Monitor these closely!',
            'Table Analysis': 'Table statistics, sizes, and potential issues.',
            'Index Analysis': 'Index usage, redundant indexes, and optimization opportunities.',
            'Security Configuration': 'User accounts, privileges, and security configuration.',
            'Connection Management': 'Connection limits prevent new connections when reached. High connection churn indicates connection pooling issues. Monitor to prevent "Too many connections" errors.',
            'Lock Analysis': 'Lock waits, deadlocks, and contention metrics.',
            'I/O Analysis': 'I/O operations are typically the slowest part of database operations. Identifying tables and files with high I/O helps pinpoint performance bottlenecks and guides optimization efforts.',
            'Database Objects': 'Triggers, events, and stored procedures can impact performance and complicate troubleshooting. Understanding what automated processes are running helps diagnose unexpected behavior.',
            'SQL Performance': 'Query performance analysis and problematic queries.',
            'Database User Activity': 'User activity and connection patterns.',
        }
        
        return overviews.get(section_name, f'Information about {section_name}.')
    
    def _make_anchor(self, name: str) -> str:
        """
        Convert a section name to a valid Markdown anchor.
        
        GitHub-flavored Markdown converts headings to lowercase anchors
        with spaces replaced by hyphens.
        """
        # Convert to lowercase, replace spaces with hyphens, remove special chars
        anchor = name.lower()
        anchor = re.sub(r'[^a-z0-9\s-]', '', anchor)
        anchor = re.sub(r'\s+', '-', anchor)
        anchor = anchor.strip('-')
        return anchor
    
    def _escape_markdown(self, text: Any) -> str:
        """
        Escape Markdown special characters in text.
        
        Escapes: * _ ` [ ] ( ) # + - . ! | \
        """
        if text is None:
            return ''
        text = str(text)
        # Escape backslash first to avoid double-escaping
        text = text.replace('\\', '\\\\')
        # Escape other special characters
        special_chars = ['*', '_', '`', '[', ']', '(', ')', '#', '+', '-', '.', '!', '|']
        for char in special_chars:
            text = text.replace(char, '\\' + char)
        return text
    
    def _escape_table_cell(self, text: Any) -> str:
        """
        Escape text for use in a Markdown table cell.
        
        Table cells need special handling for pipe characters.
        """
        if text is None:
            return ''
        text = str(text)
        # Escape pipe characters (critical for table cells)
        text = text.replace('|', '\\|')
        # Replace newlines with spaces (tables don't support multi-line cells)
        text = text.replace('\n', ' ').replace('\r', '')
        return text
    
    def _format_cell_value(self, value: Any) -> str:
        """Format a cell value for display in a Markdown table."""
        if value is None:
            return '*NULL*'
        
        if isinstance(value, float):
            # Format floats with reasonable precision
            if value == int(value):
                return self._escape_table_cell(str(int(value)))
            return self._escape_table_cell(f'{value:.2f}')
        
        return self._escape_table_cell(str(value))
    
    def _generate_footer(self) -> str:
        """Generate the report footer."""
        return f"""---

**Diagnostic Collector for MySQL Report Complete**

Generated at: {self._timestamp}

*Review all sections carefully. Address any CRITICAL or WARNING items promptly.*

Diagnostic Collector for MySQL v{__version__} | Compatible with RDS/Aurora MySQL, Community MySQL 8.0+ and RDS MariaDB, Community MariaDB 11.4+
"""


class TextReportGenerator:
    """
    Generates plain text reports from parsed query log data.
    
    This class creates a plain text document readable in any terminal or text editor with:
    - Header with timestamp, database info, and table of contents
    - Health Overview section with critical issues and warnings
    - Sections organized per report_format.json
    - Each section has Section_Overview and subsections
    - Tables using ASCII formatting with aligned columns
    - Clear section separators using ASCII characters (=, -, etc.)
    
    Responsibilities:
    - Generate plain text report when --format text is specified
    - Include Health Overview section at the top
    - Organize report by sections defined in report_format.json
    - Include Section_Overview at the start of each section
    - Include table of contents (simple list)
    - Include timestamp and database information in report header
    """
    
    # ASCII formatting constants
    SECTION_SEPARATOR = '=' * 80
    SUBSECTION_SEPARATOR = '-' * 60
    TABLE_SEPARATOR = '-'
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        extended_checks: bool = False
    ):
        """
        Initialize the TextReportGenerator.

        Args:
            report_format: ReportFormat object defining the report structure
            collection_limit: Maximum rows per subsection (default: 50)
            timestamp: Report generation timestamp (default: current time)
            database_info: Dictionary with database information (host, version, etc.)
            extended_checks: Whether extended checks are enabled (default: False)
        """
        self._report_format = report_format
        self._limit_enforcer = CollectionLimitEnforcer(collection_limit)
        self._timestamp = timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._database_info = database_info or {}
        self._expression_cache: Dict[str, ExpressionEvaluator] = {}
        self._extended_checks = extended_checks
        # Parse database version for version filtering
        self._database_version = self._parse_version(self._database_info.get('version', ''))

    def _parse_version(self, version_string: str) -> tuple:
        """Parse version string into a tuple of integers for comparison."""
        if not version_string:
            return (0, 0, 0)
        match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', version_string)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0
            return (major, minor, patch)
        return (0, 0, 0)
    
    def _is_transaction_consumer_enabled(self, parsed_logs: Dict[str, 'ParsedLogFile']) -> bool:
        """Check if the events_transactions_current consumer is enabled in performance_schema."""
        consumer_status = parsed_logs.get('events_transactions_consumer_status')
        if consumer_status and consumer_status.success and consumer_status.rows:
            # Check if ENABLED column value is 'YES'
            if consumer_status.columns:
                try:
                    enabled_idx = consumer_status.columns.index('ENABLED')
                    return consumer_status.rows[0][enabled_idx].upper() == 'YES'
                except (ValueError, IndexError):
                    pass
        return False
    
    def _evaluate_config_warnings(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> List[Tuple[str, str]]:
        """
        Evaluate configuration parameters and return warnings for problematic values.
        
        This method delegates to the module-level evaluate_config_warnings function.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            
        Returns:
            List of (Parameter, Message) tuples for triggered warnings.
            Empty list if no warnings or query data not available.
        """
        return evaluate_config_warnings(parsed_logs)
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """
        Generate a complete plain text report from parsed log data.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            engine_type: The detected database engine type (for condition filtering)
            
        Returns:
            Complete plain text document as a string
        """
        text_parts = [
            self._generate_header(parsed_logs, engine_type),
            self._generate_table_of_contents(),
            self._generate_health_overview(parsed_logs, engine_type),
        ]
        
        # Generate each section from report_format
        for section in self._report_format.sections:
            section_text = self._generate_section(section, parsed_logs, engine_type)
            if section_text:
                text_parts.append(section_text)
        
        text_parts.append(self._generate_footer())
        
        return '\n'.join(text_parts)
    
    def _generate_header(self, parsed_logs: Optional[Dict[str, ParsedLogFile]] = None, engine_type: Optional[str] = None) -> str:
        """
        Generate the report header with timestamp and database info.
        
        Include timestamp and database information
        """
        host = self._database_info.get('host', 'Unknown')
        version = self._database_info.get('version', 'Unknown')
        engine_type = engine_type or self._database_info.get('engine_type', 'Unknown')
        
        # Helper to get value from parsed_logs
        def get_log_value(query_id: str, column_name: str) -> Optional[str]:
            if parsed_logs and query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return str(log_data.rows[0][col_idx])
                    except (ValueError, IndexError):
                        pass
            return None
        
        header_lines = [
            self.SECTION_SEPARATOR,
            '                    Diagnostic Collector for MySQL Health Report',
            self.SECTION_SEPARATOR,
            '',
            f'Generated: {self._timestamp}',
            '',
            'Database Information:',
            f'  Host:    {host}',
            f'  Version: {version}',
            f'  Engine:  {engine_type}',
        ]
        
        # Add Aurora-specific fields if Aurora MySQL
        if engine_type == 'aurora-mysql':
            aurora_server_id = get_log_value('aurora_server_id', 'aurora_server_id')
            aurora_version = get_log_value('aurora_version', 'aurora_version')
            innodb_read_only = get_log_value('server_read_only', 'innodb_read_only')
            
            if aurora_server_id:
                header_lines.append(f'  Aurora Server ID: {aurora_server_id}')
            if aurora_version:
                header_lines.append(f'  Aurora Version:   {aurora_version}')
            
            # Format read-only status
            if innodb_read_only in ('1', 'ON', 'True'):
                ro_status = "Yes (Reader)"
            else:
                ro_status = "No (Writer)"
            header_lines.append(f'  Read Only (InnoDB): {ro_status}')
        
        header_lines.extend(['', self.SECTION_SEPARATOR])
        
        return '\n'.join(header_lines)
    
    def _generate_table_of_contents(self) -> str:
        """
        Generate the table of contents as a simple list.
        
        Include table of contents (simple list)
        """
        toc_lines = [
            '',
            'TABLE OF CONTENTS',
            self.SUBSECTION_SEPARATOR,
            '',
            '  1. Health Overview',
        ]
        
        for idx, section in enumerate(self._report_format.sections, start=2):
            toc_lines.append(f'  {idx}. {section.name}')
        
        toc_lines.extend(['', self.SECTION_SEPARATOR])
        
        return '\n'.join(toc_lines)
    
    def _generate_health_overview(self, parsed_logs: Dict[str, ParsedLogFile], engine_type: Optional[str] = None) -> str:
        """
        Generate the Health Overview section.
        
        Include Health Overview section with critical issues and warnings
        """
        # Use shared health check evaluation
        health_checks = _evaluate_health_checks_common(parsed_logs, self._evaluate_config_warnings)
        
        # Count by status
        critical_count = sum(1 for h in health_checks if h[0] == 'critical')
        warning_count = sum(1 for h in health_checks if h[0] == 'warning')
        ok_count = sum(1 for h in health_checks if h[0] == 'ok')
        
        # Determine server type
        engine_type = engine_type or self._database_info.get('engine_type', 'mysql')
        if engine_type == 'aurora-mysql':
            server_type = 'Aurora'
        elif engine_type == 'mariadb':
            server_type = 'MariaDB'
        else:
            server_type = 'MySQL'
        
        # Build Aurora Cluster info if Aurora
        aurora_cluster_line = ''
        if engine_type == 'aurora-mysql' and 'aurora_cluster_info' in parsed_logs:
            log_data = parsed_logs['aurora_cluster_info']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    writer_idx = log_data.columns.index('writer_count')
                    reader_idx = log_data.columns.index('reader_count')
                    total_idx = log_data.columns.index('total_instances')
                    
                    writer_count = log_data.rows[0][writer_idx]
                    reader_count = log_data.rows[0][reader_idx]
                    total_instances = log_data.rows[0][total_idx]
                    
                    reader_label = 'reader' if int(reader_count) == 1 else 'readers'
                    aurora_cluster_line = f'  Aurora Cluster:     {total_instances} instances ({writer_count} writer, {reader_count} {reader_label})'
                except (ValueError, IndexError):
                    pass
        
        # Build health overview
        overview_lines = [
            '',
            'HEALTH OVERVIEW',
            self.SECTION_SEPARATOR,
            '',
            'Summary:',
            f'  Critical Issues:    {critical_count}',
            f'  Warnings:           {warning_count}',
            f'  OK:                 {ok_count}',
            f'  Server Type:        {server_type}',
        ]
        
        if aurora_cluster_line:
            overview_lines.append(aurora_cluster_line)
        
        overview_lines.append('')
        overview_lines.append('Health Checks:')
        overview_lines.append(self.SUBSECTION_SEPARATOR)
        overview_lines.append('')
        
        # Calculate column widths for alignment
        status_width = 12
        check_width = max(len(h[1]) for h in health_checks) + 2
        
        # Header
        overview_lines.append(f"  {'Status':<{status_width}} {'Check':<{check_width}} Details")
        overview_lines.append(f"  {'-' * status_width} {'-' * check_width} {'-' * 40}")
        
        # Add critical first, then warning, then ok
        for status, check_name, details in health_checks:
            if status == 'critical':
                overview_lines.append(f"  {'[!] CRITICAL':<{status_width}} {check_name:<{check_width}} {details}")
        for status, check_name, details in health_checks:
            if status == 'warning':
                overview_lines.append(f"  {'[*] WARNING':<{status_width}} {check_name:<{check_width}} {details}")
        for status, check_name, details in health_checks:
            if status == 'ok':
                overview_lines.append(f"  {'[+] OK':<{status_width}} {check_name:<{check_width}} {details}")
        
        overview_lines.append('')
        overview_lines.append(self.SECTION_SEPARATOR)
        
        return '\n'.join(overview_lines)
    
    def _generate_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> str:
        """
        Generate plain text for a report section.
        
        Behavior:
        - Organize report by sections defined in report_format.json
        - Include Section_Overview at the start of each section
        """
        # Generate subsections
        subsections_text = []
        has_data = False
        
        for subsection in section.subsections:
            # Check condition (engine and version filtering)
            if not self._check_condition(
                subsection.condition,
                engine_type,
                subsection.min_engine_version,
                subsection.max_engine_version
            ):
                continue
            
            subsection_text = self._generate_subsection(subsection, parsed_logs)
            if subsection_text:
                subsections_text.append(subsection_text)
                has_data = True
        
        # Skip section if no subsections have data
        if not has_data:
            return ''
        
        # Generate section overview
        overview_text = self._get_section_overview(section.name)
        
        section_lines = [
            '',
            section.name.upper(),
            self.SECTION_SEPARATOR,
            '',
            f'Overview: {overview_text}',
            '',
        ]
        
        # Add engine-specific notes for certain sections
        if section.name == 'I/O Analysis' and engine_type == 'aurora-mysql':
            section_lines.append('Note: Aurora MySQL does not instrument physical I/O (file-level bytes) the same way as Community MySQL due to its distributed storage architecture. However, logical I/O (table-level row operations) is fully instrumented and available.')
            section_lines.append('')
        
        section_lines.extend(subsections_text)
        
        # Add Transaction Dependency Tracking for Replication section (only for MySQL < 8.1)
        if section.name == 'Replication Topology & Status':
            dep_tracking_text = self._generate_transaction_dependency_tracking_text(parsed_logs)
            if dep_tracking_text:
                section_lines.append('')
                section_lines.append(dep_tracking_text)
            
            # Add Binary Logging Configuration Advice for Replication section
            logging_advice = self._generate_binary_logging_advice(parsed_logs, engine_type)
            if logging_advice:
                section_lines.append('')
                section_lines.append(logging_advice)
        
        section_lines.append(self.SECTION_SEPARATOR)
        
        return '\n'.join(section_lines)
    
    def _generate_transaction_dependency_tracking_text(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate Transaction Dependency Tracking section in plain text format.
        
        Only shown for MySQL < 8.1 (binlog_transaction_dependency_tracking was removed in 8.1+).
        """
        # Only show for MySQL < 8.1
        if self._database_version >= (8, 1, 0):
            return ''
        
        # Get dependency tracking value
        dep_tracking = 'N/A'
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    if 'binlog_transaction_dependency_tracking' in log_data.columns:
                        idx = log_data.columns.index('binlog_transaction_dependency_tracking')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            dep_tracking = str(val)
                except (ValueError, IndexError, TypeError):
                    pass
        
        lines = [
            'Transaction Dependency Tracking (Critical for Multi-Threaded Replication Performance!)',
            self.SUBSECTION_SEPARATOR,
            '',
            'Understanding WRITESET: Transaction dependency tracking determines which transactions',
            'can run in parallel on replicas. This is CRITICAL for multi-threaded replication performance!',
            '',
            'Dependency Tracking Methods:',
            '  COMMIT_ORDER      - Uses binary log group commit. Limited parallelism. Default, safe but slower.',
            '  WRITESET          - Tracks row hashes. High parallelism. Recommended for most workloads.',
            '  WRITESET_SESSION  - Like WRITESET but preserves order within same session. High parallelism.',
            '',
            'Current Configuration Analysis:',
        ]
        
        if dep_tracking == 'COMMIT_ORDER':
            lines.extend([
                f'  [!] Dependency Tracking: {dep_tracking}',
                '  [WARNING] Consider switching to WRITESET or WRITESET_SESSION for better replication',
                '  parallelism. This can significantly reduce replication lag!',
                '  Set: binlog_transaction_dependency_tracking=WRITESET and transaction_write_set_extraction=XXHASH64',
            ])
        elif dep_tracking in ('WRITESET', 'WRITESET_SESSION'):
            lines.extend([
                f'  [OK] Dependency Tracking: {dep_tracking}',
                '  Good configuration for multi-threaded replication parallelism.',
            ])
        else:
            lines.extend([
                f'  [?] Dependency Tracking: {dep_tracking}',
                '  The binlog_transaction_dependency_tracking variable is not available or not configured.',
            ])
        
        lines.extend([
            '',
            'Key Insight: WRITESET tracking allows transactions that modify different rows to execute',
            'in parallel on replicas, even if they were in different group commits on the source.',
            'This dramatically improves replication throughput and reduces lag.',
            '',
            'IMPORTANT: The binlog_transaction_dependency_tracking setting must be configured on the',
            'SOURCE/PRIMARY server, not on replicas. Replicas read the dependency information from',
            'the binary logs generated by the source.',
        ])
        
        return '\n'.join(lines)
    
    def _generate_binary_logging_advice(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> str:
        """Generate Binary Logging Configuration Advice in plain text format."""
        # Helper to get value from parsed logs
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log = parsed_logs[query_id]
                if log.success and log.rows and log.columns:
                    try:
                        idx = log.columns.index(column_name)
                        return log.rows[0][idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        # Get replication role
        replication_role = get_value('replication_role', 'replication_role', 'UNKNOWN')
        log_bin = get_value('replication_role', 'log_bin_enabled', 0)
        
        # Get sync settings
        sync_binlog = get_value('config_warnings_check', 'sync_binlog', 'N/A')
        flush_log = get_value('config_warnings_check', 'innodb_flush_log_at_trx_commit', 'N/A')
        
        # Build the advice section
        role_text = 'Source/Standalone' if replication_role != 'REPLICA' else 'Replica'
        binlog_text = 'Enabled' if log_bin in (1, '1', 'ON', True) else 'Disabled'
        binlog_rec = 'Binary logs should be enabled on source servers for replication and PITR.' if replication_role != 'REPLICA' else 'Binary logs optional on leaf replicas unless chaining.'
        
        sync_rec = 'Safest setting (ACID compliant).' if str(sync_binlog) == '1' else 'Consider sync_binlog=1 for durability.'
        flush_rec = 'Safest setting (ACID compliant).' if str(flush_log) == '1' else 'Consider innodb_flush_log_at_trx_commit=1 for durability.'
        
        lines = [
            'Binary Logging Configuration Advice',
            self.SUBSECTION_SEPARATOR,
            '',
            'Binary Logging on Replicas: Binary logs are NOT required on replicas unless the replica',
            'is also a source for another replica [chained replication] (or) binary logs data used',
            'for data streaming services (or) you need point-in-time recovery from the replica.',
            '',
            'Current Server Role and Logging Recommendation:',
            f'  Server Role:                        {role_text} - This is a {role_text.lower()} server',
            f'  Binary Logging:                     {binlog_text} - {binlog_rec}',
            f'  sync_binlog:                        {sync_binlog} - {sync_rec}',
            f'  innodb_flush_log_at_trx_commit:     {flush_log} - {flush_rec}',
            '',
            'Logging Best Practices:',
            '  - Source Servers: Enable binary logging (log_bin=ON) with sync_binlog=1 for durability',
            '  - Leaf Replicas: Binary logging optional. Disable to save I/O unless needed for chaining or PITR',
            '  - Intermediate Replicas: Enable binary logging if this replica is a source for other replicas',
            '  - ACID Compliance: Use sync_binlog=1 AND innodb_flush_log_at_trx_commit=1 for full durability',
            '  - Performance vs Durability: Lower sync values improve performance but risk data loss on crash',
        ]
        
        # Check if general_log is enabled
        try:
            general_log_value = get_value('logging_status', 'general_log', 0)
            if general_log_value in (1, '1', 'ON', True):
                lines.append('')
                lines.append('WARNING: The general query log (general_log=ON) logs ALL queries and can severely')
                lines.append('impact performance on busy systems. Use only for short-term debugging.')
        except Exception:
            pass
        
        return '\n'.join(lines)

    def _generate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for a report subsection with data table."""
        source_id = subsection.source
        
        # Check if subsection requires extended checks and they are disabled
        if subsection.requires_extended_checks and not self._extended_checks:
            # 3-tier fallback logic:
            # 1. If --extended-checks enabled -> Use innodb_trx query (handled above, we're in the disabled case)
            # 2. If fallback_source exists and transaction consumer is enabled -> Use PS query
            # 3. Else -> Display message IN PLACE OF result set
            
            if subsection.fallback_source and self._is_transaction_consumer_enabled(parsed_logs):
                # Use fallback source (performance_schema query)
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success:
                    # Use fallback columns if specified, otherwise use original columns
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    # Create a temporary subsection with fallback source
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        requires_extended_checks=False  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
            
            # No fallback available or consumer not enabled - show disabled message
            disabled_note = subsection.extended_checks_disabled_note or "Extended checks disabled. Use --extended-checks to enable."
            lines = [
                subsection.name,
                self.SUBSECTION_SEPARATOR,
            ]
            # Add note if provided
            if subsection.note:
                lines.append(f'  Note: {subsection.note}')
                lines.append('')
            lines.append(f'  [!] WARNING: {disabled_note}')
            lines.append('')
            return '\n'.join(lines)
        
        # Special handling for Configuration Warnings subsection
        if source_id == 'config_warnings_check':
            return self._generate_config_warnings_subsection(subsection, parsed_logs)
        
        # Special handling for Key Performance Counters subsection
        if source_id == 'key_performance_counters':
            return self._generate_key_performance_counters_subsection(subsection, parsed_logs)
        
        # Special handling for Transaction Summary subsection
        if source_id == 'transaction_summary':
            return self._generate_transaction_summary_subsection(subsection, parsed_logs)
        
        # Special handling for Table Cache Statistics subsection
        if source_id == 'table_cache_stats':
            return self._generate_table_cache_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Temporary Table Usage subsection
        if source_id == 'temp_table_usage':
            return self._generate_temp_table_usage_subsection(subsection, parsed_logs)
        
        # Special handling for Memory Troubleshooting Tips subsection
        if source_id == 'memory_troubleshooting_tips':
            return self._generate_memory_troubleshooting_tips_subsection(subsection)
        
        # Special handling for Aurora InnoDB Note subsection
        if source_id == 'aurora_innodb_note':
            return self._generate_aurora_innodb_note_subsection(subsection)
        
        # Special handling for InnoDB Purge Analysis subsection
        # Special handling for Unused Indexes subsection
        if source_id == 'unused_indexes':
            return self._generate_unused_indexes_subsection(subsection, parsed_logs)

        if source_id == 'innodb_purge_analysis':
            return self._generate_innodb_purge_analysis_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Engine Status Analysis subsection
        if source_id == 'innodb_engine_status_analysis':
            return self._generate_innodb_engine_status_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Analysis subsection
        if source_id == 'long_running_transactions_analysis':
            return self._generate_long_running_transactions_subsection(subsection, parsed_logs)
        
        # Special handling for Tables Without Primary Key subsection
        if source_id == 'tables_without_pk':
            return self._generate_tables_without_pk_subsection(subsection, parsed_logs)
        
        # Special handling for Locked Accounts subsection
        if source_id == 'locked_accounts':
            return self._generate_locked_accounts_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Configuration subsection
        if source_id == 'ssl_config':
            return self._generate_ssl_config_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Connection Status subsection
        if source_id == 'ssl_connection_status':
            return self._generate_ssl_connection_status_subsection(subsection, parsed_logs)
        
        # Special handling for Connection Memory Overhead subsection
        if source_id == 'connection_memory_overhead':
            return self._generate_connection_memory_overhead_subsection(subsection, parsed_logs)
        
        # Special handling for Sort & Join Buffer Memory subsection
        if source_id == 'sort_join_buffer_memory':
            return self._generate_sort_join_buffer_memory_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Buffer Pool Statistics (Detailed) subsection
        if source_id == 'innodb_buffer_pool_stats_detailed':
            return self._generate_innodb_buffer_pool_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Per-Connection Memory Buffers subsection
        if source_id == 'per_connection_memory':
            return self._generate_per_connection_memory_subsection(subsection, parsed_logs)
        
        # Special handling for Global Memory Usage - Top Components subsection
        if source_id == 'global_memory_usage_top':
            return self._generate_global_memory_usage_top_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Row Operations subsection (metric cards)
        if source_id == 'innodb_row_operations':
            return self._generate_innodb_row_operations_subsection(subsection, parsed_logs)
        
        # Special handling for Buffer Pool Hit Rate subsection (metric cards)
        if source_id == 'health_buffer_pool_hit_rate':
            return self._generate_buffer_pool_hit_rate_subsection(subsection, parsed_logs)
        
        # Special handling for All Current Transactions Summary subsection (metric cards)
        if source_id == 'current_transactions_summary':
            return self._generate_current_transactions_summary_subsection(subsection, parsed_logs)
        
        # Special handling for History List Length subsection (metric cards)
        if source_id == 'innodb_history_length':
            return self._generate_history_list_length_subsection(subsection, parsed_logs)
        
        # Special handling for Parallel Replication Worker Stats subsection
        if source_id == 'parallel_replication_worker_stats':
            return self._generate_parallel_replication_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Activity subsection
        if source_id == 'long_running_trx_recent_queries':
            return self._generate_long_running_trx_activity_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Memory Status subsection (dynamic recommendations)
        if source_id == 'aurora_memory_status':
            return self._generate_aurora_memory_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Parallel Query Status subsection (dynamic recommendations)
        if source_id == 'aurora_pq_status':
            return self._generate_aurora_pq_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Relay Log Status subsection (dynamic recommendations)
        if source_id == 'aurora_relay_log_status':
            return self._generate_aurora_relay_log_status_subsection(subsection, parsed_logs)
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            # Primary source not available - check for fallback
            if subsection.fallback_source:
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success and fallback_log.rows:
                    # Use fallback source
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        fallback_source=None  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
                elif subsection.fallback_requires_extended_checks and not self._extended_checks:
                    # Fallback requires extended checks which are disabled
                    disabled_note = subsection.extended_checks_disabled_note or "Enable --extended-checks to view this data."
                    lines = [
                        subsection.name,
                        self.SUBSECTION_SEPARATOR,
                    ]
                    if subsection.note:
                        lines.append(f'  Note: {subsection.note}')
                        lines.append('')
                    lines.append(f'  [!] WARNING: {disabled_note}')
                    lines.append('')
                    return '\n'.join(lines)
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (No data available - query not executed)
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Handle no data case - use default_note if provided
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (No matching columns found)
"""
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Generate table
        table_text = self._generate_table(
            display_columns,
            limited_rows,
            column_map,
            log_data.columns
        )
        
        # Build subsection text
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note above the table if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        subsection_lines.append(table_text)
        
        # Add truncation notice if needed
        if was_truncated:
            subsection_lines.append(f'  ({self._limit_enforcer.get_truncation_message(total_count)})')
        
        # Add result_note after the table when there are results
        if subsection.result_note and limited_rows:
            subsection_lines.append('')
            subsection_lines.append(f'  Note: {subsection.result_note}')
        
        # Add warning for Unused Indexes subsection when it has data
        if subsection.name == "Unused Indexes" and limited_rows:
            subsection_lines.append('')
            subsection_lines.append('  ⚠️ Important - Safe Index Removal Process:')
            subsection_lines.append('  DO NOT drop indexes immediately! Indexes may be used infrequently')
            subsection_lines.append('  (daily/weekly/monthly reports, batch jobs, etc.).')
            subsection_lines.append('')
            subsection_lines.append('  Recommended approach (MySQL 8.0+):')
            subsection_lines.append('  1. Make the index INVISIBLE first:')
            subsection_lines.append('     ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE;')
            subsection_lines.append('  2. Monitor for several weeks/months to ensure no queries break')
            subsection_lines.append('  3. If no issues, drop the index:')
            subsection_lines.append('     ALTER TABLE schema.table DROP INDEX index_name;')
            subsection_lines.append('  4. If queries break, make visible again:')
            subsection_lines.append('     ALTER TABLE schema.table ALTER INDEX index_name VISIBLE;')
            subsection_lines.append('')
            subsection_lines.append('  Why: Invisible indexes are maintained by MySQL but not used by the')
            subsection_lines.append('  optimizer. This allows safe testing without the risk of losing the')
            subsection_lines.append('  index structure. Available in MySQL 8.0+ and Aurora MySQL 2.x+.')
        # Add warning from subsection config if provided
        elif subsection.warning and limited_rows:
            subsection_lines.append('')
            subsection_lines.append(f'  ⚠️ {subsection.warning}')
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_config_warnings_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate plain text for the Configuration Warnings subsection.
        
        This method transforms the raw config_warnings_check query data into
        warning rows using the evaluate_config_warnings function.
        """
        # Evaluate config warnings using the module-level function
        warnings = evaluate_config_warnings(parsed_logs)
        
        if not warnings:
            no_data_message = subsection.default_note if subsection.default_note else "No configuration warnings"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Calculate column widths
        param_width = max(len('Parameter'), max(len(p) for p, _ in warnings))
        msg_width = min(60, max(len('Message'), max(len(m) for _, m in warnings)))
        
        # Cap widths
        param_width = min(param_width, 40)
        
        # Build table
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Header row
        header = f"  {'Parameter'.ljust(param_width)} | Message"
        subsection_lines.append(header)
        
        # Separator row
        separator = f"  {'-' * param_width}-+-{'-' * msg_width}"
        subsection_lines.append(separator)
        
        # Data rows
        for param, message in warnings:
            # Truncate message if too long
            display_msg = message if len(message) <= msg_width else message[:msg_width-3] + '...'
            row = f"  {param.ljust(param_width)} | {display_msg}"
            subsection_lines.append(row)
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_key_performance_counters_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate plain text for the Key Performance Counters subsection.
        
        This method transforms the raw key_performance_counters query data into
        a single row with calculated metrics.
        """
        # Transform the data using the module-level function
        transformed_rows = evaluate_key_performance_counters(parsed_logs, 0)
        
        if not transformed_rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Define the columns for display
        columns = ["QPS", "Total Queries", "Slow Queries", "Threads Conn", "Threads Run", "Aborted Conn", "Aborted Clients"]
        
        # Get the data row
        row = transformed_rows[0]
        
        # Calculate column widths
        col_widths = []
        for i, col in enumerate(columns):
            val_str = str(row[i]) if i < len(row) else ''
            col_widths.append(max(len(col), len(val_str)))
        
        # Build table
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Header row
        header_parts = [col.ljust(col_widths[i]) for i, col in enumerate(columns)]
        header = '  ' + ' | '.join(header_parts)
        subsection_lines.append(header)
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        separator = '  ' + '-+-'.join(separator_parts)
        subsection_lines.append(separator)
        
        # Data row
        data_parts = [str(row[i]).ljust(col_widths[i]) if i < len(row) else ''.ljust(col_widths[i]) for i in range(len(columns))]
        data_row = '  ' + ' | '.join(data_parts)
        subsection_lines.append(data_row)
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_transaction_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Transaction Summary subsection with metric table."""
        source_id = subsection.source
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Extract values from the row
        # Columns: total_transactions, oldest_trx_age_seconds, total_rows_locked, total_modified_rows
        row = parsed_log.rows[0]
        total_transactions = row[0] if row[0] is not None else 0
        oldest_trx_age = row[1] if row[1] is not None else 0
        total_rows_locked = row[2] if row[2] is not None else 0
        total_modified_rows = row[3] if row[3] is not None else 0
        
        # Format values
        values = [
            f"{total_transactions:,}",
            f"{oldest_trx_age:,}" if oldest_trx_age else "0",
            f"{total_rows_locked:,}",
            f"{total_modified_rows:,}"
        ]
        
        # Define the columns for display
        columns = ["Total Trx", "Oldest Trx Age (s)", "Rows Locked", "Modified Rows"]
        
        # Calculate column widths
        col_widths = []
        for i, col in enumerate(columns):
            col_widths.append(max(len(col), len(values[i])))
        
        # Build table
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Header row
        header_parts = [col.ljust(col_widths[i]) for i, col in enumerate(columns)]
        header = '  ' + ' | '.join(header_parts)
        subsection_lines.append(header)
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        separator = '  ' + '-+-'.join(separator_parts)
        subsection_lines.append(separator)
        
        # Data row
        data_parts = [values[i].ljust(col_widths[i]) for i in range(len(columns))]
        data_row = '  ' + ' | '.join(data_parts)
        subsection_lines.append(data_row)
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_innodb_row_operations_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for InnoDB Row Operations subsection with metric table."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Extract values - columns: Rows Read, Rows Inserted, Rows Updated, Rows Deleted
        row = parsed_log.rows[0]
        rows_read = row[0] if row[0] is not None else 0
        rows_inserted = row[1] if row[1] is not None else 0
        rows_updated = row[2] if row[2] is not None else 0
        rows_deleted = row[3] if row[3] is not None else 0
        
        # Format values
        values = [f"{rows_read:,}", f"{rows_inserted:,}", f"{rows_updated:,}", f"{rows_deleted:,}"]
        columns = ["Rows Read", "Rows Inserted", "Rows Updated", "Rows Deleted"]
        
        # Calculate column widths
        col_widths = [max(len(col), len(values[i])) for i, col in enumerate(columns)]
        
        # Build table
        subsection_lines = [subsection.name, self.SUBSECTION_SEPARATOR]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f"  Note: {subsection.note}")
            subsection_lines.append('')
        
        # Header row
        header_parts = [col.ljust(col_widths[i]) for i, col in enumerate(columns)]
        subsection_lines.append('  ' + ' | '.join(header_parts))
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        subsection_lines.append('  ' + '-+-'.join(separator_parts))
        
        # Data row
        data_parts = [values[i].ljust(col_widths[i]) for i in range(len(columns))]
        subsection_lines.append('  ' + ' | '.join(data_parts))
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_buffer_pool_hit_rate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Buffer Pool Hit Rate subsection with metric table."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Extract value - column: buffer_pool_hit_rate
        row = parsed_log.rows[0]
        hit_rate = str(row[0]) if row[0] is not None else "N/A"
        
        columns = ["Buffer Pool Hit Rate"]
        values = [hit_rate]
        
        # Calculate column widths
        col_widths = [max(len(col), len(values[i])) for i, col in enumerate(columns)]
        
        # Build table
        subsection_lines = [subsection.name, self.SUBSECTION_SEPARATOR]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f"  Note: {subsection.note}")
            subsection_lines.append('')
        
        # Header row
        header_parts = [col.ljust(col_widths[i]) for i, col in enumerate(columns)]
        subsection_lines.append('  ' + ' | '.join(header_parts))
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        subsection_lines.append('  ' + '-+-'.join(separator_parts))
        
        # Data row
        data_parts = [values[i].ljust(col_widths[i]) for i in range(len(columns))]
        subsection_lines.append('  ' + ' | '.join(data_parts))
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_current_transactions_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for All Current Transactions Summary subsection with metric table."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Extract values - columns: Total Active Transactions, Oldest Transaction Age (seconds)
        row = parsed_log.rows[0]
        total_active = row[0] if row[0] is not None else 0
        oldest_age = row[1] if row[1] is not None else 0
        
        columns = ["Total Active Transactions", "Oldest Transaction Age (seconds)"]
        values = [f"{total_active:,}", f"{oldest_age:,}"]
        
        # Calculate column widths
        col_widths = [max(len(col), len(values[i])) for i, col in enumerate(columns)]
        
        # Build table
        subsection_lines = [subsection.name, self.SUBSECTION_SEPARATOR]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f"  Note: {subsection.note}")
            subsection_lines.append('')
        
        # Header row
        header_parts = [col.ljust(col_widths[i]) for i, col in enumerate(columns)]
        subsection_lines.append('  ' + ' | '.join(header_parts))
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        subsection_lines.append('  ' + '-+-'.join(separator_parts))
        
        # Data row
        data_parts = [values[i].ljust(col_widths[i]) for i in range(len(columns))]
        subsection_lines.append('  ' + ' | '.join(data_parts))
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_history_list_length_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for History List Length subsection with metric table."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Extract value - column: history_list_length
        row = parsed_log.rows[0]
        history_length = row[0] if row[0] is not None else 0
        
        columns = ["History List Length"]
        values = [f"{history_length:,}"]
        
        # Calculate column widths
        col_widths = [max(len(col), len(values[i])) for i, col in enumerate(columns)]
        
        # Build table
        subsection_lines = [subsection.name, self.SUBSECTION_SEPARATOR]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f"  Note: {subsection.note}")
            subsection_lines.append('')
        
        # Header row
        header_parts = [col.ljust(col_widths[i]) for i, col in enumerate(columns)]
        subsection_lines.append('  ' + ' | '.join(header_parts))
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        subsection_lines.append('  ' + '-+-'.join(separator_parts))
        
        # Data row
        data_parts = [values[i].ljust(col_widths[i]) for i in range(len(columns))]
        subsection_lines.append('  ' + ' | '.join(data_parts))
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_parallel_replication_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Parallel Replication Worker Stats subsection with interpretation."""
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return log_data.rows[0][col_idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        replication_role = get_value('replication_role', 'replication_role', 'STANDALONE')
        if replication_role != 'REPLICA':
            return ''
        
        parallel_workers = 0
        parallel_type = 'N/A'
        dep_tracking = 'N/A'
        
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    # Check replica_parallel_workers first (MySQL 8.0.26+), fallback to slave_parallel_workers
                    if 'replica_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    if parallel_workers == 0 and 'slave_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    
                    # Check replica_parallel_type first, fallback to slave_parallel_type
                    if 'replica_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    if parallel_type == 'N/A' and 'slave_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    
                    if 'binlog_transaction_dependency_tracking' in log_data.columns:
                        idx = log_data.columns.index('binlog_transaction_dependency_tracking')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            dep_tracking = str(val)
                except (ValueError, IndexError, TypeError):
                    pass
        
        if parallel_workers <= 1:
            return ''
        
        source_id = subsection.source
        if source_id not in parsed_logs:
            return ''
        
        log_data = parsed_logs[source_id]
        if not log_data.success:
            return (
                f"{subsection.name}\n{self.SUBSECTION_SEPARATOR}\n"
                f"  ERROR: {log_data.error or 'Unknown error'}\n"
            )
        
        # Workers are configured but no per-worker rows came back. THREAD_ID is NULL
        # in replication_applier_status_by_worker while the applier is stopped, so
        # the join yields nothing. Emitting the note keeps "could not measure" apart
        # from "distribution is fine", which an empty section cannot express.
        if not log_data.rows or not log_data.columns:
            no_data_message = (
                subsection.default_note if subsection.default_note else "No data returned"
            )
            return (
                f"{subsection.name}\n{self.SUBSECTION_SEPARATOR}\n"
                f"  ({no_data_message})\n"
            )
        
        lines = [subsection.name, self.SUBSECTION_SEPARATOR]
        if subsection.note:
            lines.append(f"  Note: {subsection.note}")
            lines.append('')
        
        try:
            channel_idx = log_data.columns.index('CHANNEL_NAME')
            thread_idx = log_data.columns.index('replica_worker_thread_id')
            trx_idx = log_data.columns.index('number_of_trx_executed')
            pct_idx = log_data.columns.index('percent_of_trx_executed_by_worker')
            
            cols = ['Channel', 'Thread ID', 'Transactions', '% of Total']
            widths = [max(10, len(cols[0])), max(10, len(cols[1])), max(12, len(cols[2])), max(10, len(cols[3]))]
            
            lines.append('  ' + ' | '.join(c.ljust(widths[i]) for i, c in enumerate(cols)))
            lines.append('  ' + '-+-'.join('-' * w for w in widths))
            
            worker_stats = []
            for row in log_data.rows:
                channel = str(row[channel_idx]) if row[channel_idx] else '(default)'
                thread_id = str(row[thread_idx])
                trx_count = str(row[trx_idx])
                pct = str(row[pct_idx]) + '%'
                worker_stats.append({'channel': channel, 'trx_count': int(row[trx_idx]) if row[trx_idx] else 0, 'pct': float(row[pct_idx]) if row[pct_idx] else 0.0})
                lines.append('  ' + ' | '.join([channel.ljust(widths[0]), thread_id.ljust(widths[1]), trx_count.ljust(widths[2]), pct.ljust(widths[3])]))
            
            lines.append('')
            
            # Add interpretation
            if worker_stats:
                lines.extend(self._generate_mts_interpretation_text(worker_stats, parallel_workers, parallel_type, dep_tracking))
        except (ValueError, IndexError) as e:
            lines.append(f"  Error: {str(e)}")
        
        lines.append('')
        return '\n'.join(lines)
    
    def _generate_mts_interpretation_text(self, worker_stats: list, parallel_workers: int, parallel_type: str, dep_tracking: str) -> list:
        """Generate plain text interpretation of MTS worker efficiency."""
        lines = ['Worker Efficiency Analysis', '-' * 30]
        
        channels = {}
        for stat in worker_stats:
            channel = stat['channel']
            if channel not in channels:
                channels[channel] = []
            channels[channel].append(stat)
        
        for channel, stats in channels.items():
            total_trx = sum(s['trx_count'] for s in stats)
            if total_trx == 0:
                lines.append(f"  Channel {channel}: No transactions yet")
                continue
            
            percentages = [s['pct'] for s in stats]
            max_pct, min_pct = max(percentages), min(percentages)
            spread = max_pct - min_pct
            active_workers = sum(1 for p in percentages if p > 5.0)
            
            lines.append(f"  Channel: {channel}")
            lines.append(f"    Total transactions: {total_trx:,}")
            lines.append(f"    Active workers: {active_workers} of {len(stats)}")
            lines.append(f"    Distribution spread: {spread:.1f}%")
            lines.append('')
            lines.append("  Configuration:")
            pt_status = '[OK]' if parallel_type == 'LOGICAL_CLOCK' else '[!]'
            lines.append(f"    {pt_status} replica_parallel_type: {parallel_type}")
            if dep_tracking != 'N/A' and self._database_version < (8, 1, 0):
                dt_status = '[OK]' if dep_tracking in ('WRITESET', 'WRITESET_SESSION') else '[!]'
                lines.append(f"    {dt_status} binlog_transaction_dependency_tracking: {dep_tracking}")
            lines.append(f"    [OK] replica_parallel_workers: {parallel_workers}")
            lines.append('')
            
            if spread < 20 and active_workers >= len(stats) * 0.8:
                lines.append("  [OK] Good Distribution: Transactions well-distributed.")
            elif spread > 50 or active_workers < len(stats) * 0.5:
                lines.append("  [!] Uneven Distribution: Workload concentrated on fewer workers.")
                lines.append('')
                lines.append("  Recommended Actions:")
                n = 1
                if parallel_type != 'LOGICAL_CLOCK':
                    lines.append(f"    {n}. Set replica_parallel_type=LOGICAL_CLOCK (stop replica first)")
                    n += 1
                if dep_tracking != 'N/A' and self._database_version < (8, 1, 0) and dep_tracking not in ('WRITESET', 'WRITESET_SESSION'):
                    lines.append(f"    {n}. Enable WRITESET on SOURCE server")
                    n += 1
                lines.append(f"    {n}. Review workload for hot spots or sequential dependencies")
                lines.append(f"    {n+1}. Monitor after changes and re-run collector")
            else:
                lines.append("  [i] Moderate Distribution: Acceptable but could be improved.")
            lines.append('')
        
        return lines
    
    def _generate_table_cache_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """
        Generate plain text for the Table Cache Statistics subsection.
        
        This method transforms the raw table_cache_stats and table_cache_config
        query data into rows with Metric, Value, Explanation format.
        """
        # Transform the data using the module-level function
        transformed_rows = evaluate_table_cache_stats(parsed_logs)
        
        if not transformed_rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Define the columns for display
        columns = ["Metric", "Value", "Explanation"]
        
        # Calculate column widths
        col_widths = [len(col) for col in columns]
        for row in transformed_rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(val)))
        
        # Build table
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Header row
        header_parts = [col.ljust(col_widths[i]) for i, col in enumerate(columns)]
        header = '  ' + ' | '.join(header_parts)
        subsection_lines.append(header)
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        separator = '  ' + '-+-'.join(separator_parts)
        subsection_lines.append(separator)
        
        # Data rows
        for row in transformed_rows:
            data_parts = [str(row[i]).ljust(col_widths[i]) for i in range(len(columns))]
            data_row = '  ' + ' | '.join(data_parts)
            subsection_lines.append(data_row)
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_temp_table_usage_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Temporary Table Usage subsection with metric display."""
        # Transform the data using the module-level function
        temp_data = evaluate_temp_table_usage(parsed_logs)
        
        if not temp_data:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  {no_data_message}
"""
        
        # Format the values
        created_tmp_tables = f"{temp_data['created_tmp_tables']:,}"
        created_tmp_disk_tables = f"{temp_data['created_tmp_disk_tables']:,}"
        disk_tmp_table_pct = f"{temp_data['disk_tmp_table_pct']:.2f}%"
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Display as metric cards in text format
        subsection_lines.append(f'  Created Tmp Tables:      {created_tmp_tables}')
        subsection_lines.append(f'  Created Tmp Disk Tables: {created_tmp_disk_tables}')
        subsection_lines.append(f'  Disk Tmp Table %:        {disk_tmp_table_pct}')
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_connection_memory_overhead_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Connection Memory Overhead subsection with metric display."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  {no_data_message}
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  {no_data_message}
"""
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        current_conn = str(row[col_map.get('Current Connections', 0)]) if 'Current Connections' in col_map else 'N/A'
        max_conn = str(row[col_map.get('Max Connections', 1)]) if 'Max Connections' in col_map else 'N/A'
        avg_mem = str(row[col_map.get('Avg Memory per Connection', 2)]) if 'Avg Memory per Connection' in col_map else 'N/A'
        total_mem = str(row[col_map.get('Total Connection Memory', 3)]) if 'Total Connection Memory' in col_map else 'N/A'
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Display as metric cards in text format
        subsection_lines.append(f'  Current Connections:        {current_conn}')
        subsection_lines.append(f'  Max Connections:            {max_conn}')
        subsection_lines.append(f'  Avg Memory per Connection:  {avg_mem}')
        subsection_lines.append(f'  Total Connection Memory:    {total_mem}')
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_sort_join_buffer_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Sort & Join Buffer Memory subsection with metric display."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  {no_data_message}
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  {no_data_message}
"""
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        sort_buffer = str(row[col_map.get('sort_buffer_size', 0)]) if 'sort_buffer_size' in col_map else 'N/A'
        join_buffer = str(row[col_map.get('join_buffer_size', 1)]) if 'join_buffer_size' in col_map else 'N/A'
        read_buffer = str(row[col_map.get('read_buffer_size', 2)]) if 'read_buffer_size' in col_map else 'N/A'
        read_rnd_buffer = str(row[col_map.get('read_rnd_buffer_size', 3)]) if 'read_rnd_buffer_size' in col_map else 'N/A'
        sort_merge = str(row[col_map.get('Sort Merge Passes', 4)]) if 'Sort Merge Passes' in col_map else 'N/A'
        sort_range = str(row[col_map.get('Sort Range', 5)]) if 'Sort Range' in col_map else 'N/A'
        sort_scan = str(row[col_map.get('Sort Scan', 6)]) if 'Sort Scan' in col_map else 'N/A'
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Display as metric cards in text format
        subsection_lines.append(f'  sort_buffer_size:      {sort_buffer}')
        subsection_lines.append(f'  join_buffer_size:      {join_buffer}')
        subsection_lines.append(f'  read_buffer_size:      {read_buffer}')
        subsection_lines.append(f'  read_rnd_buffer_size:  {read_rnd_buffer}')
        subsection_lines.append(f'  Sort Merge Passes:     {sort_merge}')
        subsection_lines.append(f'  Sort Range:            {sort_range}')
        subsection_lines.append(f'  Sort Scan:             {sort_scan}')
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_innodb_buffer_pool_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for InnoDB Buffer Pool Statistics (Detailed) subsection with metric display."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  {no_data_message}
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  {no_data_message}
"""
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Extract values
        pool_size = str(row[col_map.get('Buffer Pool Size (GB)', 0)]) if 'Buffer Pool Size (GB)' in col_map else 'N/A'
        pool_instances = str(row[col_map.get('Buffer Pool Instances', 1)]) if 'Buffer Pool Instances' in col_map else 'N/A'
        total_pages = str(row[col_map.get('Total Pages', 2)]) if 'Total Pages' in col_map else 'N/A'
        data_pages = str(row[col_map.get('Data Pages', 3)]) if 'Data Pages' in col_map else 'N/A'
        dirty_pages = str(row[col_map.get('Dirty Pages', 4)]) if 'Dirty Pages' in col_map else 'N/A'
        free_pages = str(row[col_map.get('Free Pages', 5)]) if 'Free Pages' in col_map else 'N/A'
        hit_rate = str(row[col_map.get('Buffer Pool Hit Rate', 6)]) if 'Buffer Pool Hit Rate' in col_map else 'N/A'
        dirty_pct = str(row[col_map.get('Dirty Page %', 7)]) if 'Dirty Page %' in col_map else 'N/A'
        
        # Format numeric values with commas
        try:
            total_pages = f"{int(total_pages):,}" if total_pages != 'N/A' and total_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            data_pages = f"{int(data_pages):,}" if data_pages != 'N/A' and data_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            dirty_pages = f"{int(dirty_pages):,}" if dirty_pages != 'N/A' and dirty_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        try:
            free_pages = f"{int(free_pages):,}" if free_pages != 'N/A' and free_pages else 'N/A'
        except (ValueError, TypeError):
            pass
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Display as metric cards in text format
        subsection_lines.append(f'  Buffer Pool Size:       {pool_size} GB')
        subsection_lines.append(f'  Buffer Pool Instances:  {pool_instances}')
        subsection_lines.append(f'  Total Pages:            {total_pages}')
        subsection_lines.append(f'  Data Pages:             {data_pages}')
        subsection_lines.append(f'  Dirty Pages:            {dirty_pages}')
        subsection_lines.append(f'  Free Pages:             {free_pages}')
        subsection_lines.append(f'  Buffer Pool Hit Rate:   {hit_rate}')
        subsection_lines.append(f'  Dirty Page %:           {dirty_pct}')
        subsection_lines.append('')
        
        # Generate buffer pool per-instance table if available
        if 'buffer_pool_stats' in parsed_logs:
            bp_data = parsed_logs['buffer_pool_stats']
            if bp_data.success and bp_data.rows and bp_data.columns:
                bp_col_map = {col: idx for idx, col in enumerate(bp_data.columns)}
                display_columns = [
                    ('POOL_ID', 'POOL_ID'),
                    ('POOL_SIZE', 'POOL_SIZE'),
                    ('FREE_BUFFERS', 'FREE_BUFFERS'),
                    ('DATABASE_PAGES', 'DATABASE_PAGES'),
                    ('OLD_DATABASE_PAGES', 'OLD_DATABASE_PAGES'),
                    ('MODIFIED_DATABASE_PAGES', 'MODIFIED_DATABASE_PAGES')
                ]
                table_text = self._generate_table(
                    display_columns,
                    bp_data.rows,
                    bp_col_map,
                    bp_data.columns
                )
                subsection_lines.append('  Buffer Pool Per-Instance Statistics:')
                subsection_lines.append('')
                subsection_lines.append(table_text)
                subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_per_connection_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Per-Connection Memory Buffers subsection with table and estimate note."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (No matching columns found)
"""
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Generate table
        table_text = self._generate_table(
            display_columns,
            log_data.rows,
            column_map,
            log_data.columns
        )
        subsection_lines.append(table_text)
        
        # Get per_connection_memory_estimate data for the estimate note
        if 'per_connection_memory_estimate' in parsed_logs:
            estimate_data = parsed_logs['per_connection_memory_estimate']
            if estimate_data.success and estimate_data.rows and estimate_data.columns:
                est_col_map = {col: idx for idx, col in enumerate(estimate_data.columns)}
                est_row = estimate_data.rows[0]
                
                per_conn_mb = str(est_row[est_col_map.get('Per Connection (MB)', 0)]) if 'Per Connection (MB)' in est_col_map else 'N/A'
                max_conn = str(est_row[est_col_map.get('Max Connections', 1)]) if 'Max Connections' in est_col_map else 'N/A'
                est_max_mem = str(est_row[est_col_map.get('Estimated Max Connections Memory', 2)]) if 'Estimated Max Connections Memory' in est_col_map else 'N/A'
                
                subsection_lines.append('')
                subsection_lines.append(f'  Estimated Per-Connection Memory: ~{per_conn_mb} MB per connection.')
                subsection_lines.append(f'  With max_connections={max_conn}, this could use up to {est_max_mem} GB if all connections are active.')
        
        subsection_lines.append('')
        return '\n'.join(subsection_lines)
    
    def _generate_global_memory_usage_top_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Global Memory Usage - Top Components subsection with table and total note."""
        source_id = subsection.source
        
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (No matching columns found)
"""
        
        # Apply collection limit to displayed rows
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Add truncation notice if rows were limited
        if was_truncated:
            subsection_lines.append(f'  (Showing {self._limit_enforcer.limit} of {total_count} rows)')
            subsection_lines.append('')
        
        # Generate table with limited rows
        table_text = self._generate_table(
            display_columns,
            limited_rows,
            column_map,
            log_data.columns
        )
        subsection_lines.append(table_text)
        
        # Get global_memory_usage_total data for the total note
        # This uses ALL rows for the total calculation, not just the displayed rows
        if 'global_memory_usage_total' in parsed_logs:
            total_data = parsed_logs['global_memory_usage_total']
            if total_data.success and total_data.rows and total_data.columns:
                total_col_map = {col: idx for idx, col in enumerate(total_data.columns)}
                total_row = total_data.rows[0]
                
                total_allocated = str(total_row[total_col_map.get('Total Allocated', 0)]) if 'Total Allocated' in total_col_map else 'N/A'
                high_watermark = str(total_row[total_col_map.get('High Watermark', 1)]) if 'High Watermark' in total_col_map else 'N/A'
                
                subsection_lines.append('')
                subsection_lines.append(f'  Total Tracked Memory: {total_allocated} currently allocated. High watermark: {high_watermark}')
        
        subsection_lines.append('')
        return '\n'.join(subsection_lines)
    
    def _generate_memory_troubleshooting_tips_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> str:
        """Generate plain text for Memory Troubleshooting Tips subsection."""
        tips = [
            ("High Memory Usage", "Check section 4.3 (Global Memory Usage) to identify which components are consuming memory"),
            ("Memory Spikes", 'Look at "High Watermark" columns - they show peak memory usage even if current usage is low'),
            ("Per-Connection Issues", "Check section 4.4 (Memory by Thread) to find memory-hungry connections"),
            ("Application Issues", "Use section 4.5 (Memory by User) to identify which applications/users consume most memory"),
            ("Prepared Statement Memory", "Check section 4.7 for prepared statement memory usage - common cause of memory issues"),
            ("Temporary Table Issues", "Section 4.9 shows if queries are creating too many disk temp tables"),
            ("Reset Statistics", "To reset high watermarks, run: TRUNCATE TABLE performance_schema.memory_summary_global_by_event_name;")
        ]
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
            '  How to Use This Section for Troubleshooting:',
            '',
        ]
        
        for title, desc in tips:
            subsection_lines.append(f'  * {title}: {desc}')
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_aurora_innodb_note_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> str:
        """Generate plain text for Aurora InnoDB Note subsection (Aurora MySQL only)."""
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
            '  Note:',
            '  * Aurora MySQL uses a distributed storage layer that automatically manages I/O,',
            '    flushing, and redo log sizing. Many traditional InnoDB tuning parameters are',
            '    not applicable or modifiable in Aurora.',
            '  * Redo log sizing and flushing are managed automatically by Aurora\'s distributed',
            '    storage layer. These configuration parameters do not apply to Aurora.',
            '  * Redo log management is handled automatically by Aurora\'s distributed storage',
            '    layer. Redo log occupancy monitoring and sizing are not applicable to Aurora',
            '    instances.',
            '',
        ]
        
        return '\n'.join(subsection_lines)
    
    def _generate_innodb_purge_analysis_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for InnoDB Purge Analysis composite subsection."""
        
        # Get history list length for health status
        history_list_length = 0
        if 'innodb_history_length' in parsed_logs:
            hll_data = parsed_logs['innodb_history_length']
            if hll_data.success and hll_data.rows and hll_data.columns:
                try:
                    history_list_length = int(hll_data.rows[0][0])
                except (ValueError, TypeError, IndexError):
                    pass
        
        # Check for long-running transactions (older than 5 minutes = 300 seconds)
        has_long_running_trx = False
        if 'long_running_idle_trx' in parsed_logs:
            trx_data = parsed_logs['long_running_idle_trx']
            if trx_data.success and trx_data.rows:
                has_long_running_trx = True
        if not has_long_running_trx and 'innodb_transactions' in parsed_logs:
            trx_data = parsed_logs['innodb_transactions']
            if trx_data.success and trx_data.rows and trx_data.columns:
                try:
                    duration_idx = trx_data.columns.index('duration_seconds')
                    for row in trx_data.rows:
                        if row[duration_idx] and int(row[duration_idx]) > 300:
                            has_long_running_trx = True
                            break
                except (ValueError, IndexError):
                    pass
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
            '  Detailed analysis of InnoDB purge operations. Purge removes old row versions',
            '  (undo logs) created by MVCC. Slow purge causes History List Length growth,',
            '  causing performance degradation.',
            '',
            '  Understanding Purge:',
            '  InnoDB purge is a background process that removes old row versions (undo logs)',
            '  no longer needed for MVCC or rollback. When purge cannot keep up with',
            '  transaction rate, the History List Length grows, causing:',
            '    * Increased disk space (undo logs)',
            '    * Slower queries (more versions to skip)',
            '    * Higher I/O consumption',
            '    * Potential disk log pressure',
            '',
            '  Reference: https://aws.amazon.com/blogs/database/achieve-a-high-speed-innodb-purge-on-amazon-rds-for-mysql-and-amazon-aurora-mysql/',
            '',
        ]
        
        # Build Purge Thread Activity table
        subsection_lines.extend(self._build_purge_thread_activity_text(parsed_logs))
        
        # Build Purge Configuration table
        subsection_lines.extend(self._build_purge_configuration_text(parsed_logs))
        
        # Build Purge Performance Metrics
        subsection_lines.extend(self._build_purge_performance_metrics_text(parsed_logs))
        
        # Build Purge Optimization Recommendations
        subsection_lines.extend([
            '  Purge Optimization Recommendations (Based on AWS Best Practices):',
            '    * Monitor History List Length: Set CloudWatch alarms for values >10K (warning)',
            '      and >50K (critical)',
            '    * Avoid Long-Running Transactions: Keep transactions short; long transactions',
            '      block purge for all subsequent transactions',
            '    * Optimize Workload: Use DROP PARTITION or DROP TABLE instead of DELETE for',
            '      bulk deletion use cases (age-partitioned)',
            '    * Secondary Indexes: Purge is slower with many secondary indexes. Review',
            '      unused indexes with sys.schema_unused_indexes',
            '    * Table Sharding: Spread writes across multiple tables to increase number of',
            '      purge threads for better parallelism',
            '    * Instance Sizing: Ensure adequate CPU, memory (buffer pool), and I/O capacity',
            '      for purge threads',
            '    * Aurora-Specific: In Aurora, purge performance scales to Serverless v2 scales',
            '      on replicas. Consider aurora_read_replica_read_committed=ON',
            '',
        ])
        
        # Build Purge Health Status
        if history_list_length < 50000:
            status_text = f"  Purge Health Status: OK - Purge operating normally. History list length ({history_list_length:,}) is healthy."
        elif history_list_length < 100000:
            status_text = f"  Purge Health Status: WARNING - History list length ({history_list_length:,}) is elevated. Monitor purge performance and check for long-running transactions."
        else:
            if has_long_running_trx:
                status_text = f"  Purge Health Status: CRITICAL - History list length ({history_list_length:,}) is very high. Long-running transaction(s) detected that may be blocking purge. See 'Active Transactions' section."
            else:
                status_text = f"  Purge Health Status: CRITICAL - History list length ({history_list_length:,}) is very high. Purge may not be keeping up with workload."
        
        subsection_lines.append(status_text)
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_innodb_engine_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for InnoDB Engine Status (Detailed Diagnostics) composite subsection."""
        
        # Get parsed InnoDB status data
        status_data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        if not status_data:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (InnoDB engine status not available)
"""
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
            '  Detailed diagnostics from SHOW ENGINE INNODB STATUS. This provides internal',
            '  InnoDB metrics for advanced troubleshooting.',
            '',
        ]
        
        # 1. Latest Detected Deadlock
        subsection_lines.extend(self._build_deadlock_text(status_data))
        
        # 2. Latest Foreign Key Error
        subsection_lines.extend(self._build_fk_error_text(status_data))
        
        # 3. Semaphore Contention
        subsection_lines.extend(self._build_semaphore_text(status_data))
        
        # 4. Transaction & Purge Status
        subsection_lines.extend(self._build_trx_purge_status_text(status_data))
        
        # 5. Redo Log Checkpoint Status
        subsection_lines.extend(self._build_redo_log_checkpoint_text(status_data))
        
        # 6. Buffer Pool Efficiency
        subsection_lines.extend(self._build_buffer_pool_efficiency_text(status_data))
        
        # 7. Adaptive Hash Index Efficiency
        subsection_lines.extend(self._build_adaptive_hash_text(status_data))
        
        # 8. Row Operations Summary
        subsection_lines.extend(self._build_row_operations_text(status_data))
        
        # 9. InnoDB Status Optimization Tips
        subsection_lines.extend(self._build_innodb_status_tips_text())
        
        return '\n'.join(subsection_lines)
    
    def _build_deadlock_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Latest Detected Deadlock section."""
        lines = ['  Latest Detected Deadlock:', '  ' + '-' * 60]
        
        if not data.has_deadlock:
            lines.append('    (No deadlocks detected since server restart)')
            lines.append('')
            return lines
        
        if data.deadlock_timestamp:
            lines.append(f'    Timestamp: {data.deadlock_timestamp}')
        
        lines.append('    Transactions involved:')
        for trx in data.deadlock_transactions:
            trx_line = f"      - Transaction {trx.get('number', '?')}"
            if 'trx_id' in trx:
                trx_line += f" (ID: {trx['trx_id']})"
            lines.append(trx_line)
            if 'query' in trx:
                lines.append(f"        Query: {trx['query'][:100]}")
        
        if data.deadlock_victim:
            lines.append(f'    Victim (rolled back): Transaction {data.deadlock_victim}')
        
        lines.append('')
        lines.append('    Notes:')
        lines.append('      - This shows only the most recent deadlock. To log all deadlocks to the')
        lines.append('        error log, enable innodb_print_all_deadlocks=ON.')
        lines.append('      - The innodb_deadlock_detect variable controls whether InnoDB performs')
        lines.append('        deadlock detection. When enabled (default), InnoDB automatically detects')
        lines.append('        deadlocks and rolls back one transaction. Disabling it requires relying')
        lines.append('        on innodb_lock_wait_timeout to handle deadlocks.')
        lines.append('')
        lines.append('    Best practices to avoid deadlocks:')
        lines.append('      - Access tables and rows in a consistent order across all transactions')
        lines.append('      - Keep transactions short')
        lines.append('      - Add proper indexes to reduce lock scope')
        lines.append('      - Consider using SELECT ... FOR UPDATE NOWAIT or SKIP LOCKED')
        lines.append('')
        return lines
    
    def _build_fk_error_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Latest Foreign Key Error section."""
        lines = ['  Latest Foreign Key Error:', '  ' + '-' * 60]
        
        if not data.has_fk_error:
            lines.append('    (No foreign key errors since server restart)')
            lines.append('')
            return lines
        
        if data.fk_error_timestamp:
            lines.append(f'    Timestamp:  {data.fk_error_timestamp}')
        if data.fk_error_constraint:
            lines.append(f'    Constraint: {data.fk_error_constraint}')
        if data.fk_error_table:
            lines.append(f'    Table:      {data.fk_error_table}')
        if data.fk_error_statement:
            lines.append(f'    Statement:  {data.fk_error_statement[:100]}')
        
        lines.append('')
        lines.append('    Best practices to avoid FK errors:')
        lines.append('      - Ensure parent rows exist before inserting child rows')
        lines.append('      - Use ON DELETE CASCADE or ON UPDATE CASCADE carefully')
        lines.append('      - Verify data integrity before enabling FK constraints on existing data')
        lines.append('      - Avoid performing operations with foreign_key_checks disabled as this')
        lines.append('        can compromise data integrity and lead to foreign key errors')
        lines.append('')
        return lines
    
    def _build_semaphore_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Semaphore Contention section."""
        lines = ['  Semaphore Contention:', '  ' + '-' * 60]
        
        lines.append(f'    OS Waits Reservation Count:           {data.os_waits_reservation_count:,}')
        lines.append(f'    OS Waits Signal Count:                {data.os_waits_signal_count:,}')
        lines.append(f'    RW-shared (Spins/Rounds/OS Waits):    {data.rw_shared_spins:,} / {data.rw_shared_rounds:,} / {data.rw_shared_os_waits:,}')
        lines.append(f'    RW-excl (Spins/Rounds/OS Waits):      {data.rw_excl_spins:,} / {data.rw_excl_rounds:,} / {data.rw_excl_os_waits:,}')
        lines.append(f'    RW-sx (Spins/Rounds/OS Waits):        {data.rw_sx_spins:,} / {data.rw_sx_rounds:,} / {data.rw_sx_os_waits:,}')
        lines.append(f'    Spin Rounds Per Wait (shared/excl/sx): {data.spin_rounds_per_wait_shared:.2f} / {data.spin_rounds_per_wait_excl:.2f} / {data.spin_rounds_per_wait_sx:.2f}')
        
        if data.os_waits_reservation_count > 0:
            wait_ratio = (data.os_waits_signal_count / data.os_waits_reservation_count) * 100
            lines.append(f'    Signal/Reservation Ratio:             {wait_ratio:.1f}%')
        
        if data.current_waits:
            lines.append('')
            lines.append('    WARNING: Current Waits (threads waiting for semaphores):')
            for w in data.current_waits[:5]:
                lines.append(f'      - {w[:80]}')
        
        lines.append('')
        lines.append('    Note: High spin rounds per wait values are influenced by innodb_spin_wait_delay,')
        lines.append('    innodb_spin_wait_pause_multiplier, and innodb_sync_spin_loops. The presence of')
        lines.append('    threads in the current wait list indicates active contention.')
        lines.append('')
        return lines
    
    def _build_trx_purge_status_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Transaction & Purge Status section."""
        lines = ['  Transaction & Purge Status:', '  ' + '-' * 60]
        
        # Determine purge health
        purge_status = "Unknown"
        status_indicator = ""
        if data.purge_state:
            if "running but idle" in data.purge_state.lower():
                purge_status = "Purge is current (running but idle)"
                status_indicator = "[OK]"
            elif "running" in data.purge_state.lower():
                purge_status = "Purge is actively running"
                status_indicator = "[WARNING]"
            else:
                purge_status = data.purge_state
        
        if data.purge_lag > 0:
            purge_status += f" - {data.purge_lag:,} transactions behind"
            if data.purge_lag > 1000:
                status_indicator = "[WARNING]"
            if data.purge_lag > 10000:
                status_indicator = "[CRITICAL]"
        
        lines.append(f'    Current Transaction ID Counter: {data.trx_id_counter:,}')
        lines.append(f'    Purge Done For Transaction:     {data.purge_done_for_trx:,}')
        lines.append(f'    Purge Lag (transactions behind): {data.purge_lag:,}')
        lines.append(f'    History List Length:            {data.history_list_length:,}')
        lines.append(f'    Purge State:                    {status_indicator} {purge_status}')
        
        if data.purge_lag > 10000:
            lines.append('')
            lines.append('    WARNING: Purge lag is high. Investigate long-running transactions blocking purge.')
        
        lines.append('')
        return lines
    
    def _build_redo_log_checkpoint_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Redo Log Checkpoint Status section."""
        lines = ['  Redo Log Checkpoint Status:', '  ' + '-' * 60]
        
        lines.append(f'    Log Sequence Number (LSN):  {data.log_sequence_number:,}')
        lines.append(f'    Log Flushed Up To:          {data.log_flushed_up_to:,}')
        lines.append(f'    Pages Flushed Up To:        {data.pages_flushed_up_to:,}')
        lines.append(f'    Last Checkpoint At:         {data.last_checkpoint_at:,}')
        lines.append(f'    Checkpoint Lag (bytes):     {data.checkpoint_lag:,}')
        lines.append(f'    Unflushed Log (bytes):      {data.unflushed_log:,}')
        lines.append(f'    Pending Log Flushes:        {data.pending_log_flushes}')
        lines.append(f'    Pending Checkpoint Writes:  {data.pending_chkp_writes}')
        lines.append(f'    Log I/Os Done:              {data.log_ios_done:,}')
        lines.append(f'    Log I/Os Per Second:        {data.log_ios_per_second:.2f}')
        
        if data.checkpoint_lag > 0 or data.unflushed_log > 0:
            lines.append('')
            lines.append('    Recommendations:')
            lines.append('      - Checkpoint lag indicates how much redo log data hasn\'t been checkpointed')
            lines.append('        yet. High checkpoint lag under heavy write workloads may indicate redo')
            lines.append('        log is too small or I/O is a bottleneck.')
            lines.append('      - Consider increasing innodb_log_file_size if checkpoint lag is consistently high.')
        
        lines.append('')
        return lines
    
    def _build_buffer_pool_efficiency_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Buffer Pool Efficiency section."""
        lines = ['  Buffer Pool Efficiency:', '  ' + '-' * 60]
        
        hit_rate_pct = (data.buffer_pool_hit_rate / data.buffer_pool_hit_rate_denominator * 100) if data.buffer_pool_hit_rate_denominator > 0 else 0
        
        lines.append(f'    Buffer Pool Hit Rate:              {data.buffer_pool_hit_rate} / {data.buffer_pool_hit_rate_denominator} ({hit_rate_pct:.2f}%)')
        lines.append(f'    Pages Made Young:                  {data.pages_made_young:,}')
        lines.append(f'    Pages Made Not Young:              {data.pages_made_not_young:,}')
        lines.append(f'    Youngs Per Second:                 {data.youngs_per_sec:.2f}')
        lines.append(f'    Non-Youngs Per Second:             {data.non_youngs_per_sec:.2f}')
        lines.append(f'    Read Ahead Per Second:             {data.read_ahead_per_sec:.2f}')
        lines.append(f'    Evicted Without Access Per Second: {data.evicted_without_access_per_sec:.2f}')
        lines.append(f'    LRU Length:                        {data.lru_len:,}')
        
        if hit_rate_pct < 99.9 or data.evicted_without_access_per_sec > 0:
            lines.append('')
            lines.append('    Recommendations:')
            lines.append('      - Buffer pool hit rate below 99.9% may indicate buffer pool is undersized')
            lines.append('        for the working set.')
            lines.append('      - High "evicted without access" rate suggests pages are being evicted')
            lines.append('        before being read, indicating memory pressure.')
            lines.append('      - Review queries causing full table scans that pollute the buffer pool.')
            lines.append('      - Check if innodb_buffer_pool_size, innodb_old_blocks_pct and')
            lines.append('        innodb_old_blocks_time are tuned appropriately.')
        
        lines.append('')
        return lines
    
    def _build_adaptive_hash_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Adaptive Hash Index Efficiency section."""
        lines = ['  Adaptive Hash Index Efficiency:', '  ' + '-' * 60]
        
        total_searches = data.hash_searches_per_sec + data.non_hash_searches_per_sec
        hash_ratio = (data.hash_searches_per_sec / total_searches * 100) if total_searches > 0 else 0
        
        lines.append(f'    Hash Searches Per Second:     {data.hash_searches_per_sec:.2f}')
        lines.append(f'    Non-Hash Searches Per Second: {data.non_hash_searches_per_sec:.2f}')
        lines.append(f'    Hash Search Ratio:            {hash_ratio:.1f}%')
        lines.append(f'    Hash Table Size:              {data.hash_table_size:,}')
        
        if total_searches > 0:
            lines.append('')
            lines.append('    About Adaptive Hash Index (AHI):')
            lines.append('      - AHI builds in-memory hash indexes for frequently accessed index pages.')
            lines.append('      - Low hash search ratio may indicate AHI is not benefiting your workload.')
            lines.append('')
            lines.append('    Recommendations:')
            lines.append('      - If hash searches are consistently near zero, consider disabling AHI with')
            lines.append('        innodb_adaptive_hash_index=OFF to reduce memory overhead.')
            lines.append('      - AHI works best with point lookups; range scans don\'t benefit from it.')
            lines.append('      - Monitor innodb_adaptive_hash_index_parts for partitioning to reduce contention.')
        
        lines.append('')
        return lines
    
    def _build_row_operations_text(self, data: 'InnoDBStatusData') -> List[str]:
        """Build plain text for Row Operations Summary section."""
        lines = ['  Row Operations Summary:', '  ' + '-' * 60]
        
        lines.append(f'    Queries Inside InnoDB: {data.queries_inside_innodb}')
        lines.append(f'    Queries in Queue:      {data.queries_in_queue}')
        lines.append(f'    Inserts/sec:           {data.inserts_per_sec:.2f}')
        lines.append(f'    Updates/sec:           {data.updates_per_sec:.2f}')
        lines.append(f'    Deletes/sec:           {data.deletes_per_sec:.2f}')
        lines.append(f'    Reads/sec:             {data.reads_per_sec_rows:.2f}')
        lines.append(f'    Main Thread State:     {data.main_thread_state or "Unknown"}')
        lines.append('')
        return lines
    
    def _build_innodb_status_tips_text(self) -> List[str]:
        """Build plain text for InnoDB Status Optimization Tips section."""
        lines = ['  InnoDB Status Optimization Tips:', '  ' + '-' * 60]
        
        tips = [
            ("Deadlock Victim", "The transaction InnoDB chose to roll back to break the deadlock cycle. InnoDB selects the transaction that modified fewer rows as the victim to minimize rollback cost."),
            ("Purge Lag", "The number of transactions whose old row versions haven't been cleaned up yet. Growing purge lag indicates long-running transactions are blocking purge, causing undo log growth and table bloat."),
            ("History List Length", "The number of unpurged transactions in the undo log. Values consistently above 10,000 indicate purge is falling behind, leading to increased disk usage and slower queries due to MVCC overhead."),
            ("Checkpoint Lag", "The difference between the current Log Sequence Number (LSN) and the last checkpoint. Large checkpoint lag means more redo log data needs to be replayed during crash recovery, increasing recovery time."),
            ("Unflushed Log", "The amount of data (in bytes) written to the InnoDB Log Buffer but not yet flushed to redo log files on disk. If this approaches innodb_log_buffer_size, it may indicate I/O bottleneck or undersized log buffer."),
            ("Buffer Pool Hit Rate", "The percentage of page requests served from the buffer pool cache vs disk reads. Values below 99.9% suggest the buffer pool may be undersized for your working set, causing excessive disk I/O."),
            ("Pages Evicted Without Access", "Pages that were loaded into the buffer pool but evicted before being read. High values indicate memory pressure or queries causing unnecessary page loads (e.g., full table scans)."),
            ("Pages Made Young/Not Young", "Tracks how pages move between the 'old' and 'young' sublists in the buffer pool LRU. High 'not young' values may indicate innodb_old_blocks_time is filtering out scan-loaded pages effectively."),
            ("Adaptive Hash Index (AHI) Efficiency", "The ratio of hash searches to non-hash searches. Low ratio means AHI isn't benefiting your workload; consider disabling it to free memory and reduce contention."),
            ("Spin Rounds Per Wait", "The number of spin loop iterations before a thread gives up and performs an OS wait. Values are influenced by innodb_spin_wait_delay and innodb_sync_spin_loops. High values with many OS waits indicate contention."),
            ("OS Waits vs Reservation Count", "The ratio of expensive OS-level waits to total lock attempts. Lower ratio is better, meaning more locks were acquired via cheap spinning rather than costly context switches."),
            ("Queries in Queue", "The number of queries waiting to enter InnoDB. Values consistently above 0 indicate thread contention; consider tuning innodb_thread_concurrency."),
            ("Main Thread State", "The current state of the InnoDB master background thread (sleeping, purging, flushing). Persistent non-sleeping states may indicate heavy background activity."),
        ]
        
        for title, desc in tips:
            lines.append(f'    * {title}: {desc}')
        
        lines.append('')
        return lines
    
    def _generate_long_running_transactions_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Long-Running Transactions composite subsection."""
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note if provided
        if subsection.note:
            subsection_lines.append(f'  {subsection.note}')
            subsection_lines.append('')
        
        # Build Active Transactions table
        subsection_lines.extend(self._build_active_transactions_text(parsed_logs))
        
        # Build Long-Running Idle Transactions table
        subsection_lines.extend(self._build_long_running_idle_trx_text(parsed_logs))
        
        # Build Long-Running Active Transactions table
        subsection_lines.extend(self._build_long_running_active_trx_text(parsed_logs))
        
        return '\n'.join(subsection_lines)
    
    def _build_active_transactions_text(self, parsed_logs: Dict[str, ParsedLogFile]) -> List[str]:
        """Build plain text table for Active Transactions."""
        lines = ['  Active Transactions:', '  ' + '-' * 60]
        
        if 'innodb_transactions' not in parsed_logs:
            lines.append('    (No active transactions)')
            lines.append('')
            return lines
        
        log_data = parsed_logs['innodb_transactions']
        
        if not log_data.success or not log_data.rows:
            lines.append('    (No active transactions)')
            lines.append('')
            return lines
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'duration_seconds', 'trx_mysql_thread_id', 'trx_query', 'trx_rows_locked', 'trx_rows_modified']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            lines.append('    (No matching columns found)')
            lines.append('')
            return lines
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Calculate column widths
        col_widths = []
        for col in display_columns:
            max_width = len(col)
            for row in log_data.rows[:20]:
                idx = column_map.get(col)
                if idx is not None and idx < len(row):
                    val = str(row[idx]) if row[idx] is not None else ""
                    max_width = max(max_width, len(val))
            col_widths.append(min(max_width, 25))
        
        # Build header
        header = "    " + " | ".join(col.ljust(col_widths[i]) for i, col in enumerate(display_columns))
        separator = "    " + "-+-".join("-" * w for w in col_widths)
        lines.append(header)
        lines.append(separator)
        
        # Build data rows (limit to 20)
        for row in log_data.rows[:20]:
            cells = []
            for i, col in enumerate(display_columns):
                idx = column_map.get(col)
                val = str(row[idx]) if idx is not None and idx < len(row) and row[idx] is not None else ""
                cells.append(val[:col_widths[i]].ljust(col_widths[i]))
            lines.append("    " + " | ".join(cells))
        
        lines.append('')
        return lines
    
    def _build_long_running_idle_trx_text(self, parsed_logs: Dict[str, ParsedLogFile]) -> List[str]:
        """Build plain text table for Long-Running Idle Transactions."""
        lines = [
            '  Long-Running Idle Transactions (>5 min, no query):',
            '  ' + '-' * 60,
            '    Note: Transactions that started but have no current query running.',
            '          May indicate application issues or forgotten transactions.',
            ''
        ]
        
        if 'long_running_idle_trx' not in parsed_logs:
            lines.append('    (No idle transactions older than 5 minutes)')
            lines.append('')
            return lines
        
        log_data = parsed_logs['long_running_idle_trx']
        
        if not log_data.success or not log_data.rows:
            lines.append('    (No idle transactions older than 5 minutes)')
            lines.append('')
            return lines
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'trx_rows_modified']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            lines.append('    (No matching columns found)')
            lines.append('')
            return lines
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Calculate column widths
        col_widths = []
        for col in display_columns:
            max_width = len(col)
            for row in log_data.rows[:20]:
                idx = column_map.get(col)
                if idx is not None and idx < len(row):
                    val = str(row[idx]) if row[idx] is not None else ""
                    max_width = max(max_width, len(val))
            col_widths.append(min(max_width, 25))
        
        # Build header
        header = "    " + " | ".join(col.ljust(col_widths[i]) for i, col in enumerate(display_columns))
        separator = "    " + "-+-".join("-" * w for w in col_widths)
        lines.append(header)
        lines.append(separator)
        
        # Build data rows (limit to 20)
        for row in log_data.rows[:20]:
            cells = []
            for i, col in enumerate(display_columns):
                idx = column_map.get(col)
                val = str(row[idx]) if idx is not None and idx < len(row) and row[idx] is not None else ""
                cells.append(val[:col_widths[i]].ljust(col_widths[i]))
            lines.append("    " + " | ".join(cells))
        
        lines.append('')
        return lines
    
    def _build_long_running_active_trx_text(self, parsed_logs: Dict[str, ParsedLogFile]) -> List[str]:
        """Build plain text table for Long-Running Active Transactions."""
        lines = [
            '  Long-Running Active Transactions (>1 min, with query):',
            '  ' + '-' * 60,
            '    Note: Transactions with queries currently executing.',
            '          May indicate slow queries or large batch operations.',
            ''
        ]
        
        if 'long_running_active_trx' not in parsed_logs:
            lines.append('    (No active transactions running longer than 1 minute)')
            lines.append('')
            return lines
        
        log_data = parsed_logs['long_running_active_trx']
        
        if not log_data.success or not log_data.rows:
            lines.append('    (No active transactions running longer than 1 minute)')
            lines.append('')
            return lines
        
        # Build table
        columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'current_query']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            lines.append('    (No matching columns found)')
            lines.append('')
            return lines
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Calculate column widths
        col_widths = []
        for col in display_columns:
            max_width = len(col)
            for row in log_data.rows[:20]:
                idx = column_map.get(col)
                if idx is not None and idx < len(row):
                    val = str(row[idx]) if row[idx] is not None else ""
                    max_width = max(max_width, len(val))
            col_widths.append(min(max_width, 25))
        
        # Build header
        header = "    " + " | ".join(col.ljust(col_widths[i]) for i, col in enumerate(display_columns))
        separator = "    " + "-+-".join("-" * w for w in col_widths)
        lines.append(header)
        lines.append(separator)
        
        # Build data rows (limit to 20)
        for row in log_data.rows[:20]:
            cells = []
            for i, col in enumerate(display_columns):
                idx = column_map.get(col)
                val = str(row[idx]) if idx is not None and idx < len(row) and row[idx] is not None else ""
                cells.append(val[:col_widths[i]].ljust(col_widths[i]))
            lines.append("    " + " | ".join(cells))
        
        lines.append('')
        return lines
    
    def _generate_long_running_trx_activity_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Long-Running Transactions Activity subsection."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            lines = [
                subsection.name,
                self.SUBSECTION_SEPARATOR,
            ]
            if subsection.note:
                lines.append(f'  Note: {subsection.note}')
                lines.append('')
            lines.append(f'  ({no_data_message})')
            lines.append('')
            return '\n'.join(lines)
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            lines = [
                subsection.name,
                self.SUBSECTION_SEPARATOR,
            ]
            if subsection.note:
                lines.append(f'  Note: {subsection.note}')
                lines.append('')
            lines.append(f'  ({no_data_message})')
            lines.append('')
            return '\n'.join(lines)
        
        # Build the subsection
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Build the data table
        columns = ['mysql_pid', 'trx_duration', 'trx_state', 'sql_text', 'stmt_execution_time', 'rows_sent', 'rows_modified', 'rows_examined', 'tmp_disk_tables', 'error_info']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            display_columns = log_data.columns
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Calculate column widths
        col_widths = []
        for col in display_columns:
            max_width = len(col)
            for row in log_data.rows:
                idx = column_map.get(col)
                if idx is not None and idx < len(row):
                    val = str(row[idx]) if row[idx] is not None else ""
                    # Truncate long SQL text
                    if col == 'sql_text' and len(val) > 50:
                        val = val[:50]
                    max_width = max(max_width, len(val))
            col_widths.append(min(max_width, 50))
        
        # Build header
        header = "    " + " | ".join(col.ljust(col_widths[i]) for i, col in enumerate(display_columns))
        separator = "    " + "-+-".join("-" * w for w in col_widths)
        subsection_lines.append(header)
        subsection_lines.append(separator)
        
        # Build data rows
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(display_columns):
                idx = column_map.get(col)
                val = str(row[idx]) if idx is not None and idx < len(row) and row[idx] is not None else ""
                # Truncate long SQL text
                if col == 'sql_text' and len(val) > col_widths[i]:
                    val = val[:col_widths[i]-3] + '...'
                cells.append(val[:col_widths[i]].ljust(col_widths[i]))
            subsection_lines.append("    " + " | ".join(cells))
        
        subsection_lines.append('')
        
        # Add interpretation
        subsection_lines.extend([
            '  Interpretation Guide:',
            '  ' + '-' * 60,
            '  This section shows the 10 most recent SQL statements executed within each',
            '  long-running transaction (>5 minutes). Use this to understand what operations',
            '  the transaction has performed.',
            '',
            '  Key columns:',
            '    * mysql_pid: Process ID - use to identify the connection',
            '    * trx_duration: Total time the transaction has been active',
            '    * sql_text: The SQL statement that was executed',
            '    * stmt_execution_time: How long this specific statement took',
            '    * rows_sent: Number of rows returned to the client',
            '    * rows_modified: Number of rows affected by this statement',
            '    * rows_examined: Number of rows scanned (high values may indicate missing indexes)',
            '    * tmp_disk_tables: Disk temp tables created (high values indicate inefficient queries)',
            '    * error_info: Shows if the statement encountered errors',
            '',
            '  What to look for:',
            '    * Statements with high rows_modified may be causing lock contention',
            '    * High rows_examined vs rows_sent ratio indicates inefficient queries',
            '    * High tmp_disk_tables suggests queries need optimization',
            '    * Multiple statements per PID show the transaction\'s activity history',
            '    * Use the PID to correlate with application logs or kill the connection if needed',
            '',
        ])
        
        return '\n'.join(subsection_lines)
    
    def _generate_tables_without_pk_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Tables Without Primary Key subsection with detailed info."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (No data available - query not executed)
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Handle no data case - all tables have PKs
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  [OK] {no_data_message}
"""
        
        # Tables without PK found - show critical warning and info
        table_count = len(log_data.rows)
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
            '',
            f'  [CRITICAL] CRITICAL: Found {table_count} table{"s" if table_count != 1 else ""} without primary keys!',
            '',
            '  CRITICAL REPLICATION IMPACT:',
            '  ' + '-' * 60,
            '  * Full Table Scans on Replicas: Every UPDATE/DELETE on these tables causes',
            '    a FULL TABLE SCAN on replicas to locate the row.',
            '  * Row-Based Replication (RBR): With binlog_format=ROW (default in MySQL 8.0+),',
            '    replicas must scan entire tables to find matching rows.',
            '  * Semi-Sync Replication: Without PKs, semi-sync replication can timeout',
            '    waiting for replica acknowledgment.',
            '  * Replication Lag: Large tables without PKs can cause hours or days of',
            '    replication lag, making replicas unusable for reads.',
            '  * Table Scan Avoidance: MySQL 8.0.18+ has optimizations but they are NOT',
            '    sufficient - primary keys are still required!',
            '  * Immediate Action Required: Add primary keys to ALL tables:',
            '    ALTER TABLE table_name ADD PRIMARY KEY (column);',
            '',
            '  Workaround Options:',
            '  ' + '-' * 60,
            '  * Option 1 (Best): Add a proper primary key based on business logic',
            '  * Option 2 (Temporary): Add an invisible auto-increment column (MySQL 8.0.23+):',
            '    ALTER TABLE t ADD COLUMN _pk BIGINT UNSIGNED NOT NULL AUTO_INCREMENT',
            '    INVISIBLE PRIMARY KEY;',
            '  * Option 3 (Last Resort): Use a unique index as a substitute',
            '',
            '  Prevention (MySQL 8.0.13+):',
            '  ' + '-' * 60,
            '  * Require PKs: Set sql_require_primary_key=ON (MySQL 8.0.13+)',
            '  * Auto-Generate PKs: Set sql_generate_invisible_primary_key=ON (MySQL 8.0.30+)',
            '',
            '  References:',
            '  ' + '-' * 60,
            '  * https://dev.mysql.com/doc/refman/8.0/en/table-scan-avoidance.html',
            '  * https://dev.mysql.com/doc/mysql-replication-excerpt/8.0/en/replication-features-row-searches.html',
            '  * https://jfg-mysql.blogspot.com/2017/08/danger-no-pk-with-RBR-and-mariadb-protection.html',
            '  * https://www.percona.com/blog/attack-no-pk-replication-lag-with-mysql-percona-server-8-invisible-columns/',
            '  * https://www.percona.com/blog/tuning-innodb-primary-keys/',
            '  * https://www.percona.com/blog/illustrating-primary-key-models-in-innodb-and-their-impact-on-disk-usage/',
            '',
        ]
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        
        # Calculate column widths
        col_widths = []
        for i, col in enumerate(columns):
            max_width = len(str(col))
            for row in log_data.rows:
                if i < len(row):
                    val = str(row[i]) if row[i] is not None else ""
                    max_width = max(max_width, len(val))
            col_widths.append(min(max_width, 30))  # Cap at 30 chars
        
        # Build header
        header = "  | " + " | ".join(str(col).ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
        separator = "  +" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        
        subsection_lines.append(separator)
        subsection_lines.append(header)
        subsection_lines.append(separator)
        
        # Build data rows
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                if i < len(row):
                    val = str(row[i]) if row[i] is not None else ""
                    cells.append(val[:col_widths[i]].ljust(col_widths[i]))
                else:
                    cells.append("".ljust(col_widths[i]))
            subsection_lines.append("  | " + " | ".join(cells) + " |")
        
        subsection_lines.append(separator)
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_unused_indexes_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Unused Indexes subsection with warning note when indexes found."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}

  No data available
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}

  Error: {log_data.error or "Unknown error"}
"""
        
        lines = [subsection.name, self.SUBSECTION_SEPARATOR, '']
        
        # Add note if provided
        if subsection.note:
            lines.append(f"  Note: {subsection.note}")
            lines.append('')
        
        # Handle no data case - no unused indexes
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            lines.append(f"  {no_data_message}")
            lines.append('')
            return '\n'.join(lines)
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        col_widths = [max(len(str(col)), 10) for col in columns]
        
        # Adjust widths based on data
        for row in limited_rows:
            for i, val in enumerate(row):
                if i < len(col_widths):
                    col_widths[i] = max(col_widths[i], min(len(str(val) if val else ''), 40))
        
        # Header
        header = "  | " + " | ".join(str(col).ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
        separator = "  +-" + "-+-".join("-" * w for w in col_widths) + "-+"
        
        lines.append(separator)
        lines.append(header)
        lines.append(separator)
        
        # Data rows
        for row in limited_rows:
            cells = []
            for i in range(len(columns)):
                if i < len(row):
                    val = str(row[i]) if row[i] is not None else ''
                    cells.append(val[:col_widths[i]].ljust(col_widths[i]))
                else:
                    cells.append("".ljust(col_widths[i]))
            lines.append("  | " + " | ".join(cells) + " |")
        
        lines.append(separator)
        lines.append('')
        
        # Add truncation notice if needed
        if was_truncated:
            lines.append(f"  {self._limit_enforcer.get_truncation_message(total_count)}")
            lines.append('')
        
        # Warning note for safe index removal
        lines.extend([
            '  IMPORTANT - Safe Index Removal Process:',
            '  ' + '-' * 40,
            '',
            '  DO NOT drop indexes immediately! Indexes may be used infrequently',
            '  (daily/weekly/monthly reports, batch jobs, etc.).',
            '',
            '  Recommended approach (MySQL 8.0+):',
            '    1. Make the index INVISIBLE first:',
            '       ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE;',
            '    2. Monitor for several weeks/months to ensure no queries break',
            '    3. If no issues, drop the index:',
            '       ALTER TABLE schema.table DROP INDEX index_name;',
            '    4. If queries break, make visible again:',
            '       ALTER TABLE schema.table ALTER INDEX index_name VISIBLE;',
            '',
            '  Why: Invisible indexes are maintained by MySQL but not used by the',
            '  optimizer. This allows safe testing without the risk of losing the',
            '  index structure. Available in MySQL 8.0+ and Aurora MySQL 2.x+.',
            '',
        ])
        
        return '\n'.join(lines)
    
    def _generate_locked_accounts_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Locked Accounts subsection with locked_connects warning."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (No data available - query not executed)
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Build subsection text
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note above the table if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Handle no data case - no locked accounts
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            subsection_lines.append(f'  ({no_data_message})')
            subsection_lines.append('')
            return '\n'.join(subsection_lines)
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Calculate column widths
        col_widths = []
        for col in columns:
            max_width = len(str(col))
            if col in column_map:
                for row in log_data.rows:
                    idx = column_map[col]
                    if idx < len(row):
                        val = str(row[idx]) if row[idx] is not None else ""
                        max_width = max(max_width, len(val))
            col_widths.append(min(max_width, 30))  # Cap at 30 chars
        
        # Build header
        header = "  | " + " | ".join(str(col).ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
        separator = "  +" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        
        subsection_lines.append(separator)
        subsection_lines.append(header)
        subsection_lines.append(separator)
        
        # Build data rows
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                if col in column_map and column_map[col] < len(row):
                    val = str(row[column_map[col]]) if row[column_map[col]] is not None else ""
                    cells.append(val[:col_widths[i]].ljust(col_widths[i]))
                else:
                    cells.append("".ljust(col_widths[i]))
            subsection_lines.append("  | " + " | ".join(cells) + " |")
        
        subsection_lines.append(separator)
        
        # Check locked_connects status and add warning if > 0
        if 'locked_connects_status' in parsed_logs:
            status_data = parsed_logs['locked_connects_status']
            if status_data.success and status_data.rows:
                for row in status_data.rows:
                    if len(row) >= 2 and str(row[0]).lower() == 'locked_connects':
                        try:
                            locked_count = int(row[1])
                            if locked_count > 0:
                                subsection_lines.append('')
                                subsection_lines.append(f'  ⚠️ The number of attempts to connect to locked user accounts - {locked_count:,}')
                        except (ValueError, TypeError):
                            pass
                        break
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_ssl_config_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for SSL Configuration subsection with require_secure_transport warning."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  (No data available - query not executed)
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Build subsection text
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            subsection_lines.append(f'  ({no_data_message})')
            subsection_lines.append('')
            return '\n'.join(subsection_lines)
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Calculate column widths
        col_widths = []
        for col in columns:
            max_width = len(str(col))
            if col in column_map:
                for row in log_data.rows:
                    idx = column_map[col]
                    if idx < len(row):
                        val = str(row[idx]) if row[idx] is not None else ""
                        max_width = max(max_width, len(val))
            col_widths.append(min(max_width, 40))  # Cap at 40 chars
        
        # Build header
        header = "  | " + " | ".join(str(col).ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
        separator = "  +" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        
        subsection_lines.append(separator)
        subsection_lines.append(header)
        subsection_lines.append(separator)
        
        # Check for require_secure_transport value while building rows
        require_secure_transport_off = False
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                if col in column_map and column_map[col] < len(row):
                    val = str(row[column_map[col]]) if row[column_map[col]] is not None else ""
                    cells.append(val[:col_widths[i]].ljust(col_widths[i]))
                else:
                    cells.append("".ljust(col_widths[i]))
            subsection_lines.append("  | " + " | ".join(cells) + " |")
            
            # Check if this row is require_secure_transport and if it's OFF
            if len(row) >= 2:
                var_name = str(row[0]).lower() if row[0] else ''
                var_value = str(row[1]).upper() if row[1] else ''
                if var_name == 'require_secure_transport' and var_value == 'OFF':
                    require_secure_transport_off = True
        
        subsection_lines.append(separator)
        
        # Add warning if require_secure_transport is OFF
        if require_secure_transport_off:
            subsection_lines.append('')
            subsection_lines.append('  ⚠️ Enable require_secure_transport=ON to enforce SSL/TLS for all connections.')
            subsection_lines.append('     This prevents unencrypted connections that could expose sensitive data.')
        
        subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_ssl_connection_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for SSL Connection Status subsection with metric display."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Extract values from the first row
        row = log_data.rows[0]
        total_connections = row[0] if len(row) > 0 else 0
        ssl_connections = row[1] if len(row) > 1 else 0
        non_ssl_connections = row[2] if len(row) > 2 else 0
        
        # Format values
        total_str = f"{int(total_connections):,}" if total_connections is not None else "0"
        ssl_str = f"{int(ssl_connections):,}" if ssl_connections is not None else "0"
        non_ssl_str = f"{int(non_ssl_connections):,}" if non_ssl_connections is not None else "0"
        
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
            '',
            f'  Total Connections:     {total_str}',
            f'  SSL/TLS Connections:   {ssl_str}',
            f'  Non-SSL Connections:   {non_ssl_str}',
            '',
        ]
        
        return '\n'.join(subsection_lines)
    
    def _generate_aurora_memory_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Aurora Memory Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build subsection text
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note above the table if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        col_widths = [max(len(str(col)), 10) for col in columns]
        
        # Adjust widths based on data
        for row in log_data.rows:
            for i, col in enumerate(columns):
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    val = str(row[col_idx]) if row[col_idx] is not None else ''
                    col_widths[i] = max(col_widths[i], min(len(val), 50))
        
        # Header
        header = "  | " + " | ".join(str(col).ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
        separator = "  +-" + "-+-".join("-" * w for w in col_widths) + "-+"
        
        subsection_lines.append(separator)
        subsection_lines.append(header)
        subsection_lines.append(separator)
        
        # Data rows
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    val = str(row[col_idx]) if row[col_idx] is not None else ''
                    cells.append(val[:col_widths[i]].ljust(col_widths[i]))
                else:
                    cells.append("".ljust(col_widths[i]))
            subsection_lines.append("  | " + " | ".join(cells) + " |")
        
        subsection_lines.append(separator)
        subsection_lines.append('')
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check aurora_oom_status
        oom_status = values.get('aurora_oom_status', '')
        if oom_status == 'RESERVED':
            recommendations.append('⚠️ Memory pressure detected: aurora_oom_status is RESERVED. OOM avoidance is active.')
            recommendations.append('   Review long-running queries and connections. Consider scaling up instance class.')
        
        # Check Aurora_reserved_mem_exceeded_incidents
        incidents = values.get('Aurora_reserved_mem_exceeded_incidents', '0')
        try:
            incidents_count = int(incidents)
            if incidents_count > 0:
                recommendations.append(f'⚠️ Memory limits exceeded: Aurora_reserved_mem_exceeded_incidents = {incidents_count}.')
                recommendations.append('   Review peak usage times, check for connection leaks, and consider larger instance.')
        except (ValueError, TypeError):
            pass
        
        # Check aurora_oom_avoidance_recovery_state
        recovery_state = values.get('aurora_oom_avoidance_recovery_state', '')
        if recovery_state == 'ACTIVE':
            recommendations.append('⚠️ OOM recovery active: aurora_oom_avoidance_recovery_state is ACTIVE.')
            recommendations.append('   The system is actively recovering from memory pressure.')
        
        # Check Aurora_lockmgr_memory_used (warn if > 100MB)
        lockmgr_mem = values.get('Aurora_lockmgr_memory_used', '0')
        try:
            lockmgr_bytes = int(lockmgr_mem)
            if lockmgr_bytes > 100 * 1024 * 1024:  # 100MB
                lockmgr_mb = lockmgr_bytes / (1024 * 1024)
                recommendations.append(f'⚠️ High lock manager memory: Aurora_lockmgr_memory_used = {lockmgr_mb:.1f} MB.')
                recommendations.append('   Check for long-running transactions or lock contention.')
        except (ValueError, TypeError):
            pass
        
        # Check aurora_temptable_ram_allocation (warn if > 1GB)
        temptable_mem = values.get('aurora_temptable_ram_allocation', '0')
        try:
            temptable_bytes = int(temptable_mem)
            if temptable_bytes > 1024 * 1024 * 1024:  # 1GB
                temptable_gb = temptable_bytes / (1024 * 1024 * 1024)
                recommendations.append(f'⚠️ High temporary table memory: aurora_temptable_ram_allocation = {temptable_gb:.2f} GB.')
                recommendations.append('   Review queries creating temp tables and optimize or add indexes.')
        except (ValueError, TypeError):
            pass
        
        # Add recommendations
        if recommendations:
            subsection_lines.append('  Recommendations:')
            subsection_lines.append('  ' + '-' * 40)
            for rec in recommendations:
                subsection_lines.append(f'  {rec}')
            subsection_lines.append('')
        else:
            subsection_lines.append('  ✓ All Aurora memory metrics are within normal ranges.')
            subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_aurora_pq_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Aurora Parallel Query Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build subsection text
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note above the table if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        col_widths = [max(len(str(col)), 10) for col in columns]
        
        # Adjust widths based on data
        for row in log_data.rows:
            for i, col in enumerate(columns):
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    val = str(row[col_idx]) if row[col_idx] is not None else ''
                    col_widths[i] = max(col_widths[i], min(len(val), 50))
        
        # Header
        header = "  | " + " | ".join(str(col).ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
        separator = "  +-" + "-+-".join("-" * w for w in col_widths) + "-+"
        
        subsection_lines.append(separator)
        subsection_lines.append(header)
        subsection_lines.append(separator)
        
        # Data rows
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    val = str(row[col_idx]) if row[col_idx] is not None else ''
                    cells.append(val[:col_widths[i]].ljust(col_widths[i]))
                else:
                    cells.append("".ljust(col_widths[i]))
            subsection_lines.append("  | " + " | ".join(cells) + " |")
        
        subsection_lines.append(separator)
        subsection_lines.append('')
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check Aurora_pq_request_throttled
        throttled = values.get('Aurora_pq_request_throttled', '0')
        try:
            throttled_count = int(throttled)
            if throttled_count > 0:
                recommendations.append(f'⚠️ PQ throttling detected: Aurora_pq_request_throttled = {throttled_count}.')
                recommendations.append('   Parallel query concurrency limit reached. Consider scaling to a larger instance class.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_failed
        failed = values.get('Aurora_pq_request_failed', '0')
        try:
            failed_count = int(failed)
            if failed_count > 0:
                recommendations.append(f'⚠️ PQ failures detected: Aurora_pq_request_failed = {failed_count}.')
                recommendations.append('   Check storage layer health and query complexity.')
        except (ValueError, TypeError):
            pass
        
        # Calculate PQ success rate
        attempted = values.get('Aurora_pq_request_attempted', '0')
        executed = values.get('Aurora_pq_request_executed', '0')
        try:
            attempted_count = int(attempted)
            executed_count = int(executed)
            if attempted_count > 0:
                success_rate = (executed_count / attempted_count) * 100
                if success_rate < 70:
                    recommendations.append(f'⚠️ Low PQ success rate: {success_rate:.1f}% ({executed_count}/{attempted_count}).')
                    recommendations.append('   Many queries are not using parallel query. Review query patterns and table sizes.')
                elif success_rate < 90:
                    recommendations.append(f'ℹ️ PQ success rate: {success_rate:.1f}% ({executed_count}/{attempted_count}).')
                    recommendations.append('   Some queries are not using parallel query.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_not_chosen_long_trx
        long_trx = values.get('Aurora_pq_request_not_chosen_long_trx', '0')
        try:
            long_trx_count = int(long_trx)
            if long_trx_count > 0:
                recommendations.append(f'ℹ️ Long transactions affecting PQ: Aurora_pq_request_not_chosen_long_trx = {long_trx_count}.')
                recommendations.append('   Long-running transactions prevent parallel query. Review transaction management.')
        except (ValueError, TypeError):
            pass
        
        # Add recommendations
        if recommendations:
            subsection_lines.append('  Analysis:')
            subsection_lines.append('  ' + '-' * 40)
            for rec in recommendations:
                subsection_lines.append(f'  {rec}')
            subsection_lines.append('')
        else:
            # Check if PQ is being used at all
            try:
                attempted_count = int(values.get('Aurora_pq_request_attempted', '0'))
                if attempted_count > 0:
                    subsection_lines.append('  ✓ Aurora parallel query is operating normally with no issues detected.')
                else:
                    subsection_lines.append('  ℹ️ No parallel query requests have been attempted.')
                    subsection_lines.append('     This may be normal if parallel query is disabled or workload doesn\'t benefit from it.')
            except (ValueError, TypeError):
                pass
            subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _generate_aurora_relay_log_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> str:
        """Generate plain text for Aurora Relay Log Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Get the data
        if source_id not in parsed_logs:
            no_data_message = subsection.default_note if subsection.default_note else "No data available"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ERROR: {log_data.error or "Unknown error"}
"""
        
        # Handle no data case
        if not log_data.rows:
            no_data_message = subsection.default_note if subsection.default_note else "No data returned"
            return f"""{subsection.name}
{self.SUBSECTION_SEPARATOR}
  ({no_data_message})
"""
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Build subsection text
        subsection_lines = [
            subsection.name,
            self.SUBSECTION_SEPARATOR,
        ]
        
        # Add note above the table if provided
        if subsection.note:
            subsection_lines.append(f'  Note: {subsection.note}')
            subsection_lines.append('')
        
        # Build the data table
        columns = subsection.columns if subsection.columns else log_data.columns
        col_widths = [max(len(str(col)), 10) for col in columns]
        
        # Adjust widths based on data
        for row in log_data.rows:
            for i, col in enumerate(columns):
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    val = str(row[col_idx]) if row[col_idx] is not None else ''
                    col_widths[i] = max(col_widths[i], min(len(val), 50))
        
        # Header
        header = "  | " + " | ".join(str(col).ljust(col_widths[i]) for i, col in enumerate(columns)) + " |"
        separator = "  +-" + "-+-".join("-" * w for w in col_widths) + "-+"
        
        subsection_lines.append(separator)
        subsection_lines.append(header)
        subsection_lines.append(separator)
        
        # Data rows
        for row in log_data.rows:
            cells = []
            for i, col in enumerate(columns):
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    val = str(row[col_idx]) if row[col_idx] is not None else ''
                    cells.append(val[:col_widths[i]].ljust(col_widths[i]))
                else:
                    cells.append("".ljust(col_widths[i]))
            subsection_lines.append("  | " + " | ".join(cells) + " |")
        
        subsection_lines.append(separator)
        subsection_lines.append('')
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check Aurora_in_memory_relaylog_status
        relay_status = values.get('Aurora_in_memory_relaylog_status', '')
        disabled_reason = values.get('Aurora_in_memory_relaylog_disabled_reason', '')
        if relay_status.upper() == 'DISABLED':
            reason_text = f' Reason: {disabled_reason}.' if disabled_reason else ''
            recommendations.append(f'⚠️ In-memory relay log disabled:{reason_text}')
            recommendations.append('   This may impact replica performance. Consider enabling if supported.')
        
        # Check Aurora_in_memory_relaylog_fallback_count
        fallback_count = values.get('Aurora_in_memory_relaylog_fallback_count', '0')
        try:
            fallback_int = int(fallback_count)
            if fallback_int > 0:
                recommendations.append(f'ℹ️ Relay log fallbacks: Aurora_in_memory_relaylog_fallback_count = {fallback_int}.')
                recommendations.append('   Large transactions caused fallback to persistent storage. Consider breaking up large transactions.')
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_in_memory_relaylog_recovery_count
        recovery_count = values.get('Aurora_in_memory_relaylog_recovery_count', '0')
        try:
            recovery_int = int(recovery_count)
            if recovery_int > 0:
                recommendations.append(f'ℹ️ Relay log recoveries: Aurora_in_memory_relaylog_recovery_count = {recovery_int}.')
                recommendations.append(f'   Relay log recovered from fallback state {recovery_int} times.')
        except (ValueError, TypeError):
            pass
        
        # Add recommendations
        if recommendations:
            subsection_lines.append('  Analysis:')
            subsection_lines.append('  ' + '-' * 40)
            for rec in recommendations:
                subsection_lines.append(f'  {rec}')
            subsection_lines.append('')
        else:
            if relay_status.upper() == 'ENABLED':
                subsection_lines.append('  ✓ Aurora in-memory relay log is enabled and operating normally.')
                subsection_lines.append('')
        
        return '\n'.join(subsection_lines)
    
    def _build_purge_thread_activity_text(self, parsed_logs: Dict[str, ParsedLogFile]) -> List[str]:
        """Build plain text for Purge Thread Activity table."""
        lines = ['  Purge Thread Activity:', '  ' + '-' * 40]
        
        if 'purge_thread_activity' not in parsed_logs:
            lines.append('  (Purge thread information not available)')
            lines.append('')
            return lines
        
        log_data = parsed_logs['purge_thread_activity']
        if not log_data.success or not log_data.rows or not log_data.columns:
            lines.append('  (Purge thread information not available)')
            lines.append('')
            return lines
        
        # Build table
        headers = ['Thread ID', 'Thread Name', 'Thread Type', 'State']
        col_widths = [len(h) for h in headers]
        
        # Calculate column widths
        for row in log_data.rows:
            for i, cell in enumerate(row):
                if i < len(col_widths):
                    col_widths[i] = max(col_widths[i], len(str(cell) if cell else ''))
        
        # Cap widths
        col_widths = [min(w, 30) for w in col_widths]
        
        # Header row
        header_parts = [h.ljust(col_widths[i]) for i, h in enumerate(headers)]
        lines.append('  ' + ' | '.join(header_parts))
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        lines.append('  ' + '-+-'.join(separator_parts))
        
        # Data rows
        for row in log_data.rows:
            cells = [str(cell if cell is not None else '').ljust(col_widths[i])[:col_widths[i]] for i, cell in enumerate(row)]
            lines.append('  ' + ' | '.join(cells))
        
        lines.append('')
        return lines
    
    def _build_purge_configuration_text(self, parsed_logs: Dict[str, ParsedLogFile]) -> List[str]:
        """Build plain text for Purge Configuration table."""
        lines = ['  Purge Configuration:', '  ' + '-' * 40]
        
        if 'purge_configuration' not in parsed_logs:
            lines.append('  (Purge configuration not available)')
            lines.append('')
            return lines
        
        log_data = parsed_logs['purge_configuration']
        if not log_data.success or not log_data.rows or not log_data.columns:
            lines.append('  (Purge configuration not available)')
            lines.append('')
            return lines
        
        # Build table
        headers = ['Parameter', 'Value', 'Recommendation']
        col_widths = [len(h) for h in headers]
        
        # Calculate column widths
        for row in log_data.rows:
            for i, cell in enumerate(row):
                if i < len(col_widths):
                    col_widths[i] = max(col_widths[i], len(str(cell) if cell else ''))
        
        # Cap widths
        col_widths = [min(w, 25) for w in col_widths]
        
        # Header row
        header_parts = [h.ljust(col_widths[i]) for i, h in enumerate(headers)]
        lines.append('  ' + ' | '.join(header_parts))
        
        # Separator row
        separator_parts = ['-' * w for w in col_widths]
        lines.append('  ' + '-+-'.join(separator_parts))
        
        # Data rows
        for row in log_data.rows:
            cells = [str(cell if cell is not None else '').ljust(col_widths[i])[:col_widths[i]] for i, cell in enumerate(row)]
            lines.append('  ' + ' | '.join(cells))
        
        lines.append('')
        return lines
    
    def _build_purge_performance_metrics_text(self, parsed_logs: Dict[str, ParsedLogFile]) -> List[str]:
        """Build plain text for Purge Performance Metrics."""
        lines = ['  Purge Performance Metrics:', '  ' + '-' * 40]
        
        if 'purge_performance_metrics' not in parsed_logs:
            lines.append('  (Purge performance metrics not available)')
            lines.append('')
            return lines
        
        log_data = parsed_logs['purge_performance_metrics']
        if not log_data.success or not log_data.rows or not log_data.columns:
            lines.append('  (Purge performance metrics not available)')
            lines.append('')
            return lines
        
        row = log_data.rows[0]
        purge_threads = row[0] if len(row) > 0 else 'N/A'
        undo_tablespaces = row[1] if len(row) > 1 else 'N/A'
        active_undo_tablespaces = row[2] if len(row) > 2 else 'N/A'
        
        lines.append(f'  Purge Threads:            {purge_threads}')
        lines.append(f'  Undo Tablespaces:         {undo_tablespaces}')
        lines.append(f'  Active Undo Tablespaces:  {active_undo_tablespaces}')
        lines.append('')
        return lines
    
    def _generate_table(
        self,
        display_columns: List[Tuple[str, Optional[str]]],
        rows: Optional[List[Tuple[Any, ...]]],
        column_map: Dict[str, int],
        source_columns: List[str]
    ) -> str:
        """
        Generate an ASCII table for the data.
        
        Args:
            display_columns: List of (display_name, source_column_or_expr) tuples
            rows: Data rows
            column_map: Mapping of column names to indices
            source_columns: Original column names from the query
        """
        if not rows:
            return '  (No data)'
        
        # Calculate column widths
        col_names = [col[0] for col in display_columns]
        col_widths = [len(name) for name in col_names]
        
        # Process all rows to get values and update column widths
        processed_rows = []
        for row in rows:
            # Convert row tuple to dict for expression evaluation
            row_len = len(row) if row else 0
            row_dict = {col: row[idx] for col, idx in column_map.items() if idx < row_len}
            
            row_values = []
            for idx, (display_name, source) in enumerate(display_columns):
                value = self._get_cell_value(source, row, column_map, row_dict)
                formatted = self._format_cell_value(value)
                row_values.append(formatted)
                # Update column width
                col_widths[idx] = max(col_widths[idx], len(formatted))
            
            processed_rows.append(row_values)
        
        # Cap column widths at a reasonable maximum
        max_col_width = 40
        col_widths = [min(w, max_col_width) for w in col_widths]
        
        # Build table
        table_lines = []
        
        # Header row
        header_cells = [name.ljust(col_widths[i])[:col_widths[i]] for i, name in enumerate(col_names)]
        table_lines.append('  ' + ' | '.join(header_cells))
        
        # Separator row
        separator_cells = [self.TABLE_SEPARATOR * col_widths[i] for i in range(len(col_names))]
        table_lines.append('  ' + '-+-'.join(separator_cells))
        
        # Data rows
        for row_values in processed_rows:
            cells = [val.ljust(col_widths[i])[:col_widths[i]] for i, val in enumerate(row_values)]
            table_lines.append('  ' + ' | '.join(cells))
        
        return '\n'.join(table_lines)
    
    def _resolve_columns(
        self,
        columns: List[Union[str, 'ComputedColumn']],
        column_map: Dict[str, int]
    ) -> List[Tuple[str, Optional[str]]]:
        """
        Resolve column definitions to (display_name, source) tuples.
        
        Returns list of tuples where:
        - display_name: The column header to show
        - source: Either a column name or an expression string (prefixed with 'expr:')
        """
        # Import here to avoid circular imports
        from diagnostic_collector_mysql.models import ComputedColumn
        
        resolved = []
        for col in columns:
            if isinstance(col, str):
                # Simple column reference
                if col in column_map:
                    resolved.append((col, col))
            elif isinstance(col, ComputedColumn):
                # Computed column
                resolved.append((col.name, f'expr:{col.expr}'))
            elif isinstance(col, dict):
                # Dict format from JSON parsing
                if 'name' in col and 'expr' in col:
                    resolved.append((col['name'], f"expr:{col['expr']}"))
        
        return resolved
    
    def _get_cell_value(
        self,
        source: Optional[str],
        row: Tuple[Any, ...],
        column_map: Dict[str, int],
        row_dict: Dict[str, Any]
    ) -> Any:
        """Get the value for a cell, handling both direct columns and expressions."""
        if source is None:
            return None
        
        if source.startswith('expr:'):
            # Evaluate expression
            expr = source[5:]
            return self._evaluate_expression(expr, row_dict)
        else:
            # Direct column reference
            if source in column_map:
                idx = column_map[source]
                return row[idx] if idx < len(row) else None
            return None
    
    def _evaluate_expression(self, expr: str, row_dict: Dict[str, Any]) -> Optional[float]:
        """
        Evaluate a computed column expression.
        
        Uses caching to avoid re-parsing the same expression multiple times.
        """
        if expr not in self._expression_cache:
            self._expression_cache[expr] = ExpressionEvaluator(expr)
        
        return self._expression_cache[expr].evaluate(row_dict)
    
    def _check_condition(
        self,
        condition: Optional[Dict[str, List[str]]],
        engine_type: Optional[str],
        min_engine_version: Optional[str] = None,
        max_engine_version: Optional[str] = None
    ) -> bool:
        """Check if a subsection condition is met (engine and version filtering)."""
        # Check engine condition
        if condition is not None:
            if 'engine' in condition and engine_type:
                allowed_engines = condition['engine']
                if engine_type not in allowed_engines:
                    return False

        # Check version conditions
        if min_engine_version is not None and max_engine_version is not None:
            if min_engine_version:
                min_version = self._parse_version(min_engine_version)
                if self._database_version < min_version:
                    return False
            if max_engine_version:
                max_version = self._parse_version(max_engine_version)
                if self._database_version > max_version:
                    return False

        return True
    
    def _get_section_overview(self, section_name: str) -> str:
        """
        Get the overview text for a section.
        
        Include Section_Overview at the start of each section
        """
        # Default overviews for common sections
        overviews = {
            'Server Information & Version Details': 'Server configuration and status information including version, uptime, and crucial configurations.',
            'Non-Default Configuration Variables': 'Variables that differ from their compiled defaults.',
            'Replication Topology & Status': 'Replication configuration, status, and lag information.',
            'Performance Metrics': 'Performance metrics reveal system load, query patterns, and potential bottlenecks. High slow query counts, connection issues, or table cache misses indicate areas needing optimization.',
            'Memory Configuration & Usage - Comprehensive Analysis': 'Memory configuration affects performance dramatically. The InnoDB buffer pool should be 70-80% of available RAM. Buffer pool hit rate >99% is ideal. Per-connection buffers multiply by max_connections, so should be configured appropriately. This section provides comprehensive memory analysis using Performance Schema instrumentation.',
            'InnoDB Engine & Redo Log': 'InnoDB redo logs record all changes for crash recovery. If redo logs completely utilized, writes operations will be stalled. History list length indicates purge lag - high values cause table bloat and slow queries. Monitor these closely!',
            'Table Analysis': 'Table statistics, sizes, and potential issues.',
            'Index Analysis': 'Index usage, redundant indexes, and optimization opportunities.',
            'Security Configuration': 'User accounts, privileges, and security configuration.',
            'Connection Management': 'Connection limits prevent new connections when reached. High connection churn indicates connection pooling issues. Monitor to prevent "Too many connections" errors.',
            'Lock Analysis': 'Lock waits, deadlocks, and contention metrics.',
            'I/O Analysis': 'I/O operations are typically the slowest part of database operations. Identifying tables and files with high I/O helps pinpoint performance bottlenecks and guides optimization efforts.',
            'Database Objects': 'Triggers, events, and stored procedures can impact performance and complicate troubleshooting. Understanding what automated processes are running helps diagnose unexpected behavior.',
            'SQL Performance': 'Query performance analysis and problematic queries.',
            'Database User Activity': 'User activity and connection patterns.',
        }
        
        return overviews.get(section_name, f'Information about {section_name}.')
    
    def _format_cell_value(self, value: Any) -> str:
        """Format a cell value for display in plain text."""
        if value is None:
            return 'NULL'
        
        if isinstance(value, float):
            # Format floats with reasonable precision
            if value == int(value):
                return str(int(value))
            return f'{value:.2f}'
        
        return str(value)
    
    def _wrap_text(self, text: str, width: int, indent: str = '') -> str:
        """
        Wrap text to fit within a specified width.
        
        Args:
            text: Text to wrap
            width: Maximum line width
            indent: Indentation for continuation lines
            
        Returns:
            Wrapped text with continuation lines indented
        """
        if len(text) <= width:
            return text
        
        lines = []
        current_line = ''
        words = text.split()
        
        for word in words:
            if not current_line:
                current_line = word
            elif len(current_line) + 1 + len(word) <= width:
                current_line += ' ' + word
            else:
                lines.append(current_line)
                current_line = indent + word
        
        if current_line:
            lines.append(current_line)
        
        return '\n'.join(lines)
    
    def _generate_footer(self) -> str:
        """Generate the report footer."""
        footer_lines = [
            '',
            self.SECTION_SEPARATOR,
            'Diagnostic Collector for MySQL Report Complete',
            f'Generated at: {self._timestamp}',
            'Review all sections carefully. Address any CRITICAL or WARNING items promptly.',
            f'Diagnostic Collector for MySQL v{__version__} | Compatible with RDS/Aurora MySQL, Community MySQL 8.0+ and RDS MariaDB, Community MariaDB 11.4+',
            self.SECTION_SEPARATOR,
        ]
        
        return '\n'.join(footer_lines)


import json


class JSONReportGenerator:
    """
    Generates JSON reports from parsed query log data.
    
    This class creates a structured JSON document with:
    - metadata: timestamp, database info
    - health_overview: critical issues count, warnings count, successful queries
    - sections: array of section objects, each with:
      - name: section name
      - subsections: array of subsection objects, each with:
        - name: subsection name
        - source: query ID
        - columns: array of column names
        - rows: array of row arrays
        - truncated: boolean
        - total_count: total rows before truncation
    
    Responsibilities:
    - Generate JSON report when --format json is specified
    - Include Health Overview section
    - Organize report by sections defined in report_format.json
    - Apply collection limit to each subsection
    - Default to 50 rows per subsection when not specified
    """
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        extended_checks: bool = False
    ):
        """
        Initialize the JSONReportGenerator.
        
        Args:
            report_format: ReportFormat object defining the report structure
            collection_limit: Maximum rows per subsection (default: 50)
            timestamp: Report generation timestamp (default: current time)
            database_info: Dictionary with database information (host, version, etc.)
            extended_checks: Whether extended checks are enabled (default: False)
        """
        self._report_format = report_format
        self._limit_enforcer = CollectionLimitEnforcer(collection_limit)
        self._timestamp = timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._database_info = database_info or {}
        self._expression_cache: Dict[str, ExpressionEvaluator] = {}
        self._extended_checks = extended_checks
        # Parse database version for version filtering
        self._database_version = self._parse_version(self._database_info.get('version', ''))

    def _parse_version(self, version_string: str) -> tuple:
        """Parse version string into a tuple of integers for comparison."""
        if not version_string:
            return (0, 0, 0)
        match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', version_string)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0
            return (major, minor, patch)
        return (0, 0, 0)
    
    def _is_transaction_consumer_enabled(self, parsed_logs: Dict[str, 'ParsedLogFile']) -> bool:
        """Check if the events_transactions_current consumer is enabled in performance_schema."""
        consumer_status = parsed_logs.get('events_transactions_consumer_status')
        if consumer_status and consumer_status.success and consumer_status.rows:
            # Check if ENABLED column value is 'YES'
            if consumer_status.columns:
                try:
                    enabled_idx = consumer_status.columns.index('ENABLED')
                    return consumer_status.rows[0][enabled_idx].upper() == 'YES'
                except (ValueError, IndexError):
                    pass
        return False
    
    def _evaluate_config_warnings(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> List[Tuple[str, str]]:
        """
        Evaluate configuration parameters and return warnings for problematic values.
        
        This method delegates to the module-level evaluate_config_warnings function.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            
        Returns:
            List of (Parameter, Message) tuples for triggered warnings.
            Empty list if no warnings or query data not available.
        """
        return evaluate_config_warnings(parsed_logs)
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """
        Generate a complete JSON report from parsed log data.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            engine_type: The detected database engine type (for condition filtering)
            
        Returns:
            Complete JSON document as a string
        """
        report = {
            'metadata': self._generate_metadata(parsed_logs, engine_type),
            'health_overview': self._generate_health_overview(parsed_logs, engine_type),
            'sections': self._generate_sections(parsed_logs, engine_type),
            'footer': self._generate_footer(),
        }
        
        return json.dumps(report, indent=2, default=self._json_serializer)
    
    def _generate_footer(self) -> Dict[str, str]:
        """Generate the report footer."""
        return {
            'title': 'Diagnostic Collector for MySQL Report Complete',
            'generated_at': self._timestamp,
            'notice': 'Review all sections carefully. Address any CRITICAL or WARNING items promptly.',
            'version': f'Diagnostic Collector for MySQL v{__version__}',
            'compatibility': 'Compatible with RDS/Aurora MySQL, Community MySQL 8.0+ and RDS MariaDB, Community MariaDB 11.4+'
        }
    
    def _json_serializer(self, obj: Any) -> Any:
        """
        Custom JSON serializer for objects not serializable by default.
        
        Handles:
        - datetime objects
        - bytes objects
        - Other non-serializable objects (converts to string)
        """
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return obj.decode('utf-8', errors='replace')
        return str(obj)
    
    def _generate_metadata(self, parsed_logs: Optional[Dict[str, ParsedLogFile]] = None, engine_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Generate the metadata section.
        
        Returns:
            Dictionary with timestamp and database info
        """
        engine_type = engine_type or self._database_info.get('engine_type')
        
        # Helper to get value from parsed_logs
        def get_log_value(query_id: str, column_name: str) -> Optional[str]:
            if parsed_logs and query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return str(log_data.rows[0][col_idx])
                    except (ValueError, IndexError):
                        pass
            return None
        
        metadata = {
            'timestamp': self._timestamp,
            'database': {
                'host': self._database_info.get('host'),
                'version': self._database_info.get('version'),
                'engine_type': engine_type,
            }
        }
        
        # Add Aurora-specific fields if Aurora MySQL
        if engine_type == 'aurora-mysql':
            aurora_server_id = get_log_value('aurora_server_id', 'aurora_server_id')
            aurora_version = get_log_value('aurora_version', 'aurora_version')
            innodb_read_only = get_log_value('server_read_only', 'innodb_read_only')
            
            metadata['database']['aurora_server_id'] = aurora_server_id
            metadata['database']['aurora_version'] = aurora_version
            metadata['database']['innodb_read_only'] = innodb_read_only in ('1', 'ON', 'True')
            metadata['database']['is_reader'] = innodb_read_only in ('1', 'ON', 'True')
        
        return metadata
    
    def _generate_health_overview(self, parsed_logs: Dict[str, ParsedLogFile], engine_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Generate the health overview section.
        
        Include Health Overview section
        
        Returns:
            Dictionary with health checks, counts, and server info
        """
        # Use shared health check evaluation
        health_checks = _evaluate_health_checks_common(parsed_logs, self._evaluate_config_warnings)
        
        # Count by status
        critical_count = sum(1 for h in health_checks if h[0] == 'critical')
        warning_count = sum(1 for h in health_checks if h[0] == 'warning')
        ok_count = sum(1 for h in health_checks if h[0] == 'ok')
        
        # Determine server type
        engine_type = engine_type or self._database_info.get('engine_type', 'mysql')
        if engine_type == 'aurora-mysql':
            server_type = 'Aurora'
        elif engine_type == 'mariadb':
            server_type = 'MariaDB'
        else:
            server_type = 'MySQL'
        
        # Build Aurora Cluster info if Aurora
        aurora_cluster = None
        if engine_type == 'aurora-mysql' and 'aurora_cluster_info' in parsed_logs:
            log_data = parsed_logs['aurora_cluster_info']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    writer_idx = log_data.columns.index('writer_count')
                    reader_idx = log_data.columns.index('reader_count')
                    total_idx = log_data.columns.index('total_instances')
                    
                    aurora_cluster = {
                        'total_instances': int(log_data.rows[0][total_idx]),
                        'writer_count': int(log_data.rows[0][writer_idx]),
                        'reader_count': int(log_data.rows[0][reader_idx]),
                    }
                except (ValueError, IndexError):
                    pass
        
        # Build health checks list for JSON
        checks_list = []
        for status, check_name, details in health_checks:
            checks_list.append({
                'status': status,
                'check': check_name,
                'details': details
            })
        
        result = {
            'critical_count': critical_count,
            'warning_count': warning_count,
            'ok_count': ok_count,
            'server_type': server_type,
            'health_checks': checks_list,
        }
        
        if aurora_cluster:
            result['aurora_cluster'] = aurora_cluster
        
        return result
    
    def _generate_sections(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> List[Dict[str, Any]]:
        """
        Generate all report sections.
        
        Organize report by sections defined in report_format.json
        
        Returns:
            List of section dictionaries
        """
        sections = []
        
        for section in self._report_format.sections:
            section_data = self._generate_section(section, parsed_logs, engine_type)
            if section_data:
                sections.append(section_data)
        
        return sections
    
    def _generate_section(
        self,
        section: 'ReportSection',
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Generate a single report section.
        
        Args:
            section: ReportSection object
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            engine_type: The detected database engine type
            
        Returns:
            Section dictionary or None if no subsections have data
        """
        subsections = []
        has_data = False
        
        for subsection in section.subsections:
            # Check condition (engine and version filtering)
            if not self._check_condition(
                subsection.condition,
                engine_type,
                subsection.min_engine_version,
                subsection.max_engine_version
            ):
                continue
            
            subsection_data = self._generate_subsection(subsection, parsed_logs)
            if subsection_data:
                subsections.append(subsection_data)
                # Track if we have any subsections (even with no data)
                has_data = True
        
        # Skip section only if no subsections were generated at all
        if not has_data:
            return None
        
        result = {
            'name': section.name,
            'subsections': subsections,
        }
        
        # Add section-level note
        section_note = self._get_section_overview(section.name)
        if section_note:
            result['note'] = section_note
        
        # Add engine-specific notes for certain sections
        if section.name == 'I/O Analysis' and engine_type == 'aurora-mysql':
            result['engine_note'] = 'Aurora MySQL does not instrument physical I/O (file-level bytes) the same way as Community MySQL due to its distributed storage architecture. However, logical I/O (table-level row operations) is fully instrumented and available.'
        
        # Add Binary Logging Configuration Advice for Replication section
        if section.name == 'Replication Topology & Status':
            # Add Transaction Dependency Tracking (only for MySQL < 8.1)
            dep_tracking_data = self._generate_transaction_dependency_tracking_json(parsed_logs)
            if dep_tracking_data:
                result['transaction_dependency_tracking'] = dep_tracking_data
            
            binary_logging_advice = self._generate_binary_logging_advice(parsed_logs, engine_type)
            if binary_logging_advice:
                result['binary_logging_advice'] = binary_logging_advice
        
        return result
    
    def _generate_transaction_dependency_tracking_json(
        self,
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Optional[Dict[str, Any]]:
        """
        Generate Transaction Dependency Tracking section in JSON format.
        
        Only shown for MySQL < 8.1 (binlog_transaction_dependency_tracking was removed in 8.1+).
        
        Returns:
            Dictionary with dependency tracking information or None if not applicable
        """
        # Only show for MySQL < 8.1
        if self._database_version >= (8, 1, 0):
            return None
        
        # Get dependency tracking value
        dep_tracking = 'N/A'
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    if 'binlog_transaction_dependency_tracking' in log_data.columns:
                        idx = log_data.columns.index('binlog_transaction_dependency_tracking')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            dep_tracking = str(val)
                except (ValueError, IndexError, TypeError):
                    pass
        
        result = {
            'note': 'Transaction dependency tracking determines which transactions can run in parallel on replicas. This is CRITICAL for multi-threaded replication performance!',
            'methods': [
                {
                    'name': 'COMMIT_ORDER',
                    'description': 'Uses binary log group commit. Transactions in same group commit can run in parallel.',
                    'performance': 'Limited parallelism',
                    'use_case': 'Default, safe but slower'
                },
                {
                    'name': 'WRITESET',
                    'description': 'Tracks row hashes modified by each transaction. Transactions modifying different rows can run in parallel.',
                    'performance': 'High parallelism',
                    'use_case': 'Recommended for most workloads'
                },
                {
                    'name': 'WRITESET_SESSION',
                    'description': 'Like WRITESET but preserves order within same session.',
                    'performance': 'High parallelism',
                    'use_case': 'Best for applications needing session consistency'
                }
            ],
            'current_configuration': {
                'value': dep_tracking,
                'optimal': dep_tracking in ('WRITESET', 'WRITESET_SESSION'),
            },
            'key_insight': 'WRITESET tracking allows transactions that modify different rows to execute in parallel on replicas, even if they were in different group commits on the source. This dramatically improves replication throughput and reduces lag.',
            'important_note': 'The binlog_transaction_dependency_tracking setting must be configured on the SOURCE/PRIMARY server, not on replicas. Replicas read the dependency information from the binary logs generated by the source.'
        }
        
        if dep_tracking == 'COMMIT_ORDER':
            result['current_configuration']['status'] = 'warning'
            result['current_configuration']['recommendation'] = 'Consider switching to WRITESET or WRITESET_SESSION for better replication parallelism. This can significantly reduce replication lag!'
            result['current_configuration']['commands'] = [
                "SET GLOBAL binlog_transaction_dependency_tracking = 'WRITESET';",
                "SET GLOBAL transaction_write_set_extraction = 'XXHASH64';"
            ]
        elif dep_tracking in ('WRITESET', 'WRITESET_SESSION'):
            result['current_configuration']['status'] = 'ok'
            result['current_configuration']['recommendation'] = 'Good configuration for multi-threaded replication parallelism.'
        else:
            result['current_configuration']['status'] = 'unknown'
            result['current_configuration']['recommendation'] = 'The binlog_transaction_dependency_tracking variable is not available or not configured.'
        
        return result
    
    def _get_section_overview(self, section_name: str) -> Optional[str]:
        """
        Get the overview text for a section.
        
        Args:
            section_name: Name of the section
            
        Returns:
            Overview text or None if not defined
        """
        overviews = {
            'Server Information & Version Details': 'Server configuration and status information including version, uptime, and crucial configurations.',
            'Non-Default Configuration Variables': 'Variables that differ from their compiled defaults.',
            'Replication Topology & Status': 'Replication configuration, status, and lag information.',
            'Performance Metrics': 'Performance metrics reveal system load, query patterns, and potential bottlenecks. High slow query counts, connection issues, or table cache misses indicate areas needing optimization.',
            'Memory Configuration & Usage - Comprehensive Analysis': 'Memory configuration affects performance dramatically. The InnoDB buffer pool should be 70-80% of available RAM. Buffer pool hit rate >99% is ideal. Per-connection buffers multiply by max_connections, so should be configured appropriately. This section provides comprehensive memory analysis using Performance Schema instrumentation.',
            'InnoDB Engine & Redo Log': 'InnoDB redo logs record all changes for crash recovery. If redo logs completely utilized, writes operations will be stalled. History list length indicates purge lag - high values cause table bloat and slow queries. Monitor these closely!',
            'Table Analysis': 'Table statistics, sizes, and potential issues.',
            'Index Analysis': 'Index usage, redundant indexes, and optimization opportunities.',
            'Security Configuration': 'User accounts, privileges, and security configuration.',
            'Connection Management': 'Connection limits prevent new connections when reached. High connection churn indicates connection pooling issues. Monitor to prevent "Too many connections" errors.',
            'Lock Analysis': 'Lock waits, deadlocks, and contention metrics.',
            'I/O Analysis': 'I/O operations are typically the slowest part of database operations. Identifying tables and files with high I/O helps pinpoint performance bottlenecks and guides optimization efforts.',
            'Database Objects': 'Triggers, events, and stored procedures can impact performance and complicate troubleshooting. Understanding what automated processes are running helps diagnose unexpected behavior.',
            'SQL Performance': 'Query performance analysis and problematic queries.',
            'Database User Activity': 'User activity and connection patterns.',
        }
        return overviews.get(section_name, f'Information about {section_name}.')
    
    def _generate_binary_logging_advice(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Generate Binary Logging Configuration Advice in JSON format.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            engine_type: The detected database engine type
            
        Returns:
            Dictionary with binary logging advice or None if data unavailable
        """
        # Helper to get value from parsed logs
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log = parsed_logs[query_id]
                if log.success and log.rows and log.columns:
                    try:
                        idx = log.columns.index(column_name)
                        return log.rows[0][idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        # Get replication role
        replication_role = get_value('replication_role', 'replication_role', 'UNKNOWN')
        log_bin = get_value('replication_role', 'log_bin_enabled', 0)
        
        # Get sync settings
        sync_binlog = get_value('config_warnings_check', 'sync_binlog', 'N/A')
        flush_log = get_value('config_warnings_check', 'innodb_flush_log_at_trx_commit', 'N/A')
        
        # Build the advice structure
        role_text = 'Source/Standalone' if replication_role != 'REPLICA' else 'Replica'
        binlog_enabled = log_bin in (1, '1', 'ON', True)
        binlog_text = 'Enabled' if binlog_enabled else 'Disabled'
        binlog_rec = 'Binary logs should be enabled on source servers for replication and PITR.' if replication_role != 'REPLICA' else 'Binary logs optional on leaf replicas unless chaining.'
        
        sync_rec = 'Safest setting (ACID compliant). Every commit syncs binlog to disk.' if str(sync_binlog) == '1' else 'Consider sync_binlog=1 for durability.'
        flush_rec = 'Safest setting (ACID compliant). Every commit syncs redo log to disk.' if str(flush_log) == '1' else 'Consider innodb_flush_log_at_trx_commit=1 for durability.'
        
        advice = {
            'note': 'Binary logs are NOT required on replicas unless the replica is also a source for another replica [chained replication] (or) binary logs data used for data streaming services (or) you need point-in-time recovery from the replica.',
            'current_configuration': {
                'server_role': {
                    'value': role_text,
                    'description': f'This is a {role_text.lower()} server'
                },
                'binary_logging': {
                    'value': binlog_text,
                    'enabled': binlog_enabled,
                    'recommendation': binlog_rec
                },
                'sync_binlog': {
                    'value': str(sync_binlog),
                    'recommendation': sync_rec
                },
                'innodb_flush_log_at_trx_commit': {
                    'value': str(flush_log),
                    'recommendation': flush_rec
                }
            },
            'best_practices': [
                'Source Servers: Enable binary logging (log_bin=ON) with sync_binlog=1 for durability',
                'Leaf Replicas: Binary logging optional. Disable to save I/O unless needed for chaining or PITR',
                'Intermediate Replicas: Enable binary logging if this replica is a source for other replicas',
                'ACID Compliance: Use sync_binlog=1 AND innodb_flush_log_at_trx_commit=1 for full durability',
                'Performance vs Durability: Lower sync values improve performance but risk data loss on crash'
            ]
        }
        
        # Check if general_log is enabled
        try:
            general_log_value = get_value('logging_status', 'general_log', 0)
            if general_log_value in (1, '1', 'ON', True):
                advice['performance_warning'] = 'The general query log (general_log=ON) logs ALL queries and can severely impact performance on busy systems. Use only for short-term debugging. The slow query log (slow_query_log=ON) has less impact but should still be used judiciously with appropriate long_query_time settings.'
        except Exception:
            pass
        
        return advice
    
    def _generate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """
        Generate a single report subsection.
        
        Behavior:
        - Apply collection limit to each subsection
        - Default to 50 rows per subsection
        
        Args:
            subsection: ReportSubsection object
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            
        Returns:
            Subsection dictionary with name, source, columns, rows, truncated, total_count
        """
        source_id = subsection.source
        
        # Check if subsection requires extended checks and they are disabled
        if subsection.requires_extended_checks and not self._extended_checks:
            # 3-tier fallback logic:
            # 1. If --extended-checks enabled -> Use innodb_trx query (handled above, we're in the disabled case)
            # 2. If fallback_source exists and transaction consumer is enabled -> Use PS query
            # 3. Else -> Display message IN PLACE OF result set
            
            if subsection.fallback_source and self._is_transaction_consumer_enabled(parsed_logs):
                # Use fallback source (performance_schema query)
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success:
                    # Use fallback columns if specified, otherwise use original columns
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    # Create a temporary subsection with fallback source
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        requires_extended_checks=False  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
            
            # No fallback available or consumer not enabled - show disabled message
            disabled_note = subsection.extended_checks_disabled_note or "Extended checks disabled. Use --extended-checks to enable."
            return {
                'name': subsection.name,
                'source': source_id,
                'extended_checks_disabled': True,
                'extended_checks_disabled_note': disabled_note,
                'note': subsection.note,
                'columns': [],
                'rows': [],
                'truncated': False,
                'total_count': 0,
            }
        
        # Special handling for Configuration Warnings subsection
        if source_id == 'config_warnings_check':
            return self._generate_config_warnings_subsection(subsection, parsed_logs)
        
        # Special handling for Key Performance Counters subsection
        if source_id == 'key_performance_counters':
            return self._generate_key_performance_counters_subsection(subsection, parsed_logs)
        
        # Special handling for Transaction Summary subsection
        if source_id == 'transaction_summary':
            return self._generate_transaction_summary_subsection(subsection, parsed_logs)
        
        # Special handling for Table Cache Statistics subsection
        if source_id == 'table_cache_stats':
            return self._generate_table_cache_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Temporary Table Usage subsection
        if source_id == 'temp_table_usage':
            return self._generate_temp_table_usage_subsection(subsection, parsed_logs)
        
        # Special handling for Memory Troubleshooting Tips subsection
        if source_id == 'memory_troubleshooting_tips':
            return self._generate_memory_troubleshooting_tips_subsection(subsection)
        
        # Special handling for Aurora InnoDB Note subsection
        if source_id == 'aurora_innodb_note':
            return self._generate_aurora_innodb_note_subsection(subsection)
        
        # Special handling for InnoDB Purge Analysis subsection
        if source_id == 'innodb_purge_analysis':
            return self._generate_innodb_purge_analysis_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Engine Status Analysis subsection
        if source_id == 'innodb_engine_status_analysis':
            return self._generate_innodb_engine_status_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Analysis subsection
        if source_id == 'long_running_transactions_analysis':
            return self._generate_long_running_transactions_subsection(subsection, parsed_logs)
        
        # Special handling for Tables Without Primary Key subsection
        if source_id == 'tables_without_pk':
            return self._generate_tables_without_pk_subsection(subsection, parsed_logs)
        # Special handling for Unused Indexes subsection
        if source_id == 'unused_indexes':
            return self._generate_unused_indexes_subsection(subsection, parsed_logs)

        
        # Special handling for Locked Accounts subsection
        if source_id == 'locked_accounts':
            return self._generate_locked_accounts_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Configuration subsection
        if source_id == 'ssl_config':
            return self._generate_ssl_config_subsection(subsection, parsed_logs)
        
        # Special handling for SSL Connection Status subsection
        if source_id == 'ssl_connection_status':
            return self._generate_ssl_connection_status_subsection(subsection, parsed_logs)
        
        # Special handling for Connection Memory Overhead subsection
        if source_id == 'connection_memory_overhead':
            return self._generate_connection_memory_overhead_subsection(subsection, parsed_logs)
        
        # Special handling for Sort & Join Buffer Memory subsection
        if source_id == 'sort_join_buffer_memory':
            return self._generate_sort_join_buffer_memory_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Buffer Pool Statistics (Detailed) subsection
        if source_id == 'innodb_buffer_pool_stats_detailed':
            return self._generate_innodb_buffer_pool_stats_subsection(subsection, parsed_logs)
        
        # Special handling for Per-Connection Memory Buffers subsection
        if source_id == 'per_connection_memory':
            return self._generate_per_connection_memory_subsection(subsection, parsed_logs)
        
        # Special handling for Global Memory Usage - Top Components subsection
        if source_id == 'global_memory_usage_top':
            return self._generate_global_memory_usage_top_subsection(subsection, parsed_logs)
        
        # Special handling for InnoDB Row Operations subsection (metric cards)
        if source_id == 'innodb_row_operations':
            return self._generate_innodb_row_operations_subsection(subsection, parsed_logs)
        
        # Special handling for Buffer Pool Hit Rate subsection (metric cards)
        if source_id == 'health_buffer_pool_hit_rate':
            return self._generate_buffer_pool_hit_rate_subsection(subsection, parsed_logs)
        
        # Special handling for All Current Transactions Summary subsection (metric cards)
        if source_id == 'current_transactions_summary':
            return self._generate_current_transactions_summary_subsection(subsection, parsed_logs)
        
        # Special handling for History List Length subsection (metric cards)
        if source_id == 'innodb_history_length':
            return self._generate_history_list_length_subsection(subsection, parsed_logs)
        
        # Special handling for Parallel Replication Worker Stats subsection
        if source_id == 'parallel_replication_worker_stats':
            return self._generate_parallel_replication_subsection(subsection, parsed_logs)
        
        # Special handling for Long-Running Transactions Activity subsection
        if source_id == 'long_running_trx_recent_queries':
            return self._generate_long_running_trx_activity_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Memory Status subsection (dynamic recommendations)
        if source_id == 'aurora_memory_status':
            return self._generate_aurora_memory_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Parallel Query Status subsection (dynamic recommendations)
        if source_id == 'aurora_pq_status':
            return self._generate_aurora_pq_status_subsection(subsection, parsed_logs)
        
        # Special handling for Aurora Relay Log Status subsection (dynamic recommendations)
        if source_id == 'aurora_relay_log_status':
            return self._generate_aurora_relay_log_status_subsection(subsection, parsed_logs)
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            # Primary source not available - check for fallback
            if subsection.fallback_source:
                fallback_log = parsed_logs.get(subsection.fallback_source)
                if fallback_log and fallback_log.success and fallback_log.rows:
                    # Use fallback source
                    columns_to_use = subsection.fallback_columns if subsection.fallback_columns else subsection.columns
                    from dataclasses import replace
                    fallback_subsection = replace(
                        subsection,
                        source=subsection.fallback_source,
                        columns=columns_to_use,
                        fallback_source=None  # Prevent recursion
                    )
                    return self._generate_subsection(fallback_subsection, parsed_logs)
                elif subsection.fallback_requires_extended_checks and not self._extended_checks:
                    # Fallback requires extended checks which are disabled
                    disabled_note = subsection.extended_checks_disabled_note or "Enable --extended-checks to view this data."
                    result['extended_checks_disabled'] = True
                    result['extended_checks_disabled_note'] = disabled_note
                    if subsection.note:
                        result['note'] = subsection.note
                    return result
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case - include default_note if provided
        if not log_data.columns or not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return result
        
        # Set column names
        result['columns'] = [col[0] for col in display_columns]
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        result['truncated'] = was_truncated
        result['total_count'] = total_count
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Process rows
        if limited_rows:
            result['rows'] = self._process_rows(
                display_columns,
                limited_rows,
                column_map
            )
            
            # Add result_note when there are results
            if subsection.result_note:
                result['result_note'] = subsection.result_note
            
            # Add warning for Unused Indexes subsection when it has data
            if subsection.name == "Unused Indexes":
                result['warning'] = (
                    "⚠️ Important - Safe Index Removal Process: "
                    "DO NOT drop indexes immediately! Indexes may be used infrequently "
                    "(daily/weekly/monthly reports, batch jobs, etc.). "
                    "Recommended approach (MySQL 8.0+): "
                    "1. Make the index INVISIBLE first: ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE; "
                    "2. Monitor for several weeks/months to ensure no queries break; "
                    "3. If no issues, drop the index: ALTER TABLE schema.table DROP INDEX index_name; "
                    "4. If queries break, make visible again: ALTER TABLE schema.table ALTER INDEX index_name VISIBLE; "
                    "Why: Invisible indexes are maintained by MySQL but not used by the optimizer. "
                    "This allows safe testing without the risk of losing the index structure. "
                    "Available in MySQL 8.0+ and Aurora MySQL 2.x+."
                )
            # Add warning from subsection config if provided
            elif subsection.warning:
                result['warning'] = f"⚠️ {subsection.warning}"
        
        return result
    
    def _generate_config_warnings_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """
        Generate JSON for the Configuration Warnings subsection.
        
        This method transforms the raw config_warnings_check query data into
        warning rows using the evaluate_config_warnings function.
        """
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': ['Parameter', 'Message'],
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        # Evaluate config warnings using the module-level function
        warnings = evaluate_config_warnings(parsed_logs)
        
        if not warnings:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Convert warnings to rows
        result['rows'] = [[param, message] for param, message in warnings]
        result['total_count'] = len(warnings)
        
        return result
    
    def _generate_key_performance_counters_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """
        Generate JSON for the Key Performance Counters subsection.
        
        This method transforms the raw key_performance_counters query data into
        a single row with calculated metrics.
        """
        # Define the columns for display
        columns = ["Queries Per Second", "Total Queries", "Slow Queries", "Threads Connected", "Threads Running", "Aborted Connections", "Aborted Clients"]
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': columns,
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        # Transform the data using the module-level function
        transformed_rows = evaluate_key_performance_counters(parsed_logs, 0)
        
        if not transformed_rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Convert tuples to lists for JSON
        result['rows'] = [list(row) for row in transformed_rows]
        result['total_count'] = len(transformed_rows)
        
        return result
    
    def _generate_transaction_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Transaction Summary subsection with metric data."""
        source_id = subsection.source
        
        # Define the columns for display
        columns = ["Total Transactions", "Oldest Trx Age (seconds)", "Total Rows Locked", "Total Modified Rows"]
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': columns,
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        # Get parsed log data for this source
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Extract values from the row
        # Columns: total_transactions, oldest_trx_age_seconds, total_rows_locked, total_modified_rows
        row = parsed_log.rows[0]
        total_transactions = row[0] if row[0] is not None else 0
        oldest_trx_age = row[1] if row[1] is not None else 0
        total_rows_locked = row[2] if row[2] is not None else 0
        total_modified_rows = row[3] if row[3] is not None else 0
        
        # Store as a single row with formatted values
        result['rows'] = [[total_transactions, oldest_trx_age, total_rows_locked, total_modified_rows]]
        result['total_count'] = 1
        
        return result
    
    def _generate_innodb_row_operations_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for InnoDB Row Operations subsection with metric data."""
        source_id = subsection.source
        
        columns = ["Rows Read", "Rows Inserted", "Rows Updated", "Rows Deleted"]
        
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': columns,
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        if subsection.note:
            result['note'] = subsection.note
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        row = parsed_log.rows[0]
        rows_read = row[0] if row[0] is not None else 0
        rows_inserted = row[1] if row[1] is not None else 0
        rows_updated = row[2] if row[2] is not None else 0
        rows_deleted = row[3] if row[3] is not None else 0
        
        result['rows'] = [[rows_read, rows_inserted, rows_updated, rows_deleted]]
        result['total_count'] = 1
        
        return result
    
    def _generate_buffer_pool_hit_rate_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Buffer Pool Hit Rate subsection with metric data."""
        source_id = subsection.source
        
        columns = ["Buffer Pool Hit Rate"]
        
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': columns,
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        if subsection.note:
            result['note'] = subsection.note
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        row = parsed_log.rows[0]
        hit_rate = row[0] if row[0] is not None else "N/A"
        
        result['rows'] = [[hit_rate]]
        result['total_count'] = 1
        
        return result
    
    def _generate_current_transactions_summary_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for All Current Transactions Summary subsection with metric data."""
        source_id = subsection.source
        
        columns = ["Total Active Transactions", "Oldest Transaction Age (seconds)"]
        
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': columns,
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        if subsection.note:
            result['note'] = subsection.note
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        row = parsed_log.rows[0]
        total_active = row[0] if row[0] is not None else 0
        oldest_age = row[1] if row[1] is not None else 0
        
        result['rows'] = [[total_active, oldest_age]]
        result['total_count'] = 1
        
        return result
    
    def _generate_history_list_length_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for History List Length subsection with metric data."""
        source_id = subsection.source
        
        columns = ["History List Length"]
        
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': columns,
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        if subsection.note:
            result['note'] = subsection.note
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        parsed_log = parsed_logs[source_id]
        
        if not parsed_log.success or not parsed_log.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        row = parsed_log.rows[0]
        history_length = row[0] if row[0] is not None else 0
        
        result['rows'] = [[history_length]]
        result['total_count'] = 1
        
        return result
    
    def _generate_parallel_replication_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Parallel Replication Worker Stats subsection with interpretation."""
        def get_value(query_id: str, column_name: str, default=None):
            if query_id in parsed_logs:
                log_data = parsed_logs[query_id]
                if log_data.success and log_data.rows and log_data.columns:
                    try:
                        col_idx = log_data.columns.index(column_name)
                        return log_data.rows[0][col_idx]
                    except (ValueError, IndexError):
                        pass
            return default
        
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': ['CHANNEL_NAME', 'replica_worker_thread_id', 'number_of_trx_executed', 'percent_of_trx_executed_by_worker'],
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
            'analysis': None,
        }
        
        if subsection.note:
            result['note'] = subsection.note
        
        replication_role = get_value('replication_role', 'replication_role', 'STANDALONE')
        if replication_role != 'REPLICA':
            result['skipped'] = 'Not a replica instance'
            return result
        
        parallel_workers = 0
        parallel_type = 'N/A'
        dep_tracking = 'N/A'
        
        if 'replication_config' in parsed_logs:
            log_data = parsed_logs['replication_config']
            if log_data.success and log_data.rows and log_data.columns:
                try:
                    # Check replica_parallel_workers first (MySQL 8.0.26+), fallback to slave_parallel_workers
                    if 'replica_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    if parallel_workers == 0 and 'slave_parallel_workers' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_workers')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_workers = int(val)
                    
                    # Check replica_parallel_type first, fallback to slave_parallel_type
                    if 'replica_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('replica_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    if parallel_type == 'N/A' and 'slave_parallel_type' in log_data.columns:
                        idx = log_data.columns.index('slave_parallel_type')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            parallel_type = str(val)
                    
                    if 'binlog_transaction_dependency_tracking' in log_data.columns:
                        idx = log_data.columns.index('binlog_transaction_dependency_tracking')
                        val = log_data.rows[0][idx]
                        if val is not None:
                            dep_tracking = str(val)
                except (ValueError, IndexError, TypeError):
                    pass
        
        if parallel_workers <= 1:
            result['skipped'] = 'Parallel workers not enabled (<=1)'
            return result
        
        source_id = subsection.source
        if source_id not in parsed_logs:
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        if not log_data.rows or not log_data.columns:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        try:
            channel_idx = log_data.columns.index('CHANNEL_NAME')
            thread_idx = log_data.columns.index('replica_worker_thread_id')
            trx_idx = log_data.columns.index('number_of_trx_executed')
            pct_idx = log_data.columns.index('percent_of_trx_executed_by_worker')
            
            worker_stats = []
            for row in log_data.rows:
                channel = row[channel_idx] if row[channel_idx] else ''
                result['rows'].append([channel, row[thread_idx], row[trx_idx], row[pct_idx]])
                worker_stats.append({'channel': channel or '(default)', 'trx_count': int(row[trx_idx]) if row[trx_idx] else 0, 'pct': float(row[pct_idx]) if row[pct_idx] else 0.0})
            
            result['total_count'] = len(result['rows'])
            
            # Add analysis
            if worker_stats:
                result['analysis'] = self._generate_mts_interpretation_json(worker_stats, parallel_workers, parallel_type, dep_tracking)
        except (ValueError, IndexError) as e:
            result['error'] = str(e)
        
        return result
    
    def _generate_mts_interpretation_json(self, worker_stats: list, parallel_workers: int, parallel_type: str, dep_tracking: str) -> Dict[str, Any]:
        """Generate JSON interpretation of MTS worker efficiency."""
        channels = {}
        for stat in worker_stats:
            channel = stat['channel']
            if channel not in channels:
                channels[channel] = []
            channels[channel].append(stat)
        
        analysis = {'channels': []}
        
        for channel, stats in channels.items():
            total_trx = sum(s['trx_count'] for s in stats)
            channel_analysis = {
                'channel_name': channel,
                'total_transactions': total_trx,
                'worker_count': len(stats),
            }
            
            if total_trx == 0:
                channel_analysis['status'] = 'no_transactions'
                channel_analysis['message'] = 'No transactions executed yet'
            else:
                percentages = [s['pct'] for s in stats]
                max_pct, min_pct = max(percentages), min(percentages)
                avg_pct = sum(percentages) / len(percentages)
                spread = max_pct - min_pct
                active_workers = sum(1 for p in percentages if p > 5.0)
                
                channel_analysis['active_workers'] = active_workers
                channel_analysis['distribution'] = {
                    'spread': round(spread, 1),
                    'min_pct': round(min_pct, 1),
                    'max_pct': round(max_pct, 1),
                    'avg_pct': round(avg_pct, 1),
                }
                
                channel_analysis['configuration'] = {
                    'replica_parallel_type': {'value': parallel_type, 'optimal': parallel_type == 'LOGICAL_CLOCK'},
                    'replica_parallel_workers': {'value': parallel_workers, 'optimal': True},
                }
                if dep_tracking != 'N/A' and self._database_version < (8, 1, 0):
                    channel_analysis['configuration']['binlog_transaction_dependency_tracking'] = {
                        'value': dep_tracking,
                        'optimal': dep_tracking in ('WRITESET', 'WRITESET_SESSION'),
                    }
                
                if spread < 20 and active_workers >= len(stats) * 0.8:
                    channel_analysis['status'] = 'good'
                    channel_analysis['message'] = 'Transactions well-distributed across worker threads'
                elif spread > 50 or active_workers < len(stats) * 0.5:
                    channel_analysis['status'] = 'warning'
                    channel_analysis['message'] = 'Uneven distribution - workload concentrated on fewer workers'
                    channel_analysis['recommendations'] = []
                    if parallel_type != 'LOGICAL_CLOCK':
                        channel_analysis['recommendations'].append({'action': 'Set replica_parallel_type to LOGICAL_CLOCK', 'command': "SET GLOBAL replica_parallel_type = 'LOGICAL_CLOCK';"})
                    if dep_tracking != 'N/A' and self._database_version < (8, 1, 0) and dep_tracking not in ('WRITESET', 'WRITESET_SESSION'):
                        channel_analysis['recommendations'].append({'action': 'Enable WRITESET on SOURCE server', 'command': "SET GLOBAL binlog_transaction_dependency_tracking = 'WRITESET';"})
                    channel_analysis['recommendations'].append({'action': 'Review workload for hot spots or sequential dependencies'})
                    channel_analysis['recommendations'].append({'action': 'Monitor after changes and re-run collector'})
                else:
                    channel_analysis['status'] = 'moderate'
                    channel_analysis['message'] = 'Distribution acceptable but could be improved'
            
            analysis['channels'].append(channel_analysis)
        
        return analysis
    
    def _generate_table_cache_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """
        Generate JSON for the Table Cache Statistics subsection.
        
        This method transforms the raw table_cache_stats and table_cache_config
        query data into rows with Metric, Value, Explanation format.
        """
        # Define the columns for display
        columns = ["Metric", "Value", "Explanation"]
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'columns': columns,
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        # Transform the data using the module-level function
        transformed_rows = evaluate_table_cache_stats(parsed_logs)
        
        if not transformed_rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Convert tuples to lists for JSON
        result['rows'] = [list(row) for row in transformed_rows]
        result['total_count'] = len(transformed_rows)
        
        return result
    
    def _generate_temp_table_usage_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Temporary Table Usage subsection with metric data."""
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': subsection.source,
            'metrics': None,
            'error': None,
        }
        
        # Transform the data using the module-level function
        temp_data = evaluate_temp_table_usage(parsed_logs)
        
        if not temp_data:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Store as metrics object
        result['metrics'] = {
            'created_tmp_tables': temp_data['created_tmp_tables'],
            'created_tmp_disk_tables': temp_data['created_tmp_disk_tables'],
            'disk_tmp_table_pct': temp_data['disk_tmp_table_pct'],
        }
        
        return result
    
    def _generate_connection_memory_overhead_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Connection Memory Overhead subsection with metric data."""
        source_id = subsection.source
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'metrics': None,
            'error': None,
        }
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Store as metrics object
        result['metrics'] = {
            'current_connections': row[col_map.get('Current Connections', 0)] if 'Current Connections' in col_map else None,
            'max_connections': row[col_map.get('Max Connections', 1)] if 'Max Connections' in col_map else None,
            'avg_memory_per_connection': row[col_map.get('Avg Memory per Connection', 2)] if 'Avg Memory per Connection' in col_map else None,
            'total_connection_memory': row[col_map.get('Total Connection Memory', 3)] if 'Total Connection Memory' in col_map else None,
        }
        
        return result
    
    def _generate_sort_join_buffer_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Sort & Join Buffer Memory subsection with metric data."""
        source_id = subsection.source
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'metrics': None,
            'error': None,
        }
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Store as metrics object
        result['metrics'] = {
            'sort_buffer_size': row[col_map.get('sort_buffer_size', 0)] if 'sort_buffer_size' in col_map else None,
            'join_buffer_size': row[col_map.get('join_buffer_size', 1)] if 'join_buffer_size' in col_map else None,
            'read_buffer_size': row[col_map.get('read_buffer_size', 2)] if 'read_buffer_size' in col_map else None,
            'read_rnd_buffer_size': row[col_map.get('read_rnd_buffer_size', 3)] if 'read_rnd_buffer_size' in col_map else None,
            'sort_merge_passes': row[col_map.get('Sort Merge Passes', 4)] if 'Sort Merge Passes' in col_map else None,
            'sort_range': row[col_map.get('Sort Range', 5)] if 'Sort Range' in col_map else None,
            'sort_scan': row[col_map.get('Sort Scan', 6)] if 'Sort Scan' in col_map else None,
        }
        
        return result
    
    def _generate_innodb_buffer_pool_stats_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for InnoDB Buffer Pool Statistics (Detailed) subsection with metric data."""
        source_id = subsection.source
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'metrics': None,
            'error': None,
        }
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column index map
        col_map = {col: idx for idx, col in enumerate(log_data.columns)}
        row = log_data.rows[0]
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Store as metrics object
        result['metrics'] = {
            'buffer_pool_size_gb': row[col_map.get('Buffer Pool Size (GB)', 0)] if 'Buffer Pool Size (GB)' in col_map else None,
            'buffer_pool_instances': row[col_map.get('Buffer Pool Instances', 1)] if 'Buffer Pool Instances' in col_map else None,
            'total_pages': row[col_map.get('Total Pages', 2)] if 'Total Pages' in col_map else None,
            'data_pages': row[col_map.get('Data Pages', 3)] if 'Data Pages' in col_map else None,
            'dirty_pages': row[col_map.get('Dirty Pages', 4)] if 'Dirty Pages' in col_map else None,
            'free_pages': row[col_map.get('Free Pages', 5)] if 'Free Pages' in col_map else None,
            'buffer_pool_hit_rate': row[col_map.get('Buffer Pool Hit Rate', 6)] if 'Buffer Pool Hit Rate' in col_map else None,
            'dirty_page_pct': row[col_map.get('Dirty Page %', 7)] if 'Dirty Page %' in col_map else None,
        }
        
        # Add buffer pool per-instance data if available
        if 'buffer_pool_stats' in parsed_logs:
            bp_data = parsed_logs['buffer_pool_stats']
            if bp_data.success and bp_data.rows and bp_data.columns:
                bp_col_map = {col: idx for idx, col in enumerate(bp_data.columns)}
                per_instance_data = []
                for bp_row in bp_data.rows:
                    instance = {
                        'pool_id': bp_row[bp_col_map.get('POOL_ID', 0)] if 'POOL_ID' in bp_col_map else None,
                        'pool_size': bp_row[bp_col_map.get('POOL_SIZE', 1)] if 'POOL_SIZE' in bp_col_map else None,
                        'free_buffers': bp_row[bp_col_map.get('FREE_BUFFERS', 2)] if 'FREE_BUFFERS' in bp_col_map else None,
                        'database_pages': bp_row[bp_col_map.get('DATABASE_PAGES', 3)] if 'DATABASE_PAGES' in bp_col_map else None,
                        'old_database_pages': bp_row[bp_col_map.get('OLD_DATABASE_PAGES', 4)] if 'OLD_DATABASE_PAGES' in bp_col_map else None,
                        'modified_database_pages': bp_row[bp_col_map.get('MODIFIED_DATABASE_PAGES', 5)] if 'MODIFIED_DATABASE_PAGES' in bp_col_map else None,
                    }
                    per_instance_data.append(instance)
                result['per_instance_stats'] = per_instance_data
        
        return result
    
    def _generate_per_connection_memory_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Per-Connection Memory Buffers subsection with table data and estimate."""
        source_id = subsection.source
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'error': None,
            'estimate': None,
        }
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return result
        
        # Set column names
        result['columns'] = [col[0] for col in display_columns]
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Process rows
        for row in log_data.rows:
            row_len = len(row) if row else 0
            row_dict = {col: row[idx] for col, idx in column_map.items() if idx < row_len}
            row_data = []
            for display_name, source in display_columns:
                value = self._get_cell_value(source, row, column_map, row_dict)
                row_data.append(value)
            result['rows'].append(row_data)
        
        # Get per_connection_memory_estimate data for the estimate
        if 'per_connection_memory_estimate' in parsed_logs:
            estimate_data = parsed_logs['per_connection_memory_estimate']
            if estimate_data.success and estimate_data.rows and estimate_data.columns:
                est_col_map = {col: idx for idx, col in enumerate(estimate_data.columns)}
                est_row = estimate_data.rows[0]
                
                result['estimate'] = {
                    'per_connection_mb': est_row[est_col_map.get('Per Connection (MB)', 0)] if 'Per Connection (MB)' in est_col_map else None,
                    'max_connections': est_row[est_col_map.get('Max Connections', 1)] if 'Max Connections' in est_col_map else None,
                    'estimated_max_memory_gb': est_row[est_col_map.get('Estimated Max Connections Memory', 2)] if 'Estimated Max Connections Memory' in est_col_map else None,
                }
        
        return result
    
    def _generate_global_memory_usage_top_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Global Memory Usage - Top Components subsection with table data and total."""
        source_id = subsection.source
        
        # Base subsection structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'error': None,
            'total': None,
            'truncated': False,
            'total_row_count': 0,
        }
        
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        log_data = parsed_logs[source_id]
        if not log_data.success or not log_data.rows or not log_data.columns:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Determine which columns to display
        display_columns = self._resolve_columns(subsection.columns, column_map)
        
        if not display_columns:
            return result
        
        # Apply collection limit to displayed rows
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Set column names
        result['columns'] = [col[0] for col in display_columns]
        
        # Add truncation info
        result['truncated'] = was_truncated
        result['total_row_count'] = total_count
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Process limited rows
        for row in limited_rows:
            row_len = len(row) if row else 0
            row_dict = {col: row[idx] for col, idx in column_map.items() if idx < row_len}
            row_data = []
            for display_name, source in display_columns:
                value = self._get_cell_value(source, row, column_map, row_dict)
                row_data.append(value)
            result['rows'].append(row_data)
        
        # Get global_memory_usage_total data for the total
        # This uses ALL rows for the total calculation, not just the displayed rows
        if 'global_memory_usage_total' in parsed_logs:
            total_data = parsed_logs['global_memory_usage_total']
            if total_data.success and total_data.rows and total_data.columns:
                total_col_map = {col: idx for idx, col in enumerate(total_data.columns)}
                total_row = total_data.rows[0]
                
                result['total'] = {
                    'total_allocated': total_row[total_col_map.get('Total Allocated', 0)] if 'Total Allocated' in total_col_map else None,
                    'high_watermark': total_row[total_col_map.get('High Watermark', 1)] if 'High Watermark' in total_col_map else None,
                }
        
        return result
    
    def _generate_memory_troubleshooting_tips_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> Dict[str, Any]:
        """Generate JSON for Memory Troubleshooting Tips subsection."""
        tips = [
            {"title": "High Memory Usage", "description": "Check section 4.3 (Global Memory Usage) to identify which components are consuming memory"},
            {"title": "Memory Spikes", "description": 'Look at "High Watermark" columns - they show peak memory usage even if current usage is low'},
            {"title": "Per-Connection Issues", "description": "Check section 4.4 (Memory by Thread) to find memory-hungry connections"},
            {"title": "Application Issues", "description": "Use section 4.5 (Memory by User) to identify which applications/users consume most memory"},
            {"title": "Prepared Statement Memory", "description": "Check section 4.7 for prepared statement memory usage - common cause of memory issues"},
            {"title": "Temporary Table Issues", "description": "Section 4.9 shows if queries are creating too many disk temp tables"},
            {"title": "Reset Statistics", "description": "To reset high watermarks, run: TRUNCATE TABLE performance_schema.memory_summary_global_by_event_name;"}
        ]
        
        return {
            'name': subsection.name,
            'source': subsection.source,
            'display_type': 'tips',
            'tips': tips,
        }
    
    def _generate_aurora_innodb_note_subsection(
        self,
        subsection: 'ReportSubsection'
    ) -> Dict[str, Any]:
        """Generate JSON for Aurora InnoDB Note subsection (Aurora MySQL only)."""
        notes = [
            "Aurora MySQL uses a distributed storage layer that automatically manages I/O, flushing, and redo log sizing. Many traditional InnoDB tuning parameters are not applicable or modifiable in Aurora.",
            "Redo log sizing and flushing are managed automatically by Aurora's distributed storage layer. These configuration parameters do not apply to Aurora.",
            "Redo log management is handled automatically by Aurora's distributed storage layer. Redo log occupancy monitoring and sizing are not applicable to Aurora instances."
        ]
        
        return {
            'name': subsection.name,
            'source': subsection.source,
            'display_type': 'note',
            'notes': notes,
        }
    
    def _generate_innodb_purge_analysis_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for InnoDB Purge Analysis composite subsection."""
        
        # Get history list length for health status
        history_list_length = 0
        if 'innodb_history_length' in parsed_logs:
            hll_data = parsed_logs['innodb_history_length']
            if hll_data.success and hll_data.rows and hll_data.columns:
                try:
                    history_list_length = int(hll_data.rows[0][0])
                except (ValueError, TypeError, IndexError):
                    pass
        
        # Check for long-running transactions (older than 5 minutes = 300 seconds)
        has_long_running_trx = False
        if 'long_running_idle_trx' in parsed_logs:
            trx_data = parsed_logs['long_running_idle_trx']
            if trx_data.success and trx_data.rows:
                has_long_running_trx = True
        if not has_long_running_trx and 'innodb_transactions' in parsed_logs:
            trx_data = parsed_logs['innodb_transactions']
            if trx_data.success and trx_data.rows and trx_data.columns:
                try:
                    duration_idx = trx_data.columns.index('duration_seconds')
                    for row in trx_data.rows:
                        if row[duration_idx] and int(row[duration_idx]) > 300:
                            has_long_running_trx = True
                            break
                except (ValueError, IndexError):
                    pass
        
        # Build Purge Thread Activity data
        purge_thread_activity = None
        if 'purge_thread_activity' in parsed_logs:
            log_data = parsed_logs['purge_thread_activity']
            if log_data.success and log_data.rows and log_data.columns:
                purge_thread_activity = {
                    'columns': ['Thread ID', 'Thread Name', 'Thread Type', 'State'],
                    'rows': [list(row) for row in log_data.rows]
                }
        
        # Build Purge Configuration data
        purge_configuration = None
        if 'purge_configuration' in parsed_logs:
            log_data = parsed_logs['purge_configuration']
            if log_data.success and log_data.rows and log_data.columns:
                purge_configuration = {
                    'columns': ['Parameter', 'Value', 'Recommendation'],
                    'rows': [list(row) for row in log_data.rows]
                }
        
        # Build Purge Performance Metrics data
        purge_metrics = None
        if 'purge_performance_metrics' in parsed_logs:
            log_data = parsed_logs['purge_performance_metrics']
            if log_data.success and log_data.rows and log_data.columns:
                row = log_data.rows[0]
                purge_metrics = {
                    'purge_threads': row[0] if len(row) > 0 else None,
                    'undo_tablespaces': row[1] if len(row) > 1 else None,
                    'active_undo_tablespaces': row[2] if len(row) > 2 else None
                }
        
        # Determine health status
        if history_list_length < 50000:
            health_status = 'OK'
            health_message = f"Purge operating normally. History list length ({history_list_length:,}) is healthy."
        elif history_list_length < 100000:
            health_status = 'WARNING'
            health_message = f"History list length ({history_list_length:,}) is elevated. Monitor purge performance and check for long-running transactions."
        else:
            health_status = 'CRITICAL'
            if has_long_running_trx:
                health_message = f"History list length ({history_list_length:,}) is very high. Long-running transaction(s) detected that may be blocking purge. See 'Active Transactions' section."
            else:
                health_message = f"History list length ({history_list_length:,}) is very high. Purge may not be keeping up with workload."
        
        recommendations = [
            "Monitor History List Length: Set CloudWatch alarms for values >10K (warning) and >50K (critical)",
            "Avoid Long-Running Transactions: Keep transactions short; long transactions block purge for all subsequent transactions",
            "Optimize Workload: Use DROP PARTITION or DROP TABLE instead of DELETE for bulk deletion use cases (age-partitioned)",
            "Secondary Indexes: Purge is slower with many secondary indexes. Review unused indexes with sys.schema_unused_indexes",
            "Table Sharding: Spread writes across multiple tables to increase number of purge threads for better parallelism",
            "Instance Sizing: Ensure adequate CPU, memory (buffer pool), and I/O capacity for purge threads",
            "Aurora-Specific: In Aurora, purge performance scales to Serverless v2 scales on replicas. Consider aurora_read_replica_read_committed=ON"
        ]
        
        return {
            'name': subsection.name,
            'source': subsection.source,
            'display_type': 'composite',
            'description': 'Detailed analysis of InnoDB purge operations. Purge removes old row versions (undo logs) created by MVCC. Slow purge causes History List Length growth, causing performance degradation.',
            'understanding_purge': {
                'description': 'InnoDB purge is a background process that removes old row versions (undo logs) no longer needed for MVCC or rollback.',
                'consequences': [
                    'Increased disk space (undo logs)',
                    'Slower queries (more versions to skip)',
                    'Higher I/O consumption',
                    'Potential disk log pressure'
                ],
                'reference_url': 'https://aws.amazon.com/blogs/database/achieve-a-high-speed-innodb-purge-on-amazon-rds-for-mysql-and-amazon-aurora-mysql/'
            },
            'purge_thread_activity': purge_thread_activity,
            'purge_configuration': purge_configuration,
            'purge_metrics': purge_metrics,
            'recommendations': recommendations,
            'health_status': {
                'status': health_status,
                'message': health_message,
                'history_list_length': history_list_length,
                'has_long_running_transactions': has_long_running_trx
            }
        }
    
    def _generate_innodb_engine_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for InnoDB Engine Status (Detailed Diagnostics) composite subsection."""
        
        # Get parsed InnoDB status data
        status_data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        if not status_data:
            return {
                'name': subsection.name,
                'source': subsection.source,
                'display_type': 'composite',
                'error': 'InnoDB engine status not available'
            }
        
        # Build deadlock data
        deadlock_data = None
        if status_data.has_deadlock:
            deadlock_data = {
                'timestamp': status_data.deadlock_timestamp,
                'transactions': status_data.deadlock_transactions,
                'victim': status_data.deadlock_victim,
                'notes': [
                    'This shows only the most recent deadlock. To log all deadlocks to the error log, enable innodb_print_all_deadlocks=ON.',
                    'The innodb_deadlock_detect variable controls whether InnoDB performs deadlock detection. When enabled (default), InnoDB automatically detects deadlocks and rolls back one transaction. Disabling it requires relying on innodb_lock_wait_timeout to handle deadlocks.'
                ],
                'best_practices': [
                    'Access tables and rows in a consistent order across all transactions',
                    'Keep transactions short',
                    'Add proper indexes to reduce lock scope',
                    'Consider using SELECT ... FOR UPDATE NOWAIT or SKIP LOCKED for lock-free patterns'
                ]
            }
        
        # Build FK error data
        fk_error_data = None
        if status_data.has_fk_error:
            fk_error_data = {
                'timestamp': status_data.fk_error_timestamp,
                'constraint': status_data.fk_error_constraint,
                'table': status_data.fk_error_table,
                'statement': status_data.fk_error_statement,
                'best_practices': [
                    'Ensure parent rows exist before inserting child rows',
                    'Use ON DELETE CASCADE or ON UPDATE CASCADE carefully',
                    'Verify data integrity before enabling FK constraints on existing data',
                    'Avoid performing operations with foreign_key_checks disabled as this can compromise data integrity and lead to foreign key errors'
                ]
            }
        
        # Build semaphore data
        wait_ratio = None
        if status_data.os_waits_reservation_count > 0:
            wait_ratio = (status_data.os_waits_signal_count / status_data.os_waits_reservation_count) * 100
        
        semaphore_data = {
            'os_waits_reservation_count': status_data.os_waits_reservation_count,
            'os_waits_signal_count': status_data.os_waits_signal_count,
            'rw_shared': {
                'spins': status_data.rw_shared_spins,
                'rounds': status_data.rw_shared_rounds,
                'os_waits': status_data.rw_shared_os_waits
            },
            'rw_excl': {
                'spins': status_data.rw_excl_spins,
                'rounds': status_data.rw_excl_rounds,
                'os_waits': status_data.rw_excl_os_waits
            },
            'rw_sx': {
                'spins': status_data.rw_sx_spins,
                'rounds': status_data.rw_sx_rounds,
                'os_waits': status_data.rw_sx_os_waits
            },
            'spin_rounds_per_wait': {
                'shared': status_data.spin_rounds_per_wait_shared,
                'excl': status_data.spin_rounds_per_wait_excl,
                'sx': status_data.spin_rounds_per_wait_sx
            },
            'signal_reservation_ratio': wait_ratio,
            'current_waits': status_data.current_waits[:5] if status_data.current_waits else [],
            'note': 'High spin rounds per wait values are influenced by innodb_spin_wait_delay, innodb_spin_wait_pause_multiplier, and innodb_sync_spin_loops. The presence of threads in the current wait list indicates active contention.'
        }
        
        # Build transaction & purge status data
        purge_status = "Unknown"
        purge_health = "unknown"
        if status_data.purge_state:
            if "running but idle" in status_data.purge_state.lower():
                purge_status = "Purge is current (running but idle)"
                purge_health = "ok"
            elif "running" in status_data.purge_state.lower():
                purge_status = "Purge is actively running"
                purge_health = "warning"
            else:
                purge_status = status_data.purge_state
        
        if status_data.purge_lag > 0:
            purge_status += f" - {status_data.purge_lag:,} transactions behind"
            if status_data.purge_lag > 1000:
                purge_health = "warning"
            if status_data.purge_lag > 10000:
                purge_health = "critical"
        
        trx_purge_data = {
            'trx_id_counter': status_data.trx_id_counter,
            'purge_done_for_trx': status_data.purge_done_for_trx,
            'purge_lag': status_data.purge_lag,
            'history_list_length': status_data.history_list_length,
            'purge_state': status_data.purge_state,
            'purge_status_message': purge_status,
            'purge_health': purge_health
        }
        
        # Build redo log checkpoint data
        redo_log_data = {
            'log_sequence_number': status_data.log_sequence_number,
            'log_flushed_up_to': status_data.log_flushed_up_to,
            'pages_flushed_up_to': status_data.pages_flushed_up_to,
            'last_checkpoint_at': status_data.last_checkpoint_at,
            'checkpoint_lag': status_data.checkpoint_lag,
            'unflushed_log': status_data.unflushed_log,
            'pending_log_flushes': status_data.pending_log_flushes,
            'pending_chkp_writes': status_data.pending_chkp_writes,
            'log_ios_done': status_data.log_ios_done,
            'log_ios_per_second': status_data.log_ios_per_second,
            'recommendations': [
                'Checkpoint lag indicates how much redo log data hasn\'t been checkpointed yet. High checkpoint lag under heavy write workloads may indicate redo log is too small or I/O is a bottleneck.',
                'Consider increasing innodb_log_file_size if checkpoint lag is consistently high.'
            ] if status_data.checkpoint_lag > 0 or status_data.unflushed_log > 0 else []
        }
        
        # Build buffer pool efficiency data
        hit_rate_pct = (status_data.buffer_pool_hit_rate / status_data.buffer_pool_hit_rate_denominator * 100) if status_data.buffer_pool_hit_rate_denominator > 0 else 0
        
        buffer_pool_data = {
            'buffer_pool_hit_rate': status_data.buffer_pool_hit_rate,
            'buffer_pool_hit_rate_denominator': status_data.buffer_pool_hit_rate_denominator,
            'buffer_pool_hit_rate_pct': hit_rate_pct,
            'pages_made_young': status_data.pages_made_young,
            'pages_made_not_young': status_data.pages_made_not_young,
            'youngs_per_sec': status_data.youngs_per_sec,
            'non_youngs_per_sec': status_data.non_youngs_per_sec,
            'read_ahead_per_sec': status_data.read_ahead_per_sec,
            'evicted_without_access_per_sec': status_data.evicted_without_access_per_sec,
            'lru_len': status_data.lru_len,
            'recommendations': [
                'Buffer pool hit rate below 99.9% may indicate buffer pool is undersized for the working set.',
                'High "evicted without access" rate suggests pages are being evicted before being read, indicating memory pressure.',
                'Review queries causing full table scans that pollute the buffer pool.',
                'Check if innodb_buffer_pool_size, innodb_old_blocks_pct and innodb_old_blocks_time are tuned appropriately.'
            ] if hit_rate_pct < 99.9 or status_data.evicted_without_access_per_sec > 0 else []
        }
        
        # Build adaptive hash index data
        total_searches = status_data.hash_searches_per_sec + status_data.non_hash_searches_per_sec
        hash_ratio = (status_data.hash_searches_per_sec / total_searches * 100) if total_searches > 0 else 0
        
        adaptive_hash_data = {
            'hash_searches_per_sec': status_data.hash_searches_per_sec,
            'non_hash_searches_per_sec': status_data.non_hash_searches_per_sec,
            'hash_search_ratio': hash_ratio,
            'hash_table_size': status_data.hash_table_size,
            'about': [
                'AHI builds in-memory hash indexes for frequently accessed index pages.',
                'Low hash search ratio may indicate AHI is not benefiting your workload.'
            ],
            'recommendations': [
                'If hash searches are consistently near zero, consider disabling AHI with innodb_adaptive_hash_index=OFF to reduce memory overhead.',
                'AHI works best with point lookups; range scans don\'t benefit from it.',
                'Monitor innodb_adaptive_hash_index_parts for partitioning to reduce contention.'
            ] if total_searches > 0 else []
        }
        
        # Build row operations data
        row_operations_data = {
            'queries_inside_innodb': status_data.queries_inside_innodb,
            'queries_in_queue': status_data.queries_in_queue,
            'inserts_per_sec': status_data.inserts_per_sec,
            'updates_per_sec': status_data.updates_per_sec,
            'deletes_per_sec': status_data.deletes_per_sec,
            'reads_per_sec': status_data.reads_per_sec_rows,
            'main_thread_state': status_data.main_thread_state
        }
        
        # Build optimization tips
        optimization_tips = [
            {'title': 'Deadlock Victim', 'description': 'The transaction InnoDB chose to roll back to break the deadlock cycle. InnoDB selects the transaction that modified fewer rows as the victim to minimize rollback cost.'},
            {'title': 'Purge Lag', 'description': 'The number of transactions whose old row versions haven\'t been cleaned up yet. Growing purge lag indicates long-running transactions are blocking purge, causing undo log growth and table bloat.'},
            {'title': 'History List Length', 'description': 'The number of unpurged transactions in the undo log. Values consistently above 10,000 indicate purge is falling behind, leading to increased disk usage and slower queries due to MVCC overhead.'},
            {'title': 'Checkpoint Lag', 'description': 'The difference between the current Log Sequence Number (LSN) and the last checkpoint. Large checkpoint lag means more redo log data needs to be replayed during crash recovery, increasing recovery time.'},
            {'title': 'Unflushed Log', 'description': 'The amount of data (in bytes) written to the InnoDB Log Buffer but not yet flushed to redo log files on disk. If this approaches innodb_log_buffer_size, it may indicate I/O bottleneck or undersized log buffer.'},
            {'title': 'Buffer Pool Hit Rate', 'description': 'The percentage of page requests served from the buffer pool cache vs disk reads. Values below 99.9% suggest the buffer pool may be undersized for your working set, causing excessive disk I/O.'},
            {'title': 'Pages Evicted Without Access', 'description': 'Pages that were loaded into the buffer pool but evicted before being read. High values indicate memory pressure or queries causing unnecessary page loads (e.g., full table scans).'},
            {'title': 'Pages Made Young/Not Young', 'description': 'Tracks how pages move between the \'old\' and \'young\' sublists in the buffer pool LRU. High \'not young\' values may indicate innodb_old_blocks_time is filtering out scan-loaded pages effectively.'},
            {'title': 'Adaptive Hash Index (AHI) Efficiency', 'description': 'The ratio of hash searches to non-hash searches. Low ratio means AHI isn\'t benefiting your workload; consider disabling it to free memory and reduce contention.'},
            {'title': 'Spin Rounds Per Wait', 'description': 'The number of spin loop iterations before a thread gives up and performs an OS wait. Values are influenced by innodb_spin_wait_delay and innodb_sync_spin_loops. High values with many OS waits indicate contention.'},
            {'title': 'OS Waits vs Reservation Count', 'description': 'The ratio of expensive OS-level waits to total lock attempts. Lower ratio is better, meaning more locks were acquired via cheap spinning rather than costly context switches.'},
            {'title': 'Queries in Queue', 'description': 'The number of queries waiting to enter InnoDB. Values consistently above 0 indicate thread contention; consider tuning innodb_thread_concurrency.'},
            {'title': 'Main Thread State', 'description': 'The current state of the InnoDB master background thread (sleeping, purging, flushing). Persistent non-sleeping states may indicate heavy background activity.'}
        ]
        
        return {
            'name': subsection.name,
            'source': subsection.source,
            'display_type': 'composite',
            'description': 'Detailed diagnostics from SHOW ENGINE INNODB STATUS. This provides internal InnoDB metrics for advanced troubleshooting.',
            'latest_deadlock': deadlock_data,
            'latest_fk_error': fk_error_data,
            'semaphore_contention': semaphore_data,
            'transaction_purge_status': trx_purge_data,
            'redo_log_checkpoint': redo_log_data,
            'buffer_pool_efficiency': buffer_pool_data,
            'adaptive_hash_index': adaptive_hash_data,
            'row_operations': row_operations_data,
            'optimization_tips': optimization_tips
        }
    
    def _generate_long_running_transactions_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Long-Running Transactions composite subsection."""
        
        # Build Active Transactions data
        active_transactions = None
        if 'innodb_transactions' in parsed_logs:
            log_data = parsed_logs['innodb_transactions']
            if log_data.success and log_data.rows and log_data.columns:
                columns = ['trx_id', 'trx_state', 'trx_started', 'duration_seconds', 'trx_mysql_thread_id', 'trx_query', 'trx_rows_locked', 'trx_rows_modified']
                display_columns = [col for col in columns if col in log_data.columns]
                column_map = {col: idx for idx, col in enumerate(log_data.columns)}
                
                rows = []
                for row in log_data.rows[:20]:
                    row_data = []
                    for col in display_columns:
                        idx = column_map.get(col)
                        val = row[idx] if idx is not None and idx < len(row) else None
                        row_data.append(val)
                    rows.append(row_data)
                
                active_transactions = {
                    'columns': display_columns,
                    'rows': rows
                }
        
        # Build Long-Running Idle Transactions data
        idle_transactions = None
        if 'long_running_idle_trx' in parsed_logs:
            log_data = parsed_logs['long_running_idle_trx']
            if log_data.success and log_data.rows and log_data.columns:
                columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'trx_rows_modified']
                display_columns = [col for col in columns if col in log_data.columns]
                column_map = {col: idx for idx, col in enumerate(log_data.columns)}
                
                rows = []
                for row in log_data.rows[:20]:
                    row_data = []
                    for col in display_columns:
                        idx = column_map.get(col)
                        val = row[idx] if idx is not None and idx < len(row) else None
                        row_data.append(val)
                    rows.append(row_data)
                
                idle_transactions = {
                    'columns': display_columns,
                    'rows': rows,
                    'note': 'Transactions that started but have no current query running. May indicate application issues or forgotten transactions.'
                }
        
        # Build Long-Running Active Transactions data
        active_long_transactions = None
        if 'long_running_active_trx' in parsed_logs:
            log_data = parsed_logs['long_running_active_trx']
            if log_data.success and log_data.rows and log_data.columns:
                columns = ['trx_id', 'trx_state', 'trx_started', 'age_seconds', 'trx_mysql_thread_id', 'trx_tables_locked', 'trx_rows_locked', 'current_query']
                display_columns = [col for col in columns if col in log_data.columns]
                column_map = {col: idx for idx, col in enumerate(log_data.columns)}
                
                rows = []
                for row in log_data.rows[:20]:
                    row_data = []
                    for col in display_columns:
                        idx = column_map.get(col)
                        val = row[idx] if idx is not None and idx < len(row) else None
                        row_data.append(val)
                    rows.append(row_data)
                
                active_long_transactions = {
                    'columns': display_columns,
                    'rows': rows,
                    'note': 'Transactions with queries currently executing. May indicate slow queries or large batch operations.'
                }
        
        return {
            'name': subsection.name,
            'source': subsection.source,
            'display_type': 'composite',
            'note': subsection.note,
            'active_transactions': active_transactions,
            'long_running_idle_transactions': idle_transactions,
            'long_running_active_transactions': active_long_transactions
        }
    
    def _generate_long_running_trx_activity_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Long-Running Transactions Activity subsection."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'note': subsection.note,
            'columns': [],
            'rows': [],
            'total_count': 0,
            'error': None,
            'interpretation': None,
        }
        
        # Get the data
        if source_id not in parsed_logs:
            result['default_note'] = subsection.default_note if subsection.default_note else 'No data available'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            result['default_note'] = subsection.default_note if subsection.default_note else 'No data returned'
            return result
        
        # Build the data
        columns = ['mysql_pid', 'trx_duration', 'trx_state', 'sql_text', 'stmt_execution_time', 'rows_sent', 'rows_modified', 'rows_examined', 'tmp_disk_tables', 'error_info']
        display_columns = [col for col in columns if col in log_data.columns]
        
        if not display_columns:
            display_columns = log_data.columns
        
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        result['columns'] = display_columns
        result['total_count'] = len(log_data.rows)
        
        # Build rows
        for row in log_data.rows:
            row_data = []
            for col in display_columns:
                idx = column_map.get(col)
                val = row[idx] if idx is not None and idx < len(row) else None
                row_data.append(val)
            result['rows'].append(row_data)
        
        # Add interpretation
        result['interpretation'] = {
            'description': 'This section shows the 10 most recent SQL statements executed within each long-running transaction (>5 minutes). Use this to understand what operations the transaction has performed.',
            'key_columns': {
                'mysql_pid': 'Process ID - use to identify the connection',
                'trx_duration': 'Total time the transaction has been active',
                'sql_text': 'The SQL statement that was executed',
                'stmt_execution_time': 'How long this specific statement took',
                'rows_sent': 'Number of rows returned to the client',
                'rows_modified': 'Number of rows affected by this statement',
                'rows_examined': 'Number of rows scanned (high values may indicate missing indexes)',
                'tmp_disk_tables': 'Disk temp tables created (high values indicate inefficient queries)',
                'error_info': 'Shows if the statement encountered errors'
            },
            'what_to_look_for': [
                'Statements with high rows_modified may be causing lock contention',
                'High rows_examined vs rows_sent ratio indicates inefficient queries',
                'High tmp_disk_tables suggests queries need optimization',
                'Multiple statements per PID show the transaction\'s activity history',
                'Use the PID to correlate with application logs or kill the connection if needed'
            ]
        }
        
        return result
    
    def _generate_tables_without_pk_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Tables Without Primary Key subsection with detailed info."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'display_type': 'critical_warning',
            'columns': [],
            'rows': [],
            'total_count': 0,
            'error': None,
            'status': 'ok',
            'critical_info': None,
        }
        
        # Get the data
        if source_id not in parsed_logs:
            result['error'] = 'No data available - query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case - all tables have PKs
        if not log_data.columns or not log_data.rows:
            result['status'] = 'ok'
            result['message'] = subsection.default_note if subsection.default_note else 'No data returned'
            return result
        
        # Tables without PK found - critical status
        table_count = len(log_data.rows)
        result['status'] = 'critical'
        result['total_count'] = table_count
        
        # Set columns
        columns = subsection.columns if subsection.columns else log_data.columns
        result['columns'] = list(columns)
        
        # Set rows
        for row in log_data.rows:
            row_data = []
            for i, col in enumerate(columns):
                if i < len(row):
                    row_data.append(row[i])
                else:
                    row_data.append(None)
            result['rows'].append(row_data)
        
        # Add critical info
        result['critical_info'] = {
            'severity': 'CRITICAL',
            'message': f'Found {table_count} table{"s" if table_count != 1 else ""} without primary keys!',
            'replication_impact': [
                'Full Table Scans on Replicas: Every UPDATE/DELETE on these tables causes a FULL TABLE SCAN on replicas to locate the row.',
                'Row-Based Replication (RBR): With binlog_format=ROW (default in MySQL 8.0+), replicas must scan entire tables to find matching rows.',
                'Semi-Sync Replication: Without PKs, semi-sync replication can timeout waiting for replica acknowledgment.',
                'Replication Lag: Large tables without PKs can cause hours or days of replication lag.',
                'Table Scan Avoidance: MySQL 8.0.18+ has optimizations but they are NOT sufficient - primary keys are still required!'
            ],
            'workaround_options': [
                {'option': 'Option 1 (Best)', 'description': 'Add a proper primary key based on business logic'},
                {'option': 'Option 2 (Temporary)', 'description': 'Add an invisible auto-increment column (MySQL 8.0.23+): ALTER TABLE t ADD COLUMN _pk BIGINT UNSIGNED NOT NULL AUTO_INCREMENT INVISIBLE PRIMARY KEY;'},
                {'option': 'Option 3 (Last Resort)', 'description': 'Use a unique index as a substitute, though not as effective as a true PK'}
            ],
            'prevention': {
                'require_pks': 'Set sql_require_primary_key=ON to prevent creating tables without PKs (MySQL 8.0.13+)',
                'auto_generate_pks': 'Set sql_generate_invisible_primary_key=ON to automatically add invisible PKs (MySQL 8.0.30+)'
            },
            'references': [
                {'title': 'MySQL: Replication and Table Scan Avoidance', 'url': 'https://dev.mysql.com/doc/refman/8.0/en/table-scan-avoidance.html'},
                {'title': 'MySQL: Row-Based Replication Row Searches', 'url': 'https://dev.mysql.com/doc/mysql-replication-excerpt/8.0/en/replication-features-row-searches.html'},
                {'title': 'Jean-François Gagné: Danger of No PK with RBR', 'url': 'https://jfg-mysql.blogspot.com/2017/08/danger-no-pk-with-RBR-and-mariadb-protection.html'},
                {'title': 'Percona: Attack No-PK Replication Lag with Invisible Columns', 'url': 'https://www.percona.com/blog/attack-no-pk-replication-lag-with-mysql-percona-server-8-invisible-columns/'},
                {'title': 'MySQL: Generated Invisible Primary Keys (GIPKs)', 'url': 'https://dev.mysql.com/doc/refman/8.0/en/create-table-gipks.html'},
                {'title': 'MySQL: sql_generate_invisible_primary_key', 'url': 'https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_sql_generate_invisible_primary_key'},
                {'title': 'MySQL: sql_require_primary_key', 'url': 'https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_sql_require_primary_key'},
                {'title': 'Percona: Restrict Tables Without Primary Keys', 'url': 'https://www.percona.com/blog/explore-the-new-feature-of-mysql-to-restrict-users-from-creating-a-table-without-a-primary-key/'},
                {'title': 'Percona: Tuning InnoDB Primary Keys', 'url': 'https://www.percona.com/blog/tuning-innodb-primary-keys/'},
                {'title': 'Percona: Primary Key Models and Disk Usage Impact', 'url': 'https://www.percona.com/blog/illustrating-primary-key-models-in-innodb-and-their-impact-on-disk-usage/'}
            ]
        }
        
        return result
    
    def _generate_unused_indexes_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Unused Indexes subsection with warning note when indexes found."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        if subsection.note:
            result['note'] = subsection.note
        
        # Get the data
        if source_id not in parsed_logs:
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case - no unused indexes
        if not log_data.columns or not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Apply collection limit
        limited_rows, was_truncated, total_count = self._limit_enforcer.apply_limit(
            log_data.rows,
            subsection.name
        )
        
        # Build the result
        columns = subsection.columns if subsection.columns else log_data.columns
        result['columns'] = list(columns)
        result['rows'] = [list(row) for row in limited_rows]
        result['truncated'] = was_truncated
        result['total_count'] = total_count
        
        # Add safe index removal warning
        result['safe_removal_process'] = {
            'warning': 'DO NOT drop indexes immediately! Indexes may be used infrequently (daily/weekly/monthly reports, batch jobs, etc.).',
            'recommended_approach': [
                {
                    'step': 1,
                    'action': 'Make the index INVISIBLE first',
                    'command': 'ALTER TABLE schema.table ALTER INDEX index_name INVISIBLE;'
                },
                {
                    'step': 2,
                    'action': 'Monitor for several weeks/months to ensure no queries break'
                },
                {
                    'step': 3,
                    'action': 'If no issues, drop the index',
                    'command': 'ALTER TABLE schema.table DROP INDEX index_name;'
                },
                {
                    'step': 4,
                    'action': 'If queries break, make visible again',
                    'command': 'ALTER TABLE schema.table ALTER INDEX index_name VISIBLE;'
                }
            ],
            'explanation': 'Invisible indexes are maintained by MySQL but not used by the optimizer. This allows safe testing without the risk of losing the index structure. Available in MySQL 8.0+ and Aurora MySQL 2.x+.'
        }
        
        return result
    
    def _generate_locked_accounts_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Locked Accounts subsection with locked_connects warning."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Get the data
        if source_id not in parsed_logs:
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case - no locked accounts
        if not log_data.columns or not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Set columns
        columns = subsection.columns if subsection.columns else log_data.columns
        result['columns'] = list(columns)
        result['total_count'] = len(log_data.rows)
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Set rows
        for row in log_data.rows:
            row_data = []
            for col in columns:
                if col in column_map and column_map[col] < len(row):
                    row_data.append(row[column_map[col]])
                else:
                    row_data.append(None)
            result['rows'].append(row_data)
        
        # Check locked_connects status and add warning if > 0
        if 'locked_connects_status' in parsed_logs:
            status_data = parsed_logs['locked_connects_status']
            if status_data.success and status_data.rows:
                for row in status_data.rows:
                    if len(row) >= 2 and str(row[0]).lower() == 'locked_connects':
                        try:
                            locked_count = int(row[1])
                            if locked_count > 0:
                                result['warning'] = f"⚠️ The number of attempts to connect to locked user accounts - {locked_count:,}"
                                result['locked_connects_count'] = locked_count
                        except (ValueError, TypeError):
                            pass
                        break
        
        return result
    
    def _generate_ssl_config_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for SSL Configuration subsection with require_secure_transport warning."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'truncated': False,
            'total_count': 0,
            'error': None,
        }
        
        # Get the data
        if source_id not in parsed_logs:
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Set columns
        columns = subsection.columns if subsection.columns else log_data.columns
        result['columns'] = list(columns)
        result['total_count'] = len(log_data.rows)
        
        # Build column mapping from log data
        column_map = {col: idx for idx, col in enumerate(log_data.columns)}
        
        # Check for require_secure_transport value while building rows
        require_secure_transport_off = False
        for row in log_data.rows:
            row_data = []
            for col in columns:
                if col in column_map and column_map[col] < len(row):
                    row_data.append(row[column_map[col]])
                else:
                    row_data.append(None)
            result['rows'].append(row_data)
            
            # Check if this row is require_secure_transport and if it's OFF
            if len(row) >= 2:
                var_name = str(row[0]).lower() if row[0] else ''
                var_value = str(row[1]).upper() if row[1] else ''
                if var_name == 'require_secure_transport' and var_value == 'OFF':
                    require_secure_transport_off = True
        
        # Add warning if require_secure_transport is OFF
        if require_secure_transport_off:
            result['warning'] = "⚠️ Enable require_secure_transport=ON to enforce SSL/TLS for all connections. This prevents unencrypted connections that could expose sensitive data."
            result['require_secure_transport'] = 'OFF'
        
        return result
    
    def _generate_ssl_connection_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for SSL Connection Status subsection with metric data."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'display_type': 'cards',
            'metrics': {},
            'error': None,
        }
        
        # Get the data
        if source_id not in parsed_logs:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case
        if not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Extract values from the first row
        row = log_data.rows[0]
        total_connections = int(row[0]) if len(row) > 0 and row[0] is not None else 0
        ssl_connections = int(row[1]) if len(row) > 1 and row[1] is not None else 0
        non_ssl_connections = int(row[2]) if len(row) > 2 and row[2] is not None else 0
        
        # Set metrics
        result['metrics'] = {
            'total_connections': total_connections,
            'ssl_connections': ssl_connections,
            'non_ssl_connections': non_ssl_connections,
        }
        
        return result
    
    def _generate_aurora_memory_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Aurora Memory Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'total_count': 0,
            'error': None,
            'recommendations': [],
            'status': 'ok',
        }
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Get the data
        if source_id not in parsed_logs:
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Set columns
        columns = subsection.columns if subsection.columns else log_data.columns
        result['columns'] = list(columns)
        result['total_count'] = len(log_data.rows)
        
        # Set rows
        for row in log_data.rows:
            row_data = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    row_data.append(row[col_idx])
                else:
                    row_data.append(None)
            result['rows'].append(row_data)
        
        # Build dynamic recommendations based on actual values
        recommendations = []
        
        # Check aurora_oom_status
        oom_status = values.get('aurora_oom_status', '')
        if oom_status == 'RESERVED':
            recommendations.append({
                'severity': 'warning',
                'title': 'Memory pressure detected',
                'message': 'aurora_oom_status is RESERVED. OOM avoidance is active. Review long-running queries and connections. Consider scaling up instance class.'
            })
            result['status'] = 'warning'
        
        # Check Aurora_reserved_mem_exceeded_incidents
        incidents = values.get('Aurora_reserved_mem_exceeded_incidents', '0')
        try:
            incidents_count = int(incidents)
            if incidents_count > 0:
                recommendations.append({
                    'severity': 'warning',
                    'title': 'Memory limits exceeded',
                    'message': f'Aurora_reserved_mem_exceeded_incidents = {incidents_count}. Review peak usage times, check for connection leaks, and consider larger instance.',
                    'count': incidents_count
                })
                result['status'] = 'warning'
        except (ValueError, TypeError):
            pass
        
        # Check aurora_oom_avoidance_recovery_state
        recovery_state = values.get('aurora_oom_avoidance_recovery_state', '')
        if recovery_state == 'ACTIVE':
            recommendations.append({
                'severity': 'warning',
                'title': 'OOM recovery active',
                'message': 'aurora_oom_avoidance_recovery_state is ACTIVE. The system is actively recovering from memory pressure.'
            })
            result['status'] = 'warning'
        
        # Check Aurora_lockmgr_memory_used (warn if > 100MB)
        lockmgr_mem = values.get('Aurora_lockmgr_memory_used', '0')
        try:
            lockmgr_bytes = int(lockmgr_mem)
            if lockmgr_bytes > 100 * 1024 * 1024:  # 100MB
                lockmgr_mb = lockmgr_bytes / (1024 * 1024)
                recommendations.append({
                    'severity': 'warning',
                    'title': 'High lock manager memory',
                    'message': f'Aurora_lockmgr_memory_used = {lockmgr_mb:.1f} MB. Check for long-running transactions or lock contention.',
                    'value_mb': lockmgr_mb
                })
                result['status'] = 'warning'
        except (ValueError, TypeError):
            pass
        
        # Check aurora_temptable_ram_allocation (warn if > 1GB)
        temptable_mem = values.get('aurora_temptable_ram_allocation', '0')
        try:
            temptable_bytes = int(temptable_mem)
            if temptable_bytes > 1024 * 1024 * 1024:  # 1GB
                temptable_gb = temptable_bytes / (1024 * 1024 * 1024)
                recommendations.append({
                    'severity': 'warning',
                    'title': 'High temporary table memory',
                    'message': f'aurora_temptable_ram_allocation = {temptable_gb:.2f} GB. Review queries creating temp tables and optimize or add indexes.',
                    'value_gb': temptable_gb
                })
                result['status'] = 'warning'
        except (ValueError, TypeError):
            pass
        
        result['recommendations'] = recommendations
        
        if not recommendations:
            result['status_message'] = 'All Aurora memory metrics are within normal ranges.'
        
        return result
    
    def _generate_aurora_pq_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Aurora Parallel Query Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'total_count': 0,
            'error': None,
            'analysis': [],
            'status': 'ok',
        }
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Get the data
        if source_id not in parsed_logs:
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Set columns
        columns = subsection.columns if subsection.columns else log_data.columns
        result['columns'] = list(columns)
        result['total_count'] = len(log_data.rows)
        
        # Set rows
        for row in log_data.rows:
            row_data = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    row_data.append(row[col_idx])
                else:
                    row_data.append(None)
            result['rows'].append(row_data)
        
        # Build dynamic analysis based on actual values
        analysis = []
        
        # Check Aurora_pq_request_throttled
        throttled = values.get('Aurora_pq_request_throttled', '0')
        try:
            throttled_count = int(throttled)
            if throttled_count > 0:
                analysis.append({
                    'severity': 'warning',
                    'title': 'PQ throttling detected',
                    'message': f'Aurora_pq_request_throttled = {throttled_count}. Parallel query concurrency limit reached. Consider scaling to a larger instance class for more concurrent PQ sessions.',
                    'count': throttled_count
                })
                result['status'] = 'warning'
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_failed
        failed = values.get('Aurora_pq_request_failed', '0')
        try:
            failed_count = int(failed)
            if failed_count > 0:
                analysis.append({
                    'severity': 'warning',
                    'title': 'PQ failures detected',
                    'message': f'Aurora_pq_request_failed = {failed_count}. Check storage layer health and query complexity.',
                    'count': failed_count
                })
                result['status'] = 'warning'
        except (ValueError, TypeError):
            pass
        
        # Calculate PQ success rate
        attempted = values.get('Aurora_pq_request_attempted', '0')
        executed = values.get('Aurora_pq_request_executed', '0')
        try:
            attempted_count = int(attempted)
            executed_count = int(executed)
            if attempted_count > 0:
                success_rate = (executed_count / attempted_count) * 100
                result['pq_success_rate'] = {
                    'attempted': attempted_count,
                    'executed': executed_count,
                    'rate_pct': success_rate
                }
                if success_rate < 70:
                    analysis.append({
                        'severity': 'warning',
                        'title': 'Low PQ success rate',
                        'message': f'{success_rate:.1f}% ({executed_count}/{attempted_count}). Many queries are not using parallel query. Review query patterns and table sizes.',
                        'rate_pct': success_rate
                    })
                    result['status'] = 'warning'
                elif success_rate < 90:
                    analysis.append({
                        'severity': 'info',
                        'title': 'PQ success rate',
                        'message': f'{success_rate:.1f}% ({executed_count}/{attempted_count}). Some queries are not using parallel query.',
                        'rate_pct': success_rate
                    })
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_pq_request_not_chosen_long_trx
        long_trx = values.get('Aurora_pq_request_not_chosen_long_trx', '0')
        try:
            long_trx_count = int(long_trx)
            if long_trx_count > 0:
                analysis.append({
                    'severity': 'info',
                    'title': 'Long transactions affecting PQ',
                    'message': f'Aurora_pq_request_not_chosen_long_trx = {long_trx_count}. Long-running transactions prevent parallel query. Review transaction management.',
                    'count': long_trx_count
                })
        except (ValueError, TypeError):
            pass
        
        result['analysis'] = analysis
        
        if not analysis:
            # Check if PQ is being used at all
            try:
                attempted_count = int(values.get('Aurora_pq_request_attempted', '0'))
                if attempted_count > 0:
                    result['status_message'] = 'Aurora parallel query is operating normally with no issues detected.'
                else:
                    result['status'] = 'info'
                    result['status_message'] = 'No parallel query requests have been attempted. This may be normal if parallel query is disabled or workload doesn\'t benefit from it.'
            except (ValueError, TypeError):
                pass
        
        return result
    
    def _generate_aurora_relay_log_status_subsection(
        self,
        subsection: 'ReportSubsection',
        parsed_logs: Dict[str, ParsedLogFile]
    ) -> Dict[str, Any]:
        """Generate JSON for Aurora Relay Log Status subsection with dynamic recommendations."""
        source_id = subsection.source
        
        # Base result structure
        result = {
            'name': subsection.name,
            'source': source_id,
            'columns': [],
            'rows': [],
            'total_count': 0,
            'error': None,
            'analysis': [],
            'status': 'ok',
        }
        
        # Add note if provided
        if subsection.note:
            result['note'] = subsection.note
        
        # Get the data
        if source_id not in parsed_logs:
            result['error'] = 'Query not executed'
            return result
        
        log_data = parsed_logs[source_id]
        
        # Handle error case
        if not log_data.success:
            result['error'] = log_data.error or 'Unknown error'
            return result
        
        # Handle no data case
        if not log_data.columns or not log_data.rows:
            if subsection.default_note:
                result['default_note'] = subsection.default_note
            return result
        
        # Build column mapping
        col_map = {col.upper(): idx for idx, col in enumerate(log_data.columns)}
        
        # Extract values into a dictionary for easy access
        values = {}
        for row in log_data.rows:
            var_name = str(row[col_map.get('VARIABLE_NAME', 0)]) if col_map.get('VARIABLE_NAME') is not None else ''
            var_value = row[col_map.get('VARIABLE_VALUE', 1)] if col_map.get('VARIABLE_VALUE') is not None else ''
            values[var_name] = var_value
        
        # Set columns
        columns = subsection.columns if subsection.columns else log_data.columns
        result['columns'] = list(columns)
        result['total_count'] = len(log_data.rows)
        
        # Set rows
        for row in log_data.rows:
            row_data = []
            for col in columns:
                col_idx = col_map.get(col.upper())
                if col_idx is not None and col_idx < len(row):
                    row_data.append(row[col_idx])
                else:
                    row_data.append(None)
            result['rows'].append(row_data)
        
        # Build dynamic analysis based on actual values
        analysis = []
        
        # Check Aurora_in_memory_relaylog_status
        relay_status = values.get('Aurora_in_memory_relaylog_status', '')
        disabled_reason = values.get('Aurora_in_memory_relaylog_disabled_reason', '')
        if relay_status.upper() == 'DISABLED':
            reason_text = f' Reason: {disabled_reason}.' if disabled_reason else ''
            analysis.append({
                'severity': 'warning',
                'title': 'In-memory relay log disabled',
                'message': f'{reason_text} This may impact replica performance. Consider enabling if supported.',
                'reason': disabled_reason
            })
            result['status'] = 'warning'
        
        # Check Aurora_in_memory_relaylog_fallback_count
        fallback_count = values.get('Aurora_in_memory_relaylog_fallback_count', '0')
        try:
            fallback_int = int(fallback_count)
            if fallback_int > 0:
                analysis.append({
                    'severity': 'info',
                    'title': 'Relay log fallbacks',
                    'message': f'Aurora_in_memory_relaylog_fallback_count = {fallback_int}. Large transactions caused fallback to persistent storage. Consider breaking up large transactions.',
                    'count': fallback_int
                })
        except (ValueError, TypeError):
            pass
        
        # Check Aurora_in_memory_relaylog_recovery_count
        recovery_count = values.get('Aurora_in_memory_relaylog_recovery_count', '0')
        try:
            recovery_int = int(recovery_count)
            if recovery_int > 0:
                analysis.append({
                    'severity': 'info',
                    'title': 'Relay log recoveries',
                    'message': f'Aurora_in_memory_relaylog_recovery_count = {recovery_int}. Relay log recovered from fallback state {recovery_int} times.',
                    'count': recovery_int
                })
        except (ValueError, TypeError):
            pass
        
        result['analysis'] = analysis
        
        if not analysis:
            if relay_status.upper() == 'ENABLED':
                result['status_message'] = 'Aurora in-memory relay log is enabled and operating normally.'
        
        return result
    
    def _process_rows(
        self,
        display_columns: List[Tuple[str, Optional[str]]],
        rows: List[Tuple[Any, ...]],
        column_map: Dict[str, int]
    ) -> List[List[Any]]:
        """
        Process rows and evaluate computed columns.
        
        Args:
            display_columns: List of (display_name, source_column_or_expr) tuples
            rows: Data rows
            column_map: Mapping of column names to indices
            
        Returns:
            List of row arrays with values
        """
        processed_rows = []
        
        for row in rows:
            # Convert row tuple to dict for expression evaluation
            # Guard against rows with fewer columns than expected (can happen
            # with version-dependent queries like SHOW REPLICA STATUS)
            row_len = len(row) if row else 0
            row_dict = {col: row[idx] for col, idx in column_map.items() if idx < row_len}
            
            row_values = []
            for display_name, source in display_columns:
                value = self._get_cell_value(source, row, column_map, row_dict)
                row_values.append(value)
            
            processed_rows.append(row_values)
        
        return processed_rows
    
    def _resolve_columns(
        self,
        columns: List[Union[str, 'ComputedColumn']],
        column_map: Dict[str, int]
    ) -> List[Tuple[str, Optional[str]]]:
        """
        Resolve column definitions to (display_name, source) tuples.
        
        Returns list of tuples where:
        - display_name: The column header to show
        - source: Either a column name or an expression string (prefixed with 'expr:')
        """
        # Import here to avoid circular imports
        from diagnostic_collector_mysql.models import ComputedColumn
        
        resolved = []
        for col in columns:
            if isinstance(col, str):
                # Simple column reference
                if col in column_map:
                    resolved.append((col, col))
            elif isinstance(col, ComputedColumn):
                # Computed column
                resolved.append((col.name, f'expr:{col.expr}'))
            elif isinstance(col, dict):
                # Dict format from JSON parsing
                if 'name' in col and 'expr' in col:
                    resolved.append((col['name'], f"expr:{col['expr']}"))
        
        return resolved
    
    def _get_cell_value(
        self,
        source: Optional[str],
        row: Tuple[Any, ...],
        column_map: Dict[str, int],
        row_dict: Dict[str, Any]
    ) -> Any:
        """Get the value for a cell, handling both direct columns and expressions."""
        if source is None:
            return None
        
        if source.startswith('expr:'):
            # Evaluate expression
            expr = source[5:]
            return self._evaluate_expression(expr, row_dict)
        else:
            # Direct column reference
            if source in column_map:
                idx = column_map[source]
                return row[idx] if idx < len(row) else None
            return None
    
    def _evaluate_expression(self, expr: str, row_dict: Dict[str, Any]) -> Optional[float]:
        """
        Evaluate a computed column expression.
        
        Uses caching to avoid re-parsing the same expression multiple times.
        """
        if expr not in self._expression_cache:
            self._expression_cache[expr] = ExpressionEvaluator(expr)
        
        return self._expression_cache[expr].evaluate(row_dict)
    
    def _check_condition(
        self,
        condition: Optional[Dict[str, List[str]]],
        engine_type: Optional[str],
        min_engine_version: Optional[str] = None,
        max_engine_version: Optional[str] = None
    ) -> bool:
        """Check if a subsection condition is met (engine and version filtering)."""
        # Check engine condition
        if condition is not None:
            if 'engine' in condition and engine_type:
                allowed_engines = condition['engine']
                if engine_type not in allowed_engines:
                    return False

        # Check version conditions
        if min_engine_version is not None and max_engine_version is not None:
            if min_engine_version:
                min_version = self._parse_version(min_engine_version)
                if self._database_version < min_version:
                    return False
            if max_engine_version:
                max_version = self._parse_version(max_engine_version)
                if self._database_version > max_version:
                    return False

        return True


class PartialReportGenerator:
    """
    Generates partial reports from available query log data.
    
    This class is designed to handle interrupted executions (SIGINT/SIGTERM)
    or cases where some queries fail. It scans the output directory for
    available query log files and generates a report from whatever data
    is available, marking missing sections appropriately.
    
    Responsibilities:
    - Generate partial report from available data when execution is interrupted
    
    Features:
    - Scans output directory for available query log files
    - Parses all available logs using LogFileParser
    - Generates report using the appropriate format generator
    - Marks sections/subsections as "partial" or "incomplete" where data is missing
    - Can be called from a signal handler (SIGINT/SIGTERM)
    - Thread-safe for use in signal handlers
    """
    
    def __init__(
        self,
        output_dir: str,
        report_format: 'ReportFormat',
        output_format: str = 'html',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        expected_queries: Optional[List[str]] = None
    ):
        """
        Initialize the PartialReportGenerator.
        
        Args:
            output_dir: Path to the output directory containing query log files
            report_format: ReportFormat object defining the report structure
            output_format: Output format ('html', 'markdown', 'text', 'json')
            collection_limit: Maximum rows per subsection (default: 50)
            timestamp: Report generation timestamp (default: current time)
            database_info: Dictionary with database information (host, version, etc.)
            expected_queries: List of query IDs that were expected to run
        """
        self._output_dir = output_dir
        self._report_format = report_format
        self._output_format = output_format.lower()
        self._collection_limit = collection_limit
        self._timestamp = timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._database_info = database_info or {}
        self._expected_queries = set(expected_queries) if expected_queries else set()
        self._log_parser = LogFileParser(output_dir)
        self._is_partial = False
        self._missing_queries: List[str] = []

    def generate_from_available_logs(
        self,
        engine_type: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Scan output directory and generate report from available data.
        
        This method is safe to call from a signal handler as it doesn't
        require any database connection or external resources.
        
        Args:
            engine_type: The detected database engine type (for condition filtering)
            
        Returns:
            Tuple of (report_content, output_file_path)
        """
        # Parse all available log files
        parsed_logs = self._log_parser.parse_all_logs()
        
        # Determine if this is a partial report
        self._determine_partial_status(parsed_logs)
        
        # Add partial report notice to database info
        db_info = dict(self._database_info)
        if self._is_partial:
            db_info['partial_report'] = True
            db_info['missing_queries'] = self._missing_queries
        
        # Generate report using appropriate generator
        report_content = self._generate_report(parsed_logs, engine_type, db_info)
        
        # Write report to file
        output_path = self._write_report(report_content)
        
        return report_content, output_path
    
    def _determine_partial_status(self, parsed_logs: Dict[str, ParsedLogFile]) -> None:
        """
        Determine if this is a partial report based on available data.
        
        A report is considered partial if:
        - Some expected queries are missing
        - Some queries failed with errors
        """
        available_query_ids = set(parsed_logs.keys())
        
        # Check for missing expected queries
        if self._expected_queries:
            self._missing_queries = list(self._expected_queries - available_query_ids)
            if self._missing_queries:
                self._is_partial = True
        
        # Check for failed queries
        failed_queries = [
            qid for qid, log in parsed_logs.items()
            if not log.success
        ]
        if failed_queries:
            self._is_partial = True

    def _generate_report(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str],
        database_info: Dict[str, Any]
    ) -> str:
        """
        Generate report content using the appropriate format generator.
        
        Args:
            parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
            engine_type: The detected database engine type
            database_info: Database information including partial report status
            
        Returns:
            Report content as a string
        """
        if self._output_format == 'html':
            generator = PartialHTMLReportGenerator(
                self._report_format,
                self._collection_limit,
                self._timestamp,
                database_info,
                self._is_partial,
                self._missing_queries
            )
        elif self._output_format == 'markdown':
            generator = PartialMarkdownReportGenerator(
                self._report_format,
                self._collection_limit,
                self._timestamp,
                database_info,
                self._is_partial,
                self._missing_queries
            )
        elif self._output_format == 'text':
            generator = PartialTextReportGenerator(
                self._report_format,
                self._collection_limit,
                self._timestamp,
                database_info,
                self._is_partial,
                self._missing_queries
            )
        elif self._output_format == 'json':
            generator = PartialJSONReportGenerator(
                self._report_format,
                self._collection_limit,
                self._timestamp,
                database_info,
                self._is_partial,
                self._missing_queries
            )
        else:
            # Default to HTML
            generator = PartialHTMLReportGenerator(
                self._report_format,
                self._collection_limit,
                self._timestamp,
                database_info,
                self._is_partial,
                self._missing_queries
            )
        
        return generator.generate(parsed_logs, engine_type)

    def _write_report(self, content: str) -> str:
        """
        Write report content to file.
        
        Args:
            content: Report content to write
            
        Returns:
            Path to the written report file
        """
        # Determine file extension
        ext_map = {
            'html': '.html',
            'markdown': '.md',
            'text': '.txt',
            'json': '.json'
        }
        ext = ext_map.get(self._output_format, '.html')
        
        # Build filename - use 'partial_' prefix if partial
        prefix = 'partial_report' if self._is_partial else 'report'
        # Convert timestamp to filename format (YYYYMMDD_HHMMSS)
        ts = self._timestamp.replace('-', '').replace(':', '').replace(' ', '_')
        
        filename = f"{prefix}_{ts}{ext}"
        filepath = os.path.join(self._output_dir, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"Partial report written to: {filepath}")
        except IOError as e:
            logger.error(f"Failed to write partial report: {e}")
            raise
        
        return filepath
    
    @property
    def is_partial(self) -> bool:
        """Return whether the generated report is partial."""
        return self._is_partial
    
    @property
    def missing_queries(self) -> List[str]:
        """Return list of missing query IDs."""
        return self._missing_queries


class PartialHTMLReportGenerator(HTMLReportGenerator):
    """
    HTML report generator with partial report support.
    
    Extends HTMLReportGenerator to add:
    - Partial report notice banner
    - PARTIAL badge in header
    - Uses same formatting as complete reports
    """
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        is_partial: bool = False,
        missing_queries: Optional[List[str]] = None,
        extended_checks: bool = False
    ):
        """Initialize with partial report status."""
        super().__init__(report_format, collection_limit, timestamp, database_info, extended_checks)
        self._is_partial = is_partial
        self._missing_queries = missing_queries or []
    
    def _generate_report_header(self, parsed_logs: Optional[Dict[str, ParsedLogFile]] = None, engine_type: Optional[str] = None) -> str:
        """Generate the report header with PARTIAL badge if applicable."""
        header = super()._generate_report_header(parsed_logs, engine_type)

        if self._is_partial:
            # Add PARTIAL badge after the h1 title
            header = header.replace(
                '<h1>Diagnostic Collector for MySQL Report</h1>',
                '<h1>Diagnostic Collector for MySQL Report<span style="background: #f59e0b; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-left: 10px;">PARTIAL</span></h1>'
            )

        return header
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """Generate HTML report with partial report notice if applicable."""
        # Generate the full report using parent class
        html_content = super().generate(parsed_logs, engine_type)
        
        if not self._is_partial:
            return html_content
        
        # Insert partial notice after the report header div
        partial_notice = self._generate_partial_notice()
        
        # Find the end of the report-header div and insert notice after it
        # Look for the nav div which comes after the header
        insert_marker = '<div class="nav">'
        if insert_marker in html_content:
            html_content = html_content.replace(
                insert_marker,
                partial_notice + '\n' + insert_marker
            )
        
        return html_content

    def _generate_partial_notice(self) -> str:
        """Generate a partial report notice banner."""
        missing_list = ''
        if self._missing_queries:
            missing_items = ''.join(
                f'<li>{self._escape_html(q)}</li>'
                for q in self._missing_queries[:10]
            )
            if len(self._missing_queries) > 10:
                missing_items += f'<li>... and {len(self._missing_queries) - 10} more</li>'
            missing_list = f'''
            <div style="margin-top: 10px;">
                <strong>Missing queries:</strong>
                <ul style="margin: 5px 0; padding-left: 20px;">{missing_items}</ul>
            </div>'''
        
        return f'''
<div style="background: #fef3c7; border: 2px solid #f59e0b; border-radius: 8px; padding: 15px 20px; margin-bottom: 20px;">
    <div style="display: flex; align-items: center; gap: 10px;">
        <span style="font-size: 24px;">⚠️</span>
        <div>
            <strong style="color: #92400e; font-size: 16px;">Partial Report</strong>
            <p style="margin: 5px 0 0 0; color: #78350f;">
                This report was generated from incomplete data. Some queries may not have completed 
                due to interruption or errors. Sections with missing data are marked accordingly.
            </p>
            {missing_list}
        </div>
    </div>
</div>'''


class PartialMarkdownReportGenerator(MarkdownReportGenerator):
    """
    Markdown report generator with partial report support.
    
    Extends MarkdownReportGenerator to add:
    - Partial report notice
    - Missing query indicators
    - "Data not available" messages for missing sections
    """
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        is_partial: bool = False,
        missing_queries: Optional[List[str]] = None,
        extended_checks: bool = False
    ):
        """Initialize with partial report status."""
        super().__init__(report_format, collection_limit, timestamp, database_info, extended_checks)
        self._is_partial = is_partial
        self._missing_queries = missing_queries or []
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """Generate Markdown report with partial report notice if applicable."""
        md_parts = [self._generate_header()]
        
        # Add partial report notice if applicable
        if self._is_partial:
            md_parts.append(self._generate_partial_notice())
        
        md_parts.extend([
            self._generate_table_of_contents(),
            self._generate_health_overview(parsed_logs),
        ])
        
        # Generate each section from report_format
        for section in self._report_format.sections:
            section_md = self._generate_section(section, parsed_logs, engine_type)
            if section_md:
                md_parts.append(section_md)
        
        md_parts.append(self._generate_footer())
        
        return '\n'.join(md_parts)

    def _generate_partial_notice(self) -> str:
        """Generate a partial report notice."""
        missing_list = ''
        if self._missing_queries:
            missing_items = '\n'.join(
                f'  - `{q}`'
                for q in self._missing_queries[:10]
            )
            if len(self._missing_queries) > 10:
                missing_items += f'\n  - ... and {len(self._missing_queries) - 10} more'
            missing_list = f'\n\n**Missing queries:**\n{missing_items}'
        
        return f"""
> ⚠️ **PARTIAL REPORT**
> 
> This report was generated from incomplete data. Some queries may not have completed 
> due to interruption or errors. Sections with missing data are marked accordingly.
{missing_list}

---
"""
    
    def _generate_header(self) -> str:
        """Generate report header with partial indicator."""
        host = self._database_info.get('host', 'Unknown')
        version = self._database_info.get('version', 'Unknown')
        engine = self._database_info.get('engine_type', 'Unknown')
        
        partial_badge = ' ⚠️ PARTIAL' if self._is_partial else ''
        
        return f"""# Diagnostic Collector for MySQL Health Report{partial_badge}

**Generated:** {self._timestamp}

| Property | Value |
|----------|-------|
| Host | {self._escape_markdown(host)} |
| Version | {self._escape_markdown(version)} |
| Engine | {self._escape_markdown(engine)} |

---
"""


class PartialTextReportGenerator(TextReportGenerator):
    """
    Text report generator with partial report support.
    
    Extends TextReportGenerator to add:
    - Partial report notice
    - Missing query indicators
    - "Data not available" messages for missing sections
    """
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        is_partial: bool = False,
        missing_queries: Optional[List[str]] = None,
        extended_checks: bool = False
    ):
        """Initialize with partial report status."""
        super().__init__(report_format, collection_limit, timestamp, database_info, extended_checks)
        self._is_partial = is_partial
        self._missing_queries = missing_queries or []
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """Generate text report with partial report notice if applicable."""
        text_parts = [self._generate_header()]
        
        # Add partial report notice if applicable
        if self._is_partial:
            text_parts.append(self._generate_partial_notice())
        
        text_parts.extend([
            self._generate_table_of_contents(),
            self._generate_health_overview(parsed_logs),
        ])
        
        # Generate each section from report_format
        for section in self._report_format.sections:
            section_text = self._generate_section(section, parsed_logs, engine_type)
            if section_text:
                text_parts.append(section_text)
        
        text_parts.append(self._generate_footer())
        
        return '\n'.join(text_parts)

    def _generate_partial_notice(self) -> str:
        """Generate a partial report notice."""
        notice_lines = [
            '',
            '!' * 80,
            '!                           PARTIAL REPORT WARNING                              !',
            '!' * 80,
            '',
            'This report was generated from incomplete data. Some queries may not have',
            'completed due to interruption or errors. Sections with missing data are',
            'marked accordingly.',
            '',
        ]
        
        if self._missing_queries:
            notice_lines.append('Missing queries:')
            for q in self._missing_queries[:10]:
                notice_lines.append(f'  - {q}')
            if len(self._missing_queries) > 10:
                notice_lines.append(f'  - ... and {len(self._missing_queries) - 10} more')
            notice_lines.append('')
        
        notice_lines.append(self.SECTION_SEPARATOR)
        
        return '\n'.join(notice_lines)
    
    def _generate_header(self) -> str:
        """Generate report header with partial indicator."""
        host = self._database_info.get('host', 'Unknown')
        version = self._database_info.get('version', 'Unknown')
        engine = self._database_info.get('engine_type', 'Unknown')
        
        partial_indicator = ' [PARTIAL]' if self._is_partial else ''
        
        header_lines = [
            self.SECTION_SEPARATOR,
            f'                    Diagnostic Collector for MySQL Health Report{partial_indicator}',
            self.SECTION_SEPARATOR,
            '',
            f'Generated: {self._timestamp}',
            '',
            'Database Information:',
            f'  Host:    {host}',
            f'  Version: {version}',
            f'  Engine:  {engine}',
            '',
            self.SECTION_SEPARATOR,
        ]
        
        return '\n'.join(header_lines)


class PartialJSONReportGenerator(JSONReportGenerator):
    """
    JSON report generator with partial report support.
    
    Extends JSONReportGenerator to add:
    - Partial report metadata
    - Missing query information
    - "Data not available" indicators for missing sections
    """
    
    def __init__(
        self,
        report_format: 'ReportFormat',
        collection_limit: Optional[int] = None,
        timestamp: Optional[str] = None,
        database_info: Optional[Dict[str, Any]] = None,
        is_partial: bool = False,
        missing_queries: Optional[List[str]] = None,
        extended_checks: bool = False
    ):
        """Initialize with partial report status."""
        super().__init__(report_format, collection_limit, timestamp, database_info, extended_checks)
        self._is_partial = is_partial
        self._missing_queries = missing_queries or []
    
    def generate(
        self,
        parsed_logs: Dict[str, ParsedLogFile],
        engine_type: Optional[str] = None
    ) -> str:
        """Generate JSON report with partial report metadata if applicable."""
        report = {
            'metadata': self._generate_metadata(),
            'health_overview': self._generate_health_overview(parsed_logs),
            'sections': self._generate_sections(parsed_logs, engine_type),
        }
        
        return json.dumps(report, indent=2, default=self._json_serializer)
    
    def _generate_metadata(self) -> Dict[str, Any]:
        """Generate metadata section with partial report information."""
        metadata = {
            'timestamp': self._timestamp,
            'database': {
                'host': self._database_info.get('host'),
                'version': self._database_info.get('version'),
                'engine_type': self._database_info.get('engine_type'),
            },
            'partial_report': self._is_partial,
        }
        
        if self._is_partial:
            metadata['missing_queries'] = self._missing_queries
            metadata['partial_reason'] = (
                'Report generated from incomplete data due to interruption or query failures'
            )
        
        return metadata
