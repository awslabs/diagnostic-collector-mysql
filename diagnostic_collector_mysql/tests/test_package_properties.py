"""
Property-based tests for the structural correctness of the package layout.

Verifies four correctness properties using Hypothesis:
  Property 1: All public names are importable from the top-level package
  Property 2: No relative imports exist in any package source file
  Property 3: All sub-packages are importable without error
  Property 4: InputValidator always resolves data file paths into the data/ sub-package
"""

import ast
import importlib
import os
import pathlib

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

import diagnostic_collector_mysql


# ---------------------------------------------------------------------------
# Property 1: All public names are importable from the top-level package
# ---------------------------------------------------------------------------

@given(name=st.sampled_from(diagnostic_collector_mysql.__all__))
@settings(max_examples=len(diagnostic_collector_mysql.__all__))
def test_all_public_names_importable(name):
    """Property 1: Every name in diagnostic_collector_mysql.__all__ is accessible via getattr."""
    obj = getattr(diagnostic_collector_mysql, name)
    assert obj is not None


# ---------------------------------------------------------------------------
# Property 2: No relative imports exist in any package source file
# ---------------------------------------------------------------------------

_package_root = pathlib.Path(diagnostic_collector_mysql.__file__).parent
_py_files = sorted(_package_root.rglob("*.py"))


@given(path=st.sampled_from(_py_files))
@settings(deadline=None)
def test_no_relative_imports(path):
    """Property 2: No .py file in the package tree contains a relative import."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"Relative import found in {path}: "
                f"from {'.' * node.level}{node.module or ''} import ..."
            )


# ---------------------------------------------------------------------------
# Property 3: All sub-packages are importable without error
# ---------------------------------------------------------------------------

SUBPACKAGES = ["core", "parsing", "reporting", "output", "utils", "data"]


@given(name=st.sampled_from(SUBPACKAGES))
def test_all_subpackages_importable(name):
    """Property 3: Every sub-package can be imported without raising an exception."""
    mod = importlib.import_module(f"diagnostic_collector_mysql.{name}")
    assert mod is not None


# ---------------------------------------------------------------------------
# Property 4: InputValidator always resolves data file paths into the data/ sub-package
# ---------------------------------------------------------------------------

@given(script_dir=st.one_of(st.just(""), st.just("/tmp"), st.just("/nonexistent")))
def test_input_validator_data_paths(script_dir):
    """Property 4: InputValidator always resolves paths into diagnostic_collector_mysql/data/."""
    from diagnostic_collector_mysql.parsing.input_validator import InputValidator

    validator = InputValidator(script_dir)
    for path in (validator.queries_path, validator.report_format_path):
        assert os.path.isabs(path), f"Path is not absolute: {path}"
        assert "diagnostic_collector_mysql" in path and "data" in path, (
            f"Path does not point into diagnostic_collector_mysql/data/: {path}"
        )
        assert os.path.exists(path), f"Data file does not exist: {path}"
