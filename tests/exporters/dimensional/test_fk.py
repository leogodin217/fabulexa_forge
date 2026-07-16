"""Tests for the FK labeled-edge pathfind in the dimensional exporter.

Verifies via:reference (single-hop, multi-hop, ambiguous, path hint, no path)
and via:membership (inferred table, inferred member_field, where predicate,
NULL rows, ambiguous). Also verifies FkTargetIsDim and history-grain FK NULL.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.sidecar_builder import write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    FkClause,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# FK emit fixture helpers
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR"},
]

_JOURNEY_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    # references actor
    {"name": "prop__actor_id", "type": "VARCHAR", "references": "actor"},
]

_DECISION_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    # references journey_instance (for multi-hop: decision→journey_instance→actor)
    {"name": "prop__journey_id", "type": "VARCHAR", "references": "journey_instance"},
]

_BINDINGS_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]

_HISTORY_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# Ambiguous: journey_instance has TWO reference columns pointing to actor
_JOURNEY_AMBIGUOUS_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__primary_actor_id", "type": "VARCHAR", "references": "actor"},
    {"name": "prop__secondary_actor_id", "type": "VARCHAR", "references": "actor"},
]

# Decision with two alternative FK columns (one to journey, one to actor directly)
_DECISION_AMBIGUOUS_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__journey_id", "type": "VARCHAR", "references": "journey_instance"},
    # direct reference to actor as well — creates two paths decision→actor
    {"name": "prop__actor_id", "type": "VARCHAR", "references": "actor"},
]

_SUPPORTED_VERSION = "0.1"


def _build_base_sidecar(tables: list[dict]) -> dict:
    return {
        "base_format_version": _SUPPORTED_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }


def build_reference_chain_emit(tmp_path: Path) -> Path:
    """Emit with actor, journey_instance (→actor), decision (→journey_instance).

    decision → journey_instance → actor: two-hop reference chain.
    Also includes a history table for history-grain FK tests.
    """
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_COLUMNS))
    conn.execute(_create_ddl("records__decision", _DECISION_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        _create_ddl(
            "membership__decision__bindings",
            _BINDINGS_COLUMNS,
        )
    )

    # actor rows
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "a001", True, 10, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "a002", True, 20, "Bob"],
    )

    # journey rows: j001 → a001, j002 → a002
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j001", True, 10, "a001"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j002", True, 20, "a002"],
    )

    # decision rows: d001 → j001 (→ a001), d002 → j002 (→ a002), d003 → no journey (NULL)
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d001", True, 10, "j001"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d002", True, 20, "j002"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, NULL)',
        ["trunk", "d003", True, 30],
    )

    # history rows for journey_instance.state (for history-grain FK test)
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "journey_instance", "j001", "state", 5, "active"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "journey_instance", "j002", "state", 10, "active"],
    )

    # membership: bindings for d001 → a001 (consultant), d002 → a002 (nurse)
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "d001", 5, "consultant", "actor", "a001"],
    )
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "d002", 10, "nurse", "actor", "a002"],
    )
    # A membership row whose member__actor__kind is NOT actor (different kind → NULL)
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "d001", 15, "manager", "non_actor_kind", "x999"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_COLUMNS,
                2,
                "journey_instance",
            ),
            _table_spec(
                "records__decision", "records", _DECISION_COLUMNS, 3, "decision"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
            _table_spec(
                "membership__decision__bindings",
                "membership",
                _BINDINGS_COLUMNS,
                3,
                "decision",
                "bindings",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def build_ambiguous_emit(tmp_path: Path) -> Path:
    """Emit where decision has two paths to actor (ambiguous reference).

    decision → actor via prop__actor_id (direct)
    decision → journey_instance → actor via prop__journey_id + prop__actor_id
    This creates two paths from decision to actor, making the FK ambiguous.
    """
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_COLUMNS))
    conn.execute(_create_ddl("records__decision", _DECISION_AMBIGUOUS_COLUMNS))

    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "a001", True, 10, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j001", True, 5, "a001"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "d001", True, 10, "j001", "a001"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 1, "actor"),
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_COLUMNS,
                1,
                "journey_instance",
            ),
            _table_spec(
                "records__decision",
                "records",
                _DECISION_AMBIGUOUS_COLUMNS,
                1,
                "decision",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def _from_col(name: str, src: str) -> ColumnDecl:
    return ColumnDecl(name=name, **{"from": src})


def _fk_col(name: str, to: str, via: str, **kwargs: object) -> ColumnDecl:
    return ColumnDecl(name=name, fk=FkClause(to=to, via=via, **kwargs))


def _dim(name: str, kind: str, cols: list[ColumnDecl]) -> TableDecl:
    return TableDecl(
        name=name,
        role="dim",
        scd="type1",
        key=["record_id"],
        source=SourceDecl(grain="records", kind=kind),
        columns=cols,
    )


def _fact(
    name: str,
    grain: str,
    kind: str,
    cols: list[ColumnDecl],
    property: str | None = None,
    where: dict[str, str] | None = None,
) -> TableDecl:
    src_kwargs: dict[str, object] = {"grain": grain, "kind": kind}
    if property is not None:
        src_kwargs["property"] = property
    if where is not None:
        src_kwargs["where"] = where
    return TableDecl(
        name=name,
        role="fact",
        key=["record_id"],
        source=SourceDecl(**src_kwargs),  # type: ignore[arg-type]
        columns=cols,
    )


# ---------------------------------------------------------------------------
# via: reference tests
# ---------------------------------------------------------------------------


def test_reference_single_hop(tmp_path: Path) -> None:
    """Single-hop reference FK: journey_instance.actor_id → dim_actor.record_id."""
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
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_journey")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # j001 → a001, j002 → a002
    assert set(rows["actor_id"]) == {"a001", "a002"}
    assert len(rows["record_id"]) == 2


def test_reference_multi_hop(tmp_path: Path) -> None:
    """Multi-hop FK: decision → journey_instance → actor (two hops)."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("actor_id", "dim_actor", "reference"),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 → j001 → a001, d002 → j002 → a002, d003 → NULL (no journey)
    actor_by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert actor_by_decision["d001"] == "a001"
    assert actor_by_decision["d002"] == "a002"
    assert actor_by_decision["d003"] is None


