#!/usr/bin/env python
"""
Demo: The unified base-mode `render:` map — the temporal shorthand,
`decimal`, `instant`, `json_precision`, and a relocated `date_parse` elected
together on one `records__<kind>` table's entry (`{table, render}`), plus the
anchor-required refusal for an `instant` election with no resolved anchor
(base's optional-anchor posture — `TemporalRenderRequiresAnchor`).
Sprint: value-rendering-elections
Phase: 3

Builds a scratch emit (one `widget` records kind carrying one payload column
per typed election kind), plans + renders a base table electing all forms in
a single `BaseRenderDecl.render` map, and prints the rendered row. Then shows
the same table's `instant` election refused at plan time when no anchor
resolves.
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

from fabulexa_forge.anchor import (  # noqa: E402
    EffectiveAnchor,
    resolve_effective_anchor,
)
from fabulexa_forge.config.models import BaseConfig, BaseRenderDecl  # noqa: E402
from fabulexa_forge.derivations.guard import require_single_branch  # noqa: E402
from fabulexa_forge.errors import TemporalRenderRequiresAnchor  # noqa: E402
from fabulexa_forge.exporters.base.plan import BasePlan, build_base_plan  # noqa: E402
from fabulexa_forge.exporters.base.renders import build_base_render_sql  # noqa: E402
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
    column per typed election kind (decimal / instant / date_parse /
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


def _plan(
    emit_dir: Path, decl: BaseRenderDecl, anchor: "EffectiveAnchor | None"
) -> BasePlan:
    """Resolve the `widget` table's base plan for one `render` declaration.

    Shared by the happy-path render and the anchor-refusal demo below — the
    latter calls it only to observe the propagated plan-time error.

    Args:
        emit_dir: The scratch emit's directory.
        decl: The `base.render` entry to plan (always targets `records__widget`).
        anchor: The resolved effective anchor, or None to demonstrate the
            anchor-required refusal.

    Returns:
        The resolved `BasePlan`.
    """
    with open_emit(emit_dir) as emit:
        return build_base_plan(
            emit.sidecar,
            BaseConfig(render=[decl]),
            discard_notice_sink,
            anchor=anchor,
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        # --- happy path: one render: map, shorthand + three new elections --
        elected_decl = BaseRenderDecl(
            table="records__widget",
            render={
                "created_sim_time": "date",
                "prop__error_rate": {"decimal": [6, 3]},
                "prop__requested_offset_ns": {"instant": "timestamp"},
                "prop__opened_at": {"date_parse": "%Y-%m-%d"},
                "prop__context": {"json_precision": {"discount_pct": 2}},
            },
        )
        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None, "the demo emit declares a runtime anchor"
            fork_path = require_single_branch(emit.sidecar)
            plan = build_base_plan(
                emit.sidecar,
                BaseConfig(render=[elected_decl]),
                discard_notice_sink,
                anchor=anchor,
            )
            spec = plan.tables[0]
            sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
            table = emit.query_arrow(sql, ()).to_pydict()
        rendered = {name: values[0] for name, values in table.items()}

        print("rendered row (one render: map, shorthand + three elections):")
        for key in (
            "created_sim_time",
            "prop__error_rate",
            "prop__requested_offset_ns",
            "prop__opened_at",
            "prop__context",
        ):
            print(f"  {key}: {rendered[key]!r}")

        assert rendered["created_sim_time"].isoformat() == "2024-01-01"
        assert str(rendered["prop__error_rate"]) == "12.346"
        assert rendered["prop__opened_at"].isoformat() == "2024-02-01"
        assert rendered["prop__context"] == '{"discount_pct": 0.13, "note": "vip"}'

        # --- refusal: `instant` election with no resolved anchor -----------
        instant_only = BaseRenderDecl(
            table="records__widget",
            render={"prop__requested_offset_ns": {"instant": "timestamp"}},
        )
        try:
            _plan(emit_dir, instant_only, None)
        except TemporalRenderRequiresAnchor as exc:
            print(f"TemporalRenderRequiresAnchor fired: {exc}")
        else:
            raise AssertionError("expected TemporalRenderRequiresAnchor to fire")

    print(
        "SUCCESS: the unified base render map elects the temporal shorthand"
        " plus decimal/instant/date_parse/json_precision together; the"
        " anchor-required gate refuses instant with no resolved anchor"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
