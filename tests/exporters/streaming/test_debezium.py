"""Tests for debezium.py: rebased_epoch_ms, build_debezium_value_schema,
render_debezium_message.

Covers epoch-ms computation, schema structure, envelope key order, and op
semantics (c/u/d). Sink-writer behavior (stdout/file routing, byte-identity,
paced delivery) is exercised through the live sink, driver.write_line_stream,
in test_driver.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import DebeziumSourceIdentity
from fabulexa_forge.exporters.streaming.debezium import (
    build_debezium_value_schema,
    rebased_epoch_ms,
    render_debezium_message,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent

from ._helpers import make_anchor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)


def _schema_fields(schema: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return the envelope fields dict keyed by field name."""
    fields = schema["fields"]
    assert isinstance(fields, list)
    return {f["field"]: f for f in fields}


def _payload_source(payload: dict[str, object]) -> dict[str, object]:
    """Return the 'source' block from a bare Debezium envelope as a typed dict."""
    source = payload["source"]
    assert isinstance(source, dict)
    return source  # type: ignore[return-value]


def _sub_fields(field: dict[str, object]) -> list[dict[str, object]]:
    """Return the nested 'fields' list from a schema field entry."""
    sub = field["fields"]
    assert isinstance(sub, list)
    return sub


def _make_identity(
    connector: str = "postgresql",
    name: str = "fabulexa",
    db: str = "fabulexa",
    schema: str = "public",
    version: str = "2.5.0.Final",
) -> DebeziumSourceIdentity:
    """Build a DebeziumSourceIdentity."""
    return DebeziumSourceIdentity.model_validate(
        {
            "connector": connector,
            "name": name,
            "db": db,
            "schema": schema,
            "version": version,
        }
    )


def _make_event(
    seq: int = 1,
    op: Literal["c", "u", "d", "r"] = "c",
    kind: str = "actor",
    record_id: str = "r1",
    event_sim_time: int = 0,
    ts: str | int = "2026-01-01T00:00:00+00:00",
    after: dict[str, object] | None = None,
    topic: str | None = None,
    route_table: str | None = None,
    key_column: str = "record_id",
    key_value: str | None = None,
) -> StreamEvent:
    """Build a StreamEvent.

    key_column/key_value default to the byte-identical no-election rendering
    ({"record_id": record_id}); pass an elected surface to exercise the
    tombstone/key-only-before-image rendering under election.
    """
    if after is None and op != "d":
        after = {"record_id": record_id, "status": "active"}
    return StreamEvent(
        seq=seq,
        op=op,
        kind=kind,
        record_id=record_id,
        event_sim_time=event_sim_time,
        ts=ts,
        after=after,
        topic=topic if topic is not None else kind,
        route_table=route_table if route_table is not None else kind,
        key_column=key_column,
        key_value=key_value if key_value is not None else record_id,
    )


def _make_membership_event(
    seq: int = 1,
    op: Literal["join", "leave"] = "join",
    owner_kind: str = "actor",
    record_id: str = "r1",
    event_sim_time: int = 0,
    after: dict[str, object] | None = None,
    topic: str | None = None,
    route_table: str | None = None,
    key_column: str = "record_id",
    key_value: str | None = None,
) -> StreamEvent:
    """Build a membership StreamEvent with op in {'join', 'leave'}."""
    if after is None:
        after = {"record_id": record_id, "priority": "high"}
    resolved_route = (
        route_table
        if route_table is not None
        else f"membership__{owner_kind}__priority"
    )
    resolved_topic = topic if topic is not None else resolved_route
    return StreamEvent(
        seq=seq,
        op=op,
        kind=owner_kind,
        record_id=record_id,
        event_sim_time=event_sim_time,
        ts="2026-01-01T00:00:00+00:00",
        after=after,
        topic=resolved_topic,
        route_table=resolved_route,
        key_column=key_column,
        key_value=key_value if key_value is not None else record_id,
    )


# ---------------------------------------------------------------------------
# rebased_epoch_ms
# ---------------------------------------------------------------------------


