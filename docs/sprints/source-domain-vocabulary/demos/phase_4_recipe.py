#!/usr/bin/env python
"""
Demo: Recipe -- source-domain-vocabulary
Sprint: source-domain-vocabulary
Phase: 4

Runs the `examples/recipes/source/source-domain-vocabulary/config.yaml`
recipe against the recipe fixture emit via `export_source`, exactly the
corpus gate's own path (`tests/recipes/test_source_recipes.py`), and prints
the two renders the recipe's `kind_labels` / `item_type` / `rename`
declarations touch:

  - the `queue_waiters` junction head -- `patient_kind` cells read `person`,
    the `patient -> person` label applied to the projected
    `member__patient__kind` column.
  - the `versions` event-log head -- the admission source's `item_type`
    override (`encounter`) and `rename` (`ward` -> `department`), and the
    queue/waiters membership source's label-derived default item-type
    (`station.waiters`, `label(queue)="station"` + `.waiters`) with its
    `patient_kind` changes entries labeled `person`.

Each printed row is annotated with which vocabulary declaration produced
it, then asserted so the demo fails loudly if the recipe's own contract
drifts.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# The recipe fixture builder lives under tests/ (an implicit namespace
# package) -- reused here rather than duplicated, mirroring the other
# phase demos' sys.path pattern.
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (_REPO_ROOT, _REPO_ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import duckdb  # noqa: E402

from fabulexa_forge.anchor import resolve_effective_anchor  # noqa: E402
from fabulexa_forge.config.loader import load_export_config  # noqa: E402
from fabulexa_forge.exporters.notices import Notice  # noqa: E402
from fabulexa_forge.exporters.source.engine import export_source  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402
from tests.recipes._recipe_fixture import build_recipe_emit  # noqa: E402

_RECIPE_DIR = (
    _REPO_ROOT / "examples" / "recipes" / "source" / "source-domain-vocabulary"
)


def _discard_notice(notice: Notice) -> None:
    """A no-op NoticeSink -- this demo has nothing to say about notices."""


def _fetch(
    conn: duckdb.DuckDBPyConnection, table: str, columns: tuple[str, ...]
) -> "list[dict[str, object]]":
    """Every row of `table`, as column-name -> value dicts, insertion order."""
    col_list = ", ".join(f'"{c}"' for c in columns)
    rows = conn.execute(f'SELECT {col_list} FROM "{table}" ORDER BY 1').fetchall()
    return [dict(zip(columns, row)) for row in rows]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_recipe_emit(emit_dir)

        config = load_export_config(_RECIPE_DIR / "config.yaml")
        out_path = tmp_path / "out.duckdb"
        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            export_source(
                emit, config, out_path, "duckdb", anchor, notice_sink=_discard_notice
            )

        conn = duckdb.connect(str(out_path), read_only=True)
        try:
            junction_rows = _fetch(
                conn,
                "queue_waiters",
                ("queue_id", "priority", "patient_kind", "patient_id", "left_at"),
            )
            print("queue_waiters (junction head) -- kind_labels: patient -> person")
            for row in junction_rows:
                print(f"  {row}")

            event_rows = _fetch(
                conn,
                "versions",
                ("id", "item_type", "item_id", "event", "changes"),
            )
            print("\nversions (event-log head)")
            for row in event_rows:
                if row["item_type"] == "encounter":
                    why = "item_type override + rename(ward->department)"
                else:
                    why = "item_type = label(queue).waiters; patient_kind labeled"
                print(f"  {row}  # {why}")
        finally:
            conn.close()

        # The junction's labeled member-kind value: every patient_kind cell
        # reads the label, never the verbatim engine kind name.
        assert {r["patient_kind"] for r in junction_rows} == {"person"}

        # The events log: the admission source's override wins over its own
        # (unlabeled) kind name, and its rename relabels the changes key.
        admission_rows = [r for r in event_rows if r["item_type"] == "encounter"]
        assert len(admission_rows) == 3
        assert all('"department"' in str(r["changes"]) for r in admission_rows)
        assert all('"ward"' not in str(r["changes"]) for r in admission_rows)

        # The membership source's un-overridden item-type defaults to
        # label(queue).waiters, and its patient reference field is labeled
        # (never the verbatim kind name "patient") on both the join and the
        # leave half.
        waiters_rows = [r for r in event_rows if r["item_type"] == "station.waiters"]
        assert len(waiters_rows) == 3
        assert all('"person"' in str(r["changes"]) for r in waiters_rows)
        assert all('"patient"' not in str(r["changes"]) for r in waiters_rows)

    print(
        "\nSUCCESS: the source-domain-vocabulary recipe's kind_labels,"
        " item_type override, and rename all render as declared"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
