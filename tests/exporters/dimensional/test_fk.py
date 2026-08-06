"""Tests for the FK labeled-edge pathfind in the dimensional exporter.

Verifies via:reference (single-hop, multi-hop, ambiguous, path hint, no path)
and via:membership (inferred table, inferred member_field, where predicate,
NULL rows, ambiguous). Also verifies FkTargetIsDim and history-grain FK NULL.

The target_key=presentation_id subsumption case was rewritten for the
key-election sprint's Phase 6: the shipped column-presence check the old
`test_target_key_presentation_id_missing_column_raises` exercised is gone
from `fk.py`, subsumed by the statically-earlier registry-membership check
over the destination dim's (possibly discriminator-filtered) source
population set — `test_target_key_presentation_id_undeclared_population_raises`
below exercises that check directly. The other target_key=presentation_id
("surrogate") tests are unaffected: they target a flat kind, whose
population set carries no discriminator to filter, so the registry-
membership check never runs for them (`dim_population_sub_types` is the
empty tuple) and they still surface the target table's own column-presence
failure via the presentation-key derivation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    FkClause,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ElectionPresentationUndeclared, ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.fk import check_fk_slice_only
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# FK emit fixture helpers
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_JOURNEY_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    # references actor
    prop_column(
        "prop__actor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="actor",
    ),
    identity_column("ref_index__actor_id", "BIGINT"),
]

_DECISION_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    # references journey_instance (for multi-hop: decision→journey_instance→actor)
    prop_column(
        "prop__journey_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="journey_instance",
    ),
    identity_column("ref_index__journey_id", "BIGINT"),
]

_BINDINGS_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
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
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__primary_actor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="actor",
    ),
    identity_column("ref_index__primary_actor_id", "BIGINT"),
    prop_column(
        "prop__secondary_actor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="actor",
    ),
    identity_column("ref_index__secondary_actor_id", "BIGINT"),
]

# Decision with two alternative FK columns (one to journey, one to actor directly)
_DECISION_AMBIGUOUS_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__journey_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="journey_instance",
    ),
    identity_column("ref_index__journey_id", "BIGINT"),
    # direct reference to actor as well — creates two paths decision→actor
    prop_column(
        "prop__actor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="actor",
    ),
    identity_column("ref_index__actor_id", "BIGINT"),
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

    # actor rows: record_index 0, 1
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", 20, True, 20, 1, "Bob"],
    )

    # journey rows: j001 → a001 (ref_index__actor_id=0), j002 → a002 (ref_index__actor_id=1)
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 10, True, 10, 0, "a001", 0],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j002", 20, True, 20, 1, "a002", 1],
    )

    # decision rows: d001 → j001 (ref_index__journey_id=0), d002 → j002 (ref_index__journey_id=1),
    # d003 → no journey (NULL, ref_index__journey_id NULL-together)
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 10, True, 10, 0, "j001", 0],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d002", 20, True, 20, 1, "j002", 1],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL)',
        ["trunk", "d003", 30, True, 30, 2],
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
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 5, True, 5, 0, "a001", 0],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)',
        ["trunk", "d001", 10, True, 10, 0, "j001", 0, "a001", 0],
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
    grain: Literal["records", "history_point", "history_interval", "membership"],
    kind: str,
    cols: list[ColumnDecl],
    property: str | None = None,
    where: dict[str, str] | None = None,
) -> TableDecl:
    return TableDecl(
        name=name,
        role="fact",
        key=["record_id"],
        source=SourceDecl(grain=grain, kind=kind, property=property, where=where),
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    actor_by_decision = dict(zip(rows["record_id"], rows["nurse_id"]))
    assert actor_by_decision["d001"] is None  # d001 has consultant, not nurse
    assert actor_by_decision["d002"] == "a002"


def test_membership_fk_where_list_selects_multiple_roles(tmp_path: Path) -> None:
    """via:membership: a list-valued where narrows to any of the listed roles."""
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
                            where={"elem__role": ["consultant", "nurse"]},
                        ),
                    ],
                ),
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
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        assert "IN (" in fact_spec.sql
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 has consultant binding -> a001; d002 has nurse binding -> a002;
    # d003 has no binding at all -> NULL. Both listed roles resolve.
    actor_by_decision = dict(zip(rows["record_id"], rows["actor_id"]))
    assert actor_by_decision["d001"] == "a001"
    assert actor_by_decision["d002"] == "a002"
    assert actor_by_decision["d003"] is None


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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
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
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", 20, True, 20, 1, "Bob"],
    )
    # prop__journey_id references journey_instance, which this fixture does not
    # emit -- a dangling reference, so ref_index__journey_id is the fixture's
    # dangling sentinel (-1), never verified (see build_refs_dangling precedent).
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 10, True, 10, 0, "j001", -1],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d002", 20, True, 20, 1, "j002", -1],
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        # VARCHAR where must NOT use CAST form
        assert "CAST('consultant'" not in fact_spec.sql
        assert "'consultant'" in fact_spec.sql


# ---------------------------------------------------------------------------
# Surrogate-key (target_key: presentation_id) tests — F8
# ---------------------------------------------------------------------------

# Actor columns with presentation_id for surrogate tests. presentation_id
# occupies the slot immediately after record_id (base-format.md § C5), shifting
# the lifecycle prefix, record_index, and prop__ block down by one position.
_ACTOR_SURROGATE_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
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
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", "PAT_001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", "PAT_002", 20, True, 20, 1, "Bob"],
    )

    # journey rows: j001 → a001 (ref_index__actor_id=0), j002 → a002 (ref_index__actor_id=1)
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 10, True, 10, 0, "a001", 0],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j002", 20, True, 20, 1, "a002", 1],
    )

    # decision rows: d001 → j001 (ref_index__journey_id=0), d002 → j002 (ref_index__journey_id=1),
    # d003 → no journey (NULL, ref_index__journey_id NULL-together)
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 10, True, 10, 0, "j001", 0],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d002", 20, True, 20, 1, "j002", 1],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL)',
        ["trunk", "d003", 30, True, 30, 2],
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_bindings")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # d001 → a001 → PAT_001, d002 → a002 → PAT_002
    assert set(rows["actor_id"]) == {"PAT_001", "PAT_002"}


# Sub-typed actor (consultant/nurse), presentation_id declared for
# consultant only — the subsumption posture's target fixture: the shipped
# `target_key: presentation_id` column-presence check is gone (`fk.py` no
# longer looks at the target table's columns at all); an explicit
# `target_key: presentation_id` over a discriminator-filtered dim restricts
# to the dim's population set, and an undeclared population inside that set
# fails the statically-earlier registry-membership check instead
# (`ElectionPresentationUndeclared`), never a data read.
_SUBTYPED_ACTOR_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_CONSULTANT_ONLY_PRESENTATION_KEYS: dict[str, object] = {
    "actor": {
        "sub_types": {
            "consultant": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "CONS_", "width": 3},
            }
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}


def build_subtyped_actor_emit(tmp_path: Path) -> Path:
    """actor: a001 consultant (presentation_id declared), a002 nurse
    (presentation_id undeclared in the registry); journey_instance
    references actor (the FK's anchor)."""
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _SUBTYPED_ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", "CONS_001", 10, True, 10, 0, "consultant"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", None, 10, True, 10, 1, "nurse"],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 20, True, 20, 0, "a002", 1],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _SUBTYPED_ACTOR_COLUMNS, 2, "actor"
            ),
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_COLUMNS,
                1,
                "journey_instance",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {"actor": {"actor_type": ["consultant", "nurse"]}},
            "presentation_keys": _CONSULTANT_ONLY_PRESENTATION_KEYS,
        },
    )
    return tmp_path


