"""
Connection Manager for Diagnostic Collector for MySQL.

This module handles database connection establishment, SSL/TLS configuration,
authentication plugin support, and engine type detection for MySQL/Aurora MySQL/MariaDB databases.

Responsibilities:
- Establish connection with --user, --password, --host, --port arguments
- Use port 3306 as default
- Attempt SSL/TLS encryption by default; --use-encrypted-connection requires it
- Use provided CA certificate file for SSL verification when --ssl-ca is specified
- Fall back to an unencrypted connection only when the server does not
  support TLS, and log a warning
- Support mysql_native_password, caching_sha2_password, sha256_password authentication plugins
- Log clear error message for unsupported authentication plugins
- Handle authentication errors with descriptive messages
- Detect if connected database is MySQL (RDS MySQL 8.0+ or standard MySQL)
- Detect if connected database is Aurora MySQL 8.0+
- Detect if connected database is MariaDB (RDS MariaDB 11.4+ or standard MariaDB)
- When engine cannot be determined, log warning and use generic MySQL queries
- Store detected engine type for query filtering
"""

import logging
import socket
from typing import Optional

import mysql.connector
from mysql.connector import Error as MySQLError, errorcode

from diagnostic_collector_mysql.models import ConnectionConfig, EngineType


# Supported authentication plugins
SUPPORTED_AUTH_PLUGINS = frozenset([
    'mysql_native_password',
    'caching_sha2_password',
    'sha256_password',
])

# Global RDS and Aurora trust bundle, covering every region. Named in the
# unverified-certificate warning; change it here if the bundle ever moves.
RDS_GLOBAL_TRUST_BUNDLE_URL = (
    "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
)

logger = logging.getLogger(__name__)


class ConnectionError(Exception):
    """Exception raised when database connection fails."""
    pass


class UnsupportedVersionError(Exception):
    """Exception raised when database version is not supported."""
    pass


# Minimum supported versions
MIN_MYSQL_VERSION = (8, 0, 0)
MIN_MARIADB_VERSION = (11, 4, 0)
MIN_AURORA_VERSION = (3, 0, 0)