class TestRebasedEpochMs:
    """Tests for rebased_epoch_ms."""

    def test_known_start_instant_zero_sim_time(self) -> None:
        """start_instant at epoch + event_sim_time=0 => 0 ms."""
        anchor = make_anchor(datetime(1970, 1, 1, 0, 0, 0, tzinfo=_UTC))
        assert rebased_epoch_ms(0, anchor) == 0

    def test_known_start_and_sim_time(self) -> None:
        """start at 2026-01-01T00:00:00Z + 1_000_000_000 ns => correct ms."""
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=_UTC)
        anchor = make_anchor(start)
        # epoch_us = (start - epoch) // timedelta(microseconds=1)
        epoch_us = (start - _EPOCH) // timedelta(microseconds=1)
        event_sim_time = 1_000_000_000  # 1 second in ns
        expected = (epoch_us * 1000 + event_sim_time) // 1_000_000
        assert rebased_epoch_ms(event_sim_time, anchor) == expected

    def test_result_agrees_with_jsonl_ts_to_ms_precision(self) -> None:
        """Epoch-ms agrees with the JSONL ISO-string representation to ms precision."""
        start = datetime(2026, 6, 15, 12, 30, 45, 123456, tzinfo=_UTC)
        anchor = make_anchor(start)
        event_sim_time = 500_000_000  # 500 ms in ns
        ms = rebased_epoch_ms(event_sim_time, anchor)
        # Compute expected: integer arithmetic, microseconds
        epoch_us = (start - _EPOCH) // timedelta(microseconds=1)
        expected = (epoch_us * 1000 + event_sim_time) // 1_000_000
        assert ms == expected

    def test_microsecond_resolution_start_instant_is_exact(self) -> None:
        """A microsecond-resolution start_instant rounds to the exact ms."""
        start = datetime(2026, 1, 1, 0, 0, 0, 500, tzinfo=_UTC)  # 500 us
        anchor = make_anchor(start)
        epoch_us = (start - _EPOCH) // timedelta(microseconds=1)
        result = rebased_epoch_ms(0, anchor)
        assert result == epoch_us * 1000 // 1_000_000

    def test_non_utc_anchor_projects_to_utc(self) -> None:
        """A non-UTC start_instant is projected to UTC correctly."""
        start_utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)
        nyc = ZoneInfo("America/New_York")
        start_nyc = start_utc.astimezone(nyc)
        anchor_utc = make_anchor(start_utc)
        anchor_nyc = EffectiveAnchor(start_instant=start_nyc, timezone=nyc)
        assert rebased_epoch_ms(0, anchor_utc) == rebased_epoch_ms(0, anchor_nyc)

    def test_never_uses_float(self) -> None:
        """rebased_epoch_ms result is an int (no float arithmetic)."""
        anchor = make_anchor()
        result = rebased_epoch_ms(1_000_000_000, anchor)
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# build_debezium_value_schema
# ---------------------------------------------------------------------------


