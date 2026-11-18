# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for connection back-off measurement.

The connection guard has two independent parts. Concurrency counts connections
currently executing a statement, and slot pressure measures how much of
max_connections is occupied, including sleeping sessions that hold a slot without
executing anything. A single connection count cannot express both, which is why
both are measured and asserted separately here.
"""

import logging
import unittest
from unittest.mock import MagicMock

import mysql.connector

from diagnostic_collector_mysql.core.query_executor import (
    BackOffError,
    QueryExecutor,
    format_backoff_message,
)

TABLE_COUNT_OK = (100,)


class ConnectionBackOffTestCase(unittest.TestCase):
    """Shared fixture for the connection back-off guards."""

    def setUp(self):
        self.mock_connection = MagicMock()
        self.mock_cursor = MagicMock()
        self.mock_connection.cursor.return_value = self.mock_cursor
        self.logger = logging.getLogger('test_connection_backoff')
        self.logger.setLevel(logging.DEBUG)

    def _executor(self, connection_threshold=1000, connection_usage_threshold=90):
        return QueryExecutor(
            connection=self.mock_connection,
            output_dir='/tmp/test_output',
            timestamp='20260902_120000',
            query_timeout=30,
            connection_threshold=connection_threshold,
            objects_threshold=10000,
            logger=self.logger,
            connection_usage_threshold=connection_usage_threshold,
        )

    @staticmethod
    def _row(active, threads_connected=10, max_connections=1000, pfs=1):
        return (pfs, max_connections, threads_connected, active)

    def _executed_sql(self):
        return [str(call) for call in self.mock_cursor.execute.call_args_list]


class TestMeasuredPopulation(ConnectionBackOffTestCase):
    """What the guard counts, and what it deliberately does not count."""

    def test_excludes_the_collectors_own_connection(self):
        """The session running the count would otherwise consume one unit of the
        operator's threshold."""
        self.assertIn('CONNECTION_ID()', QueryExecutor.CONNECTION_CHECK_SQL)
        self.assertIn('PROCESSLIST_ID != CONNECTION_ID()', QueryExecutor.CONNECTION_CHECK_SQL)
        self.assertIn('id != CONNECTION_ID()', QueryExecutor.CONNECTION_CHECK_FALLBACK_SQL)

    def test_excludes_daemon_threads(self):
        """event_scheduler runs as a daemon and is not client work."""
        self.assertIn("'Daemon'", QueryExecutor.CONNECTION_CHECK_SQL)
        self.assertIn("NOT IN ('Sleep', 'Daemon')", QueryExecutor.CONNECTION_CHECK_FALLBACK_SQL)

    def test_excludes_sleeping_sessions_from_the_concurrency_count(self):
        """A parked connection is not doing work, so it does not belong in a
        concurrency measure. It is caught by the slot guard instead, which is why
        both guards exist."""
        self.assertIn(
            "PROCESSLIST_COMMAND NOT IN ('Sleep', 'Daemon')",
            QueryExecutor.CONNECTION_CHECK_SQL,
        )

    def test_reads_thread_attributes_from_performance_schema(self):
        """performance_schema.threads avoids the processlist mutex and is readable
        without the PROCESS privilege."""
        sql = QueryExecutor.CONNECTION_CHECK_SQL
        self.assertIn('performance_schema.threads', sql)
        self.assertIn("TYPE = 'FOREGROUND'", sql)

    def test_reads_everything_in_one_round_trip(self):
        """A single check, so the guard cannot itself add load."""
        self.mock_cursor.fetchone.side_effect = [self._row(500), TABLE_COUNT_OK]
        self._executor().check_backoff_thresholds()
        connection_queries = [
            sql for sql in self._executed_sql() if 'CONNECTION_ID()' in sql
        ]
        self.assertEqual(len(connection_queries), 1)


