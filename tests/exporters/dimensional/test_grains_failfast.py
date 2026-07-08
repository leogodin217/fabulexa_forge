"""Fail-fast tests for grain SQL builders when reader raises TableNotFoundError.

Verifies that when the reader builder raises TableNotFoundError (missing source
table), the composed grain builders surface it rather than silently falling back.

Phase 1 change: missing-table now surfaces TableNotFoundError from the reader
builder surface, not ExportError from the grain's former filter type-lookup.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from fabulexa_export.config.models import ColumnDecl, SourceDecl, TableDecl
from fabulexa_export.exporters.dimensional.grains import (
    _membership_order_by_columns,
    build_membership_sql,
    build_records_sql,
)
from fabulexa_export.reader.errors import TableNotFoundError


def _make_sidecar_raising(table_name: str) -> MagicMock:
    """Return a mock Sidecar whose .columns() raises TableNotFoundError."""
    sidecar = MagicMock()
    sidecar.columns.side_effect = TableNotFoundError(
        f"no table named '{table_name}' in sidecar"
    )
    return sidecar


def _simple_records_table_decl(filter_prop: str = "entity_type") -> TableDecl:
    """Minimal records-grain TableDecl with a filter."""
    return TableDecl(
        name="dim_entity",
        role="dim",
        scd="type1",
        source=SourceDecl(
            grain="records",
            kind="entity",
            filter={filter_prop: "consultant"},
        ),
        key=["id"],
        columns=[ColumnDecl(name="id", **{"from": "record_id"})],
    )


def _simple_membership_table_decl(where_col: str = "elem__role_name") -> TableDecl:
    """Minimal membership-grain TableDecl with a where clause."""
    return TableDecl(
        name="fact_membership",
        role="fact",
        source=SourceDecl(
            grain="membership",
            kind="journey_instance",
            property="team_members",
            where={where_col: "surgeon"},
        ),
        key=["id"],
        columns=[ColumnDecl(name="id", **{"from": "record_id"})],
    )


# ---------------------------------------------------------------------------
# Site 1: _membership_order_by_columns
# ---------------------------------------------------------------------------


def test_membership_order_by_raises_on_missing_table() -> None:
    """_membership_order_by_columns raises ExportError when the source table is absent."""
    from fabulexa_export.errors import ExportError

    sidecar = _make_sidecar_raising("membership__missing__tbl")

    with pytest.raises(ExportError, match="membership__missing__tbl"):
        _membership_order_by_columns("membership__missing__tbl", sidecar)


# ---------------------------------------------------------------------------
# Site 2: build_records_sql (reader surface raises TableNotFoundError)
# ---------------------------------------------------------------------------


def test_records_grain_filter_raises_table_not_found_from_reader() -> None:
    """build_records_sql with fork_path raises TableNotFoundError from the reader."""
    sidecar = _make_sidecar_raising("records__entity")
    table_decl = _simple_records_table_decl()

    with pytest.raises(TableNotFoundError, match="records__entity"):
        build_records_sql(
            table_decl=table_decl,
            source_table_name="records__entity",
            anchor=None,
            config=None,
            sidecar=sidecar,
            fork_path="trunk",
        )


# ---------------------------------------------------------------------------
# Site 3: build_membership_sql (reader surface raises TableNotFoundError)
# ---------------------------------------------------------------------------


def test_membership_where_raises_table_not_found_from_reader() -> None:
    """build_membership_sql with fork_path raises TableNotFoundError from the reader."""
    sidecar = _make_sidecar_raising("membership__journey_instance__team_members")
    table_decl = _simple_membership_table_decl()

    with pytest.raises(
        TableNotFoundError, match="membership__journey_instance__team_members"
    ):
        build_membership_sql(
            table_decl=table_decl,
            source_table_name="membership__journey_instance__team_members",
            sidecar=sidecar,
            anchor=None,
            config=None,
            fork_path="trunk",
        )