class ConnectionManager:
    """
    Manages database connection lifecycle for Diagnostic Collector for MySQL.
    
    This class handles:
    - Connection establishment with provided credentials
    - SSL/TLS configuration with optional CA certificate
    - Authentication plugin support
    - Engine type detection (MySQL, Aurora MySQL, MariaDB)
    - Connection state management
    
    Attributes:
        config: ConnectionConfig with connection parameters
    """
    
    def __init__(self, config: ConnectionConfig):
        """
        Initialize ConnectionManager with connection configuration.
        
        Args:
            config: ConnectionConfig containing host, port, user, password,
                   SSL settings, and connection timeout
        """
        self._config = config
        self._connection: Optional[mysql.connector.MySQLConnection] = None
        self._engine_type: Optional[EngineType] = None
        self._version_string: Optional[str] = None
        self._connection_id: Optional[int] = None
    
    def connect(self) -> None:
        """
        Establish connection to the database and detect engine type.
        
        Creates a connection using the provided configuration, supporting:
        - SSL/TLS encryption attempted by default
        - CA certificate verification when ssl_ca is provided
        - Unencrypted connection only when the server declines TLS
        - Standard MySQL authentication plugins
        
        After connection is established, verifies the encryption state of the
        session (warns if unencrypted or unverified; fails if require_ssl is
        set and the session is not encrypted), then automatically detects the
        database engine type (MySQL, Aurora MySQL, or MariaDB)
        
        Raises:
            ConnectionError: If connection fails with descriptive message, or
                if require_ssl is set and the session is not encrypted
        """
        if self._connection is not None and self._connection.is_connected():
            logger.debug("Connection already established")
            return
        
        try:
            connection_params = self._build_connection_params()
            logger.debug(
                f"Connecting to {self._config.host}:{self._config.port} "
                f"as user '{self._config.user}' "
                f"(TLS required: {self._config.require_ssl})"
            )
            
            self._connection = mysql.connector.connect(**connection_params)
            logger.info(
                f"Successfully connected to {self._config.host}:{self._config.port}"
            )
            
            # Verify the encryption state of the established session
            self._verify_encryption()
            
            # Capture the server-side connection id so an interrupt can
            # cancel the running statement with KILL QUERY.
            self._capture_connection_id()
            self._probe_abort_capability()
            
            # Detect engine type after successful connection
            self._engine_type = self._detect_engine()
            logger.info(f"Detected database engine: {self._engine_type.value}")
            
            # MariaDB does not support utf8mb4_0900_ai_ci collation (MySQL 8.0 default).
            # mysql-connector-python sets this as the connection collation by default,
            # causing "Illegal mix of collations" errors on string comparisons.
            # Set a MariaDB-compatible collation after engine detection.
            if self._engine_type == EngineType.MARIADB:
                try:
                    cursor = self._connection.cursor()
                    cursor.execute("SET NAMES utf8mb4 COLLATE utf8mb4_general_ci")
                    cursor.close()
                    logger.debug("Set MariaDB-compatible collation: utf8mb4_general_ci")
                except MySQLError as collation_err:
                    logger.warning(f"Could not set MariaDB collation: {collation_err}")
            
        except MySQLError as err:
            self._handle_mysql_error(err)
    
    def _get_ssl_cipher(self) -> Optional[str]:
        """
        Query the negotiated SSL cipher for the current session.
        
        Returns:
            The cipher name if the session is encrypted, an empty string if
            the session is confirmed unencrypted, or None if the encryption
            state could not be determined.
        """
        cursor = None
        try:
            cursor = self._connection.cursor()
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            row = cursor.fetchone()
            if row is not None and len(row) > 1:
                return row[1] if row[1] else ''
            return None
        except MySQLError as err:
            logger.debug(f"Could not determine session encryption state: {err}")
            return None
        finally:
            if cursor is not None:
                cursor.close()
    
    def _verify_encryption(self) -> None:
        """
        Verify the encryption state of the established session and enforce
        the configured TLS policy.
        
        - Encrypted without CA verification: log a warning (MITM risk).
        - Unencrypted with require_ssl: close the connection and raise.
        - Unencrypted without require_ssl (server lacks TLS): log a warning.
        
        Raises:
            ConnectionError: If require_ssl is set and the session is not
                encrypted
        """
        cipher = self._get_ssl_cipher()
        
        if cipher:
            logger.debug(f"Session encrypted with cipher: {cipher}")
            if not self._config.ssl_ca:
                logger.warning(
                    "Session is encrypted (TLS negotiated with the server), "
                    "but the server certificate was not verified because no "
                    "CA file was provided; this is the default behavior. To "
                    "also verify the server identity and protect against "
                    "man-in-the-middle, pass --ssl-ca <file>. For RDS and "
                    f"Aurora, use the global bundle: "
                    f"{RDS_GLOBAL_TRUST_BUNDLE_URL}"
                )
            return
        
        if cipher is None:
            # Encryption state undetermined. Fail closed when TLS is
            # required; otherwise skip verification without false warnings.
            if self._config.require_ssl:
                self.close()
                raise ConnectionError(
                    f"SSL/TLS was required (--use-encrypted-connection) but "
                    f"the session encryption state for the server at "
                    f"{self._config.host}:{self._config.port} could not be "
                    f"verified."
                )
            logger.debug(
                "Session encryption state could not be determined; "
                "skipping encryption verification"
            )
            return
        
        # cipher == '' -> session confirmed unencrypted
        if self._config.require_ssl:
            self.close()
            raise ConnectionError(
                f"SSL/TLS was required (--use-encrypted-connection) but the "
                f"server at {self._config.host}:{self._config.port} did not "
                f"negotiate an encrypted connection. Verify the server "
                f"supports SSL/TLS, or remove --use-encrypted-connection."
            )
        
        logger.warning(
            f"Connection to {self._config.host}:{self._config.port} is NOT "
            f"encrypted: the server did not negotiate SSL/TLS. Credentials "
            f"and query results will transit in plaintext."
        )
    
    def _build_connection_params(self) -> dict:
        """
        Build connection parameters dictionary for mysql.connector.
        
        Returns:
            Dictionary of connection parameters
        """
        params = {
            'host': self._config.host,
            'port': self._config.port,
            'user': self._config.user,
            'password': self._config.password,
            'connection_timeout': self._config.connect_timeout,
            # Allow multiple statements for complex queries
            'allow_local_infile': False,
            # Get warnings as part of results
            'get_warnings': self._config.get_warnings,
            # Pin the pure-Python implementation. The connector defaults to
            # the C extension when it is installed, and that build blocks
            # Python signal handlers for the whole of a running statement:
            # measured against MySQL 8.0.40, a SIGINT sent 3s into a 15s
            # query ran its handler only at 15s with use_pure=False, versus
            # 3.0s with use_pure=True. Interrupt handling depends on the
            # handler running mid-statement to issue KILL QUERY, and the
            # socket abort path needs the pure connection's _socket, so the
            # C extension would make Ctrl-C inert. Collection queries are
            # small and row-limited, so the C extension's throughput is not
            # worth that.
            'use_pure': True,
        }
        
        # Configure SSL/TLS
        # TLS is always attempted. There is no plaintext opt-out; an
        # unencrypted session happens only when the server declines TLS,
        # and _verify_encryption() reports or rejects that after connecting.
        params['ssl_disabled'] = False
        if self._config.ssl_ca:
            # Use provided CA certificate for verification
            params['ssl_ca'] = self._config.ssl_ca
            params['ssl_verify_cert'] = True
            params['ssl_verify_identity'] = True
            logger.debug(f"SSL enabled with CA certificate: {self._config.ssl_ca}")
        else:
            # SSL without CA verification (warned about post-connect)
            params['ssl_verify_cert'] = False
            params['ssl_verify_identity'] = False
            logger.debug("SSL enabled without CA certificate verification")
        
        return params
    
    def _handle_mysql_error(self, err: MySQLError) -> None:
        """
        Handle MySQL connection errors with descriptive messages.
        
        Args:
            err: MySQL error to handle
            
        Raises:
            ConnectionError: Always raised with descriptive message
        """
        error_msg = self._format_error_message(err)
        logger.error(error_msg)
        raise ConnectionError(error_msg)
    
    def _format_error_message(self, err: MySQLError) -> str:
        """
        Format MySQL error into descriptive message.
        
        Handles authentication errors and unsupported
        authentication plugins.
        
        Args:
            err: MySQL error to format
            
        Returns:
            Descriptive error message string
        """
        # Check for authentication-related errors
        if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
            return (
                f"Authentication failed for user '{self._config.user}' "
                f"at {self._config.host}:{self._config.port}. "
                f"Please verify username and password are correct."
            )
        
        if err.errno == errorcode.ER_ACCESS_DENIED_NO_PASSWORD_ERROR:
            return (
                f"Authentication failed for user '{self._config.user}' "
                f"at {self._config.host}:{self._config.port}. "
                f"Password was not provided but is required."
            )
        
        # Check for unsupported authentication plugin
        if err.errno == errorcode.CR_AUTH_PLUGIN_CANNOT_LOAD:
            # Extract plugin name from error message if possible
            plugin_name = self._extract_plugin_name(str(err))
            if plugin_name and plugin_name not in SUPPORTED_AUTH_PLUGINS:
                return (
                    f"Unsupported authentication plugin '{plugin_name}'. "
                    f"Supported plugins are: {', '.join(sorted(SUPPORTED_AUTH_PLUGINS))}."
                )
            return (
                f"Failed to load authentication plugin. "
                f"Supported plugins are: {', '.join(sorted(SUPPORTED_AUTH_PLUGINS))}. "
                f"Error: {err}"
            )
        
        # Check for SSL-related errors
        if 'ssl' in str(err).lower() or 'tls' in str(err).lower():
            if self._config.ssl_ca:
                return (
                    f"SSL/TLS connection failed to {self._config.host}:{self._config.port}. "
                    f"CA certificate file: {self._config.ssl_ca}. "
                    f"Please verify the CA certificate is valid and the server supports SSL. "
                    f"Error: {err}"
                )
            return (
                f"SSL/TLS connection failed to {self._config.host}:{self._config.port}. "
                f"Please verify the server supports SSL/TLS connections. "
                f"Error: {err}"
            )
        
        # Check for connection refused / host not found
        if err.errno == errorcode.CR_CONN_HOST_ERROR:
            return (
                f"Cannot connect to MySQL server at {self._config.host}:{self._config.port}. "
                f"Please verify the host is reachable and MySQL is running."
            )
        
        if err.errno == errorcode.CR_UNKNOWN_HOST:
            return (
                f"Unknown host '{self._config.host}'. "
                f"Please verify the hostname is correct."
            )
        
        if err.errno == errorcode.CR_CONNECTION_ERROR:
            return (
                f"Connection error to {self._config.host}:{self._config.port}. "
                f"Please verify the server is running and accepting connections. "
                f"Error: {err}"
            )
        
        # Check for connection timeout
        if 'timeout' in str(err).lower():
            return (
                f"Connection timeout after {self._config.connect_timeout} seconds "
                f"connecting to {self._config.host}:{self._config.port}. "
                f"Please verify network connectivity and server availability."
            )
        
        # Generic error message
        return (
            f"Failed to connect to {self._config.host}:{self._config.port}: {err}"
        )
    
    def _extract_plugin_name(self, error_message: str) -> Optional[str]:
        """
        Extract authentication plugin name from error message.
        
        Args:
            error_message: Error message string
            
        Returns:
            Plugin name if found, None otherwise
        """
        # Common patterns in mysql-connector-python error messages
        import re
        patterns = [
            r"plugin '([^']+)'",
            r"authentication plugin ([^\s]+)",
            r"auth_plugin=([^\s,]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, error_message, re.IGNORECASE)
            if match:
                return match.group(1)
        return None
    
    def _detect_engine(self) -> EngineType:
        """
        Detect the database engine type from VERSION() and aurora_version variable.
        
        Detection logic:
        - Aurora MySQL: aurora_version variable exists
        - MariaDB: VERSION() contains "MariaDB"
        - MySQL: Default fallback
        
        When engine cannot be determined, logs a warning and defaults to MySQL.
        
        Returns:
            EngineType enum value representing the detected engine
            
        Raises:
            UnsupportedVersionError: If database version is below minimum supported
        """
        if self._connection is None or not self._connection.is_connected():
            logger.warning(
                "Cannot detect engine type: no active connection. "
                "Defaulting to MySQL."
            )
            return EngineType.MYSQL
        
        cursor = None
        try:
            cursor = self._connection.cursor()
            
            # Query VERSION() to get the database version string
            cursor.execute("SELECT VERSION()")
            version_result = cursor.fetchone()
            version_string = version_result[0] if version_result else ""
            logger.debug(f"Database VERSION(): {version_string}")
            
            # Store version string for later use
            self._version_string = version_string
            
            # Check for MariaDB first (VERSION() contains "MariaDB")
            if "mariadb" in version_string.lower():
                logger.debug("Detected MariaDB from VERSION() string")
                self._validate_version(version_string, EngineType.MARIADB)
                return EngineType.MARIADB
            
            # Check for Aurora MySQL by querying aurora_version variable
            try:
                cursor.execute("SHOW VARIABLES LIKE 'aurora_version'")
                aurora_result = cursor.fetchone()
                if aurora_result is not None:
                    aurora_version = aurora_result[1] if len(aurora_result) > 1 else None
                    logger.debug(f"Detected Aurora MySQL, aurora_version: {aurora_version}")
                    self._validate_version(aurora_version, EngineType.AURORA_MYSQL)
                    return EngineType.AURORA_MYSQL
            except MySQLError as aurora_err:
                # aurora_version variable doesn't exist - not Aurora
                logger.debug(f"aurora_version variable not found: {aurora_err}")
            
            # Default to standard MySQL
            logger.debug("Defaulting to standard MySQL")
            self._validate_version(version_string, EngineType.MYSQL)
            return EngineType.MYSQL
            
        except MySQLError as err:
            # When engine cannot be determined, log warning and use MySQL
            logger.warning(
                f"Could not determine database engine type: {err}. "
                f"Defaulting to MySQL for generic query compatibility."
            )
            return EngineType.MYSQL
        finally:
            if cursor is not None:
                cursor.close()
    
    def _parse_version(self, version_string: str) -> tuple:
        """
        Parse version string into a tuple of integers.
        
        Handles formats like:
        - "8.0.35" (MySQL)
        - "8.0.35-0ubuntu0.22.04.1" (MySQL with suffix)
        - "11.4.2-MariaDB" (MariaDB)
        - "3.05.2" (Aurora)
        
        Args:
            version_string: Version string to parse
            
        Returns:
            Tuple of (major, minor, patch) integers
        """
        import re
        
        if not version_string:
            return (0, 0, 0)
        
        # Extract version numbers from the beginning of the string
        # Match patterns like "8.0.35", "11.4.2", "3.05.2"
        match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', version_string)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            patch = int(match.group(3)) if match.group(3) else 0
            return (major, minor, patch)
        
        return (0, 0, 0)
    
    def _validate_version(self, version_string: str, engine_type: EngineType) -> None:
        """
        Validate that the database version meets minimum requirements.
        
        Minimum supported versions:
        - MySQL: 8.0.0
        - MariaDB: 11.4.0
        - Aurora MySQL: 3.0.0
        
        Args:
            version_string: Version string from database
            engine_type: Detected engine type
            
        Raises:
            UnsupportedVersionError: If version is below minimum supported
        """
        version_tuple = self._parse_version(version_string)
        
        if engine_type == EngineType.MYSQL:
            min_version = MIN_MYSQL_VERSION
            engine_name = "MySQL"
        elif engine_type == EngineType.MARIADB:
            min_version = MIN_MARIADB_VERSION
            engine_name = "MariaDB"
        elif engine_type == EngineType.AURORA_MYSQL:
            min_version = MIN_AURORA_VERSION
            engine_name = "Aurora MySQL"
        else:
            # Unknown engine type, skip validation
            return
        
        if version_tuple < min_version:
            min_version_str = '.'.join(str(v) for v in min_version)
            detected_version_str = '.'.join(str(v) for v in version_tuple)
            raise UnsupportedVersionError(
                f"Unsupported {engine_name} version: {detected_version_str} "
                f"(detected from '{version_string}'). "
                f"Minimum supported version is {min_version_str}."
            )
    
    def get_engine_type(self) -> EngineType:
        """
        Return the detected engine type.
        
        The engine type is detected automatically during connect() and stored
        for query filtering
        
        Returns:
            EngineType enum value (MYSQL, AURORA_MYSQL, or MARIADB)
            
        Raises:
            ConnectionError: If no active connection exists (engine not yet detected)
        """
        if self._engine_type is None:
            if self._connection is None or not self._connection.is_connected():
                raise ConnectionError(
                    "Engine type not available. Call connect() first to establish "
                    "connection and detect engine type."
                )
            # If connected but engine type not set, detect it now
            self._engine_type = self._detect_engine()
        return self._engine_type
    
    def get_version_string(self) -> Optional[str]:
        """
        Return the database version string.
        
        The version string is captured during engine detection in connect().
        
        Returns:
            Version string (e.g., "8.0.35", "11.4.2-MariaDB", "3.05.2") or None
        """
        return self._version_string
    
    def get_connection(self) -> mysql.connector.MySQLConnection:
        """
        Return the active database connection.
        
        Returns:
            Active MySQLConnection object
            
        Raises:
            ConnectionError: If no active connection exists
        """
        if self._connection is None or not self._connection.is_connected():
            raise ConnectionError(
                "No active database connection. Call connect() first."
            )
        return self._connection
    
    def is_connected(self) -> bool:
        """
        Check if connection is active.
        
        Returns:
            True if connection is active, False otherwise
        """
        return self._connection is not None and self._connection.is_connected()
    
    def _capture_connection_id(self) -> None:
        """
        Record the server-side connection id of the active session.
        
        The id is what KILL QUERY needs to cancel a statement running on
        this connection from another session. Failure to capture it is
        non-fatal: interrupts then fall back to flag-only behavior.
        """
        try:
            cursor = self._connection.cursor()
            try:
                cursor.execute("SELECT CONNECTION_ID()")
                row = cursor.fetchone()
            finally:
                cursor.close()
            self._connection_id = int(row[0]) if row and row[0] is not None else None
            logger.debug(f"Captured connection id: {self._connection_id}")
        except Exception as err:
            # Deliberately broad: this runs inside connect(), whose handler
            # only catches MySQLError, so anything else escaping here would
            # fail the whole connection over an optional capture. Losing the
            # id only costs the interrupt path its KILL QUERY.
            self._connection_id = None
            logger.debug(f"Could not capture connection id: {err}")
    
    def _current_connection_id(self) -> Optional[int]:
        """
        Best available server-side id for the active session.
        
        Prefers the connector's own ``connection_id``, which it refreshes
        when it reconnects, over the value captured at connect time. A
        reconnect underneath us would otherwise leave a stale id, and
        KILL QUERY on a stale id can name a different session. Falls back
        to the captured id when the connector does not expose one.
        """
        live_id = getattr(self._connection, 'connection_id', None)
        if live_id is not None:
            try:
                return int(live_id)
            except (TypeError, ValueError):
                logger.debug(f"Connector reported unusable connection id: {live_id!r}")
        return self._connection_id
    
    def _raw_socket(self):
        """
        The live socket under the connector, or None when this build does
        not expose one.
        
        Single place that reaches for ``_socket.sock``, which is private
        connector API. Probed at connect time and used by
        abort_connection(), so a rename upstream shows up in the log once
        rather than only on an interrupt.
        """
        sock = getattr(self._connection, '_socket', None)
        return getattr(sock, 'sock', None) if sock is not None else None
    
    def _probe_abort_capability(self) -> None:
        """
        Record whether the second-interrupt socket abort will be available.
        
        abort_connection() depends on private connector internals. Probing
        once at connect makes a missing socket visible in the log up front,
        instead of only when a user presses Ctrl-C twice.
        """
        if self._raw_socket() is None:
            logger.debug(
                "Connection socket is not exposed by this connector build: "
                "a second interrupt will exit without waiting for cleanup "
                "rather than aborting the socket"
            )
        else:
            logger.debug("Connection socket available for interrupt abort")
    
    def kill_current_query(self) -> bool:
        """
        Cancel the statement currently running on this manager's connection.
        
        Opens a short-lived side connection with the same credentials and
        issues KILL QUERY <connection_id>, the same mechanism the mysql
        client uses on Ctrl-C. Killing a statement on your own connection
        requires no additional privilege. The main connection survives:
        the blocked statement returns with ER_QUERY_INTERRUPTED (1317)
        and the session remains usable.
        
        Safe to call from a signal handler's helper thread. Never raises.
        
        Returns:
            True if KILL QUERY was delivered, False otherwise.
        """
        connection_id = self._current_connection_id()
        if connection_id is None:
            logger.warning(
                "Cannot cancel the running query: connection id unknown"
            )
            return False
        
        side_connection = None
        try:
            params = self._build_connection_params()
            # Keep the cancel path snappy; do not inherit a long timeout.
            params['connection_timeout'] = min(
                self._config.connect_timeout or 10, 10
            )
            side_connection = mysql.connector.connect(**params)
            cursor = side_connection.cursor()
            # Safe to interpolate only because the id is an int: both
            # _capture_connection_id and _current_connection_id cast it.
            # Keep the cast if this is ever refactored: KILL takes no
            # placeholders, so there is no parameterized form to fall back on.
            cursor.execute(f"KILL QUERY {connection_id}")
            cursor.close()
            logger.info(
                f"Sent KILL QUERY {connection_id}: the running statement "
                f"was cancelled server-side"
            )
            return True
        except Exception as err:
            # Deliberately broad: this runs on a daemon thread from a
            # signal handler, where an escaping exception (an OSError from
            # the socket layer, a failure building parameters) would be
            # reported as a crash on the interrupt path. Reporting the
            # failed cancellation is the whole contract.
            logger.warning(f"KILL QUERY {connection_id} failed: {err}")
            return False
        finally:
            if side_connection is not None:
                try:
                    side_connection.close()
                except Exception:
                    pass
    
    def abort_connection(self) -> bool:
        """
        Forcibly shut down the underlying socket without talking to the
        server.
        
        Used on a second interrupt, when the caller has asked to exit now.
        A normal close() sends COM_QUIT and can block for as long as the
        server takes to finish an in-flight statement (measured at ~54s
        against a statement left running with a 60s timeout), which would
        defeat the point of the bail-out. Shutting the socket down first
        makes both the blocked read and the subsequent close() return
        immediately.
        
        This reaches into connector internals (``_socket.sock``), which are
        private and absent on some builds (the C extension connection has
        no ``_socket``). That is reported rather than hidden, because the
        caller must then exit without waiting for cleanup.
        
        Never raises: the connection is being abandoned either way.
        
        Returns:
            True if the socket was shut down, False if it could not be
            reached, in which case cleanup may still block and the caller
            should exit without it.
        """
        connection = self._connection
        if connection is None:
            return False
        
        raw_socket = self._raw_socket()
        if raw_socket is None:
            logger.warning(
                "Could not reach the connection socket to abort it; this "
                "connector build does not expose it. Exiting without "
                "waiting for connection cleanup."
            )
            return False
        
        try:
            raw_socket.shutdown(socket.SHUT_RDWR)
            logger.debug("Connection socket shut down for immediate exit")
            return True
        except OSError as err:
            logger.warning(
                f"Could not shut down connection socket ({err}). Exiting "
                f"without waiting for connection cleanup."
            )
            return False
    
    def close(self) -> None:
        """
        Close the database connection gracefully.
        
        This method safely closes the connection if it exists and is active.
        Also resets the detected engine type.
        """
        if self._connection is not None:
            try:
                if self._connection.is_connected():
                    self._connection.close()
                    logger.info("Database connection closed")
            except MySQLError as err:
                logger.warning(f"Error closing connection: {err}")
            finally:
                self._connection = None
                self._engine_type = None
                self._connection_id = None
    
    def __enter__(self) -> 'ConnectionManager':
        """Context manager entry - establish connection."""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - close connection."""
        self.close()
