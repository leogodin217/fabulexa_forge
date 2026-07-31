#!/usr/bin/env python
"""
Demo: The event-log render, standalone (`exporters/source/events.py`)
Sprint: source-declared-tables
Phase: 2

Renders and executes a `versions` event log over a small fixture emit: one
records source over a tracked `job` kind, audited with `only: [status]` (a
second tracked property, `priority`, is declared but excluded — narrower
than the kind's full audited set), and one membership source over
`job.crew`. `SourceEventSourcePlan` / `SourceEventLogPlan` are
hand-constructed directly (Phase 2 ships the render standalone; a later
phase wires a plan builder to produce them).

Shows:
  1. A records `create` row: every audited property `[null, value]`.
  2. A records `update` row: the differing entry only.
  3. A records `destroy` row: `item_id` is never NULL (an identity join,
     not the fold's nulled after-image), `[last value, null]`.
  4. A membership `create` (join) / `destroy` (leave) pair from one closed
     interval, and a still-open interval's lone `create` row — the
     join/leave -> create/destroy recode.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.source.events import (
    SourceEventLogPlan,
    SourceEventSourcePlan,
    build_event_log_sql,
)
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_ANCHOR = EffectiveAnchor(
    start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc), timezone=ZoneInfo("UTC")
)

_JOB_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__priority",
        "type": "BIGINT",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_CREW_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
]

# job001: created (c, status=queued), one status update (u, running), then
# deactivated with no further status change (d, "running" carries forward).
_JOB_ROWS: list[tuple[object, ...]] = [
    ("trunk", "job001", 0, False, 30, 30, 0, "running", 1),
]
_JOB_HISTORY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "job", "job001", "status", 0, "queued"),
    ("trunk", "job", "job001", "priority", 0, "1"),
    ("trunk", "job", "job001", "status", 10, "running"),
]

# crew: one closed interval (lead, joined 5 / left 25) and one still-open
# interval (support, joined 26).
_CREW_ROWS: list[tuple[object, ...]] = [
    ("trunk", "job001", 5, 25, "lead"),
    ("trunk", "job001", 26, None, "support"),
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _insert_all(
    conn: "duckdb.DuckDBPyConnection",
    table: str,
    cols: list[dict[str, object]],
    rows: list[tuple[object, ...]],
) -> None:
    placeholders = ", ".join("?" for _ in cols)
    for row in rows:
        conn.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', list(row))


def build_emit(emit_dir: Path) -> None:
    """Write the event-log demo emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__job", _JOB_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_ddl("membership__job__crew", _CREW_COLUMNS))

    _insert_all(conn, "records__job", _JOB_COLUMNS, _JOB_ROWS)
    _insert_all(conn, "history", _HISTORY_COLUMNS, _JOB_HISTORY_ROWS)
    _insert_all(conn, "membership__job__crew", _CREW_COLUMNS, _CREW_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 999}],
        "tables": [
            {
                "name": "records__job",
                "category": "records",
                "record_kind": "job",
                "columns": _JOB_COLUMNS,
                "rows": len(_JOB_ROWS),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_JOB_HISTORY_ROWS),
            },
            {
                "name": "membership__job__crew",
                "category": "membership",
                "record_kind": "job",
                "property": "crew",
                "columns": _CREW_COLUMNS,
                "rows": len(_CREW_ROWS),
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_versions_log_plan() -> SourceEventLogPlan:
    """One `versions` log: a records source narrowed to `only: [status]`
    (`priority` is tracked but excluded), and a membership source over
    `job.crew`. No election in this demo, so every surface is `record_id`."""
    records_source = SourceEventSourcePlan(
        item_type="job",
        kind="job",
        property=None,
        populations=(Population(kind="job", sub_type=None),),
        audited_properties=("status",),
        item_surface=((None, "record_id"),),
        change_edges=(),
    )
    membership_source = SourceEventSourcePlan(
        item_type="job.crew",
        kind="job",
        property="crew",
        populations=(Population(kind="job", sub_type=None),),
        audited_properties=("role",),
        item_surface=((None, "record_id"),),
        change_edges=(),
    )
    return SourceEventLogPlan(
        name="versions",
        sources=(records_source, membership_source),
        item_id_type="VARCHAR",
    )


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        build_emit(emit_dir)
        log = build_versions_log_plan()

        with open_emit(emit_dir) as emit:
            sql = build_event_log_sql(emit.sidecar, _FORK_PATH, log, _ANCHOR, None)
            rows = emit.query(sql, ())

        print(
            "=== versions event log"
            " (item_type, item_id, event, occurred_at, changes) ==="
        )
        for row in rows:
            print(f"  {row}")
        print()

        by_key = {(r[0], r[1], r[2]): r for r in rows}

        # ---- 1. Create: every audited property [null, value] --------------
        job_create = by_key[("job", "job001", "create")]
        if json.loads(job_create[4]) != {"status": [None, "queued"]}:
            raise _fail(
                f"job create changes should be status:[null,queued]: {job_create}"
            )
        print(f"  OK: create -> {job_create[4]}")

        # ---- 2. Update: exactly the differing entry ------------------------
        job_update = by_key[("job", "job001", "update")]
        if json.loads(job_update[4]) != {"status": ["queued", "running"]}:
            raise _fail(f"job update changes wrong: {job_update}")
        print(f"  OK: update -> {job_update[4]}")

        # ---- 3. Destroy: item_id never NULL, [last, null] ------------------
        job_destroy = by_key[("job", "job001", "destroy")]
        if job_destroy[1] is None:
            raise _fail("destroy row's item_id is NULL — should be the identity join")
        if json.loads(job_destroy[4]) != {"status": ["running", None]}:
            raise _fail(f"job destroy changes wrong: {job_destroy}")
        print(f"  OK: destroy -> item_id={job_destroy[1]!r}, changes={job_destroy[4]}")
        print()

        # ---- 4. Membership: join/leave -> create/destroy -------------------
        # (item_type, item_id, event) is not a per-row key for a membership
        # source — one owner logs one create per joining member, so the two
        # crew intervals share it; select by the role each carries instead.
        crew_creates = [r for r in rows if r[0] == "job.crew" and r[2] == "create"]
        crew_destroys = [r for r in rows if r[0] == "job.crew" and r[2] == "destroy"]
        if len(crew_creates) != 2 or len(crew_destroys) != 1:
            raise _fail(
                f"expected 2 crew creates + 1 destroy (still-open interval never"
                f" closes): got {len(crew_creates)} creates, {len(crew_destroys)}"
                f" destroys"
            )
        lead_create = next(
            r for r in crew_creates if json.loads(r[4])["role"][1] == "lead"
        )
        lead_destroy = crew_destroys[0]
        if json.loads(lead_destroy[4]) != {"role": ["lead", None]}:
            raise _fail(f"crew leave changes wrong: {lead_destroy}")
        print(f"  OK: crew join  -> {lead_create[4]}")
        print(f"  OK: crew leave -> {lead_destroy[4]}")
        print("  OK: the still-open 'support' interval contributes a create only")
        print()

        print(
            "SUCCESS: event-log render composes create/update/destroy from the"
            " row-state-events fold and create/destroy from the membership-events"
            " fold, with a never-NULL destroy item_id and the join/leave recode"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
