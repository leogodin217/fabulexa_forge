#!/usr/bin/env python
"""
Demo: The event-log reach (`ElectionKindConflict` and the elected `changes`
text) — a `decimal`-elected payload property whose `create` after-image and
`u` `[old, new]` pair carry the elected text in the log's `changes` column,
byte-identical to the declaring table's own column. Then the per-kind
agreement gate: a second declared table left silent on the same audited
property makes a previously-legal pair illegal (`ElectionKindConflict`,
naming both tables); narrowing the property out of the events source's
audited set via `ignore` legalizes the same pair of table declarations.
Sprint: value-rendering-elections
Phase: 4

Builds a scratch emit (one `widget` records kind carrying one tracked,
decimal-electable payload column, with a creation history row and a later
update history row), plans + renders the event log three times: one
declared table electing the property (legal, elected `changes` text); a
second, silent declared table added (refused, `ElectionKindConflict`); the
same pair legalized by narrowing the events source's audited set.
"""

import sys
import tempfile
from pathlib import Path

# The vendored fixture-sidecar authority lives under tests/_support — reused
# here (as pytest itself does) rather than hand-rolling a base.json, so the
# demo's scratch emit is built through the one sidecar-conformant authority
# every fixture in this repo goes through.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.notices import discard_notice_sink  # noqa: E402
from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.anchor import resolve_effective_anchor  # noqa: E402
from fabulexa_forge.config.models import (  # noqa: E402
    ExportConfig,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.errors import ElectionKindConflict  # noqa: E402
from fabulexa_forge.exporters.election import resolve_election  # noqa: E402
from fabulexa_forge.exporters.source.events import build_event_log_sql  # noqa: E402
from fabulexa_forge.exporters.source.plan import build_source_plan  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_MS = 1_000_000  # one sim-time "tick", in nanoseconds

_WIDGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__error_rate", "DOUBLE", history_tracked=True, temporal_class="tracked"
    ),
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
    """Write the demo's scratch emit: one `widget` kind carrying one tracked
    DOUBLE payload column, one record with a creation value and one later
    update — enough for the log to emit a `create` and a `u` event.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    columns_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WIDGET_COLUMNS)
    conn.execute(f'CREATE TABLE "records__widget" ({columns_ddl})')
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w001", 100 * _MS, True, 150 * _MS, 0, 45.6789],
    )
    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "widget", "w001", "error_rate", 100 * _MS, "12.3456"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "widget", "w001", "error_rate", 150 * _MS, "45.6789"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": 1,
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 2,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 200 * _MS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def _build_plan_and_query(
    emit_dir: Path, config: ExportConfig
) -> list[dict[str, object]]:
    """Plan + render the event log for one config over the demo emit.

    Shared by every scenario below — the two refusal scenarios call it only
    to observe the propagated plan-time error.

    Args:
        emit_dir: The scratch emit's directory.
        config: A `mode: source` export config declaring `tables` and
            `events` over the `widget` kind.

    Returns:
        The event log's rendered rows, each an output-column-name -> value
        mapping.
    """
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the demo emit declares a runtime anchor"
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        assert plan.events is not None, "every scenario declares an events block"
        sql = build_event_log_sql(
            emit.sidecar, plan.fork_path, plan.events, anchor, None
        )
        cols = ("id", "item_type", "item_id", "event", "occurred_at", "changes")
        return [dict(zip(cols, row)) for row in emit.query(sql, ())]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        electing_table = SourceTableDecl(
            name="widget_a",
            kind="widget",
            render={"prop__error_rate": {"decimal": [6, 3]}},
        )
        silent_table = SourceTableDecl(name="widget_b", kind="widget")
        audits_all = SourceEventsDecl(
            name="widget_events", sources=(SourceEventSourceDecl(kind="widget"),)
        )

        # --- one declared table electing the property: legal, elected text
        legal_config = ExportConfig(
            mode="source",
            source=SourceConfig(tables=(electing_table,), events=audits_all),
        )
        rows = _build_plan_and_query(emit_dir, legal_config)
        create_row = next(r for r in rows if r["event"] == "create")
        update_row = next(r for r in rows if r["event"] == "update")
        print("event log `changes` (one declared table, elected):")
        print(f"  create: {create_row['changes']}")
        print(f"  update: {update_row['changes']}")
        assert create_row["changes"] == '{"error_rate":[null,"12.346"]}'
        assert update_row["changes"] == '{"error_rate":["12.346","45.679"]}'

        # --- a second, silent declared table: ElectionKindConflict --------
        conflicting_config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(electing_table, silent_table), events=audits_all
            ),
        )
        try:
            _build_plan_and_query(emit_dir, conflicting_config)
        except ElectionKindConflict as exc:
            print(f"ElectionKindConflict fired: {exc}")
            assert "widget_a" in str(exc)
            assert "widget_b" in str(exc)
        else:
            raise AssertionError("expected ElectionKindConflict to fire")

        # --- narrowing the property out of the audited set legalizes it ---
        audits_narrowed = SourceEventsDecl(
            name="widget_events",
            sources=(SourceEventSourceDecl(kind="widget", ignore=("error_rate",)),),
        )
        legalized_config = ExportConfig(
            mode="source",
            source=SourceConfig(
                tables=(electing_table, silent_table), events=audits_narrowed
            ),
        )
        legalized_rows = _build_plan_and_query(emit_dir, legalized_config)
        legalized_create = next(r for r in legalized_rows if r["event"] == "create")
        print(f"legalized (property ignored): {legalized_create['changes']}")
        assert legalized_create["changes"] == "{}"

    print(
        "SUCCESS: the event log's `changes` carries the elected decimal text;"
        " a silent sibling table triggers ElectionKindConflict; `ignore`"
        " narrows the property out of the gate's scope"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
