"""Message-key election end-to-end tests: the engine's key-election render
sites (key map, tombstones, after-image identity re-key + absorption,
reference/member-field translation, static gates, the elected-key uniqueness
guard, ordering invariance, and the never-schema-wrapped key rule) exercised
through `iter_stream_events` / `stream_export` against one combined emit
(`_election_fixtures.build_election_emit`).

Absent `keys`, every render site is byte-identical to the pre-election
(phase 2) rendering — the additive contract this suite pins.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    DebeziumConfig,
    DebeziumSourceIdentity,
    KindStream,
    MembershipRef,
    MembershipStream,
    StreamConfig,
)
from fabulexa_forge.errors import (
    ElectedKeyDuplicate,
    ElectionKindUnknown,
    ElectionMixedIdentity,
    ElectionPresentationUndeclared,
    ElectionSubTypeUnknown,
    ElectionUnionUnsafe,
)
from fabulexa_forge.exporters.streaming.debezium import (
    build_debezium_value_schema,
    rebased_epoch_ms,
    render_debezium_message,
)
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.exporters.streaming.encoding import encode_pinned
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.reader.emit import open_emit

from ._election_fixtures import FULL_REGISTRY, build_election_emit
from ._helpers import make_anchor

# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------


def _kind_config(
    name: str,
    kind: str,
    properties: list[str],
    keys: dict[str, object] | None = None,
    debezium: DebeziumConfig | None = None,
) -> StreamConfig:
    """Build a single-KindStream content='state-changes' StreamConfig."""
    return StreamConfig(
        content="state-changes",
        streams=[KindStream(name=name, kind=kind, properties=properties)],
        keys=keys,
        debezium=debezium,
    )


def _membership_config(
    streams: list[MembershipStream],
    keys: dict[str, object] | None = None,
) -> StreamConfig:
    """Build a content='membership-events' StreamConfig."""
    return StreamConfig(content="membership-events", streams=streams, keys=keys)


def _events_by_op(events: list[StreamEvent], record_id: str) -> dict[str, StreamEvent]:
    """Index a record's events by op (assumes one event per op)."""
    return {e.op: e for e in events if e.record_id == record_id}


_DEBEZIUM_SOURCE = DebeziumSourceIdentity.model_validate(
    {
        "connector": "postgresql",
        "name": "fabulexa",
        "db": "fabulexa",
        "schema": "public",
        "version": "2.5.0.Final",
    }
)


# ---------------------------------------------------------------------------
# No keys -> byte-identical to the pre-election (phase 2) rendering
# ---------------------------------------------------------------------------


class TestDefaultByteIdentity:
    """Absent `keys`, every render site is the byte-identical default."""

    def test_key_column_is_record_id_and_key_value_is_record_id(
        self, tmp_path: Path
    ) -> None:
        """key_column == 'record_id' and key_value == record_id on every event."""
        emit_dir = build_election_emit(tmp_path)
        config = _kind_config("widgets", "widget", ["status"])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        assert len(events) == 4
        for event in events:
            assert event.key_column == "record_id"
            assert event.key_value == event.record_id

    def test_golden_jsonl_rendering_pinned(self, tmp_path: Path) -> None:
        """The rendered JSONL objects match the pre-election literal, verbatim."""
        emit_dir = build_election_emit(tmp_path)
        config = _kind_config("widgets", "widget", ["status"])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        by_op = _events_by_op(events, "w1")
        assert render_jsonl_object(by_op["c"]) == {
            "seq": 1,
            "op": "c",
            "ts": 0,
            "kind": "widget",
            "key": {"record_id": "w1"},
            "after": {
                "record_id": "w1",
                "presentation_id": "W_001",
                "prop__status": "new",
            },
        }
        assert render_jsonl_object(by_op["u"]) == {
            "seq": 3,
            "op": "u",
            "ts": 100,
            "kind": "widget",
            "key": {"record_id": "w1"},
            "after": {
                "record_id": "w1",
                "presentation_id": "W_001",
                "prop__status": "active",
            },
        }

        w2_delete = _events_by_op(events, "w2")["d"]
        assert render_jsonl_object(w2_delete) == {
            "seq": 4,
            "op": "d",
            "ts": 200,
            "kind": "widget",
            "key": {"record_id": "w2"},
            "after": None,
        }


