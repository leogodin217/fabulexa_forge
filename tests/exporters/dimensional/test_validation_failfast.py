"""Fail-fast tests for TableNotFoundError paths in dimensional validation.

Verifies that both silent-fallback sites in validation.py now raise ExportError
instead of swallowing TableNotFoundError and returning an empty collection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from fabulexa_forge.config.models import ColumnDecl, SourceDecl, TableDecl
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.validation import (
    _grain_projectable_surface,
    check_scd2_needs_history,
)
from fabulexa_forge.reader.errors import TableNotFoundError


def _make_source(grain: str = "records", kind: str = "entity") -> SourceDecl:
    return SourceDecl(grain=grain, kind=kind)  # type: ignore[call-arg]


def _make_table_decl(
    name: str = "t",
    grain: str = "records",
    kind: str = "entity",
    scd: str = "type2",
) -> TableDecl:
    cols = [
        ColumnDecl(name="id", **{"from": "record_id"}),
    ]
    key = ["id"]
    src_kwargs: dict[str, object] = {"grain": grain, "kind": kind}
    if grain in ("history_point", "history_interval"):
        src_kwargs["property"] = "state"
    return TableDecl(
        name=name,
        role="dim",
        scd=scd,
        source=SourceDecl(**src_kwargs),  # type: ignore[arg-type]
        key=key,
        columns=cols,
    )


def _sidecar_raising(table_name: str) -> MagicMock:
    """Return a mock Sidecar whose .columns() raises TableNotFoundError."""
    sidecar = MagicMock()
    sidecar.columns.side_effect = TableNotFoundError(table_name)
    return sidecar


# ---------------------------------------------------------------------------
# Site 1: _grain_projectable_surface — line ~119
# ---------------------------------------------------------------------------


def test_available_columns_raises_export_error_on_missing_table() -> None:
    """_grain_projectable_surface must raise ExportError (not silently empty) when
    sidecar.columns() raises TableNotFoundError for an unresolved source table."""
    source = _make_source(grain="records", kind="entity")
    sidecar = _sidecar_raising("records__entity")

    with pytest.raises(ExportError, match="records__entity"):
        _grain_projectable_surface(source, sidecar, "records__entity")


# ---------------------------------------------------------------------------
# Site 2: history_tracked_available branch — line ~429
# ---------------------------------------------------------------------------


def test_tracked_column_check_raises_export_error_on_missing_table() -> None:
    """check_scd2_needs_history must raise ExportError (not silently misclassify
    has_tracked) when sidecar.columns() raises TableNotFoundError."""
    table_decl = _make_table_decl(grain="records", kind="consultant", scd="type2")
    sidecar = _sidecar_raising("records__consultant")
    # history_tracked_available() returns True so the flag check passes and
    # the raising branch for sidecar.columns() is entered.
    sidecar.history_tracked_available.return_value = True

    with pytest.raises(ExportError, match="records__consultant"):
        check_scd2_needs_history(table_decl, "records__consultant", sidecar)
