#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for OutputManager - output directory management.

Tests validate:
- Create output directory named diagnostic_collector_mysql_data_YYYYMMDD_HHMMSS/
- Use same timestamp across all files within a single run
- Store all output files within timestamped directory
- Create output directory BEFORE database connection
- Print full output directory path to stdout (unless --quiet)
- Exit with code 4 and clear error message when directory cannot be created
- Validate --output-dir path exists and is writable
"""

import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from io import StringIO
from unittest.mock import patch

from diagnostic_collector_mysql.output.output_manager import OutputManager, OutputDirectoryError


class TestTimestampGeneration(unittest.TestCase):
    """Tests for timestamp generation."""
    
    def test_timestamp_format(self):
        """Test timestamp is in YYYYMMDD_HHMMSS format."""
        manager = OutputManager()
        timestamp = manager.timestamp
        
        # Verify format with regex
        pattern = r'^\d{8}_\d{6}$'
        self.assertRegex(timestamp, pattern, 
            f"Timestamp '{timestamp}' should match YYYYMMDD_HHMMSS format")
    
    def test_timestamp_is_valid_datetime(self):
        """Test timestamp can be parsed back to datetime."""
        manager = OutputManager()
        timestamp = manager.timestamp
        
        # Should not raise exception
        parsed = datetime.strptime(timestamp, "%Y%m%d_%H%M%S")
        self.assertIsInstance(parsed, datetime)
    
    def test_custom_timestamp_preserved(self):
        """Test that custom timestamp is preserved when provided."""
        custom_ts = "20260218_143052"
        manager = OutputManager(timestamp=custom_ts)
        
        self.assertEqual(manager.timestamp, custom_ts)
    
    def test_timestamp_consistency_within_instance(self):
        """Test timestamp remains consistent across multiple accesses."""
        manager = OutputManager()
        ts1 = manager.timestamp
        ts2 = manager.timestamp
        ts3 = manager.timestamp
        
        self.assertEqual(ts1, ts2)
        self.assertEqual(ts2, ts3)


class TestBaseDirectoryValidation(unittest.TestCase):
    """Tests for base directory validation."""
    
    def setUp(self):
        """Create temporary directories for testing."""
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up temporary directories."""
        if os.path.exists(self.temp_dir):
            # Restore permissions if needed
            for root, dirs, files in os.walk(self.temp_dir):
                for d in dirs:
                    os.chmod(os.path.join(root, d), stat.S_IRWXU)
            shutil.rmtree(self.temp_dir)
    
    def test_valid_base_directory(self):
        """Test validation passes for valid directory."""
        manager = OutputManager(base_dir=self.temp_dir)
        
        # Should not raise
        manager.validate_base_directory()
    
    def test_nonexistent_base_directory_gets_created(self):
        """Test that nonexistent base directory is created automatically."""
        nonexistent = os.path.join(self.temp_dir, "nonexistent")
        manager = OutputManager(base_dir=nonexistent)
        
        # Should not raise - directory should be created
        manager.validate_base_directory()
        
        # Verify directory was created
        self.assertTrue(os.path.exists(nonexistent))
        self.assertTrue(os.path.isdir(nonexistent))
    
    def test_nested_nonexistent_directory_gets_created(self):
        """Test that nested nonexistent directories are created automatically."""
        nested = os.path.join(self.temp_dir, "level1", "level2", "level3")
        manager = OutputManager(base_dir=nested)
        
        # Should not raise - directories should be created
        manager.validate_base_directory()
        
        # Verify directory was created
        self.assertTrue(os.path.exists(nested))
        self.assertTrue(os.path.isdir(nested))
    
    def test_base_path_is_file_not_directory(self):
        """Test error when base path is a file, not directory."""
        file_path = os.path.join(self.temp_dir, "file.txt")
        with open(file_path, 'w') as f:
            f.write("test")
        
        manager = OutputManager(base_dir=file_path)
        
        with self.assertRaises(OutputDirectoryError) as ctx:
            manager.validate_base_directory()
        
        self.assertIn("not a directory", ctx.exception.reason)
    
    @unittest.skipIf(os.name == 'nt', "Permission tests not reliable on Windows")
    def test_unwritable_base_directory(self):
        """Test error when base directory is not writable."""
        readonly_dir = os.path.join(self.temp_dir, "readonly")
        os.makedirs(readonly_dir)
        os.chmod(readonly_dir, stat.S_IRUSR | stat.S_IXUSR)  # Read and execute only
        
        try:
            manager = OutputManager(base_dir=readonly_dir)
            
            with self.assertRaises(OutputDirectoryError) as ctx:
                manager.validate_base_directory()
            
            self.assertIn("not writable", ctx.exception.reason)
        finally:
            # Restore permissions for cleanup
            os.chmod(readonly_dir, stat.S_IRWXU)
    
    @unittest.skipIf(os.name == 'nt', "Permission tests not reliable on Windows")
    def test_custom_dir_permission_error(self):
        """Test error message when directory cannot be created due to permissions."""
        # Create a readonly parent directory
        readonly_parent = os.path.join(self.temp_dir, "readonly_parent")
        os.makedirs(readonly_parent)
        os.chmod(readonly_parent, stat.S_IRUSR | stat.S_IXUSR)  # Read and execute only
        
        try:
            # Try to create a subdirectory in the readonly parent
            nonexistent = os.path.join(readonly_parent, "custom_path")
            manager = OutputManager(base_dir=nonexistent)
            
            with self.assertRaises(OutputDirectoryError) as ctx:
                manager.validate_base_directory()
            
            self.assertTrue(ctx.exception.is_custom_dir)
            self.assertIn("permission denied", ctx.exception.reason.lower())
        finally:
            # Restore permissions for cleanup
            os.chmod(readonly_parent, stat.S_IRWXU)
    
    def test_default_dir_flag_in_error(self):
        """Test error message indicates default directory when using '.'."""
        # Create a manager with default directory, then test with nonexistent
        # We need to simulate a case where default dir doesn't exist
        manager = OutputManager(base_dir='.')
        manager.base_dir = "/nonexistent/path"
        manager._is_custom_dir = False
        
        with self.assertRaises(OutputDirectoryError) as ctx:
            manager.validate_base_directory()
        
        self.assertFalse(ctx.exception.is_custom_dir)
        self.assertIn("default directory", str(ctx.exception))


