"""Tests for row selection: `where` (both stream shapes) and membership owner
`sub_types`, over the promoted mode-neutral selection spine
(`exporters.selection_spine`, `exporters.streaming.selection`).

Materialized against one shared `gizmo` (sub-typed: red/blue) emit, owning a
`gizmo.assignment` membership table, exercised through `iter_stream_events` /
`stream_export`. Covers the gate matrix (`StreamWhere*`), kind-stream and
membership-stream selection behavior, the declared-but-empty topic, the
out-of-domain-value notice, and the addressed-owner-set uniformity
granularity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.config.models import KindStream, MembershipStream, StreamConfig
from fabulexa_forge.errors import (
    ElectionMixedIdentity,
    StreamWhereColumnUnresolved,
    StreamWhereNotConstant,
    StreamWhereOnDiscriminator,
    StreamWhereValueUncastable,
)
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl, _membership_table_spec

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_GIZMO_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__gizmo_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__legacy", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
    prop_column(
        "prop__priority", "BIGINT", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__site", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__depot_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="depot",
    ),
    identity_column("ref_index__depot_id", "BIGINT"),
]

_ASSIGNMENT_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__region", "type": "VARCHAR"},
    {"name": "elem__shift", "type": "VARCHAR"},
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# record_rows tuple order (15 values; ref_index__depot_id is a literal NULL):
# fork_path, record_id, presentation_id, created_sim_time, active,
# deactivated_at, last_mutation_sim_time, record_index, gizmo_type, region,
# status, legacy, priority, site, depot_id
#
#   r1: red,  emea, dep-1, presentation_id="R_001" — the plain accepted case.
#   r2: blue, apac, dep-2 — never elects presentation_id in the mixed-election
#       fixture (targets record_index instead).
#   r3: red,  apac, dep-1 — a closed membership interval (join + leave).
#   r4: blue, emea, dep-3 — one 'u' (status a0 -> b at t=200) then a 'd' at
#       t=500, for the "non-satisfying record excludes c/u/d" test.
#   r5: red,  region=NULL, depot_id=NULL — the "NULL never satisfies" case.
#: Every row carries a distinct, non-NULL presentation_id — the whole-table
#: elected-key uniqueness guard (`check_elected_key_unique`) ranges over the
#: full kind relation whenever any stream's addressed set elects
#: presentation_id, independent of which sub_types that stream itself scopes.
_DEFAULT_RECORD_ROWS: list[tuple[Any, ...]] = [
    (
        "trunk",
        "r1",
        "R_001",
        0,
        True,
        None,
        0,
        0,
        "red",
        "emea",
        "a",
        "x",
        10,
        "hq",
        "dep-1",
    ),
    (
        "trunk",
        "r2",
        "R_002",
        0,
        True,
        None,
        0,
        1,
        "blue",
        "apac",
        "b",
        "y",
        20,
        "hq",
        "dep-2",
    ),
    (
        "trunk",
        "r3",
        "R_003",
        0,
        True,
        None,
        0,
        2,
        "red",
        "apac",
        "a",
        "x",
        30,
        "remote",
        "dep-1",
    ),
    (
        "trunk",
        "r4",
        "R_004",
        0,
        False,
        500,
        200,
        3,
        "blue",
        "emea",
        "a0",
        "y",
        40,
        "hq",
        "dep-3",
    ),
    (
        "trunk",
        "r5",
        "R_005",
        0,
        True,
        None,
        0,
        4,
        "red",
        None,
        "a",
        "x",
        50,
        "hq",
        None,
    ),
]

_DEFAULT_HISTORY_ROWS: list[tuple[Any, ...]] = [
    ("trunk", "gizmo", "r4", "status", 200, "b"),
]

# membership_rows tuple order: fork_path, record_id, joined_sim_time,
# left_sim_time, elem__region, elem__shift. Every elem__region is a
# deliberate "SHADOW*" value distinct from its owner's prop__region, so a
# `where: {region: ...}` on the membership stream can only be satisfied by
# resolving the key against the owner property, never the element field.
_DEFAULT_MEMBERSHIP_ROWS: list[tuple[Any, ...]] = [
    ("trunk", "r1", 0, None, "SHADOW1", "morning"),
    ("trunk", "r2", 0, None, "SHADOW2", "night"),
    ("trunk", "r3", 0, 300, "SHADOW3", "day"),
]

_ENUM_DOMAINS: dict[str, object] = {
    "gizmo": {
        "gizmo_type": ["red", "blue"],
        "region": ["emea", "apac"],
        "priority": ["10", "20", "30", "40"],
    }
}

#: A mixed-election registry: `red` elects presentation_id (registry-eligible
#: via this entry), `blue` elects record_index (no registry entry needed).
#: Used only by TestUniformityGranularity.
_GIZMO_MIXED_REGISTRY: dict[str, object] = {
    "gizmo": {
        "sub_types": {
            "red": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "R_", "width": 3},
            }
        },
        # A single declared sub_type's rollup equals that sub_type's own
        # claim (no union ambiguity with one member) — the two-subtype
        # union-unsafe registries omit this (see CREATURE_UNSAFE_REGISTRY).
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}


# ---------------------------------------------------------------------------
# Emit builder helpers
# ---------------------------------------------------------------------------


def _table_spec(
    name: str, category: str, cols: list[dict[str, object]], rows: int
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if category == "records":
        spec["record_kind"] = "gizmo"
    return spec


def _build_gizmo_emit(
    tmp_path: Path,
    record_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]],
    membership_rows: list[tuple[Any, ...]],
    presentation_keys: dict[str, object] | None = None,
) -> Path:
    """Build a sub-typed `gizmo` emit (records + assignment membership)."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__gizmo", _GIZMO_COLS))
    for row in record_rows:
        conn.execute(
            'INSERT INTO "records__gizmo" VALUES'
            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            list(row),
        )

    conn.execute(_ddl("history", _HISTORY_COLS))
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))

    conn.execute(_ddl("membership__gizmo__assignment", _ASSIGNMENT_COLS))
    for row in membership_rows:
        conn.execute(
            'INSERT INTO "membership__gizmo__assignment" VALUES (?, ?, ?, ?, ?, ?)',
            list(row),
        )
    conn.close()

    extra: dict[str, object] = {"enum_domains": _ENUM_DOMAINS}
    if presentation_keys is not None:
        extra["presentation_keys"] = presentation_keys

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec("records__gizmo", "records", _GIZMO_COLS, len(record_rows)),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
            _membership_table_spec(
                "membership__gizmo__assignment",
                _ASSIGNMENT_COLS,
                len(membership_rows),
                "gizmo",
                "assignment",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        extra=extra,
    )
    return tmp_path


