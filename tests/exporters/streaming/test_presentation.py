"""Tests for presentation.py: output-name resolution and kind vocabulary.

Covers the two naming resolvers (resolve_stream_output_columns /
resolve_membership_output_columns) against `IdentityProjection` /
`OutputEntry`, the kind-vocabulary resolver (resolve_stream_kind_vocabulary),
the member-kind value mapping (apply_kind_vocabulary), and the
presentation-invariance / Debezium-schema-agreement properties end to end
through the engine and driver.

No absorption branch: `identity.published` always carries the elected
surface exactly once, so a `presentation_id`-elected stream publishes one
identity entry by construction — there is no special-cased duplicate-drop
to test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.config.models import (
    DebeziumSourceIdentity,
    KindStream,
    MembershipRef,
    MembershipStream,
    StreamConfig,
)
from fabulexa_forge.errors import (
    StreamKindLabelCollision,
    StreamKindLabelUnknown,
    StreamOutputNameCollision,
    StreamRenameUnresolvable,
)
from fabulexa_forge.exporters.streaming.driver import _build_value_schemas
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.presentation import (
    IdentityProjection,
    apply_kind_vocabulary,
    resolve_membership_output_columns,
    resolve_stream_envelope_kind,
    resolve_stream_kind_vocabulary,
    resolve_stream_output_columns,
)
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl, _membership_table_spec

# ---------------------------------------------------------------------------
# Fixture builders (0-row sidecars for direct-resolver unit tests)
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_RECORD_COLS_PID: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_MEMBERSHIP_SCALAR_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEMBERSHIP_REF_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__owner__kind", "type": "VARCHAR"},
    {"name": "member__owner__id", "type": "VARCHAR"},
]

_MEMBERSHIP_MULTI_SCALAR_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
    {"name": "elem__note", "type": "VARCHAR"},
]

_MEMBERSHIP_REF_AND_SCALAR_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__owner__kind", "type": "VARCHAR"},
    {"name": "member__owner__id", "type": "VARCHAR"},
    {"name": "elem__captain_kind", "type": "VARCHAR"},
]

#: The default identity projection every non-election-focused resolver test
#: uses: the byte-identical 'record_id' surface, published once.
_RECORD_ID_IDENTITY = IdentityProjection(elected="record_id", published=("record_id",))


def _table_spec(
    name: str, category: str, cols: list[dict[str, object]], rows: int, record_kind: str
) -> dict[str, object]:
    return {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
        "record_kind": record_kind,
    }


def _write_records_sidecar(
    tmp_path: Path,
    kinds: dict[str, list[dict[str, object]]],
) -> Path:
    """Write a minimal 0-row multi-kind records emit, for direct resolver tests."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    tables = []
    for kind, cols in kinds.items():
        conn.execute(_ddl(f"records__{kind}", cols))
        tables.append(_table_spec(f"records__{kind}", "records", cols, 0, kind))
    conn.close()
    _write_sidecar(tmp_path, tables=tables)
    return tmp_path