class TestBuildDebeziumValueSchema:
    """Tests for build_debezium_value_schema structure and naming."""

    def _build(
        self,
        columns: list[str] | None = None,
        source_name: str = "fabulexa",
        connector: str = "postgresql",
        table: str = "actor",
    ) -> dict[str, object]:
        cols = columns if columns is not None else ["record_id", "status"]
        return build_debezium_value_schema(table, cols, source_name, connector)

    def test_envelope_name(self) -> None:
        """Envelope named <source_name>.<table>.Envelope."""
        schema = self._build()
        assert schema["name"] == "fabulexa.actor.Envelope"

    def test_envelope_not_optional(self) -> None:
        """Envelope struct is non-optional."""
        schema = self._build()
        assert schema["optional"] is False

    def test_envelope_type_struct(self) -> None:
        """Envelope type is 'struct'."""
        schema = self._build()
        assert schema["type"] == "struct"

    def test_before_after_named_value(self) -> None:
        """before/after structs are named <source_name>.<table>.Value."""
        schema = self._build()
        fields = _schema_fields(schema)
        assert fields["before"]["name"] == "fabulexa.actor.Value"
        assert fields["after"]["name"] == "fabulexa.actor.Value"

    def test_before_after_optional(self) -> None:
        """before/after are optional structs."""
        schema = self._build()
        fields = _schema_fields(schema)
        assert fields["before"]["optional"] is True
        assert fields["after"]["optional"] is True

    def test_before_after_columns_in_order(self) -> None:
        """before/after fields match columns list in order."""
        cols = ["record_id", "presentation_id", "alpha", "beta"]
        schema = self._build(columns=cols)
        fields = _schema_fields(schema)
        before_fields = [f["field"] for f in _sub_fields(fields["before"])]
        after_fields = [f["field"] for f in _sub_fields(fields["after"])]
        assert before_fields == cols
        assert after_fields == cols

    def test_before_after_column_fields_optional_string(self) -> None:
        """Each column field inside before/after is optional string."""
        schema = self._build(columns=["record_id", "x"])
        fields = _schema_fields(schema)
        for col_field in _sub_fields(fields["after"]):
            assert col_field["type"] == "string"
            assert col_field["optional"] is True

    def test_source_struct_non_optional(self) -> None:
        """source struct is non-optional."""
        schema = self._build()
        fields = _schema_fields(schema)
        assert fields["source"]["optional"] is False

    def test_source_struct_name_includes_connector(self) -> None:
        """source struct is named io.debezium.connector.<connector>.Source."""
        schema = self._build(connector="postgresql")
        fields = _schema_fields(schema)
        assert fields["source"]["name"] == "io.debezium.connector.postgresql.Source"

    def test_source_fields_in_pinned_order(self) -> None:
        """source struct fields are in the pinned serialized source order."""
        schema = self._build()
        fields = _schema_fields(schema)
        source_field_names = [f["field"] for f in _sub_fields(fields["source"])]
        expected = [
            "version",
            "connector",
            "name",
            "ts_ms",
            "snapshot",
            "db",
            "sequence",
            "schema",
            "table",
            "txId",
            "lsn",
        ]
        assert source_field_names == expected

    def test_source_ts_ms_not_optional(self) -> None:
        """source.ts_ms is non-optional (differs from envelope ts_ms)."""
        schema = self._build()
        fields = _schema_fields(schema)
        source_fields = {f["field"]: f for f in _sub_fields(fields["source"])}
        assert source_fields["ts_ms"]["optional"] is False

    def test_envelope_ts_ms_optional(self) -> None:
        """Envelope ts_ms is optional (differs from source.ts_ms)."""
        schema = self._build()
        fields = _schema_fields(schema)
        assert fields["ts_ms"]["optional"] is True
        assert fields["ts_ms"]["type"] == "int64"

    def test_op_non_optional_string(self) -> None:
        """op is non-optional string."""
        schema = self._build()
        fields = _schema_fields(schema)
        assert fields["op"]["type"] == "string"
        assert fields["op"]["optional"] is False

    def test_transaction_optional_struct(self) -> None:
        """transaction is an optional struct."""
        schema = self._build()
        fields = _schema_fields(schema)
        assert fields["transaction"]["type"] == "struct"
        assert fields["transaction"]["optional"] is True

    def test_envelope_field_order(self) -> None:
        """Envelope fields are in the pinned order: before, after, source, op, ts_ms, transaction."""
        schema = self._build()
        field_names = [f["field"] for f in _schema_fields(schema).values()]
        assert field_names == [
            "before",
            "after",
            "source",
            "op",
            "ts_ms",
            "transaction",
        ]


# ---------------------------------------------------------------------------
# render_debezium_message
# ---------------------------------------------------------------------------


