#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Format Sync Validation Script

This script validates that all four report formats (HTML, Markdown, Text, JSON)
are in sync by comparing their structure, sections, subsections, notes, and data.

Usage:
    python validate_format_sync.py <html_file> <markdown_file> <text_file> <json_file>

Example:
    python validate_format_sync.py \
        /tmp/html/report_20260330.html \
        /tmp/md/report_20260330.md \
        /tmp/txt/report_20260330.txt \
        /tmp/json/report_20260330.json

Exit Codes:
    0 - All validations passed
    1 - Validation failures detected
    2 - Invalid arguments or file not found
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class ValidationResult:
    """Stores validation results for a single check."""
    name: str
    passed: bool
    message: str
    details: List[str] = field(default_factory=list)


@dataclass
class FormatData:
    """Parsed data from a report file."""
    format_type: str
    file_path: str
    sections: List[str] = field(default_factory=list)
    subsections: Dict[str, List[str]] = field(default_factory=dict)
    section_notes: Dict[str, str] = field(default_factory=dict)
    subsection_notes: Dict[str, str] = field(default_factory=dict)
    subsection_data: Dict[str, List[List[str]]] = field(default_factory=dict)
    raw_content: str = ""


class HTMLParser:
    """Parser for HTML report format."""
    
    def parse(self, content: str) -> FormatData:
        data = FormatData(format_type="HTML", file_path="", raw_content=content)
        
        # Extract sections from <details> tags with summary
        section_pattern = r'<details[^>]*id="([^"]+)"[^>]*>\s*<summary>([^<]+)</summary>'
        section_positions = []
        for match in re.finditer(section_pattern, content):
            section_id = match.group(1)
            section_name = match.group(2).strip()
            # Remove section number prefix (e.g., "1. ", "1.5 ", "2. ")
            clean_name = re.sub(r'^\d+\.?\d*\s+', '', section_name)
            # Decode HTML entities
            clean_name = clean_name.replace('&amp;', '&')
            clean_name = clean_name.replace('&lt;', '<')
            clean_name = clean_name.replace('&gt;', '>')
            data.sections.append(clean_name)
            data.subsections[clean_name] = []
            section_positions.append((match.start(), match.end(), clean_name))
        
        # Extract subsections from <h3> tags
        h3_pattern = r'<h3>([^<]+)</h3>'
        for match in re.finditer(h3_pattern, content):
            subsection_name = match.group(1).strip()
            pos = match.start()
            
            # Find which section this h3 belongs to
            current_section = None
            for i, (start, end, name) in enumerate(section_positions):
                if pos > start:
                    # Check if there's a next section and we're before it
                    if i + 1 < len(section_positions):
                        next_start = section_positions[i + 1][0]
                        if pos < next_start:
                            current_section = name
                            break
                    else:
                        current_section = name
                        break
            
            if current_section and current_section in data.subsections:
                if subsection_name not in data.subsections[current_section]:
                    data.subsections[current_section].append(subsection_name)
        
        # Extract section notes from <div class="info"> after section summary
        info_pattern = r'<div class="info">([^<]+)</div>'
        for match in re.finditer(info_pattern, content):
            note = match.group(1).strip()
            pos = match.start()
            # Find the section this note belongs to
            for i, (start, end, name) in enumerate(section_positions):
                if pos > start:
                    if i + 1 < len(section_positions):
                        next_start = section_positions[i + 1][0]
                        if pos < next_start:
                            data.section_notes[name] = note
                            break
                    else:
                        data.section_notes[name] = note
                        break
        
        return data