def _write_membership_sidecar(
    tmp_path: Path,
    owner_kind: str,
    property_name: str,
    cols: list[dict[str, object]],
) -> Path:
    """Write a minimal 0-row membership emit plus its owner records shell."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    table_name = f"membership__{owner_kind}__{property_name}"
    conn.execute(_ddl(table_name, cols))
    conn.execute(_ddl(f"records__{owner_kind}", _RECORD_COLS))
    conn.close()
    _write_sidecar(
        tmp_path,
        tables=[
            _membership_table_spec(table_name, cols, 0, owner_kind, property_name),
            _table_spec(
                f"records__{owner_kind}", "records", _RECORD_COLS, 0, owner_kind
            ),
        ],
    )
    return tmp_path


def _build_single_kind_emit(
    tmp_path: Path,
    kind: str,
    record_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]],
    record_cols: list[dict[str, object]] | None = None,
) -> Path:
    """Build a minimal populated single-kind emit, for engine-level tests."""
    cols = record_cols if record_cols is not None else _RECORD_COLS
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", cols))
    conn.execute(_ddl("history", _HISTORY_COLS))
    placeholders = ", ".join("?" for _ in cols)
    for row in record_rows:
        conn.execute(
            f'INSERT INTO "records__{kind}" VALUES ({placeholders})', list(row)
        )
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()
    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(f"records__{kind}", "records", cols, len(record_rows), kind),
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(history_rows),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _source_identity() -> DebeziumSourceIdentity:
    """A fixed Debezium masquerade source identity, shared by schema tests."""
    return DebeziumSourceIdentity(
        connector="postgresql", name="src", db="d", schema="s", version="1.0"
    )


# ---------------------------------------------------------------------------
# resolve_stream_output_columns
# ---------------------------------------------------------------------------


class TestResolveStreamOutputColumns:
    def test_order_identity_then_properties(self, tmp_path: Path) -> None:
        """Order: the published identity surface, then properties in sidecar order."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS_PID})
        with open_emit(emit_dir) as emit:
            entries = resolve_stream_output_columns(
                emit.sidecar, "item", ["status"], None, _RECORD_ID_IDENTITY
            )
        assert [(e.source_kind, e.source, e.output_key) for e in entries] == [
            ("identity", "record_id", "record_id"),
            ("payload", "prop__status", "status"),
        ]

    def test_bare_defaults_no_rename(self, tmp_path: Path) -> None:
        """Absent rename: every property's output key is its bare name."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        with open_emit(emit_dir) as emit:
            entries = resolve_stream_output_columns(
                emit.sidecar, "item", ["status", "label"], None, _RECORD_ID_IDENTITY
            )
        assert {e.output_key: e.source for e in entries} == {
            "record_id": "record_id",
            "status": "prop__status",
            "label": "prop__label",
        }

    def test_rename_target_applied(self, tmp_path: Path) -> None:
        """A rename entry retargets its property's output key; order unchanged."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        with open_emit(emit_dir) as emit:
            entries = resolve_stream_output_columns(
                emit.sidecar,
                "item",
                ["status", "label"],
                {"status": "state"},
                _RECORD_ID_IDENTITY,
            )
        assert [e.output_key for e in entries] == ["record_id", "state", "label"]

    def test_presentation_id_elected_published_once(self, tmp_path: Path) -> None:
        """A presentation_id-elected identity publishes exactly one identity
        entry — no absorption special case, just `published` carrying one
        surface."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS_PID})
        identity = IdentityProjection(
            elected="presentation_id", published=("presentation_id",)
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_stream_output_columns(
                emit.sidecar, "item", ["status"], None, identity
            )
        assert [(e.source_kind, e.output_key) for e in entries] == [
            ("identity", "presentation_id"),
            ("payload", "status"),
        ]

    def test_unelected_presentation_id_never_appears_in_payload(
        self, tmp_path: Path
    ) -> None:
        """Under a record_id / record_index election, the kind's own
        presentation_id column never surfaces as a payload entry, even
        though the fold's raw row still carries it."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS_PID})
        identity = IdentityProjection(
            elected="record_index", published=("record_index",)
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_stream_output_columns(
                emit.sidecar, "item", ["status"], None, identity
            )
        assert [e.output_key for e in entries] == ["record_index", "status"]

    def test_rename_addresses_elected_surface_output_key(self, tmp_path: Path) -> None:
        """`rename` may retarget the elected surface's own contract column
        name — the Phase 2 capability: a `record_index` election's `rename:
        {record_index: id}` renames the identity entry itself."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        identity = IdentityProjection(
            elected="record_index", published=("record_index",)
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_stream_output_columns(
                emit.sidecar, "item", ["status"], {"record_index": "id"}, identity
            )
        assert [(e.source_kind, e.output_key) for e in entries] == [
            ("identity", "id"),
            ("payload", "status"),
        ]

    def test_rename_key_not_selected_raises(self, tmp_path: Path) -> None:
        """A rename key naming no selected property and no published surface
        is unresolvable, with no published-set suffix (a plain typo)."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamRenameUnresolvable, match="'bogus'") as excinfo:
                resolve_stream_output_columns(
                    emit.sidecar,
                    "item",
                    ["status"],
                    {"bogus": "x"},
                    _RECORD_ID_IDENTITY,
                )
        assert "published" not in str(excinfo.value)

    def test_rename_unpublished_surface_name_raises_with_published_suffix(
        self, tmp_path: Path
    ) -> None:
        """A rename key naming a KeySurface this stream does not publish is
        unresolvable, and the message appends the published set."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS_PID})
        identity = IdentityProjection(
            elected="record_index", published=("record_index",)
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamRenameUnresolvable, match="published") as excinfo:
                resolve_stream_output_columns(
                    emit.sidecar,
                    "item",
                    ["status"],
                    {"presentation_id": "pid"},
                    identity,
                )
        assert "record_index" in str(excinfo.value)

    def test_two_rename_targets_collide(self, tmp_path: Path) -> None:
        """Two different properties renamed to the same target collide."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamOutputNameCollision):
                resolve_stream_output_columns(
                    emit.sidecar,
                    "item",
                    ["status", "label"],
                    {"status": "x", "label": "x"},
                    _RECORD_ID_IDENTITY,
                )

    def test_rename_target_vs_bare_default_collides(self, tmp_path: Path) -> None:
        """A rename target equal to another property's unrenamed bare name collides."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamOutputNameCollision):
                resolve_stream_output_columns(
                    emit.sidecar,
                    "item",
                    ["status", "label"],
                    {"status": "label"},
                    _RECORD_ID_IDENTITY,
                )

    def test_output_key_equals_identity_raises(self, tmp_path: Path) -> None:
        """A rename target equal to the identity entry's resolved key collides."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamOutputNameCollision):
                resolve_stream_output_columns(
                    emit.sidecar,
                    "item",
                    ["status"],
                    {"status": "record_id"},
                    _RECORD_ID_IDENTITY,
                )

    def test_reserved_name_follows_resolved_key_collision(self, tmp_path: Path) -> None:
        """`rename: {record_index: status}` under a record_index election
        collides with the selected 'status' property's bare output key —
        the reserved name follows the resolved key, not a fixed literal."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS})
        identity = IdentityProjection(
            elected="record_index", published=("record_index",)
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamOutputNameCollision):
                resolve_stream_output_columns(
                    emit.sidecar,
                    "item",
                    ["status"],
                    {"record_index": "status"},
                    identity,
                )

    def test_reserved_name_legal_when_surface_unpublished(self, tmp_path: Path) -> None:
        """`rename: {status: record_index}` is legal when `record_index` is
        not a published surface (a presentation_id election publishes only
        presentation_id)."""
        emit_dir = _write_records_sidecar(tmp_path, {"item": _RECORD_COLS_PID})
        identity = IdentityProjection(
            elected="presentation_id", published=("presentation_id",)
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_stream_output_columns(
                emit.sidecar,
                "item",
                ["status"],
                {"status": "record_index"},
                identity,
            )
        assert [(e.source_kind, e.output_key) for e in entries] == [
            ("identity", "presentation_id"),
            ("payload", "record_index"),
        ]


# ---------------------------------------------------------------------------
# resolve_membership_output_columns
# ---------------------------------------------------------------------------


class TestResolveMembershipOutputColumns:
    def test_order_owner_identity_then_scalar_field(self, tmp_path: Path) -> None:
        """Owner identity first, then selected fields, element-schema order."""
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_SCALAR_COLS
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_membership_output_columns(
                emit.sidecar,
                MembershipRef(kind="person", property="team"),
                ["priority"],
                None,
                _RECORD_ID_IDENTITY,
            )
        assert [(e.source_kind, e.output_key) for e in entries] == [
            ("identity", "record_id"),
            ("payload", "priority"),
        ]

    def test_owner_identity_rename_addresses_elected_surface(
        self, tmp_path: Path
    ) -> None:
        """The owner identity entry re-keys the same way a kind stream's
        does: `rename` may retarget the elected owner surface's own
        contract column name."""
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_SCALAR_COLS
        )
        identity = IdentityProjection(
            elected="record_index", published=("record_index",)
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_membership_output_columns(
                emit.sidecar,
                MembershipRef(kind="person", property="team"),
                ["priority"],
                {"record_index": "id"},
                identity,
            )
        assert [(e.source_kind, e.output_key) for e in entries] == [
            ("identity", "id"),
            ("payload", "priority"),
        ]

    def test_reference_field_renamed_in_place(self, tmp_path: Path) -> None:
        """A reference field's rename retargets both halves of its pair."""
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_REF_COLS
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_membership_output_columns(
                emit.sidecar,
                MembershipRef(kind="person", property="team"),
                ["owner"],
                {"owner": "captain"},
                _RECORD_ID_IDENTITY,
            )
        assert [e.output_key for e in entries] == [
            "record_id",
            "captain_kind",
            "captain_id",
        ]

    def test_order_is_element_schema_order_not_config_fields_order(
        self, tmp_path: Path
    ) -> None:
        """Output order follows element-schema declaration order, not the
        config `fields` list's order."""
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_MULTI_SCALAR_COLS
        )
        with open_emit(emit_dir) as emit:
            entries = resolve_membership_output_columns(
                emit.sidecar,
                MembershipRef(kind="person", property="team"),
                ["note", "priority"],
                None,
                _RECORD_ID_IDENTITY,
            )
        assert [e.output_key for e in entries] == ["record_id", "priority", "note"]

    def test_rename_key_not_selected_raises(self, tmp_path: Path) -> None:
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_SCALAR_COLS
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamRenameUnresolvable):
                resolve_membership_output_columns(
                    emit.sidecar,
                    MembershipRef(kind="person", property="team"),
                    ["priority"],
                    {"bogus": "x"},
                    _RECORD_ID_IDENTITY,
                )

    def test_renamed_reference_pair_member_collides_with_other_output(
        self, tmp_path: Path
    ) -> None:
        """A rename target's expanded `<f>_kind` half collides with another
        selected field's own (unrenamed) output key."""
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_REF_AND_SCALAR_COLS
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamOutputNameCollision):
                resolve_membership_output_columns(
                    emit.sidecar,
                    MembershipRef(kind="person", property="team"),
                    ["owner", "captain_kind"],
                    {"owner": "captain"},
                    _RECORD_ID_IDENTITY,
                )

    def test_output_key_equals_event_raises(self, tmp_path: Path) -> None:
        """A field renamed to the reserved membership 'event' name collides."""
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_SCALAR_COLS
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamOutputNameCollision):
                resolve_membership_output_columns(
                    emit.sidecar,
                    MembershipRef(kind="person", property="team"),
                    ["priority"],
                    {"priority": "event"},
                    _RECORD_ID_IDENTITY,
                )

    def test_output_key_equals_owner_identity_raises(self, tmp_path: Path) -> None:
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_SCALAR_COLS
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(StreamOutputNameCollision):
                resolve_membership_output_columns(
                    emit.sidecar,
                    MembershipRef(kind="person", property="team"),
                    ["priority"],
                    {"priority": "record_id"},
                    _RECORD_ID_IDENTITY,
                )


