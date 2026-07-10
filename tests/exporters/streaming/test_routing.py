"""Tests for exporters/streaming/routing.py — Phase 2 routing functions.

Covers route_attributes, resolve_topic, enumerate_topics, resolve_subtype_index.
resolve_subtype_index tests build a minimal in-process emit via duckdb.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from fabulexa_forge.config.models import RoutingConfig
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.routing import (
    enumerate_topics,
    membership_route_attributes,
    resolve_subtype_index,
    resolve_topic,
    route_attributes,
)
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl

SUPPORTED_VERSION = 4

# ---------------------------------------------------------------------------
# Emit builder for resolve_subtype_index tests
# ---------------------------------------------------------------------------

_RECORD_COLS_WITH_TYPE: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__actor_type", "type": "VARCHAR"},
]

_RECORD_COLS_WITHOUT_TYPE: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
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
    """Build a minimal v4 emit with one sub-typed kind."""
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

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
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
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
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

    def test_is_subtyped_false_returns_bare_kind_attrs(self) -> None:
        """is_subtyped=False: bare-kind attributes, no sub_type key."""
        result = route_attributes(False, "device", None)
        assert result == {"kind": "device", "route_table": "device"}
        assert "sub_type" not in result

    def test_is_subtyped_false_with_sub_type_given_raises(self) -> None:
        """is_subtyped=False with sub_type given raises ValueError."""
        with pytest.raises(ValueError, match="not sub-typed"):
            route_attributes(False, "device", "x")


# ---------------------------------------------------------------------------
# Tests: resolve_topic
# ---------------------------------------------------------------------------


_DEFAULT_ROUTING = RoutingConfig()


class TestResolveTopic:
    """resolve_topic applies Layer-B policy."""

    def test_default_template_returns_route_table(self) -> None:
        """Default template {route_table} -> leaf name."""
        attrs = {"kind": "device", "route_table": "device"}
        assert resolve_topic(_DEFAULT_ROUTING, attrs) == "device"

    def test_kind_template_collapses_sub_types(self) -> None:
        """Template {kind} -> kind name (sub-types collapse to same topic)."""
        routing = RoutingConfig(topic_template="{kind}")
        attrs_c = {"kind": "actor", "route_table": "customer", "sub_type": "customer"}
        attrs_v = {
            "kind": "actor",
            "route_table": "vip_customer",
            "sub_type": "vip_customer",
        }
        assert resolve_topic(routing, attrs_c) == "actor"
        assert resolve_topic(routing, attrs_v) == "actor"

    def test_prefixed_template(self) -> None:
        """Template cdc.{route_table} -> prefixed name."""
        routing = RoutingConfig(topic_template="cdc.{route_table}")
        attrs = {"kind": "device", "route_table": "device"}
        assert resolve_topic(routing, attrs) == "cdc.device"

    def test_qualified_template(self) -> None:
        """Template {kind}.{sub_type} -> qualified name."""
        routing = RoutingConfig(topic_template="{kind}.{sub_type}")
        attrs = {"kind": "actor", "route_table": "customer", "sub_type": "customer"}
        assert resolve_topic(routing, attrs) == "actor.customer"

    def test_groups_remap_member_to_target(self) -> None:
        """A rendered name that is a member gets remapped to the group target."""
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        attrs_c = {"kind": "actor", "route_table": "customer", "sub_type": "customer"}
        attrs_v = {
            "kind": "actor",
            "route_table": "vip_customer",
            "sub_type": "vip_customer",
        }
        assert resolve_topic(routing, attrs_c) == "premium"
        assert resolve_topic(routing, attrs_v) == "premium"

    def test_non_member_passes_through(self) -> None:
        """A rendered name not in any group passes through unchanged."""
        routing = RoutingConfig(groups={"premium": ["customer"]})
        attrs = {"kind": "actor", "route_table": "staff", "sub_type": "staff"}
        assert resolve_topic(routing, attrs) == "staff"

    def test_missing_placeholder_raises_key_error(self) -> None:
        """Template referencing an absent placeholder raises KeyError."""
        routing = RoutingConfig(topic_template="{kind}.{sub_type}")
        attrs = {"kind": "device", "route_table": "device"}  # no sub_type
        with pytest.raises(KeyError):
            resolve_topic(routing, attrs)

    def test_two_attributes_rendering_same_name_resolve_same_topic(self) -> None:
        """Two distinct attribute mappings rendering to one name resolve identically."""
        routing = RoutingConfig(topic_template="{kind}")
        attrs_a = {"kind": "actor", "route_table": "customer", "sub_type": "customer"}
        attrs_b = {"kind": "actor", "route_table": "staff", "sub_type": "staff"}
        assert resolve_topic(routing, attrs_a) == resolve_topic(routing, attrs_b)


# ---------------------------------------------------------------------------
# Tests: enumerate_topics
# ---------------------------------------------------------------------------


class TestEnumerateTopics:
    """enumerate_topics returns deterministic, de-duplicated ordered topic set."""

    def test_selection_order_preserved(self) -> None:
        """Topics appear in selection order."""
        routing = RoutingConfig()
        selected = [
            {"kind": "actor", "route_table": "customer", "sub_type": "customer"},
            {"kind": "actor", "route_table": "staff", "sub_type": "staff"},
            {"kind": "device", "route_table": "device"},
        ]
        topics = enumerate_topics(routing, selected)
        assert topics == ("customer", "staff", "device")

    def test_group_target_added_after_selection_order(self) -> None:
        """Declared group targets not already present appear after selection topics."""
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        selected = [
            {"kind": "actor", "route_table": "customer", "sub_type": "customer"},
            {"kind": "actor", "route_table": "staff", "sub_type": "staff"},
        ]
        topics = enumerate_topics(routing, selected)
        # customer -> premium, staff stays; premium declared-but-empty is NOT added
        # because the group absorbs customer => premium is already in topics at customer position
        assert "premium" in topics
        assert "staff" in topics

    def test_group_target_coincides_with_rendered_name_stays_at_first_occurrence(
        self,
    ) -> None:
        """Group target coinciding with a rendered name stays at that earlier position."""
        # topic_template={route_table}, groups={staff: [customer]}
        # customer -> staff (remapped); staff sub-type -> staff too
        routing = RoutingConfig(groups={"staff": ["customer"]})
        selected = [
            {"kind": "actor", "route_table": "customer", "sub_type": "customer"},
            {"kind": "actor", "route_table": "staff", "sub_type": "staff"},
        ]
        topics = enumerate_topics(routing, selected)
        # customer renders to "staff"; staff renders to "staff" — deduplicated
        # "staff" appears once, group target "staff" not re-added
        assert topics == ("staff",)

    def test_declared_but_empty_group_target_included(self) -> None:
        """A group target with no matching rendered selection is still included."""
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        # Only staff selected — premium members not selected
        selected = [
            {"kind": "actor", "route_table": "staff", "sub_type": "staff"},
        ]
        topics = enumerate_topics(routing, selected)
        assert "staff" in topics
        assert "premium" in topics

    def test_first_occurrence_deduplication(self) -> None:
        """De-duplication keeps each topic's first occurrence."""
        routing = RoutingConfig(topic_template="{kind}")
        selected = [
            {"kind": "actor", "route_table": "customer", "sub_type": "customer"},
            {
                "kind": "actor",
                "route_table": "vip_customer",
                "sub_type": "vip_customer",
            },
        ]
        topics = enumerate_topics(routing, selected)
        assert topics == ("actor",)

    def test_empty_selection_only_group_targets(self) -> None:
        """Empty selection yields only group target topics."""
        routing = RoutingConfig(groups={"premium": ["customer"]})
        topics = enumerate_topics(routing, [])
        assert topics == ("premium",)

    def test_empty_selection_and_no_groups_returns_empty(self) -> None:
        """Empty selection with no groups returns empty tuple."""
        topics = enumerate_topics(_DEFAULT_ROUTING, [])
        assert topics == ()