class TestRenderDebeziumMessage:
    """Tests for render_debezium_message envelope and wrapping."""

    def setup_method(self) -> None:
        self.identity = _make_identity()
        self.anchor = make_anchor()
        self.columns = ["record_id", "status"]
        self.schema = build_debezium_value_schema(
            "actor", self.columns, "fabulexa", "postgresql"
        )

    def _render(
        self,
        event: StreamEvent,
        value_schema: dict[str, object] | None = None,
    ) -> dict[str, object]:
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        return render_debezium_message(
            event, ts_ms, self.identity, event.kind, value_schema
        )

    def test_c_event_before_null_after_present(self) -> None:
        """c event: before is null, after is the full after-image."""
        event = _make_event(op="c", after={"record_id": "r1", "status": "active"})
        payload = self._render(event)
        assert payload["before"] is None
        assert payload["after"] == {"record_id": "r1", "status": "active"}

    def test_u_event_before_null_after_present(self) -> None:
        """u event: before is null, after is the full after-image."""
        event = _make_event(op="u", after={"record_id": "r1", "status": "updated"})
        payload = self._render(event)
        assert payload["before"] is None
        assert payload["after"] == {"record_id": "r1", "status": "updated"}

    def test_d_event_before_record_id_after_null(self) -> None:
        """d event: before is {record_id}, after is null."""
        event = _make_event(op="d", record_id="r1", after=None)
        payload = self._render(event)
        assert payload["before"] == {"record_id": "r1"}
        assert payload["after"] is None

    def test_op_verbatim(self) -> None:
        """op field matches event.op verbatim."""
        for op in ("c", "u", "d"):
            event = _make_event(op=op)  # type: ignore[arg-type]
            payload = self._render(event)
            assert payload["op"] == op

    def test_ts_ms_equals_source_ts_ms(self) -> None:
        """envelope ts_ms equals source.ts_ms."""
        event = _make_event(event_sim_time=1_000_000_000)
        payload = self._render(event)
        assert payload["ts_ms"] == _payload_source(payload)["ts_ms"]

    def test_source_lsn_equals_event_seq(self) -> None:
        """source.lsn equals event.seq."""
        event = _make_event(seq=42)
        payload = self._render(event)
        assert _payload_source(payload)["lsn"] == 42

    def test_source_sequence_format(self) -> None:
        """source.sequence is '[null,\"<seq>\"]'."""
        event = _make_event(seq=7)
        payload = self._render(event)
        assert _payload_source(payload)["sequence"] == '[null,"7"]'

    def test_source_snapshot_false(self) -> None:
        """source.snapshot is 'false'."""
        event = _make_event()
        payload = self._render(event)
        assert _payload_source(payload)["snapshot"] == "false"

    def test_source_txid_null(self) -> None:
        """source.txId is null."""
        event = _make_event()
        payload = self._render(event)
        assert _payload_source(payload)["txId"] is None

    def test_source_table_equals_kind(self) -> None:
        """source.table equals the table argument (event.kind)."""
        event = _make_event(kind="actor")
        payload = self._render(event)
        assert _payload_source(payload)["table"] == "actor"

    def test_transaction_null(self) -> None:
        """transaction is null."""
        event = _make_event()
        payload = self._render(event)
        assert payload["transaction"] is None

    def test_without_schema_bare_payload(self) -> None:
        """value_schema=None returns bare envelope (no schema/payload wrapper)."""
        event = _make_event()
        result = self._render(event, value_schema=None)
        assert "schema" not in result
        assert "payload" not in result
        assert "op" in result

    def test_with_schema_wrapped(self) -> None:
        """value_schema provided returns {schema, payload} wrapper."""
        event = _make_event()
        result = self._render(event, value_schema=self.schema)
        assert set(result.keys()) == {"schema", "payload"}
        assert result["schema"] is self.schema
        assert "op" in result["payload"]  # type: ignore[operator]

    def test_before_content_same_with_and_without_schema(self) -> None:
        """before content is identical regardless of value_schema on d."""
        event = _make_event(op="d", record_id="r42", after=None)
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        bare = render_debezium_message(event, ts_ms, self.identity, event.kind, None)
        wrapped = render_debezium_message(
            event, ts_ms, self.identity, event.kind, self.schema
        )
        assert bare["before"] == wrapped["payload"]["before"]  # type: ignore[index]

    def test_envelope_key_order(self) -> None:
        """Serialized envelope key order is pinned: before, after, source, op, ts_ms, transaction."""
        event = _make_event(op="c", after={"record_id": "r1", "status": "active"})
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        bare = render_debezium_message(event, ts_ms, self.identity, event.kind, None)
        encoded = json.dumps(bare, separators=(",", ":"), sort_keys=False)
        parsed = json.loads(encoded)
        assert list(parsed.keys()) == [
            "before",
            "after",
            "source",
            "op",
            "ts_ms",
            "transaction",
        ]

    def test_message_key_order_with_schema(self) -> None:
        """Serialized message key order is: schema, payload."""
        event = _make_event()
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        msg = render_debezium_message(
            event, ts_ms, self.identity, event.kind, self.schema
        )
        encoded = json.dumps(msg, separators=(",", ":"), sort_keys=False)
        parsed = json.loads(encoded)
        assert list(parsed.keys()) == ["schema", "payload"]

    def test_source_key_order(self) -> None:
        """Serialized source key order is pinned."""
        event = _make_event()
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        bare = render_debezium_message(event, ts_ms, self.identity, event.kind, None)
        encoded = json.dumps(bare["source"], separators=(",", ":"), sort_keys=False)
        parsed = json.loads(encoded)
        expected = [
            "version",
            "connector",
            "name",
            "ts_ms",
            "snapshot",
            "db",
            "sequence",
            "schema",
            "table",
            "txId",
            "lsn",
        ]
        assert list(parsed.keys()) == expected