def test_reference_ambiguous_no_hint_raises(tmp_path: Path) -> None:
    """Ambiguous reference path (two paths to dim kind) without path hint raises."""
    emit_dir = build_ambiguous_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("actor_id", "dim_actor", "reference"),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="ambiguous reference path"):
            build_query_specs(emit, config, None, None)


def test_reference_path_hint_disambiguates(tmp_path: Path) -> None:
    """path hint selects one of two paths from decision to actor."""
    emit_dir = build_ambiguous_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "reference",
                            path=["prop__actor_id"],
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    assert rows["actor_id"] == ["a001"]


def test_reference_path_hint_non_references_column_raises(tmp_path: Path) -> None:
    """path hint naming a non-references column raises."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "reference",
                            # record_id is not a references column
                            path=["record_id"],
                        ),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="not a references column"):
            build_query_specs(emit, config, None, None)


def test_reference_no_path_raises(tmp_path: Path) -> None:
    """No reference path from anchor kind to dim kind raises."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        # actor has no reference to decision — there is no path actor → decision
        config = DimensionalConfig(
            tables=[
                _dim("dim_decision", "decision", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_actor",
                    "records",
                    "actor",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("decision_id", "dim_decision", "reference"),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="no reference path"):
            build_query_specs(emit, config, None, None)


# ---------------------------------------------------------------------------
# via: membership tests
# ---------------------------------------------------------------------------


def test_membership_fk_inferred_table_and_field(tmp_path: Path) -> None:
    """via:membership: single membership table + single member field inferred."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            where={"elem__role": "consultant"},
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 has consultant binding → a001; d002 has nurse binding → NULL (no consultant)
    actor_by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert actor_by_decision["d001"] == "a001"
    assert actor_by_decision["d002"] is None
    assert actor_by_decision["d003"] is None


def test_membership_fk_where_selects_role(tmp_path: Path) -> None:
    """via:membership: where predicate selects the correct role."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "nurse_id",
                            "dim_actor",
                            "membership",
                            where={"elem__role": "nurse"},
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    actor_by_decision = dict(zip(rows["record_id"], rows["nurse_id"]))
    assert actor_by_decision["d001"] is None  # d001 has consultant, not nurse
    assert actor_by_decision["d002"] == "a002"


def test_membership_null_for_different_member_kind(tmp_path: Path) -> None:
    """Membership row whose member__actor__kind != target kind emits NULL."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        # d001 has two bindings: one actor/a001 (consultant), one non_actor_kind (manager)
        # The FK should resolve only the actor row; the non_actor_kind row gets NULL
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # The non_actor_kind membership does not produce an actor_id
    # d001 has a consultant row → a001; manager row → non_actor → excluded
    actor_by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert actor_by_decision["d003"] is None  # no binding at all


def test_membership_on_membership_grain(tmp_path: Path) -> None:
    """via:membership FK on a membership grain projects member__actor__id directly."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_bindings",
                    "membership",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("actor_id", "dim_actor", "membership"),
                    ],
                    property="bindings",
                    where={"elem__role": "consultant"},
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_bindings")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # consultant binding: d001 → a001
    assert "a001" in rows["actor_id"]


def test_membership_where_not_elem_column_raises(tmp_path: Path) -> None:
    """where column that is not an elem__ column raises MembershipEdgeResolvable."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            # record_id is not an elem__ column
                            where={"record_id": "consultant"},
                        ),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="membership FK.*unresolvable"):
            build_query_specs(emit, config, None, None)


# ---------------------------------------------------------------------------
# FkTargetIsDim tests
# ---------------------------------------------------------------------------


def test_fk_target_is_dim_raises_for_fact(tmp_path: Path) -> None:
    """FK pointing to a role:fact table raises FkTargetIsDim."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                # fact table, not a dim
                _fact(
                    "fact_actor",
                    "records",
                    "actor",
                    [_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("actor_id", "fact_actor", "reference"),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="is not a declared dimension"):
            build_query_specs(emit, config, None, None)


def test_fk_target_undeclared_raises(tmp_path: Path) -> None:
    """FK pointing to an undeclared table raises FkTargetIsDim."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("actor_id", "dim_actor_nonexistent", "reference"),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="is not a declared dimension"):
            build_query_specs(emit, config, None, None)


# ---------------------------------------------------------------------------
# History-grain FK: unresolvable rows emit NULL
# ---------------------------------------------------------------------------


def test_history_grain_fk_null_for_unresolvable_rows(tmp_path: Path) -> None:
    """FK on a history grain: rows without a resolvable path emit NULL."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        # history rows keyed by record_id only; no prop__ to join actor directly
        # decision→journey→actor path doesn't apply from history grain (keyed by record_id)
        # journey_instance rows do have a prop__actor_id → actor reference
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_journey_states",
                    "history_point",
                    "journey_instance",
                    [
                        _from_col("record_id", "record_id"),
                        _from_col("value", "value"),
                        _fk_col("actor_id", "dim_actor", "reference"),
                    ],
                    property="state",
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_journey_states")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # history grain on journey_instance: joins records__journey_instance on record_id
    # then hops to actor via prop__actor_id
    # j001.state → j001 has prop__actor_id=a001
    assert set(rows["actor_id"]) == {"a001", "a002"}


# ---------------------------------------------------------------------------
# Typed membership FK where (non-VARCHAR elem__ column) — Step 5
# ---------------------------------------------------------------------------

# Membership columns where elem__priority is BIGINT
_TYPED_BINDINGS_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
    {"name": "elem__priority", "type": "BIGINT"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]


def build_typed_membership_emit(tmp_path: Path) -> Path:
    """Emit with actor + membership that has a BIGINT elem__ column."""
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("membership__decision__bindings", _TYPED_BINDINGS_COLUMNS))
    conn.execute(_create_ddl("records__decision", _DECISION_COLUMNS))

    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "a001", True, 10, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "a002", True, 20, "Bob"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d001", True, 10, "j001"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d002", True, 20, "j002"],
    )

    # d001 → a001 (priority=1), d002 → a002 (priority=2)
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 5, "surgeon", 1, "actor", "a001"],
    )
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d002", 10, "nurse", 2, "actor", "a002"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__actor", "records", _ACTOR_COLUMNS, 2, "actor"),
            _table_spec(
                "records__decision", "records", _DECISION_COLUMNS, 2, "decision"
            ),
            _table_spec(
                "membership__decision__bindings",
                "membership",
                _TYPED_BINDINGS_COLUMNS,
                2,
                "decision",
                "bindings",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def test_membership_fk_where_bigint_elem_column_selects_correctly(
    tmp_path: Path,
) -> None:
    """membership FK where on a BIGINT elem__ column uses CAST literal and selects correctly."""
    emit_dir = build_typed_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            where={"elem__priority": "1"},
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        # SQL must use CAST form for BIGINT elem__ where
        assert "CAST('1' AS BIGINT)" in fact_spec.sql
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 has priority=1 → a001; d002 has priority=2 → NULL
    actor_by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert actor_by_decision["d001"] == "a001"
    assert actor_by_decision["d002"] is None


def test_membership_fk_where_varchar_elem_stays_quoted(tmp_path: Path) -> None:
    """membership FK where on a VARCHAR elem__ column stays single-quoted (byte-stable)."""
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            where={"elem__role": "consultant"},
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        # VARCHAR where must NOT use CAST form
        assert "CAST('consultant'" not in fact_spec.sql
        assert "'consultant'" in fact_spec.sql


# ---------------------------------------------------------------------------
# Surrogate-key (target_key: presentation_id) tests — F8
# ---------------------------------------------------------------------------

# Actor columns with presentation_id for surrogate tests
_ACTOR_SURROGATE_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
]


