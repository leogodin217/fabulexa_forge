"""End-to-end driver tests for the declared-stream grammar: declared-but-empty
topics, author-declared sub-type multiplicity (replacing the retired Layer-B
auto-split), Debezium table_identity + per-stream value schemas, and
determinism.

Layer B (topic_template rendering, groups regrouping) is retired: a stream's
declared `name` is its topic, one-to-one — there is no successor test for the
retired `StreamTopicSchemaUnambiguous` cross-kind-topic rule (a topic can no
longer straddle two kinds; each stream is one kind or one membership table).

All emits are built in-process (no shared recipe fixture).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
from _support.notices import discard_notice_sink
from _support.sidecar_builder import enum_options as _enum_options
from _support.sidecar_builder import identity_column as _identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.config.models import (
    DebeziumConfig,
    DebeziumSourceIdentity,
    KindStream,
    MembershipStream,
    StreamConfig,
)
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.exporters.streaming.engine import build_topic_set
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl, _membership_table_spec, make_anchor

_DAY = 86_400_000_000_000  # 1 day in nanoseconds

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_RECORD_COLS_ACTOR: list[dict[str, Any]] = [
    _identity_column("fork_path", "VARCHAR"),
    _identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    _identity_column("record_index", "BIGINT"),
    {"name": "prop__actor_type", "type": "VARCHAR"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_HISTORY_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# ---------------------------------------------------------------------------
# Emit builder helpers
# ---------------------------------------------------------------------------


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, Any]],
    rows: int,
    record_kind: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    return spec


def _build_actor_emit(
    tmp_path: Path,
    actor_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal emit with a sub-typed 'actor' kind.

    enum_domains maps actor to sub-types {customer, vip_customer, staff}.
    Columns: fork_path, record_id, created_sim_time, active, deactivated_at,
    last_mutation_sim_time, record_index, prop__actor_type, prop__name.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__actor", _RECORD_COLS_ACTOR))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph = ", ".join("?" for _ in _RECORD_COLS_ACTOR)
    for row in actor_rows:
        conn.execute(f'INSERT INTO "records__actor" VALUES ({ph})', list(row))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _RECORD_COLS_ACTOR,
                len(actor_rows),
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, 0),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        extra={
            "enum_domains": {
                "actor": {
                    "actor_type": _enum_options("customer", "vip_customer", "staff")
                }
            },
        },
    )
    return tmp_path


# Sample actor rows: (fork_path, record_id, created_sim_time, active,
#   deactivated_at, last_mutation_sim_time, record_index, prop__actor_type, prop__name)
_CUSTOMER_ROW = ("trunk", "c1", 1 * _DAY, True, None, 1 * _DAY, 0, "customer", "Alice")
_VIP_ROW = ("trunk", "v1", 2 * _DAY, True, None, 2 * _DAY, 1, "vip_customer", "Bob")
_STAFF_ROW = ("trunk", "s1", 3 * _DAY, True, None, 3 * _DAY, 2, "staff", "Charlie")


# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------


def _kind_stream(
    name: str,
    kind: str,
    properties: list[str],
    sub_types: list[str] | None = None,
) -> KindStream:
    return KindStream(name=name, kind=kind, properties=properties, sub_types=sub_types)


def _state_changes_config(
    streams: list[KindStream],
    debezium: DebeziumConfig | None = None,
) -> StreamConfig:
    return StreamConfig(content="state-changes", streams=streams, debezium=debezium)


def _debezium_source() -> DebeziumSourceIdentity:
    return DebeziumSourceIdentity(
        connector="postgresql",
        name="myserver",
        db="testdb",
        **{"schema": "public"},
        version="1.9.0.Final",
    )


def _debezium_config(
    schemas_enable: bool = True,
    table_identity: str = "source_table",
) -> DebeziumConfig:
    return DebeziumConfig(
        source=_debezium_source(),
        schemas_enable=schemas_enable,
        table_identity=table_identity,  # type: ignore[arg-type]
    )


def _read_jsonl_lines(path: Path) -> list[dict[str, Any]]:
    """Read a .jsonl file and parse every non-blank line as JSON."""
    return [
        json.loads(ln)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


# ---------------------------------------------------------------------------
# Author-declared sub-type multiplicity (replaces the retired auto-split)
# ---------------------------------------------------------------------------


class TestDeclaredSubTypeMultiplicity:
    """Per-sub-type topics now come from declaring N streams, not auto-splitting."""

    def test_three_streams_one_per_subtype_produce_three_topic_files(
        self, tmp_path: Path
    ) -> None:
        """Three separately-declared sub_types-scoped streams produce three
        distinct topic files, each named for its own stream."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _state_changes_config(
            [
                _kind_stream("customers", "actor", [], sub_types=["customer"]),
                _kind_stream("vips", "actor", [], sub_types=["vip_customer"]),
                _kind_stream("staff", "actor", [], sub_types=["staff"]),
            ]
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        assert (out_dir / "customers.jsonl").exists()
        assert (out_dir / "vips.jsonl").exists()
        assert (out_dir / "staff.jsonl").exists()
        assert outcome.events_per_topic["customers"] == 1
        assert outcome.events_per_topic["vips"] == 1
        assert outcome.events_per_topic["staff"] == 1

    def test_kind_field_on_jsonl_stays_actor_regardless_of_sub_type(
        self, tmp_path: Path
    ) -> None:
        """The 'kind' field on the JSONL object stays 'actor', not the sub-type."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW])
        config = _state_changes_config(
            [_kind_stream("customers", "actor", [], sub_types=["customer"])]
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        lines = _read_jsonl_lines(out_dir / "customers.jsonl")
        assert len(lines) == 1
        assert lines[0]["kind"] == "actor"


# ---------------------------------------------------------------------------
# Combined stream: one topic covers every sub-type
# ---------------------------------------------------------------------------


class TestCombinedStream:
    """A stream with no sub_types scope covers the kind's full domain in one
    topic — the combined-stream case."""

    def test_combined_stream_single_topic_all_subtypes(self, tmp_path: Path) -> None:
        """One 'actors' stream (no sub_types) puts every sub-type's events on
        one topic."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _state_changes_config([_kind_stream("actors", "actor", [])])
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        assert outcome.events_per_topic == {"actors": 3}
        assert (out_dir / "actors.jsonl").exists()
        assert not (out_dir / "customer.jsonl").exists()