class TestPerformanceSchemaFallback(ConnectionBackOffTestCase):
    """performance_schema.threads returns no rows when instrumentation is off."""

    def test_falls_back_to_processlist_when_performance_schema_disabled(self):
        # pfs=0, so the count in the first row is not usable; the fallback reports 42.
        self.mock_cursor.fetchone.side_effect = [
            self._row(0, pfs=0),
            (42,),
            (42,),                # unfiltered rows visible, not blind
            TABLE_COUNT_OK,
        ]
        with self.assertRaises(BackOffError) as ctx:
            self._executor(connection_threshold=41).check_backoff_thresholds()
        self.assertEqual(ctx.exception.current_value, 42)
        self.assertTrue(
            any('information_schema.processlist' in sql for sql in self._executed_sql())
        )

    def test_does_not_query_processlist_when_performance_schema_enabled(self):
        """The processlist read takes a global mutex, so it is avoided where the
        performance_schema source is available."""
        self.mock_cursor.fetchone.side_effect = [self._row(500), TABLE_COUNT_OK]
        self._executor().check_backoff_thresholds()
        self.assertFalse(
            any('information_schema.processlist' in sql for sql in self._executed_sql())
        )

    def test_fallback_warns_that_the_count_may_under_report(self):
        """Without the PROCESS privilege the processlist view shows only the
        caller's own threads. The warning fires on the evidence that it happened,
        which is the account seeing fewer rows than there are connections."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(0, threads_connected=8, pfs=0),
            (1,),                 # filtered concurrency count
            (1,),                 # unfiltered rows visible: 1 of 8 -> blind
            TABLE_COUNT_OK,
        ]
        with self.assertLogs('test_connection_backoff', level='WARNING') as logs:
            self._executor().check_backoff_thresholds()
        joined = ' '.join(logs.output)
        self.assertIn('PROCESS', joined)
        self.assertIn('under-report', joined)

    def test_fallback_warning_names_both_remedies(self):
        """An operator reading this warning mid-run needs to know which remedy
        applies immediately. Granting PROCESS takes effect on the next connection,
        and because the collector connects once per run, that means the next run.
        performance_schema is a read-only variable set at server startup, so
        enabling it needs a restart, and saying so keeps the warning actionable.
        """
        self.mock_cursor.fetchone.side_effect = [
            self._row(0, threads_connected=8, pfs=0),
            (1,),
            (1,),
            TABLE_COUNT_OK,
        ]
        with self.assertLogs('test_connection_backoff', level='WARNING') as logs:
            self._executor().check_backoff_thresholds()
        joined = ' '.join(logs.output)
        self.assertIn('GRANT PROCESS', joined)
        self.assertIn('restart', joined)
        self.assertIn('next run', joined)

    def test_no_under_report_warning_when_the_account_sees_every_connection(self):
        """No warning for an account that sees every connection.

        The documented minimum grants include PROCESS, so such an account counts
        every busy connection even with performance_schema off. Warning it would
        be noise that trains operators to ignore a real signal, and MariaDB ships
        performance_schema off, so the noise would reach every MariaDB run."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(0, threads_connected=8, pfs=0),
            (6,),                 # filtered concurrency count
            (9,),                 # unfiltered rows visible: 9 >= 8 -> not blind
            TABLE_COUNT_OK,
        ]
        with self.assertNoLogs('test_connection_backoff', level='WARNING'):
            self._executor().check_backoff_thresholds()


