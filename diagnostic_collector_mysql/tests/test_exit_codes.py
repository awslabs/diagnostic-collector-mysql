"""
Unit tests for exit code handling.

Tests validate:
- Exit with code 0 on successful completion
- Exit with code 1 for invalid command line arguments
- Exit with code 2 for database connection failures
- Exit with code 3 for input file errors
- Exit with code 4 for output directory errors
- Exit with code 5 for back-off threshold exceeded
- Exit with code 6 for partial completion
- Exit with code 6 when interrupted (SIGINT/SIGTERM)
"""

import signal
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

from diagnostic_collector_mysql.models import ExitCode, BackOffError
from diagnostic_collector_mysql.cli import main_cli, validate_arguments


class TestExitCodeConstants(unittest.TestCase):
    """Tests for ExitCode constant values."""
    
    def test_exit_code_success_is_zero(self):
        """Test that SUCCESS exit code is 0."""
        self.assertEqual(ExitCode.SUCCESS, 0)
    
    def test_exit_code_invalid_arguments_is_one(self):
        """Test that INVALID_ARGUMENTS exit code is 1."""
        self.assertEqual(ExitCode.INVALID_ARGUMENTS, 1)
    
    def test_exit_code_connection_failure_is_two(self):
        """Test that CONNECTION_FAILURE exit code is 2."""
        self.assertEqual(ExitCode.CONNECTION_FAILURE, 2)
    
    def test_exit_code_input_file_error_is_three(self):
        """Test that INPUT_FILE_ERROR exit code is 3."""
        self.assertEqual(ExitCode.INPUT_FILE_ERROR, 3)
    
    def test_exit_code_output_directory_error_is_four(self):
        """Test that OUTPUT_DIRECTORY_ERROR exit code is 4."""
        self.assertEqual(ExitCode.OUTPUT_DIRECTORY_ERROR, 4)
    
    def test_exit_code_backoff_exceeded_is_five(self):
        """Test that BACKOFF_EXCEEDED exit code is 5."""
        self.assertEqual(ExitCode.BACKOFF_EXCEEDED, 5)
    
    def test_exit_code_partial_completion_is_six(self):
        """Test that PARTIAL_COMPLETION exit code is 6."""
        self.assertEqual(ExitCode.PARTIAL_COMPLETION, 6)
    
    def test_all_exit_codes_are_unique(self):
        """Test that all exit codes have unique values."""
        exit_codes = [
            ExitCode.SUCCESS,
            ExitCode.INVALID_ARGUMENTS,
            ExitCode.CONNECTION_FAILURE,
            ExitCode.INPUT_FILE_ERROR,
            ExitCode.OUTPUT_DIRECTORY_ERROR,
            ExitCode.BACKOFF_EXCEEDED,
            ExitCode.PARTIAL_COMPLETION,
        ]
        self.assertEqual(len(exit_codes), len(set(exit_codes)))
    
    def test_exit_codes_are_in_valid_range(self):
        """Test that all exit codes are in valid Unix range (0-255)."""
        exit_codes = [
            ExitCode.SUCCESS,
            ExitCode.INVALID_ARGUMENTS,
            ExitCode.CONNECTION_FAILURE,
            ExitCode.INPUT_FILE_ERROR,
            ExitCode.OUTPUT_DIRECTORY_ERROR,
            ExitCode.BACKOFF_EXCEEDED,
            ExitCode.PARTIAL_COMPLETION,
        ]
        for code in exit_codes:
            self.assertGreaterEqual(code, 0)
            self.assertLessEqual(code, 255)


