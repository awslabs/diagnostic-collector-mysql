# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
InnoDB Status Parser module for Diagnostic Collector for MySQL.

This module provides functionality to parse the output of SHOW ENGINE INNODB STATUS
and extract meaningful metrics from each section.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class InnoDBStatusData:
    """Parsed data from SHOW ENGINE INNODB STATUS output."""
    
    # Deadlock section
    has_deadlock: bool = False
    deadlock_timestamp: Optional[str] = None
    deadlock_transactions: List[Dict[str, Any]] = field(default_factory=list)
    deadlock_victim: Optional[str] = None
    deadlock_raw: Optional[str] = None
    
    # Foreign Key Error section
    has_fk_error: bool = False
    fk_error_timestamp: Optional[str] = None
    fk_error_statement: Optional[str] = None
    fk_error_constraint: Optional[str] = None
    fk_error_table: Optional[str] = None
    fk_error_raw: Optional[str] = None
    
    # Semaphores section
    os_waits_reservation_count: int = 0
    os_waits_signal_count: int = 0
    rw_shared_spins: int = 0
    rw_shared_rounds: int = 0
    rw_shared_os_waits: int = 0
    rw_excl_spins: int = 0
    rw_excl_rounds: int = 0
    rw_excl_os_waits: int = 0
    rw_sx_spins: int = 0
    rw_sx_rounds: int = 0
    rw_sx_os_waits: int = 0
    spin_rounds_per_wait_shared: float = 0.0
    spin_rounds_per_wait_excl: float = 0.0
    spin_rounds_per_wait_sx: float = 0.0
    current_waits: List[str] = field(default_factory=list)
    
    # Transactions section
    trx_id_counter: int = 0
    purge_done_for_trx: int = 0
    purge_undo_no: int = 0
    purge_state: str = ""
    history_list_length: int = 0
    purge_lag: int = 0
    
    # Log section
    log_sequence_number: int = 0
    log_flushed_up_to: int = 0
    pages_flushed_up_to: int = 0
    last_checkpoint_at: int = 0
    pending_log_flushes: int = 0
    pending_chkp_writes: int = 0
    log_ios_done: int = 0
    log_ios_per_second: float = 0.0
    checkpoint_lag: int = 0
    unflushed_log: int = 0
    
    # Buffer Pool section
    total_memory_allocated: int = 0
    buffer_pool_size: int = 0
    free_buffers: int = 0
    database_pages: int = 0
    old_database_pages: int = 0
    modified_db_pages: int = 0
    pending_reads: int = 0
    pending_writes_lru: int = 0
    pending_writes_flush_list: int = 0
    pending_writes_single_page: int = 0
    pages_made_young: int = 0
    pages_made_not_young: int = 0
    youngs_per_sec: float = 0.0
    non_youngs_per_sec: float = 0.0
    pages_read: int = 0
    pages_created: int = 0
    pages_written: int = 0
    reads_per_sec: float = 0.0
    creates_per_sec: float = 0.0
    writes_per_sec: float = 0.0
    buffer_pool_hit_rate: int = 0  # X out of 1000
    buffer_pool_hit_rate_denominator: int = 1000
    young_making_rate: int = 0
    not_young_rate: int = 0
    read_ahead_per_sec: float = 0.0
    evicted_without_access_per_sec: float = 0.0
    lru_len: int = 0
    
    # Adaptive Hash Index section
    hash_searches_per_sec: float = 0.0
    non_hash_searches_per_sec: float = 0.0
    hash_table_size: int = 0
    
    # Row Operations section
    queries_inside_innodb: int = 0
    queries_in_queue: int = 0
    read_views_open: int = 0
    main_thread_state: str = ""
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_deleted: int = 0
    rows_read: int = 0
    inserts_per_sec: float = 0.0
    updates_per_sec: float = 0.0
    deletes_per_sec: float = 0.0
    reads_per_sec_rows: float = 0.0
    
    # Raw sections for display
    raw_output: str = ""