# ---------------------------------------------------------------------------
# Tests: resolve_subtype_index
# ---------------------------------------------------------------------------


class TestResolveSubtypeIndex:
    """resolve_subtype_index over a built sub-typed emit."""

    def test_maps_record_id_to_discriminator(self, tmp_path: Path) -> None:
        """Every record_id maps to its discriminator value."""
        rows = [
            ("trunk", "r1", 1, True, None, 1, "customer"),
            ("trunk", "r2", 2, True, None, 2, "vip_customer"),
            ("trunk", "r3", 3, True, None, 3, "staff"),
        ]
        emit_dir = _build_subtyped_emit(tmp_path, "actor", _RECORD_COLS_WITH_TYPE, rows)
        with open_emit(emit_dir) as emit:
            index = resolve_subtype_index(emit, "actor")

        assert index == {"r1": "customer", "r2": "vip_customer", "r3": "staff"}

    def test_independent_of_selected_properties(self, tmp_path: Path) -> None:
        """The index reads the discriminator only, not selected properties."""
        # Same as above — the function doesn't take a properties parameter
        rows = [
            ("trunk", "r1", 1, True, None, 1, "staff"),
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
            ("trunk", "r1", 1, True, None, 1),
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
            ("trunk", "r1", 1, True, None, 1),
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
