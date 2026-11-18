# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for ConnectionManager.

Tests connection establishment, SSL/TLS configuration, authentication plugin
support, and error handling.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import mysql.connector
from mysql.connector import errorcode

from diagnostic_collector_mysql.core.connection_manager import (
    ConnectionManager,
    ConnectionError,
    SUPPORTED_AUTH_PLUGINS,
)
from diagnostic_collector_mysql.models import ConnectionConfig


class TestConnectionManagerInit:
    """Tests for ConnectionManager initialization."""
    
    def test_init_with_basic_config(self):
        """Test initialization with basic configuration."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        assert manager._config == config
        assert manager._connection is None
    
    def test_init_with_ssl_config(self):
        """Test initialization with SSL configuration."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
            ssl_ca='/path/to/ca.pem',
        )
        manager = ConnectionManager(config)
        assert manager._config.ssl_ca == '/path/to/ca.pem'
    
    def test_init_with_custom_timeout(self):
        """Test initialization with custom connection timeout."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
            connect_timeout=30,
        )
        manager = ConnectionManager(config)
        assert manager._config.connect_timeout == 30


class TestBuildConnectionParams:
    """Tests for _build_connection_params method."""
    
    def test_basic_params(self):
        """Test basic connection parameters (TLS attempted by default)."""
        config = ConnectionConfig(
            host='dbhost.example.com',
            port=3307,
            user='myuser',
            password='mypass',
            connect_timeout=15,
        )
        manager = ConnectionManager(config)
        params = manager._build_connection_params()
        
        assert params['host'] == 'dbhost.example.com'
        assert params['port'] == 3307
        assert params['user'] == 'myuser'
        assert params['password'] == 'mypass'
        assert params['connection_timeout'] == 15
        # Secure-by-default: TLS is attempted without any SSL flags
        assert params['ssl_disabled'] is False
        assert params['ssl_verify_cert'] is False
        assert params['ssl_verify_identity'] is False
    
    def test_ssl_enabled_without_ca(self):
        """Test SSL enabled without CA certificate."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        params = manager._build_connection_params()
        
        assert params['ssl_disabled'] is False
        assert params['ssl_verify_cert'] is False
        assert params['ssl_verify_identity'] is False
        assert 'ssl_ca' not in params
    
    def test_ssl_enabled_with_ca(self):
        """Test SSL enabled with CA certificate."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
            ssl_ca='/path/to/ca.pem',
        )
        manager = ConnectionManager(config)
        params = manager._build_connection_params()
        
        assert params['ssl_disabled'] is False
        assert params['ssl_ca'] == '/path/to/ca.pem'
        assert params['ssl_verify_cert'] is True
        assert params['ssl_verify_identity'] is True
    
    def test_no_plaintext_opt_out(self):
        """TLS is always attempted; ssl_disabled is never True."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        params = manager._build_connection_params()
        
        assert params['ssl_disabled'] is False
        assert not hasattr(config, 'use_ssl')