def _build_default_gizmo_emit(
    tmp_path: Path, presentation_keys: dict[str, object] | None = None
) -> Path:
    """Build the shared default-rows gizmo emit (see the row tables above)."""
    return _build_gizmo_emit(
        tmp_path,
        _DEFAULT_RECORD_ROWS,
        _DEFAULT_HISTORY_ROWS,
        _DEFAULT_MEMBERSHIP_ROWS,
        presentation_keys=presentation_keys,
    )


# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------


def _kind_stream(
    name: str,
    properties: list[str],
    sub_types: list[str] | None = None,
    where: dict[str, object] | None = None,
) -> KindStream:
    """Build one KindStream over kind 'gizmo'."""
    return KindStream(
        name=name,
        kind="gizmo",
        properties=properties,
        sub_types=sub_types,
        where=where,
    )


def _state_changes_config(
    streams: list[KindStream], keys: dict[str, object] | None = None
) -> StreamConfig:
    return StreamConfig(content="state-changes", streams=streams, keys=keys)


def _membership_stream(
    name: str,
    fields: list[str],
    sub_types: list[str] | None = None,
    where: dict[str, object] | None = None,
) -> MembershipStream:
    """Build one MembershipStream over gizmo's 'assignment' membership table."""
    return MembershipStream(
        name=name,
        membership={"kind": "gizmo", "property": "assignment"},
        fields=fields,
        sub_types=sub_types,
        where=where,
    )


def _membership_events_config(
    streams: list[MembershipStream], keys: dict[str, object] | None = None
) -> StreamConfig:
    return StreamConfig(content="membership-events", streams=streams, keys=keys)