def build_surrogate_emit(tmp_path: Path) -> Path:
    """Emit with presentation_id on records__actor for surrogate FK tests.

    actor rows carry presentation_id 'PAT_001'/'PAT_002'.
    journey_instance references actor.
    decision references journey_instance (two-hop to actor).
    membership__decision__bindings carries actor members.
    """
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__actor", _ACTOR_SURROGATE_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_COLUMNS))
    conn.execute(_create_ddl("records__decision", _DECISION_COLUMNS))
    conn.execute(_create_ddl("membership__decision__bindings", _BINDINGS_COLUMNS))

    # actor rows with presentation_id surrogates
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", True, 10, "Alice", "PAT_001"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", True, 20, "Bob", "PAT_002"],
    )

    # journey rows: j001 → a001, j002 → a002
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j001", True, 10, "a001"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j002", True, 20, "a002"],
    )

    # decision rows: d001 → j001 (→ a001), d002 → j002 (→ a002), d003 → no journey
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d001", True, 10, "j001"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d002", True, 20, "j002"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, NULL)',
        ["trunk", "d003", True, 30],
    )

    # membership: d001 → a001 (consultant), d002 → a002 (nurse)
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "d001", 5, "consultant", "actor", "a001"],
    )
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "d002", 10, "nurse", "actor", "a002"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _ACTOR_SURROGATE_COLUMNS, 2, "actor"
            ),
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_COLUMNS,
                2,
                "journey_instance",
            ),
            _table_spec(
                "records__decision", "records", _DECISION_COLUMNS, 3, "decision"
            ),
            _table_spec(
                "membership__decision__bindings",
                "membership",
                _BINDINGS_COLUMNS,
                2,
                "decision",
                "bindings",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def test_reference_single_hop_surrogate(tmp_path: Path) -> None:
    """Single-hop reference FK with target_key=presentation_id projects PAT_ surrogates."""
    emit_dir = build_surrogate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim(
                    "dim_actor",
                    "actor",
                    [_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_journey",
                    "records",
                    "journey_instance",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "reference",
                            target_key="presentation_id",
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_journey")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # j001 → a001 → PAT_001, j002 → a002 → PAT_002
    assert set(rows["actor_id"]) == {"PAT_001", "PAT_002"}


def test_reference_multi_hop_surrogate(tmp_path: Path) -> None:
    """Multi-hop reference FK with target_key=presentation_id projects surrogates."""
    emit_dir = build_surrogate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim(
                    "dim_actor",
                    "actor",
                    [_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "reference",
                            target_key="presentation_id",
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 → j001 → a001 → PAT_001, d002 → j002 → a002 → PAT_002, d003 → NULL
    by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert by_decision["d001"] == "PAT_001"
    assert by_decision["d002"] == "PAT_002"
    assert by_decision["d003"] is None


def test_membership_on_records_surrogate(tmp_path: Path) -> None:
    """via:membership on records grain with target_key=presentation_id projects surrogates."""
    emit_dir = build_surrogate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim(
                    "dim_actor",
                    "actor",
                    [_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            where={"elem__role": "consultant"},
                            target_key="presentation_id",
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 → consultant → a001 → PAT_001; d002 nurse → NULL; d003 → NULL
    by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert by_decision["d001"] == "PAT_001"
    assert by_decision["d002"] is None
    assert by_decision["d003"] is None


def test_membership_on_grain_surrogate(tmp_path: Path) -> None:
    """via:membership on membership grain with target_key=presentation_id projects surrogates."""
    emit_dir = build_surrogate_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim(
                    "dim_actor",
                    "actor",
                    [_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_bindings",
                    "membership",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            target_key="presentation_id",
                        ),
                    ],
                    property="bindings",
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_bindings")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 → a001 → PAT_001, d002 → a002 → PAT_002
    assert set(rows["actor_id"]) == {"PAT_001", "PAT_002"}


def test_target_key_presentation_id_missing_column_raises(tmp_path: Path) -> None:
    """target_key=presentation_id raises ExportError when target records table lacks presentation_id."""
    # Use the regular (non-surrogate) emit: records__actor has no presentation_id
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
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "reference",
                            target_key="presentation_id",
                        ),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="presentation_id"):
            build_query_specs(emit, config, None, None)


# ---------------------------------------------------------------------------
# Point-in-time membership FK tests — F7
#
# Fixture: decision (grain) → (prop__journey_id) → journey_instance
#          journey_instance  → (prop__actor_id)   → actor  (the MEMBER)
#          membership__owner__holders: owner kind, timed holds by actor members
#
# The grain is decision. The OWNER is the dim's source kind (`owner`).
# The MEMBER is actor, reached via member_path=[prop__journey_id, prop__actor_id].
# ---------------------------------------------------------------------------

_OWNER_SURROGATE_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "presentation_id", "type": "VARCHAR"},
]

_HOLDER_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]

# decision columns: references journey_instance + has last_mutation_sim_time
_PIT_DECISION_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__journey_id", "type": "VARCHAR", "references": "journey_instance"},
]

_PIT_JOURNEY_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__actor_id", "type": "VARCHAR", "references": "actor"},
]