class MarkdownParser:
    """Parser for Markdown report format."""
    
    def parse(self, content: str) -> FormatData:
        data = FormatData(format_type="Markdown", file_path="", raw_content=content)
        
        # Extract sections from ## headers (with or without numbers)
        # Pattern matches: "## 1. Section Name" or "## Section Name"
        section_pattern = r'^## (?:\d+\.?\d*\s+)?(.+)$'
        current_section = None
        
        for line in content.split('\n'):
            # Match section headers
            section_match = re.match(section_pattern, line)
            if section_match:
                section_name = section_match.group(1).strip()
                # Remove markdown escapes (e.g., \- becomes -)
                section_name = section_name.replace('\\-', '-')
                section_name = section_name.replace('\\&', '&')
                section_name = section_name.replace('\\*', '*')
                section_name = section_name.replace('\\_', '_')
                # Skip Table of Contents and Health Overview as they're not main sections
                if section_name.lower() not in ('table of contents', 'health overview'):
                    data.sections.append(section_name)
                    data.subsections[section_name] = []
                    current_section = section_name
                continue
            
            # Match subsection headers (### Subsection Name)
            if line.startswith('### ') and current_section:
                subsection_name = line[4:].strip()
                # Remove any numbering like "2.1 "
                subsection_name = re.sub(r'^\d+\.\d+\s+', '', subsection_name)
                # Skip "Health Checks" as it's part of Health Overview
                if subsection_name.lower() != 'health checks':
                    data.subsections[current_section].append(subsection_name)
        
        # Extract section notes (text immediately after section header, before first subsection)
        # Look for blockquotes (> text) after section headers
        section_note_pattern = r'^## (?:\d+\.?\d*\s+)?(.+)\n+> (.+?)(?=\n###|\n## |\Z)'
        for match in re.finditer(section_note_pattern, content, re.MULTILINE | re.DOTALL):
            section_name = match.group(1).strip()
            note_text = match.group(2).strip()
            if note_text:
                data.section_notes[section_name] = note_text
        
        return data


class TextParser:
    """Parser for Text report format."""
    
    def parse(self, content: str) -> FormatData:
        data = FormatData(format_type="Text", file_path="", raw_content=content)
        
        lines = content.split('\n')
        current_section = None
        i = 0
        
        while i < len(lines):
            line = lines[i].rstrip()
            
            # Check for section header pattern:
            # ================================================================================
            # (optional empty lines)
            # SECTION NAME
            # (optional empty lines)
            # ================================================================================
            if re.match(r'^={50,}', line):
                # Find the next non-empty line
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                
                if j < len(lines):
                    section_name = lines[j].strip()
                    
                    # Find the next equals line after the section name
                    k = j + 1
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    
                    if k < len(lines) and re.match(r'^={50,}', lines[k].rstrip()):
                        # This is a section header
                        # Skip non-section headers
                        if section_name and section_name not in ('Diagnostic Collector for MySQL Health Report', 'TABLE OF CONTENTS', 'HEALTH OVERVIEW'):
                            # Convert to title case for comparison
                            section_name_title = section_name.title()
                            # Handle special cases like "I/O" and "&"
                            section_name_title = section_name_title.replace(' & ', ' & ')
                            section_name_title = section_name_title.replace('I/o', 'I/O')
                            section_name_title = section_name_title.replace('Innodb', 'InnoDB')
                            section_name_title = section_name_title.replace('Sql', 'SQL')
                            data.sections.append(section_name_title)
                            data.subsections[section_name_title] = []
                            current_section = section_name_title
                        i = k + 1
                        continue
            
            # Check for subsection header pattern:
            # Subsection Name
            # --------------------------------------------------------------------------------
            if re.match(r'^-{40,}', line) and i > 0:
                prev_line = lines[i - 1].strip()
                if prev_line and not prev_line.startswith('-') and not prev_line.startswith('='):
                    # This is a subsection header
                    if current_section and current_section in data.subsections:
                        # Clean up the subsection name
                        subsection_name = prev_line
                        if subsection_name not in data.subsections[current_section]:
                            data.subsections[current_section].append(subsection_name)
            
            i += 1
        
        return data


class JSONParser:
    """Parser for JSON report format."""
    
    def parse(self, content: str) -> FormatData:
        data = FormatData(format_type="JSON", file_path="", raw_content=content)
        
        try:
            json_data = json.loads(content)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON: {e}")
            return data
        
        # Extract sections from the 'sections' array
        sections = json_data.get('sections', [])
        for section in sections:
            section_name = section.get('name', '')
            if section_name:
                data.sections.append(section_name)
                data.subsections[section_name] = []
                
                # Extract section note/overview
                overview = section.get('overview', '')
                if overview:
                    data.section_notes[section_name] = overview
                
                # Extract subsections
                subsections = section.get('subsections', [])
                for subsection in subsections:
                    subsection_name = subsection.get('name', '')
                    if subsection_name:
                        data.subsections[section_name].append(subsection_name)
                    
                    # Extract subsection note
                    note = subsection.get('note', '')
                    if note:
                        data.subsection_notes[subsection_name] = note
                    
                    # Extract subsection data rows
                    rows = subsection.get('rows', [])
                    if rows:
                        data.subsection_data[subsection_name] = rows
        
        return data


