# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep the README Health Overview table in step with the evaluator.

The table is the public description of what the Health Overview checks and at
which severity, so a check missing from it, or listed in the wrong severity
column, misleads every reader who relies on the README.

These tests read the check names and severities straight from the source of
_evaluate_health_checks_common, so a check added, renamed or re-graded in code
fails here until the README is updated to match.
"""

import inspect
import re
from pathlib import Path

from diagnostic_collector_mysql.reporting import report_generator

REPO_ROOT = Path(__file__).resolve().parents[2]

# Names a check prints in one status only. The README lists each check once,
# under the name in the key, and explains the alternate name in prose.
ALTERNATE_NAMES = {
    'Auto-Increment Usage': 'Auto-Increment >90% Full',
    'Deadlocks (since restart)': 'Deadlocks',
}

APPEND_RE = re.compile(r"health_checks\.append\(\('(critical|warning|ok)', '([^']+)'")


def _emitted_checks():
    """Map each README check name to the set of statuses the evaluator can emit."""
    source = inspect.getsource(report_generator._evaluate_health_checks_common)
    checks = {}
    for status, name in APPEND_RE.findall(source):
        checks.setdefault(ALTERNATE_NAMES.get(name, name), set()).add(status)
    return checks


def _readme_table():
    """Map each check name in the README table to its (warning, critical) cells."""
    readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')
    start = readme.index('## Health Overview')
    header = readme.index('| Check | Warning Threshold | Critical Threshold |', start)
    rows = {}
    for line in readme[header:].splitlines()[2:]:
        if not line.startswith('|'):
            break
        cells = [cell.strip() for cell in line.strip('|').split('|')]
        rows[cells[0]] = (cells[1], cells[2])
    return rows


def test_evaluator_source_yields_checks():
    assert len(_emitted_checks()) > 20, 'regex no longer matches the evaluator source'


def test_readme_lists_every_check_the_evaluator_emits():
    missing = sorted(set(_emitted_checks()) - set(_readme_table()))
    assert not missing, f'README Health Overview table is missing: {missing}'


def test_readme_lists_no_check_the_evaluator_does_not_emit():
    extra = sorted(set(_readme_table()) - set(_emitted_checks()))
    assert not extra, f'README Health Overview table lists unknown checks: {extra}'


def test_readme_severity_columns_match_the_evaluator():
    table = _readme_table()
    for name, statuses in _emitted_checks().items():
        warning, critical = table[name]
        assert ('warning' in statuses) == (warning not in ('-', '') and 'always OK' not in warning), (
            f'{name}: README warning column {warning!r} does not match emitted statuses {sorted(statuses)}'
        )
        assert ('critical' in statuses) == (critical not in ('-', '')), (
            f'{name}: README critical column {critical!r} does not match emitted statuses {sorted(statuses)}'
        )