class TestOutputDirectoryCreation(unittest.TestCase):
    """Tests for output directory creation."""
    
    def setUp(self):
        """Create temporary directory for testing."""
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up temporary directories."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_creates_timestamped_directory(self):
        """Test directory is created with correct name format."""
        timestamp = "20260218_143052"
        manager = OutputManager(base_dir=self.temp_dir, timestamp=timestamp)
        
        output_dir = manager.create_output_directory()
        
        expected_name = f"diagnostic_collector_mysql_data_{timestamp}"
        expected_path = os.path.join(self.temp_dir, expected_name)
        
        self.assertEqual(output_dir, expected_path)
        self.assertTrue(os.path.isdir(output_dir))
    
    def test_output_dir_property_set_after_creation(self):
        """Test output_dir property is set after creation."""
        manager = OutputManager(base_dir=self.temp_dir)
        
        self.assertIsNone(manager.output_dir)
        
        output_dir = manager.create_output_directory()
        
        self.assertEqual(manager.output_dir, output_dir)
    
    def test_directory_created_before_connection(self):
        """Test directory can be created independently."""
        manager = OutputManager(base_dir=self.temp_dir)
        
        # Should be able to create directory without any database connection
        output_dir = manager.create_output_directory()
        
        self.assertTrue(os.path.exists(output_dir))
    
    def test_error_on_invalid_base_directory(self):
        """Test OutputDirectoryError raised for invalid base directory."""
        manager = OutputManager(base_dir="/nonexistent/path")
        
        with self.assertRaises(OutputDirectoryError):
            manager.create_output_directory()
    
    def test_error_message_contains_path(self):
        """Test error message contains the path that failed."""
        invalid_path = "/nonexistent/path"
        manager = OutputManager(base_dir=invalid_path)
        
        with self.assertRaises(OutputDirectoryError) as ctx:
            manager.create_output_directory()
        
        self.assertIn(invalid_path, str(ctx.exception))
    
    def test_error_message_contains_reason(self):
        """Test error message contains reason for failure."""
        manager = OutputManager(base_dir="/nonexistent/path")
        
        with self.assertRaises(OutputDirectoryError) as ctx:
            manager.create_output_directory()
        
        # Should contain some indication of why it failed
        error_str = str(ctx.exception)
        self.assertTrue(
            "does not exist" in error_str or 
            "Reason:" in error_str
        )
    
    def test_handles_existing_directory(self):
        """Test handles case where directory already exists."""
        timestamp = "20260218_143052"
        manager = OutputManager(base_dir=self.temp_dir, timestamp=timestamp)
        
        # Pre-create the directory
        expected_path = os.path.join(self.temp_dir, f"diagnostic_collector_mysql_data_{timestamp}")
        os.makedirs(expected_path)
        
        # Should not raise, should return existing path
        output_dir = manager.create_output_directory()
        
        self.assertEqual(output_dir, expected_path)


