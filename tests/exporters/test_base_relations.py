"""Tests for the `base_relations` compile indirection (`exporters/base_relations.py`)
and its threading through both pure compile surfaces.

Covers: `base_relations=None` byte-identical to the unparameterized compile (both
modes); `shadow_base_relations`'s CTE wrap, including a compiled query that already
opens with its own `WITH`; the self-read-binds-physical binding rule; total
shadowing across a dimensional FK hop and a source change-log read (history +
records spine), with an unmapped name falling back physical.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink

from exporters._emit_fixtures import build_test_emit
from exporters.dimensional.test_fk import (
    _dim,
    _fact,
    _fk_col,
    _from_col,
    build_reference_chain_emit,
)
from exporters.source._source_fixtures import build_source_test_emit
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.derivations import require_single_branch
from fabulexa_forge.exporters.base_relations import (
    apply_base_relations,
    shadow_base_relations,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.grains import build_grain_sql
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.reader.emit import open_emit

_MS = 1_000_000

# ---------------------------------------------------------------------------
# base_relations=None: byte-identical to the unparameterized compile
# ---------------------------------------------------------------------------


def test_dimensional_none_byte_identical(tmp_path: Path) -> None:
    """base_relations=None compiles the same SQL as calling build_grain_sql directly."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_state_changes",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point",
                        kind="journey_instance",
                        property="state",
                    ),
                    key=["record_id"],
                    columns=[
                        _from_col("record_id", "record_id"),
                        _from_col("new_state", "value"),
                    ],
                )
            ]
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

        sidecar = emit.sidecar
        fork_path = require_single_branch(sidecar)
        table_decl = config.tables[0]
        source_table_name = validate_table(
            table_decl, config, sidecar, None, discard_notice_sink
        )
        sql_direct, _, _, _ = build_grain_sql(
            table_decl, source_table_name, sidecar, None, fork_path, config, None
        )

    assert specs[0].sql == sql_direct