# ---------------------------------------------------------------------------
# render_debezium_message — the 'r' snapshot-read op
# ---------------------------------------------------------------------------


class TestRenderDebeziumMessageSnapshot:
    """'r' (the seek snapshot read): before=null, after=the full after-image,
    op='r', source.snapshot='true', source.lsn/sequence carry the shared
    snapshot position N. Every other op still renders snapshot='false'
    byte-identically (TestRenderDebeziumMessage, unchanged)."""

    def setup_method(self) -> None:
        self.identity = _make_identity()
        self.anchor = make_anchor()

    def _render(self, event: StreamEvent) -> dict[str, object]:
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        return render_debezium_message(event, ts_ms, self.identity, event.kind, None)

    def test_op_is_r(self) -> None:
        event = _make_event(op="r")
        payload = self._render(event)
        assert payload["op"] == "r"

    def test_before_null_after_full_image(self) -> None:
        after = {"record_id": "r1", "status": "active"}
        event = _make_event(op="r", after=after)
        payload = self._render(event)
        assert payload["before"] is None
        assert payload["after"] == after

    def test_source_snapshot_true(self) -> None:
        event = _make_event(op="r")
        payload = self._render(event)
        assert _payload_source(payload)["snapshot"] == "true"

    def test_source_lsn_equals_shared_seq(self) -> None:
        event = _make_event(op="r", seq=9)
        payload = self._render(event)
        assert _payload_source(payload)["lsn"] == 9

    def test_source_sequence_format(self) -> None:
        event = _make_event(op="r", seq=9)
        payload = self._render(event)
        assert _payload_source(payload)["sequence"] == '[null,"9"]'

    def test_every_other_op_still_reports_snapshot_false(self) -> None:
        for op in ("c", "u", "d"):
            event = _make_event(op=op)  # type: ignore[arg-type]
            payload = self._render(event)
            assert _payload_source(payload)["snapshot"] == "false"


# ---------------------------------------------------------------------------
# render_debezium_message under key election: tombstone / before key map
# ---------------------------------------------------------------------------


