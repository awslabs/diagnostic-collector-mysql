"""
Tests for the InnoDB Status Parser module.

This module tests the parsing of SHOW ENGINE INNODB STATUS output
and extraction of metrics from various sections.
"""

import pytest
from diagnostic_collector_mysql.parsing.innodb_status_parser import (
    parse_innodb_status,
    InnoDBStatusData,
    get_innodb_status_from_parsed_logs,
    _split_into_sections,
    _parse_deadlock_section,
    _parse_fk_error_section,
    _parse_semaphores_section,
    _parse_transactions_section,
    _parse_log_section,
    _parse_buffer_pool_section,
    _parse_adaptive_hash_section,
    _parse_row_operations_section,
)


class TestInnoDBStatusDataClass:
    """Tests for the InnoDBStatusData dataclass."""
    
    def test_default_values(self):
        """Test that InnoDBStatusData has correct default values."""
        data = InnoDBStatusData()
        
        # Deadlock defaults
        assert data.has_deadlock is False
        assert data.deadlock_timestamp is None
        assert data.deadlock_transactions == []
        assert data.deadlock_victim is None
        
        # FK error defaults
        assert data.has_fk_error is False
        assert data.fk_error_timestamp is None
        
        # Semaphore defaults
        assert data.os_waits_reservation_count == 0
        assert data.rw_shared_spins == 0
        
        # Transaction defaults
        assert data.trx_id_counter == 0
        assert data.history_list_length == 0
        
        # Log defaults
        assert data.log_sequence_number == 0
        assert data.checkpoint_lag == 0
        
        # Buffer pool defaults
        assert data.buffer_pool_hit_rate == 0
        
        # Row operations defaults
        assert data.rows_inserted == 0


class TestSplitIntoSections:
    """Tests for the _split_into_sections function."""
    
    def test_split_basic_sections(self):
        """Test splitting basic InnoDB status output into sections."""
        status_text = """
=====================================
2026-03-27 22:00:00 INNODB MONITOR OUTPUT
=====================================
----------
SEMAPHORES
----------
OS WAIT ARRAY INFO: reservation count 100
----------
TRANSACTIONS
----------
Trx id counter 12345
----------------------------
END OF INNODB MONITOR OUTPUT
============================
"""
        sections = _split_into_sections(status_text)
        
        assert 'SEMAPHORES' in sections
        assert 'TRANSACTIONS' in sections
        assert 'OS WAIT ARRAY INFO' in sections['SEMAPHORES']
        assert 'Trx id counter' in sections['TRANSACTIONS']
    
    def test_empty_input(self):
        """Test handling of empty input."""
        sections = _split_into_sections("")
        assert sections == {}
    
    def test_no_sections(self):
        """Test handling of input with no recognizable sections."""
        sections = _split_into_sections("Some random text without sections")
        assert sections == {}


class TestParseDeadlockSection:
    """Tests for deadlock section parsing."""
    
    def test_parse_deadlock_with_victim(self):
        """Test parsing a deadlock section with victim information."""
        deadlock_text = """
2026-03-27 21:55:00 0x7f1234567890
*** (1) TRANSACTION:
TRANSACTION 123456, ACTIVE 5 sec starting index read
mysql tables in use 1, locked 1
MySQL thread id 100, OS thread handle 0x7f1234567890, query id 1000 localhost test updating
UPDATE users SET name = 'test' WHERE id = 1
*** (2) TRANSACTION:
TRANSACTION 123457, ACTIVE 3 sec starting index read
mysql tables in use 1, locked 1
MySQL thread id 101, OS thread handle 0x7f1234567891, query id 1001 localhost test updating
UPDATE users SET name = 'test2' WHERE id = 2
*** WE ROLL BACK TRANSACTION (1)
"""
        data = InnoDBStatusData()
        _parse_deadlock_section(deadlock_text, data)
        
        assert data.has_deadlock is True
        assert data.deadlock_timestamp == "2026-03-27 21:55:00"
        assert data.deadlock_victim == "1"
        assert len(data.deadlock_transactions) == 2
    
    def test_parse_deadlock_extracts_transaction_numbers(self):
        """Test that transaction numbers are correctly extracted."""
        deadlock_text = """
2026-03-27 21:55:00
*** (1) TRANSACTION:
TRANSACTION 111, ACTIVE 5 sec
*** (2) TRANSACTION:
TRANSACTION 222, ACTIVE 3 sec
*** WE ROLL BACK TRANSACTION (2)
"""
        data = InnoDBStatusData()
        _parse_deadlock_section(deadlock_text, data)
        
        assert data.deadlock_victim == "2"
        assert any(t.get('number') == '1' for t in data.deadlock_transactions)
        assert any(t.get('number') == '2' for t in data.deadlock_transactions)


