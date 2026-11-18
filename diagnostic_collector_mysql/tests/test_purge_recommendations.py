# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the Purge Optimization Recommendations box in the InnoDB Purge
Analysis subsection.

Every renderer (HTML, Markdown, Text, JSON) carries its own copy of the box, so
each one is checked for the same two items:

- Aurora-Specific: a long-running query on an Aurora Replica blocks purge, and
  aurora_read_replica_read_committed with READ COMMITTED reduces that for
  reporting queries that accept less strict consistency.
- Write Concentration: purge work is split across purge threads by table, which
  matters only when no transaction or read view holds purge back. It must not
  read as advice to add purge threads, because the Purge Health Status in the
  same subsection points at long-running transactions instead.
"""

import re
import unittest

from diagnostic_collector_mysql.models import ReportFormat, ReportSection, ReportSubsection
from diagnostic_collector_mysql.reporting.report_generator import (
    HTMLReportGenerator,
    JSONReportGenerator,
    MarkdownReportGenerator,
    ParsedLogFile,
    TextReportGenerator,
)

GARBLED_PHRASES = (
    'scales to Serverless v2',
    'Serverless v2 scales on replicas',
    'Table Sharding',
    'increase number of purge threads',
)

REQUIRED_PHRASES = (
    'Aurora-Specific',
    'long-running query on an Aurora Replica also blocks purge',
    'history list length',
    'aurora_read_replica_read_committed=ON',
    'READ COMMITTED isolation',
    'less strict consistency',
    'Write Concentration',
    'purge work to purge threads by table',
    'heavy DML on a single table is purged by one thread',
    'no long-running transaction or read view holds it back',
)


def _subsection():
    return ReportSubsection(
        name='InnoDB Purge Analysis',
        source='innodb_purge_analysis',
        columns=[],
    )


def _report_format(subsection):
    return ReportFormat(sections=[
        ReportSection(
            number='6',
            name='InnoDB Engine & Redo Log',
            subsections=[subsection],
        )
    ])


def _logs():
    return {
        'innodb_history_length': ParsedLogFile(
            query_id='innodb_history_length',
            success=True,
            columns=['history_list_length'],
            rows=[(150000,)],
            error=None,
            executed_timestamp='2026-11-06 12:00:00',
        ),
    }


def _normalize(text):
    """Collapse whitespace so wrapped Text-report lines compare as one string."""
    return re.sub(r'\s+', ' ', text)


class PurgeRecommendationsTestCase(unittest.TestCase):
    def setUp(self):
        self.subsection = _subsection()
        self.report_format = _report_format(self.subsection)
        self.logs = _logs()

    def _render(self, generator_cls):
        generator = generator_cls(report_format=self.report_format)
        out = generator._generate_innodb_purge_analysis_subsection(self.subsection, self.logs)
        if isinstance(out, dict):
            return out
        if isinstance(out, list):
            return '\n'.join(out)
        return out

    def _assert_wording(self, text):
        flat = _normalize(text)
        for phrase in GARBLED_PHRASES:
            self.assertNotIn(phrase, flat)
        for phrase in REQUIRED_PHRASES:
            self.assertIn(phrase, flat)


class TestPurgeRecommendationsWording(PurgeRecommendationsTestCase):
    def test_html(self):
        html = self._render(HTMLReportGenerator)
        self._assert_wording(html)
        self.assertIn('<strong>Aurora-Specific:</strong>', html)
        self.assertIn('<strong>Write Concentration:</strong>', html)

    def test_markdown(self):
        md = self._render(MarkdownReportGenerator)
        self._assert_wording(md)
        self.assertIn('**Aurora-Specific:**', md)
        self.assertIn('**Write Concentration:**', md)

    def test_text(self):
        text = self._render(TextReportGenerator)
        self._assert_wording(text)
        self.assertIn('* Aurora-Specific:', text)
        self.assertIn('* Write Concentration:', text)

    def test_json(self):
        data = self._render(JSONReportGenerator)
        recommendations = data['recommendations']
        self._assert_wording(' '.join(recommendations))
        aurora = [r for r in recommendations if r.startswith('Aurora-Specific:')]
        write = [r for r in recommendations if r.startswith('Write Concentration:')]
        self.assertEqual(len(aurora), 1)
        self.assertEqual(len(write), 1)

    def test_all_formats_carry_the_same_items(self):
        """The four renderers must not drift apart."""
        json_items = self._render(JSONReportGenerator)['recommendations']
        renderings = {
            'html': _normalize(self._render(HTMLReportGenerator)),
            'markdown': _normalize(self._render(MarkdownReportGenerator)),
            'text': _normalize(self._render(TextReportGenerator)),
        }
        for item in json_items:
            label, body = item.split(': ', 1)
            for fmt, rendered in renderings.items():
                with self.subTest(format=fmt, item=label):
                    self.assertIn(label, rendered)
                    self.assertIn(_normalize(body), rendered)


if __name__ == '__main__':
    unittest.main()