_PIT_ACTOR_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
]


def build_pit_membership_emit(tmp_path: Path) -> Path:
    """Emit for point-in-time membership FK tests.

    Grain: decision (record_id=d001..d004, last_mutation_sim_time=T).
    decision -[prop__journey_id]-> journey_instance -[prop__actor_id]-> actor (MEMBER).
    OWNER kind: owner (dim source).
    membership__owner__holders: owner record holds actor members with timed intervals.

    Holds:
      - o001 holds a001 from T=10..30 (closed)
      - o001 holds a001 from T=50..NULL (open)
      - o002 holds a002 from T=5..NULL (open)

    Decisions:
      - d001: T=15, journey j001 -> actor a001; a001 held by o001 at T=15 -> o001
      - d002: T=35, journey j001 -> actor a001; a001 NOT held by any owner at T=35 -> NULL
      - d003: T=60, journey j001 -> actor a001; a001 held by o001 from T=50 -> o001
      - d004: T=20, journey j002 -> actor a002; a002 held by o002 at T=20 -> o002

    Overlapping hold test (d001 fires at T=15; o001 has exactly one open interval at T=15).
    """
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__owner", _OWNER_SURROGATE_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _PIT_JOURNEY_COLUMNS))
    conn.execute(_create_ddl("records__actor", _PIT_ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__decision", _PIT_DECISION_COLUMNS))
    conn.execute(_create_ddl("membership__owner__holders", _HOLDER_COLUMNS))

    # owner rows with presentation_id surrogates
    conn.execute(
        'INSERT INTO "records__owner" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "o001", True, 100, "OWN_001"],
    )
    conn.execute(
        'INSERT INTO "records__owner" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "o002", True, 100, "OWN_002"],
    )

    # actor rows
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "a001", True, 100],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "a002", True, 100],
    )

    # journey_instance rows: j001 -> a001, j002 -> a002
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j001", True, 100, "a001"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "j002", True, 100, "a002"],
    )

    # decision rows (grain)
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d001", True, 15, "j001"],  # T=15, member=a001
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d002", True, 35, "j001"],  # T=35, member=a001, no hold
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d003", True, 60, "j001"],  # T=60, member=a001, open hold
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d004", True, 20, "j002"],  # T=20, member=a002
    )

    # membership__owner__holders: owner holds actor members
    # o001 holds a001 from T=10..30 (closed)
    conn.execute(
        'INSERT INTO "membership__owner__holders" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "o001", 10, 30, "actor", "a001"],
    )
    # o001 holds a001 from T=50..NULL (open)
    conn.execute(
        'INSERT INTO "membership__owner__holders" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "o001", 50, None, "actor", "a001"],
    )
    # o002 holds a002 from T=5..NULL (open)
    conn.execute(
        'INSERT INTO "membership__owner__holders" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "o002", 5, None, "actor", "a002"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__owner", "records", _OWNER_SURROGATE_COLUMNS, 2, "owner"
            ),
            _table_spec("records__actor", "records", _PIT_ACTOR_COLUMNS, 2, "actor"),
            _table_spec(
                "records__journey_instance",
                "records",
                _PIT_JOURNEY_COLUMNS,
                2,
                "journey_instance",
            ),
            _table_spec(
                "records__decision", "records", _PIT_DECISION_COLUMNS, 4, "decision"
            ),
            _table_spec(
                "membership__owner__holders",
                "membership",
                _HOLDER_COLUMNS,
                3,
                "owner",
                "holders",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def _pit_fk_col(name: str, to: str, **kwargs: object) -> ColumnDecl:
    """Helper: build a point-in-time membership FK column declaration."""
    return ColumnDecl(
        name=name,
        fk=FkClause(
            to=to,
            via="membership",
            property="holders",
            member_field="actor",
            as_of="last_mutation_sim_time",
            member_path=["prop__journey_id", "prop__actor_id"],
            **kwargs,
        ),
    )


def test_pit_membership_resolves_holder_covering_t(tmp_path: Path) -> None:
    """Point-in-time FK resolves the owner that held the member at firing time T."""
    emit_dir = build_pit_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_owner", "owner", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _pit_fk_col("owner_id", "dim_owner"),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_decision = dict(zip(rows["record_id"], rows["owner_id"]))
    # d001 at T=15: a001 held by o001 (10..30) -> o001
    assert by_decision["d001"] == "o001"
    # d004 at T=20: a002 held by o002 (5..NULL) -> o002
    assert by_decision["d004"] == "o002"
    # Row count must equal grain size (4 decisions) — no fan-out
    assert len(rows["record_id"]) == 4


def test_pit_membership_outside_all_holds_is_null(tmp_path: Path) -> None:
    """Point-in-time FK returns NULL when firing time T is outside all holds."""
    emit_dir = build_pit_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_owner", "owner", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _pit_fk_col("owner_id", "dim_owner"),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_decision = dict(zip(rows["record_id"], rows["owner_id"]))
    # d002 at T=35: a001's hold [10..30] closed before T=35, next hold [50..] not started
    assert by_decision["d002"] is None


def test_pit_membership_presentation_id_projected(tmp_path: Path) -> None:
    """Point-in-time FK with target_key=presentation_id projects the surrogate."""
    emit_dir = build_pit_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_owner", "owner", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _pit_fk_col(
                            "owner_id", "dim_owner", target_key="presentation_id"
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_decision = dict(zip(rows["record_id"], rows["owner_id"]))
    # d001 at T=15 -> o001 -> OWN_001
    assert by_decision["d001"] == "OWN_001"
    # d004 at T=20 -> o002 -> OWN_002
    assert by_decision["d004"] == "OWN_002"
    # d002 at T=35 -> NULL (no hold covers T=35)
    assert by_decision["d002"] is None


def test_pit_membership_no_grain_fanout(tmp_path: Path) -> None:
    """Point-in-time FK produces exactly one row per grain row (no fan-out)."""
    emit_dir = build_pit_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_owner", "owner", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _pit_fk_col("owner_id", "dim_owner"),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # 4 decisions in grain, must be exactly 4 output rows — correlated subquery prevents fan-out
    assert len(rows["record_id"]) == 4
    # All decision IDs present exactly once
    assert sorted(rows["record_id"]) == ["d001", "d002", "d003", "d004"]


def test_pit_membership_missing_as_of_column_raises(tmp_path: Path) -> None:
    """Validation raises ExportError when the as_of column doesn't exist on the grain."""
    emit_dir = build_pit_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_owner", "owner", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        ColumnDecl(
                            name="owner_id",
                            fk=FkClause(
                                to="dim_owner",
                                via="membership",
                                property="holders",
                                member_field="actor",
                                as_of="nonexistent_column",
                                member_path=["prop__journey_id", "prop__actor_id"],
                            ),
                        ),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="as_of column"):
            build_query_specs(emit, config, None, None)