class TestParseFKErrorSection:
    """Tests for foreign key error section parsing."""
    
    def test_parse_fk_error_basic(self):
        """Test parsing a basic FK error section."""
        fk_text = """
2026-03-27 20:00:00 Transaction:
Foreign key constraint fails for table `test`.`child`
CONSTRAINT `fk_parent` FOREIGN KEY (`parent_id`) REFERENCES `parent` (`id`)
INSERT INTO child (parent_id) VALUES (999)
"""
        data = InnoDBStatusData()
        _parse_fk_error_section(fk_text, data)
        
        assert data.has_fk_error is True
        assert data.fk_error_timestamp == "2026-03-27 20:00:00"
        assert data.fk_error_constraint == "fk_parent"
    
    def test_parse_fk_error_extracts_table(self):
        """Test that table name is extracted from FK error."""
        fk_text = """
2026-03-27 20:00:00
Foreign key constraint fails for table `mydb`.`orders`
"""
        data = InnoDBStatusData()
        _parse_fk_error_section(fk_text, data)
        
        assert data.has_fk_error is True
        assert "`mydb`.`orders`" in data.fk_error_table or "orders" in str(data.fk_error_table)


class TestParseSemaphoresSection:
    """Tests for semaphores section parsing."""
    
    def test_parse_semaphores_basic(self):
        """Test parsing basic semaphore metrics."""
        sem_text = """
OS WAIT ARRAY INFO: reservation count 12345
OS WAIT ARRAY INFO: signal count 11000
RW-shared spins 1000, rounds 2000, OS waits 500
RW-excl spins 800, rounds 1600, OS waits 400
RW-sx spins 200, rounds 400, OS waits 100
Spin rounds per wait: 2.00 RW-shared, 2.00 RW-excl, 2.00 RW-sx
"""
        data = InnoDBStatusData()
        _parse_semaphores_section(sem_text, data)
        
        assert data.os_waits_reservation_count == 12345
        assert data.os_waits_signal_count == 11000
        assert data.rw_shared_spins == 1000
        assert data.rw_shared_rounds == 2000
        assert data.rw_shared_os_waits == 500
        assert data.rw_excl_spins == 800
        assert data.rw_sx_spins == 200
        assert data.spin_rounds_per_wait_shared == 2.00
        assert data.spin_rounds_per_wait_excl == 2.00
        assert data.spin_rounds_per_wait_sx == 2.00
    
    def test_parse_semaphores_with_current_waits(self):
        """Test parsing semaphores with threads waiting."""
        sem_text = """
OS WAIT ARRAY INFO: reservation count 100
--Thread 12345 has waited at lock0lock.cc line 1234 for 5.00 seconds
--Thread 12346 has waited at buf0buf.cc line 5678 for 2.00 seconds
"""
        data = InnoDBStatusData()
        _parse_semaphores_section(sem_text, data)
        
        assert len(data.current_waits) == 2


class TestParseTransactionsSection:
    """Tests for transactions section parsing."""
    
    def test_parse_transactions_basic(self):
        """Test parsing basic transaction metrics."""
        trx_text = """
Trx id counter 123456789
Purge done for trx's n:o < 123456700 undo n:o < 0 state: running but idle
History list length 500
"""
        data = InnoDBStatusData()
        _parse_transactions_section(trx_text, data)
        
        assert data.trx_id_counter == 123456789
        assert data.purge_done_for_trx == 123456700
        assert data.purge_undo_no == 0
        assert data.purge_state == "running but idle"
        assert data.history_list_length == 500
    
    def test_parse_transactions_running_state(self):
        """Test parsing transactions with running purge state."""
        trx_text = """
Trx id counter 999999
Purge done for trx's n:o < 999000 undo n:o < 5 state: running
History list length 10000
"""
        data = InnoDBStatusData()
        _parse_transactions_section(trx_text, data)
        
        assert data.purge_state == "running"
        assert data.history_list_length == 10000