class TestConnect:
    """Tests for connect method."""
    
    def _create_mysql_mock_cursor(self):
        """Helper to create a mock cursor that returns MySQL 8.0 version."""
        mock_cursor = MagicMock()
        call_count = [0]
        
        def execute_side_effect(query):
            if "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "version":
                return ("8.0.35",)
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        return mock_cursor
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_successful_connection(self, mock_connect):
        """Test successful database connection."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_conn.cursor.return_value = self._create_mysql_mock_cursor()
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        assert manager.is_connected() is True
        mock_connect.assert_called_once()
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_connect_already_connected(self, mock_connect):
        """Test connect when already connected does not reconnect."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_conn.cursor.return_value = self._create_mysql_mock_cursor()
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        manager.connect()  # Second call should not reconnect
        
        # Should only be called once
        assert mock_connect.call_count == 1
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_connect_with_ssl(self, mock_connect):
        """Test connection with SSL enabled."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_conn.cursor.return_value = self._create_mysql_mock_cursor()
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs['ssl_disabled'] is False


class TestErrorHandling:
    """Tests for error handling."""
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_access_denied_error(self, mock_connect):
        """Test authentication failure error message."""
        error = mysql.connector.Error(
            msg="Access denied",
            errno=errorcode.ER_ACCESS_DENIED_ERROR
        )
        mock_connect.side_effect = error
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='baduser',
            password='badpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(ConnectionError) as exc_info:
            manager.connect()
        
        assert "Authentication failed" in str(exc_info.value)
        assert "baduser" in str(exc_info.value)
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_unknown_host_error(self, mock_connect):
        """Test unknown host error message."""
        error = mysql.connector.Error(
            msg="Unknown host",
            errno=errorcode.CR_UNKNOWN_HOST
        )
        mock_connect.side_effect = error
        
        config = ConnectionConfig(
            host='nonexistent.host',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(ConnectionError) as exc_info:
            manager.connect()
        
        assert "Unknown host" in str(exc_info.value)
        assert "nonexistent.host" in str(exc_info.value)
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_connection_refused_error(self, mock_connect):
        """Test connection refused error message."""
        error = mysql.connector.Error(
            msg="Can't connect to MySQL server",
            errno=errorcode.CR_CONN_HOST_ERROR
        )
        mock_connect.side_effect = error
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(ConnectionError) as exc_info:
            manager.connect()
        
        assert "Cannot connect" in str(exc_info.value)
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_auth_plugin_error(self, mock_connect):
        """Test unsupported authentication plugin error."""
        error = mysql.connector.Error(
            msg="Authentication plugin 'unsupported_plugin' cannot be loaded",
            errno=errorcode.CR_AUTH_PLUGIN_CANNOT_LOAD
        )
        mock_connect.side_effect = error
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(ConnectionError) as exc_info:
            manager.connect()
        
        assert "unsupported_plugin" in str(exc_info.value).lower() or \
               "authentication plugin" in str(exc_info.value).lower()
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_ssl_error(self, mock_connect):
        """Test SSL connection error message."""
        error = mysql.connector.Error(
            msg="SSL connection error"
        )
        mock_connect.side_effect = error
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
            ssl_ca='/path/to/ca.pem',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(ConnectionError) as exc_info:
            manager.connect()
        
        assert "SSL" in str(exc_info.value) or "ssl" in str(exc_info.value)


class TestGetConnection:
    """Tests for get_connection method."""
    
    def _create_mysql_mock_cursor(self):
        """Helper to create a mock cursor that returns MySQL 8.0 version."""
        mock_cursor = MagicMock()
        
        def execute_side_effect(query):
            if "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "version":
                return ("8.0.35",)
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        return mock_cursor
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_get_connection_when_connected(self, mock_connect):
        """Test getting connection when connected."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_conn.cursor.return_value = self._create_mysql_mock_cursor()
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        conn = manager.get_connection()
        assert conn == mock_conn
    
    def test_get_connection_when_not_connected(self):
        """Test getting connection when not connected raises error."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(ConnectionError) as exc_info:
            manager.get_connection()
        
        assert "No active database connection" in str(exc_info.value)


class TestIsConnected:
    """Tests for is_connected method."""
    
    def _create_mysql_mock_cursor(self):
        """Helper to create a mock cursor that returns MySQL 8.0 version."""
        mock_cursor = MagicMock()
        
        def execute_side_effect(query):
            if "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "version":
                return ("8.0.35",)
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        return mock_cursor
    
    def test_is_connected_when_not_connected(self):
        """Test is_connected returns False when not connected."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        assert manager.is_connected() is False
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_is_connected_when_connected(self, mock_connect):
        """Test is_connected returns True when connected."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_conn.cursor.return_value = self._create_mysql_mock_cursor()
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        assert manager.is_connected() is True


class TestClose:
    """Tests for close method."""
    
    def _create_mysql_mock_cursor(self):
        """Helper to create a mock cursor that returns MySQL 8.0 version."""
        mock_cursor = MagicMock()
        
        def execute_side_effect(query):
            if "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "version":
                return ("8.0.35",)
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        return mock_cursor
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_close_connection(self, mock_connect):
        """Test closing an active connection."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_conn.cursor.return_value = self._create_mysql_mock_cursor()
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        manager.close()
        
        mock_conn.close.assert_called_once()
        assert manager._connection is None
    
    def test_close_when_not_connected(self):
        """Test closing when not connected does not raise error."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        # Should not raise
        manager.close()


class TestContextManager:
    """Tests for context manager support."""
    
    def _create_mysql_mock_cursor(self):
        """Helper to create a mock cursor that returns MySQL 8.0 version."""
        mock_cursor = MagicMock()
        
        def execute_side_effect(query):
            if "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "version":
                return ("8.0.35",)
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        return mock_cursor
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_context_manager(self, mock_connect):
        """Test using ConnectionManager as context manager."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_conn.cursor.return_value = self._create_mysql_mock_cursor()
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        
        with ConnectionManager(config) as manager:
            assert manager.is_connected() is True
        
        mock_conn.close.assert_called_once()