class TestRenderDebeziumMessageElectedKey:
    """A 'd' event's before is the elected key map {key_column: key_value};
    c/u events never carry a before key map, elected or not."""

    def setup_method(self) -> None:
        self.identity = _make_identity()
        self.anchor = make_anchor()

    def _render(
        self,
        event: StreamEvent,
        value_schema: dict[str, object] | None = None,
    ) -> dict[str, object]:
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        return render_debezium_message(
            event, ts_ms, self.identity, event.kind, value_schema
        )

    def test_tombstone_before_is_elected_presentation_id_key_map(self) -> None:
        """A presentation_id-elected 'd' event's before is {"presentation_id": ...}."""
        event = _make_event(
            op="d",
            record_id="r1",
            after=None,
            key_column="presentation_id",
            key_value="P_001",
        )
        payload = self._render(event)
        assert payload["before"] == {"presentation_id": "P_001"}
        assert payload["after"] is None

    def test_tombstone_before_is_elected_record_index_key_map(self) -> None:
        """A record_index-elected 'd' event's before is {"record_index": "<digits>"}."""
        event = _make_event(
            op="d", record_id="r1", after=None, key_column="record_index", key_value="3"
        )
        payload = self._render(event)
        assert payload["before"] == {"record_index": "3"}

    def test_tombstone_before_wrapped_and_bare_agree(self) -> None:
        """The elected before key map is identical wrapped and bare."""
        event = _make_event(
            op="d",
            record_id="r1",
            after=None,
            key_column="presentation_id",
            key_value="P_002",
        )
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        schema = build_debezium_value_schema(
            "actor", ["presentation_id", "status"], "fabulexa", "postgresql"
        )
        bare = render_debezium_message(event, ts_ms, self.identity, event.kind, None)
        wrapped = render_debezium_message(
            event, ts_ms, self.identity, event.kind, schema
        )
        assert bare["before"] == wrapped["payload"]["before"]  # type: ignore[index]

    def test_create_update_events_never_carry_elected_before(self) -> None:
        """c/u events' before stays null regardless of an elected key_column."""
        for op in ("c", "u"):
            event = _make_event(
                op=op,  # type: ignore[arg-type]
                key_column="presentation_id",
                key_value="P_003",
                after={"presentation_id": "P_003", "status": "active"},
            )
            payload = self._render(event)
            assert payload["before"] is None

    def test_published_non_elected_surface_in_after_never_in_before(self) -> None:
        """A published non-elected surface (record_id, here) rides the 'c'
        after payload alongside the elected key, but a 'd' tombstone's
        before carries the elected key alone — the non-elected surface never
        reaches a message key."""
        create = _make_event(
            op="c",
            key_column="presentation_id",
            key_value="P_004",
            after={"record_id": "r1", "presentation_id": "P_004", "status": "active"},
        )
        create_payload = self._render(create)
        assert create_payload["after"] == {
            "record_id": "r1",
            "presentation_id": "P_004",
            "status": "active",
        }

        delete = _make_event(
            op="d", after=None, key_column="presentation_id", key_value="P_004"
        )
        delete_payload = self._render(delete)
        assert delete_payload["before"] == {"presentation_id": "P_004"}
        assert delete_payload["after"] is None


# ---------------------------------------------------------------------------
# render_debezium_message — membership op branch
# ---------------------------------------------------------------------------


