#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# Convenience script to validate format sync after generating reports.
#
# Usage:
#   ./scripts/validate_formats.sh <output_base_dir>
#
# Example:
#   # After running collector with all formats:
#   export DIAGNOSTIC_COLLECTOR_MYSQL_PASSWORD='your_password'
#   uv run python -m diagnostic_collector_mysql --host myhost --user test --output-dir /tmp/html --format html
#   uv run python -m diagnostic_collector_mysql --host myhost --user test --output-dir /tmp/md --format markdown
#   uv run python -m diagnostic_collector_mysql --host myhost --user test --output-dir /tmp/txt --format text
#   uv run python -m diagnostic_collector_mysql --host myhost --user test --output-dir /tmp/json --format json
#
#   # Then validate:
#   ./scripts/validate_formats.sh /tmp
#
# The script expects the following directory structure:
#   <output_base_dir>/html/diagnostic_collector_mysql_data_*/report_*.html
#   <output_base_dir>/md/diagnostic_collector_mysql_data_*/report_*.md
#   <output_base_dir>/txt/diagnostic_collector_mysql_data_*/report_*.txt
#   <output_base_dir>/json/diagnostic_collector_mysql_data_*/report_*.json
#

set -e

if [ $# -lt 1 ]; then
    echo "Usage: $0 <output_base_dir>"
    echo ""
    echo "Or provide all four file paths directly:"
    echo "  $0 <html_file> <md_file> <txt_file> <json_file>"
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_DIR="$(dirname "$SCRIPT_DIR")"

if [ $# -eq 4 ]; then
    # Four arguments provided - use them directly
    HTML_FILE="$1"
    MD_FILE="$2"
    TXT_FILE="$3"
    JSON_FILE="$4"
else
    # Single argument - base directory
    BASE_DIR="$1"
    
    # Find the most recent report files
    HTML_FILE=$(find "$BASE_DIR/html" -name "report_*.html" 2>/dev/null | sort -r | head -1)
    MD_FILE=$(find "$BASE_DIR/md" -name "report_*.md" 2>/dev/null | sort -r | head -1)
    TXT_FILE=$(find "$BASE_DIR/txt" -name "report_*.txt" 2>/dev/null | sort -r | head -1)
    JSON_FILE=$(find "$BASE_DIR/json" -name "report_*.json" 2>/dev/null | sort -r | head -1)
    
    if [ -z "$HTML_FILE" ]; then
        echo "ERROR: No HTML report found in $BASE_DIR/html/"
        exit 2
    fi
    if [ -z "$MD_FILE" ]; then
        echo "ERROR: No Markdown report found in $BASE_DIR/md/"
        exit 2
    fi
    if [ -z "$TXT_FILE" ]; then
        echo "ERROR: No Text report found in $BASE_DIR/txt/"
        exit 2
    fi
    if [ -z "$JSON_FILE" ]; then
        echo "ERROR: No JSON report found in $BASE_DIR/json/"
        exit 2
    fi
fi

echo "Validating format sync..."
echo ""

uv run python -m diagnostic_collector_mysql.utils.validate_format_sync "$HTML_FILE" "$MD_FILE" "$TXT_FILE" "$JSON_FILE"