class TestSupportedAuthPlugins:
    """Tests for supported authentication plugins."""
    
    def test_supported_plugins_list(self):
        """Test that required authentication plugins are supported."""
        assert 'mysql_native_password' in SUPPORTED_AUTH_PLUGINS
        assert 'caching_sha2_password' in SUPPORTED_AUTH_PLUGINS
        assert 'sha256_password' in SUPPORTED_AUTH_PLUGINS


class TestExtractPluginName:
    """Tests for _extract_plugin_name helper method."""
    
    def test_extract_plugin_from_quoted_message(self):
        """Test extracting plugin name from quoted error message."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        msg = "Authentication plugin 'some_plugin' cannot be loaded"
        result = manager._extract_plugin_name(msg)
        assert result == 'some_plugin'
    
    def test_extract_plugin_returns_none_for_no_match(self):
        """Test returns None when no plugin name found."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        msg = "Some generic error message"
        result = manager._extract_plugin_name(msg)
        assert result is None


class TestEngineDetection:
    """Tests for engine detection."""
    
    def _create_mock_cursor(self, version_result, aurora_result=None, aurora_raises=False):
        """Helper to create a mock cursor with configured responses."""
        mock_cursor = MagicMock()
        
        # Track which query is being executed
        call_count = [0]
        
        def execute_side_effect(query):
            call_count[0] += 1
            if "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                if aurora_raises:
                    raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "version":
                return version_result
            elif mock_cursor._current_query == "aurora":
                return aurora_result
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        
        return mock_cursor
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_detect_mysql_engine(self, mock_connect):
        """Test detection of standard MySQL."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # MySQL version without MariaDB, aurora_version query returns None
        mock_cursor = self._create_mock_cursor(
            version_result=("8.0.35",),
            aurora_result=None,
            aurora_raises=False
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.MYSQL
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_detect_aurora_mysql_engine(self, mock_connect):
        """Test detection of Aurora MySQL."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # Aurora MySQL: aurora_version variable exists
        mock_cursor = self._create_mock_cursor(
            version_result=("8.0.mysql_aurora.3.04.0",),
            aurora_result=("aurora_version", "3.04.0"),
            aurora_raises=False
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='aurora-cluster.cluster-xxx.us-east-1.rds.amazonaws.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.AURORA_MYSQL
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_detect_mariadb_engine(self, mock_connect):
        """Test detection of MariaDB."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # MariaDB: VERSION() contains "MariaDB"
        mock_cursor = self._create_mock_cursor(
            version_result=("11.4.2-MariaDB",),
            aurora_result=None,
            aurora_raises=True  # aurora_version doesn't exist in MariaDB
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.MARIADB
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_detect_mariadb_rds(self, mock_connect):
        """Test detection of RDS MariaDB."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # RDS MariaDB version string
        mock_cursor = self._create_mock_cursor(
            version_result=("11.4.2-MariaDB-log",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='mariadb-instance.xxx.us-east-1.rds.amazonaws.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.MARIADB
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_detect_engine_defaults_to_mysql_on_error(self, mock_connect):
        """Test engine detection defaults to MySQL when detection fails."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # Cursor that raises error on any query
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = mysql.connector.Error(msg="Query failed")
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        from diagnostic_collector_mysql.models import EngineType
        # Should default to MySQL when detection fails
        assert manager.get_engine_type() == EngineType.MYSQL
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_engine_type_stored_after_connect(self, mock_connect):
        """Test engine type is stored for query filtering."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        mock_cursor = self._create_mock_cursor(
            version_result=("8.0.35",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        # Engine type should be stored and accessible
        assert manager._engine_type is not None
        
        # Multiple calls should return the same stored value
        engine1 = manager.get_engine_type()
        engine2 = manager.get_engine_type()
        assert engine1 == engine2
    
    def test_get_engine_type_before_connect_raises_error(self):
        """Test get_engine_type raises error when not connected."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(ConnectionError) as exc_info:
            manager.get_engine_type()
        
        assert "Engine type not available" in str(exc_info.value)
        assert "connect()" in str(exc_info.value)
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_engine_type_reset_on_close(self, mock_connect):
        """Test engine type is reset when connection is closed."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        mock_cursor = self._create_mock_cursor(
            version_result=("8.0.35",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        # Engine type should be set
        assert manager._engine_type is not None
        
        # Close connection
        manager.close()
        
        # Engine type should be reset
        assert manager._engine_type is None
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_detect_mysql_with_aurora_variable_not_found(self, mock_connect):
        """Test MySQL detection when aurora_version variable doesn't exist."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # Standard MySQL: aurora_version query raises error (variable doesn't exist)
        mock_cursor = self._create_mock_cursor(
            version_result=("8.0.35-0ubuntu0.22.04.1",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.MYSQL


class TestVersionValidation:
    """Tests for version validation - rejecting unsupported database versions."""
    
    def _create_mock_cursor(self, version_result, aurora_result=None, aurora_raises=False):
        """Helper to create a mock cursor with configured responses."""
        mock_cursor = MagicMock()
        
        def execute_side_effect(query):
            if "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                if aurora_raises:
                    raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "version":
                return version_result
            elif mock_cursor._current_query == "aurora":
                return aurora_result
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        
        return mock_cursor
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_mysql_57_rejected(self, mock_connect):
        """Test that MySQL 5.7 is rejected as unsupported."""
        from diagnostic_collector_mysql.core.connection_manager import UnsupportedVersionError
        
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # MySQL 5.7 version
        mock_cursor = self._create_mock_cursor(
            version_result=("5.7.44",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(UnsupportedVersionError) as exc_info:
            manager.connect()
        
        assert "MySQL" in str(exc_info.value)
        assert "5.7.44" in str(exc_info.value)
        assert "8.0.0" in str(exc_info.value)
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_mariadb_10x_rejected(self, mock_connect):
        """Test that MariaDB 10.x is rejected as unsupported."""
        from diagnostic_collector_mysql.core.connection_manager import UnsupportedVersionError
        
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # MariaDB 10.6 version
        mock_cursor = self._create_mock_cursor(
            version_result=("10.6.18-MariaDB",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(UnsupportedVersionError) as exc_info:
            manager.connect()
        
        assert "MariaDB" in str(exc_info.value)
        assert "10.6.18" in str(exc_info.value)
        assert "11.4.0" in str(exc_info.value)
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_aurora_2x_rejected(self, mock_connect):
        """Test that Aurora MySQL 2.x is rejected as unsupported."""
        from diagnostic_collector_mysql.core.connection_manager import UnsupportedVersionError
        
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        # Aurora MySQL 2.x version
        mock_cursor = self._create_mock_cursor(
            version_result=("5.7.mysql_aurora.2.11.3",),
            aurora_result=("aurora_version", "2.11.3"),
            aurora_raises=False
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='aurora-cluster.cluster-xxx.us-east-1.rds.amazonaws.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with pytest.raises(UnsupportedVersionError) as exc_info:
            manager.connect()
        
        assert "Aurora MySQL" in str(exc_info.value)
        assert "2.11.3" in str(exc_info.value)
        assert "3.0.0" in str(exc_info.value)
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_mysql_80_accepted(self, mock_connect):
        """Test that MySQL 8.0 is accepted."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        mock_cursor = self._create_mock_cursor(
            version_result=("8.0.35",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()  # Should not raise
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.MYSQL
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_mysql_84_accepted(self, mock_connect):
        """Test that MySQL 8.4 is accepted."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        mock_cursor = self._create_mock_cursor(
            version_result=("8.4.0",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()  # Should not raise
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.MYSQL
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_mariadb_114_accepted(self, mock_connect):
        """Test that MariaDB 11.4 is accepted."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        mock_cursor = self._create_mock_cursor(
            version_result=("11.4.2-MariaDB",),
            aurora_result=None,
            aurora_raises=True
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()  # Should not raise
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.MARIADB
    
    @patch('diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect')
    def test_aurora_3x_accepted(self, mock_connect):
        """Test that Aurora MySQL 3.x is accepted."""
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        
        mock_cursor = self._create_mock_cursor(
            version_result=("8.0.mysql_aurora.3.04.0",),
            aurora_result=("aurora_version", "3.04.0"),
            aurora_raises=False
        )
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        config = ConnectionConfig(
            host='aurora-cluster.cluster-xxx.us-east-1.rds.amazonaws.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        manager.connect()  # Should not raise
        
        from diagnostic_collector_mysql.models import EngineType
        assert manager.get_engine_type() == EngineType.AURORA_MYSQL


class TestParseVersion:
    """Tests for _parse_version helper method."""
    
    def test_parse_simple_version(self):
        """Test parsing simple version string."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        assert manager._parse_version("8.0.35") == (8, 0, 35)
    
    def test_parse_version_with_suffix(self):
        """Test parsing version with suffix."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        assert manager._parse_version("8.0.35-0ubuntu0.22.04.1") == (8, 0, 35)
    
    def test_parse_mariadb_version(self):
        """Test parsing MariaDB version string."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        assert manager._parse_version("11.4.2-MariaDB") == (11, 4, 2)
    
    def test_parse_aurora_version(self):
        """Test parsing Aurora version string."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        assert manager._parse_version("3.04.0") == (3, 4, 0)
    
    def test_parse_empty_version(self):
        """Test parsing empty version string."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        assert manager._parse_version("") == (0, 0, 0)
    
    def test_parse_none_version(self):
        """Test parsing None version string."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        assert manager._parse_version(None) == (0, 0, 0)
    
    def test_parse_two_part_version(self):
        """Test parsing version with only major.minor."""
        config = ConnectionConfig(
            host='localhost',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        assert manager._parse_version("8.0") == (8, 0, 0)


class TestTLSPolicy:
    """Tests for secure-by-default TLS behavior and encryption verification."""
    
    def _create_mock_cursor(self, ssl_cipher):
        """
        Helper to create a mock cursor covering the Ssl_cipher status query
        and MySQL 8.0 engine detection.
        
        Args:
            ssl_cipher: Value returned for Ssl_cipher ('' = unencrypted,
                        cipher name = encrypted)
        """
        mock_cursor = MagicMock()
        
        def execute_side_effect(query):
            if "Ssl_cipher" in query:
                mock_cursor._current_query = "cipher"
            elif "VERSION()" in query:
                mock_cursor._current_query = "version"
            elif "aurora_version" in query.lower():
                mock_cursor._current_query = "aurora"
                raise mysql.connector.Error(msg="Unknown system variable 'aurora_version'")
        
        def fetchone_side_effect():
            if mock_cursor._current_query == "cipher":
                return ("Ssl_cipher", ssl_cipher)
            if mock_cursor._current_query == "version":
                return ("8.0.35",)
            return None
        
        mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
        mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
        mock_cursor.close = MagicMock()
        mock_cursor._current_query = None
        return mock_cursor
    
    def test_ssl_attempted_by_default(self):
        """TLS is attempted without any SSL flags (secure-by-default)."""
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        params = manager._build_connection_params()
        
        assert params['ssl_disabled'] is False
    
    def test_encrypted_without_ca_warns_unverified(self, caplog):
        """Encrypted session without --ssl-ca logs a cert-not-verified warning."""
        import logging
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            mock_conn.cursor.return_value = self._create_mock_cursor(
                'TLS_AES_256_GCM_SHA384'
            )
            mock_connect.return_value = mock_conn
            
            with caplog.at_level(logging.WARNING):
                manager.connect()
        
        assert any(
            'certificate was not verified' in record.message
            for record in caplog.records
        )
    
    def test_encrypted_no_ca_warning_names_remedy(self, caplog):
        """The unverified-certificate warning tells the user the session IS
        encrypted and names both the flag and the RDS global bundle."""
        import logging
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            mock_conn.cursor.return_value = self._create_mock_cursor(
                'TLS_AES_256_GCM_SHA384'
            )
            mock_connect.return_value = mock_conn
            
            with caplog.at_level(logging.WARNING):
                manager.connect()
        
        warning = next(
            r.message for r in caplog.records
            if 'certificate was not verified' in r.message
        )
        assert 'Session is encrypted' in warning
        assert '--ssl-ca' in warning
        assert 'truststore.pki.rds.amazonaws.com' in warning
    
    def test_encrypted_with_ca_no_warning(self, caplog):
        """Encrypted session with --ssl-ca produces no TLS warnings."""
        import logging
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
            ssl_ca='/path/to/ca.pem',
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            mock_conn.cursor.return_value = self._create_mock_cursor(
                'TLS_AES_256_GCM_SHA384'
            )
            mock_connect.return_value = mock_conn
            
            with caplog.at_level(logging.WARNING):
                manager.connect()
        
        assert not any(
            'certificate was not verified' in record.message
            or 'NOT encrypted' in record.message
            for record in caplog.records
        )
    
    def test_unencrypted_fallback_warns(self, caplog):
        """Server without TLS support falls back to plaintext with a warning."""
        import logging
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            mock_conn.cursor.return_value = self._create_mock_cursor('')
            mock_connect.return_value = mock_conn
            
            with caplog.at_level(logging.WARNING):
                manager.connect()
        
        assert any(
            'NOT encrypted' in record.message for record in caplog.records
        )
    
    def test_require_ssl_fails_when_unencrypted(self):
        """--use-encrypted-connection fails if the session is not encrypted."""
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
            require_ssl=True,
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            mock_conn.cursor.return_value = self._create_mock_cursor('')
            mock_connect.return_value = mock_conn
            
            with pytest.raises(ConnectionError) as exc_info:
                manager.connect()
        
        assert 'SSL/TLS was required' in str(exc_info.value)
        assert manager.is_connected() is False
    
    def test_require_ssl_succeeds_when_encrypted(self):
        """--use-encrypted-connection succeeds on an encrypted session."""
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
            require_ssl=True,
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            mock_conn.cursor.return_value = self._create_mock_cursor(
                'TLS_AES_256_GCM_SHA384'
            )
            mock_connect.return_value = mock_conn
            
            manager.connect()
        
        assert manager.is_connected() is True
    
    def test_undetermined_cipher_does_not_fail(self):
        """Encryption state undetermined without require_ssl: no failure."""
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            # Cursor raises on the cipher query -> undetermined
            mock_cursor = MagicMock()
            
            def execute_side_effect(query):
                if "Ssl_cipher" in query:
                    raise mysql.connector.Error(msg="status query failed")
                if "VERSION()" in query:
                    mock_cursor._current_query = "version"
                elif "aurora_version" in query.lower():
                    raise mysql.connector.Error(msg="Unknown system variable")
            
            def fetchone_side_effect():
                if mock_cursor._current_query == "version":
                    return ("8.0.35",)
                return None
            
            mock_cursor.execute = MagicMock(side_effect=execute_side_effect)
            mock_cursor.fetchone = MagicMock(side_effect=fetchone_side_effect)
            mock_cursor._current_query = None
            mock_conn.cursor.return_value = mock_cursor
            mock_connect.return_value = mock_conn
            
            manager.connect()
        
        assert manager.is_connected() is True

    def test_undetermined_cipher_fails_closed_when_require_ssl(self):
        """Encryption state undetermined with require_ssl: fail closed."""
        config = ConnectionConfig(
            host='remote.example.com',
            port=3306,
            user='testuser',
            password='testpass',
            require_ssl=True,
        )
        manager = ConnectionManager(config)
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.is_connected.return_value = True
            # Cursor raises on the cipher query -> undetermined
            mock_cursor = MagicMock()
            mock_cursor.execute.side_effect = mysql.connector.Error(
                msg="status query failed"
            )
            mock_conn.cursor.return_value = mock_cursor
            mock_connect.return_value = mock_conn
            
            with pytest.raises(ConnectionError) as exc_info:
                manager.connect()
        
        assert 'could not be verified' in str(exc_info.value)
        assert manager.is_connected() is False


class TestKillCurrentQuery:
    """Tests for server-side query cancellation."""
    
    def _make_manager(self):
        config = ConnectionConfig(
            host='127.0.0.1',
            port=3306,
            user='testuser',
            password='testpass',
        )
        return ConnectionManager(config)
    
    def test_kill_without_connection_id_returns_false(self):
        """No captured connection id means nothing to kill."""
        manager = self._make_manager()
        assert manager._connection_id is None
        assert manager.kill_current_query() is False
    
    def test_kill_issues_kill_query_on_side_connection(self):
        """kill_current_query opens a side connection and issues
        KILL QUERY <id>, then reports success."""
        manager = self._make_manager()
        manager._connection_id = 42
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect'
        ) as mock_connect:
            side_conn = MagicMock()
            side_cursor = MagicMock()
            side_conn.cursor.return_value = side_cursor
            mock_connect.return_value = side_conn
            
            assert manager.kill_current_query() is True
            
            side_cursor.execute.assert_called_once_with('KILL QUERY 42')
            side_conn.close.assert_called_once()
    
    def test_kill_returns_false_on_connect_failure(self):
        """A failed side connection is reported, never raised."""
        manager = self._make_manager()
        manager._connection_id = 42
        
        with patch(
            'diagnostic_collector_mysql.core.connection_manager.mysql.connector.connect',
            side_effect=mysql.connector.Error(msg='cannot connect')
        ):
            assert manager.kill_current_query() is False
    
    def test_capture_connection_id_reads_server_value(self):
        """_capture_connection_id stores SELECT CONNECTION_ID()'s result."""
        manager = self._make_manager()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (77,)
        mock_conn.cursor.return_value = mock_cursor
        manager._connection = mock_conn
        
        manager._capture_connection_id()
        
        assert manager._connection_id == 77
        mock_cursor.execute.assert_called_once_with('SELECT CONNECTION_ID()')
    
    def test_capture_connection_id_failure_is_non_fatal(self):
        """A failing CONNECTION_ID() query leaves the id unset."""
        manager = self._make_manager()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = mysql.connector.Error(msg='denied')
        mock_conn.cursor.return_value = mock_cursor
        manager._connection = mock_conn
        
        manager._capture_connection_id()
        
        assert manager._connection_id is None
    
    def test_close_resets_connection_id(self):
        """close() clears the captured id so a stale kill cannot target a
        recycled server thread id."""
        manager = self._make_manager()
        manager._connection = MagicMock()
        manager._connection.is_connected.return_value = True
        manager._connection_id = 42
        
        manager.close()
        
        assert manager._connection_id is None
    
    def test_abort_connection_shuts_down_socket(self):
        """abort_connection shuts the raw socket down both ways so a
        blocked read and a pending close return immediately.
        
        Uses explicit stubs rather than a bare MagicMock: MagicMock
        auto-creates any attribute, so _socket.sock would resolve even if
        the connector renamed it and this test would pass while the feature
        silently degraded.
        """
        import socket as socket_module
        
        class FakeRawSocket:
            def __init__(self):
                self.shutdown_calls = []
            def shutdown(self, how):
                self.shutdown_calls.append(how)
        
        class FakeConnectorSocket:
            def __init__(self, raw):
                self.sock = raw
        
        class FakeConnection:
            def __init__(self, raw):
                self._socket = FakeConnectorSocket(raw)
        
        raw_socket = FakeRawSocket()
        manager = self._make_manager()
        manager._connection = FakeConnection(raw_socket)
        
        assert manager.abort_connection() is True
        assert raw_socket.shutdown_calls == [socket_module.SHUT_RDWR]
    
    def test_connector_still_exposes_the_socket_path_we_reach_for(self):
        """Pin the private connector internals abort_connection depends on.
        
        The code reaches for connection._socket.sock, which is not public
        API. If a connector upgrade renames or restructures it, the mocked
        tests above would stay green while the feature degraded to the
        hard-exit path. This asserts against the real connector so the
        upgrade breaks a test instead.
        """
        import inspect
        from mysql.connector.connection import MySQLConnection
        
        source = inspect.getsource(MySQLConnection.__init__)
        assert '_socket' in source, (
            "MySQLConnection no longer sets _socket; "
            "ConnectionManager._raw_socket needs updating"
        )
        
        from mysql.connector.network import MySQLTCPSocket
        defines_sock = any(
            'self.sock' in inspect.getsource(cls)
            for cls in MySQLTCPSocket.__mro__
            if cls.__module__.startswith('mysql.connector')
        )
        assert defines_sock, (
            "The connector socket class no longer exposes .sock; "
            "ConnectionManager._raw_socket needs updating"
        )
    
    def test_connection_params_pin_pure_python(self):
        """The pure-Python connector is pinned on purpose.
        
        The C extension blocks Python signal handlers for the duration of a
        running statement (measured: a SIGINT 3s into a 15s query ran its
        handler at 15s with use_pure=False, 3.0s with use_pure=True), and it
        exposes no _socket for the abort path. Both interrupt mechanisms
        depend on those, so this must not be dropped.
        """
        config = ConnectionConfig(
            host='127.0.0.1',
            port=3306,
            user='testuser',
            password='testpass',
        )
        manager = ConnectionManager(config)
        
        params = manager._build_connection_params()
        
        assert params['use_pure'] is True
    
    def test_probe_logs_when_socket_unavailable(self, caplog):
        """The connect-time probe records an unavailable socket at debug so
        the degradation is visible before anyone presses Ctrl-C twice."""
        import logging
        
        class FakeConnection:
            _socket = None
        
        manager = self._make_manager()
        manager._connection = FakeConnection()
        
        with caplog.at_level(logging.DEBUG):
            manager._probe_abort_capability()
        
        assert any(
            'not exposed by this connector build' in record.message
            for record in caplog.records
        )
    
    def test_abort_connection_without_connection_is_noop(self):
        """abort_connection on a manager that never connected reports
        failure rather than raising."""
        manager = self._make_manager()
        assert manager._connection is None
        assert manager.abort_connection() is False
    
    def test_abort_connection_swallows_socket_errors(self):
        """A socket that cannot be shut down is reported as a failure so the
        caller exits without waiting for cleanup."""
        manager = self._make_manager()
        mock_conn = MagicMock()
        mock_conn._socket.sock.shutdown.side_effect = OSError('not connected')
        manager._connection = mock_conn
        
        assert manager.abort_connection() is False
    
    def test_abort_connection_reports_when_socket_not_exposed(self, caplog):
        """A connector build that does not expose the socket (e.g. the C
        extension) is reported at WARNING, not hidden at debug."""
        import logging
        manager = self._make_manager()
        mock_conn = MagicMock()
        mock_conn._socket = None
        manager._connection = mock_conn
        
        with caplog.at_level(logging.WARNING):
            assert manager.abort_connection() is False
        
        assert any(
            'Could not reach the connection socket' in record.message
            for record in caplog.records
        )