class TestRenderDebeziumMessageMembership:
    """Tests for render_debezium_message with op in {'join', 'leave'}."""

    def setup_method(self) -> None:
        self.identity = _make_identity()
        self.anchor = make_anchor()
        self.columns = ["event", "record_id", "priority"]
        self.schema = build_debezium_value_schema(
            "membership__actor__priority", self.columns, "fabulexa", "postgresql"
        )

    def _render(
        self,
        event: StreamEvent,
        value_schema: dict[str, object] | None = None,
        table: str = "membership__actor__priority",
    ) -> dict[str, object]:
        ts_ms = rebased_epoch_ms(event.event_sim_time, self.anchor)
        return render_debezium_message(event, ts_ms, self.identity, table, value_schema)

    def test_join_op_envelope_op_is_c(self) -> None:
        """op='join' -> envelope op='c'."""
        event = _make_membership_event(op="join")
        payload = self._render(event)
        assert payload["op"] == "c"

    def test_leave_op_envelope_op_is_c(self) -> None:
        """op='leave' -> envelope op='c'."""
        event = _make_membership_event(op="leave")
        payload = self._render(event)
        assert payload["op"] == "c"

    def test_join_before_null(self) -> None:
        """op='join' -> before is null."""
        event = _make_membership_event(op="join")
        payload = self._render(event)
        assert payload["before"] is None

    def test_leave_before_null(self) -> None:
        """op='leave' -> before is null."""
        event = _make_membership_event(op="leave")
        payload = self._render(event)
        assert payload["before"] is None

    def test_join_after_leads_with_event_key(self) -> None:
        """op='join' -> after has 'event' as first key with value 'join'."""
        event = _make_membership_event(op="join")
        payload = self._render(event)
        after = payload["after"]
        assert isinstance(after, dict)
        keys = list(after.keys())
        assert keys[0] == "event"
        assert after["event"] == "join"

    def test_leave_after_leads_with_event_key(self) -> None:
        """op='leave' -> after has 'event' as first key with value 'leave'."""
        event = _make_membership_event(op="leave")
        payload = self._render(event)
        after = payload["after"]
        assert isinstance(after, dict)
        keys = list(after.keys())
        assert keys[0] == "event"
        assert after["event"] == "leave"

    def test_after_minus_event_equals_event_after_scalar(self) -> None:
        """Membership after minus leading 'event' equals event.after (scalar field)."""
        event_after = {"record_id": "r1", "priority": "high"}
        event = _make_membership_event(op="join", after=event_after)
        payload = self._render(event)
        after = payload["after"]
        assert isinstance(after, dict)
        after_minus_event = {k: v for k, v in after.items() if k != "event"}
        assert after_minus_event == event_after

    def test_after_minus_event_equals_event_after_reference_pair(self) -> None:
        """Membership after minus leading 'event' equals event.after (reference-pair fields)."""
        event_after = {
            "record_id": "r1",
            "ref_kind": "x1",
            "ref_id": "alpha",
        }
        event = _make_membership_event(op="join", after=event_after)
        payload = self._render(event)
        after = payload["after"]
        assert isinstance(after, dict)
        after_minus_event = {k: v for k, v in after.items() if k != "event"}
        assert after_minus_event == event_after

    def test_after_minus_event_equals_event_after_empty_fields(self) -> None:
        """Membership after minus leading 'event' equals event.after (owner identity only)."""
        event_after: dict[str, object] = {"record_id": "r1"}
        event = _make_membership_event(op="join", after=event_after)
        payload = self._render(event)
        after = payload["after"]
        assert isinstance(after, dict)
        after_minus_event = {k: v for k, v in after.items() if k != "event"}
        assert after_minus_event == event_after

    def test_membership_never_emits_d_op(self) -> None:
        """Membership events never emit envelope op='d'."""
        for op in ("join", "leave"):
            event = _make_membership_event(op=op)  # type: ignore[arg-type]
            payload = self._render(event)
            assert payload["op"] != "d"

    def test_membership_before_never_key_only(self) -> None:
        """Membership events never produce a key-only before (before is always null)."""
        for op in ("join", "leave"):
            event = _make_membership_event(op=op)  # type: ignore[arg-type]
            payload = self._render(event)
            assert payload["before"] is None

    def test_without_schema_bare_envelope(self) -> None:
        """value_schema=None returns bare envelope for membership event."""
        event = _make_membership_event(op="join")
        result = self._render(event, value_schema=None)
        assert "schema" not in result
        assert "payload" not in result
        assert result["op"] == "c"

    def test_with_schema_wrapped(self) -> None:
        """value_schema non-None returns {schema, payload} wrapper for membership event."""
        event = _make_membership_event(op="join")
        result = self._render(event, value_schema=self.schema)
        assert set(result.keys()) == {"schema", "payload"}
        assert result["schema"] is self.schema
        payload = result["payload"]
        assert isinstance(payload, dict)
        assert payload["before"] is None
        assert payload["op"] == "c"

    def test_membership_before_null_under_schema(self) -> None:
        """Membership before is null in schema-wrapped payload."""
        event = _make_membership_event(op="leave")
        result = self._render(event, value_schema=self.schema)
        payload = result["payload"]
        assert isinstance(payload, dict)
        assert payload["before"] is None

    def test_source_block_lsn_eq_seq(self) -> None:
        """source.lsn equals event.seq for a membership event."""
        event = _make_membership_event(seq=42, op="join")
        payload = self._render(event)
        assert _payload_source(payload)["lsn"] == 42

    def test_source_block_sequence_format(self) -> None:
        """source.sequence is '[null,\"<seq>\"]' for a membership event."""
        event = _make_membership_event(seq=7, op="join")
        payload = self._render(event)
        assert _payload_source(payload)["sequence"] == '[null,"7"]'

    def test_source_block_txid_null(self) -> None:
        """source.txId is null for a membership event."""
        event = _make_membership_event(op="join")
        payload = self._render(event)
        assert _payload_source(payload)["txId"] is None

    def test_source_block_snapshot_false(self) -> None:
        """source.snapshot is 'false' for a membership event."""
        event = _make_membership_event(op="join")
        payload = self._render(event)
        assert _payload_source(payload)["snapshot"] == "false"

    def test_source_block_table_is_passed_table(self) -> None:
        """source.table equals the table argument for a membership event."""
        event = _make_membership_event(op="join")
        payload = self._render(event, table="membership__actor__priority")
        assert _payload_source(payload)["table"] == "membership__actor__priority"

    def test_ts_ms_equals_source_ts_ms(self) -> None:
        """envelope ts_ms equals source.ts_ms for a membership event."""
        event = _make_membership_event(op="join", event_sim_time=1_000_000_000)
        payload = self._render(event)
        assert payload["ts_ms"] == _payload_source(payload)["ts_ms"]