class TestCLIExitCodes(unittest.TestCase):
    """Tests for CLI argument parsing exit codes."""
    
    def test_missing_required_argument_returns_exit_code_1(self):
        """Test that missing required arguments return exit code 1."""
        # Missing --user
        args, exit_code = main_cli(['--password', 'pass', '--host', 'localhost'])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    def test_missing_password_returns_exit_code_1(self):
        """Test that missing --password returns exit code 1."""
        args, exit_code = main_cli(['--user', 'user', '--host', 'localhost'])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    def test_missing_host_returns_exit_code_1(self):
        """Test that missing --host returns exit code 1."""
        args, exit_code = main_cli(['--user', 'user', '--password', 'pass'])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    def test_invalid_port_returns_exit_code_1(self):
        """Test that invalid port value returns exit code 1."""
        args, exit_code = main_cli([
            '--user', 'user', '--password', 'pass', '--host', 'localhost',
            '--port', '0'
        ])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    def test_invalid_port_too_high_returns_exit_code_1(self):
        """Test that port > 65535 returns exit code 1."""
        args, exit_code = main_cli([
            '--user', 'user', '--password', 'pass', '--host', 'localhost',
            '--port', '70000'
        ])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    def test_negative_timeout_returns_exit_code_1(self):
        """Test that negative timeout returns exit code 1."""
        args, exit_code = main_cli([
            '--user', 'user', '--password', 'pass', '--host', 'localhost',
            '--connect-timeout', '-1'
        ])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    def test_negative_collection_limit_returns_exit_code_1(self):
        """Test that negative collection limit returns exit code 1."""
        args, exit_code = main_cli([
            '--user', 'user', '--password', 'pass', '--host', 'localhost',
            '--collection-limit', '-5'
        ])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    def test_valid_arguments_returns_exit_code_0(self):
        """Test that valid arguments return exit code 0."""
        args, exit_code = main_cli([
            '--user', 'user', '--password', 'pass', '--host', 'localhost'
        ])
        self.assertIsNotNone(args)
        self.assertEqual(exit_code, ExitCode.SUCCESS)
    
    def test_help_returns_exit_code_0(self):
        """Test that --help returns exit code 0."""
        args, exit_code = main_cli(['--help'])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.SUCCESS)
    
    def test_version_returns_exit_code_0(self):
        """Test that --version returns exit code 0."""
        args, exit_code = main_cli(['--version'])
        self.assertIsNone(args)
        self.assertEqual(exit_code, ExitCode.SUCCESS)


class TestBackOffErrorExitCode(unittest.TestCase):
    """Tests for BackOffError and exit code 5."""
    
    def test_backoff_error_stores_reason(self):
        """Test that BackOffError stores the reason correctly."""
        error = BackOffError("Connection count exceeded", 1500, 1000)
        self.assertEqual(error.reason, "Connection count exceeded")
    
    def test_backoff_error_stores_current_value(self):
        """Test that BackOffError stores the current value correctly."""
        error = BackOffError("Connection count exceeded", 1500, 1000)
        self.assertEqual(error.current_value, 1500)
    
    def test_backoff_error_stores_threshold(self):
        """Test that BackOffError stores the threshold correctly."""
        error = BackOffError("Connection count exceeded", 1500, 1000)
        self.assertEqual(error.threshold, 1000)
    
    def test_backoff_error_message_format(self):
        """Test that BackOffError message contains all relevant info."""
        error = BackOffError("Connection count exceeded", 1500, 1000)
        message = str(error)
        self.assertIn("Connection count exceeded", message)
        self.assertIn("1500", message)
        self.assertIn("1000", message)
    
    def test_backoff_error_for_table_count(self):
        """Test BackOffError for table count threshold."""
        error = BackOffError("Table count exceeded", 15000, 10000)
        self.assertEqual(error.reason, "Table count exceeded")
        self.assertEqual(error.current_value, 15000)
        self.assertEqual(error.threshold, 10000)


class TestMainEntryPointExitCodes(unittest.TestCase):
    """Tests for main() function exit codes."""
    
    @patch('diagnostic_collector_mysql.__main__.main_cli')
    def test_main_returns_invalid_arguments_on_cli_error(self, mock_main_cli):
        """Test that main() returns INVALID_ARGUMENTS when CLI parsing fails."""
        mock_main_cli.return_value = (None, ExitCode.INVALID_ARGUMENTS)
        
        from diagnostic_collector_mysql.__main__ import main
        exit_code = main()
        
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
    
    @patch('diagnostic_collector_mysql.__main__.main_cli')
    @patch('diagnostic_collector_mysql.__main__.InputValidator')
    @patch('diagnostic_collector_mysql.__main__.OutputManager')
    def test_main_returns_success_on_valid_args(self, mock_output_manager, mock_validator, mock_main_cli):
        """Test that main() returns SUCCESS with valid args in dry-run mode."""
        mock_args = MagicMock()
        mock_args.progress = False
        mock_args.dry_run = True  # Use dry-run to avoid database connection
        mock_args.verbose = False
        mock_args.output_dir = '.'
        mock_main_cli.return_value = (mock_args, ExitCode.SUCCESS)
        
        # Mock InputValidator
        mock_validator_instance = MagicMock()
        mock_validator_instance.validate_queries_json.return_value = ([], [])
        mock_validator_instance.validate_health_check_references.return_value = []
        mock_validator_instance.validate_report_format_json.return_value = ([], MagicMock(sections=[]))
        mock_validator.return_value = mock_validator_instance
        
        # Mock OutputManager
        mock_output_manager_instance = MagicMock()
        mock_output_manager_instance.create_output_directory.return_value = '/tmp/test_output'
        mock_output_manager.return_value = mock_output_manager_instance
        
        # Mock get_enabled_sections to return None (all sections)
        with patch('diagnostic_collector_mysql.__main__.get_enabled_sections', return_value=None):
            with patch('diagnostic_collector_mysql.__main__.is_interrupted', return_value=False):
                from diagnostic_collector_mysql.__main__ import main
                exit_code = main()
        
        self.assertEqual(exit_code, ExitCode.SUCCESS)
    
    @patch('diagnostic_collector_mysql.__main__.main_cli')
    @patch('diagnostic_collector_mysql.__main__.InputValidator')
    def test_main_returns_input_file_error_on_missing_queries(self, mock_validator, mock_main_cli):
        """Test that main() returns INPUT_FILE_ERROR when queries.json is missing."""
        mock_args = MagicMock()
        mock_args.progress = False
        mock_args.dry_run = False
        mock_args.verbose = False
        mock_main_cli.return_value = (mock_args, ExitCode.SUCCESS)
        
        # Mock InputValidator to return errors
        mock_validator_instance = MagicMock()
        mock_validator_instance.validate_queries_json.return_value = (
            [MagicMock(file='queries.json', location='file', message='File not found')],
            []
        )
        mock_validator.return_value = mock_validator_instance
        
        with patch('diagnostic_collector_mysql.__main__.get_enabled_sections', return_value=None):
            from diagnostic_collector_mysql.__main__ import main
            exit_code = main()
        
        self.assertEqual(exit_code, ExitCode.INPUT_FILE_ERROR)