def _record_ids(events: list[StreamEvent]) -> frozenset[str]:
    """The distinct record_ids carried by a list of StreamEvents."""
    return frozenset(e.record_id for e in events)


def _iter_events(emit_dir: Path, config: StreamConfig) -> list[StreamEvent]:
    with open_emit(emit_dir) as emit:
        return list(iter_stream_events(emit, config, None, discard_notice_sink))


# ---------------------------------------------------------------------------
# Gate matrix
# ---------------------------------------------------------------------------


class TestGateMatrix:
    """The `where` constant-column gate (design doc § Row selection table),
    each refusal message leading with `stream '{name}'`."""

    def test_constant_class_key_accepted(self, tmp_path: Path) -> None:
        """A constant-class payload property resolves without error."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("region_stream", ["region"], where={"region": "emea"})]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r1", "r4"}

    def test_tracked_property_refused(self, tmp_path: Path) -> None:
        """A tracked-class `where` key raises StreamWhereNotConstant."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("tracked_stream", [], where={"status": "a"})]
        )
        with pytest.raises(StreamWhereNotConstant, match="stream 'tracked_stream'"):
            _iter_events(emit_dir, config)

    def test_slice_only_property_refused(self, tmp_path: Path) -> None:
        """A non-discriminator slice_only `where` key raises
        StreamWhereNotConstant."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("legacy_stream", [], where={"legacy": "x"})]
        )
        with pytest.raises(StreamWhereNotConstant, match="stream 'legacy_stream'"):
            _iter_events(emit_dir, config)

    def test_discriminator_refused_pointing_at_sub_types(self, tmp_path: Path) -> None:
        """A `where` key naming the discriminator raises
        StreamWhereOnDiscriminator, pointing at sub_types."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("type_stream", [], where={"gizmo_type": "red"})]
        )
        with pytest.raises(StreamWhereOnDiscriminator, match="use sub_types"):
            _iter_events(emit_dir, config)

    def test_structural_column_unresolvable(self, tmp_path: Path) -> None:
        """A structural (non-prop__) column name raises
        StreamWhereColumnUnresolved."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("structural_stream", [], where={"created_sim_time": "0"})]
        )
        with pytest.raises(StreamWhereColumnUnresolved):
            _iter_events(emit_dir, config)

    def test_unknown_name_unresolvable(self, tmp_path: Path) -> None:
        """An unknown column name raises StreamWhereColumnUnresolved."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("bogus_stream", [], where={"bogus": "x"})]
        )
        with pytest.raises(StreamWhereColumnUnresolved):
            _iter_events(emit_dir, config)

    def test_membership_element_field_unresolvable(self, tmp_path: Path) -> None:
        """A `where` key naming a membership element field (not an owner
        property) raises StreamWhereColumnUnresolved."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _membership_events_config(
            [_membership_stream("shift_stream", ["shift"], where={"shift": "morning"})]
        )
        with pytest.raises(StreamWhereColumnUnresolved, match="stream 'shift_stream'"):
            _iter_events(emit_dir, config)

    def test_uncastable_value_refused_before_any_fold(self, tmp_path: Path) -> None:
        """An uncastable `where` value raises StreamWhereValueUncastable,
        before any fold materializes (a discarding notice sink still
        receives nothing)."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("priority_stream", [], where={"priority": "abc"})]
        )
        sink = RecordingNoticeSink()
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                StreamWhereValueUncastable, match="stream 'priority_stream'"
            ):
                list(iter_stream_events(emit, config, None, sink))
        assert sink.notices == []


# ---------------------------------------------------------------------------
# Kind stream `where`
# ---------------------------------------------------------------------------