# ---------------------------------------------------------------------------
# build_debezium_value_schema — membership column contract
# ---------------------------------------------------------------------------


class TestBuildDebeziumValueSchemaMembership:
    """Tests for build_debezium_value_schema with ('event',) + membership columns."""

    def test_before_after_lead_with_event_field(self) -> None:
        """before/after Value structs lead with an 'event' optional-string field."""
        columns = ["event", "record_id", "priority"]
        schema = build_debezium_value_schema(
            "membership__actor__priority", columns, "fabulexa", "postgresql"
        )
        fields = _schema_fields(schema)
        before_fields = _sub_fields(fields["before"])
        after_fields = _sub_fields(fields["after"])
        assert before_fields[0]["field"] == "event"
        assert after_fields[0]["field"] == "event"

    def test_event_field_is_optional_string(self) -> None:
        """The 'event' field in the Value struct is optional string."""
        columns = ["event", "record_id", "priority"]
        schema = build_debezium_value_schema(
            "membership__actor__priority", columns, "fabulexa", "postgresql"
        )
        fields = _schema_fields(schema)
        after_fields = {f["field"]: f for f in _sub_fields(fields["after"])}
        assert after_fields["event"]["type"] == "string"
        assert after_fields["event"]["optional"] is True

    def test_before_struct_is_optional(self) -> None:
        """The before struct is optional (always-null membership before is schema-legal)."""
        columns = ["event", "record_id", "priority"]
        schema = build_debezium_value_schema(
            "membership__actor__priority", columns, "fabulexa", "postgresql"
        )
        fields = _schema_fields(schema)
        assert fields["before"]["optional"] is True

    def test_all_columns_present_in_order(self) -> None:
        """All columns including 'event' are present in order in both structs."""
        columns = ["event", "record_id", "priority"]
        schema = build_debezium_value_schema(
            "membership__actor__priority", columns, "fabulexa", "postgresql"
        )
        fields = _schema_fields(schema)
        before_cols = [f["field"] for f in _sub_fields(fields["before"])]
        after_cols = [f["field"] for f in _sub_fields(fields["after"])]
        assert before_cols == columns
        assert after_cols == columns