def parse_innodb_status(status_text: str) -> InnoDBStatusData:
    """
    Parse the output of SHOW ENGINE INNODB STATUS.
    
    Args:
        status_text: The raw text output from SHOW ENGINE INNODB STATUS
        
    Returns:
        InnoDBStatusData object with parsed metrics
    """
    data = InnoDBStatusData()
    data.raw_output = status_text
    
    if not status_text:
        return data
    
    # Split into sections
    sections = _split_into_sections(status_text)
    
    # Parse each section
    if 'LATEST DETECTED DEADLOCK' in sections:
        _parse_deadlock_section(sections['LATEST DETECTED DEADLOCK'], data)
    
    if 'LATEST FOREIGN KEY ERROR' in sections:
        _parse_fk_error_section(sections['LATEST FOREIGN KEY ERROR'], data)
    
    if 'SEMAPHORES' in sections:
        _parse_semaphores_section(sections['SEMAPHORES'], data)
    
    if 'TRANSACTIONS' in sections:
        _parse_transactions_section(sections['TRANSACTIONS'], data)
    
    if 'LOG' in sections:
        _parse_log_section(sections['LOG'], data)
    
    if 'BUFFER POOL AND MEMORY' in sections:
        _parse_buffer_pool_section(sections['BUFFER POOL AND MEMORY'], data)
    
    if 'INSERT BUFFER AND ADAPTIVE HASH INDEX' in sections:
        _parse_adaptive_hash_section(sections['INSERT BUFFER AND ADAPTIVE HASH INDEX'], data)
    
    if 'ROW OPERATIONS' in sections:
        _parse_row_operations_section(sections['ROW OPERATIONS'], data)
    
    # Calculate derived metrics
    data.purge_lag = data.trx_id_counter - data.purge_done_for_trx if data.trx_id_counter > 0 else 0
    data.checkpoint_lag = data.log_sequence_number - data.last_checkpoint_at if data.log_sequence_number > 0 else 0
    data.unflushed_log = data.log_sequence_number - data.log_flushed_up_to if data.log_sequence_number > 0 else 0
    
    return data


def _split_into_sections(text: str) -> Dict[str, str]:
    """Split the InnoDB status output into named sections."""
    sections = {}
    current_section = None
    current_content = []
    
    # Section headers are typically preceded by dashes
    section_pattern = re.compile(r'^-+\s*$')
    header_pattern = re.compile(r'^([A-Z][A-Z\s&]+[A-Z])\s*$')
    
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Check for section header pattern: dashes, then header, then dashes
        if section_pattern.match(line) and i + 2 < len(lines):
            potential_header = lines[i + 1].strip()
            if header_pattern.match(potential_header) and section_pattern.match(lines[i + 2]):
                # Save previous section
                if current_section:
                    sections[current_section] = '\n'.join(current_content)
                
                current_section = potential_header
                current_content = []
                i += 3  # Skip the header lines
                continue
        
        if current_section:
            current_content.append(line)
        
        i += 1
    
    # Save last section
    if current_section:
        sections[current_section] = '\n'.join(current_content)
    
    return sections


