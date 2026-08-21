#!/usr/bin/env python
"""
Demo: The unified source-mode `render:` map — `decimal`, `instant`,
`json_precision`, and a relocated `date_parse` elected together for four
payload columns of one declared table, plus two of the new plan-time
refusals: `RenderKeyResolves`' form-domain gate (a typed election naming a
structural column) and `DecimalSourceIsDouble`'s source-type gate (`decimal`
on a non-DOUBLE source).
Sprint: value-rendering-elections
Phase: 2

Builds a scratch emit (one `widget` records kind carrying one payload column
per election kind), plans + renders a `state` table electing all four forms
in a single `render:` map, and prints the rendered row. Then shows the two
refused configs above, each caught and printed.
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
    SourceTableDecl,
)
from fabulexa_forge.errors import DecimalSourceIsDouble, RenderKeyResolves  # noqa: E402
from fabulexa_forge.exporters.election import resolve_election  # noqa: E402
from fabulexa_forge.exporters.source.plan import (  # noqa: E402
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.exporters.source.renders import build_state_render_sql  # noqa: E402
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
        "prop__error_rate", "DOUBLE", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__requested_offset_ns",
        "BIGINT",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__opened_at", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__context", "VARCHAR", history_tracked=False, temporal_class="constant"
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
    """Write the demo's scratch emit: one `widget` kind carrying one payload
    column per election kind (decimal / instant / date_parse /
    json_precision), one row.

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
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "w001",
            0,
            True,
            0,
            0,
            12.3456,
            5 * 3600 * 1_000_000_000,
            "2024-02-01",
            '{"discount_pct": 0.125, "note": "vip"}',
        ],
    )
    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
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
                "rows": 0,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100 * _MS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def _plan_and_render(emit_dir: Path, table: SourceTableDecl) -> list[dict[str, object]]:
    """Plan + render one `widget` table declaration's `state` export.

    Shared by the happy-path render and both refusal demos below — the
    latter two call it only to observe the propagated plan-time error.

    Args:
        emit_dir: The scratch emit's directory.
        table: The `tables[]` declaration to plan (always named "widget").

    Returns:
        The rendered rows, each an output-column-name -> value mapping.
    """
    config = ExportConfig(mode="source", source=SourceConfig(tables=(table,)))
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the demo emit declares a runtime anchor"
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        widget = plan.tables[0]
        assert isinstance(widget, SourceStateTablePlan)
        sql = build_state_render_sql(emit.sidecar, plan.fork_path, widget, anchor, None)
        cols = [out for _, out in widget.columns]
        return [dict(zip(cols, row)) for row in emit.query(sql, ())]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        # --- happy path: one render: map, all four election kinds ----------
        elected_table = SourceTableDecl(
            name="widget",
            kind="widget",
            render={
                "prop__error_rate": {"decimal": [6, 3]},
                "prop__requested_offset_ns": {"instant": "timestamp"},
                "prop__opened_at": {"date_parse": "%Y-%m-%d"},
                "prop__context": {"json_precision": {"discount_pct": 2}},
            },
        )
        (rendered,) = _plan_and_render(emit_dir, elected_table)

        print("rendered row (one render: map, four elections):")
        for key in ("error_rate", "requested_offset_ns", "opened_at", "context"):
            print(f"  {key}: {rendered[key]!r}")

        assert str(rendered["error_rate"]) == "12.346"
        assert rendered["opened_at"].isoformat() == "2024-02-01"
        assert rendered["context"] == '{"discount_pct": 0.13, "note": "vip"}'

        # --- refusal: a typed election naming a structural column ----------
        domain_violation = SourceTableDecl(
            name="widget",
            kind="widget",
            render={"created_sim_time": {"instant": "timestamp"}},
        )
        try:
            _plan_and_render(emit_dir, domain_violation)
        except RenderKeyResolves as exc:
            print(f"RenderKeyResolves fired: {exc}")
        else:
            raise AssertionError("expected RenderKeyResolves to fire")

        # --- refusal: `decimal` on a non-DOUBLE (VARCHAR) source ------------
        type_violation = SourceTableDecl(
            name="widget",
            kind="widget",
            render={"prop__opened_at": {"decimal": [4, 3]}},
        )
        try:
            _plan_and_render(emit_dir, type_violation)
        except DecimalSourceIsDouble as exc:
            print(f"DecimalSourceIsDouble fired: {exc}")
        else:
            raise AssertionError("expected DecimalSourceIsDouble to fire")

    print(
        "SUCCESS: the unified render map elects decimal/instant/date_parse/"
        "json_precision together; both plan-time gates refuse correctly"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