class TestSignalHandling(unittest.TestCase):
    """Tests for signal handling and interrupt behavior."""
    
    def test_signal_handler_sets_interrupted_flag(self):
        """Test that signal handler sets the interrupted flag."""
        from diagnostic_collector_mysql.__main__ import _signal_handler, is_interrupted
        import diagnostic_collector_mysql.__main__ as mc
        
        # Reset the flag
        mc._interrupted = False
        self.assertFalse(is_interrupted())
        
        # Simulate signal
        _signal_handler(2, None)  # SIGINT = 2
        
        self.assertTrue(is_interrupted())
        
        # Reset for other tests
        mc._interrupted = False
    
    def test_is_interrupted_returns_global_flag(self):
        """Test that is_interrupted() returns the global flag value."""
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import is_interrupted
        
        mc._interrupted = False
        self.assertFalse(is_interrupted())
        
        mc._interrupted = True
        self.assertTrue(is_interrupted())
        
        # Reset for other tests
        mc._interrupted = False
    
    def test_first_signal_invokes_kill_callback(self):
        """First interrupt fires the registered kill callback on a thread."""
        import threading
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import (
            _signal_handler, register_interrupt_kill_callback
        )
        
        called = threading.Event()
        mc._interrupted = False
        register_interrupt_kill_callback(lambda: called.set() or True)
        try:
            _signal_handler(2, None)
            self.assertTrue(mc._interrupted)
            self.assertTrue(
                called.wait(timeout=5),
                "kill callback was not invoked within 5s"
            )
        finally:
            register_interrupt_kill_callback(None)
            mc._interrupted = False
    
    def test_second_signal_exits_with_partial_completion_code(self):
        """A second interrupt exits with the documented interrupt code and
        no traceback. With the socket aborted successfully, cleanup is safe
        to run, so it unwinds via SystemExit."""
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import (
            _signal_handler, register_interrupt_kill_callback
        )
        
        register_interrupt_kill_callback(lambda: False, lambda: True)
        mc._interrupted = True
        try:
            with patch(
                'diagnostic_collector_mysql.__main__.signal.signal'
            ) as mock_signal:
                with self.assertRaises(SystemExit) as ctx:
                    _signal_handler(2, None)
                self.assertEqual(ctx.exception.code, ExitCode.PARTIAL_COMPLETION)
                # Both SIGINT and SIGTERM are re-pointed at the hard exit,
                # so a second SIGTERM cannot re-enter this branch.
                registered = {
                    call.args[0] for call in mock_signal.call_args_list
                }
                self.assertEqual(
                    registered, {signal.SIGINT, signal.SIGTERM}
                )
        finally:
            register_interrupt_kill_callback(None)
            mc._interrupted = False
    
    def test_second_signal_aborts_connection_before_exit(self):
        """The second interrupt aborts the socket first, so cleanup cannot
        block waiting on the statement it is escaping."""
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import (
            _signal_handler, register_interrupt_kill_callback
        )
        
        aborted = []
        register_interrupt_kill_callback(
            lambda: False, lambda: (aborted.append(True), True)[1]
        )
        mc._interrupted = True
        try:
            with patch('diagnostic_collector_mysql.__main__.signal.signal'):
                with self.assertRaises(SystemExit) as ctx:
                    _signal_handler(2, None)
            self.assertEqual(aborted, [True])
            self.assertEqual(ctx.exception.code, ExitCode.PARTIAL_COMPLETION)
        finally:
            register_interrupt_kill_callback(None)
            mc._interrupted = False
    
    def test_second_signal_hard_exits_when_abort_fails(self):
        """When the socket cannot be aborted, cleanup could block on the
        in-flight statement, so the process exits without it."""
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import (
            _signal_handler, register_interrupt_kill_callback
        )
        
        register_interrupt_kill_callback(lambda: False, lambda: False)
        mc._interrupted = True
        try:
            with patch('diagnostic_collector_mysql.__main__.signal.signal'):
                with patch('diagnostic_collector_mysql.__main__.os._exit') as mock_exit:
                    # os._exit is patched, so the handler continues and
                    # raises; the assertion of record is the exit call.
                    with self.assertRaises(SystemExit):
                        _signal_handler(2, None)
                    mock_exit.assert_called_once_with(
                        ExitCode.PARTIAL_COMPLETION
                    )
        finally:
            register_interrupt_kill_callback(None)
            mc._interrupted = False
    
    def test_register_clears_abort_callback_by_default(self):
        """Passing only the kill callback clears the abort callback, so
        callers that need it across a blocking close must re-pass it."""
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import register_interrupt_kill_callback
        
        try:
            register_interrupt_kill_callback(lambda: True, lambda: True)
            self.assertIsNotNone(mc._abort_connection_callback)
            
            register_interrupt_kill_callback(None)
            self.assertIsNone(mc._kill_query_callback)
            self.assertIsNone(mc._abort_connection_callback)
            
            # The cleanup ordering keeps abort available while close() runs.
            register_interrupt_kill_callback(None, lambda: True)
            self.assertIsNone(mc._kill_query_callback)
            self.assertIsNotNone(mc._abort_connection_callback)
        finally:
            register_interrupt_kill_callback(None)
    
    def test_first_signal_without_callback_only_sets_flag(self):
        """With no callback registered, the first interrupt just sets the
        flag (pre-connection behavior)."""
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import (
            _signal_handler, register_interrupt_kill_callback
        )
        
        register_interrupt_kill_callback(None)
        mc._interrupted = False
        try:
            _signal_handler(2, None)
            self.assertTrue(mc._interrupted)
        finally:
            mc._interrupted = False
    
    def test_first_signal_message_matches_state(self):
        """The first-signal message promises a query cancellation and a
        partial report only when a connection exists to cancel on."""
        import io
        import diagnostic_collector_mysql.__main__ as mc
        from diagnostic_collector_mysql.__main__ import (
            _signal_handler, register_interrupt_kill_callback
        )
        
        # Pre-connection: nothing to cancel, nothing collected to report.
        register_interrupt_kill_callback(None)
        mc._interrupted = False
        try:
            stderr = io.StringIO()
            with patch('diagnostic_collector_mysql.__main__.sys.stderr', stderr):
                _signal_handler(2, None)
            message = stderr.getvalue()
            self.assertIn('stopping', message)
            self.assertNotIn('cancelling the running query', message)
            self.assertNotIn('partial report', message)
        finally:
            mc._interrupted = False
        
        # Connected: both promises are true.
        register_interrupt_kill_callback(lambda: True, lambda: True)
        mc._interrupted = False
        try:
            stderr = io.StringIO()
            with patch('diagnostic_collector_mysql.__main__.sys.stderr', stderr):
                _signal_handler(2, None)
            message = stderr.getvalue()
            self.assertIn('attempting to cancel', message)
            # An attempt, not a completed outcome: the kill can fail.
            self.assertNotIn('cancelling the running query', message)
            # The report is announced by main() only when it happens, so the
            # handler must not promise one here.
            self.assertNotIn('partial report', message)
        finally:
            register_interrupt_kill_callback(None)
            mc._interrupted = False


class TestExitCodeDocumentation(unittest.TestCase):
    """Tests to verify exit code documentation is accurate."""
    
    def test_exit_code_class_has_docstring(self):
        """Test that ExitCode class has documentation."""
        self.assertIsNotNone(ExitCode.__doc__)
        self.assertIn("Exit codes", ExitCode.__doc__)
    
    def test_exit_code_docstring_mentions_all_codes(self):
        """Test that ExitCode docstring documents all exit codes."""
        doc = ExitCode.__doc__
        self.assertIn("0", doc)  # SUCCESS
        self.assertIn("1", doc)  # INVALID_ARGUMENTS
        self.assertIn("2", doc)  # CONNECTION_FAILURE
        self.assertIn("3", doc)  # INPUT_FILE_ERROR
        self.assertIn("4", doc)  # OUTPUT_DIRECTORY_ERROR
        self.assertIn("5", doc)  # BACKOFF_EXCEEDED
        self.assertIn("6", doc)  # PARTIAL_COMPLETION


if __name__ == '__main__':
    unittest.main()