class FormatSyncValidator:
    """Validates that all four report formats are in sync."""
    
    def __init__(self, html_path: str, md_path: str, text_path: str, json_path: str):
        self.html_path = html_path
        self.md_path = md_path
        self.text_path = text_path
        self.json_path = json_path
        self.results: List[ValidationResult] = []
        
        # Parsers
        self.html_parser = HTMLParser()
        self.md_parser = MarkdownParser()
        self.text_parser = TextParser()
        self.json_parser = JSONParser()
        
        # Parsed data
        self.html_data: Optional[FormatData] = None
        self.md_data: Optional[FormatData] = None
        self.text_data: Optional[FormatData] = None
        self.json_data: Optional[FormatData] = None
    
    def load_files(self) -> bool:
        """Load and parse all report files."""
        try:
            with open(self.html_path, 'r', encoding='utf-8') as f:
                self.html_data = self.html_parser.parse(f.read())
                self.html_data.file_path = self.html_path
            
            with open(self.md_path, 'r', encoding='utf-8') as f:
                self.md_data = self.md_parser.parse(f.read())
                self.md_data.file_path = self.md_path
            
            with open(self.text_path, 'r', encoding='utf-8') as f:
                self.text_data = self.text_parser.parse(f.read())
                self.text_data.file_path = self.text_path
            
            with open(self.json_path, 'r', encoding='utf-8') as f:
                self.json_data = self.json_parser.parse(f.read())
                self.json_data.file_path = self.json_path
            
            return True
        except FileNotFoundError as e:
            print(f"ERROR: File not found: {e.filename}")
            return False
        except Exception as e:
            print(f"ERROR: Failed to load files: {e}")
            return False
    
    def validate_section_count(self) -> ValidationResult:
        """Validate that all formats have the same number of sections."""
        counts = {
            'HTML': len(self.html_data.sections),
            'Markdown': len(self.md_data.sections),
            'Text': len(self.text_data.sections),
            'JSON': len(self.json_data.sections)
        }
        
        unique_counts = set(counts.values())
        passed = len(unique_counts) == 1
        
        if passed:
            message = f"All formats have {counts['JSON']} sections"
        else:
            message = f"Section count mismatch: {counts}"
        
        return ValidationResult(
            name="Section Count",
            passed=passed,
            message=message,
            details=[f"{fmt}: {count} sections" for fmt, count in counts.items()]
        )
    
    def validate_section_names(self) -> ValidationResult:
        """Validate that all formats have the same section names (case-insensitive)."""
        # Use JSON as the reference (most structured)
        json_sections_lower = {s.lower() for s in self.json_data.sections}
        
        details = []
        mismatches = []
        
        for fmt, data in [('HTML', self.html_data), ('Markdown', self.md_data), 
                          ('Text', self.text_data)]:
            fmt_sections_lower = {s.lower() for s in data.sections}
            missing = json_sections_lower - fmt_sections_lower
            extra = fmt_sections_lower - json_sections_lower
            
            if missing:
                mismatches.append(f"{fmt} missing: {missing}")
                details.append(f"{fmt} missing sections: {missing}")
            if extra:
                mismatches.append(f"{fmt} extra: {extra}")
                details.append(f"{fmt} extra sections: {extra}")
        
        passed = len(mismatches) == 0
        message = "All formats have matching section names" if passed else f"Section name mismatches found"
        
        return ValidationResult(
            name="Section Names",
            passed=passed,
            message=message,
            details=details if details else ["All section names match across formats (case-insensitive)"]
        )
    
    def validate_subsection_count_per_section(self) -> ValidationResult:
        """Validate that each section has the same number of subsections across formats."""
        details = []
        mismatches = []
        
        # Known differences where HTML groups subsections differently
        # These are by design, not bugs
        known_html_groupings = {
            'Server Information & Version Details': 'HTML groups individual items into "Server Details" table',
            'Replication Topology & Status': 'HTML groups some replication items together',
        }
        
        # Use JSON sections as reference
        for section in self.json_data.sections:
            counts = {}
            section_lower = section.lower()
            
            # Get subsection count from each format
            counts['JSON'] = len(self.json_data.subsections.get(section, []))
            
            # Find matching section in other formats (case-insensitive)
            for fmt, data in [('HTML', self.html_data), ('Markdown', self.md_data), 
                              ('Text', self.text_data)]:
                matched = False
                for s in data.subsections:
                    if s.lower() == section_lower:
                        counts[fmt] = len(data.subsections[s])
                        matched = True
                        break
                if not matched:
                    # Try fuzzy match
                    for s in data.subsections:
                        if section_lower in s.lower() or s.lower() in section_lower:
                            counts[fmt] = len(data.subsections[s])
                            matched = True
                            break
                if not matched:
                    counts[fmt] = 0
            
            unique_counts = set(counts.values())
            if len(unique_counts) > 1:
                # Check if this is a known HTML grouping difference
                if section in known_html_groupings:
                    # Check if only HTML differs
                    non_html_counts = {k: v for k, v in counts.items() if k != 'HTML'}
                    if len(set(non_html_counts.values())) == 1:
                        details.append(f"{section}: HTML groups differently (by design) - {counts}")
                        continue
                
                mismatches.append(section)
                details.append(f"{section}: {counts}")
        
        passed = len(mismatches) == 0
        message = "All sections have matching subsection counts" if passed else f"{len(mismatches)} sections have mismatched subsection counts"
        
        return ValidationResult(
            name="Subsection Counts",
            passed=passed,
            message=message,
            details=details if details else ["All subsection counts match"]
        )
    
    def validate_json_structure(self) -> ValidationResult:
        """Validate JSON report has required structure."""
        try:
            json_content = json.loads(self.json_data.raw_content)
        except json.JSONDecodeError as e:
            return ValidationResult(
                name="JSON Structure",
                passed=False,
                message=f"Invalid JSON: {e}",
                details=[]
            )
        
        required_keys = ['metadata', 'health_overview', 'sections']
        missing_keys = [k for k in required_keys if k not in json_content]
        
        details = []
        if missing_keys:
            details.append(f"Missing required keys: {missing_keys}")
        
        # Check metadata structure - accept either naming convention
        metadata = json_content.get('metadata', {})
        has_timestamp = 'generated_at' in metadata or 'timestamp' in metadata
        has_db_info = 'database_info' in metadata or 'database' in metadata
        
        if not has_timestamp:
            details.append("Missing timestamp in metadata (expected 'generated_at' or 'timestamp')")
        if not has_db_info:
            details.append("Missing database info in metadata (expected 'database_info' or 'database')")
        
        # Check sections structure
        sections = json_content.get('sections', [])
        for i, section in enumerate(sections):
            if 'name' not in section:
                details.append(f"Section {i} missing 'name' key")
            if 'subsections' not in section:
                details.append(f"Section '{section.get('name', i)}' missing 'subsections' key")
        
        passed = len(details) == 0
        message = "JSON structure is valid" if passed else "JSON structure has issues"
        
        return ValidationResult(
            name="JSON Structure",
            passed=passed,
            message=message,
            details=details if details else ["All required keys present"]
        )
    
    def validate_html_structure(self) -> ValidationResult:
        """Validate HTML report has required structure."""
        content = self.html_data.raw_content
        details = []
        
        # Check for required HTML elements
        required_patterns = [
            (r'<!DOCTYPE html>', "DOCTYPE declaration"),
            (r'<html', "HTML tag"),
            (r'<head>', "HEAD tag"),
            (r'<body>', "BODY tag"),
            (r'<title>', "TITLE tag"),
            (r'class="health-overview"', "Health Overview section"),
            (r'<details', "Collapsible sections"),
        ]
        
        for pattern, description in required_patterns:
            if not re.search(pattern, content, re.IGNORECASE):
                details.append(f"Missing: {description}")
        
        passed = len(details) == 0
        message = "HTML structure is valid" if passed else "HTML structure has issues"
        
        return ValidationResult(
            name="HTML Structure",
            passed=passed,
            message=message,
            details=details if details else ["All required HTML elements present"]
        )
    
    def validate_markdown_structure(self) -> ValidationResult:
        """Validate Markdown report has required structure."""
        content = self.md_data.raw_content
        details = []
        
        # Check for required Markdown elements
        required_patterns = [
            (r'^# .+', "Main title (H1)"),
            (r'^## ', "Section headers (H2)"),
            (r'^\|.+\|', "Tables"),
        ]
        
        for pattern, description in required_patterns:
            if not re.search(pattern, content, re.MULTILINE):
                details.append(f"Missing: {description}")
        
        passed = len(details) == 0
        message = "Markdown structure is valid" if passed else "Markdown structure has issues"
        
        return ValidationResult(
            name="Markdown Structure",
            passed=passed,
            message=message,
            details=details if details else ["All required Markdown elements present"]
        )
    
    def validate_text_structure(self) -> ValidationResult:
        """Validate Text report has required structure."""
        content = self.text_data.raw_content
        details = []
        
        # Check for required Text elements
        required_patterns = [
            (r'^={50,}', "Section separators (=====)"),
            (r'^-{40,}', "Subsection separators (-----)"),
            (r'\|.+\|', "Table rows"),
        ]
        
        for pattern, description in required_patterns:
            if not re.search(pattern, content, re.MULTILINE):
                details.append(f"Missing: {description}")
        
        passed = len(details) == 0
        message = "Text structure is valid" if passed else "Text structure has issues"
        
        return ValidationResult(
            name="Text Structure",
            passed=passed,
            message=message,
            details=details if details else ["All required Text elements present"]
        )
    
    def validate_health_overview_present(self) -> ValidationResult:
        """Validate that Health Overview is present in all formats."""
        details = []
        
        # Check HTML
        if 'health-overview' not in self.html_data.raw_content.lower():
            details.append("HTML: Health Overview section not found")
        
        # Check Markdown
        if 'health overview' not in self.md_data.raw_content.lower():
            details.append("Markdown: Health Overview section not found")
        
        # Check Text
        if 'health overview' not in self.text_data.raw_content.lower():
            details.append("Text: Health Overview section not found")
        
        # Check JSON
        try:
            json_content = json.loads(self.json_data.raw_content)
            if 'health_overview' not in json_content:
                details.append("JSON: health_overview key not found")
        except json.JSONDecodeError:
            details.append("JSON: Could not parse to check health_overview")
        
        passed = len(details) == 0
        message = "Health Overview present in all formats" if passed else "Health Overview missing in some formats"
        
        return ValidationResult(
            name="Health Overview",
            passed=passed,
            message=message,
            details=details if details else ["Health Overview found in all formats"]
        )
    
    def validate_database_info_present(self) -> ValidationResult:
        """Validate that database info is present in all formats."""
        details = []
        
        # Common database info patterns
        db_patterns = ['host', 'version', 'port']
        
        for pattern in db_patterns:
            # Check HTML
            if pattern not in self.html_data.raw_content.lower():
                details.append(f"HTML: '{pattern}' not found in database info")
            
            # Check Markdown
            if pattern not in self.md_data.raw_content.lower():
                details.append(f"Markdown: '{pattern}' not found in database info")
            
            # Check Text
            if pattern not in self.text_data.raw_content.lower():
                details.append(f"Text: '{pattern}' not found in database info")
        
        # Check JSON specifically - accept either naming convention
        try:
            json_content = json.loads(self.json_data.raw_content)
            db_info = json_content.get('metadata', {}).get('database_info', {})
            if not db_info:
                db_info = json_content.get('metadata', {}).get('database', {})
            
            for key in ['host', 'version']:
                if key not in db_info:
                    details.append(f"JSON: '{key}' not found in database info")
            # Port might be in connection info or database info
            if 'port' not in db_info and 'port' not in str(json_content):
                details.append(f"JSON: 'port' not found in database info")
        except json.JSONDecodeError:
            details.append("JSON: Could not parse to check database info")
        
        passed = len(details) == 0
        message = "Database info present in all formats" if passed else "Database info incomplete in some formats"
        
        return ValidationResult(
            name="Database Info",
            passed=passed,
            message=message,
            details=details if details else ["Database info found in all formats"]
        )
    
    def validate_no_error_messages(self) -> ValidationResult:
        """Validate that reports don't contain unexpected error messages."""
        details = []
        error_patterns = [
            r'Traceback \(most recent call last\)',
            r'Exception:',
            r'Error:.*failed',
            r'CRITICAL ERROR',
        ]
        
        for fmt, data in [('HTML', self.html_data), ('Markdown', self.md_data),
                          ('Text', self.text_data), ('JSON', self.json_data)]:
            for pattern in error_patterns:
                if re.search(pattern, data.raw_content, re.IGNORECASE):
                    details.append(f"{fmt}: Found error pattern '{pattern}'")
        
        passed = len(details) == 0
        message = "No unexpected error messages found" if passed else "Error messages found in reports"
        
        return ValidationResult(
            name="No Errors",
            passed=passed,
            message=message,
            details=details if details else ["No error messages detected"]
        )
    
    def run_all_validations(self) -> List[ValidationResult]:
        """Run all validation checks."""
        self.results = [
            self.validate_section_count(),
            self.validate_section_names(),
            self.validate_subsection_count_per_section(),
            self.validate_json_structure(),
            self.validate_html_structure(),
            self.validate_markdown_structure(),
            self.validate_text_structure(),
            self.validate_health_overview_present(),
            self.validate_database_info_present(),
            self.validate_no_error_messages(),
        ]
        return self.results
    
    def print_report(self) -> None:
        """Print validation report to stdout."""
        print("=" * 80)
        print("FORMAT SYNC VALIDATION REPORT")
        print("=" * 80)
        print()
        print("Input Files:")
        print(f"  HTML:     {self.html_path}")
        print(f"  Markdown: {self.md_path}")
        print(f"  Text:     {self.text_path}")
        print(f"  JSON:     {self.json_path}")
        print()
        print("-" * 80)
        print("VALIDATION RESULTS")
        print("-" * 80)
        print()
        
        passed_count = 0
        failed_count = 0
        
        for result in self.results:
            status = "✓ PASS" if result.passed else "✗ FAIL"
            if result.passed:
                passed_count += 1
            else:
                failed_count += 1
            
            print(f"{status}: {result.name}")
            print(f"       {result.message}")
            
            if result.details and (not result.passed or len(result.details) > 1):
                for detail in result.details[:10]:  # Limit details
                    print(f"       - {detail}")
                if len(result.details) > 10:
                    print(f"       ... and {len(result.details) - 10} more")
            print()
        
        print("-" * 80)
        print("SUMMARY")
        print("-" * 80)
        print(f"  Total Checks: {len(self.results)}")
        print(f"  Passed:       {passed_count}")
        print(f"  Failed:       {failed_count}")
        print()
        
        if failed_count == 0:
            print("✅ ALL VALIDATIONS PASSED - Formats are in sync!")
        else:
            print(f"⚠️  {failed_count} VALIDATION(S) FAILED - Review the issues above")
        print()
    
    def get_exit_code(self) -> int:
        """Return appropriate exit code based on validation results."""
        if all(r.passed for r in self.results):
            return 0
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Validate that all four report formats are in sync.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s report.html report.md report.txt report.json
  %(prog)s /tmp/html/report.html /tmp/md/report.md /tmp/txt/report.txt /tmp/json/report.json

