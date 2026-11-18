# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Require the MIT-0 short license header on every shipped source file.

The header is the Amazon MIT-0 short form: a copyright line and an SPDX
identifier, written in the file's own comment style. It sits on the first two
lines, or directly after a shebang line when the file has one.

Files are discovered from the package directory, so a new module or test is
checked the moment it is added, with no list to update.
"""

from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1]

HEADER = (
    "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
    "# SPDX-License-Identifier: MIT-0",
)

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".hypothesis", ".ruff_cache"}


def _source_files():
    files = []
    for pattern in ("*.py", "*.sh"):
        for path in PACKAGE_DIR.rglob(pattern):
            if SKIP_DIRS.intersection(path.relative_to(PACKAGE_DIR).parts):
                continue
            files.append(path)
    return sorted(files)


def _header_lines(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0
    return tuple(lines[start : start + len(HEADER)])


def test_source_files_are_discovered():
    files = _source_files()
    assert any(p.suffix == ".py" for p in files), "no .py files found"
    assert any(p.suffix == ".sh" for p in files), "no .sh files found"


@pytest.mark.parametrize(
    "path",
    _source_files(),
    ids=lambda p: str(p.relative_to(PACKAGE_DIR)),
)
def test_file_has_mit0_header(path):
    assert _header_lines(path) == HEADER, (
        f"{path.relative_to(PACKAGE_DIR)} does not start with the MIT-0 header "
        "(after the shebang line, if there is one)"
    )