# ---------------------------------------------------------------------------
# resolve_stream_kind_vocabulary / apply_kind_vocabulary / resolve_stream_envelope_kind
# ---------------------------------------------------------------------------


class TestResolveStreamKindVocabulary:
    def test_precedence_kind_label_over_kind_labels_over_verbatim(
        self, tmp_path: Path
    ) -> None:
        emit_dir = _write_records_sidecar(
            tmp_path, {"person": _RECORD_COLS, "team": _RECORD_COLS}
        )
        with open_emit(emit_dir) as emit:
            config = StreamConfig(
                content="state-changes",
                kind_labels={"person": "Person"},
                streams=[
                    KindStream(
                        name="s1", kind="person", properties=[], kind_label="Human"
                    ),
                    KindStream(name="s2", kind="person", properties=[]),
                    KindStream(name="s3", kind="team", properties=[]),
                ],
            )
            vocabulary = resolve_stream_kind_vocabulary(config, emit.sidecar)
            assert (
                resolve_stream_envelope_kind("Human", vocabulary, "person") == "Human"
            )
            assert resolve_stream_envelope_kind(None, vocabulary, "person") == "Person"
            assert resolve_stream_envelope_kind(None, vocabulary, "team") == "team"

    def test_member_kind_value_identity_fallthrough(self) -> None:
        vocabulary = {"person": "Person"}
        assert apply_kind_vocabulary("person", vocabulary) == "Person"
        assert apply_kind_vocabulary("team", vocabulary) == "team"
        assert apply_kind_vocabulary(None, vocabulary) is None

    def test_byte_identical_passthrough_no_labels(self) -> None:
        assert apply_kind_vocabulary("person", {}) == "person"
        assert resolve_stream_envelope_kind(None, {}, "person") == "person"

    def test_kind_labels_unknown_kind_raises(self, tmp_path: Path) -> None:
        emit_dir = _write_records_sidecar(tmp_path, {"person": _RECORD_COLS})
        with open_emit(emit_dir) as emit:
            config = StreamConfig(
                content="state-changes",
                kind_labels={"ghost": "Ghost"},
                streams=[KindStream(name="s1", kind="person", properties=[])],
            )
            with pytest.raises(StreamKindLabelUnknown, match="'ghost'"):
                resolve_stream_kind_vocabulary(config, emit.sidecar)

    def test_label_equals_unlabeled_kind_name_raises(self, tmp_path: Path) -> None:
        emit_dir = _write_records_sidecar(
            tmp_path, {"person": _RECORD_COLS, "team": _RECORD_COLS}
        )
        with open_emit(emit_dir) as emit:
            config = StreamConfig(
                content="state-changes",
                kind_labels={"person": "team"},
                streams=[KindStream(name="s1", kind="person", properties=[])],
            )
            with pytest.raises(StreamKindLabelCollision):
                resolve_stream_kind_vocabulary(config, emit.sidecar)

    def test_per_stream_kind_label_masquerade_raises(self, tmp_path: Path) -> None:
        """A per-stream kind_label equal to a different kind's rendered name refuses."""
        emit_dir = _write_records_sidecar(
            tmp_path, {"person": _RECORD_COLS, "team": _RECORD_COLS}
        )
        with open_emit(emit_dir) as emit:
            config = StreamConfig(
                content="state-changes",
                streams=[
                    KindStream(
                        name="s1", kind="person", properties=[], kind_label="team"
                    )
                ],
            )
            with pytest.raises(StreamKindLabelCollision, match="s1"):
                resolve_stream_kind_vocabulary(config, emit.sidecar)

    def test_per_stream_kind_label_masquerade_raises_for_membership_stream(
        self, tmp_path: Path
    ) -> None:
        """A membership stream's per-stream kind_label equal to a different
        kind's rendered name refuses (the membership branch of
        `_stream_subject_kind`)."""
        emit_dir = _write_records_sidecar(
            tmp_path, {"person": _RECORD_COLS, "team": _RECORD_COLS}
        )
        with open_emit(emit_dir) as emit:
            config = StreamConfig(
                content="membership-events",
                streams=[
                    MembershipStream(
                        name="s1",
                        membership=MembershipRef(kind="person", property="team"),
                        fields=[],
                        kind_label="team",
                    )
                ],
            )
            with pytest.raises(StreamKindLabelCollision, match="s1"):
                resolve_stream_kind_vocabulary(config, emit.sidecar)

    def test_two_streams_sharing_one_kind_label_legal(self, tmp_path: Path) -> None:
        emit_dir = _write_records_sidecar(
            tmp_path, {"person": _RECORD_COLS, "team": _RECORD_COLS}
        )
        with open_emit(emit_dir) as emit:
            config = StreamConfig(
                content="state-changes",
                streams=[
                    KindStream(
                        name="s1", kind="person", properties=[], kind_label="Feed"
                    ),
                    KindStream(
                        name="s2", kind="team", properties=[], kind_label="Feed"
                    ),
                ],
            )
            # Legal: no exception.
            resolve_stream_kind_vocabulary(config, emit.sidecar)