class TestConcurrencyGuard(ConnectionBackOffTestCase):
    """--connection-back-off, counting connections executing a statement."""

    def test_exceeding_the_threshold_backs_off(self):
        self.mock_cursor.fetchone.return_value = self._row(1500)
        with self.assertRaises(BackOffError) as ctx:
            self._executor(connection_threshold=1000).check_backoff_thresholds()
        self.assertEqual(ctx.exception.current_value, 1500)
        self.assertIn('connection count', ctx.exception.reason.lower())

    def test_sleeping_connections_do_not_trip_it(self):
        """21 connections are open but only 2 are executing, so concurrency is low
        while slot usage is not. This is the distinction the second guard exists
        for, and why one number cannot serve both."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=21, max_connections=151),
            TABLE_COUNT_OK,
        ]
        self._executor(connection_threshold=5).check_backoff_thresholds()


class TestSlotUsageGuard(ConnectionBackOffTestCase):
    """--connection-usage-back-off, measuring occupancy of max_connections."""

    def test_exceeding_the_percentage_backs_off(self):
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=95, max_connections=100),
            TABLE_COUNT_OK,
        ]
        with self.assertRaises(BackOffError) as ctx:
            self._executor(connection_usage_threshold=90).check_backoff_thresholds()
        self.assertEqual(ctx.exception.current_value, 95)
        self.assertEqual(ctx.exception.threshold, 90)
        self.assertIn('slot usage', ctx.exception.reason.lower())

    def test_exactly_at_the_threshold_passes(self):
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=90, max_connections=100),
            TABLE_COUNT_OK,
        ]
        self._executor(connection_usage_threshold=90).check_backoff_thresholds()

    def test_zero_disables_the_guard(self):
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=100, max_connections=100),
            TABLE_COUNT_OK,
        ]
        self._executor(connection_usage_threshold=0).check_backoff_thresholds()

    def test_is_proportional_so_it_fires_on_any_instance_size(self):
        """An absolute count has to be tuned per instance. A small instance and a
        large one at the same proportion both trip."""
        for max_connections in (151, 1000, 20000):
            with self.subTest(max_connections=max_connections):
                self.mock_cursor.reset_mock(side_effect=True)
                self.mock_cursor.fetchone.side_effect = [
                    self._row(
                        2,
                        threads_connected=int(max_connections * 0.95),
                        max_connections=max_connections,
                    ),
                    TABLE_COUNT_OK,
                ]
                with self.assertRaises(BackOffError):
                    self._executor(connection_usage_threshold=90).check_backoff_thresholds()

    def test_missing_max_connections_reports_the_guard_as_disabled(self):
        """Rather than dividing by zero, or quietly computing 0% and never firing."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=50, max_connections=0),
            TABLE_COUNT_OK,
        ]
        with self.assertLogs('test_connection_backoff', level='WARNING') as logs:
            self._executor(connection_usage_threshold=90).check_backoff_thresholds()
        self.assertIn('DISABLED', ' '.join(logs.output))

    def test_unreadable_connected_count_reports_the_guard_as_disabled(self):
        """This session is connected, so a count of 0 means the value could not be
        read. Treating it as 0% would leave the guard permanently silent. The
        portable read is attempted first; only when that also yields nothing is
        the guard reported as skipped."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=0, max_connections=151),
            ('Threads_connected', None),   # portable read also yields nothing
            TABLE_COUNT_OK,
        ]
        with self.assertLogs('test_connection_backoff', level='WARNING') as logs:
            self._executor(connection_usage_threshold=90).check_backoff_thresholds()
        self.assertIn('DISABLED', ' '.join(logs.output))

    def test_null_threads_connected_is_read_through_show_global_status(self):
        """MariaDB ships performance_schema off and then leaves
        performance_schema.global_status empty, where MySQL keeps it populated, so
        the subquery returns NULL and the slot guard would never run. Verified on
        MariaDB 11.4.9: 0 rows in that table with 26 connections open, while
        SHOW GLOBAL STATUS returned 26. Falling back to it makes the guard work on
        both engines."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(0, threads_connected=0, max_connections=151, pfs=0),
            ('Threads_connected', '140'),  # portable read: 140 of 151 = 92.7%
            (0,),                          # filtered concurrency count
            (0,),                          # unfiltered rows visible
            TABLE_COUNT_OK,
        ]
        with self.assertRaises(BackOffError) as ctx:
            self._executor(connection_usage_threshold=90).check_backoff_thresholds()
        self.assertIn('slot usage', ctx.exception.reason.lower())
        self.assertTrue(
            any('SHOW GLOBAL STATUS' in sql for sql in self._executed_sql()),
            "the portable read should have been used"
        )

    def test_portable_read_is_not_used_when_the_subquery_answers(self):
        """On MySQL the subquery works with performance_schema either way, so the
        extra statement must not be issued there."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=21, max_connections=151),
            TABLE_COUNT_OK,
        ]
        self._executor().check_backoff_thresholds()
        self.assertFalse(
            any('SHOW GLOBAL STATUS' in sql for sql in self._executed_sql())
        )

    def test_usage_just_over_the_threshold_is_caught(self):
        """Rounding before the comparison let real breaches pass: 904 of 1000 is
        90.40% and 181 of 200 is 90.50%, and both rounded to 90 and passed a
        threshold of 90, the second because Python rounds halves to even. The help
        text promises "more than PCT percent". max_connections is 1000 on an
        Aurora db.r6g.large, so the first case is reachable in practice."""
        for connected, maxc in ((904, 1000), (181, 200), (905, 1000)):
            with self.subTest(connected=connected, max_connections=maxc):
                self.mock_cursor.reset_mock(side_effect=True)
                self.mock_cursor.fetchone.side_effect = [
                    self._row(2, threads_connected=connected, max_connections=maxc),
                    TABLE_COUNT_OK,
                ]
                with self.assertRaises(BackOffError) as ctx:
                    self._executor(
                        connection_usage_threshold=90
                    ).check_backoff_thresholds()
                # Rounded up, so raising the threshold to the suggested value
                # actually lets the run proceed.
                self.assertGreater(ctx.exception.current_value, 90)

    def test_usage_exactly_at_the_threshold_still_passes(self):
        """The comparison is strictly greater, so 90.0% against a threshold of 90
        proceeds. Only values above it back off."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=900, max_connections=1000),
            TABLE_COUNT_OK,
        ]
        self._executor(connection_usage_threshold=90).check_backoff_thresholds()

    def test_slot_usage_is_read_from_a_source_that_survives_pfs_off(self):
        """performance_schema.global_status is a status variable table and stays
        readable with the instrumentation flag off, unlike performance_schema.threads."""
        self.assertIn(
            'performance_schema.global_status', QueryExecutor.CONNECTION_CHECK_SQL
        )
        self.assertIn("'Threads_connected'", QueryExecutor.CONNECTION_CHECK_SQL)


