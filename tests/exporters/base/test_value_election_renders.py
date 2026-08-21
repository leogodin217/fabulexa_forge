"""Render tests for the three new value-rendering elections (`decimal`,
`instant`, `json_precision`) on `build_base_render_sql`
(`docs/architecture/pending/value-rendering-elections.md` § Semantics,
§ Cross-mode identity and determinism).

Each new election's compiled expression is asserted against the shared
authority it composes through (`render_decimal_expr` /
`render_json_precision_expr` / the existing `render_anchor_temporal_expr`),
the same authorities source-mode composes — the two modes' rendered text for
the same value is therefore byte-identical by construction. Structural-instant
shorthand and `date_parse` render behavior is already covered by
`test_renders.py`'s migrated `render` suite; this module tests only what the
unified map's three new typed forms add, plus the cross-cutting guarantees
the design states for them: a no-election config renders byte-identical SQL
to today, an unelected sibling column's cast-back rendering is unaffected,
and a reference-value column's own elected-surface rendering takes priority
over a `render` entry naming it.

Tests build their `BaseTableSpec.render` directly via `dataclasses.replace`
on the fixture's default (unelected) plan — `test_renders.py`'s own
convention — since these tests exercise `build_base_render_sql`, not
`build_base_plan`'s gates (already covered by `test_value_election_plan.py`).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from _support.notices import discard_notice_sink

from fabulexa_forge._sql import render_decimal_expr, render_json_precision_expr
from fabulexa_forge.anchor import render_anchor_temporal_expr, resolve_effective_anchor
from fabulexa_forge.config.models import (
    DecimalElection,
    InstantElection,
    JsonPrecisionElection,
)
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.base.plan import BaseTableSpec, build_base_plan
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.reader.emit import Emit, open_emit

from ._base_fixtures import build_base_value_election_emit, build_reference_edge_emit

# Emission order for the value-election fixture's default (unrenamed) plan:
# the self key (record_index -> widget_key; no reference property, so no edge
# key), STATE_AT_COLUMNS (record_id -> id), then prop__<p> in sidecar
# declaration order (no presentation_id — the fixture omits it).
_COLUMN_ORDER = (
    "widget_key",
    "id",
    "created_sim_time",
    "active",
    "deactivated_at",
    "prop__error_rate",
    "prop__requested_offset_ns",
    "prop__opened_at",
    "prop__context",
)


@contextmanager
def _widget_emit(tmp_path: Path) -> Iterator[tuple[Emit, BaseTableSpec, str]]:
    """Open the value-election fixture emit and resolve its plan and fork_path."""
    emit_dir = build_base_value_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "widget")
        yield emit, spec, fork_path


def _rows(emit: Emit, sql: str) -> list[dict[str, object]]:
    """Execute sql and zip every row against the fixture's default column order."""
    return [dict(zip(_COLUMN_ORDER, row)) for row in emit.query(sql, ())]


# ---------------------------------------------------------------------------
# `decimal`: composes `render_decimal_expr`
# ---------------------------------------------------------------------------


