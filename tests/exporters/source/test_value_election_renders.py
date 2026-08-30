"""Render tests for the three new value-rendering elections (`decimal`,
`instant`, `json_precision`) on `build_state_render_sql` /
`build_junction_render_sql`
(`docs/architecture/pending/value-rendering-elections.md` § Semantics,
§ Cross-mode identity and determinism).

Each new election's compiled expression is asserted against the shared
authority it composes through (`render_decimal_expr` / `render_json_precision_expr`
/ the existing `render_anchor_temporal_expr`), and executed end-to-end against
a real row to confirm the rendered value. Structural-instant shorthand and
`date_parse` render behavior is already covered by `test_renders.py`'s
migrated `render` suite; this module tests only what the unified map's three
new typed forms add, plus the two cross-cutting guarantees the design states
for them: a no-election config renders byte-identical SQL to today, and an
elected column composes with `rename` (source-name addressing preserved).
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from _support.notices import discard_notice_sink

from fabulexa_forge._sql import render_decimal_expr, render_json_precision_expr
from fabulexa_forge.anchor import render_anchor_temporal_expr, resolve_effective_anchor
from fabulexa_forge.config.models import (
    DecimalElection,
    ExportConfig,
    InstantElection,
    JsonPrecisionElection,
    MembershipRef,
    SourceConfig,
    SourceTableDecl,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourcePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.exporters.source.renders import (
    build_junction_render_sql,
    build_state_render_sql,
)
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import build_source_test_emit, build_value_election_source_emit

if TYPE_CHECKING:
    from fabulexa_forge.reader.emit import Emit

# ---------------------------------------------------------------------------
# Plan-building + row-mapping helpers
# ---------------------------------------------------------------------------


@contextmanager
def _plan(
    emit_dir: Path, tables: "tuple[SourceTableDecl, ...]"
) -> "Iterator[tuple[Emit, SourcePlan]]":
    """Open `emit_dir` and build a SourcePlan over `tables`, resolving the
    anchor and election the way the engine does."""
    config = ExportConfig(mode="source", source=SourceConfig(tables=tables))
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        yield emit, plan


def _state(plan: SourcePlan, name: str) -> SourceStateTablePlan:
    """The sole `state` unit named `name`."""
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceStateTablePlan)
    return table


def _junction(plan: SourcePlan, name: str) -> SourceJunctionTablePlan:
    """The sole `junction` unit named `name`."""
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceJunctionTablePlan)
    return table


def _mapped_rows(
    emit: "Emit", table: "SourceStateTablePlan | SourceJunctionTablePlan", sql: str
) -> list[dict[str, object]]:
    """Execute sql and zip every row against `table`'s output column names."""
    cols = [out for _, out in table.columns]
    return [dict(zip(cols, row)) for row in emit.query(sql, ())]


# ---------------------------------------------------------------------------
# `decimal`: composes `render_decimal_expr`
# ---------------------------------------------------------------------------


def test_state_render_decimal_composes_authority_expr(tmp_path: Path) -> None:
    """A `decimal`-elected DOUBLE payload column renders through
    `render_decimal_expr`, in place, and produces the rounded DECIMAL value."""
    tables = (
        SourceTableDecl(
            name="orders",
            kind="order",
            render={"prop__amount": DecimalElection(decimal=(6, 2))},
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "orders")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    expected = (
        render_decimal_expr('"_rec"."prop__amount"', 6, 2, "prop__amount", "orders")
        + ' AS "amount"'
    )
    assert expected in sql
    assert rows[0]["amount"] == Decimal("250.50")


def test_state_render_decimal_composes_with_rename(tmp_path: Path) -> None:
    """`decimal` composes with `rename`: the elected column stays addressable
    by its source name in `render`, and the renamed output name lands in
    `table.columns`."""
    tables = (
        SourceTableDecl(
            name="orders",
            kind="order",
            rename={"prop__amount": "total"},
            render={"prop__amount": DecimalElection(decimal=(6, 2))},
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "orders")
        assert table.render == (("prop__amount", DecimalElection(decimal=(6, 2))),)
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert rows[0]["total"] == Decimal("250.50")


# ---------------------------------------------------------------------------
# `json_precision`: composes `render_json_precision_expr`
# ---------------------------------------------------------------------------


def test_state_render_json_precision_composes_authority_expr(tmp_path: Path) -> None:
    """A `json_precision`-elected VARCHAR payload column renders through
    `render_json_precision_expr`, rounding the declared leaf in place while
    preserving every other byte."""
    tables = (
        SourceTableDecl(
            name="widgets",
            kind="widget",
            render={
                "prop__context": JsonPrecisionElection(
                    json_precision={"discount_pct": 2}
                )
            },
        ),
    )
    with _plan(build_value_election_source_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "widgets")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    expected = (
        render_json_precision_expr(
            '"_rec"."prop__context"', {"discount_pct": 2}, "prop__context", "widgets"
        )
        + ' AS "context"'
    )
    assert expected in sql
    assert rows[0]["context"] == '{"discount_pct": 0.13, "note": "vip"}'


# ---------------------------------------------------------------------------
# `instant`: composes the shared wallclock renderer identically to a
# structural instant of the same value
# ---------------------------------------------------------------------------


def test_state_render_instant_renders_identically_to_structural_instant(
    tmp_path: Path,
) -> None:
    """An `instant`-elected payload BIGINT carrying the same raw ns offset as
    a structural instant column renders the identical wallclock value —
    both compile through `render_anchor_temporal_expr`."""
    tables = (
        SourceTableDecl(
            name="widgets",
            kind="widget",
            render={"prop__requested_offset_ns": InstantElection(instant="timestamp")},
        ),
    )
    with _plan(build_value_election_source_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "widgets")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    expected = render_anchor_temporal_expr(
        plan.anchor,
        '"_rec"."prop__requested_offset_ns"',
        "requested_offset_ns",
        "timestamp",
    )
    assert expected in sql
    assert rows[0]["requested_offset_ns"] == rows[0]["created_at"]


# ---------------------------------------------------------------------------
# No-election default rendering is unaffected
# ---------------------------------------------------------------------------


def test_state_render_no_election_byte_identical_to_default(tmp_path: Path) -> None:
    """A table declaring no `render` map at all composes no decimal guard and
    no json_precision call: the default rendering stays verbatim passthrough."""
    tables = (SourceTableDecl(name="orders", kind="order"),)
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "orders")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    assert '"_rec"."prop__amount" AS "amount"' in sql
    assert "DECIMAL(" not in sql
    assert "forge_json_precision(" not in sql


# ---------------------------------------------------------------------------
# Junction dispatch: the same typed-form compiler over `elem__<f>`
# ---------------------------------------------------------------------------


def test_junction_render_json_precision_composes_authority_expr_on_elem_field(
    tmp_path: Path,
) -> None:
    """A `json_precision`-elected `elem__<f>` element column composes the
    same authority call the state render uses, keyed on the junction's own
    field name."""
    tables = (
        SourceTableDecl(
            name="visit_team",
            membership=MembershipRef(kind="visit", property="team"),
            render={"elem__role_name": JsonPrecisionElection(json_precision={"x": 2})},
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _junction(plan, "visit_team")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    expected = (
        render_json_precision_expr(
            '"_mem"."elem__role_name"', {"x": 2}, "elem__role_name", "visit_team"
        )
        + ' AS "role_name"'
    )
    assert expected in sql
