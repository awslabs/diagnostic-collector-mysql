# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for InputValidator - queries.json validation.

Tests validate:
- Query definition parsing
- queries.json validation
- Display ALL validation errors found
"""

import json
import os
import tempfile
import unittest

from diagnostic_collector_mysql.parsing.input_validator import InputValidator
from diagnostic_collector_mysql.models import QueryDefinition, ValidationError


class TestQueriesJsonValidation(unittest.TestCase):
    """Tests for queries.json validation."""
    
    def setUp(self):
        """Create a temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.validator = InputValidator()
        # Override paths to point to temp dir for test isolation
        self.validator.queries_path = os.path.join(self.temp_dir, 'queries.json')
        self.validator.report_format_path = os.path.join(self.temp_dir, 'report_format.json')
    
    def tearDown(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _write_queries_json(self, content):
        """Helper to write queries.json content."""
        queries_path = os.path.join(self.temp_dir, 'queries.json')
        with open(queries_path, 'w', encoding='utf-8') as f:
            if isinstance(content, str):
                f.write(content)
            else:
                json.dump(content, f)
    
    # =========================================================================
    # Verify queries.json exists and contains valid JSON
    # =========================================================================
    
    def test_missing_queries_json_file(self):
        """Test error when queries.json file does not exist."""
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].file, 'queries.json')
        self.assertEqual(errors[0].location, 'file')
        self.assertIn('not found', errors[0].message.lower())
        self.assertEqual(len(queries), 0)
    
    def test_invalid_json_syntax(self):
        """Test error when queries.json contains invalid JSON."""
        self._write_queries_json('{ invalid json }')
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].file, 'queries.json')
        self.assertIn('Invalid JSON', errors[0].message)
        self.assertEqual(len(queries), 0)
    
    def test_valid_json_structure(self):
        """Test successful parsing of valid queries.json."""
        self._write_queries_json({
            "queries": [
                {
                    "id": "TEST_QUERY",
                    "section": "status",
                    "command": "SELECT 1"
                }
            ]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].id, "TEST_QUERY")
    
    def test_missing_queries_array(self):
        """Test error when 'queries' array is missing."""
        self._write_queries_json({"other_key": []})
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("'queries' array", errors[0].message)
    
    def test_queries_not_array(self):
        """Test error when 'queries' is not an array."""
        self._write_queries_json({"queries": "not an array"})
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an array", errors[0].message)
    
    # =========================================================================
    # Verify each query object has required fields
    # =========================================================================
    
    def test_missing_required_field_id(self):
        """Test error when 'id' field is missing."""
        self._write_queries_json({
            "queries": [{
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("id", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_missing_required_field_section(self):
        """Test error when 'section' field is missing."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("section", errors[0].location)
    
    def test_missing_required_field_command(self):
        """Test error when 'command' field is missing."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("command", errors[0].location)
    
    def test_missing_multiple_required_fields(self):
        """Test that all missing fields are reported."""
        self._write_queries_json({
            "queries": [{}]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        # Should report all 3 missing fields (id, section, command; engine is optional)
        self.assertEqual(len(errors), 3)
        error_fields = {e.location.split('.')[-1] for e in errors}
        self.assertEqual(error_fields, {'id', 'section', 'command'})
    
    # =========================================================================
    # Verify query IDs are unique
    # =========================================================================
    
    def test_duplicate_query_ids(self):
        """Test error when query IDs are duplicated."""
        self._write_queries_json({
            "queries": [
                {"id": "DUPLICATE_ID", "section": "status", "command": "SELECT 1"},
                {"id": "DUPLICATE_ID", "section": "status", "command": "SELECT 2"}
            ]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("Duplicate query ID", errors[0].message)
        self.assertIn("DUPLICATE_ID", errors[0].message)
    
    def test_multiple_duplicate_ids(self):
        """Test that all duplicate IDs are reported."""
        self._write_queries_json({
            "queries": [
                {"id": "ID_A", "section": "status", "command": "SELECT 1"},
                {"id": "ID_A", "section": "status", "command": "SELECT 2"},
                {"id": "ID_B", "section": "status", "command": "SELECT 3"},
                {"id": "ID_B", "section": "status", "command": "SELECT 4"},
                {"id": "ID_B", "section": "status", "command": "SELECT 5"}
            ]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        # Should report 3 duplicates (1 for ID_A, 2 for ID_B)
        self.assertEqual(len(errors), 3)
        duplicate_errors = [e for e in errors if "Duplicate" in e.message]
        self.assertEqual(len(duplicate_errors), 3)
    
    # =========================================================================
    # Display ALL validation errors found
    # =========================================================================
    
    def test_multiple_errors_all_reported(self):
        """Test that multiple different errors are all reported."""
        self._write_queries_json({
            "queries": [
                {"id": "VALID", "section": "status", "command": "SELECT 1"},
                {"id": "VALID", "section": "status", "command": "SELECT 2"},
                {"section": "status", "command": "SELECT 3"},
                {"id": "", "section": "", "command": "SELECT 4"}
            ]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        # Should have multiple errors: duplicate ID, missing id, empty id, empty section
        self.assertGreater(len(errors), 3)
    
    # =========================================================================
    # Field type validation
    # =========================================================================
    
    def test_invalid_id_type(self):
        """Test error when 'id' is not a string."""
        self._write_queries_json({
            "queries": [{
                "id": 123,
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be a string", errors[0].message)
    
    def test_empty_id(self):
        """Test error when 'id' is empty string."""
        self._write_queries_json({
            "queries": [{
                "id": "",
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be empty", errors[0].message)
    
    def test_invalid_condition_engine_type(self):
        """Test error when 'condition.engine' has invalid value."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": ["postgres"]}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("Invalid engine type", errors[0].message)
    
    def test_valid_condition_engine_types(self):
        """Test all valid engine types are accepted in condition."""
        valid_engines = ['mysql', 'aurora-mysql', 'mariadb']
        
        for engine in valid_engines:
            self._write_queries_json({
                "queries": [{
                    "id": f"TEST_{engine}",
                    "section": "status",
                    "command": "SELECT 1",
                    "condition": {"engine": [engine]}
                }]
            })
            
            errors, queries = self.validator.validate_queries_json()
            
            self.assertEqual(len(errors), 0, f"Engine '{engine}' should be valid")
            self.assertEqual(len(queries), 1)
            self.assertEqual(queries[0].condition, {"engine": [engine]})
    
    def test_no_condition_runs_on_all_engines(self):
        """Test that queries without condition run on all engines."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertIsNone(queries[0].condition)
    
    # =========================================================================
    # Extract query fields into QueryDefinition
    # =========================================================================
    
    def test_query_definition_parsing(self):
        """Test that valid queries are parsed into QueryDefinition objects."""
        self._write_queries_json({
            "queries": [
                {
                    "id": "GLOBAL_STATUS",
                    "section": "status",
                    "command": "SHOW GLOBAL STATUS"
                },
                {
                    "id": "REPLICATION_STATUS",
                    "section": "replication",
                    "command": "SHOW REPLICA STATUS",
                    "condition": {"engine": ["mysql"]}
                }
            ]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 2)
        
        # Verify first query (no condition = runs on all engines)
        self.assertIsInstance(queries[0], QueryDefinition)
        self.assertEqual(queries[0].id, "GLOBAL_STATUS")
        self.assertIsNone(queries[0].condition)
        self.assertEqual(queries[0].section, "status")
        self.assertEqual(queries[0].command, "SHOW GLOBAL STATUS")
        
        # Verify second query (with condition)
        self.assertEqual(queries[1].id, "REPLICATION_STATUS")
        self.assertEqual(queries[1].condition, {"engine": ["mysql"]})
        self.assertEqual(queries[1].section, "replication")
    
    def test_load_queries_alias(self):
        """Test that load_queries() works as alias for validate_queries_json()."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.load_queries()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
    
    def test_get_query_ids(self):
        """Test get_query_ids() returns set of all query IDs."""
        self._write_queries_json({
            "queries": [
                {"id": "QUERY_A", "section": "status", "command": "SELECT 1"},
                {"id": "QUERY_B", "section": "status", "command": "SELECT 2"},
                {"id": "QUERY_C", "section": "status", "command": "SELECT 3"}
            ]
        })
        
        errors, query_ids = self.validator.get_query_ids()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(query_ids, {"QUERY_A", "QUERY_B", "QUERY_C"})


if __name__ == '__main__':
    unittest.main()


class TestReportFormatJsonValidation(unittest.TestCase):
    """
    Tests for report_format.json validation.
    
    Tests validate:
    - Report format definition file
    - report_format.json validation
    - Display ALL validation errors found
    """
    
    def setUp(self):
        """Create a temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.validator = InputValidator()
        # Override paths to point to temp dir for test isolation
        self.validator.queries_path = os.path.join(self.temp_dir, 'queries.json')
        self.validator.report_format_path = os.path.join(self.temp_dir, 'report_format.json')
        # Default set of valid query IDs for cross-reference validation
        self.valid_query_ids = {'QUERY_A', 'QUERY_B', 'QUERY_C', 'SERVER_INFO', 'GLOBAL_STATUS'}
    
    def tearDown(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _write_report_format_json(self, content):
        """Helper to write report_format.json content."""
        report_path = os.path.join(self.temp_dir, 'report_format.json')
        with open(report_path, 'w', encoding='utf-8') as f:
            if isinstance(content, str):
                f.write(content)
            else:
                json.dump(content, f)
    
    # =========================================================================
    # Verify report_format.json exists and contains valid JSON
    # =========================================================================
    
    def test_missing_report_format_json_file(self):
        """Test error when report_format.json file does not exist."""
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].file, 'report_format.json')
        self.assertEqual(errors[0].location, 'file')
        self.assertIn('not found', errors[0].message.lower())
        self.assertIsNone(report_format)
    
    def test_invalid_json_syntax(self):
        """Test error when report_format.json contains invalid JSON."""
        self._write_report_format_json('{ invalid json }')
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].file, 'report_format.json')
        self.assertIn('Invalid JSON', errors[0].message)
        self.assertIsNone(report_format)
    
    def test_valid_json_structure(self):
        """Test successful parsing of valid report_format.json."""
        self._write_report_format_json({
            "sections": [
                {
                    "name": "Server Information",
                    "subsections": [
                        {
                            "name": "Server Details",
                            "source": "SERVER_INFO",
                            "columns": ["Variable_name", "Value"]
                        }
                    ]
                }
            ]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
        self.assertEqual(len(report_format.sections), 1)
        self.assertEqual(report_format.sections[0].name, "Server Information")
    
    def test_missing_sections_array(self):
        """Test error when 'sections' array is missing."""
        self._write_report_format_json({"other_key": []})
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("'sections' array", errors[0].message)
    
    def test_sections_not_array(self):
        """Test error when 'sections' is not an array."""
        self._write_report_format_json({"sections": "not an array"})
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an array", errors[0].message)
    
    def test_root_not_object(self):
        """Test error when root is not a JSON object."""
        self._write_report_format_json([])
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("Expected JSON object", errors[0].message)
    
    # =========================================================================
    # Section structure validation (name and subsections)
    # =========================================================================
    
    def test_section_missing_name(self):
        """Test error when section is missing 'name' field."""
        self._write_report_format_json({
            "sections": [{
                "subsections": []
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("name", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_section_missing_subsections(self):
        """Test error when section is missing 'subsections' field."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section"
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("subsections", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_section_name_not_string(self):
        """Test error when section 'name' is not a string."""
        self._write_report_format_json({
            "sections": [{
                "name": 123,
                "subsections": []
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be a string", errors[0].message)
    
    def test_section_name_empty(self):
        """Test error when section 'name' is empty."""
        self._write_report_format_json({
            "sections": [{
                "name": "",
                "subsections": []
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be empty", errors[0].message)
    
    def test_section_not_object(self):
        """Test error when section is not an object."""
        self._write_report_format_json({
            "sections": ["not an object"]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an object", errors[0].message)
    
    def test_subsections_not_array(self):
        """Test error when 'subsections' is not an array."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": "not an array"
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an array", errors[0].message)
    
    # =========================================================================
    # Subsection structure validation (name, source, columns, condition)
    # =========================================================================
    
    def test_subsection_missing_name(self):
        """Test error when subsection is missing 'name' field."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "source": "QUERY_A",
                    "columns": ["col1"]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("name", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_subsection_missing_source(self):
        """Test error when subsection is missing 'source' field."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "columns": ["col1"]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("source", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_subsection_missing_columns(self):
        """Test error when subsection is missing 'columns' field."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A"
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("columns", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_subsection_missing_all_required_fields(self):
        """Test that all missing subsection fields are reported."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{}]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        # Should report all 3 missing fields: name, source, columns
        self.assertEqual(len(errors), 3)
        error_fields = {e.location.split('.')[-1] for e in errors}
        self.assertEqual(error_fields, {'name', 'source', 'columns'})
    
    def test_subsection_not_object(self):
        """Test error when subsection is not an object."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": ["not an object"]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an object", errors[0].message)
    
    def test_subsection_columns_not_array(self):
        """Test error when 'columns' is not an array."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": "not an array"
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an array", errors[0].message)
    
    def test_subsection_empty_column_name(self):
        """Test error when column name is empty string."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": ["", "valid_col"]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be empty", errors[0].message)
    
    def test_subsection_invalid_column_type(self):
        """Test error when column is neither string nor object."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [123]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be a string or computed column object", errors[0].message)
    
    # =========================================================================
    # Cross-reference source query IDs with queries.json
    # =========================================================================
    
    def test_source_query_id_not_found(self):
        """Test error when source query ID doesn't exist in queries.json."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "NONEXISTENT_QUERY",
                    "columns": ["col1"]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("not found in queries.json", errors[0].message)
        self.assertIn("NONEXISTENT_QUERY", errors[0].message)
    
    def test_multiple_invalid_source_references(self):
        """Test that all invalid source references are reported."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [
                    {"name": "Sub1", "source": "INVALID_1", "columns": ["col1"]},
                    {"name": "Sub2", "source": "INVALID_2", "columns": ["col1"]},
                    {"name": "Sub3", "source": "QUERY_A", "columns": ["col1"]}  # Valid
                ]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 2)
        error_messages = [e.message for e in errors]
        self.assertTrue(any("INVALID_1" in msg for msg in error_messages))
        self.assertTrue(any("INVALID_2" in msg for msg in error_messages))
    
    def test_valid_source_query_id(self):
        """Test that valid source query IDs are accepted."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": ["col1", "col2"]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
        self.assertEqual(report_format.sections[0].subsections[0].source, "QUERY_A")
    
    # =========================================================================
    # Computed columns with name and expr fields
    # =========================================================================
    
    def test_computed_column_valid(self):
        """Test valid computed column with name and expr."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [
                        "col1",
                        {"name": "Days", "expr": "Value / 86400"}
                    ]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
        columns = report_format.sections[0].subsections[0].columns
        self.assertEqual(len(columns), 2)
        self.assertEqual(columns[0], "col1")
        self.assertEqual(columns[1].name, "Days")
        self.assertEqual(columns[1].expr, "Value / 86400")
    
    def test_computed_column_missing_name(self):
        """Test error when computed column is missing 'name' field."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [{"expr": "Value / 86400"}]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("name", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_computed_column_missing_expr(self):
        """Test error when computed column is missing 'expr' field."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [{"name": "Days"}]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("expr", errors[0].location)
        self.assertIn("Missing required field", errors[0].message)
    
    def test_computed_column_missing_both_fields(self):
        """Test that both missing fields are reported for computed column."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [{}]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 2)
        error_fields = {e.location.split('.')[-1] for e in errors}
        self.assertEqual(error_fields, {'name', 'expr'})
    
    def test_computed_column_empty_name(self):
        """Test error when computed column 'name' is empty."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [{"name": "", "expr": "Value / 86400"}]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be empty", errors[0].message)
    
    def test_computed_column_empty_expr(self):
        """Test error when computed column 'expr' is empty."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [{"name": "Days", "expr": ""}]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be empty", errors[0].message)
    
    # =========================================================================
    # Optional condition field validation
    # =========================================================================
    
    def test_valid_condition(self):
        """Test valid condition with engine-specific filtering."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": ["col1"],
                    "condition": {"engine": ["mysql", "aurora-mysql"]}
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
        condition = report_format.sections[0].subsections[0].condition
        self.assertEqual(condition, {"engine": ["mysql", "aurora-mysql"]})
    
    def test_condition_not_object(self):
        """Test error when 'condition' is not an object."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": ["col1"],
                    "condition": "not an object"
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an object", errors[0].message)
    
    def test_condition_value_not_array(self):
        """Test error when condition value is not an array."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": ["col1"],
                    "condition": {"engine": "mysql"}
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an array", errors[0].message)
    
    def test_condition_array_value_not_string(self):
        """Test error when condition array contains non-string values."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": ["col1"],
                    "condition": {"engine": [123, "mysql"]}
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be strings", errors[0].message)
    
    # =========================================================================
    # Display ALL validation errors found
    # =========================================================================
    
    def test_multiple_errors_all_reported(self):
        """Test that multiple different errors across sections are all reported."""
        self._write_report_format_json({
            "sections": [
                {
                    "name": "Section 1",
                    "subsections": [
                        {"name": "Sub1", "source": "INVALID_QUERY", "columns": ["col1"]},
                        {"source": "QUERY_A", "columns": ["col1"]}  # Missing name
                    ]
                },
                {
                    "subsections": []  # Missing name
                },
                {
                    "name": "Section 3",
                    "subsections": [
                        {"name": "Sub3", "source": "QUERY_B", "columns": [{"name": ""}]}  # Empty computed name
                    ]
                }
            ]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        # Should have multiple errors: invalid query, missing subsection name, missing section name, empty computed name + missing expr
        self.assertGreaterEqual(len(errors), 4)
    
    # =========================================================================
    # Complete valid report format parsing
    # =========================================================================
    
    def test_complete_valid_report_format(self):
        """Test parsing of a complete valid report_format.json."""
        self._write_report_format_json({
            "sections": [
                {
                    "name": "Server Information",
                    "subsections": [
                        {
                            "name": "Server Details",
                            "source": "SERVER_INFO",
                            "columns": ["Variable_name", "Value"]
                        },
                        {
                            "name": "Uptime",
                            "source": "GLOBAL_STATUS",
                            "columns": [
                                "Variable_name",
                                "Value",
                                {"name": "Days", "expr": "Value / 86400"}
                            ]
                        }
                    ]
                },
                {
                    "name": "Replication Status",
                    "subsections": [
                        {
                            "name": "Replica Status",
                            "source": "QUERY_A",
                            "columns": ["col1", "col2"],
                            "condition": {"engine": ["mysql", "aurora-mysql"]}
                        }
                    ]
                }
            ]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
        self.assertEqual(len(report_format.sections), 2)
        
        # Verify first section
        section1 = report_format.sections[0]
        self.assertEqual(section1.name, "Server Information")
        self.assertEqual(len(section1.subsections), 2)
        self.assertEqual(section1.subsections[0].name, "Server Details")
        self.assertEqual(section1.subsections[0].source, "SERVER_INFO")
        
        # Verify computed column
        uptime_subsection = section1.subsections[1]
        self.assertEqual(len(uptime_subsection.columns), 3)
        self.assertEqual(uptime_subsection.columns[2].name, "Days")
        self.assertEqual(uptime_subsection.columns[2].expr, "Value / 86400")
        
        # Verify second section with condition
        section2 = report_format.sections[1]
        self.assertEqual(section2.name, "Replication Status")
        self.assertEqual(section2.subsections[0].condition, {"engine": ["mysql", "aurora-mysql"]})
    
    def test_empty_sections_array(self):
        """Test that empty sections array is valid."""
        self._write_report_format_json({"sections": []})
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
        self.assertEqual(len(report_format.sections), 0)
    
    def test_empty_subsections_array(self):
        """Test that empty subsections array is valid."""
        self._write_report_format_json({
            "sections": [{
                "name": "Empty Section",
                "subsections": []
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
        self.assertEqual(len(report_format.sections[0].subsections), 0)


if __name__ == '__main__':
    unittest.main()


class TestExpressionValidation(unittest.TestCase):
    """
    Tests for computed column expression validation.
    
    Tests validate:
    - Verify computed column expressions contain only supported operators (+, -, *, /, parentheses)
    - Report specific expression and syntax error when invalid
    - Defer column reference validation until runtime (don't validate column names exist)
    """
    
    def setUp(self):
        """Create a temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.validator = InputValidator(self.temp_dir)
    
    def tearDown(self):
        """Clean up temporary files."""
        os.rmdir(self.temp_dir)
    
    # =========================================================================
    # Valid expressions - should return None
    # =========================================================================
    
    def test_simple_division(self):
        """Test simple division expression: Value / 86400"""
        result = self.validator.validate_expression("Value / 86400")
        self.assertIsNone(result)
    
    def test_simple_addition(self):
        """Test simple addition expression: col_a + col_b"""
        result = self.validator.validate_expression("col_a + col_b")
        self.assertIsNone(result)
    
    def test_simple_subtraction(self):
        """Test simple subtraction expression: total - used"""
        result = self.validator.validate_expression("total - used")
        self.assertIsNone(result)
    
    def test_simple_multiplication(self):
        """Test simple multiplication expression: rate * 100"""
        result = self.validator.validate_expression("rate * 100")
        self.assertIsNone(result)
    
    def test_parentheses_expression(self):
        """Test expression with parentheses: (col_a + col_b) / 2"""
        result = self.validator.validate_expression("(col_a + col_b) / 2")
        self.assertIsNone(result)
    
    def test_nested_parentheses(self):
        """Test expression with nested parentheses: ((a + b) * c) / d"""
        result = self.validator.validate_expression("((a + b) * c) / d")
        self.assertIsNone(result)
    
    def test_multiple_operators(self):
        """Test expression with multiple operators: a + b - c * d / e"""
        result = self.validator.validate_expression("a + b - c * d / e")
        self.assertIsNone(result)
    
    def test_decimal_numbers(self):
        """Test expression with decimal numbers: Value / 86400.0"""
        result = self.validator.validate_expression("Value / 86400.0")
        self.assertIsNone(result)
    
    def test_decimal_only(self):
        """Test expression with decimal starting with dot: col * 0.5"""
        result = self.validator.validate_expression("col * 0.5")
        self.assertIsNone(result)
    
    def test_underscore_in_identifier(self):
        """Test expression with underscores in column names: Seconds_Behind_Source / 60"""
        result = self.validator.validate_expression("Seconds_Behind_Source / 60")
        self.assertIsNone(result)
    
    def test_single_identifier(self):
        """Test expression with just a column name: Value"""
        result = self.validator.validate_expression("Value")
        self.assertIsNone(result)
    
    def test_single_number(self):
        """Test expression with just a number: 42"""
        result = self.validator.validate_expression("42")
        self.assertIsNone(result)
    
    def test_whitespace_handling(self):
        """Test expression with extra whitespace: a   +   b"""
        result = self.validator.validate_expression("  a   +   b  ")
        self.assertIsNone(result)
    
    def test_unary_minus_at_start(self):
        """Test expression with unary minus at start: -Value"""
        result = self.validator.validate_expression("-Value")
        self.assertIsNone(result)
    
    def test_unary_minus_after_lparen(self):
        """Test expression with unary minus after parenthesis: (-a + b)"""
        result = self.validator.validate_expression("(-a + b)")
        self.assertIsNone(result)
    
    def test_complex_expression(self):
        """Test complex expression: (total_reads + total_writes) / (uptime_seconds / 3600)"""
        result = self.validator.validate_expression("(total_reads + total_writes) / (uptime_seconds / 3600)")
        self.assertIsNone(result)
    
    def test_identifier_starting_with_underscore(self):
        """Test identifier starting with underscore: _private_col + 1"""
        result = self.validator.validate_expression("_private_col + 1")
        self.assertIsNone(result)
    
    def test_identifier_with_numbers(self):
        """Test identifier with numbers: col1 + col2"""
        result = self.validator.validate_expression("col1 + col2")
        self.assertIsNone(result)
    
    # =========================================================================
    # Invalid expressions - should return error message
    # =========================================================================
    
    def test_empty_expression(self):
        """Test empty expression returns error."""
        result = self.validator.validate_expression("")
        self.assertIsNotNone(result)
        self.assertIn("empty", result.lower())
    
    def test_whitespace_only_expression(self):
        """Test whitespace-only expression returns error."""
        result = self.validator.validate_expression("   ")
        self.assertIsNotNone(result)
        self.assertIn("empty", result.lower())
    
    def test_invalid_character_at_sign(self):
        """Test expression with @ character returns error."""
        result = self.validator.validate_expression("col @ 5")
        self.assertIsNotNone(result)
        self.assertIn("Invalid character", result)
    
    def test_invalid_character_percent(self):
        """Test expression with % character returns error."""
        result = self.validator.validate_expression("col % 5")
        self.assertIsNotNone(result)
        self.assertIn("Invalid character", result)
    
    def test_invalid_character_caret(self):
        """Test expression with ^ character returns error."""
        result = self.validator.validate_expression("col ^ 2")
        self.assertIsNotNone(result)
        self.assertIn("Invalid character", result)
    
    def test_invalid_character_ampersand(self):
        """Test expression with & character returns error."""
        result = self.validator.validate_expression("a & b")
        self.assertIsNotNone(result)
        self.assertIn("Invalid character", result)
    
    def test_invalid_character_pipe(self):
        """Test expression with | character returns error."""
        result = self.validator.validate_expression("a | b")
        self.assertIsNotNone(result)
        self.assertIn("Invalid character", result)
    
    def test_unmatched_opening_paren(self):
        """Test expression with unmatched opening parenthesis."""
        result = self.validator.validate_expression("(a + b")
        self.assertIsNotNone(result)
        self.assertIn("Unmatched", result)
    
    def test_unmatched_closing_paren(self):
        """Test expression with unmatched closing parenthesis."""
        result = self.validator.validate_expression("a + b)")
        self.assertIsNotNone(result)
        self.assertIn("Unmatched", result)
    
    def test_multiple_unmatched_parens(self):
        """Test expression with multiple unmatched parentheses."""
        result = self.validator.validate_expression("((a + b)")
        self.assertIsNotNone(result)
        self.assertIn("Unmatched", result)
    
    def test_operator_at_end(self):
        """Test expression ending with operator."""
        result = self.validator.validate_expression("a + b +")
        self.assertIsNotNone(result)
        self.assertIn("end with an operator", result)
    
    def test_consecutive_operators(self):
        """Test expression with consecutive operators (not unary minus)."""
        result = self.validator.validate_expression("a + * b")
        self.assertIsNotNone(result)
        self.assertIn("Unexpected operator", result)
    
    def test_consecutive_operands(self):
        """Test expression with consecutive operands."""
        result = self.validator.validate_expression("a b + c")
        self.assertIsNotNone(result)
        self.assertIn("Unexpected", result)
    
    def test_empty_parentheses(self):
        """Test expression with empty parentheses."""
        result = self.validator.validate_expression("a + ()")
        self.assertIsNotNone(result)
        self.assertIn("Unexpected", result)
    
    def test_operator_before_rparen(self):
        """Test expression with operator before closing paren."""
        result = self.validator.validate_expression("(a +)")
        self.assertIsNotNone(result)
        self.assertIn("Unexpected", result)
    
    def test_invalid_number_format(self):
        """Test expression with invalid number format (trailing dot)."""
        result = self.validator.validate_expression("a + 5.")
        self.assertIsNotNone(result)
        self.assertIn("Invalid number", result)
    
    def test_lparen_after_operand(self):
        """Test expression with ( directly after operand."""
        result = self.validator.validate_expression("a(b + c)")
        self.assertIsNotNone(result)
        self.assertIn("Unexpected", result)
    
    # =========================================================================
    # Column reference validation deferred to runtime
    # =========================================================================
    
    def test_any_identifier_accepted(self):
        """Test that any valid identifier is accepted (column validation deferred to runtime)."""
        # These column names don't need to exist - validation is deferred
        result = self.validator.validate_expression("NonExistentColumn / 100")
        self.assertIsNone(result)
    
    def test_mixed_case_identifiers(self):
        """Test that mixed case identifiers are accepted."""
        result = self.validator.validate_expression("CamelCaseColumn + snake_case_column")
        self.assertIsNone(result)


class TestExpressionValidationIntegration(unittest.TestCase):
    """
    Integration tests for expression validation within report_format.json validation.
    
    Tests validate that invalid expressions are properly reported when validating
    the full report_format.json file.
    """
    
    def setUp(self):
        """Create a temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.validator = InputValidator()
        # Override paths to point to temp dir for test isolation
        self.validator.queries_path = os.path.join(self.temp_dir, 'queries.json')
        self.validator.report_format_path = os.path.join(self.temp_dir, 'report_format.json')
        self.valid_query_ids = {'QUERY_A', 'GLOBAL_STATUS'}
    
    def tearDown(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _write_report_format_json(self, content):
        """Helper to write report_format.json content."""
        report_path = os.path.join(self.temp_dir, 'report_format.json')
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(content, f)
    
    def test_valid_expression_in_report_format(self):
        """Test that valid expressions pass validation in report_format.json."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [
                        "col1",
                        {"name": "Days", "expr": "Value / 86400"},
                        {"name": "Average", "expr": "(col_a + col_b) / 2"}
                    ]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 0)
        self.assertIsNotNone(report_format)
    
    def test_invalid_expression_reported_with_details(self):
        """Test that invalid expressions are reported with specific details."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [
                        {"name": "Bad", "expr": "col % 5"}
                    ]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("expr", errors[0].location)
        self.assertIn("col % 5", errors[0].message)  # Specific expression
        self.assertIn("Invalid", errors[0].message)  # Error details
    
    def test_multiple_invalid_expressions_all_reported(self):
        """Test that multiple invalid expressions are all reported."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [
                        {"name": "Bad1", "expr": "a % b"},
                        {"name": "Good", "expr": "a + b"},
                        {"name": "Bad2", "expr": "(a + b"},
                        {"name": "Bad3", "expr": "a +"}
                    ]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        # Should have 3 errors for the 3 invalid expressions
        self.assertEqual(len(errors), 3)
        error_messages = [e.message for e in errors]
        self.assertTrue(any("a % b" in msg for msg in error_messages))
        self.assertTrue(any("(a + b" in msg for msg in error_messages))
        self.assertTrue(any("a +" in msg for msg in error_messages))
    
    def test_unmatched_paren_error_message(self):
        """Test that unmatched parenthesis error includes helpful message."""
        self._write_report_format_json({
            "sections": [{
                "name": "Test Section",
                "subsections": [{
                    "name": "Test Subsection",
                    "source": "QUERY_A",
                    "columns": [
                        {"name": "Bad", "expr": "((a + b)"}
                    ]
                }]
            }]
        })
        
        errors, report_format = self.validator.validate_report_format_json(self.valid_query_ids)
        
        self.assertEqual(len(errors), 1)
        self.assertIn("Unmatched", errors[0].message)


if __name__ == '__main__':
    unittest.main()


class TestNewQueryFormatValidation(unittest.TestCase):
    """
    Tests for the new query format with condition and version filtering.
    
    Tests validate:
    - Deprecated 'engine' field rejection
    - New 'condition' field validation
    - Version string format validation (min_engine_version, max_engine_version)
    """
    
    def setUp(self):
        """Create a temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.validator = InputValidator()
        # Override paths to point to temp dir for test isolation
        self.validator.queries_path = os.path.join(self.temp_dir, 'queries.json')
        self.validator.report_format_path = os.path.join(self.temp_dir, 'report_format.json')
    
    def tearDown(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _write_queries_json(self, content):
        """Helper to write queries.json content."""
        queries_path = os.path.join(self.temp_dir, 'queries.json')
        with open(queries_path, 'w', encoding='utf-8') as f:
            if isinstance(content, str):
                f.write(content)
            else:
                json.dump(content, f)
    
    # =========================================================================
    # Test deprecated 'engine' field rejection
    # =========================================================================
    
    def test_deprecated_engine_field_rejected(self):
        """Test that the unsupported 'engine' field is rejected with a clear error message."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "engine": "mysql",
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("engine", errors[0].location)
        self.assertIn("removed", errors[0].message.lower())
        self.assertIn("condition", errors[0].message.lower())
        self.assertEqual(len(queries), 0)  # Query should not be parsed
    
    def test_deprecated_engine_all_rejected(self):
        """Test that 'engine': 'all' is also rejected."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "engine": "all",
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("engine", errors[0].location)
        self.assertEqual(len(queries), 0)
    
    # =========================================================================
    # Test new format without filtering (runs on all engines/versions)
    # =========================================================================
    
    def test_query_without_filtering_fields_valid(self):
        """Test that queries without condition/version fields are valid (run on all)."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertIsNone(queries[0].condition)
        self.assertIsNone(queries[0].min_engine_version)
        self.assertIsNone(queries[0].max_engine_version)
    
    # =========================================================================
    # Test 'condition' field validation
    # =========================================================================
    
    def test_valid_condition_single_engine(self):
        """Test valid condition with single engine."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": ["mysql"]}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].condition, {"engine": ["mysql"]})
    
    def test_valid_condition_multiple_engines(self):
        """Test valid condition with multiple engines."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": ["mysql", "aurora-mysql", "mariadb"]}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].condition["engine"], ["mysql", "aurora-mysql", "mariadb"])
    
    def test_condition_not_object(self):
        """Test error when condition is not an object."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": "mysql"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("condition", errors[0].location)
        self.assertIn("must be an object", errors[0].message)
    
    def test_condition_missing_engine_key(self):
        """Test error when condition doesn't have 'engine' key."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"other": ["mysql"]}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("condition", errors[0].location)
        self.assertIn("engine", errors[0].message)
    
    def test_condition_engine_not_array(self):
        """Test error when condition.engine is not an array."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": "mysql"}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("condition.engine", errors[0].location)
        self.assertIn("must be an array", errors[0].message)
    
    def test_condition_engine_empty_array(self):
        """Test error when condition.engine is empty array."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": []}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be empty", errors[0].message)
    
    def test_condition_invalid_engine_type(self):
        """Test error when condition.engine contains invalid engine type."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": ["mysql", "postgres"]}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("Invalid engine type", errors[0].message)
        self.assertIn("postgres", errors[0].message)
    
    def test_condition_engine_value_not_string(self):
        """Test error when condition.engine contains non-string value."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": ["mysql", 123]}
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be a string", errors[0].message)
    
    # =========================================================================
    # Test version field validation
    # =========================================================================
    
    def test_valid_version_xy_format(self):
        """Test valid version in x.y format."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "min_engine_version": "8.0",
                "max_engine_version": "8.4"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].min_engine_version, "8.0")
        self.assertEqual(queries[0].max_engine_version, "8.4")
    
    def test_valid_version_xyz_format(self):
        """Test valid version in x.y.z format."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "min_engine_version": "8.0.29",
                "max_engine_version": "8.4.0"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].min_engine_version, "8.0.29")
        self.assertEqual(queries[0].max_engine_version, "8.4.0")
    
    def test_valid_version_empty_string(self):
        """Test valid empty string for version (no constraint)."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "min_engine_version": "",
                "max_engine_version": ""
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].min_engine_version, "")
        self.assertEqual(queries[0].max_engine_version, "")
    
    def test_invalid_version_format(self):
        """Test error for invalid version format."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "min_engine_version": "8",
                "max_engine_version": "8.4"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("min_engine_version", errors[0].location)
        self.assertIn("Invalid version format", errors[0].message)
    
    def test_invalid_version_with_text(self):
        """Test error for version with text."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "min_engine_version": "8.0-beta"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("Invalid version format", errors[0].message)
    
    def test_version_not_string(self):
        """Test error when version is not a string."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "min_engine_version": 8.0
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 1)
        self.assertIn("must be a string", errors[0].message)
    
    # =========================================================================
    # Test combined condition and version fields
    # =========================================================================
    
    def test_valid_condition_and_version_combined(self):
        """Test valid query with both condition and version fields."""
        self._write_queries_json({
            "queries": [{
                "id": "TEST",
                "section": "status",
                "command": "SELECT 1",
                "condition": {"engine": ["mysql", "aurora-mysql"]},
                "min_engine_version": "8.0",
                "max_engine_version": "8.4"
            }]
        })
        
        errors, queries = self.validator.validate_queries_json()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0].condition["engine"], ["mysql", "aurora-mysql"])
        self.assertEqual(queries[0].min_engine_version, "8.0")
        self.assertEqual(queries[0].max_engine_version, "8.4")


class TestVersionNormalization(unittest.TestCase):
    """Tests for version normalization helper in InputValidator."""
    
    def setUp(self):
        """Create a temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.validator = InputValidator()
        # Override paths to point to temp dir for test isolation
        self.validator.queries_path = os.path.join(self.temp_dir, 'queries.json')
        self.validator.report_format_path = os.path.join(self.temp_dir, 'report_format.json')
    
    def tearDown(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_is_valid_version_format_xy(self):
        """Test x.y format is valid."""
        self.assertTrue(self.validator._is_valid_version_format("8.0"))
        self.assertTrue(self.validator._is_valid_version_format("10.6"))
        self.assertTrue(self.validator._is_valid_version_format("5.7"))
    
    def test_is_valid_version_format_xyz(self):
        """Test x.y.z format is valid."""
        self.assertTrue(self.validator._is_valid_version_format("8.0.29"))
        self.assertTrue(self.validator._is_valid_version_format("10.6.12"))
        self.assertTrue(self.validator._is_valid_version_format("5.7.44"))
    
    def test_is_valid_version_format_invalid(self):
        """Test invalid version formats."""
        self.assertFalse(self.validator._is_valid_version_format("8"))
        self.assertFalse(self.validator._is_valid_version_format("8.0.29.1"))
        self.assertFalse(self.validator._is_valid_version_format("8.0-beta"))
        self.assertFalse(self.validator._is_valid_version_format("v8.0"))
        self.assertFalse(self.validator._is_valid_version_format("8.0."))
        self.assertFalse(self.validator._is_valid_version_format(".8.0"))


if __name__ == '__main__':
    unittest.main()