def test_target_key_presentation_id_undeclared_population_raises(
    tmp_path: Path,
) -> None:
    """target_key=presentation_id over a nurse-filtered (discriminator-
    filtered, proper-subset) dim raises ElectionPresentationUndeclared —
    nurse carries no presentation_keys registry entry."""
    emit_dir = build_subtyped_actor_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        config = DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_actor_nurse",
                    role="dim",
                    scd="type1",
                    key=["record_id"],
                    source=SourceDecl(
                        grain="records",
                        kind="actor",
                        filter={"prop__actor_type": "nurse"},
                    ),
                    columns=[_from_col("record_id", "record_id")],
                ),
                _fact(
                    "fact_journey",
                    "records",
                    "journey_instance",
                    [
                        _from_col("record_id", "record_id"),
                        _fk_col(
                            "actor_id",
                            "dim_actor_nurse",
                            "reference",
                            target_key="presentation_id",
                        ),
                    ],
                ),
            ]
        )
        with pytest.raises(ElectionPresentationUndeclared):
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


# ---------------------------------------------------------------------------
# List-filtered dim source population: FK closure over a subset
# ---------------------------------------------------------------------------

_THREE_SUBTYPE_ACTOR_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]


def build_three_subtype_actor_emit(tmp_path: Path) -> Path:
    """actor split consultant/registrar/nurse; journey_instance references
    one actor each. a1 consultant, a2 registrar, a3 nurse; j1 -> a1
    (in-set), j2 -> a2 (in-set), j3 -> a3 (out-of-set for a
    consultant+registrar list filter)."""
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _THREE_SUBTYPE_ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_COLUMNS))
    for record_id, index, actor_type in (
        ("a1", 0, "consultant"),
        ("a2", 1, "registrar"),
        ("a3", 2, "nurse"),
    ):
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", record_id, 10, True, 10, index, actor_type],
        )
    for record_id, index, actor_id in (
        ("j1", 0, "a1"),
        ("j2", 1, "a2"),
        ("j3", 2, "a3"),
    ):
        conn.execute(
            'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
            ["trunk", record_id, 20, True, 20, index, actor_id, index],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _THREE_SUBTYPE_ACTOR_COLUMNS, 3, "actor"
            ),
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_COLUMNS,
                3,
                "journey_instance",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {
                "actor": {"actor_type": ["consultant", "registrar", "nurse"]}
            },
        },
    )
    return tmp_path


