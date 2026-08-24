#!/usr/bin/env python
"""
Demo: Wire naming + kind vocabulary — bare-name after-images, rename, kind labels
Sprint: streaming-authoring-parity
Phase: 3

Builds a two-kind emit (`person`, `team`) with a membership table
(`membership__person__assignment`, one reference field `owner` -> `team`),
then:

  1. Streams `person` as JSONL: `nickname` rides bare, `sess` rides under its
     `rename` target `session_id`, and the envelope `kind` is the stream's own
     `kind_label` ("Members").
  2. Streams the membership as JSONL: the reference field's `owner_kind` value
     ("team") renders through the config-level `kind_labels` mapping ("Team").
  3. Renders the Debezium value schema for the `person` stream and shows its
     field list equals the rendered after-image's keys, in order.
  4. Shows a `rename` collision refusal (two properties resolving to one
     output key).
  5. Shows a `kind_label` masquerade refusal (a stream's `kind_label` equal
     to a different kind's `kind_labels`-rendered name).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# The vendored fixture-sidecar authority lives under tests/_support — reused
# here (as pytest itself does) rather than hand-rolling a base.json.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.notices import discard_notice_sink  # noqa: E402
from _support.sidecar_builder import identity_column  # noqa: E402
from _support.sidecar_builder import write_emit as _write_sidecar  # noqa: E402

from fabulexa_forge.config.models import (  # noqa: E402
    DebeziumSourceIdentity,
    KindStream,
    MembershipStream,
    StreamConfig,
)
from fabulexa_forge.errors import (  # noqa: E402
    StreamKindLabelCollision,
    StreamOutputNameCollision,
)
from fabulexa_forge.exporters.streaming.driver import _build_value_schemas  # noqa: E402
from fabulexa_forge.exporters.streaming.engine import iter_stream_events  # noqa: E402
from fabulexa_forge.exporters.streaming.jsonl import (  # noqa: E402
    render_jsonl_object,
)
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_PERSON_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__sess",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__nickname",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_TEAM_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__owner__kind", "type": "VARCHAR"},
    {"name": "member__owner__id", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_demo_emit(tmp_path: Path) -> Path:
    """Write the demo's scratch emit: `person`, `team`, and their membership.

    `p1` is a person created with `sess='s1'`, later updated to `sess='s2'`,
    carrying the constant `nickname='ace'`. `p1` also holds an `assignment`
    membership interval referencing team `t1`.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    person_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _PERSON_COLUMNS)
    conn.execute(f'CREATE TABLE "records__person" ({person_ddl})')
    conn.execute(
        'INSERT INTO "records__person" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p1", 0, True, None, 5_000_000, 0, "s1", "ace"],
    )

    team_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _TEAM_COLUMNS)
    conn.execute(f'CREATE TABLE "records__team" ({team_ddl})')
    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "t1", 0, True, None, 0, 0],
    )

    membership_ddl = ", ".join(
        f'"{c["name"]}" {c["type"]}' for c in _MEMBERSHIP_COLUMNS
    )
    conn.execute(f'CREATE TABLE "membership__person__assignment" ({membership_ddl})')
    conn.execute(
        'INSERT INTO "membership__person__assignment" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "p1", 0, None, "team", "t1"],
    )

    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.executemany(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        [
            ("trunk", "person", "p1", "sess", 0, "s1"),
            ("trunk", "person", "p1", "sess", 5_000_000, "s2"),
        ],
    )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__person",
                "category": "records",
                "columns": _PERSON_COLUMNS,
                "rows": 1,
                "record_kind": "person",
            },
            {
                "name": "records__team",
                "category": "records",
                "columns": _TEAM_COLUMNS,
                "rows": 1,
                "record_kind": "team",
            },
            {
                "name": "membership__person__assignment",
                "category": "membership",
                "columns": _MEMBERSHIP_COLUMNS,
                "rows": 1,
                "record_kind": "person",
                "property": "assignment",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 2,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100_000_000}],
    )
    return tmp_path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        people_stream = KindStream(
            name="people",
            kind="person",
            properties=["sess", "nickname"],
            rename={"sess": "session_id"},
            kind_label="Members",
        )
        person_config = StreamConfig(content="state-changes", streams=[people_stream])

        assignments_stream = MembershipStream(
            name="assignments",
            membership={"kind": "person", "property": "assignment"},
            fields=["owner"],
        )
        membership_config = StreamConfig(
            content="membership-events",
            kind_labels={"team": "Team"},
            streams=[assignments_stream],
        )

        with open_emit(emit_dir) as emit:
            # 1. Bare `nickname`, renamed `session_id`, labeled envelope kind.
            person_events = list(
                iter_stream_events(emit, person_config, None, discard_notice_sink)
            )
            print("person stream (kind_label='Members', rename sess->session_id):")
            for event in person_events:
                obj = render_jsonl_object(event)
                print(f"  op={obj['op']} kind={obj['kind']} after={obj['after']}")
            assert {e.kind for e in person_events} == {"Members"}
            create_event = next(e for e in person_events if e.op == "c")
            assert create_event.after is not None
            assert create_event.after["nickname"] == "ace"
            assert create_event.after["session_id"] == "s1"
            assert "sess" not in create_event.after

        with open_emit(emit_dir) as emit:
            # 2. kind_labels-mapped member-kind value.
            membership_events = list(
                iter_stream_events(emit, membership_config, None, discard_notice_sink)
            )
            print("\nassignments stream (kind_labels={'team': 'Team'}):")
            for event in membership_events:
                obj = render_jsonl_object(event)
                print(f"  op={obj['op']} after={obj['after']}")
            join_event = next(e for e in membership_events if e.op == "join")
            assert join_event.after is not None
            assert join_event.after["owner_kind"] == "Team"

        with open_emit(emit_dir) as emit:
            # 3. Debezium value schema field list == rendered after-image keys.
            source_identity = DebeziumSourceIdentity(
                connector="postgresql",
                name="demo",
                db="d",
                schema="s",
                version="1.0",
            )
            schemas = _build_value_schemas(
                emit, person_config, source_identity, "topic"
            )
            schema_fields = [
                f["field"] for f in schemas["people"]["fields"][1]["fields"]
            ]
            rendered_keys = list(create_event.after.keys())
            print(f"\nDebezium schema fields: {schema_fields}")
            print(f"Rendered after-image keys: {rendered_keys}")
            assert schema_fields == rendered_keys

        with open_emit(emit_dir) as emit:
            # 4. rename collision refusal.
            colliding_stream = KindStream(
                name="bad",
                kind="person",
                properties=["sess", "nickname"],
                rename={"sess": "nickname"},
            )
            colliding_config = StreamConfig(
                content="state-changes", streams=[colliding_stream]
            )
            try:
                list(
                    iter_stream_events(
                        emit, colliding_config, None, discard_notice_sink
                    )
                )
            except StreamOutputNameCollision as exc:
                print(f"\nrename-collision refusal: {exc}")
            else:
                raise AssertionError("expected StreamOutputNameCollision")

        with open_emit(emit_dir) as emit:
            # 5. kind_label masquerade refusal.
            masquerade_stream = KindStream(
                name="masquerade",
                kind="person",
                properties=[],
                kind_label="Team",
            )
            masquerade_config = StreamConfig(
                content="state-changes",
                kind_labels={"team": "Team"},
                streams=[masquerade_stream],
            )
            try:
                list(
                    iter_stream_events(
                        emit, masquerade_config, None, discard_notice_sink
                    )
                )
            except StreamKindLabelCollision as exc:
                print(f"kind_label masquerade refusal: {exc}")
            else:
                raise AssertionError("expected StreamKindLabelCollision")

    print(
        "\nSUCCESS: bare/renamed after-image keys, kind_label envelope override,"
        " kind_labels member-kind mapping, and both naming/vocabulary refusals"
        " all verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
