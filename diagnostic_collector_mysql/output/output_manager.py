#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Output Manager for Diagnostic Collector for MySQL.

This module handles output directory creation, timestamp generation,
and file path management for all output files.

- Create timestamped output directory
- Validate output directory path
- Generate consistent timestamps across all files
- Set restrictive file permissions (0600) for security
"""

import os
import stat
import sys
from datetime import datetime
from typing import Optional


# Restrictive file permissions: owner read/write only (0600)
SECURE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR

# Restrictive directory permissions: owner read/write/execute only (0700)
SECURE_DIR_MODE = stat.S_IRWXU


class OutputDirectoryError(Exception):
    """
    Exception raised when output directory cannot be created.
    
    Attributes:
        path: The directory path that could not be created
        reason: The reason for failure
        is_custom_dir: Whether a custom --output-dir was specified
    """
    
    def __init__(self, path: str, reason: str, is_custom_dir: bool = False):
        self.path = path
        self.reason = reason
        self.is_custom_dir = is_custom_dir
        super().__init__(self._format_message())
    
    def _format_message(self) -> str:
        """Format the error message with all details."""
        dir_type = "custom --output-dir" if self.is_custom_dir else "default directory"
        return (
            f"Cannot create output directory: {self.path}\n"
            f"Reason: {self.reason}\n"
            f"Directory type: {dir_type}"
        )


class OutputManager:
    """
    Manages output directory creation and file path generation.
    
    This class handles:
    - Timestamp generation in YYYYMMDD_HHMMSS format
    - Output directory creation with validation
    - File path generation for query logs, main log, and reports
    
    Attributes:
        base_dir: The base output directory (--output-dir or current directory)
        timestamp: The timestamp string used for all files in this run
        output_dir: The full path to the timestamped output directory
    """
    
    TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
    OUTPUT_DIR_PREFIX = "diagnostic_collector_mysql_data_"
    MAIN_LOG_PREFIX = "diagnostic_collector_mysql_"
    REPORT_PREFIX = "report_"
    
    # Format extension mapping
    FORMAT_EXTENSIONS = {
        'html': '.html',
        'json': '.json',
        'markdown': '.md',
        'text': '.txt'
    }
    
    def __init__(
        self,
        base_dir: str = '.',
        timestamp: Optional[str] = None
    ):
        """
        Initialize the OutputManager.
        
        Args:
            base_dir: Base output directory path (--output-dir value)
            timestamp: Optional timestamp string. If None, generates current timestamp.
        
        Note:
            The output directory is NOT created during initialization.
            Call create_output_directory() to create it.
        """
        self.base_dir = base_dir
        self._is_custom_dir = base_dir != '.'
        self._timestamp = timestamp or self._generate_timestamp()
        self._output_dir: Optional[str] = None
    
    @property
    def timestamp(self) -> str:
        """
        Get the timestamp string for this run.
        
        Returns:
            Timestamp in YYYYMMDD_HHMMSS format
        """
        return self._timestamp
    
    @property
    def display_timestamp(self) -> str:
        """
        Get the timestamp in human-readable format (similar to MySQL NOW()).
        
        Returns:
            Timestamp in YYYY-MM-DD HH:MM:SS format
        """
        # Convert from YYYYMMDD_HHMMSS to YYYY-MM-DD HH:MM:SS
        ts = self._timestamp
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
    
    @property
    def output_dir(self) -> Optional[str]:
        """
        Get the full path to the timestamped output directory.
        
        Returns:
            Full path to output directory, or None if not yet created
        """
        return self._output_dir
    
    @staticmethod
    def _generate_timestamp() -> str:
        """
        Generate timestamp in YYYYMMDD_HHMMSS format.
        
        Returns:
            Timestamp string
        """
        return datetime.now().strftime(OutputManager.TIMESTAMP_FORMAT)
    
    def validate_base_directory(self) -> None:
        """
        Validate that the base directory exists and is writable.
        If the directory doesn't exist, attempt to create it.
        
        Raises:
            OutputDirectoryError: If base directory can't be created or isn't writable
        
        Validate --output-dir path exists and is writable
        """
        # Check if base directory exists, if not try to create it
        if not os.path.exists(self.base_dir):
            try:
                os.makedirs(self.base_dir, exist_ok=True)
            except PermissionError:
                raise OutputDirectoryError(
                    path=self.base_dir,
                    reason="Cannot create directory (permission denied). Please create the directory manually or choose a different path.",
                    is_custom_dir=self._is_custom_dir
                )
            except OSError as e:
                raise OutputDirectoryError(
                    path=self.base_dir,
                    reason=f"Cannot create directory: {e}",
                    is_custom_dir=self._is_custom_dir
                )
        
        # Check if it's a directory
        if not os.path.isdir(self.base_dir):
            raise OutputDirectoryError(
                path=self.base_dir,
                reason="Path exists but is not a directory",
                is_custom_dir=self._is_custom_dir
            )
        
        # Check if it's writable
        if not os.access(self.base_dir, os.W_OK):
            raise OutputDirectoryError(
                path=self.base_dir,
                reason="Directory is not writable (permission denied). Please ensure you have write permissions.",
                is_custom_dir=self._is_custom_dir
            )
    
    def create_output_directory(self) -> str:
        """
        Create the timestamped output directory.
        
        This method:
        1. Validates the base directory exists and is writable
        2. Creates diagnostic_collector_mysql_data_YYYYMMDD_HHMMSS/ directory
        
        Returns:
            Full path to the created output directory
        
        Raises:
            OutputDirectoryError: If directory cannot be created (exit code 4)
        
        """
        # Validate base directory first
        self.validate_base_directory()
        
        # Create timestamped directory name
        dir_name = f"{self.OUTPUT_DIR_PREFIX}{self._timestamp}"
        full_path = os.path.join(self.base_dir, dir_name)
        
        # Attempt to create the directory with restrictive permissions
        try:
            os.makedirs(full_path, mode=SECURE_DIR_MODE, exist_ok=False)
        except FileExistsError:
            # Directory already exists - this is fine, use it
            if not os.path.isdir(full_path):
                raise OutputDirectoryError(
                    path=full_path,
                    reason="Path exists but is not a directory",
                    is_custom_dir=self._is_custom_dir
                )
            # Ensure existing directory has restrictive permissions
            try:
                os.chmod(full_path, SECURE_DIR_MODE)
            except OSError:
                pass  # Best effort - may fail if not owner
        except PermissionError:
            raise OutputDirectoryError(
                path=full_path,
                reason="Permission denied when creating directory",
                is_custom_dir=self._is_custom_dir
            )
        except OSError as e:
            # Handle other OS errors (disk full, invalid path, etc.)
            raise OutputDirectoryError(
                path=full_path,
                reason=str(e),
                is_custom_dir=self._is_custom_dir
            )
        
        self._output_dir = full_path
        return full_path
    
    def get_query_log_path(self, query_id: str) -> str:
        """
        Get the file path for a query log file.
        
        Args:
            query_id: The query ID from queries.json
        
        Returns:
            Full path to the query log file: {QUERY_ID}_YYYYMMDD_HHMMSS.log
        
        Raises:
            RuntimeError: If output directory hasn't been created yet
        
        """
        if self._output_dir is None:
            raise RuntimeError("Output directory not created. Call create_output_directory() first.")
        
        filename = f"{query_id}_{self._timestamp}.log"
        return os.path.join(self._output_dir, filename)
    
    def get_main_log_path(self) -> str:
        """
        Get the file path for the main execution log.
        
        Returns:
            Full path to main log: diagnostic_collector_mysql_YYYYMMDD_HHMMSS.log
        
        Raises:
            RuntimeError: If output directory hasn't been created yet
        
        """
        if self._output_dir is None:
            raise RuntimeError("Output directory not created. Call create_output_directory() first.")
        
        filename = f"{self.MAIN_LOG_PREFIX}{self._timestamp}.log"
        return os.path.join(self._output_dir, filename)
    
    def get_report_path(self, output_format: str) -> str:
        """
        Get the file path for the report file.
        
        Args:
            output_format: Output format (html, json, markdown, text)
        
        Returns:
            Full path to report: report_YYYYMMDD_HHMMSS.{ext}
        
        Raises:
            RuntimeError: If output directory hasn't been created yet
            ValueError: If output_format is not recognized
        
        """
        if self._output_dir is None:
            raise RuntimeError("Output directory not created. Call create_output_directory() first.")
        
        ext = self.FORMAT_EXTENSIONS.get(output_format)
        if ext is None:
            raise ValueError(
                f"Unknown output format: {output_format}. "
                f"Valid formats: {', '.join(self.FORMAT_EXTENSIONS.keys())}"
            )
        
        filename = f"{self.REPORT_PREFIX}{self._timestamp}{ext}"
        return os.path.join(self._output_dir, filename)
    
    def print_output_directory(self, quiet: bool = False) -> None:
        """
        Print the full output directory path to stdout.
        
        Args:
            quiet: If True, suppress output
        
        """
        if not quiet and self._output_dir:
            print(f"Output directory: {self._output_dir}")
    
    @staticmethod
    def set_secure_file_permissions(filepath: str) -> None:
        """
        Set restrictive permissions (0600) on a file for security.
        
        Output files may contain sensitive information such as:
        - Database user accounts and privileges
        - Connection details and hostnames
        - Password ages and security configurations
        
        This method ensures only the file owner can read/write the file.
        
        Args:
            filepath: Path to the file to secure
            
        Note:
            This is a best-effort operation. It may fail silently if:
            - The file doesn't exist yet
            - The user doesn't have permission to change file mode
            - The filesystem doesn't support Unix permissions (e.g., Windows)
        """
        try:
            if os.path.exists(filepath):
                os.chmod(filepath, SECURE_FILE_MODE)
        except (OSError, AttributeError):
            # Best effort - may fail on Windows or if not owner
            pass
