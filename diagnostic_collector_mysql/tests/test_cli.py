#!/usr/bin/env python3
"""
Unit tests for CLI - command line interface.

Tests validate:
- Environment variable password support (DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD)
- Argument parsing and validation
"""

import os
import sys
import unittest
from unittest.mock import patch

from diagnostic_collector_mysql.cli import (
    PASSWORD_ENV_VAR,
    create_argument_parser,
    main_cli,
    parse_arguments,
    validate_arguments,
)
from diagnostic_collector_mysql.models import ExitCode


class TestEnvironmentVariablePassword(unittest.TestCase):
    """Tests for DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD environment variable support."""
    
    def setUp(self):
        """Save original environment and clear password env var."""
        self.original_env = os.environ.copy()
        # Clear the password env var if it exists
        if PASSWORD_ENV_VAR in os.environ:
            del os.environ[PASSWORD_ENV_VAR]
    
    def tearDown(self):
        """Restore original environment."""
        os.environ.clear()
        os.environ.update(self.original_env)
    
    def test_password_from_env_var(self):
        """Test password is read from environment variable when --password not provided."""
        os.environ[PASSWORD_ENV_VAR] = 'env_secret_password'
        
        args, _ = parse_arguments(['--user', 'testuser', '--host', 'localhost'])
        errors = validate_arguments(args)
        
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(args.password, 'env_secret_password')
    
    def test_cli_password_overrides_env_var(self):
        """Test --password argument takes precedence over environment variable."""
        os.environ[PASSWORD_ENV_VAR] = 'env_password'
        
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--password', 'cli_password'
        ])
        errors = validate_arguments(args)
        
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(args.password, 'cli_password')
    
    def test_error_when_no_password_provided(self):
        """Test error when neither --password nor env var is provided."""
        # Ensure env var is not set
        if PASSWORD_ENV_VAR in os.environ:
            del os.environ[PASSWORD_ENV_VAR]
        
        args, _ = parse_arguments(['--user', 'testuser', '--host', 'localhost'])
        errors = validate_arguments(args)
        
        self.assertEqual(len(errors), 1)
        self.assertIn('--password is required', errors[0])
        self.assertIn(PASSWORD_ENV_VAR, errors[0])
    
    def test_empty_env_var_treated_as_not_set(self):
        """Test empty environment variable is treated as not set."""
        os.environ[PASSWORD_ENV_VAR] = ''
        
        args, _ = parse_arguments(['--user', 'testuser', '--host', 'localhost'])
        errors = validate_arguments(args)
        
        # Empty string is falsy, so should require password
        self.assertEqual(len(errors), 1)
        self.assertIn('--password is required', errors[0])
    
    def test_back_off_help_does_not_duplicate_the_default(self):
        """CustomHelpFormatter appends '(default: ...)' unless the help text already
        contains the %(default) token, so hardcoding it renders the default twice."""
        parser = create_argument_parser()
        rendered = parser.format_help()
        for flag in ('--connection-back-off', '--connection-usage-back-off',
                     '--objects-back-off'):
            action = next(
                a for a in parser._actions if flag in (a.option_strings or [])
            )
            help_text = action.help or ''
            self.assertNotIn(
                '(default:', help_text,
                f"{flag} hardcodes its default; the formatter already appends it"
            )
        self.assertNotIn('(default: 90) (default: 90)', rendered)
        self.assertNotIn('(default: 1000) (default: 1000)', rendered)

    def test_env_var_name_constant(self):
        """Test the environment variable name constant is correct."""
        self.assertEqual(PASSWORD_ENV_VAR, 'DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD')

    def test_connection_usage_back_off_defaults_to_ninety(self):
        """A proportion of max_connections needs no per-instance tuning, so it can
        ship with a default that actually triggers."""
        args, _ = parse_arguments([
            '--user', 'u', '--host', 'h', '--password', 'p'
        ])
        self.assertEqual(args.connection_usage_back_off, 90)

    def test_connection_usage_back_off_accepts_zero_to_disable(self):
        args, _ = parse_arguments([
            '--user', 'u', '--host', 'h', '--password', 'p',
            '--connection-usage-back-off', '0'
        ])
        self.assertEqual(validate_arguments(args), [])

    def test_connection_usage_back_off_rejects_above_one_hundred(self):
        """Usage is a percentage of max_connections and cannot exceed it, so a
        threshold above 100 would silently never trigger."""
        args, _ = parse_arguments([
            '--user', 'u', '--host', 'h', '--password', 'p',
            '--connection-usage-back-off', '150'
        ])
        errors = validate_arguments(args)
        self.assertEqual(len(errors), 1)
        self.assertIn('--connection-usage-back-off', errors[0])
        self.assertIn('between 0 and 100', errors[0])

    def test_connection_usage_back_off_rejects_negative(self):
        args, _ = parse_arguments([
            '--user', 'u', '--host', 'h', '--password', 'p',
            '--connection-usage-back-off', '-1'
        ])
        errors = validate_arguments(args)
        self.assertEqual(len(errors), 1)
        self.assertIn('--connection-usage-back-off', errors[0])
    
    def test_main_cli_with_env_password(self):
        """Test main_cli accepts password from environment variable."""
        os.environ[PASSWORD_ENV_VAR] = 'test_password'
        
        args, exit_code = main_cli(['--user', 'testuser', '--host', 'localhost'])
        
        self.assertEqual(exit_code, ExitCode.SUCCESS)
        self.assertIsNotNone(args)
        self.assertEqual(args.password, 'test_password')
    
    def test_main_cli_error_without_password(self):
        """Test main_cli returns error when no password provided."""
        # Ensure env var is not set
        if PASSWORD_ENV_VAR in os.environ:
            del os.environ[PASSWORD_ENV_VAR]
        
        args, exit_code = main_cli(['--user', 'testuser', '--host', 'localhost'])
        
        self.assertEqual(exit_code, ExitCode.INVALID_ARGUMENTS)
        self.assertIsNone(args)


