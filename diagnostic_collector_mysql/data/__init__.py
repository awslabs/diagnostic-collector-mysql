"""Bundled data files (queries.json, report_format.json) and path resolution helper."""

import importlib.resources as pkg_resources
import os


def get_data_path(filename: str) -> str:
    """Return the absolute path to a bundled data file.

    Args:
        filename: Name of the data file (e.g. 'queries.json').

    Returns:
        Absolute path string to the file inside diagnostic_collector_mysql/data/.

    Raises:
        FileNotFoundError: If the file does not exist at the expected location.
    """
    ref = pkg_resources.files(__package__) / filename
    path = str(ref)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Data file not found: {path}. "
            f"Expected at diagnostic_collector_mysql/data/{filename}"
        )
    return path
