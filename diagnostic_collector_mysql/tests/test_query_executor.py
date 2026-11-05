"""
Unit tests for QueryExecutor back-off threshold checks and query filtering.

Tests the back-off mechanism that protects the database from additional
load when it's already under stress, and query filtering by engine type
and sections.

Covers:
- Check current active connection count from processlist
- Exit with code 5 when active connections exceed threshold
- Check total table count in the instance
- Exit with code 5 when table count exceeds threshold
- Display clear message explaining reason, current value, threshold
- Log all back-off events to main log file
- Do NOT retry or wait when back-off threshold is exceeded
- When --replication-check is specified, execute only queries in the replication section
- When --performance-check is specified, execute only queries in the performance section
- When multiple section-specific check flags are specified, execute queries for all specified sections (OR logic)
- When no section-specific check flag is specified, execute all applicable queries
- Filter queries based on the detected database engine type
- Support queries that apply to all engine types (using engine: "all")
- Execute each query from queries.json exactly once per run
- When --query-timeout is specified, cancel queries that exceed the timeout duration
- When --query-timeout is not specified, use a default timeout of 30 seconds
- When a query fails, log the error and continue with remaining queries
- Capture query results including column names and row data
- Execute queries without row limits and store full results for logging
"""

import logging
import os
import unittest
from unittest.mock import MagicMock, patch, call

import mysql.connector

from diagnostic_collector_mysql.models import BackOffError, EngineType, QueryDefinition
from diagnostic_collector_mysql.core.query_executor import QueryExecutor, format_backoff_message


def _conn_row(active, threads_connected=10, max_connections=1000, pfs=1):
    """
    Build the row the connection back-off check reads.

    The check reads performance_schema enabled, max_connections,
    Threads_connected and the active connection count in a single query, so a
    test has to supply all four. The defaults leave slot usage at 1%, far under
    the default threshold, so tests concerned only with concurrency are
    unaffected by the slot guard.
    """
    return (pfs, max_connections, threads_connected, active)