# ---------------------------------------------------------------------------
# presentation_id election: key map, tombstone, after-image re-key + absorb
# ---------------------------------------------------------------------------


class TestPresentationIdElection:
    """`keys: {widget: presentation_id}` renders through every site."""

    def _events(self, tmp_path: Path) -> list[StreamEvent]:
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets", "widget", ["status"], keys={"widget": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            return list(iter_stream_events(emit, config, None))

    def test_key_map_is_presentation_id_on_every_op(self, tmp_path: Path) -> None:
        """Every op's key map is {"presentation_id": <elected value>}."""
        events = self._events(tmp_path)
        by_op = _events_by_op(events, "w1")
        assert render_jsonl_object(by_op["c"])["key"] == {"presentation_id": "W_001"}
        assert render_jsonl_object(by_op["u"])["key"] == {"presentation_id": "W_001"}
        w2_delete = _events_by_op(events, "w2")["d"]
        assert render_jsonl_object(w2_delete)["key"] == {"presentation_id": "W_002"}

    def test_tombstone_and_debezium_before_carry_the_same_one_entry(
        self, tmp_path: Path
    ) -> None:
        """The 'd' tombstone key map and the Debezium key-only before-image
        carry the identical single entry."""
        events = self._events(tmp_path)
        w2_delete = _events_by_op(events, "w2")["d"]

        jsonl_key = render_jsonl_object(w2_delete)["key"]
        anchor = make_anchor()
        ts_ms = rebased_epoch_ms(w2_delete.event_sim_time, anchor)
        envelope = render_debezium_message(
            w2_delete, ts_ms, _DEBEZIUM_SOURCE, "widget", None
        )

        assert jsonl_key == {"presentation_id": "W_002"}
        assert envelope["before"] == jsonl_key
        assert envelope["after"] is None

    def test_after_image_identity_entry_rekeyed(self, tmp_path: Path) -> None:
        """The after-image's leading record_id entry is renamed to
        'presentation_id' carrying the record's own elected value."""
        events = self._events(tmp_path)
        create = _events_by_op(events, "w1")["c"]
        assert create.after is not None
        assert list(create.after.keys())[0] == "presentation_id"
        assert create.after["presentation_id"] == "W_001"

    def test_standalone_presentation_id_absorbed_no_duplicate_column(
        self, tmp_path: Path
    ) -> None:
        """The kind's own presentation_id column does not also appear —
        emitting both would duplicate a column."""
        events = self._events(tmp_path)
        create = _events_by_op(events, "w1")["c"]
        assert create.after is not None
        assert list(create.after.keys()) == ["presentation_id", "prop__status"]

    def test_debezium_value_schema_follows_elect_after_image_columns(
        self, tmp_path: Path
    ) -> None:
        """schemas_enable's after schema field list equals
        elect_after_image_columns' output — the declared schema and the
        rendered after-image are the same list by construction."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets",
            "widget",
            ["status"],
            keys={"widget": "presentation_id"},
            debezium=DebeziumConfig(source=_DEBEZIUM_SOURCE, schemas_enable=True),
        )
        anchor = EffectiveAnchor(
            start_instant=datetime(2026, 1, 1, tzinfo=timezone.utc),
            timezone=ZoneInfo("UTC"),
        )
        out = tmp_path / "out"
        out.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="debezium", sink="file", out=out, anchor=anchor
            )

        lines = (out / "widgets.jsonl").read_text(encoding="utf-8").splitlines()
        msg = json.loads(lines[0])
        after_field = next(f for f in msg["schema"]["fields"] if f["field"] == "after")
        after_columns = [f["field"] for f in after_field["fields"]]
        assert after_columns == ["presentation_id", "prop__status"]
        assert list(msg["payload"]["after"].keys()) == after_columns


# ---------------------------------------------------------------------------
# record_index election: digit-form values, surrogate ships verbatim
# ---------------------------------------------------------------------------


class TestRecordIndexElection:
    """`keys: {widget: record_index}` renders digit-form values; the kind's
    own presentation_id column ships verbatim beside it."""

    def _events(self, tmp_path: Path) -> list[StreamEvent]:
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets", "widget", ["status"], keys={"widget": "record_index"}
        )
        with open_emit(emit_dir) as emit:
            return list(iter_stream_events(emit, config, None))

    def test_key_value_is_digit_form_str(self, tmp_path: Path) -> None:
        """The elected key value is the digit-form record_index, as a str."""
        events = self._events(tmp_path)
        w1_create = _events_by_op(events, "w1")["c"]
        w2_create = _events_by_op(events, "w2")["c"]
        assert w1_create.key_column == "record_index"
        assert w1_create.key_value == "0"
        assert w2_create.key_value == "1"

    def test_surrogate_ships_verbatim_beside_it(self, tmp_path: Path) -> None:
        """presentation_id ships as its own after-image entry, unchanged."""
        events = self._events(tmp_path)
        create = _events_by_op(events, "w1")["c"]
        assert create.after is not None
        assert list(create.after.keys()) == [
            "record_index",
            "presentation_id",
            "prop__status",
        ]
        assert create.after["record_index"] == "0"
        assert create.after["presentation_id"] == "W_001"


# ---------------------------------------------------------------------------
# Reference translation: prop__ entries render the target's elected surface
# ---------------------------------------------------------------------------


class TestReferenceEdgeTranslation:
    """A kind-shaped stream's reference-valued property translates through
    its target kind's own elected surface."""

    def test_reference_renders_target_presentation_id(self, tmp_path: Path) -> None:
        """gadget.prop__target_id renders widget's elected presentation_id."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "gadgets", "gadget", ["target_id"], keys={"widget": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        by_id = {e.record_id: e for e in events}
        assert by_id["g1"].after is not None
        assert by_id["g1"].after["prop__target_id"] == "W_001"
        assert by_id["g2"].after["prop__target_id"] == "W_002"
        # gadget itself elects no surface — its own message key is unaffected.
        assert by_id["g1"].key_column == "record_id"

    def test_reference_renders_target_record_index(self, tmp_path: Path) -> None:
        """gadget.prop__target_id renders widget's elected record_index digits."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "gadgets", "gadget", ["target_id"], keys={"widget": "record_index"}
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        by_id = {e.record_id: e for e in events}
        assert by_id["g1"].after["prop__target_id"] == "0"
        assert by_id["g2"].after["prop__target_id"] == "1"