class TestPassedSummaryLine(ConnectionBackOffTestCase):
    """The summary line has to describe what was actually evaluated.

    A skipped slot guard must not be reported as passed with its threshold,
    or a run logs "Slot usage back-off protection is DISABLED for this run" and
    then "checks passed ... 0 of 151 slots in use (threshold: 90%)", which
    contradict each other.
    """

    def test_skipped_slot_check_is_not_reported_as_passed(self):
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=0, max_connections=151),
            ('Threads_connected', None),   # portable read yields nothing either
            TABLE_COUNT_OK,
        ]
        with self.assertLogs('test_connection_backoff', level='INFO') as logs:
            self._executor(connection_usage_threshold=90).check_backoff_thresholds()
        passed = [line for line in logs.output if 'checks passed' in line]
        self.assertEqual(len(passed), 1)
        self.assertIn('SKIPPED', passed[0])
        self.assertNotIn('threshold: 90%', passed[0])

    def test_disabled_slot_check_says_so_rather_than_reporting_zero_percent(self):
        """With --connection-usage-back-off 0 the line names the check as
        disabled. "(threshold: 0%)" would read as a threshold of zero."""
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=100, max_connections=100),
            TABLE_COUNT_OK,
        ]
        with self.assertLogs('test_connection_backoff', level='INFO') as logs:
            self._executor(connection_usage_threshold=0).check_backoff_thresholds()
        passed = [line for line in logs.output if 'checks passed' in line]
        self.assertEqual(len(passed), 1)
        self.assertIn('disabled', passed[0])
        self.assertNotIn('threshold: 0%', passed[0])

    def test_evaluated_slot_check_reports_the_numbers(self):
        self.mock_cursor.fetchone.side_effect = [
            self._row(2, threads_connected=21, max_connections=151),
            TABLE_COUNT_OK,
        ]
        with self.assertLogs('test_connection_backoff', level='INFO') as logs:
            self._executor(connection_usage_threshold=90).check_backoff_thresholds()
        passed = [line for line in logs.output if 'checks passed' in line]
        self.assertEqual(len(passed), 1)
        self.assertIn('21 of 151 slots in use', passed[0])
        self.assertIn('threshold: 90%', passed[0])