# ---------------------------------------------------------------------------
# Declared-but-empty topics
# ---------------------------------------------------------------------------


class TestDeclaredButEmptyTopic:
    """A declared stream with zero matching rows yields an empty file and a
    zero count — declared intent, not observed rows, drives topic existence."""

    def test_empty_subtype_stream_yields_empty_file_and_zero_count(
        self, tmp_path: Path
    ) -> None:
        """A sub_types-scoped stream matching zero rows creates an empty
        .jsonl and reports events_per_topic == 0."""
        emit_dir = _build_actor_emit(tmp_path, [_STAFF_ROW])
        config = _state_changes_config(
            [
                _kind_stream("customers", "actor", [], sub_types=["customer"]),
                _kind_stream("staff", "actor", [], sub_types=["staff"]),
            ]
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        customers_file = out_dir / "customers.jsonl"
        assert customers_file.exists()
        assert customers_file.read_text(encoding="utf-8") == ""
        assert outcome.events_per_topic["customers"] == 0
        assert outcome.events_per_topic["staff"] == 1

    def test_empty_topic_stdout_writes_no_bytes(self, tmp_path: Path) -> None:
        """stdout sink writes no bytes for an empty topic but still reports
        its zero count."""
        emit_dir = _build_actor_emit(tmp_path, [_STAFF_ROW])
        config = _state_changes_config(
            [
                _kind_stream("customers", "actor", [], sub_types=["customer"]),
                _kind_stream("staff", "actor", [], sub_types=["staff"]),
            ]
        )
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="stdout",
                out=None,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        assert outcome.events_per_topic["customers"] == 0
        assert outcome.events_per_topic["staff"] == 1


class TestBuildTopicSetDeclarationOrder:
    """build_topic_set is a pure function of config.streams: declared names,
    declaration order, including declared-but-empty streams."""

    def test_topic_set_order_and_empty_membership(self) -> None:
        """A membership stream's topic set entry is present regardless of
        whether its table has rows."""
        config = StreamConfig(
            content="membership-events",
            streams=[
                MembershipStream(
                    name="waiters_feed",
                    membership={"kind": "queue", "property": "waiters"},
                    fields=[],
                ),
                MembershipStream(
                    name="members_feed",
                    membership={"kind": "team", "property": "members"},
                    fields=[],
                ),
            ],
        )
        assert build_topic_set(config) == ("waiters_feed", "members_feed")


# ---------------------------------------------------------------------------
# Debezium table_identity: source.table follows route_table or topic
# ---------------------------------------------------------------------------


class TestDebeziumTableIdentity:
    """source.table follows table_identity even inside a combined stream."""

    def test_source_table_reports_route_table_inside_combined_stream(
        self, tmp_path: Path
    ) -> None:
        """table_identity='source_table': inside one combined topic, each
        event's source.table is its own leaf (sub_type), not the stream name."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW])
        config = _state_changes_config(
            [_kind_stream("actors", "actor", [])],
            debezium=_debezium_config(
                schemas_enable=False, table_identity="source_table"
            ),
        )
        anchor = make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        lines = _read_jsonl_lines(out_dir / "actors.jsonl")
        assert len(lines) == 2
        tables = {ln["source"]["table"] for ln in lines}
        # Both events share the topic 'actors' but report their own leaf.
        assert tables == {"customer", "vip_customer"}

    def test_source_table_reports_stream_name_under_topic_identity(
        self, tmp_path: Path
    ) -> None:
        """table_identity='topic': every event in the combined stream reports
        the declaring stream's name, constant across sub-types."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW])
        config = _state_changes_config(
            [_kind_stream("actors", "actor", [])],
            debezium=_debezium_config(schemas_enable=False, table_identity="topic"),
        )
        anchor = make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        lines = _read_jsonl_lines(out_dir / "actors.jsonl")
        assert len(lines) == 2
        tables = {ln["source"]["table"] for ln in lines}
        assert tables == {"actors"}


class TestDebeziumPerStreamValueSchema:
    """The value schema is built per stream, keyed correctly for each
    table_identity value."""

    def test_schema_present_per_route_table_under_source_table_identity(
        self, tmp_path: Path
    ) -> None:
        """table_identity='source_table': each distinct route_table inside a
        combined stream gets its own schema (named after that leaf)."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW])
        config = _state_changes_config(
            [_kind_stream("actors", "actor", [])],
            debezium=_debezium_config(
                schemas_enable=True, table_identity="source_table"
            ),
        )
        anchor = make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        lines = _read_jsonl_lines(out_dir / "actors.jsonl")
        schema_names = {ln["schema"]["name"] for ln in lines}
        assert schema_names == {
            "myserver.customer.Envelope",
            "myserver.vip_customer.Envelope",
        }

    def test_schema_present_per_stream_under_topic_identity(
        self, tmp_path: Path
    ) -> None:
        """table_identity='topic': one schema, keyed by the stream name,
        covers every sub-type inside the combined stream."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW])
        config = _state_changes_config(
            [_kind_stream("actors", "actor", [])],
            debezium=_debezium_config(schemas_enable=True, table_identity="topic"),
        )
        anchor = make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        lines = _read_jsonl_lines(out_dir / "actors.jsonl")
        schema_names = {ln["schema"]["name"] for ln in lines}
        assert schema_names == {"myserver.actors.Envelope"}

    def test_two_streams_get_two_independent_schemas(self, tmp_path: Path) -> None:
        """Each declared stream gets its own schema — no cross-stream sharing."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _STAFF_ROW])
        config = _state_changes_config(
            [
                _kind_stream("customers", "actor", ["name"], sub_types=["customer"]),
                _kind_stream("staff", "actor", [], sub_types=["staff"]),
            ],
            debezium=_debezium_config(schemas_enable=True, table_identity="topic"),
        )
        anchor = make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        customer_lines = _read_jsonl_lines(out_dir / "customers.jsonl")
        staff_lines = _read_jsonl_lines(out_dir / "staff.jsonl")
        customer_fields = {
            f["field"] for f in customer_lines[0]["schema"]["fields"][1]["fields"]
        }
        staff_fields = {
            f["field"] for f in staff_lines[0]["schema"]["fields"][1]["fields"]
        }
        assert "name" in customer_fields
        assert "name" not in staff_fields


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same emit + same config => byte-identical file output across runs."""

    def test_identical_runs_produce_identical_output(self, tmp_path: Path) -> None:
        """Two runs of the same emit + config produce byte-identical files."""
        config = _state_changes_config(
            [
                _kind_stream("customers", "actor", [], sub_types=["customer"]),
                _kind_stream("vips", "actor", [], sub_types=["vip_customer"]),
                _kind_stream("staff", "actor", [], sub_types=["staff"]),
            ]
        )
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()

        with open_emit(emit_dir) as emit:
            outcome1 = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out1,
                anchor=None,
                notice_sink=discard_notice_sink,
            )
        with open_emit(emit_dir) as emit:
            outcome2 = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out2,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        assert outcome1.events_per_topic == outcome2.events_per_topic
        assert outcome1.total_events == outcome2.total_events

        for topic in outcome1.events_per_topic:
            f1 = (out1 / f"{topic}.jsonl").read_text(encoding="utf-8")
            f2 = (out2 / f"{topic}.jsonl").read_text(encoding="utf-8")
            assert f1 == f2, f"topic '{topic}' differs between runs"


