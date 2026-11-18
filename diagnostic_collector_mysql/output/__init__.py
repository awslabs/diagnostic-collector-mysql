# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Output directory management and file writing."""

from diagnostic_collector_mysql.output.output_manager import OutputManager, OutputDirectoryError

__all__ = ["OutputManager", "OutputDirectoryError"]