def test_list_filtered_dim_fk_in_set_resolves_out_of_set_null(tmp_path: Path) -> None:
    """A dim filtered to `["consultant", "registrar"]`, under an elected
    non-record_id surface, restricts the FK's identity relation to that
    subset: in-set owners (j1, j2) resolve, the out-of-set owner (j3, nurse)
    resolves NULL — closure, no dangling reference."""
    emit_dir = build_three_subtype_actor_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_actor_clinical",
                role="dim",
                scd="type1",
                key=["actor_index"],
                source=SourceDecl(
                    grain="records",
                    kind="actor",
                    filter={"prop__actor_type": ["consultant", "registrar"]},
                ),
                columns=[_from_col("actor_index", "record_index")],
            ),
            _fact(
                "fact_journey",
                "records",
                "journey_instance",
                [
                    _from_col("record_id", "record_id"),
                    _fk_col("actor_id", "dim_actor_clinical", "reference"),
                ],
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(
            emit.sidecar,
            {"actor": {"consultant": "record_index", "registrar": "record_index"}},
        )
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_journey")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(zip(rows["record_id"], rows["actor_id"]))
    assert by_id["j1"] == 0
    assert by_id["j2"] == 1
    assert by_id["j3"] is None


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
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_HOLDER_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]

# decision columns: references journey_instance + has last_mutation_sim_time
_PIT_DECISION_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__journey_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="journey_instance",
    ),
    identity_column("ref_index__journey_id", "BIGINT"),
]

