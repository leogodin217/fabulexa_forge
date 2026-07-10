"""Fail-fast test: build_column_expr raises ExportError on unresolved source table.

Verifies that a value_map column whose source table is not found in the sidecar
raises ExportError immediately — never silently falling back to VARCHAR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import ColumnDecl, DerivedSpec, ValueMapSpec
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.columns import build_column_expr
from fabulexa_forge.reader.errors import TableNotFoundError


def _anchor() -> EffectiveAnchor:
    return EffectiveAnchor(
        start_instant=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        timezone=ZoneInfo("UTC"),
    )


def _value_map_col(name: str = "status_label", from_col: str = "status") -> ColumnDecl:
    return ColumnDecl(
        name=name,
        derived=DerivedSpec(
            value_map=ValueMapSpec(
                **{"from": from_col, "map": {"A": "Active", "I": "Inactive"}},
            )
        ),
    )


def test_build_column_expr_raises_on_unresolved_source_table() -> None:
    """build_column_expr raises ExportError when sidecar.columns() raises TableNotFoundError."""
    sidecar = MagicMock()
    sidecar.columns.side_effect = TableNotFoundError("records__ghost")

    col_decl = _value_map_col()

    with pytest.raises(ExportError) as exc_info:
        build_column_expr(
            col_decl=col_decl,
            anchor=_anchor(),
            sidecar=sidecar,
            source_table_name="records__ghost",
        )

    msg = str(exc_info.value)
    assert "status_label" in msg
    assert "records__ghost" in msg


def test_build_column_expr_no_varchar_fallback_on_table_not_found() -> None:
    """When TableNotFoundError is raised, no VARCHAR-typed SQL is returned (build must fail)."""
    sidecar = MagicMock()
    sidecar.columns.side_effect = TableNotFoundError("records__missing")

    col_decl = _value_map_col(name="risk_label", from_col="risk_code")

    raised = False
    result_sql = None
    try:
        result_sql, _ = build_column_expr(
            col_decl=col_decl,
            anchor=_anchor(),
            sidecar=sidecar,
            source_table_name="records__missing",
        )
    except ExportError:
        raised = True

    assert raised, "ExportError must be raised — silent VARCHAR fallback is forbidden"
    assert result_sql is None, (
        "No SQL must be produced when the source table is unresolved"
    )