class TestFilePathGeneration(unittest.TestCase):
    """Tests for file path generation."""
    
    def setUp(self):
        """Create temporary directory and output manager."""
        self.temp_dir = tempfile.mkdtemp()
        self.timestamp = "20260218_143052"
        self.manager = OutputManager(base_dir=self.temp_dir, timestamp=self.timestamp)
        self.output_dir = self.manager.create_output_directory()
    
    def tearDown(self):
        """Clean up temporary directories."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_query_log_path_format(self):
        """Test query log path format: {QUERY_ID}_YYYYMMDD_HHMMSS.log."""
        query_id = "GLOBAL_STATUS"
        path = self.manager.get_query_log_path(query_id)
        
        expected_filename = f"{query_id}_{self.timestamp}.log"
        expected_path = os.path.join(self.output_dir, expected_filename)
        
        self.assertEqual(path, expected_path)
    
    def test_query_log_path_within_output_dir(self):
        """Test query log is within timestamped directory."""
        path = self.manager.get_query_log_path("TEST_QUERY")
        
        self.assertTrue(path.startswith(self.output_dir))
    
    def test_main_log_path_format(self):
        """Test main log path format: diagnostic_collector_mysql_YYYYMMDD_HHMMSS.log."""
        path = self.manager.get_main_log_path()
        
        expected_filename = f"diagnostic_collector_mysql_{self.timestamp}.log"
        expected_path = os.path.join(self.output_dir, expected_filename)
        
        self.assertEqual(path, expected_path)
    
    def test_main_log_path_within_output_dir(self):
        """Test main log is within timestamped directory."""
        path = self.manager.get_main_log_path()
        
        self.assertTrue(path.startswith(self.output_dir))
    
    def test_report_path_html_format(self):
        """Test report path for HTML format."""
        path = self.manager.get_report_path('html')
        
        expected_filename = f"report_{self.timestamp}.html"
        expected_path = os.path.join(self.output_dir, expected_filename)
        
        self.assertEqual(path, expected_path)
    
    def test_report_path_json_format(self):
        """Test report path for JSON format."""
        path = self.manager.get_report_path('json')
        
        self.assertTrue(path.endswith('.json'))
    
    def test_report_path_markdown_format(self):
        """Test report path for Markdown format."""
        path = self.manager.get_report_path('markdown')
        
        self.assertTrue(path.endswith('.md'))
    
    def test_report_path_text_format(self):
        """Test report path for text format."""
        path = self.manager.get_report_path('text')
        
        self.assertTrue(path.endswith('.txt'))
    
    def test_report_path_within_output_dir(self):
        """Test report is within timestamped directory."""
        path = self.manager.get_report_path('html')
        
        self.assertTrue(path.startswith(self.output_dir))
    
    def test_invalid_report_format(self):
        """Test error for invalid report format."""
        with self.assertRaises(ValueError) as ctx:
            self.manager.get_report_path('invalid')
        
        self.assertIn("Unknown output format", str(ctx.exception))
    
    def test_all_paths_use_same_timestamp(self):
        """Test all file paths use the same timestamp."""
        query_path = self.manager.get_query_log_path("TEST")
        main_log_path = self.manager.get_main_log_path()
        report_path = self.manager.get_report_path('html')
        
        # Extract timestamps from filenames
        self.assertIn(self.timestamp, query_path)
        self.assertIn(self.timestamp, main_log_path)
        self.assertIn(self.timestamp, report_path)
    
    def test_error_when_output_dir_not_created(self):
        """Test error when trying to get paths before creating output directory."""
        manager = OutputManager(base_dir=self.temp_dir)
        
        with self.assertRaises(RuntimeError):
            manager.get_query_log_path("TEST")
        
        with self.assertRaises(RuntimeError):
            manager.get_main_log_path()
        
        with self.assertRaises(RuntimeError):
            manager.get_report_path('html')


class TestPrintOutputDirectory(unittest.TestCase):
    """Tests for printing output directory path."""
    
    def setUp(self):
        """Create temporary directory and output manager."""
        self.temp_dir = tempfile.mkdtemp()
        self.manager = OutputManager(base_dir=self.temp_dir)
        self.output_dir = self.manager.create_output_directory()
    
    def tearDown(self):
        """Clean up temporary directories."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_prints_output_directory(self):
        """Test output directory is printed to stdout."""
        captured = StringIO()
        with patch('sys.stdout', captured):
            self.manager.print_output_directory(quiet=False)
        
        output = captured.getvalue()
        self.assertIn(self.output_dir, output)
        self.assertIn("Output directory:", output)
    
    def test_quiet_suppresses_output(self):
        """Test --quiet suppresses output."""
        captured = StringIO()
        with patch('sys.stdout', captured):
            self.manager.print_output_directory(quiet=True)
        
        output = captured.getvalue()
        self.assertEqual(output, "")
    
    def test_no_output_when_dir_not_created(self):
        """Test no output when directory hasn't been created."""
        manager = OutputManager(base_dir=self.temp_dir)
        
        captured = StringIO()
        with patch('sys.stdout', captured):
            manager.print_output_directory(quiet=False)
        
        output = captured.getvalue()
        self.assertEqual(output, "")