class TestDegradedPath(ConnectionBackOffTestCase):
    """When the guard cannot be evaluated, the run continues and says so."""

    def test_performance_schema_failure_keeps_the_concurrency_guard(self):
        """An account with no grant on performance_schema cannot run the combined
        statement. Before, that disabled both guards, which was a regression
        against the earlier implementation that queried the processlist directly.
        The degraded read routes it through the processlist instead."""
        self.mock_cursor.execute.side_effect = [
            mysql.connector.Error("SELECT command denied to user for table 'threads'"),
            None,   # degraded read
            None,   # SHOW GLOBAL STATUS
            None,   # processlist concurrency count
            None,   # processlist visible rows
            None,   # table count
        ]
        self.mock_cursor.fetchone.side_effect = [
            (0, 151, None, 0),            # degraded read
            ('Threads_connected', '30'),  # portable slot read still works
            (42,),                        # concurrency count from the processlist
            (42,),                        # visible rows, not blind
            TABLE_COUNT_OK,
        ]
        with self.assertRaises(BackOffError) as ctx:
            self._executor(connection_threshold=41).check_backoff_thresholds()
        self.assertEqual(ctx.exception.current_value, 42)
        self.assertIn('connection count', ctx.exception.reason.lower())

    def test_connector_error_is_reported_as_protection_disabled(self):
        """Both reads have to fail before protection is genuinely gone. A failure
        of the performance_schema statement alone now degrades to the processlist
        rather than disabling the guards, so this drives the case where even the
        degraded read is refused."""
        self.mock_cursor.execute.side_effect = [
            mysql.connector.Error("Access denied"),   # combined statement
            mysql.connector.Error("Access denied"),   # degraded read too
            None,                                     # table count
        ]
        self.mock_cursor.fetchone.return_value = TABLE_COUNT_OK
        with self.assertLogs('test_connection_backoff', level='WARNING') as logs:
            self._executor().check_backoff_thresholds()
        self.assertIn('DISABLED', ' '.join(logs.output))

    def test_empty_result_is_reported_as_protection_disabled(self):
        self.mock_cursor.fetchone.side_effect = [None, TABLE_COUNT_OK]
        with self.assertLogs('test_connection_backoff', level='WARNING') as logs:
            self._executor().check_backoff_thresholds()
        self.assertIn('DISABLED', ' '.join(logs.output))


class TestSlotUsageMessage(unittest.TestCase):
    """The remediation text has to name the flag that actually applies."""

    def test_reports_the_measured_percentage_not_the_rounded_suggestion(self):
        """current_value doubles as the basis for the threshold suggestion, which
        has to be an integer above the reading. Rendering that integer as the
        reading overstated a genuine 90.476% breach as 91%. The message now shows
        the measurement and derives the suggestion separately."""
        message = format_backoff_message(BackOffError(
            reason="Connection slot usage exceeds threshold",
            current_value=19 * 100 / 21,   # 90.476%
            threshold=90,
        ))
        self.assertIn('90.5%', message)
        self.assertNotIn('usage: 91%', message)
        # The suggestion still has to clear the actual usage.
        self.assertIn('--connection-usage-back-off=96', message)

    def test_does_not_recommend_raising_max_connections(self):
        """Raising max_connections frees no slot, it moves the denominator. The
        README says so for this guard, so the runtime message must not contradict
        it."""
        message = format_backoff_message(BackOffError(
            reason="Connection slot usage exceeds threshold",
            current_value=95.0,
            threshold=90,
        ))
        self.assertIn('find the sessions holding', message)
        self.assertNotIn('or raise max_connections', message)

    def test_names_the_usage_flag_and_keeps_the_suggestion_within_range(self):
        message = format_backoff_message(BackOffError(
            reason="Connection slot usage exceeds threshold",
            current_value=98,
            threshold=90,
        ))
        self.assertIn('--connection-usage-back-off', message)
        self.assertNotIn('--connection-back-off=', message)
        self.assertIn('100', message)
        self.assertNotIn('198', message)

    def test_concurrency_reason_still_names_the_count_flag(self):
        message = format_backoff_message(BackOffError(
            reason="Active connection count exceeds threshold",
            current_value=1500,
            threshold=1000,
        ))
        self.assertIn('--connection-back-off', message)
        self.assertIn('1600', message)


if __name__ == '__main__':
    unittest.main()
