"""
Input validation for Diagnostic Collector for MySQL.

This module provides the InputValidator class that validates all input files
(queries.json, report_format.json) before any database operations begin.

Checks performed:
- Query definition parsing
- queries.json validation
- Display ALL validation errors found
"""

import json
import os
from typing import Any, List, Optional, Set, Tuple, Union

from diagnostic_collector_mysql.data import get_data_path
from diagnostic_collector_mysql.models import (
    ComputedColumn,
    QueryDefinition,
    ReportFormat,
    ReportSection,
    ReportSubsection,
    RequiresCondition,
    ValidationError,
    ValidationResult,
    HEALTH_CHECK_QUERY_IDS,
    VALID_OPERATORS,
)


class InputValidator:
    """
    Validates all input files before database operations.
    
    This class implements fail-fast validation that checks queries.json
    and report_format.json for structural correctness and cross-references
    before any database connection is attempted.
    """
    
    # Required fields for each query object in queries.json
    REQUIRED_QUERY_FIELDS = {'id', 'section', 'command'}
    
    # Valid engine types for condition filtering
    VALID_ENGINE_TYPES = {'mysql', 'aurora-mysql', 'mariadb'}
    
    def __init__(self, script_dir: str = ""):
        """
        Initialize the validator with the directory containing input files.
        
        Args:
            script_dir: Kept for backward compatibility but ignored;
                        paths are resolved via importlib.resources
        """
        # script_dir kept for backward compatibility but ignored;
        # paths are resolved via importlib.resources
        self.script_dir = script_dir
        self.queries_path = get_data_path("queries.json")
        self.report_format_path = get_data_path("report_format.json")
    
    def validate_queries_json(self) -> Tuple[List[ValidationError], List[QueryDefinition]]:
        """
        Validate queries.json structure and required fields.
        
        Behavior:
        - File exists and contains valid JSON
        - Each query object has required fields: id, engine, section, command
        - Query IDs are unique within queries.json
        - All validation errors are collected, not just the first one
        
        Returns:
            Tuple of (list of validation errors, list of parsed QueryDefinition objects)
            If there are errors, the QueryDefinition list may be partial or empty.
        """
        errors: List[ValidationError] = []
        queries: List[QueryDefinition] = []
        
        # Check if file exists
        if not os.path.exists(self.queries_path):
            errors.append(ValidationError(
                file='queries.json',
                location='file',
                message=f"File not found: {self.queries_path}"
            ))
            return errors, queries
        
        # Try to load and parse JSON
        try:
            with open(self.queries_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            errors.append(ValidationError(
                file='queries.json',
                location=f"line {e.lineno}, column {e.colno}",
                message=f"Invalid JSON: {e.msg}"
            ))
            return errors, queries
        except IOError as e:
            errors.append(ValidationError(
                file='queries.json',
                location='file',
                message=f"Cannot read file: {e}"
            ))
            return errors, queries
        
        # Validate structure - expect a "queries" array
        if not isinstance(data, dict):
            errors.append(ValidationError(
                file='queries.json',
                location='root',
                message="Expected JSON object with 'queries' array"
            ))
            return errors, queries
        
        if 'queries' not in data:
            errors.append(ValidationError(
                file='queries.json',
                location='root',
                message="Missing required 'queries' array"
            ))
            return errors, queries
        
        queries_array = data['queries']
        if not isinstance(queries_array, list):
            errors.append(ValidationError(
                file='queries.json',
                location='queries',
                message="'queries' must be an array"
            ))
            return errors, queries
        
        # Track seen IDs for uniqueness check
        seen_ids: Set[str] = set()
        
        # Validate each query object
        for index, query_obj in enumerate(queries_array):
            location_prefix = f"queries[{index}]"
            
            # Check if query is a dictionary
            if not isinstance(query_obj, dict):
                errors.append(ValidationError(
                    file='queries.json',
                    location=location_prefix,
                    message="Query must be an object"
                ))
                continue
            
            # Check for required fields
            missing_fields = self.REQUIRED_QUERY_FIELDS - set(query_obj.keys())
            for field in sorted(missing_fields):
                errors.append(ValidationError(
                    file='queries.json',
                    location=f"{location_prefix}.{field}",
                    message=f"Missing required field: {field}"
                ))
            
            # Validate field types and values
            query_id = query_obj.get('id')
            section = query_obj.get('section')
            command = query_obj.get('command')
            condition = query_obj.get('condition')
            min_engine_version = query_obj.get('min_engine_version')
            max_engine_version = query_obj.get('max_engine_version')
            requires = query_obj.get('requires')
            requires_extended_checks = query_obj.get('requires_extended_checks', False)
            
            # Validate requires_extended_checks field
            if not isinstance(requires_extended_checks, bool):
                errors.append(ValidationError(
                    file='queries.json',
                    location=f"{location_prefix}.requires_extended_checks",
                    message=f"'requires_extended_checks' must be a boolean, got {type(requires_extended_checks).__name__}"
                ))
                requires_extended_checks = False
            
            # Check for deprecated 'engine' field and reject it
            if 'engine' in query_obj:
                errors.append(ValidationError(
                    file='queries.json',
                    location=f"{location_prefix}.engine",
                    message="The 'engine' field has been removed. Use 'condition': {'engine': ['mysql', 'aurora-mysql', 'mariadb']} for engine filtering, or omit for all engines."
                ))
            
            # Validate id field
            if query_id is not None:
                if not isinstance(query_id, str):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.id",
                        message=f"'id' must be a string, got {type(query_id).__name__}"
                    ))
                elif not query_id.strip():
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.id",
                        message="'id' cannot be empty"
                    ))
                else:
                    # Check for duplicate IDs
                    # Duplicate IDs are allowed when queries have different conditions
                    # (engine type, version ranges, or requires conditions) to support
                    # version-specific SQL syntax variations.
                    if query_id in seen_ids:
                        # Check if this is an intentional duplicate with different conditions
                        has_condition = condition is not None
                        has_version = (min_engine_version and min_engine_version != "") or (max_engine_version and max_engine_version != "")
                        has_requires = requires is not None
                        
                        if not (has_condition or has_version or has_requires):
                            # No differentiating conditions - this is a true duplicate
                            errors.append(ValidationError(
                                file='queries.json',
                                location=f"{location_prefix}.id",
                                message=f"Duplicate query ID: '{query_id}'. Duplicate IDs are only allowed when queries have different conditions (engine, version, or requires)."
                            ))
                    seen_ids.add(query_id)
            
            # Validate section field
            if section is not None:
                if not isinstance(section, str):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.section",
                        message=f"'section' must be a string, got {type(section).__name__}"
                    ))
                elif not section.strip():
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.section",
                        message="'section' cannot be empty"
                    ))
            
            # Validate command field
            if command is not None:
                if not isinstance(command, str):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.command",
                        message=f"'command' must be a string, got {type(command).__name__}"
                    ))
                elif not command.strip():
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.command",
                        message="'command' cannot be empty"
                    ))
            
            # Validate optional 'condition' field
            parsed_condition = None
            condition_valid = True
            if condition is not None:
                if not isinstance(condition, dict):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.condition",
                        message=f"'condition' must be an object, got {type(condition).__name__}"
                    ))
                    condition_valid = False
                elif 'engine' not in condition:
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.condition",
                        message="'condition' must contain an 'engine' key with a list of valid engines"
                    ))
                    condition_valid = False
                else:
                    engine_list = condition.get('engine')
                    if not isinstance(engine_list, list):
                        errors.append(ValidationError(
                            file='queries.json',
                            location=f"{location_prefix}.condition.engine",
                            message=f"'condition.engine' must be an array, got {type(engine_list).__name__}"
                        ))
                        condition_valid = False
                    elif len(engine_list) == 0:
                        errors.append(ValidationError(
                            file='queries.json',
                            location=f"{location_prefix}.condition.engine",
                            message="'condition.engine' array cannot be empty"
                        ))
                        condition_valid = False
                    else:
                        for eng_idx, eng_val in enumerate(engine_list):
                            if not isinstance(eng_val, str):
                                errors.append(ValidationError(
                                    file='queries.json',
                                    location=f"{location_prefix}.condition.engine[{eng_idx}]",
                                    message=f"Engine value must be a string, got {type(eng_val).__name__}"
                                ))
                                condition_valid = False
                            elif eng_val not in self.VALID_ENGINE_TYPES:
                                errors.append(ValidationError(
                                    file='queries.json',
                                    location=f"{location_prefix}.condition.engine[{eng_idx}]",
                                    message=f"Invalid engine type: '{eng_val}'. Must be one of: {', '.join(sorted(self.VALID_ENGINE_TYPES))}"
                                ))
                                condition_valid = False
                if condition_valid:
                    parsed_condition = condition
            
            # Validate optional 'requires' field (already extracted above)
            parsed_requires = None
            if requires is not None:
                requires_errors, parsed_requires = self._validate_requires_field(
                    requires,
                    f"{location_prefix}.requires"
                )
                errors.extend(requires_errors)
            
            # Validate optional version fields
            parsed_min_version = None
            parsed_max_version = None
            
            if min_engine_version is not None:
                if not isinstance(min_engine_version, str):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.min_engine_version",
                        message=f"'min_engine_version' must be a string, got {type(min_engine_version).__name__}"
                    ))
                elif min_engine_version != "" and not self._is_valid_version_format(min_engine_version):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.min_engine_version",
                        message=f"Invalid version format: '{min_engine_version}'. Must be 'x.y' or 'x.y.z' format, or empty string for no constraint."
                    ))
                else:
                    parsed_min_version = min_engine_version
            
            if max_engine_version is not None:
                if not isinstance(max_engine_version, str):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.max_engine_version",
                        message=f"'max_engine_version' must be a string, got {type(max_engine_version).__name__}"
                    ))
                elif max_engine_version != "" and not self._is_valid_version_format(max_engine_version):
                    errors.append(ValidationError(
                        file='queries.json',
                        location=f"{location_prefix}.max_engine_version",
                        message=f"Invalid version format: '{max_engine_version}'. Must be 'x.y' or 'x.y.z' format, or empty string for no constraint."
                    ))
                else:
                    parsed_max_version = max_engine_version
            
            # If no errors for this query, create QueryDefinition object
            has_deprecated_engine = 'engine' in query_obj
            if not missing_fields and not has_deprecated_engine and all([
                isinstance(query_id, str) and query_id.strip(),
                isinstance(section, str) and section.strip(),
                isinstance(command, str) and command.strip()
            ]):
                queries.append(QueryDefinition(
                    id=query_id,
                    section=section,
                    command=command,
                    condition=parsed_condition,
                    min_engine_version=parsed_min_version,
                    max_engine_version=parsed_max_version,
                    requires=parsed_requires,
                    requires_extended_checks=requires_extended_checks
                ))
        
        return errors, queries
    
    def load_queries(self) -> Tuple[List[ValidationError], List[QueryDefinition]]:
        """
        Load and parse queries from queries.json.
        
        This is an alias for validate_queries_json() that provides a cleaner
        interface for callers who just want to load queries.
        
        Returns:
            Tuple of (list of validation errors, list of QueryDefinition objects)
        """
        return self.validate_queries_json()
    
    def get_query_ids(self) -> Tuple[List[ValidationError], Set[str]]:
        """
        Get the set of all query IDs from queries.json.
        
        This is useful for cross-reference validation with report_format.json.
        
        Returns:
            Tuple of (list of validation errors, set of query IDs)
        """
        errors, queries = self.validate_queries_json()
        query_ids = {q.id for q in queries}
        return errors, query_ids

    def validate_health_check_references(self, valid_query_ids: Set[str]) -> List[ValidationError]:
        """
        Verify every query ID the Health Overview evaluator reads exists in queries.json.

        A referenced ID that does not exist fails silently at runtime: the evaluator
        reads it, gets None back, and its guard skips the branch, so the check is
        absent from the report with no error raised. Absence then reads as health.
        This check turns that into a startup failure instead.

        Args:
            valid_query_ids: Set of query IDs actually present in queries.json

        Returns:
            List of validation errors, one per missing ID. Empty when all are present.
        """
        errors: List[ValidationError] = []

        for query_id in sorted(HEALTH_CHECK_QUERY_IDS - valid_query_ids):
            errors.append(ValidationError(
                file='queries.json',
                location=f"health check reference '{query_id}'",
                message=(
                    f"Health Overview reads query ID '{query_id}', which is not "
                    f"defined in queries.json. The check would be silently omitted "
                    f"from every report. Add the query or remove the reference."
                )
            ))

        return errors

    def _is_valid_version_format(self, version: str) -> bool:
        """
        Validate that a version string is in x.y or x.y.z format.
        
        Args:
            version: The version string to validate
            
        Returns:
            True if the version format is valid, False otherwise
        """
        import re
        # Match x.y or x.y.z format where x, y, z are non-negative integers
        pattern = r'^\d+\.\d+(\.\d+)?$'
        return bool(re.match(pattern, version))

    def _validate_requires_field(
        self,
        requires_obj: Any,
        location_prefix: str
    ) -> Tuple[List[ValidationError], Optional[RequiresCondition]]:
        """
        Validate the requires field structure.
        
        Behavior:
        - requires is an object (dict)
        - variable key exists and is a non-empty string
        - value key exists and is a string (can be empty)
        - optional operator key is one of valid operators: eq, ne, gt, gte, lt, lte
        
        Args:
            requires_obj: The requires field value to validate
            location_prefix: Location prefix for error messages (e.g., "queries[0].requires")
            
        Returns:
            Tuple of (list of validation errors, parsed RequiresCondition or None)
        """
        errors: List[ValidationError] = []
        
        # Validate requires is an object (dict)
        if not isinstance(requires_obj, dict):
            errors.append(ValidationError(
                file='queries.json',
                location=location_prefix,
                message=f"'requires' must be an object, got {type(requires_obj).__name__}"
            ))
            return errors, None
        
        # Validate variable key exists
        if 'variable' not in requires_obj:
            errors.append(ValidationError(
                file='queries.json',
                location=location_prefix,
                message="Missing required field: variable"
            ))
        
        # Validate value key exists
        if 'value' not in requires_obj:
            errors.append(ValidationError(
                file='queries.json',
                location=location_prefix,
                message="Missing required field: value"
            ))
        
        # If required keys are missing, return early
        if 'variable' not in requires_obj or 'value' not in requires_obj:
            return errors, None
        
        variable = requires_obj['variable']
        value = requires_obj['value']
        
        # Validate variable is a string
        if not isinstance(variable, str):
            errors.append(ValidationError(
                file='queries.json',
                location=f"{location_prefix}.variable",
                message=f"'variable' must be a string, got {type(variable).__name__}"
            ))
        elif not variable:
            # Validate variable is not empty
            errors.append(ValidationError(
                file='queries.json',
                location=f"{location_prefix}.variable",
                message="'variable' cannot be empty"
            ))
        
        # Validate value is a string (can be empty)
        if not isinstance(value, str):
            errors.append(ValidationError(
                file='queries.json',
                location=f"{location_prefix}.value",
                message=f"'value' must be a string, got {type(value).__name__}"
            ))
        
        # Validate optional operator key
        operator = requires_obj.get('operator', 'eq')
        if 'operator' in requires_obj:
            if requires_obj['operator'] not in VALID_OPERATORS:
                errors.append(ValidationError(
                    file='queries.json',
                    location=f"{location_prefix}.operator",
                    message=f"Invalid operator '{requires_obj['operator']}'. Must be one of: eq, ne, gt, gte, lt, lte"
                ))
        
        # If there are errors, return None for the RequiresCondition
        if errors:
            return errors, None
        
        # Create and return the RequiresCondition
        return errors, RequiresCondition(
            variable=variable,
            operator=operator,
            value=value
        )

    def validate_report_format_json(self, valid_query_ids: Set[str]) -> Tuple[List[ValidationError], Optional[ReportFormat]]:
        """
        Validate report_format.json structure and cross-reference query IDs.

        Behavior:
        - File exists and contains valid JSON
        - Section and subsection structure
        - Computed columns with name and expr fields
        - Source query IDs exist in queries.json
        - All validation errors are collected, not just the first one

        Args:
            valid_query_ids: Set of valid query IDs from queries.json for cross-reference

        Returns:
            Tuple of (list of validation errors, parsed ReportFormat object or None)
            If there are errors, the ReportFormat may be partial or None.
        """
        errors: List[ValidationError] = []
        report_format: Optional[ReportFormat] = None

        # Check if file exists
        if not os.path.exists(self.report_format_path):
            errors.append(ValidationError(
                file='report_format.json',
                location='file',
                message=f"File not found: {self.report_format_path}"
            ))
            return errors, report_format

        # Try to load and parse JSON
        try:
            with open(self.report_format_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            errors.append(ValidationError(
                file='report_format.json',
                location=f"line {e.lineno}, column {e.colno}",
                message=f"Invalid JSON: {e.msg}"
            ))
            return errors, report_format
        except IOError as e:
            errors.append(ValidationError(
                file='report_format.json',
                location='file',
                message=f"Cannot read file: {e}"
            ))
            return errors, report_format

        # Validate structure - expect a "sections" array
        if not isinstance(data, dict):
            errors.append(ValidationError(
                file='report_format.json',
                location='root',
                message="Expected JSON object with 'sections' array"
            ))
            return errors, report_format

        if 'sections' not in data:
            errors.append(ValidationError(
                file='report_format.json',
                location='root',
                message="Missing required 'sections' array"
            ))
            return errors, report_format

        sections_array = data['sections']
        if not isinstance(sections_array, list):
            errors.append(ValidationError(
                file='report_format.json',
                location='sections',
                message="'sections' must be an array"
            ))
            return errors, report_format

        # Parse and validate each section
        parsed_sections: List[ReportSection] = []

        for section_index, section_obj in enumerate(sections_array):
            section_location = f"sections[{section_index}]"

            # Check if section is a dictionary
            if not isinstance(section_obj, dict):
                errors.append(ValidationError(
                    file='report_format.json',
                    location=section_location,
                    message="Section must be an object"
                ))
                continue

            # Validate section 'name' field
            section_name = section_obj.get('name')
            if section_name is None:
                errors.append(ValidationError(
                    file='report_format.json',
                    location=f"{section_location}.name",
                    message="Missing required field: name"
                ))
            elif not isinstance(section_name, str):
                errors.append(ValidationError(
                    file='report_format.json',
                    location=f"{section_location}.name",
                    message=f"'name' must be a string, got {type(section_name).__name__}"
                ))
                section_name = None
            elif not section_name.strip():
                errors.append(ValidationError(
                    file='report_format.json',
                    location=f"{section_location}.name",
                    message="'name' cannot be empty"
                ))
                section_name = None

            # Parse optional 'number' field for section ordering
            section_number = section_obj.get('number', '')
            if not isinstance(section_number, str):
                section_number = str(section_number) if section_number is not None else ''

            # Parse optional 'short_name' field for navigation display
            section_short_name = section_obj.get('short_name', '')
            if not isinstance(section_short_name, str):
                section_short_name = str(section_short_name) if section_short_name is not None else ''

            # Validate section 'subsections' field
            subsections_array = section_obj.get('subsections')
            if subsections_array is None:
                errors.append(ValidationError(
                    file='report_format.json',
                    location=f"{section_location}.subsections",
                    message="Missing required field: subsections"
                ))
                continue

            if not isinstance(subsections_array, list):
                errors.append(ValidationError(
                    file='report_format.json',
                    location=f"{section_location}.subsections",
                    message="'subsections' must be an array"
                ))
                continue

            # Parse and validate each subsection
            parsed_subsections: List[ReportSubsection] = []

            for subsection_index, subsection_obj in enumerate(subsections_array):
                subsection_location = f"{section_location}.subsections[{subsection_index}]"

                # Check if subsection is a dictionary
                if not isinstance(subsection_obj, dict):
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=subsection_location,
                        message="Subsection must be an object"
                    ))
                    continue

                subsection_valid = True

                # Validate subsection 'name' field
                subsection_name = subsection_obj.get('name')
                if subsection_name is None:
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.name",
                        message="Missing required field: name"
                    ))
                    subsection_valid = False
                elif not isinstance(subsection_name, str):
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.name",
                        message=f"'name' must be a string, got {type(subsection_name).__name__}"
                    ))
                    subsection_valid = False
                elif not subsection_name.strip():
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.name",
                        message="'name' cannot be empty"
                    ))
                    subsection_valid = False

                # Validate subsection 'source' field
                source = subsection_obj.get('source')
                if source is None:
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.source",
                        message="Missing required field: source"
                    ))
                    subsection_valid = False
                elif not isinstance(source, str):
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.source",
                        message=f"'source' must be a string, got {type(source).__name__}"
                    ))
                    subsection_valid = False
                elif not source.strip():
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.source",
                        message="'source' cannot be empty"
                    ))
                    subsection_valid = False
                else:
                    # Cross-reference validation
                    if source not in valid_query_ids:
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.source",
                            message=f"Source query ID '{source}' not found in queries.json"
                        ))
                        subsection_valid = False

                # Validate subsection 'columns' field
                columns_array = subsection_obj.get('columns')
                if columns_array is None:
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.columns",
                        message="Missing required field: columns"
                    ))
                    subsection_valid = False
                elif not isinstance(columns_array, list):
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.columns",
                        message="'columns' must be an array"
                    ))
                    subsection_valid = False
                else:
                    # Validate each column entry
                    parsed_columns: List[Union[str, ComputedColumn]] = []
                    columns_valid = True

                    for col_index, col_entry in enumerate(columns_array):
                        col_location = f"{subsection_location}.columns[{col_index}]"

                        if isinstance(col_entry, str):
                            # Simple column name
                            if not col_entry.strip():
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=col_location,
                                    message="Column name cannot be empty"
                                ))
                                columns_valid = False
                            else:
                                parsed_columns.append(col_entry)
                        elif isinstance(col_entry, dict):
                            # Computed column
                            computed_valid = True

                            # Validate 'name' field
                            col_name = col_entry.get('name')
                            if col_name is None:
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=f"{col_location}.name",
                                    message="Missing required field: name"
                                ))
                                computed_valid = False
                            elif not isinstance(col_name, str):
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=f"{col_location}.name",
                                    message=f"'name' must be a string, got {type(col_name).__name__}"
                                ))
                                computed_valid = False
                            elif not col_name.strip():
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=f"{col_location}.name",
                                    message="'name' cannot be empty"
                                ))
                                computed_valid = False

                            # Validate 'expr' field
                            col_expr = col_entry.get('expr')
                            if col_expr is None:
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=f"{col_location}.expr",
                                    message="Missing required field: expr"
                                ))
                                computed_valid = False
                            elif not isinstance(col_expr, str):
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=f"{col_location}.expr",
                                    message=f"'expr' must be a string, got {type(col_expr).__name__}"
                                ))
                                computed_valid = False
                            elif not col_expr.strip():
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=f"{col_location}.expr",
                                    message="'expr' cannot be empty"
                                ))
                                computed_valid = False
                            else:
                                # Validate expression syntax
                                expr_error = self.validate_expression(col_expr)
                                if expr_error:
                                    errors.append(ValidationError(
                                        file='report_format.json',
                                        location=f"{col_location}.expr",
                                        message=f"Invalid expression '{col_expr}': {expr_error}"
                                    ))
                                    computed_valid = False

                            if computed_valid:
                                parsed_columns.append(ComputedColumn(name=col_name, expr=col_expr))
                            else:
                                columns_valid = False
                        else:
                            errors.append(ValidationError(
                                file='report_format.json',
                                location=col_location,
                                message=f"Column must be a string or computed column object, got {type(col_entry).__name__}"
                            ))
                            columns_valid = False

                    if not columns_valid:
                        subsection_valid = False

                # Validate optional 'condition' field
                condition = subsection_obj.get('condition')
                parsed_condition = None
                if condition is not None:
                    if not isinstance(condition, dict):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.condition",
                            message=f"'condition' must be an object, got {type(condition).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        # Validate condition structure (engine-specific conditions)
                        for cond_key, cond_value in condition.items():
                            if not isinstance(cond_value, list):
                                errors.append(ValidationError(
                                    file='report_format.json',
                                    location=f"{subsection_location}.condition.{cond_key}",
                                    message=f"Condition value must be an array, got {type(cond_value).__name__}"
                                ))
                                subsection_valid = False
                            else:
                                for val_index, val in enumerate(cond_value):
                                    if not isinstance(val, str):
                                        errors.append(ValidationError(
                                            file='report_format.json',
                                            location=f"{subsection_location}.condition.{cond_key}[{val_index}]",
                                            message=f"Condition array values must be strings, got {type(val).__name__}"
                                        ))
                                        subsection_valid = False

                        if subsection_valid or condition:
                            parsed_condition = condition

                # Validate optional 'min_engine_version' and 'max_engine_version' fields
                min_engine_version = subsection_obj.get('min_engine_version')
                max_engine_version = subsection_obj.get('max_engine_version')
                parsed_min_version = None
                parsed_max_version = None
                
                # Both must be present or both must be absent
                has_min = min_engine_version is not None
                has_max = max_engine_version is not None
                
                if has_min != has_max:
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}",
                        message="'min_engine_version' and 'max_engine_version' must both be present or both be absent"
                    ))
                    subsection_valid = False
                elif has_min and has_max:
                    # Validate min_engine_version
                    if not isinstance(min_engine_version, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.min_engine_version",
                            message=f"'min_engine_version' must be a string, got {type(min_engine_version).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_min_version = min_engine_version
                    
                    # Validate max_engine_version
                    if not isinstance(max_engine_version, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.max_engine_version",
                            message=f"'max_engine_version' must be a string, got {type(max_engine_version).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_max_version = max_engine_version

                # Validate optional 'default_note' field
                default_note = subsection_obj.get('default_note')
                parsed_default_note = None
                if default_note is not None:
                    if not isinstance(default_note, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.default_note",
                            message=f"'default_note' must be a string, got {type(default_note).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_default_note = default_note

                # Validate optional 'note' field
                note = subsection_obj.get('note')
                parsed_note = None
                if note is not None:
                    if not isinstance(note, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.note",
                            message=f"'note' must be a string, got {type(note).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_note = note

                # Validate optional 'warning' field
                warning = subsection_obj.get('warning')
                parsed_warning = None
                if warning is not None:
                    if not isinstance(warning, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.warning",
                            message=f"'warning' must be a string, got {type(warning).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_warning = warning

                # Validate optional 'result_note' field
                result_note = subsection_obj.get('result_note')
                parsed_result_note = None
                if result_note is not None:
                    if not isinstance(result_note, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.result_note",
                            message=f"'result_note' must be a string, got {type(result_note).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_result_note = result_note

                # Validate optional 'requires_extended_checks' field
                requires_extended_checks = subsection_obj.get('requires_extended_checks', False)
                if not isinstance(requires_extended_checks, bool):
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.requires_extended_checks",
                        message=f"'requires_extended_checks' must be a boolean, got {type(requires_extended_checks).__name__}"
                    ))
                    subsection_valid = False
                    requires_extended_checks = False

                # Validate optional 'extended_checks_disabled_note' field
                extended_checks_disabled_note = subsection_obj.get('extended_checks_disabled_note')
                parsed_extended_checks_disabled_note = None
                if extended_checks_disabled_note is not None:
                    if not isinstance(extended_checks_disabled_note, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.extended_checks_disabled_note",
                            message=f"'extended_checks_disabled_note' must be a string, got {type(extended_checks_disabled_note).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_extended_checks_disabled_note = extended_checks_disabled_note

                # Validate optional 'fallback_source' field
                fallback_source = subsection_obj.get('fallback_source')
                parsed_fallback_source = None
                if fallback_source is not None:
                    if not isinstance(fallback_source, str):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.fallback_source",
                            message=f"'fallback_source' must be a string, got {type(fallback_source).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_fallback_source = fallback_source

                # Validate optional 'fallback_columns' field
                fallback_columns = subsection_obj.get('fallback_columns')
                parsed_fallback_columns = None
                if fallback_columns is not None:
                    if not isinstance(fallback_columns, list):
                        errors.append(ValidationError(
                            file='report_format.json',
                            location=f"{subsection_location}.fallback_columns",
                            message=f"'fallback_columns' must be an array, got {type(fallback_columns).__name__}"
                        ))
                        subsection_valid = False
                    else:
                        parsed_fallback_columns = fallback_columns

                # Validate optional 'fallback_requires_extended_checks' field
                fallback_requires_extended_checks = subsection_obj.get('fallback_requires_extended_checks', False)
                if not isinstance(fallback_requires_extended_checks, bool):
                    errors.append(ValidationError(
                        file='report_format.json',
                        location=f"{subsection_location}.fallback_requires_extended_checks",
                        message=f"'fallback_requires_extended_checks' must be a boolean, got {type(fallback_requires_extended_checks).__name__}"
                    ))
                    subsection_valid = False
                    fallback_requires_extended_checks = False

                # Create ReportSubsection if valid
                if subsection_valid and columns_array is not None:
                    parsed_subsections.append(ReportSubsection(
                        name=subsection_name,
                        source=source,
                        columns=parsed_columns,
                        condition=parsed_condition,
                        min_engine_version=parsed_min_version,
                        max_engine_version=parsed_max_version,
                        default_note=parsed_default_note,
                        note=parsed_note,
                        warning=parsed_warning,
                        result_note=parsed_result_note,
                        requires_extended_checks=requires_extended_checks,
                        extended_checks_disabled_note=parsed_extended_checks_disabled_note,
                        fallback_source=parsed_fallback_source,
                        fallback_columns=parsed_fallback_columns,
                        fallback_requires_extended_checks=fallback_requires_extended_checks
                    ))

            # Create ReportSection if section name is valid
            if section_name is not None:
                parsed_sections.append(ReportSection(
                    name=section_name,
                    subsections=parsed_subsections,
                    number=section_number,
                    short_name=section_short_name
                ))

        # Create ReportFormat if we have any valid sections
        if parsed_sections or not errors:
            report_format = ReportFormat(sections=parsed_sections)

        return errors, report_format
    def validate_expression(self, expr: str) -> Optional[str]:
        """
        Validate computed column expression syntax.

        Validates that the expression contains only supported operators (+, -, *, /, parentheses),
        column names (identifiers), and numeric literals.

        Verify computed column expressions contain only supported operators
        Report specific expression and syntax error when invalid
        Defer column reference validation until runtime (don't validate column names exist)

        Args:
            expr: The expression string to validate (e.g., "Value / 86400", "(col_a + col_b) / 2")

        Returns:
            None if the expression is valid, or an error message string if invalid.
        """
        if not expr or not expr.strip():
            return "Expression cannot be empty"

        expr = expr.strip()

        # Tokenize the expression
        tokens, error = self._tokenize_expression(expr)
        if error:
            return error

        # Validate the token sequence (syntax check)
        error = self._validate_token_sequence(tokens)
        if error:
            return error

        return None

    def _tokenize_expression(self, expr: str) -> Tuple[List[Tuple[str, str]], Optional[str]]:
        """
        Tokenize an expression into a list of (token_type, token_value) tuples.

        Token types:
        - 'NUMBER': numeric literal (integer or decimal)
        - 'IDENTIFIER': column name
        - 'OPERATOR': +, -, *, /
        - 'LPAREN': (
        - 'RPAREN': )

        Args:
            expr: The expression string to tokenize

        Returns:
            Tuple of (list of tokens, error message or None)
        """
        tokens: List[Tuple[str, str]] = []
        i = 0

        while i < len(expr):
            char = expr[i]

            # Skip whitespace
            if char.isspace():
                i += 1
                continue

            # Operators
            if char in '+-*/':
                tokens.append(('OPERATOR', char))
                i += 1
                continue

            # Parentheses
            if char == '(':
                tokens.append(('LPAREN', char))
                i += 1
                continue

            if char == ')':
                tokens.append(('RPAREN', char))
                i += 1
                continue

            # Numbers (integer or decimal)
            if char.isdigit() or (char == '.' and i + 1 < len(expr) and expr[i + 1].isdigit()):
                start = i
                has_decimal = False

                while i < len(expr):
                    c = expr[i]
                    if c.isdigit():
                        i += 1
                    elif c == '.' and not has_decimal:
                        has_decimal = True
                        i += 1
                    else:
                        break

                number_str = expr[start:i]
                # Validate the number format
                if number_str == '.' or number_str.endswith('.'):
                    return [], f"Invalid number format: '{number_str}'"

                tokens.append(('NUMBER', number_str))
                continue

            # Identifiers (column names) - start with letter or underscore
            if char.isalpha() or char == '_':
                start = i
                while i < len(expr) and (expr[i].isalnum() or expr[i] == '_'):
                    i += 1

                identifier = expr[start:i]
                tokens.append(('IDENTIFIER', identifier))
                continue

            # Unknown character
            return [], f"Invalid character '{char}' at position {i + 1}"

        return tokens, None

    def _validate_token_sequence(self, tokens: List[Tuple[str, str]]) -> Optional[str]:
        """
        Validate that the token sequence forms a valid expression.

        Valid expression grammar (simplified):
        - Expression must not be empty
        - Expression must start with NUMBER, IDENTIFIER, LPAREN, or unary minus
        - Expression must end with NUMBER, IDENTIFIER, or RPAREN
        - Operators must be between operands
        - Parentheses must be balanced

        Args:
            tokens: List of (token_type, token_value) tuples

        Returns:
            Error message if invalid, None if valid
        """
        if not tokens:
            return "Expression cannot be empty"

        paren_depth = 0
        prev_type = None

        # Track what can come next
        # After operand (NUMBER, IDENTIFIER, RPAREN): expect OPERATOR or RPAREN or end
        # After operator: expect operand (NUMBER, IDENTIFIER, LPAREN) or unary minus
        # After LPAREN: expect operand or unary minus

        for i, (token_type, token_value) in enumerate(tokens):
            if token_type == 'LPAREN':
                paren_depth += 1
                # LPAREN can come at start, after operator, or after another LPAREN
                if prev_type is not None and prev_type not in ('OPERATOR', 'LPAREN'):
                    return f"Unexpected '(' at position {i + 1}"

            elif token_type == 'RPAREN':
                paren_depth -= 1
                if paren_depth < 0:
                    return "Unmatched closing parenthesis ')'"
                # RPAREN must come after operand or another RPAREN
                if prev_type not in ('NUMBER', 'IDENTIFIER', 'RPAREN'):
                    return f"Unexpected ')' at position {i + 1}"

            elif token_type == 'OPERATOR':
                # Handle unary minus at start or after LPAREN
                if token_value == '-' and (prev_type is None or prev_type == 'LPAREN'):
                    # Unary minus is allowed - treat as if we're expecting an operand
                    pass
                elif prev_type not in ('NUMBER', 'IDENTIFIER', 'RPAREN'):
                    return f"Unexpected operator '{token_value}' at position {i + 1}"

            elif token_type in ('NUMBER', 'IDENTIFIER'):
                # Operands can come at start, after operator, or after LPAREN
                if prev_type is not None and prev_type not in ('OPERATOR', 'LPAREN'):
                    return f"Unexpected {token_type.lower()} '{token_value}' at position {i + 1}"

            prev_type = token_type

        # Check parentheses are balanced
        if paren_depth != 0:
            return f"Unmatched opening parenthesis '(' ({paren_depth} unclosed)"

        # Expression must end with operand or RPAREN
        if prev_type not in ('NUMBER', 'IDENTIFIER', 'RPAREN'):
            return "Expression cannot end with an operator"

        return None



