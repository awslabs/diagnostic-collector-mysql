#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for ExecutionLogger - main execution log management.

Tests validate:
- Write main execution log to diagnostic_collector_mysql_YYYYMMDD_HHMMSS.log
- Log connection establishment status and engine detection result
- Log each query execution start and completion with timing
- Log warnings for queries that exceed 50% of timeout
- Log all errors with descriptive messages
- Log total execution duration at completion
- Include timestamp prefix on all log entries
"""

import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

from diagnostic_collector_mysql.core.execution_logger import ExecutionLogger
from diagnostic_collector_mysql.models import EngineType


class TestExecutionLoggerInit(unittest.TestCase):
    """Tests for ExecutionLogger initialization."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_init_with_defaults(self):
        """Test initialization with default values."""
        logger = ExecutionLogger(self.log_file)
        
        self.assertEqual(logger.log_file_path, self.log_file)
        self.assertEqual(logger._query_timeout, 30)
        self.assertFalse(logger._verbose)
    
    def test_init_with_custom_timeout(self):
        """Test initialization with custom query timeout."""
        logger = ExecutionLogger(self.log_file, query_timeout=60)
        
        self.assertEqual(logger._query_timeout, 60)
    
    def test_init_with_verbose(self):
        """Test initialization with verbose mode enabled."""
        logger = ExecutionLogger(self.log_file, verbose=True)
        
        self.assertTrue(logger._verbose)


class TestExecutionLoggerStartStop(unittest.TestCase):
    """Tests for start/stop functionality."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_start_creates_log_file(self):
        """Test that start() creates the log file."""
        logger = ExecutionLogger(self.log_file)
        logger.start()
        
        try:
            self.assertTrue(os.path.exists(self.log_file))
        finally:
            logger.stop()
    
    def test_start_writes_header(self):
        """Test that start() writes header with timestamp."""
        logger = ExecutionLogger(self.log_file)
        logger.start()
        logger.stop()
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Diagnostic Collector for MySQL Execution Log", content)
        self.assertIn("Started:", content)
    
    def test_stop_writes_footer(self):
        """Test that stop() writes completion footer."""
        logger = ExecutionLogger(self.log_file)
        logger.start()
        logger.stop()
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Completed:", content)
    
    def test_context_manager(self):
        """Test context manager usage."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_info("Test message")
        
        self.assertTrue(os.path.exists(self.log_file))
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Test message", content)
        self.assertIn("Completed:", content)