# ---------------------------------------------------------------------------
# Membership: owner re-key + member-field translation, format parity
# ---------------------------------------------------------------------------


class TestMembershipOwnerRekeyAndMemberFieldTranslation:
    """The owner's elected identity re-keys the after-image; a reference
    member field translates through its member row's own kind's surface;
    the owner re-key is identical whether the stream carries elem__ (scalar)
    or member__ (reference) fields."""

    def _events(self, tmp_path: Path) -> list[StreamEvent]:
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _membership_config(
            [
                MembershipStream(
                    name="waiters_priority",
                    membership=MembershipRef(kind="person", property="waiters"),
                    fields=["priority"],
                ),
                MembershipStream(
                    name="waiters_companion",
                    membership=MembershipRef(kind="person", property="waiters"),
                    fields=["companion"],
                ),
            ],
            keys={"person": "presentation_id", "pet": "record_index"},
        )
        with open_emit(emit_dir) as emit:
            return list(iter_stream_events(emit, config, None))

    def test_member_reference_field_translates_to_member_kind_surface(
        self, tmp_path: Path
    ) -> None:
        """member__companion__id renders pet's elected record_index digits;
        member__companion__kind ships verbatim."""
        events = self._events(tmp_path)
        companion_join = next(
            e for e in events if e.topic == "waiters_companion" and e.op == "join"
        )
        assert companion_join.after is not None
        assert companion_join.after["member__companion__kind"] == "pet"
        assert companion_join.after["member__companion__id"] == "0"

    def test_owner_rekeys_identically_under_elem_and_member_field_formats(
        self, tmp_path: Path
    ) -> None:
        """The owner's elected 'presentation_id' entry is the same value on
        both the elem__ scalar-only stream and the member__ reference-only
        stream (element-field format parity)."""
        events = self._events(tmp_path)
        priority_join = next(
            e for e in events if e.topic == "waiters_priority" and e.op == "join"
        )
        companion_join = next(
            e for e in events if e.topic == "waiters_companion" and e.op == "join"
        )
        assert priority_join.after is not None
        assert companion_join.after is not None
        assert priority_join.after["presentation_id"] == "P_001"
        assert companion_join.after["presentation_id"] == "P_001"
        assert priority_join.key_column == "presentation_id"
        assert companion_join.key_column == "presentation_id"
        assert priority_join.key_value == companion_join.key_value == "P_001"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class TestGates:
    """Static gates fail before any fold runs, naming the stream (and the
    column, for the edge gate)."""

    def test_mixed_election_across_spanned_populations_raises_naming_stream(
        self, tmp_path: Path
    ) -> None:
        """A stream spanning cat+dog with differing elected surfaces fails
        ElectionMixedIdentity, naming the stream."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "creatures",
            "creature",
            [],
            keys={"creature": {"cat": "presentation_id", "dog": "record_index"}},
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ElectionMixedIdentity, match="stream 'creatures'"):
                list(iter_stream_events(emit, config, None))

    def test_union_unsafe_uniform_presentation_id_raises(self, tmp_path: Path) -> None:
        """A uniform presentation_id election over union-unsafe key spaces
        fails ElectionUnionUnsafe, naming the stream."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "creatures", "creature", [], keys={"creature": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ElectionUnionUnsafe, match="stream 'creatures'"):
                list(iter_stream_events(emit, config, None))

    def test_edge_over_union_unsafe_admitted_domain_raises_naming_stream_and_column(
        self, tmp_path: Path
    ) -> None:
        """An edge admitting creature's full declared domain under a uniform
        union-unsafe presentation_id election fails ElectionUnionUnsafe,
        naming the stream and the column."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "trainers", "trainer", ["pet_id"], keys={"creature": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ElectionUnionUnsafe, match=r"stream 'trainers'\.prop__pet_id"
            ):
                list(iter_stream_events(emit, config, None))

    def test_unknown_kind_raises_election_kind_unknown(self, tmp_path: Path) -> None:
        """A `keys` entry naming no declared records kind fails ElectionKindUnknown."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets", "widget", [], keys={"phantom_kind": "record_id"}
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ElectionKindUnknown):
                list(iter_stream_events(emit, config, None))

    def test_unknown_sub_type_raises_election_sub_type_unknown(
        self, tmp_path: Path
    ) -> None:
        """A `keys` map key outside the kind's discriminator domain fails
        ElectionSubTypeUnknown."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets", "widget", [], keys={"creature": {"fish": "record_index"}}
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ElectionSubTypeUnknown):
                list(iter_stream_events(emit, config, None))

    def test_undeclared_registry_raises_election_presentation_undeclared(
        self, tmp_path: Path
    ) -> None:
        """A presentation_id election over a population with no registry
        entry fails ElectionPresentationUndeclared."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets", "widget", [], keys={"gadget": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ElectionPresentationUndeclared):
                list(iter_stream_events(emit, config, None))