class TestPasswordArgumentOptional(unittest.TestCase):
    """Tests verifying --password argument is optional."""
    
    def setUp(self):
        """Save original environment."""
        self.original_env = os.environ.copy()
    
    def tearDown(self):
        """Restore original environment."""
        os.environ.clear()
        os.environ.update(self.original_env)
    
    def test_password_argument_not_required(self):
        """Test --password is not a required argument (can be provided via env var)."""
        parser = create_argument_parser()
        
        # Find the password action
        password_action = None
        for action in parser._actions:
            if '--password' in action.option_strings:
                password_action = action
                break
        
        self.assertIsNotNone(password_action)
        self.assertFalse(password_action.required)
    
    def test_parsing_without_password_succeeds(self):
        """Test argument parsing succeeds without --password."""
        # This should not raise an exception
        args, _ = parse_arguments(['--user', 'testuser', '--host', 'localhost'])
        
        self.assertEqual(args.user, 'testuser')
        self.assertEqual(args.host, 'localhost')
        self.assertIsNone(args.password)


class TestArgumentValidation(unittest.TestCase):
    """Tests for argument validation."""
    
    def setUp(self):
        """Set up environment with password."""
        self.original_env = os.environ.copy()
        os.environ[PASSWORD_ENV_VAR] = 'test_password'
    
    def tearDown(self):
        """Restore original environment."""
        os.environ.clear()
        os.environ.update(self.original_env)
    
    def test_valid_arguments(self):
        """Test validation passes for valid arguments."""
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--port', '3306'
        ])
        errors = validate_arguments(args)
        
        self.assertEqual(len(errors), 0)
    
    def test_invalid_port_zero(self):
        """Test validation fails for port 0."""
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--port', '0'
        ])
        errors = validate_arguments(args)
        
        self.assertTrue(any('--port' in e for e in errors))
    
    def test_invalid_port_negative(self):
        """Test validation fails for negative port."""
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--port', '-1'
        ])
        errors = validate_arguments(args)
        
        self.assertTrue(any('--port' in e for e in errors))
    
    def test_invalid_port_too_high(self):
        """Test validation fails for port > 65535."""
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--port', '65536'
        ])
        errors = validate_arguments(args)
        
        self.assertTrue(any('--port' in e for e in errors))
    
    def test_invalid_connect_timeout(self):
        """Test validation fails for non-positive connect timeout."""
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--connect-timeout', '0'
        ])
        errors = validate_arguments(args)
        
        self.assertTrue(any('--connect-timeout' in e for e in errors))
    
    def test_invalid_query_timeout(self):
        """Test validation fails for non-positive query timeout."""
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--query-timeout', '-5'
        ])
        errors = validate_arguments(args)
        
        self.assertTrue(any('--query-timeout' in e for e in errors))
    
    def test_invalid_collection_limit(self):
        """Test validation fails for non-positive collection limit."""
        args, _ = parse_arguments([
            '--user', 'testuser',
            '--host', 'localhost',
            '--collection-limit', '0'
        ])
        errors = validate_arguments(args)
        
        self.assertTrue(any('--collection-limit' in e for e in errors))


if __name__ == '__main__':
    unittest.main()
