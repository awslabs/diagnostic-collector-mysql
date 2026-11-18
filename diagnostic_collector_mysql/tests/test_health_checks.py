# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Health Overview evaluation.

Covers the Auto-Increment, Replication Lag and Replication Errors checks across
their critical, warning, OK and skipped states, and the invariant that every
health query ID the evaluator reads is defined in queries.json.

That invariant carries weight because each check looks up its input by query ID.
An ID that is not defined yields no value, the check's guard skips the branch, and
the check is omitted from the report with no error raised, which a reader cannot
tell apart from a healthy result.

Covers:
- Every health query ID referenced by the evaluator exists in queries.json
- Auto-Increment, Replication Lag and Replication Errors evaluate and report OK
- A health query that runs and fails is counted in Query Failures
"""

import json
import re
import unittest
from pathlib import Path

from diagnostic_collector_mysql.data import get_data_path
from diagnostic_collector_mysql.models import HEALTH_CHECK_QUERY_IDS
from diagnostic_collector_mysql.parsing.input_validator import InputValidator
from diagnostic_collector_mysql.reporting import report_generator
from diagnostic_collector_mysql.reporting.report_generator import (
    ParsedLogFile,
    _evaluate_health_checks_common,
)


def _log(query_id, columns, rows, success=True, error=None):
    """Build a ParsedLogFile the way the log parser would."""
    return ParsedLogFile(
        query_id=query_id,
        success=success,
        columns=columns,
        rows=rows,
        error=error,
        executed_timestamp="2026-09-02 12:00:00",
    )


def _statuses(checks):
    """Map check name to status for easier assertions."""
    return {name: status for status, name, _ in checks}


def _details(checks):
    """Map check name to details for easier assertions."""
    return {name: details for _, name, details in checks}


class TestHealthCheckQueryReferences(unittest.TestCase):
    """
    Assert that every health query ID the evaluator reads is defined.

    The evaluator reads health query IDs as string literals. A literal that is not
    defined in queries.json yields no value, so the check is omitted from every
    report with no error raised. Comparing the two sets mechanically is the only
    reliable way to catch that.
    """

    @staticmethod
    def _referenced_health_ids():
        source = Path(report_generator.__file__).read_text(encoding="utf-8")
        pattern = r"get_(?:val|health_value)\(\s*'(health_[a-z_]+)'"
        return set(re.findall(pattern, source))

    @staticmethod
    def _defined_query_ids():
        with open(get_data_path("queries.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        return {query["id"] for query in data["queries"]}

    def test_every_referenced_health_id_exists_in_queries_json(self):
        missing = self._referenced_health_ids() - self._defined_query_ids()
        self.assertEqual(
            missing,
            set(),
            f"Health Overview reads query IDs that do not exist: {sorted(missing)}. "
            f"Each one is silently omitted from every report.",
        )

    def test_declared_constant_matches_the_source(self):
        """HEALTH_CHECK_QUERY_IDS is what the startup check validates, so it must
        not drift from the IDs the evaluator actually reads."""
        self.assertEqual(
            self._referenced_health_ids(),
            set(HEALTH_CHECK_QUERY_IDS),
            "HEALTH_CHECK_QUERY_IDS has drifted from the IDs read in "
            "report_generator.py. Update the constant.",
        )

    def test_declared_constant_is_fully_defined(self):
        missing = set(HEALTH_CHECK_QUERY_IDS) - self._defined_query_ids()
        self.assertEqual(missing, set(), f"Not defined in queries.json: {sorted(missing)}")


class TestValidateHealthCheckReferences(unittest.TestCase):
    """The startup check that makes an undefined health query ID fail fast."""

    def setUp(self):
        self.validator = InputValidator()

    def test_no_errors_when_all_ids_present(self):
        errors = self.validator.validate_health_check_references(set(HEALTH_CHECK_QUERY_IDS))
        self.assertEqual(errors, [])

    def test_reports_each_missing_id(self):
        present = set(HEALTH_CHECK_QUERY_IDS) - {
            "health_auto_increment_critical",
            "health_replication_errors",
        }
        errors = self.validator.validate_health_check_references(present)
        self.assertEqual(len(errors), 2)
        located = " ".join(error.location for error in errors)
        self.assertIn("health_auto_increment_critical", located)
        self.assertIn("health_replication_errors", located)
        self.assertTrue(all(error.file == "queries.json" for error in errors))

    def test_shipped_queries_json_passes(self):
        """The real file must satisfy the check, otherwise the tool cannot start."""
        _, query_ids = self.validator.get_query_ids()
        self.assertEqual(self.validator.validate_health_check_references(query_ids), [])


class TestAutoIncrementCheck(unittest.TestCase):
    """Counts AUTO_INCREMENT columns past 90% of their data type range."""

    def test_critical_when_columns_are_near_full(self):
        logs = {
            "health_auto_increment_critical": _log(
                "health_auto_increment_critical", ["auto_increment_critical_count"], [(3,)]
            )
        }
        checks = _evaluate_health_checks_common(logs)
        self.assertEqual(_statuses(checks)["Auto-Increment >90% Full"], "critical")
        self.assertIn("3 column(s)", _details(checks)["Auto-Increment >90% Full"])

    def test_ok_when_count_is_zero(self):
        logs = {
            "health_auto_increment_critical": _log(
                "health_auto_increment_critical", ["auto_increment_critical_count"], [(0,)]
            )
        }
        checks = _evaluate_health_checks_common(logs)
        self.assertEqual(_statuses(checks)["Auto-Increment Usage"], "ok")

    def test_absent_when_query_did_not_run(self):
        checks = _evaluate_health_checks_common({})
        names = _statuses(checks)
        self.assertNotIn("Auto-Increment >90% Full", names)
        self.assertNotIn("Auto-Increment Usage", names)


class TestReplicationErrorsCheck(unittest.TestCase):
    """Counts applier workers reporting an error, and reports OK when none do."""

    def test_critical_when_workers_report_errors(self):
        logs = {
            "health_replication_errors": _log(
                "health_replication_errors", ["replication_error_count"], [(2,)]
            )
        }
        checks = _evaluate_health_checks_common(logs)
        self.assertEqual(_statuses(checks)["Replication Errors"], "critical")
        self.assertIn("2 worker(s)", _details(checks)["Replication Errors"])

    def test_ok_when_no_errors_on_a_replica(self):
        """A healthy replica reports OK explicitly, so it can be told apart from a
        check that did not run at all."""
        logs = {
            "health_replication_errors": _log(
                "health_replication_errors", ["replication_error_count"], [(0,)]
            )
        }
        checks = _evaluate_health_checks_common(logs)
        self.assertEqual(_statuses(checks)["Replication Errors"], "ok")

    def test_absent_on_a_source(self):
        """The query uses HAVING so it returns no rows when there is no applier."""
        logs = {
            "health_replication_errors": _log(
                "health_replication_errors", ["replication_error_count"], []
            )
        }
        checks = _evaluate_health_checks_common(logs)
        self.assertNotIn("Replication Errors", _statuses(checks))


class TestReplicationLagCheck(unittest.TestCase):
    """Reads replica_status by column name, because position is not stable."""

    def _lag_logs(self, column_name, value):
        return {
            "replica_status": _log(
                "replica_status",
                ["Replica_IO_Running", column_name, "Last_Error"],
                [("Yes", value, "")],
            )
        }

    def test_critical_above_sixty_seconds(self):
        checks = _evaluate_health_checks_common(self._lag_logs("Seconds_Behind_Source", 321))
        self.assertEqual(_statuses(checks)["Replication Lag"], "critical")
        self.assertIn("321s", _details(checks)["Replication Lag"])

    def test_warning_above_ten_seconds(self):
        checks = _evaluate_health_checks_common(self._lag_logs("Seconds_Behind_Source", 30))
        self.assertEqual(_statuses(checks)["Replication Lag"], "warning")

    def test_ok_at_zero_lag(self):
        checks = _evaluate_health_checks_common(self._lag_logs("Seconds_Behind_Source", 0))
        self.assertEqual(_statuses(checks)["Replication Lag"], "ok")

    def test_reads_the_mariadb_and_pre_8_0_23_column_name(self):
        checks = _evaluate_health_checks_common(self._lag_logs("Seconds_Behind_Master", 90))
        self.assertEqual(_statuses(checks)["Replication Lag"], "critical")

    def test_skipped_when_sql_thread_stopped(self):
        """Seconds_Behind_Source is NULL with the applier stopped. Reporting that
        as zero lag would be a false OK, so the check is skipped instead."""
        checks = _evaluate_health_checks_common(self._lag_logs("Seconds_Behind_Source", None))
        self.assertNotIn("Replication Lag", _statuses(checks))

    def test_absent_on_a_source(self):
        logs = {"replica_status": _log("replica_status", ["Replica_IO_Running"], [])}
        checks = _evaluate_health_checks_common(logs)
        self.assertNotIn("Replication Lag", _statuses(checks))

    def test_column_lookup_is_case_insensitive(self):
        checks = _evaluate_health_checks_common(self._lag_logs("seconds_behind_source", 70))
        self.assertEqual(_statuses(checks)["Replication Lag"], "critical")


class TestFailedHealthQueryIsVisible(unittest.TestCase):
    """A failed health query emits no health line, so it must reach Query Failures."""

    def test_failed_health_query_counted(self):
        logs = {
            "health_connection_usage": _log(
                "health_connection_usage", None, None, success=False, error="Access denied"
            )
        }
        checks = _evaluate_health_checks_common(logs)
        self.assertEqual(_statuses(checks)["Query Failures"], "warning")
        self.assertIn("1 of 1", _details(checks)["Query Failures"])


if __name__ == "__main__":
    unittest.main()