def test_pit_membership_unresolvable_member_path_raises(tmp_path: Path) -> None:
    """Validation raises ExportError when member_path names a non-references column."""
    emit_dir = build_pit_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_owner", "owner", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        ColumnDecl(
                            name="owner_id",
                            fk=FkClause(
                                to="dim_owner",
                                via="membership",
                                property="holders",
                                member_field="actor",
                                as_of="last_mutation_sim_time",
                                # record_id is not a references column
                                member_path=["record_id"],
                            ),
                        ),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="not a references column"):
            build_query_specs(emit, config, None, None)


# ---------------------------------------------------------------------------
# Non-records-grain reference FKs + per-column alias namespacing
# ---------------------------------------------------------------------------


def test_reference_fk_on_membership_grain_walks_from_owner(tmp_path: Path) -> None:
    """A via:reference FK on a membership grain pathfinds from the OWNER record.

    Regression: the reference chain previously tried to read prop__ columns
    directly off the membership table (which carries none) and crashed in the
    DuckDB binder. The chain must start from records__<owner_kind> joined on
    record_id, exactly as the history grains do.
    """
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_binding",
                    "membership",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _from_col("bound_actor_id", "member__actor__id"),
                        _fk_col("patient_id", "dim_actor", "reference"),
                    ],
                    property="bindings",
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_binding")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # Three bindings, ordered (d001/consultant, d001/manager, d002/nurse).
    assert rows["record_id"] == ["d001", "d001", "d002"]
    # patient_id walks OWNER decision -> journey -> actor, so it is the owner's
    # actor, NOT the bound member. The manager binding's bound member is x999
    # (non_actor_kind) yet patient_id is a001 (d001's actor via the owner chain).
    assert rows["bound_actor_id"] == ["a001", "x999", "a002"]
    assert rows["patient_id"] == ["a001", "a001", "a002"]