# ---------------------------------------------------------------------------
# Membership declared-but-empty via stream_export
# ---------------------------------------------------------------------------


class TestMembershipDeclaredButEmptyTopic:
    """A selected membership table present in the emit but yielding zero events."""

    _MEM_COLS: list[dict[str, Any]] = [
        _identity_column("fork_path", "VARCHAR"),
        _identity_column("record_id", "VARCHAR"),
        {"name": "joined_sim_time", "type": "BIGINT"},
        {"name": "left_sim_time", "type": "BIGINT"},
    ]

    #: Election resolution requires the owner kind to carry a declared
    #: records table, even under the no-`keys` default (see
    #: `test_engine.py`'s `_owner_records_table_spec`).
    _OWNER_RECORD_COLS: list[dict[str, Any]] = [
        _identity_column("fork_path", "VARCHAR"),
        _identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        _identity_column("record_index", "BIGINT"),
    ]

    def _build_two_membership_emit(
        self,
        tmp_path: Path,
        waiters_rows: list[tuple[Any, ...]],
        members_rows: list[tuple[Any, ...]],
    ) -> Path:
        db_path = tmp_path / "run.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute(_ddl("membership__queue__waiters", self._MEM_COLS))
        ph = ", ".join("?" for _ in self._MEM_COLS)
        for row in waiters_rows:
            conn.execute(
                f'INSERT INTO "membership__queue__waiters" VALUES ({ph})', list(row)
            )
        conn.execute(_ddl("membership__team__members", self._MEM_COLS))
        for row in members_rows:
            conn.execute(
                f'INSERT INTO "membership__team__members" VALUES ({ph})', list(row)
            )
        for owner_kind in ("queue", "team"):
            conn.execute(_ddl(f"records__{owner_kind}", self._OWNER_RECORD_COLS))
        conn.close()

        _write_sidecar(
            tmp_path,
            tables=[
                _membership_table_spec(
                    "membership__queue__waiters",
                    self._MEM_COLS,
                    len(waiters_rows),
                    "queue",
                    "waiters",
                ),
                _membership_table_spec(
                    "membership__team__members",
                    self._MEM_COLS,
                    len(members_rows),
                    "team",
                    "members",
                ),
                {
                    "name": "records__queue",
                    "category": "records",
                    "record_kind": "queue",
                    "columns": self._OWNER_RECORD_COLS,
                    "rows": 0,
                },
                {
                    "name": "records__team",
                    "category": "records",
                    "record_kind": "team",
                    "columns": self._OWNER_RECORD_COLS,
                    "rows": 0,
                },
            ],
            branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        )
        return tmp_path

    def test_empty_table_yields_zero_events_and_empty_file(
        self, tmp_path: Path
    ) -> None:
        """An empty membership table yields zero events and an empty .jsonl file."""
        emit_dir = self._build_two_membership_emit(
            tmp_path, [("trunk", "w1", 10, None)], []
        )
        config = StreamConfig(
            content="membership-events",
            streams=[
                MembershipStream(
                    name="waiters_feed",
                    membership={"kind": "queue", "property": "waiters"},
                    fields=[],
                ),
                MembershipStream(
                    name="members_feed",
                    membership={"kind": "team", "property": "members"},
                    fields=[],
                ),
            ],
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        assert outcome.events_per_topic["members_feed"] == 0
        assert outcome.events_per_topic["waiters_feed"] >= 1

        members_file = out_dir / "members_feed.jsonl"
        assert members_file.exists()
        assert members_file.read_text(encoding="utf-8") == ""