class TestParseLogSection:
    """Tests for log section parsing."""
    
    def test_parse_log_basic(self):
        """Test parsing basic log metrics."""
        log_text = """
Log sequence number 1234567890
Log flushed up to   1234567800
Pages flushed up to 1234567700
Last checkpoint at  1234567600
0 pending log flushes, 0 pending chkp writes
100 log i/o's done, 1.50 log i/o's/second
"""
        data = InnoDBStatusData()
        _parse_log_section(log_text, data)
        
        assert data.log_sequence_number == 1234567890
        assert data.log_flushed_up_to == 1234567800
        assert data.pages_flushed_up_to == 1234567700
        assert data.last_checkpoint_at == 1234567600
        assert data.pending_log_flushes == 0
        assert data.pending_chkp_writes == 0
        assert data.log_ios_done == 100
        assert data.log_ios_per_second == 1.50
    
    def test_parse_log_with_pending_writes(self):
        """Test parsing log with pending writes."""
        log_text = """
Log sequence number 9999999999
5 pending log flushes, 3 pending chkp writes
"""
        data = InnoDBStatusData()
        _parse_log_section(log_text, data)
        
        assert data.log_sequence_number == 9999999999
        assert data.pending_log_flushes == 5
        assert data.pending_chkp_writes == 3


class TestParseBufferPoolSection:
    """Tests for buffer pool section parsing."""
    
    def test_parse_buffer_pool_basic(self):
        """Test parsing basic buffer pool metrics."""
        bp_text = """
Total large memory allocated 1073741824
Buffer pool size   65536
Free buffers       1000
Database pages     64000
Old database pages 23000
Modified db pages  100
Pending reads      0
Pending writes: LRU 0, flush list 0, single page 0
Pages made young 10000, not young 5000
0.00 youngs/s, 0.00 non-youngs/s
Pages read 50000, created 10000, written 20000
0.00 reads/s, 0.00 creates/s, 0.00 writes/s
Buffer pool hit rate 999 / 1000
young-making rate 10 / 1000 not 5 / 1000
Pages read ahead 0.00/s, evicted without access 0.00/s
LRU len: 64000
"""
        data = InnoDBStatusData()
        _parse_buffer_pool_section(bp_text, data)
        
        assert data.total_memory_allocated == 1073741824
        assert data.buffer_pool_size == 65536
        assert data.free_buffers == 1000
        assert data.database_pages == 64000
        assert data.old_database_pages == 23000
        assert data.modified_db_pages == 100
        assert data.pages_made_young == 10000
        assert data.pages_made_not_young == 5000
        assert data.pages_read == 50000
        assert data.pages_created == 10000
        assert data.pages_written == 20000
        assert data.buffer_pool_hit_rate == 999
        assert data.buffer_pool_hit_rate_denominator == 1000
        assert data.lru_len == 64000
    
    def test_parse_buffer_pool_hit_rate_variations(self):
        """Test parsing different buffer pool hit rate formats."""
        bp_text = """
Buffer pool hit rate 950 / 1000
"""
        data = InnoDBStatusData()
        _parse_buffer_pool_section(bp_text, data)
        
        assert data.buffer_pool_hit_rate == 950
        assert data.buffer_pool_hit_rate_denominator == 1000


class TestParseAdaptiveHashSection:
    """Tests for adaptive hash index section parsing."""
    
    def test_parse_adaptive_hash_basic(self):
        """Test parsing basic adaptive hash metrics."""
        ahi_text = """
Ibuf: size 1, free list len 0, seg size 2, 0 merges
merged operations:
 insert 0, delete mark 0, delete 0
discarded operations:
 insert 0, delete mark 0, delete 0
Hash table size 26041, node heap has 0 buffer(s)
100.50 hash searches/s, 50.25 non-hash searches/s
"""
        data = InnoDBStatusData()
        _parse_adaptive_hash_section(ahi_text, data)
        
        assert data.hash_table_size == 26041
        assert data.hash_searches_per_sec == 100.50
        assert data.non_hash_searches_per_sec == 50.25
    
    def test_parse_adaptive_hash_zero_searches(self):
        """Test parsing adaptive hash with zero searches."""
        ahi_text = """
0.00 hash searches/s, 0.00 non-hash searches/s
Hash table size 1000
"""
        data = InnoDBStatusData()
        _parse_adaptive_hash_section(ahi_text, data)
        
        assert data.hash_searches_per_sec == 0.0
        assert data.non_hash_searches_per_sec == 0.0