class TestQueryExecutorBackoffThresholds(unittest.TestCase):
    """Test cases for QueryExecutor back-off threshold checks."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.mock_connection = MagicMock()
        self.mock_cursor = MagicMock()
        self.mock_connection.cursor.return_value = self.mock_cursor
        self.test_logger = logging.getLogger('test_query_executor')
        self.test_logger.setLevel(logging.DEBUG)
        
        # Default thresholds
        self.connection_threshold = 1000
        self.objects_threshold = 10000
    
    def _create_executor(
        self,
        connection_threshold: int = None,
        objects_threshold: int = None
    ) -> QueryExecutor:
        """Create a QueryExecutor instance with test configuration."""
        return QueryExecutor(
            connection=self.mock_connection,
            output_dir='/tmp/test_output',
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=connection_threshold or self.connection_threshold,
            objects_threshold=objects_threshold or self.objects_threshold,
            logger=self.test_logger
        )
    
    # =========================================================================
    # Connection Count Threshold Tests
    # =========================================================================
    
    def test_connection_count_below_threshold_passes(self):
        """
        Test that connection count below threshold allows execution to proceed.
        
        Check active connection count from processlist
        """
        # Setup: 500 active connections, threshold is 1000
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(500),  # Connection count query
            (100,),  # Table count query
        ]
        
        executor = self._create_executor()
        
        # Should not raise any exception
        executor.check_backoff_thresholds()
        
        # Verify processlist was queried
        calls = self.mock_cursor.execute.call_args_list
        self.assertTrue(
            any('processlist' in str(c).lower() for c in calls),
            "Should query processlist for connection count"
        )
    
    def test_connection_count_at_threshold_passes(self):
        """
        Test that connection count exactly at threshold allows execution.
        
        Only exceeding threshold triggers back-off
        """
        # Setup: 1000 active connections, threshold is 1000 (at threshold, not exceeding)
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(1000),  # Connection count query - exactly at threshold
            (100,),   # Table count query
        ]
        
        executor = self._create_executor(connection_threshold=1000)
        
        # Should not raise - threshold is "exceeds", not "equals or exceeds"
        executor.check_backoff_thresholds()
    
    def test_connection_count_exceeds_threshold_raises_backoff_error(self):
        """
        Test that connection count exceeding threshold raises BackOffError.
        
        Exit with code 5 when connections exceed threshold
        """
        # Setup: 1500 active connections, threshold is 1000
        self.mock_cursor.fetchone.return_value = _conn_row(1500)
        
        executor = self._create_executor(connection_threshold=1000)
        
        with self.assertRaises(BackOffError) as context:
            executor.check_backoff_thresholds()
        
        error = context.exception
        self.assertEqual(error.current_value, 1500)
        self.assertEqual(error.threshold, 1000)
        self.assertIn("connection", error.reason.lower())
    
    def test_connection_count_custom_threshold(self):
        """
        Test that custom connection threshold is respected.
        
        Accept --connection-back-off argument
        """
        # Setup: 150 active connections, custom threshold of 100
        self.mock_cursor.fetchone.return_value = _conn_row(150)
        
        executor = self._create_executor(connection_threshold=100)
        
        with self.assertRaises(BackOffError) as context:
            executor.check_backoff_thresholds()
        
        error = context.exception
        self.assertEqual(error.threshold, 100)
        self.assertEqual(error.current_value, 150)
    
    # =========================================================================
    # Table Count Threshold Tests
    # =========================================================================
    
    def test_table_count_below_threshold_passes(self):
        """
        Test that table count below threshold allows execution to proceed.
        
        Check total table count in the instance
        """
        # Setup: 500 connections, 5000 tables (both below thresholds)
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(500),   # Connection count query
            (5000,),  # Table count query
        ]
        
        executor = self._create_executor()
        
        # Should not raise any exception
        executor.check_backoff_thresholds()
        
        # Verify information_schema.tables was queried
        calls = self.mock_cursor.execute.call_args_list
        self.assertTrue(
            any('information_schema.tables' in str(c).lower() for c in calls),
            "Should query information_schema.tables for table count"
        )
    
    def test_table_count_at_threshold_passes(self):
        """
        Test that table count exactly at threshold allows execution.
        
        Only exceeding threshold triggers back-off
        """
        # Setup: 500 connections, 10000 tables (at threshold, not exceeding)
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(500),    # Connection count query
            (10000,),  # Table count query - exactly at threshold
        ]
        
        executor = self._create_executor(objects_threshold=10000)
        
        # Should not raise - threshold is "exceeds", not "equals or exceeds"
        executor.check_backoff_thresholds()
    
    def test_table_count_exceeds_threshold_sets_skip_flag(self):
        """
        Test that table count exceeding threshold sets skip flag instead of raising error.
        
        Validates: Objects back-off should skip objects section and warn, not exit
        """
        # Setup: 500 connections (OK), 15000 tables (exceeds threshold)
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(500),    # Connection count query - OK
            (15000,),  # Table count query - exceeds threshold
        ]
        
        executor = self._create_executor(objects_threshold=10000)
        
        # Should NOT raise - should set skip flag instead
        executor.check_backoff_thresholds()
        
        # Verify skip flag is set
        self.assertTrue(executor.skip_objects_section)
        self.assertIsNotNone(executor.objects_backoff_warning)
        self.assertIn("15000", executor.objects_backoff_warning)
        self.assertIn("10000", executor.objects_backoff_warning)
    
    def test_table_count_custom_threshold_sets_skip_flag(self):
        """
        Test that custom table threshold is respected and sets skip flag.
        
        Accept --objects-back-off argument
        """
        # Setup: 500 connections (OK), 600 tables (exceeds custom threshold of 500)
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(100),  # Connection count query - OK
            (600,),  # Table count query - exceeds custom threshold
        ]
        
        executor = self._create_executor(objects_threshold=500)
        
        # Should NOT raise - should set skip flag instead
        executor.check_backoff_thresholds()
        
        # Verify skip flag is set with correct values in warning
        self.assertTrue(executor.skip_objects_section)
        self.assertIn("500", executor.objects_backoff_warning)
        self.assertIn("600", executor.objects_backoff_warning)
    
    # =========================================================================
    # Check Order Tests (Connection first, then tables)
    # =========================================================================
    
    def test_connection_check_runs_before_table_check(self):
        """
        Test that connection count is checked before table count.
        
        If connection count exceeds threshold, table count should not be checked.
        """
        # Setup: Both would exceed thresholds
        self.mock_cursor.fetchone.return_value = _conn_row(2000)  # Exceeds connection threshold
        
        executor = self._create_executor(
            connection_threshold=1000,
            objects_threshold=10000
        )
        
        with self.assertRaises(BackOffError) as context:
            executor.check_backoff_thresholds()
        
        # Should fail on connection count, not table count
        error = context.exception
        self.assertIn("connection", error.reason.lower())
        
        # Table count query should not have been executed
        # (only one execute call for connection count)
        self.assertEqual(self.mock_cursor.execute.call_count, 1)
    
    # =========================================================================
    # Error Message Format Tests
    # =========================================================================
    
    def test_backoff_message_for_connection_threshold(self):
        """
        Test that back-off message for connection threshold is properly formatted.
        
        Display clear message with reason, value, threshold
        """
        error = BackOffError(
            reason="Active connection count exceeds threshold",
            current_value=1500,
            threshold=1000
        )
        
        message = format_backoff_message(error)
        
        # Check required components
        self.assertIn("1500", message)  # Current value
        self.assertIn("1000", message)  # Threshold
        self.assertIn("--connection-back-off", message)  # How to adjust
        self.assertIn("Active connection count exceeds threshold", message)  # Reason
    
    def test_backoff_message_for_table_threshold(self):
        """
        Test that back-off message for table threshold is properly formatted.
        
        Display clear message with reason, value, threshold
        """
        error = BackOffError(
            reason="Table count exceeds threshold",
            current_value=15000,
            threshold=10000
        )
        
        message = format_backoff_message(error)
        
        # Check required components
        self.assertIn("15000", message)  # Current value
        self.assertIn("10000", message)  # Threshold
        self.assertIn("--objects-back-off", message)  # How to adjust
        self.assertIn("Table count exceeds threshold", message)  # Reason
    
    def test_backoff_message_includes_adjustment_instructions(self):
        """
        Test that back-off message includes instructions on how to adjust.
        
        Include instructions on how to adjust back-off settings
        """
        error = BackOffError(
            reason="Active connection count exceeds threshold",
            current_value=1500,
            threshold=1000
        )
        
        message = format_backoff_message(error)
        
        # Should include example command
        self.assertIn("diagnostic_collector_mysql", message)
        self.assertIn("--connection-back-off", message)
        # Should suggest a value higher than current
        self.assertIn("1600", message)  # current_value + 100
    
    # =========================================================================
    # Single Check Tests
    # =========================================================================
    
    def test_single_connection_check_only(self):
        """
        Test that connection count is checked exactly once (no retries).
        
        Do NOT retry when back-off threshold exceeded
        """
        # Setup: Connection count exceeds threshold
        self.mock_cursor.fetchone.return_value = _conn_row(2000)
        
        executor = self._create_executor(connection_threshold=1000)
        
        with self.assertRaises(BackOffError):
            executor.check_backoff_thresholds()
        
        # Should only execute one query (the connection count check)
        self.assertEqual(self.mock_cursor.execute.call_count, 1)
    
    def test_single_table_check_only(self):
        """
        Test that table count is checked exactly once (no retries).
        
        Do NOT retry when back-off threshold exceeded
        """
        # Setup: Connection OK, table count exceeds threshold
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(500),    # Connection count - OK
            (15000,),  # Table count - exceeds threshold
        ]
        
        executor = self._create_executor(objects_threshold=10000)
        
        # Should NOT raise - sets skip flag instead
        executor.check_backoff_thresholds()
        
        # Should execute exactly two queries (connection + table count)
        self.assertEqual(self.mock_cursor.execute.call_count, 2)
        # Verify skip flag is set
        self.assertTrue(executor.skip_objects_section)
    
    # =========================================================================
    # Error Handling Tests
    # =========================================================================
    
    def test_connection_check_error_allows_continuation(self):
        """
        Test that errors during connection check allow execution to continue.
        
        If we can't check the connection count (e.g., permission denied),
        we should log a warning and proceed rather than failing.
        
        A failure of the performance_schema statement alone is not enough to reach
        this path any more: it degrades to information_schema.processlist first, so
        an account that can read the processlist keeps the concurrency guard. Both
        reads have to be refused before protection is genuinely gone.
        """
        import mysql.connector
        
        # Setup: both the combined check and the degraded read are refused, then
        # the table check succeeds.
        self.mock_cursor.execute.side_effect = [
            mysql.connector.Error("Access denied"),  # combined check
            mysql.connector.Error("Access denied"),  # degraded read
            None,                                   # table check
        ]
        self.mock_cursor.fetchone.return_value = (100,)  # table count OK
        
        executor = self._create_executor()
        
        # Should not raise
        executor.check_backoff_thresholds()
    
    def test_table_check_error_allows_continuation(self):
        """
        Test that errors during table check allow execution to continue.
        
        If we can't check the table count (e.g., permission denied),
        we should log a warning and proceed rather than failing.
        """
        import mysql.connector
        
        # Setup: Connection check OK, table check fails
        call_count = [0]
        
        def execute_side_effect(query):
            call_count[0] += 1
            if call_count[0] == 2:  # Table check
                raise mysql.connector.Error("Access denied")
        
        self.mock_cursor.execute.side_effect = execute_side_effect
        self.mock_cursor.fetchone.return_value = _conn_row(100)  # Connection count OK
        
        executor = self._create_executor()
        
        # Should not raise - should log warning and continue
        executor.check_backoff_thresholds()


class TestBackOffError(unittest.TestCase):
    """Test cases for BackOffError exception."""
    
    def test_backoff_error_attributes(self):
        """Test that BackOffError stores all required attributes."""
        error = BackOffError(
            reason="Test reason",
            current_value=100,
            threshold=50
        )
        
        self.assertEqual(error.reason, "Test reason")
        self.assertEqual(error.current_value, 100)
        self.assertEqual(error.threshold, 50)
    
    def test_backoff_error_message(self):
        """Test that BackOffError has a descriptive message."""
        error = BackOffError(
            reason="Connection count exceeded",
            current_value=1500,
            threshold=1000
        )
        
        message = str(error)
        self.assertIn("1500", message)
        self.assertIn("1000", message)
        self.assertIn("Connection count exceeded", message)


class TestQueryExecutorFilterQueries(unittest.TestCase):
    """
    Test cases for QueryExecutor.filter_queries method.
    
    Tests query filtering by engine type and sections.
    
    Covers:
    - When --replication-check is specified, execute only queries in the replication section
    - When --performance-check is specified, execute only queries in the performance section
    - When multiple section-specific check flags are specified, execute queries for all specified sections (OR logic)
    - When no section-specific check flag is specified, execute all applicable queries
    - Filter queries based on the detected database engine type
    - Support queries that apply to all engine types (using engine: "all")
    """
    
    def setUp(self):
        """Set up test fixtures."""
        self.mock_connection = MagicMock()
        self.test_logger = logging.getLogger('test_query_executor_filter')
        self.test_logger.setLevel(logging.DEBUG)
        
        # Create executor instance
        self.executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir='/tmp/test_output',
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
        
        # Sample queries for testing
        self.sample_queries = [
            QueryDefinition(id="Q1", condition={"engine": ["mysql"]}, section="replication", command="SELECT 1"),
            QueryDefinition(id="Q2", condition={"engine": ["aurora-mysql"]}, section="replication", command="SELECT 2"),
            QueryDefinition(id="Q3", condition={"engine": ["mariadb"]}, section="replication", command="SELECT 3"),
            QueryDefinition(id="Q4", section="replication", command="SELECT 4"),
            QueryDefinition(id="Q5", condition={"engine": ["mysql"]}, section="performance", command="SELECT 5"),
            QueryDefinition(id="Q6", condition={"engine": ["aurora-mysql"]}, section="performance", command="SELECT 6"),
            QueryDefinition(id="Q7", section="performance", command="SELECT 7"),
            QueryDefinition(id="Q8", condition={"engine": ["mysql"]}, section="status", command="SELECT 8"),
            QueryDefinition(id="Q9", section="status", command="SELECT 9"),
            QueryDefinition(id="Q10", condition={"engine": ["mariadb"]}, section="memory", command="SELECT 10"),
        ]
    
    # =========================================================================
    # Engine Type Filtering Tests
    # =========================================================================
    
    def test_filter_by_mysql_engine(self):
        """
        Test filtering queries for MySQL engine type.
        
        Filter queries based on detected database engine type
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # Should include mysql-specific and "all" queries
        result_ids = {q.id for q in result}
        expected_ids = {"Q1", "Q4", "Q5", "Q7", "Q8", "Q9"}
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_filter_by_aurora_mysql_engine(self):
        """
        Test filtering queries for Aurora MySQL engine type.
        
        Filter queries based on detected database engine type
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.AURORA_MYSQL,
            sections=None
        )
        
        # Should include aurora-mysql-specific and "all" queries
        result_ids = {q.id for q in result}
        expected_ids = {"Q2", "Q4", "Q6", "Q7", "Q9"}
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_filter_by_mariadb_engine(self):
        """
        Test filtering queries for MariaDB engine type.
        
        Filter queries based on detected database engine type
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MARIADB,
            sections=None
        )
        
        # Should include mariadb-specific and "all" queries
        result_ids = {q.id for q in result}
        expected_ids = {"Q3", "Q4", "Q7", "Q9", "Q10"}
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_engine_all_matches_any_engine(self):
        """
        Test that queries without condition match any engine type.
        
        Support queries that apply to all engine types
        """
        all_queries = [
            QueryDefinition(id="ALL1", section="status", command="SELECT 1"),
            QueryDefinition(id="ALL2", section="replication", command="SELECT 2"),
        ]
        
        # Test with each engine type
        for engine_type in [EngineType.MYSQL, EngineType.AURORA_MYSQL, EngineType.MARIADB]:
            result = self.executor.filter_queries(all_queries, engine_type, sections=None)
            result_ids = {q.id for q in result}
            self.assertEqual(result_ids, {"ALL1", "ALL2"}, 
                           f"engine='all' queries should match {engine_type}")
    
    # =========================================================================
    # Section Filtering Tests
    # =========================================================================
    
    def test_filter_by_replication_section(self):
        """
        Test filtering queries for replication section only.
        
        When --replication-check is specified, execute only 
                            queries in the replication section
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections={"replication"}
        )
        
        # Should include only replication section queries that match MySQL engine
        result_ids = {q.id for q in result}
        expected_ids = {"Q1", "Q4"}  # mysql + all engine, replication section
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_filter_by_performance_section(self):
        """
        Test filtering queries for performance section only.
        
        When --performance-check is specified, execute only 
                            queries in the performance section
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections={"performance"}
        )
        
        # Should include only performance section queries that match MySQL engine
        result_ids = {q.id for q in result}
        expected_ids = {"Q5", "Q7"}  # mysql + all engine, performance section
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_filter_by_multiple_sections_or_logic(self):
        """
        Test filtering queries for multiple sections uses OR logic.
        
        When multiple section-specific check flags are specified,
                            execute queries for all specified sections (OR logic)
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections={"replication", "performance"}
        )
        
        # Should include queries from both sections that match MySQL engine
        result_ids = {q.id for q in result}
        expected_ids = {"Q1", "Q4", "Q5", "Q7"}  # replication + performance for mysql/all
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_filter_no_sections_returns_all_applicable(self):
        """
        Test that no section filter returns all applicable queries.
        
        When no section-specific check flag is specified,
                            execute all applicable queries
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # Should include all queries that match MySQL engine (mysql + all)
        result_ids = {q.id for q in result}
        expected_ids = {"Q1", "Q4", "Q5", "Q7", "Q8", "Q9"}
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_filter_empty_sections_set_returns_all_applicable(self):
        """
        Test that empty sections set returns all applicable queries.
        
        When no section-specific check flag is specified,
                            execute all applicable queries
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=set()  # Empty set
        )
        
        # Should include all queries that match MySQL engine (mysql + all)
        result_ids = {q.id for q in result}
        expected_ids = {"Q1", "Q4", "Q5", "Q7", "Q8", "Q9"}
        
        self.assertEqual(result_ids, expected_ids)
    
    # =========================================================================
    # Combined Engine and Section Filtering Tests
    # =========================================================================
    
    def test_filter_aurora_mysql_replication_section(self):
        """
        Test combined filtering for Aurora MySQL engine and replication section.
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.AURORA_MYSQL,
            sections={"replication"}
        )
        
        result_ids = {q.id for q in result}
        expected_ids = {"Q2", "Q4"}  # aurora-mysql + all engine, replication section
        
        self.assertEqual(result_ids, expected_ids)
    
    def test_filter_mariadb_multiple_sections(self):
        """
        Test combined filtering for MariaDB engine and multiple sections.
        """
        result = self.executor.filter_queries(
            self.sample_queries,
            EngineType.MARIADB,
            sections={"replication", "memory"}
        )
        
        result_ids = {q.id for q in result}
        expected_ids = {"Q3", "Q4", "Q10"}  # mariadb + all engine, replication + memory
        
        self.assertEqual(result_ids, expected_ids)
    
    # =========================================================================
    # Edge Cases
    # =========================================================================
    
    def test_filter_empty_queries_list(self):
        """Test filtering an empty queries list returns empty list."""
        result = self.executor.filter_queries(
            [],
            EngineType.MYSQL,
            sections={"replication"}
        )
        
        self.assertEqual(result, [])
    
    def test_filter_no_matching_queries(self):
        """Test filtering when no queries match returns empty list."""
        queries = [
            QueryDefinition(id="Q1", condition={"engine": ["aurora-mysql"]}, section="replication", command="SELECT 1"),
        ]
        
        result = self.executor.filter_queries(
            queries,
            EngineType.MARIADB,  # Different engine
            sections={"performance"}  # Different section
        )
        
        self.assertEqual(result, [])
    
    def test_filter_preserves_query_order(self):
        """Test that filtering preserves the original query order."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1"),
            QueryDefinition(id="Q2", section="status", command="SELECT 2"),
            QueryDefinition(id="Q3", section="status", command="SELECT 3"),
        ]
        
        result = self.executor.filter_queries(
            queries,
            EngineType.MYSQL,
            sections=None
        )
        
        result_ids = [q.id for q in result]
        self.assertEqual(result_ids, ["Q1", "Q2", "Q3"])
    
    def test_filter_section_case_sensitive(self):
        """Test that section filtering is case-sensitive."""
        queries = [
            QueryDefinition(id="Q1", section="replication", command="SELECT 1"),
            QueryDefinition(id="Q2", section="Replication", command="SELECT 2"),
            QueryDefinition(id="Q3", section="REPLICATION", command="SELECT 3"),
        ]
        
        result = self.executor.filter_queries(
            queries,
            EngineType.MYSQL,
            sections={"replication"}  # lowercase
        )
        
        result_ids = {q.id for q in result}
        self.assertEqual(result_ids, {"Q1"})  # Only exact match
    
    def test_filter_engine_case_sensitive(self):
        """Test that engine filtering is case-sensitive."""
        queries = [
            QueryDefinition(id="Q1", condition={"engine": ["mysql"]}, section="status", command="SELECT 1"),
            QueryDefinition(id="Q2", condition={"engine": ["MySQL"]}, section="status", command="SELECT 2"),
            QueryDefinition(id="Q3", condition={"engine": ["MYSQL"]}, section="status", command="SELECT 3"),
        ]
        
        result = self.executor.filter_queries(
            queries,
            EngineType.MYSQL,  # value is "mysql" (lowercase)
            sections=None
        )
        
        result_ids = {q.id for q in result}
        self.assertEqual(result_ids, {"Q1"})  # Only exact match
    
    # =========================================================================
    # Objects Section Back-Off Tests
    # =========================================================================
    
    def test_filter_excludes_objects_section_when_backoff_triggered(self):
        """
        Test that objects section queries are excluded when back-off is triggered.
        
        When objects back-off threshold is exceeded, queries in the 'objects'
        section should be filtered out while other sections continue.
        """
        # Set the skip flag to simulate back-off triggered
        self.executor._skip_objects_section = True
        
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1"),
            QueryDefinition(id="Q2", section="objects", command="SELECT 2"),
            QueryDefinition(id="Q3", section="replication", command="SELECT 3"),
            QueryDefinition(id="Q4", section="objects", command="SELECT 4"),
        ]
        
        result = self.executor.filter_queries(
            queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # Objects section queries should be excluded
        result_ids = {q.id for q in result}
        self.assertEqual(result_ids, {"Q1", "Q3"})
        self.assertNotIn("Q2", result_ids)
        self.assertNotIn("Q4", result_ids)
    
    def test_filter_includes_objects_section_when_backoff_not_triggered(self):
        """
        Test that objects section queries are included when back-off is not triggered.
        """
        # Ensure skip flag is False (default)
        self.executor._skip_objects_section = False
        
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1"),
            QueryDefinition(id="Q2", section="objects", command="SELECT 2"),
            QueryDefinition(id="Q3", section="replication", command="SELECT 3"),
        ]
        
        result = self.executor.filter_queries(
            queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # All queries should be included
        result_ids = {q.id for q in result}
        self.assertEqual(result_ids, {"Q1", "Q2", "Q3"})
    
    def test_filter_objects_backoff_with_section_filter(self):
        """
        Test that objects back-off works correctly with section filtering.
        
        Even if 'objects' is in the requested sections, it should be excluded
        when back-off is triggered.
        """
        # Set the skip flag to simulate back-off triggered
        self.executor._skip_objects_section = True
        
        queries = [
            QueryDefinition(id="Q1", section="objects", command="SELECT 1"),
            QueryDefinition(id="Q2", section="status", command="SELECT 2"),
        ]
        
        result = self.executor.filter_queries(
            queries,
            EngineType.MYSQL,
            sections={"objects", "status"}  # Both sections requested
        )
        
        # Objects section should still be excluded due to back-off
        result_ids = {q.id for q in result}
        self.assertEqual(result_ids, {"Q2"})
        self.assertNotIn("Q1", result_ids)


class TestQueryExecutorExecuteQuery(unittest.TestCase):
    """
    Test cases for QueryExecutor.execute_query method.
    
    Tests single query execution with timeout, result capture, and error handling.
    
    Covers:
    - Execute each query from queries.json exactly once per run
    - When --query-timeout is specified, cancel queries that exceed the timeout duration
    - When --query-timeout is not specified, use a default timeout of 30 seconds
    - When a query fails, log the error and continue with remaining queries
    - Capture query results including column names and row data
    - Execute queries without row limits and store full results for logging
    """
    
    def setUp(self):
        """Set up test fixtures."""
        self.mock_connection = MagicMock()
        self.mock_cursor = MagicMock()
        self.mock_connection.cursor.return_value = self.mock_cursor
        self.test_logger = logging.getLogger('test_execute_query')
        self.test_logger.setLevel(logging.DEBUG)
        
        # Create a temporary output directory
        import tempfile
        self.temp_dir = tempfile.mkdtemp()
        
        # Create executor instance
        self.executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    # =========================================================================
    # Successful Query Execution Tests
    # =========================================================================
    
    def test_execute_query_captures_column_names(self):
        """
        Test that execute_query captures column names from query results.
        
        Capture query results including column names
        """
        # Setup mock cursor with column descriptions
        self.mock_cursor.description = [
            ('Variable_name', None, None, None, None, None, None),
            ('Value', None, None, None, None, None, None),
        ]
        self.mock_cursor.fetchall.return_value = [
            ('max_connections', '151'),
            ('wait_timeout', '28800'),
        ]
        
        query = QueryDefinition(
            id="TEST_QUERY",
            section="status",
            command="SHOW GLOBAL STATUS"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertTrue(result.success)
        self.assertEqual(result.columns, ['Variable_name', 'Value'])
    
    def test_execute_query_captures_row_data(self):
        """
        Test that execute_query captures all row data.
        
        Capture query results including row data
        """
        # Setup mock cursor with row data
        self.mock_cursor.description = [
            ('id', None, None, None, None, None, None),
            ('name', None, None, None, None, None, None),
        ]
        self.mock_cursor.fetchall.return_value = [
            (1, 'test1'),
            (2, 'test2'),
            (3, 'test3'),
        ]
        
        query = QueryDefinition(
            id="TEST_QUERY",
            section="status",
            command="SELECT id, name FROM test"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertTrue(result.success)
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.rows[0], (1, 'test1'))
        self.assertEqual(result.rows[1], (2, 'test2'))
        self.assertEqual(result.rows[2], (3, 'test3'))
    
    def test_execute_query_no_row_limit(self):
        """
        Test that execute_query captures all rows without limit.
        
        Execute queries without row limits
        """
        # Setup mock cursor with many rows
        self.mock_cursor.description = [('id', None, None, None, None, None, None)]
        # Simulate 1000 rows - should all be captured
        self.mock_cursor.fetchall.return_value = [(i,) for i in range(1000)]
        
        query = QueryDefinition(
            id="LARGE_QUERY",
            section="status",
            command="SELECT id FROM large_table"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertTrue(result.success)
        self.assertEqual(len(result.rows), 1000)
    
    def test_execute_query_creates_log_file(self):
        """
        Test that execute_query creates a consolidated log file for queries.
        
        Write output to consolidated queries-output.log file
        """
        import os
        
        self.mock_cursor.description = [('col1', None, None, None, None, None, None)]
        self.mock_cursor.fetchall.return_value = [('value1',)]
        
        query = QueryDefinition(
            id="LOG_TEST",
            section="status",
            command="SELECT 1"
        )
        
        result = self.executor.execute_query(query)
        
        # Implementation uses consolidated log file
        expected_log_file = os.path.join(self.temp_dir, "queries-output.log")
        self.assertEqual(result.log_file, expected_log_file)
        self.assertTrue(os.path.exists(expected_log_file))
    
    def test_execute_query_log_file_contains_results(self):
        """
        Test that the log file contains query results.
        
        Log file SHALL contain query command followed by output
        """
        self.mock_cursor.description = [
            ('Variable_name', None, None, None, None, None, None),
            ('Value', None, None, None, None, None, None),
        ]
        self.mock_cursor.fetchall.return_value = [
            ('max_connections', '151'),
        ]
        
        query = QueryDefinition(
            id="LOG_CONTENT_TEST",
            section="status",
            command="SHOW VARIABLES LIKE 'max_connections'"
        )
        
        result = self.executor.execute_query(query)
        
        with open(result.log_file, 'r') as f:
            content = f.read()
        
        # Check log file contains expected content
        self.assertIn("LOG_CONTENT_TEST", content)
        self.assertIn("SHOW VARIABLES LIKE 'max_connections'", content)
        self.assertIn("Variable_name", content)
        self.assertIn("Value", content)
        self.assertIn("max_connections", content)
        self.assertIn("151", content)
    
    def test_execute_query_records_execution_time(self):
        """
        Test that execute_query records execution time.
        """
        self.mock_cursor.description = [('col1', None, None, None, None, None, None)]
        self.mock_cursor.fetchall.return_value = [('value1',)]
        
        query = QueryDefinition(
            id="TIME_TEST",
            section="status",
            command="SELECT 1"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertIsInstance(result.execution_time, float)
        self.assertGreaterEqual(result.execution_time, 0)
    
    # =========================================================================
    # Query Failure Tests
    # =========================================================================
    
    def test_execute_query_handles_mysql_error(self):
        """
        Test that execute_query handles MySQL errors gracefully.
        
        When a query fails, log the error
        """
        # Setup mock cursor to raise MySQL error on the actual query execution
        # First call is for setting timeout, second is for the actual query
        error = mysql.connector.Error(msg="Table doesn't exist", errno=1146, sqlstate="42S02")
        self.mock_cursor.execute.side_effect = [None, error]  # First for timeout, second for query
        
        query = QueryDefinition(
            id="ERROR_TEST",
            section="status",
            command="SELECT * FROM nonexistent_table"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertIn("1146", result.error)
    
    def test_execute_query_error_written_to_log(self):
        """
        Test that query errors are written to the log file.
        
        When a query produces an error, write error message to log file
        """
        # Setup mock cursor to raise MySQL error
        error = mysql.connector.Error(msg="Access denied", errno=1045, sqlstate="28000")
        self.mock_cursor.execute.side_effect = [None, error]
        
        query = QueryDefinition(
            id="ERROR_LOG_TEST",
            section="status",
            command="SELECT * FROM restricted_table"
        )
        
        result = self.executor.execute_query(query)
        
        with open(result.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("ERROR", content)
        self.assertIn("1045", content)
    
    def test_execute_query_returns_none_columns_on_error(self):
        """
        Test that columns is None when query fails.
        """
        error = mysql.connector.Error(msg="Syntax error", errno=1064, sqlstate="42000")
        self.mock_cursor.execute.side_effect = [None, error]
        
        query = QueryDefinition(
            id="NULL_COLS_TEST",
            section="status",
            command="INVALID SQL"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertFalse(result.success)
        self.assertIsNone(result.columns)
        self.assertIsNone(result.rows)
    
    # =========================================================================
    # Query Timeout Tests
    # =========================================================================
    
    def test_execute_query_sets_timeout(self):
        """
        Test that execute_query sets the query timeout.
        
        Cancel queries that exceed timeout duration
        """
        self.mock_cursor.description = [('col1', None, None, None, None, None, None)]
        self.mock_cursor.fetchall.return_value = [('value1',)]
        
        # Create executor with specific timeout
        executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=60,  # 60 second timeout
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
        
        query = QueryDefinition(
            id="TIMEOUT_TEST",
            section="status",
            command="SELECT 1"
        )
        
        executor.execute_query(query)
        
        # Verify timeout was set (max_execution_time in milliseconds)
        calls = self.mock_cursor.execute.call_args_list
        timeout_call = [c for c in calls if 'max_execution_time' in str(c)]
        self.assertTrue(len(timeout_call) > 0, "Should set max_execution_time")
        self.assertIn("60000", str(timeout_call[0]))  # 60 seconds = 60000 ms
    
    def test_execute_query_default_timeout(self):
        """
        Test that execute_query uses default 30 second timeout.
        
        Use default timeout of 30 seconds
        """
        self.mock_cursor.description = [('col1', None, None, None, None, None, None)]
        self.mock_cursor.fetchall.return_value = [('value1',)]
        
        query = QueryDefinition(
            id="DEFAULT_TIMEOUT_TEST",
            section="status",
            command="SELECT 1"
        )
        
        self.executor.execute_query(query)
        
        # Verify default timeout was set (30 seconds = 30000 ms)
        calls = self.mock_cursor.execute.call_args_list
        timeout_call = [c for c in calls if 'max_execution_time' in str(c)]
        self.assertTrue(len(timeout_call) > 0, "Should set max_execution_time")
        self.assertIn("30000", str(timeout_call[0]))
    
    # =========================================================================
    # Edge Cases
    # =========================================================================
    
    def test_execute_query_handles_empty_result(self):
        """Test that execute_query handles queries returning no rows."""
        self.mock_cursor.description = [('col1', None, None, None, None, None, None)]
        self.mock_cursor.fetchall.return_value = []
        
        query = QueryDefinition(
            id="EMPTY_RESULT_TEST",
            section="status",
            command="SELECT * FROM empty_table"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertTrue(result.success)
        self.assertEqual(result.columns, ['col1'])
        self.assertEqual(result.rows, [])
    
    def test_execute_query_handles_null_values(self):
        """Test that execute_query handles NULL values in results."""
        self.mock_cursor.description = [
            ('col1', None, None, None, None, None, None),
            ('col2', None, None, None, None, None, None),
        ]
        self.mock_cursor.fetchall.return_value = [
            ('value1', None),
            (None, 'value2'),
        ]
        
        query = QueryDefinition(
            id="NULL_VALUES_TEST",
            section="status",
            command="SELECT col1, col2 FROM test"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertTrue(result.success)
        self.assertEqual(result.rows[0], ('value1', None))
        self.assertEqual(result.rows[1], (None, 'value2'))
        
        # Check log file handles NULL correctly
        with open(result.log_file, 'r') as f:
            content = f.read()
        self.assertIn("NULL", content)
    
    def test_execute_query_handles_no_description(self):
        """Test that execute_query handles queries with no column description (e.g., DDL)."""
        self.mock_cursor.description = None
        self.mock_cursor.fetchall.return_value = []
        
        query = QueryDefinition(
            id="NO_DESC_TEST",
            section="status",
            command="SET @var = 1"
        )
        
        result = self.executor.execute_query(query)
        
        self.assertTrue(result.success)
        self.assertIsNone(result.columns)


class TestQueryExecutorExecuteQueries(unittest.TestCase):
    """
    Test cases for QueryExecutor.execute_queries method.
    
    Tests batch query execution with filtering and error handling.
    
    Covers:
    - Execute each query from queries.json exactly once per run
    - When a query fails, log the error and continue with remaining queries
    - Filter queries by section
    - Filter queries by engine type
    """
    
    def setUp(self):
        """Set up test fixtures."""
        self.mock_connection = MagicMock()
        self.mock_cursor = MagicMock()
        self.mock_connection.cursor.return_value = self.mock_cursor
        self.test_logger = logging.getLogger('test_execute_queries')
        self.test_logger.setLevel(logging.DEBUG)
        
        # Create a temporary output directory
        import tempfile
        self.temp_dir = tempfile.mkdtemp()
        
        # Setup default cursor behavior for back-off checks
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(100),  # Connection count - OK
            (100,),  # Table count - OK
        ]
        
        # Create executor instance
        self.executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
        
        # Sample queries for testing
        self.sample_queries = [
            QueryDefinition(id="Q1", condition={"engine": ["mysql"]}, section="replication", command="SELECT 1"),
            QueryDefinition(id="Q2", section="status", command="SELECT 2"),
            QueryDefinition(id="Q3", condition={"engine": ["mysql"]}, section="performance", command="SELECT 3"),
        ]
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _setup_successful_query_execution(self):
        """Setup mock cursor for successful query execution."""
        self.mock_cursor.description = [('col1', None, None, None, None, None, None)]
        self.mock_cursor.fetchall.return_value = [('value1',)]
        # Reset fetchone for back-off checks
        self.mock_cursor.fetchone.side_effect = [
            _conn_row(100),  # Connection count
            (100,),  # Table count
        ]
    
    # =========================================================================
    # Successful Execution Tests
    # =========================================================================
    
    def test_execute_queries_runs_all_filtered_queries(self):
        """
        Test that execute_queries runs all filtered queries exactly once.
        
        Execute each query exactly once per run
        """
        self._setup_successful_query_execution()
        
        result = self.executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # All MySQL-compatible queries should be executed
        self.assertEqual(result.total_queries, 3)  # Q1, Q2, Q3 all match MySQL
        self.assertEqual(result.successful, 3)
        self.assertEqual(result.failed, 0)
    
    def test_execute_queries_stops_when_interrupt_check_true(self):
        """
        An interrupt_check returning True before any query stops execution
        immediately with zero results.
        """
        self._setup_successful_query_execution()
        
        executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger,
            interrupt_check=lambda: True
        )
        
        result = executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        self.assertEqual(len(result.query_results), 0)
        self.assertEqual(result.successful, 0)
        self.assertEqual(result.failed, 0)
    
    def test_execute_queries_stops_mid_batch_on_interrupt(self):
        """
        An interrupt raised after the first query stops the batch early
        and keeps the results gathered so far.
        """
        self._setup_successful_query_execution()
        
        state = {'calls': 0}
        def interrupt_after_first():
            state['calls'] += 1
            return state['calls'] > 1  # False for Q1, True afterwards
        
        executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger,
            interrupt_check=interrupt_after_first
        )
        
        result = executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # Only Q1 ran; Q2/Q3 were skipped by the interrupt
        self.assertEqual(len(result.query_results), 1)
        self.assertIn('Q1', result.query_results)
    
    def test_execute_queries_without_interrupt_check_runs_all(self):
        """
        The default executor (no interrupt_check) preserves the original
        run-everything behavior.
        """
        self._setup_successful_query_execution()
        
        result = self.executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        self.assertEqual(len(result.query_results), 3)
    
    def test_cancelled_query_is_not_counted_as_a_failure(self):
        """
        A statement cancelled by the interrupt's KILL QUERY raises
        ER_QUERY_INTERRUPTED. That is a deliberate cancellation, so it is
        excluded from the results and the failed count rather than being
        reported as a query defect.
        """
        import mysql.connector
        from diagnostic_collector_mysql.core.query_executor import ER_QUERY_INTERRUPTED
        
        self._setup_successful_query_execution()
        
        interrupted = mysql.connector.Error(
            msg='Query execution was interrupted',
            errno=ER_QUERY_INTERRUPTED
        )
        
        # Raise on Q1's statement specifically; back-off checks and the
        # SET SESSION preamble also go through cursor.execute.
        def execute_side_effect(sql, *args, **kwargs):
            if sql == 'SELECT 1':
                raise interrupted
            return None
        self.mock_cursor.execute.side_effect = execute_side_effect
        
        # The interrupt arrives while Q1 is running: the loop's poll sees
        # nothing yet, then the KILL QUERY lands mid-statement. Polling
        # False first is what makes Q1 actually execute, so this exercises
        # the cancellation carve-out rather than the top-of-loop break.
        polls = {'n': 0}
        def interrupt_check():
            polls['n'] += 1
            return polls['n'] > 1
        
        executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger,
            interrupt_check=interrupt_check
        )
        
        result = executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.successful, 0)
        self.assertEqual(len(result.query_results), 0)
        self.assertNotIn('Q1', result.query_results)
        
        # The cancelled statement was attempted, so it counts toward the
        # attempt total and the three counts must reconcile against it.
        self.assertEqual(result.cancelled, 1)
        self.assertEqual(result.total_queries, 1)
        self.assertEqual(
            result.successful + result.failed + result.cancelled,
            result.total_queries
        )
        
        # The report generator parses queries-output.log independently, so a
        # discarded statement must leave no block behind to be re-read.
        log_path = os.path.join(self.temp_dir, 'queries-output.log')
        if os.path.exists(log_path):
            with open(log_path) as log_file:
                self.assertNotIn('Query: Q1', log_file.read())
    
    def test_external_kill_is_still_a_failure(self):
        """
        Error 1317 is also raised when another session kills the statement.
        Without an interrupt of our own in progress, that is a real failure
        and must not be silently treated as a cancellation.
        """
        import mysql.connector
        from diagnostic_collector_mysql.core.query_executor import ER_QUERY_INTERRUPTED
        
        self._setup_successful_query_execution()
        
        killed = mysql.connector.Error(
            msg='Query execution was interrupted',
            errno=ER_QUERY_INTERRUPTED
        )
        
        def execute_side_effect(sql, *args, **kwargs):
            if sql == 'SELECT 1':
                raise killed
            return None
        self.mock_cursor.execute.side_effect = execute_side_effect
        
        # interrupt_check reports False: we never asked to stop.
        executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger,
            interrupt_check=lambda: False
        )
        
        result = executor.execute_queries(
            self.sample_queries[:1],
            EngineType.MYSQL,
            sections=None
        )
        
        self.assertEqual(result.failed, 1)
        self.assertIn('Q1', result.query_results)
    
    def test_real_query_failure_is_still_counted(self):
        """
        An ordinary query error is still recorded and counted, so the
        cancellation carve-out cannot hide genuine failures.
        """
        import mysql.connector
        
        self._setup_successful_query_execution()
        
        failure = mysql.connector.Error(msg='Table does not exist', errno=1146)
        
        def execute_side_effect(sql, *args, **kwargs):
            if sql == 'SELECT 1':
                raise failure
            return None
        self.mock_cursor.execute.side_effect = execute_side_effect
        
        result = self.executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        self.assertEqual(result.failed, 1)
        self.assertIn('Q1', result.query_results)
        self.assertFalse(result.query_results['Q1'].success)
    
    def test_execute_queries_returns_results_for_each_query(self):
        """
        Test that execute_queries returns results for each executed query.
        """
        self._setup_successful_query_execution()
        
        result = self.executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # Should have results for each query
        self.assertIn("Q1", result.query_results)
        self.assertIn("Q2", result.query_results)
        self.assertIn("Q3", result.query_results)
    
    def test_execute_queries_filters_by_engine(self):
        """
        Test that execute_queries filters queries by engine type.
        
        Filter queries based on detected database engine type
        """
        self._setup_successful_query_execution()
        
        queries = [
            QueryDefinition(id="MYSQL_Q", condition={"engine": ["mysql"]}, section="status", command="SELECT 1"),
            QueryDefinition(id="AURORA_Q", condition={"engine": ["aurora-mysql"]}, section="status", command="SELECT 2"),
            QueryDefinition(id="ALL_Q", section="status", command="SELECT 3"),
        ]
        
        result = self.executor.execute_queries(
            queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # Only MySQL and "all" queries should be executed
        self.assertEqual(result.total_queries, 2)
        self.assertIn("MYSQL_Q", result.query_results)
        self.assertIn("ALL_Q", result.query_results)
        self.assertNotIn("AURORA_Q", result.query_results)
    
    def test_execute_queries_filters_by_section(self):
        """
        Test that execute_queries filters queries by section.
        
        Filter by section when check flags specified
        """
        self._setup_successful_query_execution()
        
        result = self.executor.execute_queries(
            self.sample_queries,
            EngineType.MYSQL,
            sections={"replication"}
        )
        
        # Only replication section queries should be executed
        self.assertEqual(result.total_queries, 1)
        self.assertIn("Q1", result.query_results)
    
    # =========================================================================
    # Error Handling Tests
    # =========================================================================
    
    def test_execute_queries_continues_on_failure(self):
        """
        Test that execute_queries continues with remaining queries after failure.
        
        When a query fails, continue with remaining queries
        """
        # Create a fresh executor with fresh mock
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        
        # Track call count to control behavior
        call_count = [0]
        
        def execute_side_effect(query):
            call_count[0] += 1
            # Calls 1-2: Back-off check queries (connection count, table count)
            if call_count[0] <= 2:
                return None
            # Call 3: First query timeout setting
            if call_count[0] == 3:
                return None
            # Call 4: First actual query - FAIL
            if call_count[0] == 4:
                raise mysql.connector.Error(msg="Query failed", errno=1064, sqlstate="42000")
            # Call 5: Second query timeout setting
            if call_count[0] == 5:
                return None
            # Call 6: Second actual query - SUCCESS
            return None
        
        mock_cursor.execute.side_effect = execute_side_effect
        mock_cursor.description = [('col1', None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [('value1',)]
        mock_cursor.fetchone.side_effect = [
            _conn_row(100),  # Connection count - OK
            (100,),  # Table count - OK
        ]
        
        executor = QueryExecutor(
            connection=mock_conn,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
        
        queries = [
            QueryDefinition(id="FAIL_Q", section="status", command="INVALID"),
            QueryDefinition(id="SUCCESS_Q", section="status", command="SELECT 1"),
        ]
        
        result = executor.execute_queries(
            queries,
            EngineType.MYSQL,
            sections=None
        )
        
        # Both queries should be attempted
        self.assertEqual(result.total_queries, 2)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.successful, 1)
        
        # Results should exist for both
        self.assertIn("FAIL_Q", result.query_results)
        self.assertIn("SUCCESS_Q", result.query_results)
        self.assertFalse(result.query_results["FAIL_Q"].success)
        self.assertTrue(result.query_results["SUCCESS_Q"].success)
    
    def test_execute_queries_counts_failures_correctly(self):
        """
        Test that execute_queries correctly counts successful and failed queries.
        """
        # Create a fresh executor with fresh mock
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        
        # Track call count to control behavior
        call_count = [0]
        query_num = [0]  # Track which query we're on
        
        def execute_side_effect(query):
            call_count[0] += 1
            # Calls 1-2: Back-off check queries
            if call_count[0] <= 2:
                return None
            # For each query: timeout setting (odd calls after 2) and actual query (even calls after 2)
            adjusted_call = call_count[0] - 2
            if adjusted_call % 2 == 1:
                # Timeout setting call
                return None
            else:
                # Actual query call - alternate fail/success
                query_num[0] += 1
                if query_num[0] % 2 == 1:  # Q1, Q3 fail
                    raise mysql.connector.Error(msg="Failed", errno=1064, sqlstate="42000")
                return None  # Q2, Q4 succeed
        
        mock_cursor.execute.side_effect = execute_side_effect
        mock_cursor.description = [('col1', None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [('value1',)]
        mock_cursor.fetchone.side_effect = [
            _conn_row(100),  # Connection count
            (100,),  # Table count
        ]
        
        executor = QueryExecutor(
            connection=mock_conn,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
        
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1"),
            QueryDefinition(id="Q2", section="status", command="SELECT 2"),
            QueryDefinition(id="Q3", section="status", command="SELECT 3"),
            QueryDefinition(id="Q4", section="status", command="SELECT 4"),
        ]
        
        result = executor.execute_queries(
            queries,
            EngineType.MYSQL,
            sections=None
        )
        
        self.assertEqual(result.total_queries, 4)
        self.assertEqual(result.failed, 2)  # Q1, Q3 fail
        self.assertEqual(result.successful, 2)  # Q2, Q4 succeed
    
    # =========================================================================
    # Back-Off Integration Tests
    # =========================================================================
    
    def test_execute_queries_checks_backoff_first(self):
        """
        Test that execute_queries checks back-off thresholds before executing queries.
        """
        # Create a fresh executor with fresh mock
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        
        # Setup: Back-off threshold exceeded - connection count is 2000
        mock_cursor.fetchone.return_value = _conn_row(2000)  # Exceeds connection threshold of 1000
        
        executor = QueryExecutor(
            connection=mock_conn,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
        
        with self.assertRaises(BackOffError):
            executor.execute_queries(
                self.sample_queries,
                EngineType.MYSQL,
                sections=None
            )
    
    def test_execute_queries_no_queries_executed_on_backoff(self):
        """
        Test that no queries are executed when back-off threshold is exceeded.
        """
        # Create a fresh executor with fresh mock
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        
        # Setup: Back-off threshold exceeded
        mock_cursor.fetchone.return_value = _conn_row(2000)  # Exceeds connection threshold
        
        executor = QueryExecutor(
            connection=mock_conn,
            output_dir=self.temp_dir,
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
        
        try:
            executor.execute_queries(
                self.sample_queries,
                EngineType.MYSQL,
                sections=None
            )
        except BackOffError:
            pass
        
        # Only back-off check query should have been executed
        # No actual query commands should be in the execute calls
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        query_calls = [c for c in calls if 'SELECT 1' in c or 'SELECT 2' in c or 'SELECT 3' in c]
        self.assertEqual(len(query_calls), 0)
    
    # =========================================================================
    # Edge Cases
    # =========================================================================
    
    def test_execute_queries_empty_list(self):
        """Test that execute_queries handles empty query list."""
        self._setup_successful_query_execution()
        
        result = self.executor.execute_queries(
            [],
            EngineType.MYSQL,
            sections=None
        )
        
        self.assertEqual(result.total_queries, 0)
        self.assertEqual(result.successful, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.query_results, {})
    
    def test_execute_queries_all_filtered_out(self):
        """Test that execute_queries handles case where all queries are filtered out."""
        self._setup_successful_query_execution()
        
        queries = [
            QueryDefinition(id="AURORA_Q", condition={"engine": ["aurora-mysql"]}, section="status", command="SELECT 1"),
        ]
        
        result = self.executor.execute_queries(
            queries,
            EngineType.MARIADB,  # Different engine
            sections=None
        )
        
        self.assertEqual(result.total_queries, 0)
        self.assertEqual(result.query_results, {})


class TestNewQueryFormatFiltering(unittest.TestCase):
    """
    Test cases for the new query format with condition and version filtering.
    
    Tests validate:
    - Filtering using condition["engine"] list
    - Version filtering with min_engine_version and max_engine_version
    - Default behavior (no filtering fields = run on all engines/versions)
    - Combined condition and version filtering (AND logic)
    """
    
    def setUp(self):
        """Set up test fixtures."""
        self.mock_connection = MagicMock()
        self.test_logger = logging.getLogger('test_new_format_filter')
        self.test_logger.setLevel(logging.DEBUG)
        
        # Create executor instance
        self.executor = QueryExecutor(
            connection=self.mock_connection,
            output_dir='/tmp/test_output',
            timestamp='20260218_143052',
            query_timeout=30,
            connection_threshold=1000,
            objects_threshold=10000,
            logger=self.test_logger
        )
    
    # =========================================================================
    # Condition-based Engine Filtering Tests
    # =========================================================================
    
    def test_filter_with_condition_single_engine(self):
        """Test filtering with condition containing single engine."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          condition={"engine": ["mysql"]}),
            QueryDefinition(id="Q2", section="status", command="SELECT 2",
                          condition={"engine": ["aurora-mysql"]}),
        ]
        
        result = self.executor.filter_queries(queries, EngineType.MYSQL, None, None)
        result_ids = {q.id for q in result}
        
        self.assertEqual(result_ids, {"Q1"})
    
    def test_filter_with_condition_multiple_engines(self):
        """Test filtering with condition containing multiple engines."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          condition={"engine": ["mysql", "aurora-mysql"]}),
            QueryDefinition(id="Q2", section="status", command="SELECT 2",
                          condition={"engine": ["mariadb"]}),
        ]
        
        result = self.executor.filter_queries(queries, EngineType.MYSQL, None, None)
        result_ids = {q.id for q in result}
        
        self.assertEqual(result_ids, {"Q1"})
        
        result = self.executor.filter_queries(queries, EngineType.AURORA_MYSQL, None, None)
        result_ids = {q.id for q in result}
        
        self.assertEqual(result_ids, {"Q1"})
    
    def test_filter_no_condition_runs_on_all_engines(self):
        """Test that queries without condition run on all engines."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1"),  # No condition
            QueryDefinition(id="Q2", section="status", command="SELECT 2",
                          condition={"engine": ["mysql"]}),
        ]
        
        # Q1 should be included for all engine types
        for engine_type in [EngineType.MYSQL, EngineType.AURORA_MYSQL, EngineType.MARIADB]:
            result = self.executor.filter_queries(queries, engine_type, None, None)
            result_ids = {q.id for q in result}
            self.assertIn("Q1", result_ids, f"Q1 should run on {engine_type}")
    
    # =========================================================================
    # Version Filtering Tests
    # =========================================================================
    
    def test_filter_min_version_constraint(self):
        """Test filtering with min_engine_version constraint."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          min_engine_version="8.0"),
            QueryDefinition(id="Q2", section="status", command="SELECT 2",
                          min_engine_version="8.4"),
        ]
        
        # Version 8.0.29 should include Q1 but not Q2
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "8.0.29", None)
        result_ids = {q.id for q in result}
        
        self.assertEqual(result_ids, {"Q1"})
    
    def test_filter_max_version_constraint(self):
        """Test filtering with max_engine_version constraint."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          max_engine_version="8.0"),
            QueryDefinition(id="Q2", section="status", command="SELECT 2",
                          max_engine_version="8.4"),
        ]
        
        # Version 8.2.0 should include Q2 but not Q1
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "8.2.0", None)
        result_ids = {q.id for q in result}
        
        self.assertEqual(result_ids, {"Q2"})
    
    def test_filter_version_range(self):
        """Test filtering with both min and max version constraints."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          min_engine_version="8.0", max_engine_version="8.4"),
        ]
        
        # Version 8.2.0 should be included (within range)
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "8.2.0", None)
        self.assertEqual(len(result), 1)
        
        # Version 7.5.0 should be excluded (below min)
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "7.5.0", None)
        self.assertEqual(len(result), 0)
        
        # Version 9.0.0 should be excluded (above max)
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "9.0.0", None)
        self.assertEqual(len(result), 0)
    
    def test_filter_empty_version_string_no_constraint(self):
        """Test that empty version string means no constraint."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          min_engine_version="", max_engine_version=""),
        ]
        
        # Should be included for any version
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "5.7.0", None)
        self.assertEqual(len(result), 1)
        
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "8.4.0", None)
        self.assertEqual(len(result), 1)
    
    def test_filter_no_version_fields_runs_on_all_versions(self):
        """Test that queries without version fields run on all versions."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1"),  # No version fields
        ]
        
        # Should be included for any version
        for version in ["5.7.0", "8.0.29", "8.4.0", "10.6.12"]:
            result = self.executor.filter_queries(queries, EngineType.MYSQL, version, None)
            self.assertEqual(len(result), 1, f"Q1 should run on version {version}")
    
    def test_filter_version_normalization_xy_to_xyz(self):
        """Test that x.y version format is normalized to x.y.0 for comparison."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          min_engine_version="8.0", max_engine_version="8.4"),
        ]
        
        # Version 8.0 (normalized to 8.0.0) should be included (at min boundary)
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "8.0", None)
        self.assertEqual(len(result), 1)
        
        # Version 8.4 (normalized to 8.4.0) should be included (at max boundary)
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "8.4", None)
        self.assertEqual(len(result), 1)
    
    # =========================================================================
    # Combined Condition and Version Filtering Tests (AND logic)
    # =========================================================================
    
    def test_filter_combined_condition_and_version(self):
        """Test combined filtering with both condition and version (AND logic)."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          condition={"engine": ["mysql"]},
                          min_engine_version="8.0", max_engine_version="8.4"),
        ]
        
        # MySQL 8.2.0 should be included (engine matches AND version in range)
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "8.2.0", None)
        self.assertEqual(len(result), 1)
        
        # Aurora MySQL 8.2.0 should be excluded (engine doesn't match)
        result = self.executor.filter_queries(queries, EngineType.AURORA_MYSQL, "8.2.0", None)
        self.assertEqual(len(result), 0)
        
        # MySQL 9.0.0 should be excluded (version out of range)
        result = self.executor.filter_queries(queries, EngineType.MYSQL, "9.0.0", None)
        self.assertEqual(len(result), 0)
    
    def test_filter_no_detected_version_skips_version_check(self):
        """Test that version filtering is skipped when no detected version is provided."""
        queries = [
            QueryDefinition(id="Q1", section="status", command="SELECT 1",
                          min_engine_version="8.0", max_engine_version="8.4"),
        ]
        
        # When no version is provided, version filtering should be skipped
        result = self.executor.filter_queries(queries, EngineType.MYSQL, None, None)
        self.assertEqual(len(result), 1)


class TestVersionNormalizationFunction(unittest.TestCase):
    """Test cases for the normalize_version helper function."""
    
    def test_normalize_xy_format(self):
        """Test normalizing x.y format to (x, y, 0) tuple."""
        from diagnostic_collector_mysql.core.query_executor import normalize_version
        
        self.assertEqual(normalize_version("8.0"), (8, 0, 0))
        self.assertEqual(normalize_version("10.6"), (10, 6, 0))
        self.assertEqual(normalize_version("5.7"), (5, 7, 0))
    
    def test_normalize_xyz_format(self):
        """Test normalizing x.y.z format to (x, y, z) tuple."""
        from diagnostic_collector_mysql.core.query_executor import normalize_version
        
        self.assertEqual(normalize_version("8.0.29"), (8, 0, 29))
        self.assertEqual(normalize_version("10.6.12"), (10, 6, 12))
        self.assertEqual(normalize_version("5.7.44"), (5, 7, 44))
    
    def test_version_comparison(self):
        """Test that normalized versions compare correctly."""
        from diagnostic_collector_mysql.core.query_executor import normalize_version
        
        # 8.0.29 > 8.0 (8.0.0)
        self.assertGreater(normalize_version("8.0.29"), normalize_version("8.0"))
        
        # 8.4 > 8.0.29
        self.assertGreater(normalize_version("8.4"), normalize_version("8.0.29"))
        
        # 8.0 == 8.0.0
        self.assertEqual(normalize_version("8.0"), normalize_version("8.0.0"))
        
        # 10.6.12 > 8.4.0
        self.assertGreater(normalize_version("10.6.12"), normalize_version("8.4.0"))


if __name__ == '__main__':
    unittest.main()
