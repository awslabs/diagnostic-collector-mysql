"""Keep the maintainer credit in README.md and MAINTAINERS.md consistent.

The public repository starts from a fresh history, so these two files are the
only in-repo attribution. A maintainer added to one and not the other would
leave the project naming different people in different places.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _maintainers_from_file():
    text = (REPO_ROOT / "MAINTAINERS.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*Maintainer\s*\|\s*$", text, re.MULTILINE)
    return rows


def test_maintainers_file_lists_at_least_one_maintainer():
    assert _maintainers_from_file(), "MAINTAINERS.md lists no maintainers"


def test_readme_credits_every_maintainer_and_links_the_file():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## Maintainers")
    end = readme.index("\n## ", start + 1)
    section = readme[start:end]

    assert "(MAINTAINERS.md)" in section
    for name in _maintainers_from_file():
        assert name in section, f"README Maintainers section does not credit {name}"