class TestOutputDirectoryErrorFormatting(unittest.TestCase):
    """Tests for OutputDirectoryError message formatting."""
    
    def test_error_contains_path(self):
        """Test error message contains the path."""
        error = OutputDirectoryError(
            path="/test/path",
            reason="Test reason",
            is_custom_dir=True
        )
        
        self.assertIn("/test/path", str(error))
    
    def test_error_contains_reason(self):
        """Test error message contains the reason."""
        error = OutputDirectoryError(
            path="/test/path",
            reason="Permission denied",
            is_custom_dir=True
        )
        
        self.assertIn("Permission denied", str(error))
    
    def test_error_indicates_custom_dir(self):
        """Test error indicates custom --output-dir."""
        error = OutputDirectoryError(
            path="/test/path",
            reason="Test reason",
            is_custom_dir=True
        )
        
        self.assertIn("custom --output-dir", str(error))
    
    def test_error_indicates_default_dir(self):
        """Test error indicates default directory."""
        error = OutputDirectoryError(
            path="/test/path",
            reason="Test reason",
            is_custom_dir=False
        )
        
        self.assertIn("default directory", str(error))
    
    def test_error_attributes_accessible(self):
        """Test error attributes are accessible."""
        error = OutputDirectoryError(
            path="/test/path",
            reason="Test reason",
            is_custom_dir=True
        )
        
        self.assertEqual(error.path, "/test/path")
        self.assertEqual(error.reason, "Test reason")
        self.assertTrue(error.is_custom_dir)