class TestParseRowOperationsSection:
    """Tests for row operations section parsing."""
    
    def test_parse_row_operations_basic(self):
        """Test parsing basic row operations metrics."""
        row_text = """
0 queries inside InnoDB, 0 queries in queue
0 read views open inside InnoDB
Main thread process no. 1234, id 0x7f1234567890, state: sleeping
Number of rows inserted 100000, updated 50000, deleted 10000, read 500000
10.00 inserts/s, 5.00 updates/s, 1.00 deletes/s, 100.00 reads/s
"""
        data = InnoDBStatusData()
        _parse_row_operations_section(row_text, data)
        
        assert data.queries_inside_innodb == 0
        assert data.queries_in_queue == 0
        assert data.read_views_open == 0
        assert data.main_thread_state == "sleeping"
        assert data.rows_inserted == 100000
        assert data.rows_updated == 50000
        assert data.rows_deleted == 10000
        assert data.rows_read == 500000
        assert data.inserts_per_sec == 10.00
        assert data.updates_per_sec == 5.00
        assert data.deletes_per_sec == 1.00
        assert data.reads_per_sec_rows == 100.00
    
    def test_parse_row_operations_with_queue(self):
        """Test parsing row operations with queries in queue."""
        row_text = """
5 queries inside InnoDB, 10 queries in queue
3 read views open inside InnoDB
"""
        data = InnoDBStatusData()
        _parse_row_operations_section(row_text, data)
        
        assert data.queries_inside_innodb == 5
        assert data.queries_in_queue == 10
        assert data.read_views_open == 3


class TestParseInnoDBStatus:
    """Tests for the main parse_innodb_status function."""
    
    def test_parse_complete_status(self):
        """Test parsing a complete InnoDB status output."""
        status_text = """
=====================================
2026-03-27 22:00:00 0x7f1234567890 INNODB MONITOR OUTPUT
=====================================
Per second averages calculated from the last 30 seconds
----------
SEMAPHORES
----------
OS WAIT ARRAY INFO: reservation count 12345
OS WAIT ARRAY INFO: signal count 11000
RW-shared spins 1000, rounds 2000, OS waits 500
RW-excl spins 800, rounds 1600, OS waits 400
RW-sx spins 200, rounds 400, OS waits 100
Spin rounds per wait: 2.00 RW-shared, 2.00 RW-excl, 2.00 RW-sx
------------
TRANSACTIONS
------------
Trx id counter 123456789
Purge done for trx's n:o < 123456700 undo n:o < 0 state: running but idle
History list length 500
---
LOG
---
Log sequence number 1234567890
Log flushed up to   1234567800
Pages flushed up to 1234567700
Last checkpoint at  1234567600
0 pending log flushes, 0 pending chkp writes
100 log i/o's done, 1.00 log i/o's/second
----------------------
BUFFER POOL AND MEMORY
----------------------
Total large memory allocated 1073741824
Buffer pool size   65536
Free buffers       1000
Database pages     64000
Old database pages 23000
Modified db pages  100
Pending reads      0
Pending writes: LRU 0, flush list 0, single page 0
Pages made young 10000, not young 5000
0.00 youngs/s, 0.00 non-youngs/s
Pages read 50000, created 10000, written 20000
0.00 reads/s, 0.00 creates/s, 0.00 writes/s
Buffer pool hit rate 999 / 1000
young-making rate 10 / 1000 not 5 / 1000
Pages read ahead 0.00/s, evicted without access 0.00/s
LRU len: 64000
-------------------------------------
INSERT BUFFER AND ADAPTIVE HASH INDEX
-------------------------------------
0.00 hash searches/s, 0.00 non-hash searches/s
Hash table size 26041
---
ROW OPERATIONS
---
0 queries inside InnoDB, 0 queries in queue
0 read views open inside InnoDB
Main thread process no. 1234, id 0x7f1234567890, state: sleeping
Number of rows inserted 100000, updated 50000, deleted 10000, read 500000
0.00 inserts/s, 0.00 updates/s, 0.00 deletes/s, 0.00 reads/s
----------------------------
END OF INNODB MONITOR OUTPUT
============================
"""
        data = parse_innodb_status(status_text)
        
        # Verify semaphores
        assert data.os_waits_reservation_count == 12345
        assert data.rw_shared_spins == 1000
        
        # Verify transactions
        assert data.trx_id_counter == 123456789
        assert data.purge_state == "running but idle"
        assert data.history_list_length == 500
        
        # Verify derived metrics
        assert data.purge_lag == 123456789 - 123456700  # 89
        assert data.checkpoint_lag == 1234567890 - 1234567600  # 290
        assert data.unflushed_log == 1234567890 - 1234567800  # 90
        
        # Verify buffer pool
        assert data.buffer_pool_hit_rate == 999
        
        # Verify row operations
        assert data.rows_inserted == 100000
    
    def test_parse_empty_status(self):
        """Test parsing empty status returns default values."""
        data = parse_innodb_status("")
        
        assert data.has_deadlock is False
        assert data.has_fk_error is False
        assert data.trx_id_counter == 0
    
    def test_parse_status_with_deadlock(self):
        """Test parsing status with deadlock section."""
        status_text = """
=====================================
2026-03-27 22:00:00 INNODB MONITOR OUTPUT
=====================================
------------------------
LATEST DETECTED DEADLOCK
------------------------
2026-03-27 21:55:00 0x7f1234567890
*** (1) TRANSACTION:
TRANSACTION 123456, ACTIVE 5 sec
*** (2) TRANSACTION:
TRANSACTION 123457, ACTIVE 3 sec
*** WE ROLL BACK TRANSACTION (1)
----------------------------
END OF INNODB MONITOR OUTPUT
============================
"""
        data = parse_innodb_status(status_text)
        
        assert data.has_deadlock is True
        assert data.deadlock_victim == "1"