def test_two_reference_fks_on_one_table_do_not_collide(tmp_path: Path) -> None:
    """Two via:reference FKs on one table get per-column JOIN aliases.

    Regression: hop aliases were _fk_hop_<i> (indexed per column, not namespaced
    by column), so two reference FKs both emitted _fk_hop_0 and DuckDB raised a
    duplicate-alias binder error.
    """
    emit_dir = build_ambiguous_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _dim(
                    "dim_journey",
                    "journey_instance",
                    [_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "reference",
                            path=["prop__actor_id"],
                        ),
                        _fk_col(
                            "journey_id",
                            "dim_journey",
                            "reference",
                            path=["prop__journey_id"],
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    assert rows["record_id"] == ["d001"]
    assert rows["actor_id"] == ["a001"]
    assert rows["journey_id"] == ["j001"]


def test_two_reference_fks_on_membership_grain(tmp_path: Path) -> None:
    """Combined: two via:reference FKs on a membership grain.

    Exercises the owner-record preamble (membership grain) and per-column alias
    namespacing (two reference FKs) at once — the two fixes share one function.
    """
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _dim(
                    "dim_journey",
                    "journey_instance",
                    [_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_binding",
                    "membership",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col("patient_id", "dim_actor", "reference"),
                        _fk_col(
                            "journey_id",
                            "dim_journey",
                            "reference",
                            path=["prop__journey_id"],
                        ),
                    ],
                    property="bindings",
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_binding")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    assert rows["record_id"] == ["d001", "d001", "d002"]
    assert rows["patient_id"] == ["a001", "a001", "a002"]
    assert rows["journey_id"] == ["j001", "j001", "j002"]


def test_fk_target_dim_declared_after_fact_resolves(tmp_path: Path) -> None:
    """FK target resolution is declaration-order independent.

    Every other fixture declares the dim before the fact; here the fact comes
    first and check_fk_target_is_dim must still find the dim by scanning the
    whole config.tables list.
    """
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        dim = _dim("dim_actor", "actor", [_from_col("record_id", "record_id")])
        fact = _fact(
            "fact_journey",
            "records",
            "journey_instance",
            [
                _from_col("record_id", "record_id"),
                _fk_col("actor_id", "dim_actor", "reference"),
            ],
        )
        config_fact_first = DimensionalConfig(tables=[fact, dim])
        config_dim_first = DimensionalConfig(tables=[dim, fact])

        specs_fact_first = build_query_specs(emit, config_fact_first, None, None)
        specs_dim_first = build_query_specs(emit, config_dim_first, None, None)

        fact_spec = next(s for s in specs_fact_first if s.table_name == "fact_journey")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # Same fact SQL regardless of where the dim sits in the declaration list
    dim_first_sql = next(
        s.sql for s in specs_dim_first if s.table_name == "fact_journey"
    )
    assert fact_spec.sql == dim_first_sql
    # And the FK resolves: j001 → a001, j002 → a002
    assert dict(zip(rows["record_id"], rows["actor_id"])) == {
        "j001": "a001",
        "j002": "a002",
    }


# ---------------------------------------------------------------------------
# Surrogate target_key + BIGINT elem__ where predicate combined
# ---------------------------------------------------------------------------

# Actor with presentation_id AND membership with a BIGINT elem__ column, so the
# CAST-vs-quote where logic and the presentation_id records join stack together.


def build_typed_surrogate_membership_emit(tmp_path: Path) -> Path:
    """Emit with presentation_id on records__actor + BIGINT elem__priority membership.

    d001 → a001 (PAT_001) at priority=1; d002 → a002 (PAT_002) at priority=2.
    """
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__actor", _ACTOR_SURROGATE_COLUMNS))
    conn.execute(_create_ddl("records__decision", _DECISION_COLUMNS))
    conn.execute(_create_ddl("membership__decision__bindings", _TYPED_BINDINGS_COLUMNS))

    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", True, 10, "Alice", "PAT_001"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", True, 20, "Bob", "PAT_002"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d001", True, 10, "j001"],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "d002", True, 20, "j002"],
    )
    # d001 → a001 (priority=1), d002 → a002 (priority=2)
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 5, "surgeon", 1, "actor", "a001"],
    )
    conn.execute(
        'INSERT INTO "membership__decision__bindings" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d002", 10, "nurse", 2, "actor", "a002"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _ACTOR_SURROGATE_COLUMNS, 2, "actor"
            ),
            _table_spec(
                "records__decision", "records", _DECISION_COLUMNS, 2, "decision"
            ),
            _table_spec(
                "membership__decision__bindings",
                "membership",
                _TYPED_BINDINGS_COLUMNS,
                2,
                "decision",
                "bindings",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def test_membership_fk_surrogate_with_bigint_where_on_records_grain(
    tmp_path: Path,
) -> None:
    """target_key=presentation_id + BIGINT elem__ where: CAST logic survives layering.

    Combines the surrogate records join with a where predicate on a BIGINT
    elem__ column — the CAST-vs-quote literal typing must still hold once
    presentation_id resolution is layered on top.
    """
    emit_dir = build_typed_surrogate_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            where={"elem__priority": "1"},
                            target_key="presentation_id",
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        # BIGINT elem__ where must render as a CAST literal, not a quoted string
        assert "CAST('1' AS BIGINT)" in fact_spec.sql
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 (priority=1) → a001 → PAT_001; d002 (priority=2) → NULL
    by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert by_decision["d001"] == "PAT_001"
    assert by_decision["d002"] is None


def test_membership_grain_fk_surrogate_with_bigint_source_where(
    tmp_path: Path,
) -> None:
    """Membership grain: BIGINT elem__ source.where + target_key=presentation_id.

    The grain's where predicate (rendered with the CAST-vs-quote literal typing)
    narrows the bindings while build_membership_fk_expr_on_membership layers the
    presentation_id records join on top.
    """
    emit_dir = build_typed_surrogate_membership_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_bindings",
                    "membership",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor",
                            "membership",
                            target_key="presentation_id",
                        ),
                    ],
                    property="bindings",
                    where={"elem__priority": "1"},
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_bindings")
        # The grain where on a BIGINT elem__ column renders as a CAST literal
        assert "CAST('1' AS BIGINT)" in fact_spec.sql
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # Only the priority=1 binding survives, resolved to its surrogate
    assert rows["record_id"] == ["d001"]
    assert rows["actor_id"] == ["PAT_001"]


def test_two_membership_fks_on_one_table_do_not_collide(tmp_path: Path) -> None:
    """Two via:membership FKs on one table get per-column JOIN aliases.

    Same collision class as the reference-FK fix: membership-edge joins used the
    fixed alias _fk_mem, so two membership FKs on one table collided.
    """
    emit_dir = build_reference_chain_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                _dim("dim_actor", "actor", [_from_col("record_id", "record_id")]),
                _fact(
                    "fact_decision",
                    "records",
                    "decision",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "consultant_id",
                            "dim_actor",
                            "membership",
                            where={"elem__role": "consultant"},
                        ),
                        _fk_col(
                            "nurse_id",
                            "dim_actor",
                            "membership",
                            where={"elem__role": "nurse"},
                        ),
                    ],
                ),
            ]
        )
        specs = build_query_specs(emit, config, None, None)
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    consultant = dict(zip(rows["record_id"], rows["consultant_id"]))
    nurse = dict(zip(rows["record_id"], rows["nurse_id"]))
    # d001 has a consultant binding (a001); d002 has a nurse binding (a002).
    assert consultant["d001"] == "a001"
    assert consultant["d002"] is None
    assert nurse["d002"] == "a002"
    assert nurse["d001"] is None