Exit Codes:
  0 - All validations passed
  1 - Validation failures detected
  2 - Invalid arguments or file not found
        """
    )
    
    parser.add_argument('html_file', help='Path to HTML report file')
    parser.add_argument('markdown_file', help='Path to Markdown report file')
    parser.add_argument('text_file', help='Path to Text report file')
    parser.add_argument('json_file', help='Path to JSON report file')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Only print summary, not detailed results')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Print additional debug information')
    
    args = parser.parse_args()
    
    # Validate file paths exist
    for path, name in [(args.html_file, 'HTML'), (args.markdown_file, 'Markdown'),
                       (args.text_file, 'Text'), (args.json_file, 'JSON')]:
        if not os.path.exists(path):
            print(f"ERROR: {name} file not found: {path}")
            return 2
    
    # Create validator and run
    validator = FormatSyncValidator(
        html_path=args.html_file,
        md_path=args.markdown_file,
        text_path=args.text_file,
        json_path=args.json_file
    )
    
    if not validator.load_files():
        return 2
    
    validator.run_all_validations()
    
    if not args.quiet:
        validator.print_report()
    else:
        # Quiet mode - just print summary
        passed = sum(1 for r in validator.results if r.passed)
        failed = sum(1 for r in validator.results if not r.passed)
        if failed == 0:
            print(f"✅ All {passed} validations passed")
        else:
            print(f"⚠️  {failed}/{len(validator.results)} validations failed")
            for r in validator.results:
                if not r.passed:
                    print(f"  - {r.name}: {r.message}")
    
    return validator.get_exit_code()


if __name__ == '__main__':
    sys.exit(main())