_PIT_JOURNEY_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="actor",
    ),
    identity_column("ref_index__actor_id", "BIGINT"),
]

_PIT_ACTOR_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
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
        'INSERT INTO "records__owner" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "o001", "OWN_001", 100, True, 100, 0],
    )
    conn.execute(
        'INSERT INTO "records__owner" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "o002", "OWN_002", 100, True, 100, 1],
    )

    # actor rows
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "a001", 100, True, 100, 0],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "a002", 100, True, 100, 1],
    )

    # journey_instance rows: j001 -> a001 (ref_index__actor_id=0), j002 -> a002 (ref_index__actor_id=1)
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 100, True, 100, 0, "a001", 0],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j002", 100, True, 100, 1, "a002", 1],
    )

    # decision rows (grain): all reference journey j001 (record_index 0) or j002
    # (record_index 1) -> ref_index__journey_id follows accordingly.
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 15, True, 15, 0, "j001", 0],  # T=15, member=a001
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d002", 35, True, 35, 1, "j001", 0],  # T=35, member=a001, no hold
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d003", 60, True, 60, 2, "j001", 0],  # T=60, member=a001, open hold
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d004", 20, True, 20, 3, "j002", 1],  # T=20, member=a002
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


# ---------------------------------------------------------------------------
# Point-in-time membership FK: `where` predicate on elem__ columns
#
# Regression: FkClause.where was silently dropped on the PIT builder while the
# on_records / membership-grain paths rendered it. A `where` must narrow the
# resolved interval (matching the other membership paths), never be ignored.
# ---------------------------------------------------------------------------

_HOLDER_WITH_ROLE_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
    {"name": "elem__role", "type": "VARCHAR"},
]


def build_pit_membership_where_emit(tmp_path: Path) -> Path:
    """Emit for PIT membership FK `where` tests.

    Decision d001 fires at T=15, resolving member actor a001 via the journey
    reference path. At T=15 a001 is held by BOTH owners (overlapping intervals),
    distinguished only by elem__role:
      - o001: [10..30], role='secondary'
      - o002: [12..30], role='primary'  (later joined_sim_time — the natural
              deterministic winner of ORDER BY joined_sim_time DESC)

    So the PIT resolution WITHOUT a where predicate yields o002. A where of
    {elem__role: 'secondary'} must instead yield o001, and {elem__role:
    'primary'} must yield o002 — proving the predicate filters.
    """
    import duckdb

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__owner", _OWNER_SURROGATE_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _PIT_JOURNEY_COLUMNS))
    conn.execute(_create_ddl("records__actor", _PIT_ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__decision", _PIT_DECISION_COLUMNS))
    conn.execute(_create_ddl("membership__owner__holders", _HOLDER_WITH_ROLE_COLUMNS))

    conn.execute(
        'INSERT INTO "records__owner" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "o001", "OWN_001", 100, True, 100, 0],
    )
    conn.execute(
        'INSERT INTO "records__owner" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "o002", "OWN_002", 100, True, 100, 1],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "a001", 100, True, 100, 0],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 100, True, 100, 0, "a001", 0],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 15, True, 15, 0, "j001", 0],  # T=15, member=a001
    )

    # Two overlapping holds of a001 covering T=15, distinguished by role.
    conn.execute(
        'INSERT INTO "membership__owner__holders" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "o001", 10, 30, "actor", "a001", "secondary"],
    )
    conn.execute(
        'INSERT INTO "membership__owner__holders" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "o002", 12, 30, "actor", "a001", "primary"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__owner", "records", _OWNER_SURROGATE_COLUMNS, 2, "owner"
            ),
            _table_spec("records__actor", "records", _PIT_ACTOR_COLUMNS, 1, "actor"),
            _table_spec(
                "records__journey_instance",
                "records",
                _PIT_JOURNEY_COLUMNS,
                1,
                "journey_instance",
            ),
            _table_spec(
                "records__decision", "records", _PIT_DECISION_COLUMNS, 1, "decision"
            ),
            _table_spec(
                "membership__owner__holders",
                "membership",
                _HOLDER_WITH_ROLE_COLUMNS,
                2,
                "owner",
                "holders",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def _resolve_pit_owner_with_where(emit_dir: Path, role: str | list[str]) -> object:
    """Resolve d001's PIT owner_id FK filtered by elem__role against role."""
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
                            "owner_id", "dim_owner", where={"elem__role": role}
                        ),
                    ],
                ),
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
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()
    return dict(zip(rows["record_id"], rows["owner_id"]))["d001"]


