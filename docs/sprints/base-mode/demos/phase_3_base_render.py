#!/usr/bin/env python
"""
Demo: build_base_render_sql over the recipe fixture emit
Sprint: base-mode
Phase: 3

Builds the bare (config=None) base plan over the shared recipe fixture emit
(tests/recipes, kind: patient — history-tracked prop__status, seeded
pending@1*DAY, active@2*DAY, discharged@3*DAY), then renders `patient` twice:
once at the tape's end (horizon_ns=None) and once at an inclusive
`slice_at: 2*DAY` (horizon_ns = 2*DAY + 1). Prints both result sets side by
side so the as-of difference in prop__status — "discharged" at the tape's
end vs "active" at the 2*DAY horizon — is visible.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.base.plan import BaseTableSpec, build_base_plan
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import Emit, open_emit

_REPO_ROOT = Path(__file__).resolve().parents[4]

_DAY = 86_400_000_000_000  # one civil day, in sim-time nanoseconds


def build_recipe_emit_dir(dest: Path) -> Path:
    """Build the shared recipe fixture emit into dest and return it."""
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
    from recipes._recipe_fixture import build_recipe_emit

    build_recipe_emit(dest)
    return dest


def render_patient(
    emit: Emit, spec: BaseTableSpec, fork_path: str, horizon_ns: int | None
) -> list[tuple[str, object, object]]:
    """Render `patient` at horizon_ns and return (id, status, created_at) rows."""
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, horizon_ns)
    rows = emit.query(sql, ())
    # Column order (no presentation_id on patient): id, created_sim_time,
    # active, deactivated_at, then prop__<p> in sidecar declaration order —
    # prop__status is the first prop__ column, at index 4.
    return [(row[0], row[4], row[1]) for row in rows]


def print_side_by_side(
    end_rows: list[tuple[str, object, object]],
    horizon_rows: list[tuple[str, object, object]],
) -> None:
    """Print both result sets' (id, status) pairs side by side."""
    end_by_id = {row[0]: row[1] for row in end_rows}
    horizon_by_id = {row[0]: row[1] for row in horizon_rows}
    print(f"{'id':<6} {'tape end status':<18} {'2*DAY-horizon status':<20}")
    for record_id in sorted(end_by_id):
        print(
            f"{record_id:<6} {end_by_id[record_id]:<18}"
            f" {horizon_by_id.get(record_id, '<absent>'):<20}"
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = build_recipe_emit_dir(Path(tmp))
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            notices: list[Notice] = []
            plan = build_base_plan(emit.sidecar, None, notices.append)
            spec = next(t for t in plan.tables if t.kind == "patient")

            end_rows = render_patient(emit, spec, fork_path, None)
            horizon_rows = render_patient(emit, spec, fork_path, 2 * _DAY + 1)

    print_side_by_side(end_rows, horizon_rows)

    end_status = {r[0]: r[1] for r in end_rows}
    horizon_status = {r[0]: r[1] for r in horizon_rows}
    assert end_status["p001"] == "discharged"
    assert horizon_status["p001"] == "active"

    print(
        "\nSUCCESS: build_base_render_sql reconstructed p001.prop__status as"
        " 'discharged' at the tape's end and 'active' at the 2*DAY horizon"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
