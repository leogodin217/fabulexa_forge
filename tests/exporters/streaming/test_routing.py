"""Tests for exporters/streaming/routing.py — Layer-A-only routing surface.

Covers route_attributes, membership_route_attributes, resolve_subtype_index.
Layer B (topic_template, groups) is retired; a declared stream's `name` is
the topic, carried straight through by the engine — there is nothing left in
routing.py to resolve or enumerate. resolve_subtype_index tests build a
minimal in-process emit via duckdb.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.sidecar_builder import identity_column, prop_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.routing import (
    membership_route_attributes,
    resolve_subtype_index,
    route_attributes,
)
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl

# ---------------------------------------------------------------------------
# Emit builder for resolve_subtype_index tests
# ---------------------------------------------------------------------------

_RECORD_COLS_WITH_TYPE: list[dict[str, Any]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
]

_RECORD_COLS_WITHOUT_TYPE: list[dict[str, Any]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_HISTORY_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_subtyped_emit(
    tmp_path: Path,
    kind: str,
    cols: list[dict[str, Any]],
    rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal emit with one sub-typed kind."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", cols))
    conn.execute(_ddl("history", _HISTORY_COLS))

    placeholders = ", ".join("?" for _ in cols)
    for row in rows:
        conn.execute(
            f'INSERT INTO "records__{kind}" VALUES ({placeholders})', list(row)
        )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": f"records__{kind}",
                "category": "records",
                "columns": cols,
                "rows": len(rows),
                "record_kind": kind,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": 0,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests: route_attributes
# ---------------------------------------------------------------------------


class TestRouteAttributesSubtyped:
    """route_attributes for a sub-typed kind."""

    def test_sub_typed_returns_kind_route_table_sub_type(self) -> None:
        """Sub-typed kind: route_table == sub_type, sub_type key present."""
        result = route_attributes(True, "actor", "customer")
        assert result == {
            "kind": "actor",
            "route_table": "customer",
            "sub_type": "customer",
        }

    def test_sub_typed_vip_customer(self) -> None:
        """Different sub_type value resolves correctly."""
        result = route_attributes(True, "actor", "vip_customer")
        assert result == {
            "kind": "actor",
            "route_table": "vip_customer",
            "sub_type": "vip_customer",
        }

    def test_sub_typed_staff(self) -> None:
        """Staff sub_type resolves correctly."""
        result = route_attributes(True, "actor", "staff")
        assert result == {"kind": "actor", "route_table": "staff", "sub_type": "staff"}

    def test_sub_typed_but_sub_type_none_raises(self) -> None:
        """Sub-typed kind with sub_type=None raises ValueError."""
        with pytest.raises(ValueError, match="sub-typed"):
            route_attributes(True, "actor", None)


class TestRouteAttributesNonSubtyped:
    """route_attributes for a non-sub-typed kind."""

    def test_non_sub_typed_returns_kind_only(self) -> None:
        """Non-sub-typed kind: route_table == kind, no sub_type key."""
        result = route_attributes(False, "device", None)
        assert result == {"kind": "device", "route_table": "device"}
        assert "sub_type" not in result

    def test_non_sub_typed_with_sub_type_given_raises(self) -> None:
        """Non-sub-typed kind with sub_type given raises ValueError."""
        with pytest.raises(ValueError, match="not sub-typed"):
            route_attributes(False, "device", "sensor")


# ---------------------------------------------------------------------------
# Tests: resolve_subtype_index
# ---------------------------------------------------------------------------


class TestResolveSubtypeIndex:
    """resolve_subtype_index over a built sub-typed emit."""

    def test_maps_record_id_to_discriminator(self, tmp_path: Path) -> None:
        """Every record_id maps to its discriminator value."""
        rows = [
            ("trunk", "r1", 1, True, None, 1, 0, "customer"),
            ("trunk", "r2", 2, True, None, 2, 1, "vip_customer"),
            ("trunk", "r3", 3, True, None, 3, 2, "staff"),
        ]
        emit_dir = _build_subtyped_emit(tmp_path, "actor", _RECORD_COLS_WITH_TYPE, rows)
        with open_emit(emit_dir) as emit:
            index = resolve_subtype_index(emit, "actor")

        assert index == {"r1": "customer", "r2": "vip_customer", "r3": "staff"}

    def test_independent_of_selected_properties(self, tmp_path: Path) -> None:
        """The index reads the discriminator only, not selected properties."""
        # Same as above — the function doesn't take a properties parameter
        rows = [
            ("trunk", "r1", 1, True, None, 1, 0, "staff"),
        ]
        emit_dir = _build_subtyped_emit(tmp_path, "actor", _RECORD_COLS_WITH_TYPE, rows)
        with open_emit(emit_dir) as emit:
            index = resolve_subtype_index(emit, "actor")
        assert index == {"r1": "staff"}

    def test_raises_export_error_when_discriminator_absent(
        self, tmp_path: Path
    ) -> None:
        """Raises ExportError when prop__<kind>_type column is absent from sidecar."""
        rows: list[tuple[Any, ...]] = [
            ("trunk", "r1", 1, True, None, 1, 0),
        ]
        emit_dir = _build_subtyped_emit(
            tmp_path, "actor", _RECORD_COLS_WITHOUT_TYPE, rows
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="discriminator column"):
                resolve_subtype_index(emit, "actor")

    def test_raises_export_error_when_table_absent(self, tmp_path: Path) -> None:
        """Raises ExportError when records__<kind> table is absent from sidecar."""
        # Build an emit with "device" kind, then ask for "actor"
        rows: list[tuple[Any, ...]] = [
            ("trunk", "r1", 1, True, None, 1, 0),
        ]
        emit_dir = _build_subtyped_emit(
            tmp_path, "device", _RECORD_COLS_WITHOUT_TYPE, rows
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="not found in sidecar"):
                resolve_subtype_index(emit, "actor")


# ---------------------------------------------------------------------------
# membership_route_attributes tests
# ---------------------------------------------------------------------------


class TestMembershipRouteAttributes:
    """Tests for membership_route_attributes Layer-A route attributes."""

    def test_returns_expected_attrs(self) -> None:
        """Result contains owner_kind, property, and route_table with correct values."""
        attrs = membership_route_attributes("queue", "waiters")
        assert attrs["owner_kind"] == "queue"
        assert attrs["property"] == "waiters"
        assert attrs["route_table"] == "queue__waiters"

    def test_no_sub_type_key(self) -> None:
        """Result does not contain a 'sub_type' key."""
        attrs = membership_route_attributes("team", "members")
        assert "sub_type" not in attrs

    def test_exact_keys(self) -> None:
        """Result has exactly the three expected keys."""
        attrs = membership_route_attributes("actor", "roles")
        assert set(attrs.keys()) == {"owner_kind", "property", "route_table"}

    def test_route_table_format(self) -> None:
        """route_table uses double underscore separator."""
        attrs = membership_route_attributes("patient", "ward_assignments")
        assert attrs["route_table"] == "patient__ward_assignments"
