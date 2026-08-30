"""Result-level determinism gate over the recipe corpus.

Exports every recipe in every DuckDB-producing mode TWICE from a freshly opened
emit and asserts the two datasets are identical table-for-table, row-for-row, in
scan order. This is the Deterministic invariant (CLAUDE.md § Key Invariants)
checked against output rather than against code.

**Why this exists separately from the other determinism tests.** The suite already
carries determinism tests under ``tests/exporters/`` — but they compare *compiled
SQL strings*. A render whose SQL is byte-identical every run can still return rows
in a different order or with different values, if the SQL itself under-specifies
an ordering. That is exactly what shipped: the source event log's before-image
window ordered by ``event_sim_time`` alone, so two events sharing an instant
resolved arbitrarily, and no SQL-comparing test could see it. Comparing results
catches the class; comparing SQL cannot.

Verified honestly: this gate does **not** reproduce that particular bug. No
record in the recipe emit carries two events at one instant, so the tie never
fires here — ``TestCoincidentUpdateAndDestroy`` in
``tests/exporters/source/test_events_render.py`` is what pins that condition,
against a fixture built to contain it. What this gate adds is breadth: every
table of every recipe in every mode, watched for a class of defect that had no
result-level coverage at all. Its value is prospective, not retrospective.

**Why the recipe corpus and not the examples.** ``tools/qa/determinism.sh`` runs
the same comparison against ``docs/examples/*/``, which is richer data — but those
bundles are gitignored, so that gate cannot run anywhere but a developer's
machine. The recipe emit is built from a fixture (DuckDB + stdlib, no producer),
so this runs on every checkout including CI. The shell gate stays as the
larger-data complement; neither replaces the other.

Scope: the three modes that write a DuckDB file of named tables. The corrupter
and streaming corpora write other artifacts (a base bundle plus defects.json,
and JSONL respectively) and would need their own comparison; they are out of
scope here rather than overlooked.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.reader.emit import open_emit

from ._harness import RecipeFolder, discover_recipes

_RECIPES_ROOT = Path(__file__).parent.parent.parent / "examples" / "recipes"

#: mode name -> (corpus root, export engine). All three engines share one
#: signature, so the dispatch is a table rather than three near-identical tests.
_MODE_CORPORA: dict[str, tuple[Path, Callable[..., Any]]] = {
    "dimensional": (_RECIPES_ROOT, export_dimensional),
    "source": (_RECIPES_ROOT / "source", export_source),
    "base": (_RECIPES_ROOT / "base", export_base),
}

# Collect once at module import so parametrize IDs are stable.
_ALL: list[tuple[str, RecipeFolder]] = [
    (mode, recipe)
    for mode, (root, _) in _MODE_CORPORA.items()
    for recipe in discover_recipes(root)
]


def _export_once(
    mode: str, recipe: RecipeFolder, emit_dir: Path, out_path: Path
) -> None:
    """Run one export of `recipe` in `mode` to `out_path`.

    Opens the emit fresh so the two runs a determinism check compares share no
    reader state — the same independence two CLI invocations would have.
    """
    _, engine = _MODE_CORPORA[mode]
    config = load_export_config(recipe.config_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(
            emit.sidecar.runtime(),
            config.rebase,
            None,
            None,
        )
        engine(
            emit,
            config,
            out_path,
            "duckdb",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )


def _table_names(con: duckdb.DuckDBPyConnection, alias: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "select table_name from duckdb_tables() where database_name = ?", [alias]
        ).fetchall()
    }


def _columns(con: duckdb.DuckDBPyConnection, alias: str, table: str) -> list[str]:
    return [row[0] for row in con.execute(f'describe {alias}."{table}"').fetchall()]


def _rows(
    con: duckdb.DuckDBPyConnection, alias: str, table: str
) -> list[tuple[Any, ...]]:
    """Every row in scan order.

    Fetched whole and compared as an ordered list rather than diffed with
    ``EXCEPT``: set difference would call two runs equal when they differ only in
    row order or in duplicate multiplicity, and row order is exactly what an
    under-specified ORDER BY gets wrong. The recipe emit is small enough that
    materializing it costs nothing.
    """
    return con.execute(f'select * from {alias}."{table}"').fetchall()


@pytest.mark.parametrize(("mode", "recipe"), _ALL, ids=lambda v: getattr(v, "name", v))
def test_recipe_export_is_deterministic(
    mode: str, recipe: RecipeFolder, recipe_emit_dir: Path, tmp_path: Path
) -> None:
    """Two exports of one recipe produce identical datasets."""
    out_a = tmp_path / f"{recipe.name}-a.duckdb"
    out_b = tmp_path / f"{recipe.name}-b.duckdb"
    _export_once(mode, recipe, recipe_emit_dir, out_a)
    _export_once(mode, recipe, recipe_emit_dir, out_b)

    con = duckdb.connect(":memory:")
    con.execute(f"attach '{out_a}' as a (read_only)")
    con.execute(f"attach '{out_b}' as b (read_only)")

    tables_a = _table_names(con, "a")
    assert tables_a == _table_names(con, "b"), (
        f"{mode}/{recipe.name}: the two runs wrote different table sets"
    )
    assert tables_a, f"{mode}/{recipe.name}: the export wrote no tables at all"

    for table in sorted(tables_a):
        assert _columns(con, "a", table) == _columns(con, "b", table), (
            f"{mode}/{recipe.name}/{table}: column names or order differ between runs"
        )
        rows_a = _rows(con, "a", table)
        rows_b = _rows(con, "b", table)
        assert len(rows_a) == len(rows_b), (
            f"{mode}/{recipe.name}/{table}: row counts differ between runs "
            f"({len(rows_a)} vs {len(rows_b)})"
        )
        first_diff = next(
            (i for i, (ra, rb) in enumerate(zip(rows_a, rows_b)) if ra != rb), None
        )
        assert first_diff is None, (
            f"{mode}/{recipe.name}/{table}: rows differ between runs, first at "
            f"index {first_diff}:\n  run A: {rows_a[first_diff]}\n"
            f"  run B: {rows_b[first_diff]}"
        )


def test_determinism_corpus_nonempty() -> None:
    """Every mode contributes at least one recipe to the parametrization.

    Without this, a corpus that moved or emptied would turn the gate above into
    zero tests and stay green.
    """
    covered = {mode for mode, _ in _ALL}
    assert covered == set(_MODE_CORPORA), (
        f"determinism gate covers {sorted(covered)}; expected every mode in "
        f"{sorted(_MODE_CORPORA)}. A corpus root moved or emptied."
    )
