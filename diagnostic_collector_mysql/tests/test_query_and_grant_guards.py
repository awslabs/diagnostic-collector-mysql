"""
Guards for failure paths that produce no error.

Every assertion here covers a failure that produces no error. A query that does not
resolve for an engine, a subsection that does not render where its query runs, a
column name the renderer cannot match, or a documented GRANT that cannot execute all
leave the report looking healthy, so none of them is caught by running the tool.

Deliberately narrow. Assertions that restate the implementation, or that pin the
absence of a key a future author might legitimately add, are left out: they fail on
edits that are correct and train people to delete them.

Covers:
- deadlock_count resolves for every supported engine
- Every deadlock_count variant returns its count where the evaluator reads it
- The Deadlock Count subsection renders on every engine its query runs on
- The MySQL variant returns the column name the subsection declares
- No documented surface tells a user to GRANT on information_schema
"""

import json
import re
import unittest
from pathlib import Path

from diagnostic_collector_mysql.data import get_data_path


def _by_id(query_id):
    """Every query row carrying this ID, since IDs are deliberately duplicated."""
    with open(get_data_path("queries.json"), encoding="utf-8") as handle:
        return [q for q in json.load(handle)["queries"] if q["id"] == query_id]


def _subsection(name):
    """The report_format subsection with this name, wherever it is nested."""
    with open(get_data_path("report_format.json"), encoding="utf-8") as handle:
        report_format = json.load(handle)
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "subsections" and isinstance(value, list):
                    found.extend(
                        s for s in value
                        if isinstance(s, dict) and s.get("name") == name
                    )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(report_format)
    return found


class TestDeadlockCountResolves(unittest.TestCase):
    """
    The evaluator reads `get_val('deadlock_count', 1)`. A missing variant yields no
    value, the guard skips the branch, and the check disappears from the report rather
    than erroring, so a reader cannot tell "no deadlocks" from "this check is broken".

    A variant gated to one engine alone leaves the check absent on the others with
    nothing in the report to show it.
    """

    SUPPORTED_ENGINES = ("mysql", "aurora-mysql", "mariadb")

    def setUp(self):
        self.variants = _by_id("deadlock_count")

    def test_every_supported_engine_resolves_exactly_one_variant(self):
        for engine in self.SUPPORTED_ENGINES:
            matching = [
                v for v in self.variants
                if engine in v.get("condition", {}).get("engine", [])
            ]
            self.assertEqual(
                len(matching),
                1,
                f"{engine} resolves {len(matching)} deadlock_count variants; zero "
                "means the check silently vanishes on that engine",
            )

    def test_every_variant_returns_the_count_where_the_evaluator_reads_it(self):
        """
        The evaluator reads column index 1 for every engine, so each variant has to
        select its label first and its count second. A variant that reversed them
        would hand a name to `int()` and fail into a bare `except ... : pass`.
        """
        for variant in self.variants:
            command = variant["command"]
            if command.strip().upper().startswith("SHOW"):
                # SHOW GLOBAL STATUS returns Variable_name, Value in that order.
                continue
            select_list = re.search(
                r"SELECT\s+(.*?)\s+FROM", command, re.IGNORECASE | re.DOTALL
            )
            self.assertIsNotNone(select_list, f"cannot parse: {command}")
            columns = [c.strip() for c in select_list.group(1).split(",")]
            self.assertEqual(len(columns), 2, f"expected two columns: {command}")
            self.assertTrue(
                columns[1].upper().startswith("COUNT"),
                f"the count must be the second column, found {columns[1]}",
            )


class TestDeadlockSubsectionWiring(unittest.TestCase):
    """
    Making the query run is not enough on its own. The Health Overview links each check to
    its subsection, so a row that renders while its subsection does not produces a
    link to an anchor that cannot exist, and the subsection matches its column by
    exact name, so a query returning different column names renders "No matching
    columns found" instead of the number.

    Enabling the query on an engine without enabling the subsection adds a dangling
    Health Overview link, and enabling the subsection without aliasing the column
    renders the note instead of the count.
    """

    def setUp(self):
        subsections = _subsection("Deadlock Count")
        self.assertEqual(
            len(subsections), 1, "expected exactly one Deadlock Count subsection"
        )
        self.subsection = subsections[0]

    def test_the_subsection_renders_on_every_engine_the_query_runs_on(self):
        query_engines = set()
        for variant in _by_id("deadlock_count"):
            query_engines.update(variant.get("condition", {}).get("engine", []))
        subsection_engines = set(self.subsection.get("condition", {}).get("engine", []))
        missing = query_engines - subsection_engines
        self.assertEqual(
            missing,
            set(),
            f"deadlock_count runs on {sorted(query_engines)} but its subsection only "
            f"renders on {sorted(subsection_engines)}, so on {sorted(missing)} the "
            "count is collected, never shown, and the Health Overview link dangles",
        )

    def test_the_mysql_variant_returns_the_column_the_subsection_declares(self):
        declared = [c for c in self.subsection["columns"] if isinstance(c, str)]
        self.assertTrue(declared, "subsection declares no plain column names")
        for variant in _by_id("deadlock_count"):
            engines = variant.get("condition", {}).get("engine", [])
            if "mysql" not in engines and "aurora-mysql" not in engines:
                continue
            for name in declared:
                self.assertIn(
                    name,
                    variant["command"],
                    f"the MySQL variant must return a column named {name!r}, or the "
                    'subsection renders "No matching columns found"',
                )


class TestDocumentedGrantsAreExecutable(unittest.TestCase):
    """
    `GRANT SELECT ON information_schema.*` fails with ERROR 1044 for every account,
    including root and the RDS or Aurora master user, because information_schema
    cannot be granted on. A documented setup that includes it fails partway when
    pasted, so neither the README nor the --help epilog may contain it.

    Only the statement lines are inspected. Prose that names the broken statement in
    order to warn about it is not a defect.
    """

    @staticmethod
    def _documented_statements():
        from diagnostic_collector_mysql.cli import MINIMUM_PRIVILEGES

        readme = (
            Path(__file__).resolve().parents[2] / "README.md"
        ).read_text(encoding="utf-8")
        start = readme.index("## Minimum Database Privileges")
        end = readme.index("## Security Best Practices", start)
        surfaces = {"README.md": readme[start:end], "cli.py epilog": MINIMUM_PRIVILEGES}
        return {
            name: [
                line.strip()
                for line in text.splitlines()
                if line.strip().upper().startswith(("GRANT ", "CREATE USER ", "FLUSH "))
            ]
            for name, text in surfaces.items()
        }

    def test_no_surface_tells_a_user_to_grant_on_information_schema(self):
        for name, statements in self._documented_statements().items():
            offending = [s for s in statements if "ON information_schema.*" in s]
            self.assertEqual(
                offending,
                [],
                f"{name} grants on information_schema, which no account can do",
            )

    def test_the_documented_statements_are_not_empty(self):
        """Guard against the parser silently matching nothing after a reformat."""
        for name, statements in self._documented_statements().items():
            self.assertGreater(
                len(statements), 1, f"{name} yielded no GRANT statements to check"
            )


if __name__ == "__main__":
    unittest.main()