class TestSecureFilePermissions(unittest.TestCase):
    """Tests for secure file permissions (0600) on output files."""
    
    def setUp(self):
        """Create temporary directory for testing."""
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up temporary directories."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    @unittest.skipIf(os.name == 'nt', "Permission tests not reliable on Windows")
    def test_set_secure_file_permissions(self):
        """Test set_secure_file_permissions sets 0600 permissions."""
        # Create a test file with default permissions
        test_file = os.path.join(self.temp_dir, "test_file.txt")
        with open(test_file, 'w') as f:
            f.write("test content")
        
        # Apply secure permissions
        OutputManager.set_secure_file_permissions(test_file)
        
        # Verify permissions are 0600 (owner read/write only)
        file_stat = os.stat(test_file)
        permissions = stat.S_IMODE(file_stat.st_mode)
        expected = stat.S_IRUSR | stat.S_IWUSR  # 0600
        
        self.assertEqual(permissions, expected,
            f"Expected permissions 0600 ({expected:o}), got {permissions:o}")
    
    @unittest.skipIf(os.name == 'nt', "Permission tests not reliable on Windows")
    def test_secure_permissions_removes_group_and_other(self):
        """Test secure permissions remove group and other access."""
        test_file = os.path.join(self.temp_dir, "test_file.txt")
        with open(test_file, 'w') as f:
            f.write("test content")
        
        # Set permissive permissions first
        os.chmod(test_file, 0o777)
        
        # Apply secure permissions
        OutputManager.set_secure_file_permissions(test_file)
        
        # Verify no group or other permissions
        file_stat = os.stat(test_file)
        permissions = stat.S_IMODE(file_stat.st_mode)
        
        # Check no group permissions
        self.assertFalse(permissions & stat.S_IRGRP, "Group read should be removed")
        self.assertFalse(permissions & stat.S_IWGRP, "Group write should be removed")
        self.assertFalse(permissions & stat.S_IXGRP, "Group execute should be removed")
        
        # Check no other permissions
        self.assertFalse(permissions & stat.S_IROTH, "Other read should be removed")
        self.assertFalse(permissions & stat.S_IWOTH, "Other write should be removed")
        self.assertFalse(permissions & stat.S_IXOTH, "Other execute should be removed")
    
    def test_secure_permissions_nonexistent_file(self):
        """Test set_secure_file_permissions handles nonexistent file gracefully."""
        nonexistent = os.path.join(self.temp_dir, "nonexistent.txt")
        
        # Should not raise exception
        OutputManager.set_secure_file_permissions(nonexistent)
    
    @unittest.skipIf(os.name == 'nt', "Permission tests not reliable on Windows")
    def test_output_directory_has_secure_permissions(self):
        """Test output directory is created with 0700 permissions."""
        manager = OutputManager(base_dir=self.temp_dir)
        output_dir = manager.create_output_directory()
        
        # Verify directory permissions are 0700 (owner read/write/execute only)
        dir_stat = os.stat(output_dir)
        permissions = stat.S_IMODE(dir_stat.st_mode)
        expected = stat.S_IRWXU  # 0700
        
        self.assertEqual(permissions, expected,
            f"Expected directory permissions 0700 ({expected:o}), got {permissions:o}")
    
    @unittest.skipIf(os.name == 'nt', "Permission tests not reliable on Windows")
    def test_secure_dir_mode_constant(self):
        """Test SECURE_DIR_MODE constant is 0700."""
        from diagnostic_collector_mysql.output.output_manager import SECURE_DIR_MODE
        
        expected = stat.S_IRWXU  # 0700
        self.assertEqual(SECURE_DIR_MODE, expected)
    
    @unittest.skipIf(os.name == 'nt', "Permission tests not reliable on Windows")
    def test_secure_file_mode_constant(self):
        """Test SECURE_FILE_MODE constant is 0600."""
        from diagnostic_collector_mysql.output.output_manager import SECURE_FILE_MODE
        
        expected = stat.S_IRUSR | stat.S_IWUSR  # 0600
        self.assertEqual(SECURE_FILE_MODE, expected)


if __name__ == '__main__':
    unittest.main()