class TestTimestampFormat(unittest.TestCase):
    """Tests for timestamp formatting."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_log_entries_have_timestamp_prefix(self):
        """Test all log entries have timestamp prefix."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_info("Test info message")
            logger.log_warning("Test warning message")
            logger.log_error("Test error message")
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        # Check for timestamp pattern [YYYY-MM-DD HH:MM:SS]
        timestamp_pattern = r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]'
        
        # Find all log entries (lines with level indicators)
        for line in content.split('\n'):
            if '[INFO]' in line or '[WARNING]' in line or '[ERROR]' in line:
                self.assertRegex(line, timestamp_pattern,
                    f"Log entry missing timestamp: {line}")
    
    def test_timestamp_format_is_valid(self):
        """Test timestamp can be parsed as datetime."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_info("Test message")
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        # Extract timestamp from a log entry
        match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', content)
        self.assertIsNotNone(match, "No timestamp found in log")
        
        # Verify it can be parsed
        timestamp_str = match.group(1)
        parsed = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        self.assertIsInstance(parsed, datetime)


class TestConnectionLogging(unittest.TestCase):
    """Tests for connection status logging."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_log_successful_connection(self):
        """Test logging successful connection."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_connection_status(
                host="localhost",
                port=3306,
                user="testuser",
                success=True,
                engine_type=EngineType.MYSQL
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Connected to localhost:3306", content)
        self.assertIn("testuser", content)
        self.assertIn("[INFO]", content)
    
    def test_log_engine_detection(self):
        """Test logging engine detection result."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_connection_status(
                host="localhost",
                port=3306,
                user="testuser",
                success=True,
                engine_type=EngineType.AURORA_MYSQL
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Detected database engine: aurora-mysql", content)
    
    def test_log_connection_failure(self):
        """Test logging connection failure."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_connection_status(
                host="localhost",
                port=3306,
                user="testuser",
                success=False,
                error_message="Access denied for user 'testuser'"
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Failed to connect", content)
        self.assertIn("Access denied", content)
        self.assertIn("[ERROR]", content)
    
    def test_log_all_engine_types(self):
        """Test logging all engine types."""
        for engine_type in EngineType:
            log_file = os.path.join(self.temp_dir, f"test_{engine_type.value}.log")
            
            with ExecutionLogger(log_file) as logger:
                logger.log_connection_status(
                    host="localhost",
                    port=3306,
                    user="testuser",
                    success=True,
                    engine_type=engine_type
                )
            
            with open(log_file, 'r') as f:
                content = f.read()
            
            self.assertIn(f"Detected database engine: {engine_type.value}", content)


class TestQueryLogging(unittest.TestCase):
    """Tests for query execution logging."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_log_query_start(self):
        """Test logging query start."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_query_start("GLOBAL_STATUS", "status")
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Starting query: GLOBAL_STATUS", content)
        self.assertIn("section: status", content)
    
    def test_log_query_complete_success(self):
        """Test logging successful query completion."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_query_complete(
                query_id="GLOBAL_STATUS",
                success=True,
                execution_time=0.523,
                row_count=150
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Completed query: GLOBAL_STATUS", content)
        self.assertIn("0.523s", content)
        self.assertIn("150 rows", content)
    
    def test_log_query_complete_failure(self):
        """Test logging failed query completion."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_query_complete(
                query_id="INVALID_QUERY",
                success=False,
                execution_time=0.100,
                error_message="Syntax error in SQL"
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Query INVALID_QUERY failed", content)
        self.assertIn("Syntax error in SQL", content)
        self.assertIn("[ERROR]", content)
    
    def test_log_slow_query_warning(self):
        """Test warning for queries exceeding 50% of timeout."""
        # With 30s timeout, warning threshold is 15s
        with ExecutionLogger(self.log_file, query_timeout=30) as logger:
            logger.log_query_complete(
                query_id="SLOW_QUERY",
                success=True,
                execution_time=16.0,  # Exceeds 50% of 30s
                row_count=10
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("[WARNING]", content)
        self.assertIn("exceeding 50% of timeout", content)
        self.assertIn("SLOW_QUERY", content)
    
    def test_no_warning_for_fast_query(self):
        """Test no warning for queries under 50% of timeout."""
        with ExecutionLogger(self.log_file, query_timeout=30) as logger:
            logger.log_query_complete(
                query_id="FAST_QUERY",
                success=True,
                execution_time=5.0,  # Under 50% of 30s
                row_count=10
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        # Should not contain warning about exceeding timeout
        self.assertNotIn("exceeding 50% of timeout", content)
    
    def test_warning_threshold_calculation(self):
        """Test warning threshold is correctly calculated as 50% of timeout."""
        # With 60s timeout, warning threshold should be 30s
        with ExecutionLogger(self.log_file, query_timeout=60) as logger:
            # 25s should NOT trigger warning (under 50%)
            logger.log_query_complete(
                query_id="QUERY_25S",
                success=True,
                execution_time=25.0,
                row_count=10
            )
            # 35s should trigger warning (over 50%)
            logger.log_query_complete(
                query_id="QUERY_35S",
                success=True,
                execution_time=35.0,
                row_count=10
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        # Only QUERY_35S should have warning
        self.assertNotIn("QUERY_25S took", content.split("exceeding")[0] if "exceeding" in content else content)
        self.assertIn("QUERY_35S", content)
        self.assertIn("exceeding 50% of timeout", content)


class TestErrorLogging(unittest.TestCase):
    """Tests for error logging."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_log_error(self):
        """Test logging error messages."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_error("Database connection lost")
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("[ERROR]", content)
        self.assertIn("Database connection lost", content)
    
    def test_log_warning(self):
        """Test logging warning messages."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_warning("Performance schema is disabled")
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("[WARNING]", content)
        self.assertIn("Performance schema is disabled", content)
    
    def test_context_manager_logs_exception(self):
        """Test context manager logs exception on error."""
        try:
            with ExecutionLogger(self.log_file) as logger:
                raise ValueError("Test exception")
        except ValueError:
            pass
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("[ERROR]", content)
        self.assertIn("terminated due to error", content)