def test_pit_membership_where_narrows_resolved_interval(tmp_path: Path) -> None:
    """PIT membership FK `where` filters the resolved hold by an elem__ column.

    Both owners hold a001 at T=15; the where predicate must pick the matching
    role. Without rendering (the old silent-drop bug) resolution always returned
    the deterministic winner o002 regardless of the where value.
    """
    emit_dir = build_pit_membership_where_emit(tmp_path)
    # where='secondary' selects o001 (NOT the deterministic winner) — proves the
    # predicate takes effect. where='primary' selects o002.
    assert _resolve_pit_owner_with_where(emit_dir, "secondary") == "o001"
    assert _resolve_pit_owner_with_where(emit_dir, "primary") == "o002"


def test_pit_membership_where_no_match_is_null(tmp_path: Path) -> None:
    """PIT membership FK `where` matching no hold resolves to NULL (not unfiltered)."""
    emit_dir = build_pit_membership_where_emit(tmp_path)
    assert _resolve_pit_owner_with_where(emit_dir, "nonexistent") is None


def test_pit_membership_where_list_matches_any_listed_role(tmp_path: Path) -> None:
    """PIT membership FK: a list-valued `where` matches any listed elem__ value.

    ['secondary', 'nonexistent'] resolves identically to the scalar
    'secondary' — only the real role has a matching hold, and the list widens
    the candidate set rather than requiring every element to match.
    """
    emit_dir = build_pit_membership_where_emit(tmp_path)
    assert (
        _resolve_pit_owner_with_where(emit_dir, ["secondary", "nonexistent"]) == "o001"
    )


def test_pit_membership_where_presentation_id_projects_surrogate(
    tmp_path: Path,
) -> None:
    """PIT `where` composes with target_key=presentation_id."""
    emit_dir = build_pit_membership_where_emit(tmp_path)
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
                            "owner_id",
                            "dim_owner",
                            where={"elem__role": "secondary"},
                            target_key="presentation_id",
                        ),
                    ],
                ),
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
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    # o001's surrogate is OWN_001 (role='secondary').
    assert dict(zip(rows["record_id"], rows["owner_id"]))["d001"] == "OWN_001"


def test_pit_membership_where_non_elem_column_raises(tmp_path: Path) -> None:
    """PIT `where` on a non-elem__ column fails fast (never silently ignored)."""
    emit_dir = build_pit_membership_where_emit(tmp_path)
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
                            "owner_id",
                            "dim_owner",
                            where={"member__actor__id": "a001"},
                        ),
                    ],
                ),
            ]
        )
        with pytest.raises(ExportError, match="not an elem__ column"):
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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

        specs_fact_first = build_query_specs(
            emit,
            config_fact_first,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        specs_dim_first = build_query_specs(
            emit,
            config_dim_first,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

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
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a001", "PAT_001", 10, True, 10, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a002", "PAT_002", 20, True, 20, 1, "Bob"],
    )
    # prop__journey_id references journey_instance, which this fixture does not
    # emit -- a dangling reference, so ref_index__journey_id is the fixture's
    # dangling sentinel (-1), never verified (see build_refs_dangling precedent).
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d001", 10, True, 10, 0, "j001", -1],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d002", 20, True, 20, 1, "j002", -1],
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
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
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    consultant = dict(zip(rows["record_id"], rows["consultant_id"]))
    nurse = dict(zip(rows["record_id"], rows["nurse_id"]))
    # d001 has a consultant binding (a001); d002 has a nurse binding (a002).
    assert consultant["d001"] == "a001"
    assert consultant["d002"] is None
    assert nurse["d002"] == "a002"
    assert nurse["d001"] is None


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused over fk-traversed hops — unit tests on
# check_fk_slice_only. Consults only the sidecar (never queries run.duckdb),
# so these skip open_emit/write_emit and parse a bare sidecar dict directly —
# mirroring tests/exporters/test_slice_only.py's own helper.
# ---------------------------------------------------------------------------