# ---------------------------------------------------------------------------
# Presentation invariance (engine-level)
# ---------------------------------------------------------------------------


class TestPresentationInvariance:
    def test_rename_and_labels_change_only_payload_strings(
        self, tmp_path: Path
    ) -> None:
        """rename + kind_labels + kind_label change only key/value strings."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[("trunk", "item", "r1", "status", 20, "b")],
        )
        base_config = StreamConfig(
            content="state-changes",
            streams=[KindStream(name="item", kind="item", properties=["status"])],
        )
        labeled_config = StreamConfig(
            content="state-changes",
            kind_labels={"item": "Item"},
            streams=[
                KindStream(
                    name="item",
                    kind="item",
                    properties=["status"],
                    rename={"status": "state"},
                    kind_label="Widget",
                )
            ],
        )
        with open_emit(emit_dir) as emit:
            base_events = list(
                iter_stream_events(emit, base_config, None, discard_notice_sink)
            )
        with open_emit(emit_dir) as emit:
            labeled_events = list(
                iter_stream_events(emit, labeled_config, None, discard_notice_sink)
            )

        assert len(base_events) == len(labeled_events)
        for base, labeled in zip(base_events, labeled_events):
            assert base.seq == labeled.seq
            assert base.ts == labeled.ts
            assert base.key_value == labeled.key_value
            assert base.topic == labeled.topic
            assert base.op == labeled.op
            assert labeled.kind == "Widget"
            assert base.kind == "item"
            if base.after is not None:
                assert labeled.after is not None
                assert base.after["status"] == labeled.after["state"]
                assert "state" not in base.after
                assert "status" not in labeled.after


# ---------------------------------------------------------------------------
# Debezium: value-schema field list equals the resolver's output keys
# ---------------------------------------------------------------------------


class TestDebeziumSchemaMatchesResolver:
    def test_value_schema_fields_equal_resolver_output_keys(
        self, tmp_path: Path
    ) -> None:
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        config = StreamConfig(
            content="state-changes",
            streams=[
                KindStream(
                    name="item",
                    kind="item",
                    properties=["status", "label"],
                    rename={"status": "state"},
                )
            ],
        )
        source_identity = _source_identity()
        with open_emit(emit_dir) as emit:
            schemas = _build_value_schemas(emit, config, source_identity, "topic")
            expected = [
                entry.output_key
                for entry in resolve_stream_output_columns(
                    emit.sidecar,
                    "item",
                    ["status", "label"],
                    {"status": "state"},
                    _RECORD_ID_IDENTITY,
                )
            ]

        schema = schemas["item"]
        after_struct = schema["fields"][1]
        field_names = [f["field"] for f in after_struct["fields"]]
        assert field_names == expected

    def test_membership_value_schema_fields_equal_event_plus_resolver_output_keys(
        self, tmp_path: Path
    ) -> None:
        """A membership stream's value schema is ['event', *resolver output keys]."""
        emit_dir = _write_membership_sidecar(
            tmp_path, "person", "team", _MEMBERSHIP_SCALAR_COLS
        )
        membership = MembershipRef(kind="person", property="team")
        config = StreamConfig(
            content="membership-events",
            streams=[
                MembershipStream(
                    name="team",
                    membership=membership,
                    fields=["priority"],
                    rename={"priority": "prio"},
                )
            ],
        )
        source_identity = _source_identity()
        with open_emit(emit_dir) as emit:
            schemas = _build_value_schemas(emit, config, source_identity, "topic")
            expected = ["event"] + [
                entry.output_key
                for entry in resolve_membership_output_columns(
                    emit.sidecar,
                    membership,
                    ["priority"],
                    {"priority": "prio"},
                    _RECORD_ID_IDENTITY,
                )
            ]

        schema = schemas["team"]
        after_struct = schema["fields"][1]
        field_names = [f["field"] for f in after_struct["fields"]]
        assert field_names == expected


