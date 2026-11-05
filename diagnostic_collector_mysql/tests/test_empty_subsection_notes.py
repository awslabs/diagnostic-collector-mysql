"""
Unit tests for report behaviour when a subsection has no rows to show.

A subsection whose query ran and returned nothing is a different situation from one
whose query never ran, and `default_note` in report_format.json is the authored text
for the first case. Rendering nothing at all leaves a reader unable to tell "there
is nothing to report" from "this was not measured".

Also covers the two health queries that read performance_schema.global_status,
which is a status variable table and stays readable with the instrumentation flag
off, so gating them on performance_schema removes the check for no reason.
"""

import json
import unittest

from diagnostic_collector_mysql.data import get_data_path
from diagnostic_collector_mysql.models import ReportFormat, ReportSection, ReportSubsection
from diagnostic_collector_mysql.reporting.report_generator import (
    HTMLReportGenerator,
    JSONReportGenerator,
    MarkdownReportGenerator,
    ParsedLogFile,
    TextReportGenerator,
)

MTS_SOURCE = 'parallel_replication_worker_stats'
MTS_NOTE = 'Multi-threaded replication not configured or no worker threads active'


def _log(query_id, columns, rows, success=True, error=None):
    return ParsedLogFile(
        query_id=query_id,
        success=success,
        columns=columns,
        rows=rows,
        error=error,
        executed_timestamp='2026-09-02 12:00:00',
    )


def _subsection():
    return ReportSubsection(
        name='Parallel Replication (MTS) Details',
        source=MTS_SOURCE,
        columns=[],
        default_note=MTS_NOTE,
    )


def _report_format(subsection):
    return ReportFormat(sections=[
        ReportSection(
            number='2',
            name='Replication Topology & Status',
            subsections=[subsection],
        )
    ])


class TestHealthQueryGating(unittest.TestCase):
    """These two read performance_schema.global_status, which is a status variable
    table and stays readable with the instrumentation flag off."""

    @staticmethod
    def _queries_by_id():
        with open(get_data_path('queries.json'), encoding='utf-8') as handle:
            data = json.load(handle)
        by_id = {}
        for query in data['queries']:
            by_id.setdefault(query['id'], []).append(query)
        return by_id

    def test_connection_usage_is_not_gated_on_performance_schema(self):
        for query in self._queries_by_id()['health_connection_usage']:
            self.assertIsNone(
                query.get('requires'),
                'health_connection_usage reads a status variable table, so gating it '
                'on performance_schema removes the check for no reason',
            )

    def test_redo_log_occupancy_is_not_gated_on_performance_schema(self):
        for query in self._queries_by_id()['health_redo_log_occupancy']:
            self.assertIsNone(query.get('requires'))

    def test_no_health_check_query_is_gated_on_performance_schema(self):
        gated = [
            query['id']
            for queries in self._queries_by_id().values()
            for query in queries
            if query.get('section') == 'health-checks'
            and (query.get('requires') or {}).get('variable') == 'performance_schema'
        ]
        self.assertEqual(gated, [], f'Health checks gated on performance_schema: {gated}')


class MTSNoDataTestCase(unittest.TestCase):
    """Workers configured, but the per-worker join returned nothing."""

    def setUp(self):
        self.subsection = _subsection()
        self.report_format = _report_format(self.subsection)
        # The subsection only renders on a replica with more than one worker
        # configured, so the fixture has to establish both before the no-data path
        # is even reachable.
        self.logs = {
            'replication_role': _log(
                'replication_role', ['replication_role'], [('REPLICA',)]
            ),
            'replication_config': _log(
                'replication_config',
                ['replica_parallel_workers', 'replica_parallel_type'],
                [(16, 'LOGICAL_CLOCK')],
            ),
            MTS_SOURCE: _log(MTS_SOURCE, [], []),
        }

    def _markdown(self):
        return MarkdownReportGenerator(report_format=self.report_format)

    def _text(self):
        return TextReportGenerator(report_format=self.report_format)

    def _json(self):
        return JSONReportGenerator(report_format=self.report_format)