def _bare_sidecar(
    tables: list[dict[str, object]],
    enum_domains: dict[str, dict[str, list[str]]] | None = None,
) -> Sidecar:
    """Build a minimal Sidecar (no DuckDB) for check_fk_slice_only unit tests."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": tables,
    }
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    return Sidecar.from_raw(raw)


def _records_table(
    name: str, kind: str, columns: list[dict[str, object]]
) -> dict[str, object]:
    """Build one records-category table entry for _bare_sidecar."""
    return {
        "name": name,
        "category": "records",
        "record_kind": kind,
        "columns": columns,
        "rows": 0,
    }


def _reference_hop_sidecar(hop_column: dict[str, object]) -> Sidecar:
    """Sidecar with journey_instance.<hop_column> -> actor, plus a bare actor table."""
    return _bare_sidecar(
        [
            _records_table(
                "records__journey_instance",
                "journey_instance",
                [identity_column("record_id", "VARCHAR"), hop_column],
            ),
            _records_table(
                "records__actor", "actor", [identity_column("record_id", "VARCHAR")]
            ),
        ]
    )


_SLICE_ONLY_HOP_COLUMN = prop_column(
    "prop__actor_id",
    "VARCHAR",
    history_tracked=False,
    temporal_class="slice_only",
    references="actor",
)


def test_fk_reference_hop_path_hint_refuses_slice_only() -> None:
    """fk via:reference with an author-hinted path: a slice_only hop is refused."""
    sidecar = _reference_hop_sidecar(_SLICE_ONLY_HOP_COLUMN)
    tbl = _fact(
        "fact_journey",
        "records",
        "journey_instance",
        [
            _from_col("record_id", "record_id"),
            _fk_col("actor_id", "dim_actor", "reference", path=["prop__actor_id"]),
        ],
    )
    col = tbl.columns[1]
    with pytest.raises(ExportError, match="temporal_class: slice_only"):
        check_fk_slice_only(
            col_decl=col,
            table_decl=tbl,
            source_grain="records",
            anchor_kind="journey_instance",
            target_kind="actor",
            sidecar=sidecar,
        )


def test_fk_reference_hop_pathfound_refuses_slice_only() -> None:
    """fk via:reference with pathfind (no path hint): a slice_only hop is refused."""
    sidecar = _reference_hop_sidecar(_SLICE_ONLY_HOP_COLUMN)
    tbl = _fact(
        "fact_journey",
        "records",
        "journey_instance",
        [
            _from_col("record_id", "record_id"),
            _fk_col("actor_id", "dim_actor", "reference"),
        ],
    )
    col = tbl.columns[1]
    with pytest.raises(ExportError, match="temporal_class: slice_only"):
        check_fk_slice_only(
            col_decl=col,
            table_decl=tbl,
            source_grain="records",
            anchor_kind="journey_instance",
            target_kind="actor",
            sidecar=sidecar,
        )


def test_fk_reference_hop_exempt_discriminator_passes() -> None:
    """A reference hop landing on the exempt discriminator passes at any class.

    The carve-out is mechanical and identical on every surface: even a hop
    column shaped as the owning kind's discriminator (prop__<kind>_type) with
    a non-empty subtype_values is exempt regardless of its declared class.
    """
    sidecar = _bare_sidecar(
        [
            _records_table(
                "records__journey_instance",
                "journey_instance",
                [
                    identity_column("record_id", "VARCHAR"),
                    prop_column(
                        "prop__journey_instance_type",
                        "VARCHAR",
                        history_tracked=False,
                        temporal_class="slice_only",
                        references="actor",
                    ),
                ],
            ),
            _records_table(
                "records__actor", "actor", [identity_column("record_id", "VARCHAR")]
            ),
        ],
        enum_domains={"journey_instance": {"journey_instance_type": ["a", "b"]}},
    )
    tbl = _fact(
        "fact_journey",
        "records",
        "journey_instance",
        [
            _from_col("record_id", "record_id"),
            _fk_col(
                "actor_id",
                "dim_actor",
                "reference",
                path=["prop__journey_instance_type"],
            ),
        ],
    )
    col = tbl.columns[1]
    check_fk_slice_only(
        col_decl=col,
        table_decl=tbl,
        source_grain="records",
        anchor_kind="journey_instance",
        target_kind="actor",
        sidecar=sidecar,
    )  # must not raise


def test_fk_membership_member_path_hop_refuses_slice_only() -> None:
    """fk via:membership: a slice_only member_path-traversed hop is refused."""
    sidecar = _bare_sidecar(
        [
            _records_table(
                "records__decision",
                "decision",
                [
                    identity_column("record_id", "VARCHAR"),
                    prop_column(
                        "prop__journey_id",
                        "VARCHAR",
                        history_tracked=False,
                        temporal_class="slice_only",
                        references="journey_instance",
                    ),
                ],
            ),
            _records_table(
                "records__journey_instance",
                "journey_instance",
                [identity_column("record_id", "VARCHAR")],
            ),
        ]
    )
    col = ColumnDecl(
        name="owner_id",
        fk=FkClause(
            to="dim_owner",
            via="membership",
            property="holders",
            member_field="actor",
            as_of="last_mutation_sim_time",
            member_path=["prop__journey_id"],
        ),
    )
    tbl = _fact(
        "fact_decision",
        "records",
        "decision",
        [_from_col("record_id", "record_id"), col],
    )
    with pytest.raises(ExportError, match="temporal_class: slice_only"):
        check_fk_slice_only(
            col_decl=col,
            table_decl=tbl,
            source_grain="records",
            anchor_kind="decision",
            target_kind="owner",
            sidecar=sidecar,
        )


def test_fk_membership_as_of_column_refuses_slice_only() -> None:
    """fk via:membership point-in-time: a slice_only as_of column is refused."""
    sidecar = _bare_sidecar(
        [
            _records_table(
                "records__decision",
                "decision",
                [
                    identity_column("record_id", "VARCHAR"),
                    prop_column(
                        "prop__journey_id",
                        "VARCHAR",
                        history_tracked=False,
                        temporal_class="constant",
                        references="journey_instance",
                    ),
                    prop_column(
                        "prop__fired_at",
                        "BIGINT",
                        history_tracked=False,
                        temporal_class="slice_only",
                    ),
                ],
            ),
            _records_table(
                "records__journey_instance",
                "journey_instance",
                [identity_column("record_id", "VARCHAR")],
            ),
        ]
    )
    col = ColumnDecl(
        name="owner_id",
        fk=FkClause(
            to="dim_owner",
            via="membership",
            property="holders",
            member_field="actor",
            as_of="prop__fired_at",
            member_path=["prop__journey_id"],
        ),
    )
    tbl = _fact(
        "fact_decision",
        "records",
        "decision",
        [_from_col("record_id", "record_id"), col],
    )
    with pytest.raises(ExportError, match="temporal_class: slice_only"):
        check_fk_slice_only(
            col_decl=col,
            table_decl=tbl,
            source_grain="records",
            anchor_kind="decision",
            target_kind="owner",
            sidecar=sidecar,
        )
