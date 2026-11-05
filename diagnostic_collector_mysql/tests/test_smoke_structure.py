"""
Smoke tests for the package layout.

Verifies that every expected file exists at its sub-package path.

"""

import os
import pathlib
import pytest


# Project root is two levels up from this file (tests/ -> diagnostic_collector_mysql/ -> project root)
PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent
PACKAGE_ROOT = PROJECT_ROOT / "diagnostic_collector_mysql"


class TestSubPackageStructure:
    """Verify all sub-packages exist with __init__.py files."""

    def test_data_subpackage_exists(self):
        assert (PACKAGE_ROOT / "data" / "__init__.py").exists()

    def test_core_subpackage_exists(self):
        assert (PACKAGE_ROOT / "core" / "__init__.py").exists()

    def test_parsing_subpackage_exists(self):
        assert (PACKAGE_ROOT / "parsing" / "__init__.py").exists()

    def test_reporting_subpackage_exists(self):
        assert (PACKAGE_ROOT / "reporting" / "__init__.py").exists()

    def test_output_subpackage_exists(self):
        assert (PACKAGE_ROOT / "output" / "__init__.py").exists()

    def test_utils_subpackage_exists(self):
        assert (PACKAGE_ROOT / "utils" / "__init__.py").exists()


class TestSourceModuleLocations:
    """Verify all source modules are at their sub-package paths."""

    def test_connection_manager_in_core(self):
        assert (PACKAGE_ROOT / "core" / "connection_manager.py").exists()

    def test_query_executor_in_core(self):
        assert (PACKAGE_ROOT / "core" / "query_executor.py").exists()

    def test_execution_logger_in_core(self):
        assert (PACKAGE_ROOT / "core" / "execution_logger.py").exists()

    def test_innodb_status_parser_in_parsing(self):
        assert (PACKAGE_ROOT / "parsing" / "innodb_status_parser.py").exists()

    def test_input_validator_in_parsing(self):
        assert (PACKAGE_ROOT / "parsing" / "input_validator.py").exists()

    def test_report_generator_in_reporting(self):
        assert (PACKAGE_ROOT / "reporting" / "report_generator.py").exists()

    def test_output_manager_in_output(self):
        assert (PACKAGE_ROOT / "output" / "output_manager.py").exists()

    def test_validate_format_sync_in_utils(self):
        assert (PACKAGE_ROOT / "utils" / "validate_format_sync.py").exists()

    def test_models_at_package_root(self):
        assert (PACKAGE_ROOT / "models.py").exists()

    def test_cli_at_package_root(self):
        assert (PACKAGE_ROOT / "cli.py").exists()

    def test_main_at_package_root(self):
        assert (PACKAGE_ROOT / "__main__.py").exists()

    def test_init_at_package_root(self):
        assert (PACKAGE_ROOT / "__init__.py").exists()


class TestDataFiles:
    """Verify data files are in the data/ sub-package."""

    def test_queries_json_in_data(self):
        assert (PACKAGE_ROOT / "data" / "queries.json").exists()

    def test_report_format_json_in_data(self):
        assert (PACKAGE_ROOT / "data" / "report_format.json").exists()


class TestScriptsDirectory:
    """Verify scripts/ directory structure."""

    def test_validate_formats_sh_exists(self):
        assert (PACKAGE_ROOT / "scripts" / "validate_formats.sh").exists()

    def test_scripts_has_no_init_py(self):
        """scripts/ is not a Python package — no __init__.py."""
        assert not (PACKAGE_ROOT / "scripts" / "__init__.py").exists()



class TestProjectRootFiles:
    """Verify project root files exist."""

    def test_pyproject_toml_exists(self):
        assert (PROJECT_ROOT / "pyproject.toml").exists()

    def test_uv_lock_exists(self):
        assert (PROJECT_ROOT / "uv.lock").exists()

    def test_pyproject_toml_has_scripts_entry(self):
        content = (PROJECT_ROOT / "pyproject.toml").read_text()
        assert 'diagnostic-collector-mysql = "diagnostic_collector_mysql.__main__:main"' in content

    def test_pyproject_toml_has_ruff_tid252(self):
        content = (PROJECT_ROOT / "pyproject.toml").read_text()
        assert "TID252" in content
