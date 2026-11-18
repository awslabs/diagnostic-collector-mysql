# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for HTML report navigation.

The Health Overview turns each check name into a fragment link into the body of the
report. Three independent conditions have to hold for such a link to land somewhere
the reader can see, and each is asserted here against a generated document rather
than against the generator's source, because only the output shows whether they hold.

A subsection is reachable only if its div carries an `id`, so every emission path has
to write one. A fragment jump reaches an element only if that element has layout, and
all but the first section render inside a collapsed `<details>`, so the document needs
a handler that opens a target's ancestors. The navigation bar is `position: sticky`,
so a jump that lands has to reserve the bar's height or the heading sits underneath
it.

Covers:
- Every subsection div carries an id, so every subsection is addressable
- Every fragment link in the document resolves to an id in the same document
- Every id is unique, including where two subsection names slug identically
- A scroll offset is reserved for the sticky navigation bar
- A hashchange handler opens the details ancestors of a fragment target
"""

import inspect
import re
import unittest
from collections import Counter

from diagnostic_collector_mysql.parsing.input_validator import InputValidator
from diagnostic_collector_mysql.reporting.report_generator import (
    HTMLReportGenerator,
    ParsedLogFile,
)


def _log(query_id, columns, rows, success=True):
    """Build a ParsedLogFile the way the log parser would."""
    return ParsedLogFile(
        query_id=query_id,
        success=success,
        columns=columns,
        rows=rows,
        error=None,
        executed_timestamp="2026-10-01 12:00:00",
    )


def _shipped_report_format():
    """The report format the tool actually ships, validated the way startup does."""
    validator = InputValidator()
    query_errors, queries = validator.validate_queries_json()
    assert not query_errors, f"shipped queries.json does not validate: {query_errors}"
    format_errors, report_format = validator.validate_report_format_json(
        {q.id for q in queries}
    )
    assert not format_errors, (
        f"shipped report_format.json does not validate: {format_errors}"
    )
    return report_format


class TestNoSubsectionIsEmittedWithoutAnId(unittest.TestCase):
    """
    Assert at source level that no subsection is written without an anchor.

    The generator has roughly ninety subsection emission sites across both the
    no-data and the data-bearing render paths, and several of the bespoke ones index
    fixed column positions, so no single fixture reaches all of them. Reading the
    source covers every path including the ones a fixture cannot drive, and it catches
    a newly added site rather than only the ones that exist today.
    """

    def test_the_html_generator_emits_no_bare_subsection_div(self):
        source = inspect.getsource(HTMLReportGenerator)
        # The per-channel block documented below reuses the class without being a
        # named subsection, so it is excluded by the lookahead rather than by a count.
        bare = re.findall(
            r'<div class="subsection">(?!<strong>Channel: )', source
        )
        self.assertEqual(
            bare,
            [],
            f"{len(bare)} subsection divs are emitted without an id. A link into one "
            "cannot resolve. Emit the anchor with _emit_subsection_anchor_id().",
        )

    def test_the_per_channel_block_is_not_mistaken_for_a_subsection(self):
        """
        One site reuses the subsection class for a repeated per-channel block inside
        the replication section. It carries no subsection name and is not a link
        target, so it is deliberately left without an anchor. This records that, so
        the absence does not read as a missed site.
        """
        source = inspect.getsource(HTMLReportGenerator)
        self.assertIn(
            '<div class="subsection"><strong>Channel: ', source
        )

    def test_no_subsection_anchor_is_written_as_a_literal(self):
        """
        Every anchor goes through _emit_subsection_anchor_id, which is what registers
        it for collision detection. A hand-written literal id is emitted without
        registering, so the registry can later hand the same slug to a different
        subsection and produce a duplicate. "Configuration Warnings" is both a
        hardcoded anchor candidate in the 1.5 renderer and a subsection name in the
        shipped report format, so the two can collide on one document.
        """
        source = inspect.getsource(HTMLReportGenerator)
        literals = re.findall(
            r'<div class="subsection" id="([^{"][^"]*)"', source
        )
        self.assertEqual(
            literals,
            [],
            f"subsection anchors written as literals: {literals}. Emit them with "
            "_emit_subsection_anchor_id() so they register for collision detection.",
        )


class HTMLNavigationTestCase(unittest.TestCase):
    """
    Generates one HTML report from the shipped report format, once per class.

    The fixture supplies no query output, so every subsection renders through its
    no-data path. That is deliberate: it reaches every subsection the report format
    defines without having to satisfy each bespoke renderer's column expectations,
    and the anchor, link and script invariants under test do not depend on row data.
    The data-bearing paths are covered by the source-level assertion above.
    """

    @classmethod
    def setUpClass(cls):
        generator = HTMLReportGenerator(
            report_format=_shipped_report_format(),
            timestamp="2026-10-01 12:00:00",
        )
        cls.html = generator.generate({}, engine_type="mysql")
        cls.ids = re.findall(r'\bid="([^"]+)"', cls.html)
        cls.hrefs = re.findall(r'href="#([^"]+)"', cls.html)


class TestEverySubsectionIsAddressable(HTMLNavigationTestCase):
    """
    Assert no subsection reaches the document without an id.

    The generator has more than one subsection emission form, so an id missing from any
    one of them makes a subsection unaddressable depending on which render path the data
    takes.
    """

    def test_no_subsection_div_in_the_document_lacks_an_id(self):
        total = len(re.findall(r'<div class="subsection"', self.html))
        with_id = len(re.findall(r'<div class="subsection" id=', self.html))
        self.assertGreater(total, 0, "the fixture produced no subsections")
        self.assertEqual(
            total,
            with_id,
            f"{total - with_id} of {total} subsection divs carry no id, so a link "
            "into them cannot resolve",
        )


class TestEveryFragmentLinkResolves(HTMLNavigationTestCase):
    """
    Assert every in-document link has a target.

    A fragment link whose id is absent raises nothing and logs nothing: the browser
    simply does not move. The condition is invisible in the generator and invisible to
    a reader clicking the link, so it is asserted here, where it is cheap.
    """

    def test_no_link_points_at_a_missing_id(self):
        missing = sorted({h for h in self.hrefs if h not in set(self.ids)})
        self.assertEqual(
            missing,
            [],
            "these links point at ids that do not exist in the document: "
            + ", ".join("#" + m for m in missing),
        )

    def test_the_document_contains_fragment_links_at_all(self):
        """Guard against the test passing because nothing links anywhere."""
        self.assertGreater(len(self.hrefs), 0)


class TestAnchorIdsAreUnique(HTMLNavigationTestCase):
    """
    Assert no id is emitted twice.

    The slug collapses runs of non-alphanumeric characters, so two distinct subsection
    names can produce the same anchor: "Long-Running Transactions" in the InnoDB
    section and "Long Running Transactions" in Connection Management both yield
    `long-running-transactions`. Duplicate ids are invalid HTML and a fragment jump
    resolves to whichever appears first, which makes the target depend on section
    order.
    """

    def test_no_id_appears_more_than_once(self):
        duplicates = {k: v for k, v in Counter(self.ids).items() if v > 1}
        self.assertEqual(
            duplicates, {}, f"duplicate ids in the document: {duplicates}"
        )

    def test_a_colliding_subsection_name_keeps_the_first_slug_unsuffixed(self):
        """
        The first emission keeps the plain slug so an existing health check link still
        resolves. Only the later collision is suffixed.
        """
        generator = HTMLReportGenerator.__new__(HTMLReportGenerator)
        generator._emitted_anchor_ids = set()
        first = generator._emit_subsection_anchor_id("Long-Running Transactions")
        second = generator._emit_subsection_anchor_id("Long Running Transactions")
        third = generator._emit_subsection_anchor_id("Long  Running  Transactions")
        self.assertEqual(first, "long-running-transactions")
        self.assertEqual(second, "long-running-transactions-2")
        self.assertEqual(third, "long-running-transactions-3")

    def test_the_registry_resets_between_reports(self):
        """
        Anchors are unique per document, not per generator, so a second report must
        not inherit the first report's suffixes.
        """
        generator = HTMLReportGenerator(
            report_format=_shipped_report_format(),
            timestamp="2026-10-01 12:00:00",
        )
        first = generator.generate({}, engine_type="mysql")
        second = generator.generate({}, engine_type="mysql")
        self.assertEqual(
            re.findall(r'\bid="([^"]+)"', first),
            re.findall(r'\bid="([^"]+)"', second),
            "a second report produced different anchors from the first",
        )


class TestStickyNavigationOffset(HTMLNavigationTestCase):
    """
    Assert a scroll offset is reserved for the sticky navigation bar.

    `.nav` is `position: sticky; top: 10px; z-index: 100`. Without a scroll offset a
    fragment jump puts the target at viewport top, underneath the bar, so the reader
    cannot see what they navigated to.

    Each assertion strips CSS comments first and then requires the declaration with a
    value. The comment explaining why the declaration is there contains the property
    name, so an assertion on the name alone would hold with the declaration deleted.
    """

    @staticmethod
    def _without_comments(css):
        return re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)

    def _assert_reserves_space(self, rule_source, where):
        self.assertRegex(
            self._without_comments(rule_source),
            r'scroll-margin-top\s*:\s*\d+px',
            f"{where} declares no scroll-margin-top value",
        )

    def test_subsections_reserve_space_for_the_sticky_bar(self):
        subsection_rule = re.search(
            r'\.subsection \{(.*?)\}', self.html, re.DOTALL
        )
        self.assertIsNotNone(subsection_rule, "no .subsection CSS rule found")
        self._assert_reserves_space(subsection_rule.group(1), "the .subsection rule")

    def test_sections_reserve_space_for_the_sticky_bar(self):
        """Section-level links target the <details> elements, so those need it too."""
        details_rule = re.search(r'\n        details \{(.*?)\}', self.html, re.DOTALL)
        self.assertIsNotNone(details_rule, "no details CSS rule found")
        self._assert_reserves_space(details_rule.group(1), "the details rule")

    def test_the_offset_is_relaxed_where_the_bar_is_not_sticky(self):
        """
        The responsive rule drops the bar to `position: static` below 768px, so the
        reserved space would be a gap rather than a correction.
        """
        responsive = re.search(
            r'@media \(max-width: 768px\) \{(.*?)\n        \}', self.html, re.DOTALL
        )
        self.assertIsNotNone(responsive, "no responsive block found")
        self._assert_reserves_space(responsive.group(1), "the responsive block")


class TestCollapsedSectionsAreOpenedOnNavigation(HTMLNavigationTestCase):
    """
    Assert the document can reveal a fragment target inside a collapsed section.

    Only the first section renders open, so every link into a later section targets an
    element with no layout and the document needs a handler to open its ancestors.
    Section links in the navigation bar do not, because their ids sit on the
    `<details>` elements themselves, which have layout while collapsed.
    """

    def _script(self):
        scripts = re.findall(r'<script>(.*?)</script>', self.html, re.DOTALL)
        self.assertTrue(scripts, "the document carries no script block")
        return "\n".join(scripts)

    def test_a_hashchange_handler_is_registered(self):
        """
        Asserts the registration, not the word. The script's own comments mention
        hashchange, so a substring check passes with the listener deleted.
        """
        self.assertRegex(
            self._script(),
            r"addEventListener\(\s*'hashchange'\s*,\s*revealFragmentTarget\s*\)",
            "no hashchange listener is registered",
        )

    def test_the_handler_also_runs_for_the_initial_url(self):
        """hashchange does not fire for the fragment a document is opened at."""
        self.assertRegex(
            self._script(),
            r"addEventListener\(\s*'DOMContentLoaded'\s*,\s*revealFragmentTarget\s*\)",
            "no DOMContentLoaded listener is registered",
        )

    def test_the_handler_opens_details_ancestors(self):
        script = self._script()
        self.assertIn("DETAILS", script)
        self.assertIn("parentElement", script)
        self.assertRegex(script, r'\.open\s*=\s*true')

    def test_only_the_first_section_is_open_so_the_handler_is_load_bearing(self):
        """
        If this ever stops being true the handler becomes redundant rather than wrong,
        but the assertion records why it exists.
        """
        sections = re.findall(r'<details id="([^"]+)"( open)?>', self.html)
        collapsed = [name for name, is_open in sections if not is_open]
        self.assertGreater(
            len(collapsed),
            0,
            "no section renders collapsed, so this fixture cannot exercise the handler",
        )

    def test_the_rendered_script_is_not_left_with_doubled_braces(self):
        """
        The HTML head is built from an f-string, so every literal brace in the script
        is written doubled. A missed pair ships broken JavaScript that no Python test
        would otherwise catch.
        """
        script = self._script()
        self.assertNotIn("{{", script)
        self.assertNotIn("}}", script)
        self.assertEqual(
            script.count("{"),
            script.count("}"),
            "unbalanced braces in the rendered script",
        )


if __name__ == "__main__":
    unittest.main()