class TestMarkdownEmitsTheNote(MTSNoDataTestCase):
    def test_note_is_rendered(self):
        out = self._markdown()._generate_parallel_replication_subsection(
            self.subsection, self.logs
        )
        # The renderer escapes markdown, so the hyphen in "Multi-threaded" arrives
        # backslashed. Assert on a fragment escaping does not touch.
        self.assertIn('no worker threads active', out)
        self.assertIn('Parallel Replication', out)

    def test_query_failure_is_rendered(self):
        self.logs[MTS_SOURCE] = _log(
            MTS_SOURCE, None, None, success=False, error='Access denied'
        )
        out = self._markdown()._generate_parallel_replication_subsection(
            self.subsection, self.logs
        )
        self.assertIn('Access denied', out)


class TestTextEmitsTheNote(MTSNoDataTestCase):
    def test_note_is_rendered(self):
        out = self._text()._generate_parallel_replication_subsection(
            self.subsection, self.logs
        )
        self.assertIn(MTS_NOTE, out)


class TestJsonStillEmitsTheNote(MTSNoDataTestCase):
    """JSON already behaved correctly; this pins it so the four stay in step."""

    def test_note_is_present(self):
        result = self._json()._generate_parallel_replication_subsection(
            self.subsection, self.logs
        )
        self.assertEqual(result.get('default_note'), MTS_NOTE)


class TestHtmlEmitsTheNote(MTSNoDataTestCase):
    """The HTML path is a section-level renderer with a different signature from the
    other three, which is why it needs its own coverage rather than being assumed
    to behave like them."""

    def test_note_is_rendered(self):
        out = HTMLReportGenerator(
            report_format=self.report_format
        )._generate_parallel_replication_section(self.logs)
        self.assertIn(MTS_NOTE, out)
        self.assertIn('Parallel Replication (MTS) Details', out)

    def test_note_is_resolved_from_report_format(self):
        """The renderer has no subsection in scope, so it looks the text up by
        source id. A report_format without the note falls back rather than raising."""
        generator = HTMLReportGenerator(report_format=self.report_format)
        self.assertEqual(
            generator._default_note_for_source(MTS_SOURCE, 'fallback'), MTS_NOTE
        )
        self.assertEqual(
            generator._default_note_for_source('no_such_query', 'fallback'), 'fallback'
        )

    def test_query_failure_is_rendered(self):
        self.logs[MTS_SOURCE] = _log(
            MTS_SOURCE, None, None, success=False, error='Access denied'
        )
        out = HTMLReportGenerator(
            report_format=self.report_format
        )._generate_parallel_replication_section(self.logs)
        self.assertIn('Access denied', out)


class TestAllFormatsAgree(MTSNoDataTestCase):
    def test_no_format_stays_silent(self):
        """All four formats read the same authored note, so silence in any one of
        them is a divergence between formats rather than a property of the data."""
        markdown = self._markdown()._generate_parallel_replication_subsection(
            self.subsection, self.logs
        )
        text = self._text()._generate_parallel_replication_subsection(
            self.subsection, self.logs
        )
        json_result = self._json()._generate_parallel_replication_subsection(
            self.subsection, self.logs
        )
        html = HTMLReportGenerator(
            report_format=self.report_format
        )._generate_parallel_replication_section(self.logs)
        self.assertNotEqual(markdown.strip(), '')
        self.assertNotEqual(text.strip(), '')
        self.assertNotEqual(html.strip(), '')
        self.assertTrue(json_result.get('default_note'))


class TestSubsectionsCarryANote(unittest.TestCase):
    def test_mts_subsection_has_a_default_note_to_emit(self):
        """The renderers above depend on this text existing in report_format.json."""
        with open(get_data_path('report_format.json'), encoding='utf-8') as handle:
            data = json.load(handle)
        notes = [
            sub.get('default_note')
            for section in data['sections']
            for sub in section.get('subsections', [])
            if sub.get('source') == MTS_SOURCE
        ]
        self.assertTrue(notes, 'No MTS subsection found in report_format.json')
        self.assertTrue(all(notes), 'MTS subsection is missing its default_note')


class TestJsonHealthKeyIsDocumented(unittest.TestCase):
    """The JSON field names are a public interface."""

    def test_readme_documents_the_health_check_fields(self):
        import pathlib

        readme = pathlib.Path(__file__).resolve().parents[2] / 'README.md'
        if not readme.exists():                      # pragma: no cover
            self.skipTest('README.md not present in this layout')
        text = readme.read_text(encoding='utf-8')
        self.assertIn('health_overview', text)
        self.assertIn('"check"', text)
        self.assertIn('"status"', text)


if __name__ == '__main__':
    unittest.main()
