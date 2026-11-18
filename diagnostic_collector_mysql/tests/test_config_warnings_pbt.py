# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Property-based tests for MySQL Configuration Warnings feature.

This module contains property-based tests using Hypothesis to verify:
1. Warning display: each non-optimal setting produces its configuration warning
2. Preservation: optimal configurations produce no warnings

"""

import unittest
from hypothesis import given, strategies as st, settings, assume
from typing import Dict, Any, List, Tuple, Optional
from unittest.mock import MagicMock, patch
import tempfile
import os
import shutil

from diagnostic_collector_mysql.reporting.report_generator import (
    HTMLReportGenerator,
    ParsedLogFile,
    LogFileParser,
)
from diagnostic_collector_mysql.models import (
    ReportFormat,
    ReportSection,
    ReportSubsection,
)


# ============================================================================
# Configuration Strategies for Property-Based Testing
# ============================================================================

# Strategy for sync_binlog values that trigger warnings (not equal to 1)
sync_binlog_warning_values = st.sampled_from([0, 2, 5, 10, 100])

# Strategy for innodb_flush_log_at_trx_commit values that trigger warnings (not equal to 1)
innodb_flush_warning_values = st.sampled_from([0, 2])

# Strategy for foreign_key_checks values that trigger warnings (OFF)
foreign_key_checks_warning_values = st.just('OFF')

# Strategy for innodb_stats_persistent values that trigger warnings (OFF)
innodb_stats_persistent_warning_values = st.just('OFF')

# Strategy for innodb_stats_auto_recalc values that trigger warnings (OFF)
innodb_stats_auto_recalc_warning_values = st.just('OFF')

# Strategy for binlog_row_image + binlog_format combination that triggers warnings
# (binlog_row_image=FULL AND binlog_format=ROW)
binlog_warning_combination = st.just(('FULL', 'ROW'))

# Strategy for default_storage_engine values that trigger warnings (not InnoDB)
default_storage_engine_warning_values = st.sampled_from(['MyISAM', 'MEMORY', 'CSV', 'ARCHIVE'])

# Strategy for log_output values that trigger warnings (TABLE)
log_output_warning_values = st.just('TABLE')

# Strategy for max_connect_errors values that trigger warnings (> 10000)
max_connect_errors_warning_values = st.integers(min_value=10001, max_value=1000000)


# ============================================================================
# Expected warning messages
# ============================================================================

EXPECTED_WARNINGS = {
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

HEALTH_OVERVIEW_WARNING_TEXT = "Issues detected"


def create_config_warnings_parsed_log(
    sync_binlog: int = 1,
    innodb_flush_log_at_trx_commit: int = 1,
    foreign_key_checks: str = 'ON',
    innodb_stats_persistent: str = 'ON',
    innodb_stats_auto_recalc: str = 'ON',
    binlog_row_image: str = 'MINIMAL',
    binlog_format: str = 'ROW',
    default_storage_engine: str = 'InnoDB',
    log_output: str = 'FILE',
    max_connect_errors: int = 100,
    log_bin: str = 'ON',
    general_log: str = 'OFF',
    slow_query_log: str = 'OFF',
) -> ParsedLogFile:
    """
    Create a ParsedLogFile for config_warnings_check query with specified values.
    
    This simulates the query result that would be returned by the config_warnings_check
    query defined in queries.json.
    """
    columns = [
        'sync_binlog',
        'innodb_flush_log_at_trx_commit',
        'foreign_key_checks',
        'innodb_stats_persistent',
        'innodb_stats_auto_recalc',
        'binlog_row_image',
        'binlog_format',
        'default_storage_engine',
        'log_output',
        'max_connect_errors',
        'log_bin',
        'general_log',
        'slow_query_log',
    ]
    
    rows = [(
        sync_binlog,
        innodb_flush_log_at_trx_commit,
        foreign_key_checks,
        innodb_stats_persistent,
        innodb_stats_auto_recalc,
        binlog_row_image,
        binlog_format,
        default_storage_engine,
        log_output,
        max_connect_errors,
        log_bin,
        general_log,
        slow_query_log,
    )]
    
    return ParsedLogFile(
        query_id='config_warnings_check',
        success=True,
        columns=columns,
        rows=rows,
        error=None,
        executed_timestamp='2026-02-18 14:30:52',
        select_now_timestamp='2026-02-18 14:30:52',
        query_command='SELECT @@sync_binlog AS sync_binlog, ...',
    )


def create_minimal_parsed_logs(config_log: Optional[ParsedLogFile] = None) -> Dict[str, ParsedLogFile]:
    """
    Create minimal parsed logs dictionary for report generation.
    
    Includes basic server info queries that are required for report generation.
    """
    logs = {
        'server_version': ParsedLogFile(
            query_id='server_version',
            success=True,
            columns=['version'],
            rows=[('8.0.35',)],
            error=None,
            executed_timestamp='2026-02-18 14:30:52',
        ),
        'server_hostname': ParsedLogFile(
            query_id='server_hostname',
            success=True,
            columns=['hostname'],
            rows=[('test-server',)],
            error=None,
            executed_timestamp='2026-02-18 14:30:52',
        ),
    }
    
    if config_log:
        logs['config_warnings_check'] = config_log
    
    return logs


def create_test_report_format() -> ReportFormat:
    """
    Create a minimal ReportFormat for testing configuration warnings.
    
    This includes:
    - Section 1.5 with Configuration Warnings subsection (where warnings should appear)
    - Basic server info section
    """
    return ReportFormat(
        sections=[
            ReportSection(
                name="Server Information",
                number="1",
                short_name="Server Info",
                subsections=[
                    ReportSubsection(
                        name="Server Details",
                        source="server_version",
                        columns=["version"]
                    ),
                ]
            ),
            ReportSection(
                name="Non-Default Configuration Variables",
                number="1.5",
                short_name="Config Variables",
                subsections=[
                    ReportSubsection(
                        name="Configuration Warnings",
                        source="config_warnings_check",
                        columns=["Parameter", "Message"],
                        condition={"engine": ["mysql", "aurora-mysql"]},
                        default_note="No configuration warnings - all checked parameters are optimally configured"
                    ),
                ]
            ),
        ]
    )


# ============================================================================
# Warning Display Property Tests
# ============================================================================

class TestConfigurationWarningDisplay(unittest.TestCase):
    """
    Property-based tests that non-optimal settings produce configuration warnings.
    
    **Property 1: Warning display** - each non-optimal value shows its warning.
    
    """
    
    def setUp(self):
        """Set up test fixtures."""
        self.report_format = create_test_report_format()
    
    @given(sync_binlog=sync_binlog_warning_values)
    @settings(max_examples=5, deadline=None)
    def test_sync_binlog_warning_displayed(self, sync_binlog: int):
        """
        
        Property: When sync_binlog != 1, the report SHOULD contain the expected warning message.
        """
        # Create config with sync_binlog != 1 (warning condition)
        config_log = create_config_warnings_parsed_log(sync_binlog=sync_binlog)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        # Generate HTML report
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        # Expected behavior: Warning message should be present
        expected_warning = EXPECTED_WARNINGS['sync_binlog']
        
        # Assert the warning IS displayed
        self.assertIn(
            expected_warning,
            report_html,
            f"sync_binlog={sync_binlog} should trigger warning but doesn't. "
            f"Expected warning: '{expected_warning}'"
        )
    
    @given(innodb_flush=innodb_flush_warning_values)
    @settings(max_examples=3, deadline=None)
    def test_innodb_flush_log_warning_displayed(self, innodb_flush: int):
        """
        
        Property: When innodb_flush_log_at_trx_commit != 1, the report SHOULD contain the expected warning.
        """
        config_log = create_config_warnings_parsed_log(innodb_flush_log_at_trx_commit=innodb_flush)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['innodb_flush_log_at_trx_commit']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"innodb_flush_log_at_trx_commit={innodb_flush} should trigger warning but doesn't."
        )
    
    @given(foreign_key_checks=foreign_key_checks_warning_values)
    @settings(max_examples=1, deadline=None)
    def test_foreign_key_checks_warning_displayed(self, foreign_key_checks: str):
        """
        
        Property: When foreign_key_checks = OFF, the report SHOULD contain the expected warning.
        """
        config_log = create_config_warnings_parsed_log(foreign_key_checks=foreign_key_checks)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['foreign_key_checks']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"foreign_key_checks={foreign_key_checks} should trigger warning but doesn't."
        )
    
    @given(innodb_stats_persistent=innodb_stats_persistent_warning_values)
    @settings(max_examples=1, deadline=None)
    def test_innodb_stats_persistent_warning_displayed(self, innodb_stats_persistent: str):
        """
        
        Property: When innodb_stats_persistent = OFF, the report SHOULD contain the expected warning.
        """
        config_log = create_config_warnings_parsed_log(innodb_stats_persistent=innodb_stats_persistent)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['innodb_stats_persistent']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"innodb_stats_persistent={innodb_stats_persistent} should trigger warning but doesn't."
        )
    
    @given(innodb_stats_auto_recalc=innodb_stats_auto_recalc_warning_values)
    @settings(max_examples=1, deadline=None)
    def test_innodb_stats_auto_recalc_warning_displayed(self, innodb_stats_auto_recalc: str):
        """
        
        Property: When innodb_stats_auto_recalc = OFF, the report SHOULD contain the expected warning.
        """
        config_log = create_config_warnings_parsed_log(innodb_stats_auto_recalc=innodb_stats_auto_recalc)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['innodb_stats_auto_recalc']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"innodb_stats_auto_recalc={innodb_stats_auto_recalc} should trigger warning but doesn't."
        )
    
    @given(binlog_combo=binlog_warning_combination)
    @settings(max_examples=1, deadline=None)
    def test_binlog_row_image_warning_displayed(self, binlog_combo: Tuple[str, str]):
        """
        
        Property: When binlog_row_image=FULL AND binlog_format=ROW AND log_bin=ON, the report SHOULD contain the expected warning.
        """
        binlog_row_image, binlog_format = binlog_combo
        config_log = create_config_warnings_parsed_log(
            binlog_row_image=binlog_row_image,
            binlog_format=binlog_format,
            log_bin='ON'
        )
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['binlog_row_image']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"binlog_row_image={binlog_row_image} with binlog_format={binlog_format} and log_bin=ON should trigger warning but doesn't."
        )
    
    @given(default_storage_engine=default_storage_engine_warning_values)
    @settings(max_examples=3, deadline=None)
    def test_default_storage_engine_warning_displayed(self, default_storage_engine: str):
        """
        
        Property: When default_storage_engine != InnoDB, the report SHOULD contain the expected warning.
        """
        config_log = create_config_warnings_parsed_log(default_storage_engine=default_storage_engine)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['default_storage_engine']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"default_storage_engine={default_storage_engine} should trigger warning but doesn't."
        )
    
    @given(log_output=log_output_warning_values)
    @settings(max_examples=1, deadline=None)
    def test_log_output_warning_displayed(self, log_output: str):
        """
        
        Property: When log_output = TABLE and general_log or slow_query_log is enabled,
        the report SHOULD contain the expected warning.
        """
        config_log = create_config_warnings_parsed_log(log_output=log_output, general_log='ON')
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['log_output']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"log_output={log_output} should trigger warning but doesn't."
        )
    
    @given(max_connect_errors=max_connect_errors_warning_values)
    @settings(max_examples=3, deadline=None)
    def test_max_connect_errors_warning_displayed(self, max_connect_errors: int):
        """
        
        Property: When max_connect_errors > 10000, the report SHOULD contain the expected warning.
        """
        config_log = create_config_warnings_parsed_log(max_connect_errors=max_connect_errors)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        expected_warning = EXPECTED_WARNINGS['max_connect_errors']
        
        self.assertIn(
            expected_warning,
            report_html,
            f"max_connect_errors={max_connect_errors} should trigger warning but doesn't."
        )
    
    @given(sync_binlog=sync_binlog_warning_values)
    @settings(max_examples=3, deadline=None)
    def test_health_overview_shows_warning_status(self, sync_binlog: int):
        """
        
        Property: When any configuration warning exists, Health Overview SHOULD show 
        "Server Configurations need attention" status.
        """
        config_log = create_config_warnings_parsed_log(sync_binlog=sync_binlog)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        self.assertIn(
            HEALTH_OVERVIEW_WARNING_TEXT,
            report_html,
            f"Health Overview should show '{HEALTH_OVERVIEW_WARNING_TEXT}' when sync_binlog={sync_binlog} but doesn't."
        )


# ============================================================================
# Preservation Property Tests
# ============================================================================

# Strategies for optimal (non-warning) configuration values
# These values should NOT trigger any warnings

# Strategy for sync_binlog optimal value (equals 1)
sync_binlog_optimal_values = st.just(1)

# Strategy for innodb_flush_log_at_trx_commit optimal value (equals 1)
innodb_flush_optimal_values = st.just(1)

# Strategy for foreign_key_checks optimal value (ON)
foreign_key_checks_optimal_values = st.just('ON')

# Strategy for innodb_stats_persistent optimal value (ON)
innodb_stats_persistent_optimal_values = st.just('ON')

# Strategy for innodb_stats_auto_recalc optimal value (ON)
innodb_stats_auto_recalc_optimal_values = st.just('ON')

# Strategy for binlog_row_image values that do NOT trigger warnings
# (anything except FULL when binlog_format is ROW)
binlog_row_image_optimal_values = st.sampled_from(['MINIMAL', 'NOBLOB'])

# Strategy for binlog_format values that do NOT trigger warnings when binlog_row_image is FULL
binlog_format_optimal_values = st.sampled_from(['STATEMENT', 'MIXED'])

# Strategy for default_storage_engine optimal value (InnoDB)
default_storage_engine_optimal_values = st.just('InnoDB')

# Strategy for log_output values that do NOT trigger warnings (not TABLE)
log_output_optimal_values = st.sampled_from(['FILE', 'NONE'])

# Strategy for max_connect_errors optimal values (<= 10000)
max_connect_errors_optimal_values = st.integers(min_value=1, max_value=10000)


class TestPreservationProperty(unittest.TestCase):
    """
    Property-based tests to verify preservation behavior.
    
    **Property 2: Preservation** - Optimal Configuration No Warnings
    
    These tests verify that when all 9 parameters are optimally configured,
    no configuration warnings should appear in the report.
    
    """
    
    def setUp(self):
        """Set up test fixtures."""
        self.report_format = create_test_report_format()
    
    @given(sync_binlog=sync_binlog_optimal_values)
    @settings(max_examples=1, deadline=None)
    def test_sync_binlog_optimal_no_warning(self, sync_binlog: int):
        """
        
        Property: When sync_binlog=1, no warning is displayed for sync_binlog.
        """
        config_log = create_config_warnings_parsed_log(sync_binlog=sync_binlog)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        # The sync_binlog warning should NOT appear
        warning_text = EXPECTED_WARNINGS['sync_binlog']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: sync_binlog={sync_binlog} should NOT trigger warning."
        )
    
    @given(innodb_flush=innodb_flush_optimal_values)
    @settings(max_examples=1, deadline=None)
    def test_innodb_flush_optimal_no_warning(self, innodb_flush: int):
        """
        
        Property: When innodb_flush_log_at_trx_commit=1, no warning is displayed.
        """
        config_log = create_config_warnings_parsed_log(innodb_flush_log_at_trx_commit=innodb_flush)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['innodb_flush_log_at_trx_commit']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: innodb_flush_log_at_trx_commit={innodb_flush} should NOT trigger warning."
        )
    
    @given(foreign_key_checks=foreign_key_checks_optimal_values)
    @settings(max_examples=1, deadline=None)
    def test_foreign_key_checks_optimal_no_warning(self, foreign_key_checks: str):
        """
        
        Property: When foreign_key_checks=ON, no warning is displayed.
        """
        config_log = create_config_warnings_parsed_log(foreign_key_checks=foreign_key_checks)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['foreign_key_checks']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: foreign_key_checks={foreign_key_checks} should NOT trigger warning."
        )
    
    @given(innodb_stats_persistent=innodb_stats_persistent_optimal_values)
    @settings(max_examples=1, deadline=None)
    def test_innodb_stats_persistent_optimal_no_warning(self, innodb_stats_persistent: str):
        """
        
        Property: When innodb_stats_persistent=ON, no warning is displayed.
        """
        config_log = create_config_warnings_parsed_log(innodb_stats_persistent=innodb_stats_persistent)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['innodb_stats_persistent']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: innodb_stats_persistent={innodb_stats_persistent} should NOT trigger warning."
        )
    
    @given(innodb_stats_auto_recalc=innodb_stats_auto_recalc_optimal_values)
    @settings(max_examples=1, deadline=None)
    def test_innodb_stats_auto_recalc_optimal_no_warning(self, innodb_stats_auto_recalc: str):
        """
        
        Property: When innodb_stats_auto_recalc=ON, no warning is displayed.
        """
        config_log = create_config_warnings_parsed_log(innodb_stats_auto_recalc=innodb_stats_auto_recalc)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['innodb_stats_auto_recalc']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: innodb_stats_auto_recalc={innodb_stats_auto_recalc} should NOT trigger warning."
        )
    
    @given(binlog_row_image=binlog_row_image_optimal_values)
    @settings(max_examples=2, deadline=None)
    def test_binlog_row_image_not_full_no_warning(self, binlog_row_image: str):
        """
        
        Property: When binlog_row_image != FULL, no warning is displayed for binary log settings.
        """
        # binlog_format can be ROW, but binlog_row_image is not FULL
        config_log = create_config_warnings_parsed_log(
            binlog_row_image=binlog_row_image,
            binlog_format='ROW'
        )
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['binlog_row_image']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: binlog_row_image={binlog_row_image} with binlog_format=ROW should NOT trigger warning."
        )
    
    @given(binlog_format=binlog_format_optimal_values)
    @settings(max_examples=2, deadline=None)
    def test_binlog_format_not_row_no_warning(self, binlog_format: str):
        """
        
        Property: When binlog_format != ROW, no warning is displayed for binary log settings
        even if binlog_row_image is FULL.
        """
        # binlog_row_image is FULL, but binlog_format is not ROW
        config_log = create_config_warnings_parsed_log(
            binlog_row_image='FULL',
            binlog_format=binlog_format,
            log_bin='ON'
        )
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['binlog_row_image']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: binlog_row_image=FULL with binlog_format={binlog_format} should NOT trigger warning."
        )
    
    def test_binlog_disabled_no_warning(self):
        """
        
        Property: When log_bin=OFF, no warning is displayed for binary log settings
        even if binlog_row_image=FULL and binlog_format=ROW.
        """
        # binlog_row_image is FULL and binlog_format is ROW, but log_bin is OFF
        config_log = create_config_warnings_parsed_log(
            binlog_row_image='FULL',
            binlog_format='ROW',
            log_bin='OFF'
        )
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['binlog_row_image']
        
        self.assertNotIn(
            warning_text,
            report_html,
            "Preservation violated: binlog_row_image=FULL with binlog_format=ROW but log_bin=OFF should NOT trigger warning."
        )
    
    def test_log_output_disabled_no_warning(self):
        """
                
        Property: When log_output=TABLE but both general_log=OFF and slow_query_log=OFF,
        no warning is displayed because logging to TABLE has no impact when logging is disabled.
        """
        # log_output is TABLE, but both general_log and slow_query_log are OFF
        config_log = create_config_warnings_parsed_log(
            log_output='TABLE',
            general_log='OFF',
            slow_query_log='OFF'
        )
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['log_output']
        
        self.assertNotIn(
            warning_text,
            report_html,
            "Preservation violated: log_output=TABLE with general_log=OFF and slow_query_log=OFF should NOT trigger warning."
        )
    
    @given(default_storage_engine=default_storage_engine_optimal_values)
    @settings(max_examples=1, deadline=None)
    def test_default_storage_engine_innodb_no_warning(self, default_storage_engine: str):
        """
        
        Property: When default_storage_engine=InnoDB, no warning is displayed.
        """
        config_log = create_config_warnings_parsed_log(default_storage_engine=default_storage_engine)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['default_storage_engine']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: default_storage_engine={default_storage_engine} should NOT trigger warning."
        )
    
    @given(log_output=log_output_optimal_values)
    @settings(max_examples=2, deadline=None)
    def test_log_output_not_table_no_warning(self, log_output: str):
        """
        
        Property: When log_output != TABLE, no warning is displayed.
        """
        config_log = create_config_warnings_parsed_log(log_output=log_output)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['log_output']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: log_output={log_output} should NOT trigger warning."
        )
    
    @given(max_connect_errors=max_connect_errors_optimal_values)
    @settings(max_examples=5, deadline=None)
    def test_max_connect_errors_optimal_no_warning(self, max_connect_errors: int):
        """
        
        Property: When max_connect_errors <= 10000, no warning is displayed.
        """
        config_log = create_config_warnings_parsed_log(max_connect_errors=max_connect_errors)
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        warning_text = EXPECTED_WARNINGS['max_connect_errors']
        
        self.assertNotIn(
            warning_text,
            report_html,
            f"Preservation violated: max_connect_errors={max_connect_errors} should NOT trigger warning."
        )
    
    @given(
        sync_binlog=sync_binlog_optimal_values,
        innodb_flush=innodb_flush_optimal_values,
        foreign_key_checks=foreign_key_checks_optimal_values,
        innodb_stats_persistent=innodb_stats_persistent_optimal_values,
        innodb_stats_auto_recalc=innodb_stats_auto_recalc_optimal_values,
        binlog_row_image=binlog_row_image_optimal_values,
        default_storage_engine=default_storage_engine_optimal_values,
        log_output=log_output_optimal_values,
        max_connect_errors=max_connect_errors_optimal_values,
    )
    @settings(max_examples=10, deadline=None)
    def test_all_optimal_no_warnings(
        self,
        sync_binlog: int,
        innodb_flush: int,
        foreign_key_checks: str,
        innodb_stats_persistent: str,
        innodb_stats_auto_recalc: str,
        binlog_row_image: str,
        default_storage_engine: str,
        log_output: str,
        max_connect_errors: int,
    ):
        """
        
        Property: When ALL 9 parameters are optimally configured, NO configuration 
        warnings should appear in the report.
        
        Preservation: This is the core preservation property - optimal configs should
        never generate warnings.
        """
        config_log = create_config_warnings_parsed_log(
            sync_binlog=sync_binlog,
            innodb_flush_log_at_trx_commit=innodb_flush,
            foreign_key_checks=foreign_key_checks,
            innodb_stats_persistent=innodb_stats_persistent,
            innodb_stats_auto_recalc=innodb_stats_auto_recalc,
            binlog_row_image=binlog_row_image,
            binlog_format='ROW',  # ROW is fine when binlog_row_image is not FULL
            default_storage_engine=default_storage_engine,
            log_output=log_output,
            max_connect_errors=max_connect_errors,
        )
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        # None of the warning messages should appear
        for param, warning_text in EXPECTED_WARNINGS.items():
            self.assertNotIn(
                warning_text,
                report_html,
                f"Preservation violated: Optimal configuration should NOT trigger {param} warning."
            )
    
    def test_existing_sections_preserved(self):
        """
        
        Property: When generating reports, all existing sections continue to display correctly.
        
        Preservation: Configuration warnings do not affect the other report sections.
        """
        # Create optimal config (no warnings)
        config_log = create_config_warnings_parsed_log()
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        # Verify basic report structure is present
        self.assertIn("Server Information", report_html)
        self.assertIn("Non-Default Configuration Variables", report_html)
        
        # Verify the report is valid HTML
        self.assertIn("<!DOCTYPE html>", report_html)
        self.assertIn("</html>", report_html)
    
    def test_health_overview_ok_status_when_no_warnings(self):
        """
        
        Property: When no configuration warnings are detected, the Health Overview
        should NOT show "Server Configurations need attention" status.
        
        Preservation: Clean configurations should not trigger health warnings.
        """
        # Create optimal config (no warnings)
        config_log = create_config_warnings_parsed_log()
        parsed_logs = create_minimal_parsed_logs(config_log)
        
        generator = HTMLReportGenerator(
            report_format=self.report_format,
            timestamp="2026-02-18 14:30:52"
        )
        report_html = generator.generate(parsed_logs, engine_type='mysql')
        
        # The warning status should NOT appear for optimal config
        self.assertNotIn(
            HEALTH_OVERVIEW_WARNING_TEXT,
            report_html,
            "Preservation violated: Health Overview should NOT show warning status for optimal configuration."
        )


if __name__ == '__main__':
    unittest.main()