class TestTotalDurationLogging(unittest.TestCase):
    """Tests for total duration logging."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_log_total_duration_seconds(self):
        """Test logging total duration in seconds."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_total_duration(45.123)
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Total execution duration:", content)
        self.assertIn("45.123s", content)
    
    def test_log_total_duration_minutes(self):
        """Test logging total duration with minutes."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_total_duration(125)  # 2m 5s
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("2m 5s", content)
    
    def test_log_total_duration_hours(self):
        """Test logging total duration with hours."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_total_duration(3725)  # 1h 2m 5s
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("1h 2m 5s", content)
    
    def test_stop_logs_duration_automatically(self):
        """Test stop() automatically logs duration."""
        logger = ExecutionLogger(self.log_file)
        logger.start()
        time.sleep(0.1)  # Small delay to ensure measurable duration
        logger.stop()
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Total execution duration:", content)


class TestBackoffLogging(unittest.TestCase):
    """Tests for back-off event logging."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_log_backoff_triggered(self):
        """Test logging back-off events."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_backoff_triggered(
                reason="Active connection count exceeds threshold",
                current_value=1500,
                threshold=1000
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("[WARNING]", content)
        self.assertIn("Back-off triggered", content)
        self.assertIn("Active connection count exceeds threshold", content)
        self.assertIn("Current value: 1500", content)
        self.assertIn("Threshold: 1000", content)


class TestExecutionSummary(unittest.TestCase):
    """Tests for execution summary logging."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_log_execution_summary(self):
        """Test logging execution summary."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_execution_summary(
                total_queries=50,
                successful=48,
                failed=2
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("Execution summary:", content)
        self.assertIn("48/50 queries successful", content)
        self.assertIn("2 failed", content)
    
    def test_log_execution_summary_records_cancellation(self):
        """An interrupted run records the cancellation, so the counts in the
        log add up to the attempt total. This log ships in support bundles,
        so the console output alone is not enough."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_execution_summary(
                total_queries=1,
                successful=0,
                failed=0,
                cancelled=1
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("0/1 queries successful", content)
        self.assertIn("1 cancelled by interrupt", content)
    
    def test_log_execution_summary_omits_zero_cancelled(self):
        """An uninterrupted run's summary is unchanged."""
        with ExecutionLogger(self.log_file) as logger:
            logger.log_execution_summary(
                total_queries=50,
                successful=48,
                failed=2
            )
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertNotIn("cancelled", content)


class TestVerboseMode(unittest.TestCase):
    """Tests for verbose mode logging ."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.temp_dir, "diagnostic_collector_mysql_20260218_143052.log")
    
    def tearDown(self):
        """Clean up temporary directory."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_debug_logged_when_verbose(self):
        """Test debug messages are logged when verbose is True."""
        with ExecutionLogger(self.log_file, verbose=True) as logger:
            logger.log_debug("Debug information")
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertIn("[DEBUG]", content)
        self.assertIn("Debug information", content)
    
    def test_debug_not_logged_when_not_verbose(self):
        """Test debug messages are NOT logged when verbose is False."""
        with ExecutionLogger(self.log_file, verbose=False) as logger:
            logger.log_debug("Debug information")
        
        with open(self.log_file, 'r') as f:
            content = f.read()
        
        self.assertNotIn("[DEBUG]", content)
        self.assertNotIn("Debug information", content)


if __name__ == '__main__':
    unittest.main()
