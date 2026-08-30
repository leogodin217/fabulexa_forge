"""Shared test fixture builders for the compare test package.

Both `test_inputs.py` and `test_engine.py` build small expected/actual
DuckDB files and CSV directories to drive `compare_datasets` end-to-end.
Module-level, non-test (`_`-prefixed) so pytest never collects it.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import duckdb
import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


def assert_frozen(instance: object, field: str, value: object) -> None:
    """Assert that setting `field` on a frozen dataclass `instance` raises
    `dataclasses.FrozenInstanceError`.

    Centralizes the report dataclasses' frozen-mutation assertion (used by
    `test_canonical.py`'s four smoke tests) behind `setattr`, so the
    attribute-assignment `# type: ignore[misc]` mypy needs for a direct
    `instance.field = value` on a frozen dataclass collapses to this one site.
    """
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field, value)


def build_duckdb(path: "Path", statements: "Sequence[str]") -> "Path":
    """Create a DuckDB file at `path`, executing `statements` in order.

    Args:
        path: The file to create (must not already exist).
        statements: SQL statements (DDL/DML), executed in order.

    Returns:
        `path`, for chaining into a `compare_datasets` call.
    """
    con = duckdb.connect(str(path))
    try:
        for statement in statements:
            con.execute(statement)
    finally:
        con.close()
    return path


def write_csv_dir(directory: "Path", files: "Mapping[str, str]") -> "Path":
    """Create a CSV directory: one file per `files` entry, verbatim text.

    Args:
        directory: The directory to create (parents included).
        files: Filename (e.g. `"people.csv"`) -> full file text.

    Returns:
        `directory`, for chaining into a `compare_datasets` call.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory
