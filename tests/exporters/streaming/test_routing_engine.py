"""End-to-end routing tests for the streaming engine + sinks.

Covers sub-type routing, groups, types selection, declared-but-empty topics,
topic_template collapsing, all six business-rule ExportErrors, Debezium
table_identity, StreamTopicSchemaUnambiguous, determinism, and regression.
Also covers membership Layer-A routing through build_topic_set.

All emits are built in-process (no shared recipe fixture).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from fabulexa_export.config.models import (
    DebeziumConfig,
    DebeziumSourceIdentity,
    MembershipSelection,
    RoutingConfig,
    StreamConfig,
    StreamKindSelection,
)
from fabulexa_export.errors import ExportError
from fabulexa_export.exporters.streaming.driver import stream_export
from fabulexa_export.exporters.streaming.engine import (
    build_topic_set,
    iter_stream_events,
)
from fabulexa_export.reader.emit import open_emit

from ._helpers import _ddl, make_anchor

if TYPE_CHECKING:
    pass

SUPPORTED_VERSION = 4
_DAY = 86_400_000_000_000  # 1 day in nanoseconds

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_RECORD_COLS_ACTOR: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__actor_type", "type": "VARCHAR"},
    {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
]

_RECORD_COLS_DEVICE: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__label", "type": "VARCHAR", "history_tracked": False},
]

# Entity columns — bare-role kind carrying a discriminator column
_RECORD_COLS_ENTITY: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__entity_type", "type": "VARCHAR"},
    {"name": "prop__label", "type": "VARCHAR", "history_tracked": False},
]

# Entity sub-types declared in enum_domains (3 total; type_c intentionally has no rows)
_ENTITY_SUB_TYPES = ("type_a", "type_b", "type_c")

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
    history_rows: list[tuple[Any, ...]] | None = None,
) -> Path:
    """Build a minimal v4 emit with a sub-typed 'actor' kind.

    record_roles maps actor to {customer, vip_customer, staff} sub-types.
    Columns: fork_path, record_id, created_sim_time, active, deactivated_at,
    last_mutation_sim_time, prop__actor_type, prop__name.
    """
    if history_rows is None:
        history_rows = []

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__actor", _RECORD_COLS_ACTOR))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph = ", ".join("?" for _ in _RECORD_COLS_ACTOR)
    for row in actor_rows:
        conn.execute(f'INSERT INTO "records__actor" VALUES ({ph})', list(row))
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "record_roles": {
            "actor": {
                "customer": "dimension",
                "vip_customer": "dimension",
                "staff": "fact",
            }
        },
        "enum_domains": {
            "actor": {"actor_type": ["customer", "vip_customer", "staff"]}
        },
        "tables": [
            _table_spec(
                "records__actor",
                "records",
                _RECORD_COLS_ACTOR,
                len(actor_rows),
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _build_actor_device_emit(
    tmp_path: Path,
    actor_rows: list[tuple[Any, ...]],
    device_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]] | None = None,
) -> Path:
    """Build a v4 emit with sub-typed 'actor' and non-sub-typed 'device'."""
    if history_rows is None:
        history_rows = []

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__actor", _RECORD_COLS_ACTOR))
    conn.execute(_ddl("records__device", _RECORD_COLS_DEVICE))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph_actor = ", ".join("?" for _ in _RECORD_COLS_ACTOR)
    for row in actor_rows:
        conn.execute(f'INSERT INTO "records__actor" VALUES ({ph_actor})', list(row))

    ph_device = ", ".join("?" for _ in _RECORD_COLS_DEVICE)
    for row in device_rows:
        conn.execute(f'INSERT INTO "records__device" VALUES ({ph_device})', list(row))

    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "record_roles": {
            "actor": {
                "customer": "dimension",
                "vip_customer": "dimension",
                "staff": "fact",
            }
        },
        "enum_domains": {
            "actor": {"actor_type": ["customer", "vip_customer", "staff"]}
        },
        "tables": [
            _table_spec(
                "records__actor",
                "records",
                _RECORD_COLS_ACTOR,
                len(actor_rows),
                record_kind="actor",
            ),
            _table_spec(
                "records__device",
                "records",
                _RECORD_COLS_DEVICE,
                len(device_rows),
                record_kind="device",
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _build_nonsubtyped_emit(
    tmp_path: Path,
    kind: str,
    rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]] | None = None,
) -> Path:
    """Build a minimal v4 emit with one non-sub-typed kind, NO record_roles."""
    if history_rows is None:
        history_rows = []

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", _RECORD_COLS_DEVICE))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph = ", ".join("?" for _ in _RECORD_COLS_DEVICE)
    for row in rows:
        conn.execute(f'INSERT INTO "records__{kind}" VALUES ({ph})', list(row))
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _table_spec(
                f"records__{kind}",
                "records",
                _RECORD_COLS_DEVICE,
                len(rows),
                record_kind=kind,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _build_entity_emit(
    tmp_path: Path,
    entity_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]] | None = None,
) -> Path:
    """Build a v4 emit with a bare-role 'entity' kind carrying enum_domains.

    record_roles maps entity to a bare "dimension" role; enum_domains[entity][entity_type]
    declares three sub-types so subtype_values("entity") returns ("type_a", "type_b", "type_c").
    Columns: fork_path, record_id, created_sim_time, active, deactivated_at,
    last_mutation_sim_time, prop__entity_type, prop__label.
    """
    if history_rows is None:
        history_rows = []

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__entity", _RECORD_COLS_ENTITY))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph = ", ".join("?" for _ in _RECORD_COLS_ENTITY)
    for row in entity_rows:
        conn.execute(f'INSERT INTO "records__entity" VALUES ({ph})', list(row))
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "record_roles": {"entity": "dimension"},
        "enum_domains": {"entity": {"entity_type": list(_ENTITY_SUB_TYPES)}},
        "tables": [
            _table_spec(
                "records__entity",
                "records",
                _RECORD_COLS_ENTITY,
                len(entity_rows),
                record_kind="entity",
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------


def _actor_config(
    routing: RoutingConfig | None = None,
    types: list[str] | None = None,
) -> StreamConfig:
    """Build a StreamConfig for 'actor' with optional routing and types."""
    return StreamConfig(
        content="state-changes",
        routing=routing,
        kinds=[
            StreamKindSelection(
                kind="actor",
                properties=[],
                types=types or [],
            )
        ],
    )


def _entity_config(
    routing: RoutingConfig | None = None,
    types: list[str] | None = None,
) -> StreamConfig:
    """Build a StreamConfig for 'entity' with optional routing and types."""
    return StreamConfig(
        content="state-changes",
        routing=routing,
        kinds=[
            StreamKindSelection(
                kind="entity",
                properties=[],
                types=types or [],
            )
        ],
    )


def _debezium_source() -> DebeziumSourceIdentity:
    return DebeziumSourceIdentity(
        connector="postgresql",
        name="myserver",
        db="testdb",
        **{"schema": "public"},
        version="1.9.0.Final",
    )


def _debezium_config(schemas_enable: bool = True) -> DebeziumConfig:
    return DebeziumConfig(source=_debezium_source(), schemas_enable=schemas_enable)


# Sample actor rows: (fork_path, record_id, created_sim_time, active,
#   deactivated_at, last_mutation_sim_time, prop__actor_type, prop__name)
_CUSTOMER_ROW = ("trunk", "c1", 1 * _DAY, True, None, 1 * _DAY, "customer", "Alice")
_VIP_ROW = ("trunk", "v1", 2 * _DAY, True, None, 2 * _DAY, "vip_customer", "Bob")
_STAFF_ROW = ("trunk", "s1", 3 * _DAY, True, None, 3 * _DAY, "staff", "Charlie")

# Sample entity rows: (fork_path, record_id, created_sim_time, active,
#   deactivated_at, last_mutation_sim_time, prop__entity_type, prop__label)
# type_c intentionally has no rows to exercise declared-but-empty topic coverage
_ENTITY_TYPE_A_ROW = ("trunk", "e1", 1 * _DAY, True, None, 1 * _DAY, "type_a", "Alpha")
_ENTITY_TYPE_B_ROW = ("trunk", "e2", 2 * _DAY, True, None, 2 * _DAY, "type_b", "Beta")

# ---------------------------------------------------------------------------
# Sub-typed kind default routing
# ---------------------------------------------------------------------------


class TestSubTypedDefaultRouting:
    """actor (customer/vip_customer/staff) => one topic per sub-type by default."""

    def test_each_subtype_gets_own_topic(self, tmp_path: Path) -> None:
        """Default routing: route_table == sub_type; one topic per sub-type."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config()
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        topics = {e.topic for e in events}
        assert topics == {"customer", "vip_customer", "staff"}

    def test_route_table_equals_sub_type(self, tmp_path: Path) -> None:
        """route_table is the sub-type value for a sub-typed kind."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config()
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        for event in events:
            assert event.route_table == event.topic  # default routing

    def test_kind_field_on_jsonl_stays_actor(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The kind field on the JSONL object stays 'actor' regardless of sub-type."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW])
        config = _actor_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        customer_file = out_dir / "customer.jsonl"
        assert customer_file.exists()
        lines = [
            ln
            for ln in customer_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) >= 1
        for line in lines:
            obj = json.loads(line)
            assert obj["kind"] == "actor"


# ---------------------------------------------------------------------------
# Groups regrouping
# ---------------------------------------------------------------------------


class TestGroupsRegrouping:
    """groups={premium: [customer, vip_customer]} => premium.jsonl + staff.jsonl."""

    def test_groups_produces_correct_topic_files(self, tmp_path: Path) -> None:
        """Groups regrouping: premium.jsonl and staff.jsonl are created."""
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(routing=routing)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        assert (out_dir / "premium.jsonl").exists()
        assert (out_dir / "staff.jsonl").exists()
        assert not (out_dir / "customer.jsonl").exists()
        assert not (out_dir / "vip_customer.jsonl").exists()
        assert outcome.events_per_topic["premium"] == 2
        assert outcome.events_per_topic["staff"] == 1

    def test_global_seq_preserved_across_regroup(self, tmp_path: Path) -> None:
        """Global seq is preserved verbatim across the regroup."""
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(routing=routing)
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        seqs = [e.seq for e in events]
        assert seqs == list(range(1, len(events) + 1))
        # premium events have seq < staff events (customer at t=1_DAY, vip at t=2_DAY,
        # staff at t=3_DAY) and seq is globally monotonic
        premium_seqs = [e.seq for e in events if e.topic == "premium"]
        staff_seqs = [e.seq for e in events if e.topic == "staff"]
        assert all(ps < ss for ps in premium_seqs for ss in staff_seqs)


# ---------------------------------------------------------------------------
# types= selection pre-merge
# ---------------------------------------------------------------------------


class TestTypesSelection:
    """types=[customer, vip_customer] drops staff rows pre-merge."""

    def test_types_drops_unselected_subtype(self, tmp_path: Path) -> None:
        """Staff rows do not appear when types=[customer, vip_customer]."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(types=["customer", "vip_customer"])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        record_ids = {e.record_id for e in events}
        assert "s1" not in record_ids  # staff dropped
        assert "c1" in record_ids
        assert "v1" in record_ids

    def test_seq_numbers_only_emitted_events(self, tmp_path: Path) -> None:
        """seq is gap-free for only the emitted (non-dropped) events."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(types=["customer", "vip_customer"])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        seqs = [e.seq for e in events]
        assert seqs == list(range(1, len(events) + 1))
        # Staff row contributes no events
        assert len(events) == 2  # customer + vip_customer create events only

    def test_dropped_subtype_contributes_no_events(self, tmp_path: Path) -> None:
        """A dropped sub-type contributes zero events."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _STAFF_ROW])
        config = _actor_config(types=["customer"])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        topics = {e.topic for e in events}
        assert "staff" not in topics
        assert "customer" in topics


# ---------------------------------------------------------------------------
# Declared-but-empty topic
# ---------------------------------------------------------------------------


class TestDeclaredButEmptyTopic:
    """A groups target / selected sub-type with zero rows yields an empty file."""

    def test_empty_group_target_yields_empty_file(self, tmp_path: Path) -> None:
        """A groups target with zero matching rows creates an empty .jsonl and count=0."""
        # Only staff row; premium = [customer, vip_customer] matches nothing
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        emit_dir = _build_actor_emit(tmp_path, [_STAFF_ROW])
        config = _actor_config(routing=routing)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        premium_file = out_dir / "premium.jsonl"
        assert premium_file.exists()
        assert premium_file.read_text(encoding="utf-8") == ""
        assert outcome.events_per_topic["premium"] == 0
        assert outcome.events_per_topic["staff"] == 1

    def test_empty_topic_stdout_writes_no_bytes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """stdout sink writes no bytes for an empty topic but still reports zero count."""
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        emit_dir = _build_actor_emit(tmp_path, [_STAFF_ROW])
        config = _actor_config(routing=routing)
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="stdout", out=None, anchor=None
            )

        captured = capsys.readouterr()
        # stdout has no customer/vip_customer lines
        for line in captured.out.splitlines():
            if line.strip():
                obj = json.loads(line)
                assert obj.get("kind") == "actor"
        assert outcome.events_per_topic["premium"] == 0
        assert "premium" in outcome.events_per_topic


# ---------------------------------------------------------------------------
# topic_template={kind} collapse
# ---------------------------------------------------------------------------


class TestTopicTemplateKindCollapse:
    """topic_template='{kind}' collapses all sub-types of a kind into one topic."""

    def test_kind_template_collapses_all_subtypes(self, tmp_path: Path) -> None:
        """All three actor sub-types route to the single 'actor' topic."""
        routing = RoutingConfig(topic_template="{kind}")
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(routing=routing)
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        topics = {e.topic for e in events}
        assert topics == {"actor"}

    def test_kind_template_deterministic(self, tmp_path: Path) -> None:
        """Two runs with the same routing config produce identical event sequences."""
        routing = RoutingConfig(topic_template="{kind}")
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(routing=routing)
        with open_emit(emit_dir) as emit:
            events1 = list(iter_stream_events(emit, config, None))
        with open_emit(emit_dir) as emit:
            events2 = list(iter_stream_events(emit, config, None))

        assert [(e.seq, e.topic, e.record_id) for e in events1] == [
            (e.seq, e.topic, e.record_id) for e in events2
        ]


# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------


class TestBusinessRules:
    """Each business rule raises ExportError with its documented message."""

    def test_stream_types_require_subtyping(self, tmp_path: Path) -> None:
        """types on a non-sub-typed kind raises ExportError (registry must be present).

        Uses an emit that HAS a record_roles registry (actor is sub-typed, device is
        not), so StreamTypesRequireRegistry does not fire first.
        """
        device_row = ("trunk", "d1", 1 * _DAY, True, None, 1 * _DAY, "x")
        emit_dir = _build_actor_device_emit(tmp_path, [_CUSTOMER_ROW], [device_row])
        config = StreamConfig(
            content="state-changes",
            kinds=[StreamKindSelection(kind="device", properties=[], types=["sensor"])],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "kind 'device' is not sub-typed;"
                    " remove 'types' \\(sub-type selection requires a sub-typed kind\\)"
                ),
            ):
                iter_stream_events(emit, config, None)

    def test_no_enum_domain_raises_stream_types_require_subtyping(
        self, tmp_path: Path
    ) -> None:
        """Removing record_roles is no longer an error; lacking enum_domains actor_type raises.

        StreamTypesRequireRegistry no longer exists. A types request against a bundle
        whose enum_domains lacks the kind's <kind>_type domain now raises ExportError
        as 'not sub-typed' (StreamTypesRequireSubtyping).
        """
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW])
        sidecar_path = emit_dir / "base.json"
        raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
        # Removing record_roles is no longer an error by itself
        del raw["record_roles"]
        # Strip enum_domains so actor has empty subtype_values — triggers the real error
        del raw["enum_domains"]
        sidecar_path.write_text(json.dumps(raw), encoding="utf-8")

        config = StreamConfig(
            content="state-changes",
            kinds=[
                StreamKindSelection(kind="actor", properties=[], types=["customer"])
            ],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "kind 'actor' is not sub-typed;"
                    " remove 'types' \\(sub-type selection requires a sub-typed kind\\)"
                ),
            ):
                iter_stream_events(emit, config, None)

    def test_stream_types_declared(self, tmp_path: Path) -> None:
        """A types value not in declared sub-types raises ExportError."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW])
        config = StreamConfig(
            content="state-changes",
            kinds=[
                StreamKindSelection(
                    kind="actor", properties=[], types=["nonexistent_type"]
                )
            ],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="kind 'actor' has no sub-type 'nonexistent_type'",
            ):
                iter_stream_events(emit, config, None)

    def test_stream_template_placeholders(self, tmp_path: Path) -> None:
        """topic_template referencing absent placeholder raises ExportError."""
        # {sub_type} is valid for actor, but absent for non-sub-typed kinds.
        # Build actor+device emit; device has no sub_type attribute.
        actor_row = _CUSTOMER_ROW
        device_row = ("trunk", "d1", 1 * _DAY, True, None, 1 * _DAY, "x")
        emit_dir = _build_actor_device_emit(tmp_path, [actor_row], [device_row])
        routing = RoutingConfig(topic_template="{sub_type}")
        config = StreamConfig(
            content="state-changes",
            routing=routing,
            kinds=[
                StreamKindSelection(kind="actor", properties=[]),
                StreamKindSelection(kind="device", properties=[]),
            ],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="topic_template references 'sub_type', absent for non-sub-typed kind 'device'",
            ):
                iter_stream_events(emit, config, None)

    def test_stream_group_members_resolve(self, tmp_path: Path) -> None:
        """A groups member that no route renders raises ExportError."""
        routing = RoutingConfig(groups={"combined": ["customer", "nonexistent_route"]})
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(routing=routing)
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "routing.groups member 'nonexistent_route'"
                    " matches no streamed route \\(target 'combined'\\)"
                ),
            ):
                iter_stream_events(emit, config, None)


# ---------------------------------------------------------------------------
# Debezium table_identity
# ---------------------------------------------------------------------------


class TestDebeziumTableIdentity:
    """source.table / schema follow table_identity setting."""

    def test_source_table_identity_uses_route_table(self, tmp_path: Path) -> None:
        """table_identity='source_table' => source.table == route_table (sub_type)."""
        routing = RoutingConfig(table_identity="source_table")
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW])
        config = StreamConfig(
            content="state-changes",
            routing=routing,
            kinds=[StreamKindSelection(kind="actor", properties=[])],
            debezium=_debezium_config(schemas_enable=False),
        )
        anchor = make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="debezium", sink="file", out=out_dir, anchor=anchor
            )

        customer_file = out_dir / "customer.jsonl"
        assert customer_file.exists()
        lines = [
            ln
            for ln in customer_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) >= 1
        msg = json.loads(lines[0])
        # bare payload (schemas_enable=False)
        assert msg["source"]["table"] == "customer"  # route_table == sub_type

    def test_topic_identity_uses_topic(self, tmp_path: Path) -> None:
        """table_identity='topic' => source.table == topic."""
        routing = RoutingConfig(table_identity="topic")
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW])
        config = StreamConfig(
            content="state-changes",
            routing=routing,
            kinds=[StreamKindSelection(kind="actor", properties=[])],
            debezium=_debezium_config(schemas_enable=False),
        )
        anchor = make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            stream_export(
                emit, config, fmt="debezium", sink="file", out=out_dir, anchor=anchor
            )

        # With default topic_template={route_table}, topic == route_table == sub_type
        customer_file = out_dir / "customer.jsonl"
        assert customer_file.exists()
        lines = [
            ln
            for ln in customer_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) >= 1
        msg = json.loads(lines[0])
        assert msg["source"]["table"] == "customer"  # topic == 'customer'


# ---------------------------------------------------------------------------
# StreamTopicSchemaUnambiguous
# ---------------------------------------------------------------------------


class TestStreamTopicSchemaUnambiguous:
    """Cross-kind topic with schemas_enable raises ExportError."""

    def test_cross_kind_topic_raises_with_schemas_enable(self, tmp_path: Path) -> None:
        """table_identity='topic' + debezium + schemas_enable + cross-kind => ExportError."""
        # Use topic_template="{kind}" so both actor sub-types AND device collapse to
        # separate topics. To get a cross-kind topic we need a template that maps
        # two different kinds to the same name. Use a literal constant template.
        routing = RoutingConfig(topic_template="events", table_identity="topic")
        actor_row = _CUSTOMER_ROW
        device_row = ("trunk", "d1", 1 * _DAY, True, None, 1 * _DAY, "x")
        emit_dir = _build_actor_device_emit(tmp_path, [actor_row], [device_row])
        config = StreamConfig(
            content="state-changes",
            routing=routing,
            kinds=[
                StreamKindSelection(kind="actor", properties=[]),
                StreamKindSelection(kind="device", properties=[]),
            ],
            debezium=_debezium_config(schemas_enable=True),
        )
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="per-topic Debezium schema is ambiguous",
            ):
                stream_export(
                    emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
                )

    def test_cross_kind_topic_allowed_when_schemas_disabled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same cross-kind config with schemas_enable=False is allowed (short-circuit)."""
        routing = RoutingConfig(topic_template="events", table_identity="topic")
        actor_row = _CUSTOMER_ROW
        device_row = ("trunk", "d1", 1 * _DAY, True, None, 1 * _DAY, "x")
        emit_dir = _build_actor_device_emit(tmp_path, [actor_row], [device_row])
        config = StreamConfig(
            content="state-changes",
            routing=routing,
            kinds=[
                StreamKindSelection(kind="actor", properties=[]),
                StreamKindSelection(kind="device", properties=[]),
            ],
            debezium=_debezium_config(schemas_enable=False),
        )
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="debezium", sink="stdout", out=None, anchor=anchor
            )

        assert outcome.events_per_topic["events"] >= 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same emit + same routing => identical topic set and per-topic event sequences."""

    def test_identical_runs_produce_identical_output(self, tmp_path: Path) -> None:
        """Two runs of the same emit + config produce byte-identical file output."""
        routing = RoutingConfig(groups={"premium": ["customer", "vip_customer"]})
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config(routing=routing)
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()

        with open_emit(emit_dir) as emit:
            outcome1 = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out1, anchor=None
            )
        with open_emit(emit_dir) as emit:
            outcome2 = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out2, anchor=None
            )

        assert outcome1.events_per_topic == outcome2.events_per_topic
        assert outcome1.total_events == outcome2.total_events

        for topic in outcome1.events_per_topic:
            f1 = (out1 / f"{topic}.jsonl").read_text(encoding="utf-8")
            f2 = (out2 / f"{topic}.jsonl").read_text(encoding="utf-8")
            assert f1 == f2, f"topic '{topic}' differs between runs"


# ---------------------------------------------------------------------------
# Regression: no routing block on non-sub-typed fixtures
# ---------------------------------------------------------------------------


class TestRegressionNoRoutingBlock:
    """No routing block over non-sub-typed fixtures => per-kind behavior unchanged."""

    def test_no_routing_block_produces_per_kind_output(self, tmp_path: Path) -> None:
        """Without a routing block, non-sub-typed kinds route to <kind>.jsonl."""
        device_row = ("trunk", "d1", 1 * _DAY, True, None, 1 * _DAY, "gadget")
        emit_dir = _build_nonsubtyped_emit(tmp_path, "device", [device_row])
        config = StreamConfig(
            content="state-changes",
            kinds=[StreamKindSelection(kind="device", properties=[])],
        )
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()

        with open_emit(emit_dir) as emit:
            outcome1 = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out1, anchor=None
            )
        # Re-run identical config
        with open_emit(emit_dir) as emit:
            outcome2 = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out2, anchor=None
            )

        # Both produce device.jsonl with the same content (byte-identical)
        f1 = (out1 / "device.jsonl").read_text(encoding="utf-8")
        f2 = (out2 / "device.jsonl").read_text(encoding="utf-8")
        assert f1 == f2
        assert outcome1.events_per_topic == outcome2.events_per_topic
        assert "device" in outcome1.events_per_topic

    def test_no_routing_topic_equals_kind(self, tmp_path: Path) -> None:
        """With no routing block, topic == route_table == kind for non-sub-typed."""
        device_row = ("trunk", "d1", 1 * _DAY, True, None, 1 * _DAY, "gadget")
        emit_dir = _build_nonsubtyped_emit(tmp_path, "device", [device_row])
        config = StreamConfig(
            content="state-changes",
            kinds=[StreamKindSelection(kind="device", properties=[])],
        )
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        for event in events:
            assert event.topic == "device"
            assert event.route_table == "device"


# ---------------------------------------------------------------------------
# Membership column definitions
# ---------------------------------------------------------------------------

_MEM_QUEUE_WAITERS_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEM_TEAM_MEMBERS_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
]


def _membership_table_spec(
    name: str,
    cols: list[dict[str, Any]],
    rows: int,
    record_kind: str,
    property_name: str,
) -> dict[str, Any]:
    """Build a sidecar table spec for a membership table."""
    return {
        "name": name,
        "category": "membership",
        "columns": cols,
        "rows": rows,
        "record_kind": record_kind,
        "property": property_name,
    }


def _build_two_membership_emit(
    tmp_path: Path,
    waiters_rows: list[tuple[Any, ...]],
    members_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a v4 emit with membership__queue__waiters and membership__team__members.

    queue__waiters carries elem__priority; team__members carries elem__role.
    Both tables may have rows or be empty (for declared-but-empty topic testing).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl("membership__queue__waiters", _MEM_QUEUE_WAITERS_COLS))
    ph_w = ", ".join("?" for _ in _MEM_QUEUE_WAITERS_COLS)
    for row in waiters_rows:
        conn.execute(
            f'INSERT INTO "membership__queue__waiters" VALUES ({ph_w})', list(row)
        )

    conn.execute(_ddl("membership__team__members", _MEM_TEAM_MEMBERS_COLS))
    ph_m = ", ".join("?" for _ in _MEM_TEAM_MEMBERS_COLS)
    for row in members_rows:
        conn.execute(
            f'INSERT INTO "membership__team__members" VALUES ({ph_m})', list(row)
        )

    conn.close()

    sidecar: dict[str, Any] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _membership_table_spec(
                "membership__queue__waiters",
                _MEM_QUEUE_WAITERS_COLS,
                len(waiters_rows),
                "queue",
                "waiters",
            ),
            _membership_table_spec(
                "membership__team__members",
                _MEM_TEAM_MEMBERS_COLS,
                len(members_rows),
                "team",
                "members",
            ),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def _membership_config(
    memberships: list[dict[str, Any]],
    routing: RoutingConfig | None = None,
) -> StreamConfig:
    """Build a StreamConfig for content='membership-events'."""
    return StreamConfig(
        content="membership-events",
        routing=routing,
        memberships=[MembershipSelection(**m) for m in memberships],
    )


# Membership rows: (fork_path, record_id, joined_sim_time, left_sim_time, elem__*)
_WAITER_ROW_CLOSED = ("trunk", "w1", 1 * _DAY, 3 * _DAY, "high")  # join + leave
_WAITER_ROW_OPEN = ("trunk", "w2", 2 * _DAY, None, "low")  # join only
_MEMBER_ROW = ("trunk", "m1", 1 * _DAY, None, "lead")  # join only

_MEM_QUEUE_TASKS_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__label", "type": "VARCHAR"},
]

_TASK_ROW_OPEN = ("trunk", "t1", 1 * _DAY, None, "urgent")  # join only


def _build_two_same_owner_membership_emit(tmp_path: Path) -> Path:
    """Build a v4 emit with membership__queue__waiters and membership__queue__tasks.

    Both tables are owned by 'queue'. queue__waiters carries elem__priority;
    queue__tasks carries elem__label. Used for owner_kind template collapse tests.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl("membership__queue__waiters", _MEM_QUEUE_WAITERS_COLS))
    ph_w = ", ".join("?" for _ in _MEM_QUEUE_WAITERS_COLS)
    conn.execute(
        f'INSERT INTO "membership__queue__waiters" VALUES ({ph_w})',
        list(_WAITER_ROW_CLOSED),
    )

    conn.execute(_ddl("membership__queue__tasks", _MEM_QUEUE_TASKS_COLS))
    ph_t = ", ".join("?" for _ in _MEM_QUEUE_TASKS_COLS)
    conn.execute(
        f'INSERT INTO "membership__queue__tasks" VALUES ({ph_t})',
        list(_TASK_ROW_OPEN),
    )
    conn.close()

    sidecar: dict[str, Any] = {
        "base_format_version": SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _membership_table_spec(
                "membership__queue__waiters",
                _MEM_QUEUE_WAITERS_COLS,
                1,
                "queue",
                "waiters",
            ),
            _membership_table_spec(
                "membership__queue__tasks",
                _MEM_QUEUE_TASKS_COLS,
                1,
                "queue",
                "tasks",
            ),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Membership Layer-A routing through build_topic_set
# ---------------------------------------------------------------------------


class TestMembershipBuildTopicSet:
    """build_topic_set for membership-events content: Layer-A routing coverage."""

    def test_default_topic_template_one_topic_per_table(self, tmp_path: Path) -> None:
        """Default topic_template='{route_table}' gives one topic per membership table."""
        emit_dir = _build_two_membership_emit(
            tmp_path, [_WAITER_ROW_CLOSED], [_MEMBER_ROW]
        )
        config = _membership_config(
            [
                {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
                {"owner_kind": "team", "property": "members", "fields": ["role"]},
            ]
        )
        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)

        assert "queue__waiters" in topic_set
        assert "team__members" in topic_set
        assert len(topic_set) == 2

    def test_owner_kind_template_collapses_two_tables_to_one_topic(
        self, tmp_path: Path
    ) -> None:
        """topic_template='{owner_kind}' collapses two same-owner membership tables.

        Two tables with different `fields` (heterogeneous after-image shapes) both
        route to '{owner_kind}'. JSONL imposes no per-topic schema constraint so
        this is valid; the after-images differ in key set but the topic is shared.
        Uses two tables owned by 'queue' to demonstrate collapse onto one topic.
        """
        emit_dir = _build_two_same_owner_membership_emit(tmp_path)
        routing = RoutingConfig(topic_template="{owner_kind}")
        config = _membership_config(
            [
                {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
                {"owner_kind": "queue", "property": "tasks", "fields": ["label"]},
            ],
            routing=routing,
        )

        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)

        # Both tables collapse to the single topic 'queue'
        assert topic_set == ("queue",)

    def test_owner_kind_template_heterogeneous_after_images_same_topic(
        self, tmp_path: Path
    ) -> None:
        """Heterogeneous after-image shapes collapse onto one topic via topic_template.

        queue__waiters carries elem__priority; team__members carries elem__role.
        Both have owner_kind != the other, but we override topic_template to a
        literal constant 'membership' so both route to the same topic.
        """
        emit_dir = _build_two_membership_emit(
            tmp_path, [_WAITER_ROW_CLOSED], [_MEMBER_ROW]
        )
        routing = RoutingConfig(topic_template="membership")
        config = _membership_config(
            [
                {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
                {"owner_kind": "team", "property": "members", "fields": ["role"]},
            ],
            routing=routing,
        )

        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)

        # Both tables collapse onto one topic 'membership'
        assert topic_set == ("membership",)

    def test_heterogeneous_after_images_both_topics_present_in_events(
        self, tmp_path: Path
    ) -> None:
        """Events from two tables with different fields route to separate topics.

        queue__waiters events carry elem__priority in after; team__members events
        carry elem__role. Default routing puts them on separate topics.
        """
        emit_dir = _build_two_membership_emit(
            tmp_path, [_WAITER_ROW_OPEN], [_MEMBER_ROW]
        )
        config = _membership_config(
            [
                {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
                {"owner_kind": "team", "property": "members", "fields": ["role"]},
            ]
        )

        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        topics = {e.topic for e in events}
        assert "queue__waiters" in topics
        assert "team__members" in topics

        # After-images differ: queue__waiters has elem__priority, team__members has elem__role
        waiters_events = [e for e in events if e.topic == "queue__waiters"]
        members_events = [e for e in events if e.topic == "team__members"]
        assert all(e.after is not None for e in waiters_events)
        assert all("elem__priority" in (e.after or {}) for e in waiters_events)
        assert all(e.after is not None for e in members_events)
        assert all("elem__role" in (e.after or {}) for e in members_events)


# ---------------------------------------------------------------------------
# Declared-but-empty membership topic
# ---------------------------------------------------------------------------


class TestMembershipDeclaredButEmptyTopic:
    """A selected membership table present in the emit but yielding zero events."""

    def test_empty_table_appears_in_build_topic_set(self, tmp_path: Path) -> None:
        """A selected table with no rows still appears in build_topic_set."""
        # queue__waiters has rows; team__members is empty
        emit_dir = _build_two_membership_emit(tmp_path, [_WAITER_ROW_OPEN], [])
        config = _membership_config(
            [
                {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
                {"owner_kind": "team", "property": "members", "fields": ["role"]},
            ]
        )

        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)

        assert "queue__waiters" in topic_set
        assert "team__members" in topic_set

    def test_empty_table_yields_zero_events_and_empty_file(
        self, tmp_path: Path
    ) -> None:
        """An empty membership table yields zero events and an empty .jsonl file."""
        emit_dir = _build_two_membership_emit(tmp_path, [_WAITER_ROW_OPEN], [])
        config = _membership_config(
            [
                {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
                {"owner_kind": "team", "property": "members", "fields": ["role"]},
            ]
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        assert outcome.events_per_topic["team__members"] == 0
        assert outcome.events_per_topic["queue__waiters"] >= 1

        members_file = out_dir / "team__members.jsonl"
        assert members_file.exists()
        assert members_file.read_text(encoding="utf-8") == ""

    def test_empty_table_topic_set_order_follows_config(self, tmp_path: Path) -> None:
        """Topic set enumeration follows memberships config order, empty tables included."""
        # Config lists queue__waiters first, team__members second
        emit_dir = _build_two_membership_emit(tmp_path, [_WAITER_ROW_OPEN], [])
        config = _membership_config(
            [
                {"owner_kind": "queue", "property": "waiters", "fields": ["priority"]},
                {"owner_kind": "team", "property": "members", "fields": ["role"]},
            ]
        )

        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)

        assert topic_set[0] == "queue__waiters"
        assert topic_set[1] == "team__members"

    def test_only_empty_table_in_config_yields_declared_but_empty(
        self, tmp_path: Path
    ) -> None:
        """When only the empty table is selected, its topic appears with zero events."""
        emit_dir = _build_two_membership_emit(tmp_path, [_WAITER_ROW_OPEN], [])
        # Select only team__members (which has no rows)
        config = _membership_config(
            [
                {"owner_kind": "team", "property": "members", "fields": ["role"]},
            ]
        )
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        assert "team__members" in outcome.events_per_topic
        assert outcome.events_per_topic["team__members"] == 0
        assert outcome.total_events == 0

        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)

        assert "team__members" in topic_set


# ---------------------------------------------------------------------------
# Bare-role discriminator kind split
# ---------------------------------------------------------------------------


class TestBareRoleDiscriminatorSplit:
    """A bare-role kind carrying enum_domains[kind][<kind>_type] splits per sub-type."""

    def test_bare_role_splits_into_n_topics_in_declaration_order(
        self, tmp_path: Path
    ) -> None:
        """Headline fix: bare-role kind with N enum_domains sub-types → N topics in order."""
        emit_dir = _build_entity_emit(
            tmp_path, [_ENTITY_TYPE_A_ROW, _ENTITY_TYPE_B_ROW]
        )
        config = _entity_config()
        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)
        # All three declared sub-types appear in declaration order, including type_c (no rows)
        assert topic_set == ("type_a", "type_b", "type_c")

    def test_bare_role_types_scoping_selects_subset(self, tmp_path: Path) -> None:
        """types scoping on bare-role discriminator kind: only selected sub-types stream."""
        emit_dir = _build_entity_emit(
            tmp_path, [_ENTITY_TYPE_A_ROW, _ENTITY_TYPE_B_ROW]
        )
        config = _entity_config(types=["type_a"])
        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))

        topics = {e.topic for e in events}
        assert "type_a" in topics
        assert "type_b" not in topics
        assert "type_c" not in topics

    def test_bare_role_declared_empty_subtype_yields_topic(
        self, tmp_path: Path
    ) -> None:
        """A declared-but-empty sub-type still yields its topic (intent-not-observation)."""
        # type_a and type_b have rows; type_c is declared but has no rows
        emit_dir = _build_entity_emit(
            tmp_path, [_ENTITY_TYPE_A_ROW, _ENTITY_TYPE_B_ROW]
        )
        config = _entity_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)
            outcome = stream_export(
                emit, config, fmt="jsonl", sink="file", out=out_dir, anchor=None
            )

        assert "type_c" in topic_set
        assert outcome.events_per_topic["type_c"] == 0
        assert (out_dir / "type_c.jsonl").exists()
        assert (out_dir / "type_c.jsonl").read_text(encoding="utf-8") == ""

    def test_actor_splits_from_enum_domains(self, tmp_path: Path) -> None:
        """actor splits into sub-types declared in enum_domains, in declaration order."""
        emit_dir = _build_actor_emit(tmp_path, [_CUSTOMER_ROW, _VIP_ROW, _STAFF_ROW])
        config = _actor_config()
        with open_emit(emit_dir) as emit:
            topic_set = build_topic_set(config, emit.sidecar)
        # enum_domains declares: customer, vip_customer, staff (same set as old record_roles)
        assert topic_set == ("customer", "vip_customer", "staff")


# ---------------------------------------------------------------------------
# Re-keyed validation: StreamTypesRequireSubtyping / StreamTypesDeclared
# ---------------------------------------------------------------------------


class TestReKeyedValidation:
    """StreamTypesRequireSubtyping and StreamTypesDeclared re-keyed to enum_domains."""

    def test_stream_types_require_subtyping_empty_subtype_values(
        self, tmp_path: Path
    ) -> None:
        """types non-empty on a kind with empty subtype_values raises ExportError."""
        # Build entity emit without enum_domains so entity has empty subtype_values
        emit_dir = _build_nonsubtyped_emit(tmp_path, "device", [])
        config = StreamConfig(
            content="state-changes",
            kinds=[StreamKindSelection(kind="device", properties=[], types=["widget"])],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "kind 'device' is not sub-typed;"
                    " remove 'types' \\(sub-type selection requires a sub-typed kind\\)"
                ),
            ):
                iter_stream_events(emit, config, None)

    def test_stream_types_declared_outside_enum_domains_raises(
        self, tmp_path: Path
    ) -> None:
        """A types value outside the kind's subtype_values declared set raises ExportError."""
        emit_dir = _build_entity_emit(tmp_path, [_ENTITY_TYPE_A_ROW])
        config = _entity_config(types=["nonexistent_type"])
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match="kind 'entity' has no sub-type 'nonexistent_type'",
            ):
                iter_stream_events(emit, config, None)