class TestKindStreamWhere:
    """`where` on a KindStream: non-satisfying records are excluded whole,
    orthogonally to projection and to sub_types."""

    def test_non_satisfying_record_excludes_c_u_d_and_seq_stays_dense(
        self, tmp_path: Path
    ) -> None:
        """r4 (region=emea) has a 'c', a 'u' and a 'd'; excluding it via
        `where: {region: apac}` drops all three, and seq stays 1-based and
        gap-free over the survivors."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("apac_stream", ["status"], where={"region": "apac"})]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r2", "r3"}
        assert [e.seq for e in events] == list(range(1, len(events) + 1))

    def test_where_and_composes_with_sub_types(self, tmp_path: Path) -> None:
        """`where` narrows within the sub_types-scoped population."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [
                _kind_stream(
                    "red_emea",
                    [],
                    sub_types=["red"],
                    where={"region": "emea"},
                )
            ]
        )
        events = _iter_events(emit_dir, config)
        # r3 is red but apac (excluded by where); r5 is red but region NULL.
        assert _record_ids(events) == {"r1"}

    def test_predicated_property_need_not_be_projected(self, tmp_path: Path) -> None:
        """`where` reads the subject relation, not the projection — `region`
        selects rows even when only `status` is projected, and the
        after-image never carries `region`."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("status_only", ["status"], where={"region": "emea"})]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r1", "r4"}
        for event in events:
            if event.after is not None:  # a 'd' event carries no after-image
                assert "region" not in event.after

    def test_reference_valued_constant_property_compared_over_base_ids(
        self, tmp_path: Path
    ) -> None:
        """A `where` key on a reference-valued constant property compares
        over base-layer record ids (r1 and r3 both carry depot_id='dep-1')."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("depot_stream", [], where={"depot_id": "dep-1"})]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r1", "r3"}

    def test_null_never_satisfies(self, tmp_path: Path) -> None:
        """r5's region is NULL; it is selected by neither branch of a
        `where` on `region`."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        emea_events = _iter_events(
            emit_dir,
            _state_changes_config([_kind_stream("emea", [], where={"region": "emea"})]),
        )
        apac_events = _iter_events(
            emit_dir,
            _state_changes_config([_kind_stream("apac", [], where={"region": "apac"})]),
        )
        assert "r5" not in _record_ids(emea_events)
        assert "r5" not in _record_ids(apac_events)

    def test_overlapping_streams_select_independently(self, tmp_path: Path) -> None:
        """Two streams over the same kind with different `where` each scope
        their own feed."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [
                _kind_stream("emea_feed", [], where={"region": "emea"}),
                _kind_stream("apac_feed", [], where={"region": "apac"}),
            ]
        )
        events = _iter_events(emit_dir, config)
        by_topic: dict[str, frozenset[str]] = {
            name: _record_ids([e for e in events if e.topic == name])
            for name in ("emea_feed", "apac_feed")
        }
        assert by_topic["emea_feed"] == {"r1", "r4"}
        assert by_topic["apac_feed"] == {"r2", "r3"}


# ---------------------------------------------------------------------------
# Membership stream owner sub_types + where
# ---------------------------------------------------------------------------


class TestMembershipStreamWhere:
    """Owner `sub_types` and `where` resolve together through the parent-
    lookup spine — either alone, or AND-composed."""

    def test_sub_types_alone(self, tmp_path: Path) -> None:
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _membership_events_config(
            [_membership_stream("red_ward", [], sub_types=["red"])]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r1", "r3"}

    def test_sub_types_covering_full_domain_is_a_no_op(self, tmp_path: Path) -> None:
        """`sub_types` equal to the owner kind's full declared domain (red +
        blue) composes no discriminator filter — the same addressed set as
        declaring no `sub_types` at all (the selection spine's redundant-
        filter no-op path)."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        full_domain = _membership_events_config(
            [_membership_stream("full_ward", [], sub_types=["red", "blue"])]
        )
        unrestricted = _membership_events_config([_membership_stream("full_ward", [])])
        assert _record_ids(_iter_events(emit_dir, full_domain)) == _record_ids(
            _iter_events(emit_dir, unrestricted)
        )

    def test_where_alone(self, tmp_path: Path) -> None:
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _membership_events_config(
            [_membership_stream("emea_ward", [], where={"region": "emea"})]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r1"}

    def test_sub_types_and_where_and_composed(self, tmp_path: Path) -> None:
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _membership_events_config(
            [
                _membership_stream(
                    "red_emea_ward", [], sub_types=["red"], where={"region": "emea"}
                )
            ]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r1"}

    def test_non_satisfying_owner_join_and_leave_both_excluded(
        self, tmp_path: Path
    ) -> None:
        """r3's interval is closed (join + leave); excluding r3 via `where`
        drops both events, not just one."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _membership_events_config(
            [_membership_stream("emea_ward", [], where={"region": "emea"})]
        )
        events = _iter_events(emit_dir, config)
        assert "r3" not in _record_ids(events)
        assert [e.op for e in events if e.record_id == "r1"] == ["join"]

    def test_owner_property_shadowing_element_field_resolves_to_owner(
        self, tmp_path: Path
    ) -> None:
        """`where: {region: emea}` selects by the OWNER's prop__region
        (r1's real region is 'emea'); the after-image's projected `region`
        field is r1's elem__region value ('SHADOW1'), distinct from the
        selecting value — proving selection reads the owner, projection
        reads the element."""
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _membership_events_config(
            [_membership_stream("shadow_ward", ["region"], where={"region": "emea"})]
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"r1"}
        (event,) = events
        assert event.after is not None
        assert event.after["region"] == "SHADOW1"