def _parse_deadlock_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the LATEST DETECTED DEADLOCK section."""
    data.has_deadlock = True
    data.deadlock_raw = text
    
    # Extract timestamp
    timestamp_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', text)
    if timestamp_match:
        data.deadlock_timestamp = timestamp_match.group(1)
    
    # Extract transactions involved
    trx_pattern = re.compile(r'\*\*\*\s*\((\d+)\)\s*TRANSACTION[:\s]*\n(.*?)(?=\*\*\*|\Z)', re.DOTALL)
    for match in trx_pattern.finditer(text):
        trx_num = match.group(1)
        trx_content = match.group(2)
        
        trx_info = {'number': trx_num}
        
        # Extract transaction ID
        trx_id_match = re.search(r'TRANSACTION\s+(\d+)', trx_content)
        if trx_id_match:
            trx_info['trx_id'] = trx_id_match.group(1)
        
        # Extract query
        query_match = re.search(r'(?:MySQL thread id \d+.*?\n)(.*?)(?=\*\*\*|---|\Z)', trx_content, re.DOTALL)
        if query_match:
            query = query_match.group(1).strip()
            if query and not query.startswith('RECORD LOCKS') and not query.startswith('TABLE LOCK'):
                trx_info['query'] = query[:500]  # Limit query length
        
        data.deadlock_transactions.append(trx_info)
    
    # Extract victim
    victim_match = re.search(r'WE ROLL BACK TRANSACTION\s*\((\d+)\)', text)
    if victim_match:
        data.deadlock_victim = victim_match.group(1)


def _parse_fk_error_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the LATEST FOREIGN KEY ERROR section."""
    data.has_fk_error = True
    data.fk_error_raw = text
    
    # Extract timestamp
    timestamp_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', text)
    if timestamp_match:
        data.fk_error_timestamp = timestamp_match.group(1)
    
    # Extract constraint name
    constraint_match = re.search(r'CONSTRAINT\s+[`"]?(\w+)[`"]?', text)
    if constraint_match:
        data.fk_error_constraint = constraint_match.group(1)
    
    # Extract table name
    table_match = re.search(r'Foreign key constraint fails for table\s+[`"]?(\S+)[`"]?', text)
    if table_match:
        data.fk_error_table = table_match.group(1)
    
    # Extract failed statement (first few lines after timestamp)
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if 'Transaction:' in line or 'TRANSACTION' in line:
            # Look for the query in subsequent lines
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j].strip() and not lines[j].startswith('mysql tables') and not lines[j].startswith('RECORD'):
                    if any(kw in lines[j].upper() for kw in ['INSERT', 'UPDATE', 'DELETE', 'SELECT']):
                        data.fk_error_statement = lines[j].strip()[:500]
                        break
            break


def _parse_semaphores_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the SEMAPHORES section."""
    # OS WAIT ARRAY INFO
    reservation_match = re.search(r'reservation count\s+(\d+)', text)
    if reservation_match:
        data.os_waits_reservation_count = int(reservation_match.group(1))
    
    signal_match = re.search(r'signal count\s+(\d+)', text)
    if signal_match:
        data.os_waits_signal_count = int(signal_match.group(1))
    
    # RW-shared spins
    rw_shared_match = re.search(r'RW-shared spins\s+(\d+),\s*rounds\s+(\d+),\s*OS waits\s+(\d+)', text)
    if rw_shared_match:
        data.rw_shared_spins = int(rw_shared_match.group(1))
        data.rw_shared_rounds = int(rw_shared_match.group(2))
        data.rw_shared_os_waits = int(rw_shared_match.group(3))
    
    # RW-excl spins
    rw_excl_match = re.search(r'RW-excl spins\s+(\d+),\s*rounds\s+(\d+),\s*OS waits\s+(\d+)', text)
    if rw_excl_match:
        data.rw_excl_spins = int(rw_excl_match.group(1))
        data.rw_excl_rounds = int(rw_excl_match.group(2))
        data.rw_excl_os_waits = int(rw_excl_match.group(3))
    
    # RW-sx spins
    rw_sx_match = re.search(r'RW-sx spins\s+(\d+),\s*rounds\s+(\d+),\s*OS waits\s+(\d+)', text)
    if rw_sx_match:
        data.rw_sx_spins = int(rw_sx_match.group(1))
        data.rw_sx_rounds = int(rw_sx_match.group(2))
        data.rw_sx_os_waits = int(rw_sx_match.group(3))
    
    # Spin rounds per wait
    spin_rounds_match = re.search(r'Spin rounds per wait:\s*([\d.]+)\s*RW-shared,\s*([\d.]+)\s*RW-excl,\s*([\d.]+)\s*RW-sx', text)
    if spin_rounds_match:
        data.spin_rounds_per_wait_shared = float(spin_rounds_match.group(1))
        data.spin_rounds_per_wait_excl = float(spin_rounds_match.group(2))
        data.spin_rounds_per_wait_sx = float(spin_rounds_match.group(3))
    
    # Current waits (threads waiting for semaphores)
    wait_pattern = re.compile(r'--Thread\s+\d+\s+has waited.*?(?=--Thread|\Z)', re.DOTALL)
    for match in wait_pattern.finditer(text):
        data.current_waits.append(match.group(0).strip()[:500])


def _parse_transactions_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the TRANSACTIONS section."""
    # Trx id counter
    trx_counter_match = re.search(r'Trx id counter\s+(\d+)', text)
    if trx_counter_match:
        data.trx_id_counter = int(trx_counter_match.group(1))
    
    # Purge done for trx's
    purge_match = re.search(r"Purge done for trx's n:o\s*<\s*(\d+)\s+undo n:o\s*<\s*(\d+)\s+state:\s*(.+?)(?:\n|$)", text)
    if purge_match:
        data.purge_done_for_trx = int(purge_match.group(1))
        data.purge_undo_no = int(purge_match.group(2))
        data.purge_state = purge_match.group(3).strip()
    
    # History list length
    history_match = re.search(r'History list length\s+(\d+)', text)
    if history_match:
        data.history_list_length = int(history_match.group(1))