def test_source_none_byte_identical(tmp_path: Path) -> None:
    """base_relations=None compiles the same SQL as calling build_render_sql directly."""
    from fabulexa_forge.exporters.source.plan import build_source_plan
    from fabulexa_forge.exporters.source.renders import build_render_sql

    emit_dir = build_source_test_emit(tmp_path)
    config = ExportConfig(mode="source")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        specs = build_source_query_specs(
            emit,
            config,
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

        sidecar = emit.sidecar
        fork_path = require_single_branch(sidecar)
        table_specs = build_source_plan(sidecar, config.source, discard_notice_sink)
        location_spec = next(t for t in table_specs if t.name == "location")
        sql_direct = build_render_sql(sidecar, fork_path, location_spec, anchor, None)

    location = next(s for s in specs if s.table_name == "location")
    assert location.sql == sql_direct


# ---------------------------------------------------------------------------
# shadow_base_relations: the CTE wrap
# ---------------------------------------------------------------------------


def test_shadow_wraps_one_cte_per_mapped_name() -> None:
    """One CTE per mapped name; a plain (non-WITH) query is wrapped, not prefixed."""
    con = duckdb.connect(":memory:")
    wrapped = shadow_base_relations(
        'SELECT * FROM "history"',
        {"history": "SELECT 1 AS sim_time", "records__widget": "SELECT 2 AS x"},
    )
    assert wrapped.startswith(
        'WITH "history" AS (SELECT 1 AS sim_time), "records__widget" AS (SELECT 2 AS x)'
        " SELECT * FROM (\n"
    )
    rows = con.execute(wrapped).fetchall()
    assert rows == [(1,)]


def test_shadow_wraps_a_query_that_already_opens_with_with() -> None:
    """A compiled query that already opens with its own WITH still wraps correctly
    (never a textual prefix that would produce two consecutive WITH keywords)."""
    con = duckdb.connect(":memory:")
    inner = 'WITH "t" AS (SELECT 1 AS x) SELECT x FROM "t"'
    wrapped = shadow_base_relations(inner, {"history": "SELECT 99 AS unused"})
    rows = con.execute(wrapped).fetchall()
    assert rows == [(1,)]


def test_apply_base_relations_none_is_identity() -> None:
    """apply_base_relations(sql, None) returns sql unchanged."""
    sql = 'SELECT * FROM "history"'
    assert apply_base_relations(sql, None) == sql


def test_apply_base_relations_wraps_when_given() -> None:
    """apply_base_relations(sql, mapping) delegates to shadow_base_relations."""
    sql = 'SELECT * FROM "history"'
    mapping = {"history": "SELECT 1 AS sim_time"}
    assert apply_base_relations(sql, mapping) == shadow_base_relations(sql, mapping)


# ---------------------------------------------------------------------------
# Binding rule: a replacing relation's self-read binds physical
# ---------------------------------------------------------------------------


def test_replacing_relations_self_read_binds_physical(tmp_path: Path) -> None:
    """A replacing SELECT reading the base table it presents binds physical
    (standard non-recursive WITH scoping) — not a fixed point / not itself.

    DuckDB's binder treats a same-named *unqualified* self-read as a circular
    CTE reference (an error, per its own message: "please explicitly add
    SCHEMA before table name") rather than resolving it outward — so a
    replacing relation's self-read schema-qualifies (`main.<table>`) to reach
    the physical table; this pins that the qualified read does resolve
    physical, not to the enclosing CTE itself."""
    db_path = tmp_path / "run.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute('CREATE TABLE "history" (sim_time BIGINT, value VARCHAR)')
    con.execute("INSERT INTO \"history\" VALUES (5, 'a'), (15, 'b'), (25, 'c')")

    base_relations = {"history": 'SELECT * FROM main."history" WHERE sim_time <= 15'}
    wrapped = shadow_base_relations(
        'SELECT * FROM "history" ORDER BY sim_time', base_relations
    )

    rows = con.execute(wrapped).fetchall()
    # If the self-read had bound to the replacing CTE instead of the physical
    # table, this would either error (no base case) or return an empty/wrong
    # result; binding physical yields exactly the two rows <= 15.
    assert rows == [(5, "a"), (15, "b")]


# ---------------------------------------------------------------------------
# Shadowing is total: dimensional FK hop
# ---------------------------------------------------------------------------


def test_dimensional_fk_hop_shadowed_total(tmp_path: Path) -> None:
    """A single-hop reference FK's join read of the target kind's spine resolves
    through the mapping; the anchor's own (unmapped) source table falls back
    physical."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_journey",
                    "records",
                    "journey_instance",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("actor_id", "dim_actor", "reference"),
                    ],
                ),
            ]
        )
        # records__actor mapped (excludes a002); records__journey_instance left
        # unmapped — its own rows (j001, j002) must still surface physically.
        # The replacing relation schema-qualifies its self-read (main.<table>)
        # to reach the physical table under DuckDB's binder (see
        # test_replacing_relations_self_read_binds_physical).
        base_relations = {
            "records__actor": (
                "SELECT * FROM main.records__actor WHERE record_id <> 'a002'"
            )
        }
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=base_relations,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_journey")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    actor_by_journey = dict(zip(rows["record_id"], rows["actor_id"]))
    # Both journeys still present (records__journey_instance unmapped, physical).
    assert set(actor_by_journey) == {"j001", "j002"}
    # j001 -> a001 still resolves (a001 survives the replacing relation).
    assert actor_by_journey["j001"] == "a001"
    # j002 -> a002 resolves NULL: the FK hop's join read of records__actor
    # went through the mapping, which excludes a002 — no physical leak.
    assert actor_by_journey["j002"] is None


# ---------------------------------------------------------------------------
# Shadowing is total: source change-log read (history + records spine)
# ---------------------------------------------------------------------------


def test_source_changelog_read_shadowed_total(tmp_path: Path) -> None:
    """The change-log fold's history read resolves through the mapping; the
    unmapped records spine (identity/lifecycle) falls back physical."""
    emit_dir = build_source_test_emit(tmp_path)
    config = ExportConfig(mode="source")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None

        specs_physical = build_source_query_specs(
            emit,
            config,
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        visit_physical = next(s for s in specs_physical if s.table_name == "visit")
        rows_physical = emit.query_arrow(visit_physical.sql, ()).to_pydict()

        # v002's coincident status+priority change at 150ms coalesces into one
        # 'u' event; excluding those two history rows removes that event. The
        # replacing relation schema-qualifies its self-read (main."history")
        # to reach the physical table under DuckDB's binder.
        base_relations = {
            "history": (
                'SELECT * FROM main."history" WHERE NOT'
                " (record_id = 'v002' AND sim_time = "
                f"{150 * _MS})"
            )
        }
        specs_shadowed = build_source_query_specs(
            emit,
            config,
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=base_relations,
        )
        visit_shadowed = next(s for s in specs_shadowed if s.table_name == "visit")
        rows_shadowed = emit.query_arrow(visit_shadowed.sql, ()).to_pydict()

    # Physical: v001 c; v002 c, u; v003 c, d -> 5 rows.
    assert len(rows_physical["id"]) == 5
    # Shadowed: v002's 'u' event disappears -> 4 rows. The history read went
    # through the mapping — no physical leak.
    assert len(rows_shadowed["id"]) == 4
    # The (unmapped) records spine is unaffected: every record's identity
    # still surfaces, physical.
    assert set(rows_shadowed["id"]) == {"v001", "v002", "v003"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