# ---------------------------------------------------------------------------
# Zero-match selection
# ---------------------------------------------------------------------------


class TestZeroMatchSelection:
    """A `where` matching zero rows still declares its topic — present and
    empty, exit 0."""

    def test_declared_but_empty_topic_at_exit_zero(self, tmp_path: Path) -> None:
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [_kind_stream("no_site", [], where={"site": "nowhere"})]
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        sink = RecordingNoticeSink()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(emit, config, "jsonl", "file", out_dir, None, sink)
        assert outcome.events_per_topic["no_site"] == 0
        assert (out_dir / "no_site.jsonl").read_text() == ""
        # 'site' carries no enum_domains entry — no out-of-domain notice.
        assert sink.notices == []


# ---------------------------------------------------------------------------
# Out-of-domain where values
# ---------------------------------------------------------------------------


class TestOutOfDomainNotices:
    """An out-of-domain `where` element draws a notice, never an error, with
    the shipped two-case wording."""

    def test_two_case_wording_never_an_error(self, tmp_path: Path) -> None:
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [
                _kind_stream(
                    "wholly", [], where={"region": "namer"}
                ),  # every element unobserved
                _kind_stream(
                    "partial", [], where={"region": ["emea", "namer"]}
                ),  # 'emea' observed, 'namer' not
            ]
        )
        sink = RecordingNoticeSink()
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None, sink))

        assert all(n.code == "discriminator-value-unobserved" for n in sink.notices)
        wholly_msg = next(
            n.message for n in sink.notices if "stream 'wholly'" in n.message
        )
        partial_msg = next(
            n.message for n in sink.notices if "stream 'partial'" in n.message
        )
        assert "the topic will be empty" in wholly_msg
        assert "it contributes no events" in partial_msg
        # 'partial' still carries its observed-value events ('emea' -> r1, r4).
        assert _record_ids([e for e in events if e.topic == "partial"]) == {
            "r1",
            "r4",
        }

    def test_deterministic_order_streams_then_keys_then_elements(
        self, tmp_path: Path
    ) -> None:
        emit_dir = _build_default_gizmo_emit(tmp_path)
        config = _state_changes_config(
            [
                _kind_stream(
                    "s_first",
                    [],
                    where={"region": ["namer_a", "namer_b"], "priority": ["999"]},
                ),
                _kind_stream("s_second", [], where={"region": ["emea", "namer_c"]}),
            ]
        )
        sink = RecordingNoticeSink()
        with open_emit(emit_dir) as emit:
            list(iter_stream_events(emit, config, None, sink))

        elements_in_order = [n.message for n in sink.notices]

        def _contains(*fragments: str) -> str:
            return next(
                m
                for m in elements_in_order
                if all(fragment in m for fragment in fragments)
            )

        expected_order = [
            _contains("s_first", "region", "namer_a"),
            _contains("s_first", "region", "namer_b"),
            _contains("s_first", "priority", "999"),
            _contains("s_second", "region", "namer_c"),
        ]
        assert elements_in_order == expected_order


# ---------------------------------------------------------------------------
# Uniformity granularity (membership owner sub_types)
# ---------------------------------------------------------------------------