def _parse_log_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the LOG section."""
    # Log sequence number
    lsn_match = re.search(r'Log sequence number\s+(\d+)', text)
    if lsn_match:
        data.log_sequence_number = int(lsn_match.group(1))
    
    # Log flushed up to
    flushed_match = re.search(r'Log flushed up to\s+(\d+)', text)
    if flushed_match:
        data.log_flushed_up_to = int(flushed_match.group(1))
    
    # Pages flushed up to
    pages_flushed_match = re.search(r'Pages flushed up to\s+(\d+)', text)
    if pages_flushed_match:
        data.pages_flushed_up_to = int(pages_flushed_match.group(1))
    
    # Last checkpoint at
    checkpoint_match = re.search(r'Last checkpoint at\s+(\d+)', text)
    if checkpoint_match:
        data.last_checkpoint_at = int(checkpoint_match.group(1))
    
    # Pending log flushes and checkpoint writes
    pending_match = re.search(r'(\d+)\s+pending log flushes,\s*(\d+)\s+pending chkp writes', text)
    if pending_match:
        data.pending_log_flushes = int(pending_match.group(1))
        data.pending_chkp_writes = int(pending_match.group(2))
    
    # Log i/o's done
    log_io_match = re.search(r"(\d+)\s+log i/o's done,\s*([\d.]+)\s+log i/o's/second", text)
    if log_io_match:
        data.log_ios_done = int(log_io_match.group(1))
        data.log_ios_per_second = float(log_io_match.group(2))


def _parse_buffer_pool_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the BUFFER POOL AND MEMORY section."""
    # Total large memory allocated
    memory_match = re.search(r'Total large memory allocated\s+(\d+)', text)
    if memory_match:
        data.total_memory_allocated = int(memory_match.group(1))
    
    # Buffer pool size
    pool_size_match = re.search(r'Buffer pool size\s+(\d+)', text)
    if pool_size_match:
        data.buffer_pool_size = int(pool_size_match.group(1))
    
    # Free buffers
    free_match = re.search(r'Free buffers\s+(\d+)', text)
    if free_match:
        data.free_buffers = int(free_match.group(1))
    
    # Database pages
    db_pages_match = re.search(r'Database pages\s+(\d+)', text)
    if db_pages_match:
        data.database_pages = int(db_pages_match.group(1))
    
    # Old database pages
    old_pages_match = re.search(r'Old database pages\s+(\d+)', text)
    if old_pages_match:
        data.old_database_pages = int(old_pages_match.group(1))
    
    # Modified db pages
    modified_match = re.search(r'Modified db pages\s+(\d+)', text)
    if modified_match:
        data.modified_db_pages = int(modified_match.group(1))
    
    # Pending reads
    pending_reads_match = re.search(r'Pending reads\s+(\d+)', text)
    if pending_reads_match:
        data.pending_reads = int(pending_reads_match.group(1))
    
    # Pending writes
    pending_writes_match = re.search(r'Pending writes:\s*LRU\s+(\d+),\s*flush list\s+(\d+),\s*single page\s+(\d+)', text)
    if pending_writes_match:
        data.pending_writes_lru = int(pending_writes_match.group(1))
        data.pending_writes_flush_list = int(pending_writes_match.group(2))
        data.pending_writes_single_page = int(pending_writes_match.group(3))
    
    # Pages made young, not young
    young_match = re.search(r'Pages made young\s+(\d+),\s*not young\s+(\d+)', text)
    if young_match:
        data.pages_made_young = int(young_match.group(1))
        data.pages_made_not_young = int(young_match.group(2))
    
    # youngs/s, non-youngs/s
    youngs_rate_match = re.search(r'([\d.]+)\s+youngs/s,\s*([\d.]+)\s+non-youngs/s', text)
    if youngs_rate_match:
        data.youngs_per_sec = float(youngs_rate_match.group(1))
        data.non_youngs_per_sec = float(youngs_rate_match.group(2))
    
    # Pages read, created, written
    pages_match = re.search(r'Pages read\s+(\d+),\s*created\s+(\d+),\s*written\s+(\d+)', text)
    if pages_match:
        data.pages_read = int(pages_match.group(1))
        data.pages_created = int(pages_match.group(2))
        data.pages_written = int(pages_match.group(3))
    
    # reads/s, creates/s, writes/s
    rates_match = re.search(r'([\d.]+)\s+reads/s,\s*([\d.]+)\s+creates/s,\s*([\d.]+)\s+writes/s', text)
    if rates_match:
        data.reads_per_sec = float(rates_match.group(1))
        data.creates_per_sec = float(rates_match.group(2))
        data.writes_per_sec = float(rates_match.group(3))
    
    # Buffer pool hit rate
    hit_rate_match = re.search(r'Buffer pool hit rate\s+(\d+)\s*/\s*(\d+)', text)
    if hit_rate_match:
        data.buffer_pool_hit_rate = int(hit_rate_match.group(1))
        data.buffer_pool_hit_rate_denominator = int(hit_rate_match.group(2))
    
    # young-making rate, not rate
    young_rate_match = re.search(r'young-making rate\s+(\d+)\s*/\s*\d+\s+not\s+(\d+)\s*/\s*\d+', text)
    if young_rate_match:
        data.young_making_rate = int(young_rate_match.group(1))
        data.not_young_rate = int(young_rate_match.group(2))
    
    # Pages read ahead, evicted without access
    read_ahead_match = re.search(r'Pages read ahead\s+([\d.]+)/s,\s*evicted without access\s+([\d.]+)/s', text)
    if read_ahead_match:
        data.read_ahead_per_sec = float(read_ahead_match.group(1))
        data.evicted_without_access_per_sec = float(read_ahead_match.group(2))
    
    # LRU len
    lru_match = re.search(r'LRU len:\s*(\d+)', text)
    if lru_match:
        data.lru_len = int(lru_match.group(1))