class TestGetInnoDBStatusFromParsedLogs:
    """Tests for the get_innodb_status_from_parsed_logs function."""
    
    def test_get_status_from_valid_logs(self):
        """Test extracting status from valid parsed logs."""
        from diagnostic_collector_mysql.reporting.report_generator import ParsedLogFile
        
        status_text = """
=====================================
2026-03-27 22:00:00 INNODB MONITOR OUTPUT
=====================================
------------
TRANSACTIONS
------------
Trx id counter 999
History list length 100
----------------------------
END OF INNODB MONITOR OUTPUT
============================
"""
        parsed_logs = {
            'innodb_engine_status': ParsedLogFile(
                query_id='innodb_engine_status',
                success=True,
                columns=['Type', 'Name', 'Status'],
                rows=[['InnoDB', '', status_text]],
                error=None,
                executed_timestamp='2026-03-27 22:00:00'
            )
        }
        
        data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        assert data is not None
        assert data.history_list_length == 100
    
    def test_get_status_missing_query(self):
        """Test handling of missing innodb_engine_status query."""
        parsed_logs = {}
        
        data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        assert data is None
    
    def test_get_status_failed_query(self):
        """Test handling of failed innodb_engine_status query."""
        from diagnostic_collector_mysql.reporting.report_generator import ParsedLogFile
        
        parsed_logs = {
            'innodb_engine_status': ParsedLogFile(
                query_id='innodb_engine_status',
                success=False,
                columns=None,
                rows=None,
                error='Access denied',
                executed_timestamp='2026-03-27 22:00:00'
            )
        }
        
        data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        assert data is None
    
    def test_get_status_empty_rows(self):
        """Test handling of empty rows in innodb_engine_status query."""
        from diagnostic_collector_mysql.reporting.report_generator import ParsedLogFile
        
        parsed_logs = {
            'innodb_engine_status': ParsedLogFile(
                query_id='innodb_engine_status',
                success=True,
                columns=['Type', 'Name', 'Status'],
                rows=[],
                error=None,
                executed_timestamp='2026-03-27 22:00:00'
            )
        }
        
        data = get_innodb_status_from_parsed_logs(parsed_logs)
        
        assert data is None


class TestDerivedMetrics:
    """Tests for derived metrics calculations."""
    
    def test_purge_lag_calculation(self):
        """Test that purge lag is correctly calculated."""
        status_text = """
------------
TRANSACTIONS
------------
Trx id counter 1000
Purge done for trx's n:o < 900 undo n:o < 0 state: running
"""
        data = parse_innodb_status(status_text)
        
        assert data.purge_lag == 100  # 1000 - 900
    
    def test_checkpoint_lag_calculation(self):
        """Test that checkpoint lag is correctly calculated."""
        status_text = """
---
LOG
---
Log sequence number 10000
Last checkpoint at  9000
"""
        data = parse_innodb_status(status_text)
        
        assert data.checkpoint_lag == 1000  # 10000 - 9000
    
    def test_unflushed_log_calculation(self):
        """Test that unflushed log is correctly calculated."""
        status_text = """
---
LOG
---
Log sequence number 10000
Log flushed up to   9500
"""
        data = parse_innodb_status(status_text)
        
        assert data.unflushed_log == 500  # 10000 - 9500
    
    def test_zero_values_no_negative_lag(self):
        """Test that zero values don't produce negative lag."""
        data = parse_innodb_status("")
        
        assert data.purge_lag == 0
        assert data.checkpoint_lag == 0
        assert data.unflushed_log == 0