class TestUniformityGranularity:
    """Owner `sub_types` narrows the addressed population set the key-
    uniformity gate ranges over; `where` never narrows that set."""

    def _mixed_keys(self) -> dict[str, object]:
        return {"gizmo": {"red": "presentation_id", "blue": "record_index"}}

    def test_mixed_election_whole_domain_refused(self, tmp_path: Path) -> None:
        """No sub_types = the full owner domain (red+blue); a mixed
        election over it raises ElectionMixedIdentity."""
        emit_dir = _build_default_gizmo_emit(
            tmp_path, presentation_keys=_GIZMO_MIXED_REGISTRY
        )
        config = _membership_events_config(
            [_membership_stream("all_wards", [])], keys=self._mixed_keys()
        )
        with pytest.raises(ElectionMixedIdentity, match="stream 'all_wards'"):
            _iter_events(emit_dir, config)

    def test_where_never_narrows_the_addressed_set(self, tmp_path: Path) -> None:
        """A `where` that (value-wise) would leave only 'red' rows does not
        shrink the addressed population set the gate ranges over — the
        whole-domain mixed election still fails."""
        emit_dir = _build_default_gizmo_emit(
            tmp_path, presentation_keys=_GIZMO_MIXED_REGISTRY
        )
        config = _membership_events_config(
            [_membership_stream("emea_wards", [], where={"region": "emea"})],
            keys=self._mixed_keys(),
        )
        with pytest.raises(ElectionMixedIdentity, match="stream 'emea_wards'"):
            _iter_events(emit_dir, config)

    def test_split_per_sub_type_is_legal(self, tmp_path: Path) -> None:
        """Splitting the same mixed election across two sub_types-scoped
        streams is legal — each addressed set is trivially uniform."""
        emit_dir = _build_default_gizmo_emit(
            tmp_path, presentation_keys=_GIZMO_MIXED_REGISTRY
        )
        config = _membership_events_config(
            [
                _membership_stream("red_wards", [], sub_types=["red"]),
                _membership_stream("blue_wards", [], sub_types=["blue"]),
            ],
            keys=self._mixed_keys(),
        )
        events = _iter_events(emit_dir, config)
        red_events = [e for e in events if e.topic == "red_wards"]
        blue_events = [e for e in events if e.topic == "blue_wards"]
        assert _record_ids(red_events) == {"r1", "r3"}
        assert _record_ids(blue_events) == {"r2"}
        assert all(e.key_column == "presentation_id" for e in red_events)
        assert all(e.key_column == "record_index" for e in blue_events)


# ---------------------------------------------------------------------------
# Flat (non-sub-typed) kind `where`
# ---------------------------------------------------------------------------

_WIDGET_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]


def _build_widget_emit(tmp_path: Path, record_rows: list[tuple[Any, ...]]) -> Path:
    """Build a minimal flat (non-sub-typed) `widget` emit — no `enum_domains`
    entry, so `sidecar.subtype_values('widget')` returns `()`."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__widget", _WIDGET_COLS))
    placeholders = ", ".join("?" for _ in _WIDGET_COLS)
    for row in record_rows:
        conn.execute(
            f'INSERT INTO "records__widget" VALUES ({placeholders})', list(row)
        )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLS,
                "rows": len(record_rows),
                "record_kind": "widget",
            }
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


class TestFlatKindWhere:
    """`where` on a flat (non-sub-typed) kind resolves against the kind's
    single unconditioned population (`_stream_populations`'s flat-kind
    branch — no discriminator column exists)."""

    def test_where_selects_without_a_discriminator(self, tmp_path: Path) -> None:
        emit_dir = _build_widget_emit(
            tmp_path,
            record_rows=[
                ("trunk", "w1", 0, True, None, 0, 0, "emea"),
                ("trunk", "w2", 0, True, None, 0, 1, "apac"),
            ],
        )
        config = StreamConfig(
            content="state-changes",
            streams=[
                KindStream(
                    name="emea_widgets",
                    kind="widget",
                    properties=[],
                    where={"region": "emea"},
                )
            ],
        )
        events = _iter_events(emit_dir, config)
        assert _record_ids(events) == {"w1"}