def _parse_adaptive_hash_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the INSERT BUFFER AND ADAPTIVE HASH INDEX section."""
    # Hash searches/s, non-hash searches/s
    hash_match = re.search(r'([\d.]+)\s+hash searches/s,\s*([\d.]+)\s+non-hash searches/s', text)
    if hash_match:
        data.hash_searches_per_sec = float(hash_match.group(1))
        data.non_hash_searches_per_sec = float(hash_match.group(2))
    
    # Hash table size
    table_size_match = re.search(r'Hash table size\s+(\d+)', text)
    if table_size_match:
        data.hash_table_size = int(table_size_match.group(1))


def _parse_row_operations_section(text: str, data: InnoDBStatusData) -> None:
    """Parse the ROW OPERATIONS section."""
    # Queries inside InnoDB, queries in queue
    queries_match = re.search(r'(\d+)\s+queries inside InnoDB,\s*(\d+)\s+queries in queue', text)
    if queries_match:
        data.queries_inside_innodb = int(queries_match.group(1))
        data.queries_in_queue = int(queries_match.group(2))
    
    # Read views open
    read_views_match = re.search(r'(\d+)\s+read views open inside InnoDB', text)
    if read_views_match:
        data.read_views_open = int(read_views_match.group(1))
    
    # Main thread state
    main_thread_match = re.search(r'Main thread.*?state:\s*(\w+)', text)
    if main_thread_match:
        data.main_thread_state = main_thread_match.group(1)
    
    # Number of rows inserted, updated, deleted, read
    rows_match = re.search(r'Number of rows inserted\s+(\d+),\s*updated\s+(\d+),\s*deleted\s+(\d+),\s*read\s+(\d+)', text)
    if rows_match:
        data.rows_inserted = int(rows_match.group(1))
        data.rows_updated = int(rows_match.group(2))
        data.rows_deleted = int(rows_match.group(3))
        data.rows_read = int(rows_match.group(4))
    
    # inserts/s, updates/s, deletes/s, reads/s
    rates_match = re.search(r'([\d.]+)\s+inserts/s,\s*([\d.]+)\s+updates/s,\s*([\d.]+)\s+deletes/s,\s*([\d.]+)\s+reads/s', text)
    if rates_match:
        data.inserts_per_sec = float(rates_match.group(1))
        data.updates_per_sec = float(rates_match.group(2))
        data.deletes_per_sec = float(rates_match.group(3))
        data.reads_per_sec_rows = float(rates_match.group(4))


def get_innodb_status_from_parsed_logs(parsed_logs: Dict[str, Any]) -> Optional[InnoDBStatusData]:
    """
    Extract and parse InnoDB status from parsed query logs.
    
    Args:
        parsed_logs: Dictionary mapping query IDs to ParsedLogFile objects
        
    Returns:
        InnoDBStatusData object or None if not available
    """
    if 'innodb_engine_status' not in parsed_logs:
        return None
    
    log_data = parsed_logs['innodb_engine_status']
    if not log_data.success or not log_data.rows or not log_data.columns:
        return None
    
    # SHOW ENGINE INNODB STATUS returns columns: Type, Name, Status
    # The Status column contains the full text output
    # 
    # Due to how the log file parser handles multi-line values, the Status
    # column may be split across multiple rows. We need to reconstruct it.
    # 
    # Expected format in parsed rows:
    # Row 0: ('InnoDB', '', '') or ('InnoDB', '', 'full status text...')
    # Row 1+: Single-column rows containing the rest of the status text
    
    try:
        status_idx = log_data.columns.index('Status')
        
        # First, try to get the status from the first row's Status column
        status_text = log_data.rows[0][status_idx] if len(log_data.rows[0]) > status_idx else None
        
        if status_text:
            # Status is in the first row's Status column (ideal case)
            return parse_innodb_status(str(status_text))
        
        # If Status column is empty, reconstruct from subsequent rows
        # This handles the case where multi-line output was split by the log parser
        if len(log_data.rows) > 1:
            # Skip the first row (header row with Type, Name, empty Status)
            # and concatenate all subsequent rows
            status_lines = []
            for row in log_data.rows[1:]:
                if row:
                    # Each row may have 1 or more columns; take the first non-empty value
                    for val in row:
                        if val is not None and str(val).strip():
                            status_lines.append(str(val))
                            break
            
            if status_lines:
                # Reconstruct the status text
                status_text = '\n'.join(status_lines)
                return parse_innodb_status(status_text)
        
    except (ValueError, IndexError):
        pass
    
    return None