# ---------------------------------------------------------------------------
# The elected-key uniqueness guard
# ---------------------------------------------------------------------------


class TestElectedKeyDuplicateGuard:
    """A duplicated/mutated presentation_id emit fails ElectedKeyDuplicate."""

    def test_duplicated_presentation_id_raises(self, tmp_path: Path) -> None:
        emit_dir = build_election_emit(
            tmp_path,
            presentation_keys=FULL_REGISTRY,
            duplicate_widget_presentation_id=True,
        )
        config = _kind_config(
            "widgets", "widget", ["status"], keys={"widget": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ElectedKeyDuplicate):
                list(iter_stream_events(emit, config, None))


# ---------------------------------------------------------------------------
# Ordering: election never re-sorts
# ---------------------------------------------------------------------------


class TestOrderingInvariance:
    """seq and inter-stream interleave are identical with and without `keys`
    — only key_column/key_value/after change."""

    def test_seq_and_interleave_unchanged_by_election(self, tmp_path: Path) -> None:
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        no_keys_config = StreamConfig(
            content="state-changes",
            streams=[
                KindStream(name="widgets", kind="widget", properties=["status"]),
                KindStream(name="gadgets", kind="gadget", properties=["target_id"]),
            ],
        )
        elected_config = StreamConfig(
            content="state-changes",
            streams=[
                KindStream(name="widgets", kind="widget", properties=["status"]),
                KindStream(name="gadgets", kind="gadget", properties=["target_id"]),
            ],
            keys={"widget": "presentation_id"},
        )

        with open_emit(emit_dir) as emit:
            plain_events = list(iter_stream_events(emit, no_keys_config, None))
        with open_emit(emit_dir) as emit:
            elected_events = list(iter_stream_events(emit, elected_config, None))

        plain_order = [(e.seq, e.topic, e.record_id, e.op) for e in plain_events]
        elected_order = [(e.seq, e.topic, e.record_id, e.op) for e in elected_events]
        assert plain_order == elected_order


# ---------------------------------------------------------------------------
# Key map never schema-wrapped under schemas_enable
# ---------------------------------------------------------------------------


class TestNeverSchemaWrapped:
    """The elected key never appears schema-wrapped: the Debezium envelope
    (even wrapped {schema, payload}) carries the key only inside `before` on
    a delete, and the Kafka message key (`encode_pinned({key_column:
    key_value})`, per kafka_sink.write_kafka_stream's docstring contract) is
    always the bare one-entry dict, independent of schemas_enable."""

    def test_wrapped_envelope_never_carries_a_top_level_key_field(
        self, tmp_path: Path
    ) -> None:
        """A schema-wrapped 'u' envelope has no top-level 'key' entry."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets", "widget", ["status"], keys={"widget": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))
        update = _events_by_op(events, "w1")["u"]

        anchor = make_anchor()
        ts_ms = rebased_epoch_ms(update.event_sim_time, anchor)
        schema = build_debezium_value_schema(
            "widget", ["presentation_id", "prop__status"], "fabulexa", "postgresql"
        )
        wrapped = render_debezium_message(
            update, ts_ms, _DEBEZIUM_SOURCE, "widget", schema
        )
        assert set(wrapped.keys()) == {"schema", "payload"}
        assert "key" not in wrapped
        assert "key" not in wrapped["payload"]  # type: ignore[operator]

    def test_kafka_message_key_is_bare_regardless_of_schemas_enable(
        self, tmp_path: Path
    ) -> None:
        """The kafka_sink key formula is a bare {key_column: key_value} dict —
        never {schema, payload} — whether or not the value carries a schema."""
        emit_dir = build_election_emit(tmp_path, presentation_keys=FULL_REGISTRY)
        config = _kind_config(
            "widgets", "widget", ["status"], keys={"widget": "presentation_id"}
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))
        update = _events_by_op(events, "w1")["u"]

        key_bytes = encode_pinned({update.key_column: update.key_value}).encode("utf-8")
        key_obj = json.loads(key_bytes)
        assert key_obj == {"presentation_id": "W_001"}
        assert "schema" not in key_obj
        assert "payload" not in key_obj