def test_render_decimal_composes_authority_expr(tmp_path: Path) -> None:
    """A `decimal`-elected DOUBLE payload column renders through
    `render_decimal_expr`, in place, and produces the rounded DECIMAL value —
    byte-identical to source mode's compiled expression for the same value."""
    with _widget_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(
            spec, render=(("prop__error_rate", DecimalElection(decimal=(6, 2))),)
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    expected = (
        render_decimal_expr(
            '"_base"."prop__error_rate"', 6, 2, "prop__error_rate", spec.table_name
        )
        + ' AS "prop__error_rate"'
    )
    assert expected in sql
    assert rows["w001"]["prop__error_rate"] == Decimal("12.35")


# ---------------------------------------------------------------------------
# `json_precision`: composes `render_json_precision_expr`
# ---------------------------------------------------------------------------


def test_render_json_precision_composes_authority_expr(tmp_path: Path) -> None:
    """A `json_precision`-elected VARCHAR payload column renders through
    `render_json_precision_expr`, rounding the declared leaf in place while
    preserving every other byte."""
    with _widget_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(
            spec,
            render=(
                (
                    "prop__context",
                    JsonPrecisionElection(json_precision={"discount_pct": 2}),
                ),
            ),
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    expected = (
        render_json_precision_expr(
            '"_base"."prop__context"',
            {"discount_pct": 2},
            "prop__context",
            spec.table_name,
        )
        + ' AS "prop__context"'
    )
    assert expected in sql
    assert rows["w001"]["prop__context"] == '{"discount_pct": 0.13, "note": "vip"}'


# ---------------------------------------------------------------------------
# `instant`: composes the shared wallclock renderer identically to a
# structural instant of the same value
# ---------------------------------------------------------------------------


def test_render_instant_renders_identically_to_structural_instant(
    tmp_path: Path,
) -> None:
    """An `instant`-elected payload BIGINT carrying the same raw ns offset as
    the structural `created_sim_time` renders the identical wallclock value —
    both compile through `render_anchor_temporal_expr`."""
    with _widget_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(
            spec,
            render=(
                (
                    "prop__requested_offset_ns",
                    InstantElection(instant="timestamp"),
                ),
            ),
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    expected = render_anchor_temporal_expr(
        anchor,
        '"_base"."prop__requested_offset_ns"',
        "prop__requested_offset_ns",
        "timestamp",
    )
    assert expected in sql
    assert rows["w001"]["prop__requested_offset_ns"] == rows["w001"]["created_sim_time"]


# ---------------------------------------------------------------------------
# No-election default rendering is unaffected
# ---------------------------------------------------------------------------


def test_render_no_election_byte_identical_to_default(tmp_path: Path) -> None:
    """A table declaring no `render` map at all composes no decimal guard and
    no json_precision call: the default rendering stays a plain cast-back."""
    with _widget_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
    assert 'CAST("_base"."prop__error_rate" AS DOUBLE) AS "prop__error_rate"' in sql
    assert "DECIMAL(" not in sql
    assert "forge_json_precision(" not in sql


# ---------------------------------------------------------------------------
# Cast-back branch unaffected by a sibling column's election
# ---------------------------------------------------------------------------


def test_unelected_sibling_column_cast_back_unaffected(tmp_path: Path) -> None:
    """Electing one column leaves an unelected sibling's cast-back rendering
    untouched — the per-identity dispatch is column-scoped, not table-wide."""
    with _widget_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(
            spec, render=(("prop__error_rate", DecimalElection(decimal=(6, 2))),)
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert rows["w001"]["prop__opened_at"] == "2024-02-01"
    assert 'CAST("_base"."prop__opened_at" AS VARCHAR) AS "prop__opened_at"' in sql


# ---------------------------------------------------------------------------
# A reference-value column's own elected-surface rendering takes priority
# over a `render` entry naming it
# ---------------------------------------------------------------------------


def test_reference_value_column_unaffected_by_render_entry(tmp_path: Path) -> None:
    """A `render` entry naming a reference `prop__<p>` column is superseded
    by that column's own elected-surface rendering (`_render_reference_value`)
    — under a uniform record_id election this is the verbatim CAST, so the
    declared election never applies and no authority call appears in the SQL."""
    emit_dir = build_reference_edge_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        plan = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "actor")
        elected_spec = replace(
            spec,
            render=(
                (
                    "prop__lead_id",
                    JsonPrecisionElection(json_precision={"x": 2}),
                ),
            ),
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        table = emit.query_arrow(sql, ()).to_pydict()
    assert "forge_json_precision(" not in sql
    id_out = elected_spec.column_renames["record_id"]
    lead_out = elected_spec.column_renames.get("ref_index__lead_id", "lead_id_key")
    rows = dict(zip(table[id_out], table["prop__lead_id"]))
    assert rows["a001"] == "t001"
    key_rows = dict(zip(table[id_out], table[lead_out]))
    assert key_rows["a001"] is not None
