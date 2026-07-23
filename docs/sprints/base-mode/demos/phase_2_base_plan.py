#!/usr/bin/env python
"""
Demo: build_base_plan over the recipe fixture sidecar
Sprint: base-mode
Phase: 2

Builds a base plan over the shared recipe fixture sidecar (tests/recipes,
kinds: patient, doctor, staff, admission, queue) and prints each table's kind,
output name, property set, and rename map. Then demonstrates a
`slice-only-column-omitted` notice — the fixture itself carries no non-exempt
slice_only property, so a synthetic one is added to the loaded sidecar's
`records__doctor` table for the demonstration only — and one rejected rename
(a `rename` naming a table base does not emit).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from fabulexa_forge.config.models import BaseConfig, RenameEntry
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.base.plan import build_base_plan
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.sidecar import Sidecar

_REPO_ROOT = Path(__file__).resolve().parents[4]


def load_recipe_sidecar_raw(dest: Path) -> dict[str, Any]:
    """Build the shared recipe fixture emit and return its parsed base.json."""
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
    from recipes._recipe_fixture import build_recipe_emit

    build_recipe_emit(dest)
    raw: dict[str, Any] = json.loads((dest / "base.json").read_text(encoding="utf-8"))
    return raw


def with_synthetic_slice_only_column(raw: dict[str, Any]) -> Sidecar:
    """Add a non-exempt slice_only property to records__doctor, for the
    slice-only-omission demonstration only — the shared recipe fixture itself
    carries none.
    """
    patched = json.loads(json.dumps(raw))
    for table in patched["tables"]:
        if table["name"] == "records__doctor":
            table["columns"].append(
                {
                    "name": "prop__legacy_code",
                    "type": "VARCHAR",
                    "history_tracked": False,
                    "temporal_class": "slice_only",
                }
            )
    return Sidecar.from_raw(patched)


def print_plan(sidecar: Sidecar) -> None:
    """Build the bare (config=None) base plan and print every table's shape."""
    notices: list[Notice] = []
    plan = build_base_plan(sidecar, None, notices.append)
    print("--- Plan (config=None) ---")
    for spec in plan.tables:
        print(
            f"kind={spec.kind!r} table_name={spec.table_name!r}"
            f" properties={sorted(spec.properties)}"
            f" has_presentation_id={spec.has_presentation_id}"
            f" column_renames={dict(spec.column_renames)}"
        )


def print_slice_only_notice(sidecar: Sidecar) -> None:
    """Show the slice-only-column-omitted notice over the synthetic column."""
    notices: list[Notice] = []
    plan = build_base_plan(sidecar, None, notices.append)
    doctor = next(t for t in plan.tables if t.kind == "doctor")
    print("\n--- Slice-only omission (synthetic prop__legacy_code) ---")
    print(f"doctor properties={sorted(doctor.properties)}")
    for notice in notices:
        print(f"[{notice.code}] {notice.message}")


def print_rejected_rename(sidecar: Sidecar) -> None:
    """Show one rejected rename: a table base does not emit."""
    config = BaseConfig(rename=[RenameEntry(table="records__nonexistent", name="x")])
    print("\n--- Rejected rename ---")
    try:
        build_base_plan(sidecar, config, lambda notice: None)
    except ExportError as exc:
        print(f"FAIL: {exc}")
    else:
        raise AssertionError("expected ExportError, none raised")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        raw = load_recipe_sidecar_raw(dest)
        sidecar = Sidecar.from_raw(raw)

        print_plan(sidecar)
        print_slice_only_notice(with_synthetic_slice_only_column(raw))
        print_rejected_rename(sidecar)

    print("\nSUCCESS: build_base_plan resolved plans, a notice, and one rejection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