# ---------------------------------------------------------------------------
# kind_labels leave routing (route_table / source.table / schema names) untouched
# ---------------------------------------------------------------------------


class TestKindLabelsDoNotAffectRouting:
    def test_route_table_and_schema_names_unaffected_by_kind_labels(
        self, tmp_path: Path
    ) -> None:
        """kind_labels change only presentation values; route_table, the
        Debezium source.table schema key, and schema names are untouched."""
        emit_dir = _build_single_kind_emit(
            tmp_path,
            "item",
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "x")],
            history_rows=[],
        )
        base_config = StreamConfig(
            content="state-changes",
            streams=[KindStream(name="item", kind="item", properties=["status"])],
        )
        labeled_config = StreamConfig(
            content="state-changes",
            kind_labels={"item": "Item"},
            streams=[
                KindStream(
                    name="item",
                    kind="item",
                    properties=["status"],
                    kind_label="Widget",
                )
            ],
        )
        source_identity = _source_identity()
        with open_emit(emit_dir) as emit:
            base_schemas = _build_value_schemas(
                emit, base_config, source_identity, "source_table"
            )
            labeled_schemas = _build_value_schemas(
                emit, labeled_config, source_identity, "source_table"
            )
        assert set(base_schemas) == set(labeled_schemas)
        assert base_schemas["item"]["name"] == labeled_schemas["item"]["name"]

        with open_emit(emit_dir) as emit:
            base_events = list(
                iter_stream_events(emit, base_config, None, discard_notice_sink)
            )
        with open_emit(emit_dir) as emit:
            labeled_events = list(
                iter_stream_events(emit, labeled_config, None, discard_notice_sink)
            )
        assert [e.route_table for e in base_events] == [
            e.route_table for e in labeled_events
        ]
        assert labeled_events[0].kind == "Widget"
