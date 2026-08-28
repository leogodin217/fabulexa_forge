"""Tests for dimensional key election at FK-render and engine-guard time:
FK inheritance/override rendering over the destination dim's restricted
source population set (the FK condition table, all four `via` builders),
`correlation:` columns staying verbatim under any election, the dim-key
agreement gate, the edge union-safety gate under an explicit `target_key`
override, the engine's render-time uniqueness guard (spine iff
`proper_subset`, the dim-side leg), and `ExportConfig.keys` threading through
the incremental driver and tier-2 shaped playback
(`exporters/dimensional/fk.py`, `validation.py`, `engine.py`,
`incremental/driver.py`, `playback/shaped.py`).

`build_query_specs` is the single compile entry point every rendering /
gating test goes through (mirroring `test_fk.py`); the engine-guard and
threading sections go through `build_query_specs` with a `window`, the
incremental driver's `export_window` / `export_incremental_next`, and
`playback.shaped.open_shaped_playback` respectively, since the guard and the
threading are engine/driver-level concerns.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    FkClause,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import (
    ElectedKeyDuplicate,
    ElectionDimKeyDisagrees,
    ElectionInheritanceAmbiguous,
    ElectionPresentationUndeclared,
    ElectionUnionUnsafe,
    IncrementalFingerprintMismatch,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.incremental.driver import export_incremental_next, export_window
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.playback.shaped import open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Config-building helpers (mirrors test_fk.py's `_dim` / `_fact` / `_fk_col`)
# ---------------------------------------------------------------------------


def _from_col(name: str, src: str) -> ColumnDecl:
    return ColumnDecl(name=name, **{"from": src})


def _fk_col(name: str, to: str, via: str, **kwargs: object) -> ColumnDecl:
    return ColumnDecl(name=name, fk=FkClause(to=to, via=via, **kwargs))


def _dim_entity(
    name: str, key_from: str, sub_type: str | list[str] | None = None
) -> TableDecl:
    """A dim over `entity`, optionally filtered to one or several discriminator
    values (a scalar or a list conjunct)."""
    return TableDecl(
        name=name,
        role="dim",
        scd="type1",
        key=["entity_key"],
        source=SourceDecl(
            grain="records",
            kind="entity",
            filter={"prop__entity_type": sub_type} if sub_type is not None else None,
        ),
        columns=[ColumnDecl(name="entity_key", **{"from": key_from})],
    )


def _fact_booking(*cols: ColumnDecl) -> TableDecl:
    return TableDecl(
        name="fact_booking",
        role="fact",
        key=["booking_key"],
        source=SourceDecl(grain="records", kind="booking"),
        columns=[
            _from_col("booking_key", "record_id"),
            # Projects the records grain's window key — required for the
            # windowed (Threading section) invocations; harmless elsewhere.
            _from_col("mutated_at", "last_mutation_sim_time"),
            *cols,
        ],
    )


def _fact_staff(*cols: ColumnDecl) -> TableDecl:
    """The via:membership on-membership-grain fact over membership__booking__staff."""
    return TableDecl(
        name="fact_staff",
        role="fact",
        key=["record_id"],
        source=SourceDecl(grain="membership", kind="booking", property="staff"),
        columns=[_from_col("record_id", "record_id"), *cols],
    )


# ---------------------------------------------------------------------------
# Primary fixture: entity (sub-typed alpha/beta) + booking (reference FK +
# membership owner) + membership__booking__staff.
# ---------------------------------------------------------------------------

_ENTITY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__entity_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_BOOKING_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__entity_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="entity",
    ),
    identity_column("ref_index__entity_id", "BIGINT"),
]

_STAFF_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
    {"name": "member__entity__kind", "type": "VARCHAR"},
    {"name": "member__entity__id", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_ALPHA_ONLY_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "ALPHA_", "width": 3},
            }
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

#: The election used across the rendering / dim-key-agreement tests: alpha
#: elects presentation_id (registry-declared), beta elects record_index (no
#: registry needed) — a single mixed-election `entity` kind.
_MIXED_ELECTION_KEYS: dict[str, object] = {
    "entity": {"alpha": "presentation_id", "beta": "record_index"}
}


def build_star_emit(tmp_path: Path) -> Path:
    """entity (alpha/beta) + booking (reference to entity) + membership
    __booking__staff (owner=booking, member=entity).

    entity: e1 alpha (presentation_id 'ALPHA_001', registry-declared),
    e2 beta (presentation_id NULL — undeclared, elects record_index instead).

    booking: b1 -> e1 (in dim_entity_alpha's alpha-only population set),
    b2 -> e2 (out-of-set: beta), b3 -> no entity (absent property),
    b4 -> 'e999' (dangled sentinel — no such entity record).

    membership__booking__staff: b1/lead -> e1 (in-set), b1/backup -> e2
    (out-of-set).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_create_ddl("records__booking", _BOOKING_COLUMNS))
    conn.execute(_create_ddl("membership__booking__staff", _STAFF_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e1", "ALPHA_001", 10, True, 10, 0, "alpha"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e2", None, 10, True, 10, 1, "beta"],
    )

    conn.execute(
        'INSERT INTO "records__booking" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "b1", 20, True, 20, 0, "e1", 0],
    )
    conn.execute(
        'INSERT INTO "records__booking" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "b2", 21, True, 21, 1, "e2", 1],
    )
    conn.execute(
        'INSERT INTO "records__booking" VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL)',
        ["trunk", "b3", 22, True, 22, 2],
    )
    conn.execute(
        'INSERT INTO "records__booking" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "b4", 23, True, 23, 3, "e999", 999],
    )

    conn.execute(
        'INSERT INTO "membership__booking__staff" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "b1", 5, "lead", "entity", "e1"],
    )
    conn.execute(
        'INSERT INTO "membership__booking__staff" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "b1", 6, "backup", "entity", "e2"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__entity", "records", _ENTITY_COLUMNS, 2, "entity"),
            _table_spec("records__booking", "records", _BOOKING_COLUMNS, 4, "booking"),
            _table_spec(
                "membership__booking__staff",
                "membership",
                _STAFF_COLUMNS,
                2,
                "booking",
                "staff",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {"entity": {"entity_type": ["alpha", "beta"]}},
            "presentation_keys": _ALPHA_ONLY_PRESENTATION_KEYS,
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# FK rendering: inheritance, explicit override, the four-row condition table
# ---------------------------------------------------------------------------


def test_reference_fk_inherited_and_override_condition_table(tmp_path: Path) -> None:
    """The reference-FK condition table under an inherited presentation_id
    surface (absent / in-set / out-of-set / dangled), beside an explicit
    target_key: record_index override rendering BIGINT indices — restricted
    to the same alpha-only population set."""
    emit_dir = build_star_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "presentation_id", sub_type="alpha"),
            _fact_booking(
                _fk_col("entity_alpha_id", "dim_entity_alpha", "reference"),
                _fk_col(
                    "entity_alpha_index_id",
                    "dim_entity_alpha",
                    "reference",
                    target_key="record_index",
                ),
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(
        zip(
            rows["booking_key"],
            zip(rows["entity_alpha_id"], rows["entity_alpha_index_id"]),
        )
    )
    assert by_id["b1"] == ("ALPHA_001", 0)  # in-set: renders both surfaces
    assert by_id["b2"] == (None, None)  # out-of-set (beta) -> NULL under both
    assert by_id["b3"] == (None, None)  # absent property -> NULL
    assert by_id["b4"] == (None, None)  # dangled sentinel -> NULL
    assert isinstance(by_id["b1"][1], int)


def test_membership_on_records_grain_fk_in_set_and_out_of_set(tmp_path: Path) -> None:
    """via:membership (records grain, `where`-selected) inherits the same
    restricted surface: b1's lead (alpha) resolves, its backup (beta) is
    out-of-set NULL."""
    emit_dir = build_star_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "presentation_id", sub_type="alpha"),
            _fact_booking(
                _fk_col(
                    "lead_id",
                    "dim_entity_alpha",
                    "membership",
                    where={"elem__role": "lead"},
                ),
                _fk_col(
                    "backup_id",
                    "dim_entity_alpha",
                    "membership",
                    where={"elem__role": "backup"},
                ),
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(zip(rows["booking_key"], zip(rows["lead_id"], rows["backup_id"])))
    assert by_id["b1"] == ("ALPHA_001", None)


def test_membership_on_membership_grain_fk_in_set_and_out_of_set(
    tmp_path: Path,
) -> None:
    """via:membership on the membership grain itself: the lead row's entity
    (alpha) resolves, the backup row's (beta) is out-of-set NULL."""
    emit_dir = build_star_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "presentation_id", sub_type="alpha"),
            _fact_staff(
                _from_col("role", "elem__role"),
                _fk_col("entity_id", "dim_entity_alpha", "membership"),
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_staff")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_role = dict(zip(rows["role"], rows["entity_id"]))
    assert by_role["lead"] == "ALPHA_001"
    assert by_role["backup"] is None


def test_correlation_column_stays_verbatim_record_id_space(tmp_path: Path) -> None:
    """A `correlation:` column projects the raw base-layer value untouched —
    never joined, never resolved through the election — even for a dangled
    reference."""
    emit_dir = build_star_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "presentation_id", sub_type="alpha"),
            _fact_booking(
                ColumnDecl(name="raw_entity_ref", correlation="prop__entity_id"),
                _fk_col("entity_alpha_id", "dim_entity_alpha", "reference"),
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(zip(rows["booking_key"], rows["raw_entity_ref"]))
    assert by_id["b1"] == "e1"
    assert by_id["b2"] == "e2"
    assert by_id["b3"] is None
    assert by_id["b4"] == "e999"  # dangled — correlation never resolves it


# ---------------------------------------------------------------------------
# List-valued dim source population filter: subset inheritance / ambiguity
# ---------------------------------------------------------------------------


def build_three_subtype_emit(tmp_path: Path) -> Path:
    """entity split alpha/beta/gamma + booking referencing one entity each.

    e1 alpha, e2 beta, e3 gamma. b1 -> e1, b2 -> e2, b3 -> e3. A dim filtered
    to `["alpha", "beta"]` selects a proper subset excluding gamma; b3's
    owner (gamma) is out-of-set."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_create_ddl("records__booking", _BOOKING_COLUMNS))

    for record_id, index, sub_type in (
        ("e1", 0, "alpha"),
        ("e2", 1, "beta"),
        ("e3", 2, "gamma"),
    ):
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, NULL, ?, ?, NULL, ?, ?, ?)',
            ["trunk", record_id, 10, True, 10, index, sub_type],
        )
    for booking_id, index, entity_id in (
        ("b1", 0, "e1"),
        ("b2", 1, "e2"),
        ("b3", 2, "e3"),
    ):
        conn.execute(
            'INSERT INTO "records__booking" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
            [
                "trunk",
                booking_id,
                20 + index,
                True,
                20 + index,
                index,
                entity_id,
                index,
            ],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__entity", "records", _ENTITY_COLUMNS, 3, "entity"),
            _table_spec("records__booking", "records", _BOOKING_COLUMNS, 3, "booking"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {"entity": {"entity_type": ["alpha", "beta", "gamma"]}},
        },
    )
    return tmp_path


def test_list_filtered_subset_electing_one_surface_inherits_it(tmp_path: Path) -> None:
    """A dim filtered to a two-element list subset (alpha, beta), both
    electing record_index, inherits it: in-set owners resolve, the
    out-of-set (gamma) owner is NULL."""
    emit_dir = build_three_subtype_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_ab", "record_index", sub_type=["alpha", "beta"]),
            _fact_booking(_fk_col("entity_id", "dim_entity_ab", "reference")),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(
            emit.sidecar, {"entity": {"alpha": "record_index", "beta": "record_index"}}
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
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(zip(rows["booking_key"], rows["entity_id"]))
    assert by_id["b1"] == 0
    assert by_id["b2"] == 1
    assert by_id["b3"] is None


def test_list_filtered_subset_electing_differing_surfaces_raises_ambiguous(
    tmp_path: Path,
) -> None:
    """A dim filtered to a two-element list subset (alpha, beta) electing
    differing surfaces without an override raises the existing
    ElectionInheritanceAmbiguous, unchanged message."""
    emit_dir = build_three_subtype_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_ab", "record_index", sub_type=["alpha", "beta"]),
            _fact_booking(_fk_col("entity_id", "dim_entity_ab", "reference")),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(
            emit.sidecar, {"entity": {"alpha": "record_index", "beta": "record_id"}}
        )
        with pytest.raises(ElectionInheritanceAmbiguous) as exc_info:
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
                election=election,
            )
    message = str(exc_info.value)
    assert "alpha=record_index" in message
    assert "beta=record_id" in message


# ---------------------------------------------------------------------------
# Subsumption: explicit target_key: presentation_id, no `keys` block
# ---------------------------------------------------------------------------


def test_explicit_presentation_id_subsumption_no_keys_block(tmp_path: Path) -> None:
    """target_key: presentation_id renders alpha codes with no `keys` block
    at all — the pre-election subsumption."""
    emit_dir = build_star_emit(tmp_path)
    config = ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                _dim_entity("dim_entity_alpha", "presentation_id", sub_type="alpha"),
                _fact_booking(
                    _fk_col(
                        "entity_id",
                        "dim_entity_alpha",
                        "reference",
                        target_key="presentation_id",
                    )
                ),
            ]
        ),
    )
    assert config.keys is None
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, config.keys)
        assert config.dimensional is not None
        specs = build_query_specs(
            emit,
            config.dimensional,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(zip(rows["booking_key"], rows["entity_id"]))
    assert by_id["b1"] == "ALPHA_001"


def test_explicit_presentation_id_undeclared_population_in_restricted_set(
    tmp_path: Path,
) -> None:
    """target_key: presentation_id over a beta-filtered dim (a discriminator-
    filtered, proper-subset population set) with an undeclared population
    inside it raises ElectionPresentationUndeclared — the old shipped
    column-presence ExportError is gone."""
    emit_dir = build_star_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_beta", "presentation_id", sub_type="beta"),
            _fact_booking(
                _fk_col(
                    "entity_id",
                    "dim_entity_beta",
                    "reference",
                    target_key="presentation_id",
                )
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
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
# Dim-key agreement
# ---------------------------------------------------------------------------


def test_dim_key_agreement_violation_raises(tmp_path: Path) -> None:
    """A dim keyed `from: record_id` disagrees with an inherited (non-
    default) edge surface."""
    emit_dir = build_star_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "record_id", sub_type="alpha"),
            _fact_booking(_fk_col("entity_id", "dim_entity_alpha", "reference")),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        with pytest.raises(ElectionDimKeyDisagrees, match="dim_entity_alpha"):
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
                election=election,
            )


def test_dim_key_agreement_explicit_target_key_escapes(tmp_path: Path) -> None:
    """An explicit target_key on the edge escapes the dim-key agreement gate
    even though the dim is keyed `from: record_id` — it still renders the
    resolved surface."""
    emit_dir = build_star_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "record_id", sub_type="alpha"),
            _fact_booking(
                _fk_col(
                    "entity_id",
                    "dim_entity_alpha",
                    "reference",
                    target_key="presentation_id",
                )
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(zip(rows["booking_key"], rows["entity_id"]))
    assert by_id["b1"] == "ALPHA_001"


def test_combined_mixed_election_dim_legal_with_every_inbound_edge_explicit(
    tmp_path: Path,
) -> None:
    """An unfiltered dim over entity's whole mixed-election domain is legal
    on its own (no inbound edge at all); adding an inbound edge with an
    explicit target_key stays legal too."""
    emit_dir = build_star_emit(tmp_path)
    dim_only = DimensionalConfig(tables=[_dim_entity("dim_entity_all", "record_id")])
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        # The dim alone, no inbound edge: election renders none of its
        # columns (author-declared key), so it compiles regardless of the
        # kind's mixed election.
        build_query_specs(
            emit,
            dim_only,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )

    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_all", "record_id"),
            _fact_booking(
                _fk_col(
                    "entity_id",
                    "dim_entity_all",
                    "reference",
                    target_key="record_index",
                )
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(emit.sidecar, _MIXED_ELECTION_KEYS)
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=election,
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_id = dict(zip(rows["booking_key"], rows["entity_id"]))
    assert by_id["b1"] == 0  # e1's record_index, unfiltered dim -> in-set


# ---------------------------------------------------------------------------
# Edge gate under override: union-unsafe admitted pair
# ---------------------------------------------------------------------------


_UNSAFE_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
            "beta": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
        },
        # No unique_within at the rollup: the two bare-prefix (comparable)
        # counter spaces are union-unsafe, so the combined-claim algebra
        # computes unique_within=None — the rollup must match it exactly.
        "branch_stable": False,
        "slice_stable": False,
    }
}


def build_union_unsafe_emit(tmp_path: Path) -> Path:
    """entity alpha/beta, both registry-declared with union-unsafe (bare,
    comparable) counter prefixes — an unfiltered dim's explicit
    target_key: presentation_id admits both populations at once. `booking`
    (b1 -> e1) supplies the referencing edge."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_create_ddl("records__booking", _BOOKING_COLUMNS))
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e1", "001", 10, True, 10, 0, "alpha"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e2", "002", 10, True, 10, 1, "beta"],
    )
    conn.execute(
        'INSERT INTO "records__booking" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "b1", 20, True, 20, 0, "e1", 0],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__entity", "records", _ENTITY_COLUMNS, 2, "entity"),
            _table_spec("records__booking", "records", _BOOKING_COLUMNS, 1, "booking"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {"entity": {"entity_type": ["alpha", "beta"]}},
            "presentation_keys": _UNSAFE_PRESENTATION_KEYS,
        },
    )
    return tmp_path


def test_edge_gate_union_unsafe_override_raises(tmp_path: Path) -> None:
    """An explicit target_key: presentation_id admitting both alpha and beta
    over union-unsafe key spaces fails the edge gate — no `keys` block
    needed, the override alone triggers the check."""
    emit_dir = build_union_unsafe_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_all", "record_id"),
            _fact_booking(
                _fk_col(
                    "entity_id",
                    "dim_entity_all",
                    "reference",
                    target_key="presentation_id",
                )
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectionUnionUnsafe):
            build_query_specs(
                emit,
                config,
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
            )


# ---------------------------------------------------------------------------
# via: membership, point-in-time (the fourth `via` builder)
# ---------------------------------------------------------------------------

_PIT_OWNER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__owner_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_PIT_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_PIT_JOURNEY_COLUMNS: list[dict[str, object]] = [
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

_PIT_DECISION_COLUMNS: list[dict[str, object]] = [
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

_PIT_HOLDER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]

_OWNER_ALPHA_ONLY_PRESENTATION_KEYS: dict[str, object] = {
    "owner": {
        "sub_types": {
            "alpha": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "OWN_", "width": 3},
            }
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}


def build_pit_emit(tmp_path: Path) -> Path:
    """owner (alpha/beta) — the PIT FK's OWNER (dim source) kind — holding
    actor members over timed intervals; decision (grain) reaches its member
    identity via journey_instance.

    d1 fires at T=15 for member a1, held by alpha owner o1 -> in-set.
    d2 fires at T=15 for member a2, held by beta owner o2 -> out-of-set.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__owner", _PIT_OWNER_COLUMNS))
    conn.execute(_create_ddl("records__actor", _PIT_ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _PIT_JOURNEY_COLUMNS))
    conn.execute(_create_ddl("records__decision", _PIT_DECISION_COLUMNS))
    conn.execute(_create_ddl("membership__owner__holders", _PIT_HOLDER_COLUMNS))

    conn.execute(
        'INSERT INTO "records__owner" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "o1", "OWN_001", 5, True, 5, 0, "alpha"],
    )
    conn.execute(
        'INSERT INTO "records__owner" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "o2", None, 5, True, 5, 1, "beta"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "a1", 5, True, 5, 0],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "a2", 5, True, 5, 1],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j1", 5, True, 5, 0, "a1", 0],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j2", 5, True, 5, 1, "a2", 1],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d1", 15, True, 15, 0, "j1", 0],
    )
    conn.execute(
        'INSERT INTO "records__decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "d2", 15, True, 15, 1, "j2", 1],
    )
    conn.execute(
        'INSERT INTO "membership__owner__holders" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "o1", 10, "actor", "a1"],
    )
    conn.execute(
        'INSERT INTO "membership__owner__holders" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "o2", 10, "actor", "a2"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__owner", "records", _PIT_OWNER_COLUMNS, 2, "owner"),
            _table_spec("records__actor", "records", _PIT_ACTOR_COLUMNS, 2, "actor"),
            _table_spec(
                "records__journey_instance",
                "records",
                _PIT_JOURNEY_COLUMNS,
                2,
                "journey_instance",
            ),
            _table_spec(
                "records__decision", "records", _PIT_DECISION_COLUMNS, 2, "decision"
            ),
            _table_spec(
                "membership__owner__holders",
                "membership",
                _PIT_HOLDER_COLUMNS,
                2,
                "owner",
                "holders",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {"owner": {"owner_type": ["alpha", "beta"]}},
            "presentation_keys": _OWNER_ALPHA_ONLY_PRESENTATION_KEYS,
        },
    )
    return tmp_path


def _pit_fk_col(name: str, to: str, **kwargs: object) -> ColumnDecl:
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


def test_pit_membership_fk_out_of_set_is_null(tmp_path: Path) -> None:
    """The fourth via builder — point-in-time membership: d1's resolved
    holder (alpha owner o1) is in the alpha-filtered dim's population set
    and renders its code; d2's (beta owner o2) is out-of-set NULL, despite
    resolving to a real owner."""
    emit_dir = build_pit_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            _dim_entity_owner("dim_owner_alpha", "presentation_id", sub_type="alpha"),
            TableDecl(
                name="fact_decision",
                role="fact",
                key=["decision_key"],
                source=SourceDecl(grain="records", kind="decision"),
                columns=[
                    _from_col("decision_key", "record_id"),
                    _pit_fk_col("owner_id", "dim_owner_alpha"),
                ],
            ),
        ]
    )
    with open_emit(emit_dir) as emit:
        election = resolve_election(
            emit.sidecar,
            {"owner": {"alpha": "presentation_id", "beta": "record_index"}},
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
        fact_spec = next(s for s in specs if s.table_name == "fact_decision")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()

    by_decision = dict(zip(rows["decision_key"], rows["owner_id"]))
    assert by_decision["d1"] == "OWN_001"
    assert by_decision["d2"] is None


def _dim_entity_owner(
    name: str, key_from: str, sub_type: str | None = None
) -> TableDecl:
    """A dim over `owner`, optionally filtered to one discriminator value."""
    return TableDecl(
        name=name,
        role="dim",
        scd="type1",
        key=["owner_key"],
        source=SourceDecl(
            grain="records",
            kind="owner",
            filter={"prop__owner_type": sub_type} if sub_type is not None else None,
        ),
        columns=[ColumnDecl(name="owner_key", **{"from": key_from})],
    )


# ---------------------------------------------------------------------------
# Engine: the render-time uniqueness guard (spine iff proper_subset,
# the dim-side leg, per-window under incremental)
# ---------------------------------------------------------------------------

_GUARD_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "ALPHA_", "width": 3},
            },
            "beta": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "BETA_", "width": 3},
            },
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}


def build_guard_emit(
    tmp_path: Path,
    *,
    corrupt_within_alpha: bool = False,
    corrupt_cross_population: bool = False,
) -> Path:
    """entity: e1a/e1b alpha, e2a beta — both sub-types registry-declared.

    `corrupt_within_alpha=True` makes e1b's presentation_id duplicate e1a's
    — a genuine duplicate inside alpha's own population, which the
    alpha-restricted guard must catch. `corrupt_cross_population=True` makes
    e2a's (beta) raw presentation_id duplicate e1a's (alpha) — a duplicate
    only across populations, which the alpha-restricted guard (a semi-join
    spine iff `proper_subset`) must ignore.

    booking: b1 -> e1a (in dim_entity_alpha's alpha-only population set).
    """
    e1b_pid = "ALPHA_001" if corrupt_within_alpha else "ALPHA_002"
    e2a_pid = "ALPHA_001" if corrupt_cross_population else "BETA_001"

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_create_ddl("records__booking", _BOOKING_COLUMNS))
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e1a", "ALPHA_001", 10, True, 10, 0, "alpha"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e1b", e1b_pid, 10, True, 10, 1, "alpha"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "e2a", e2a_pid, 10, True, 10, 2, "beta"],
    )
    conn.execute(
        'INSERT INTO "records__booking" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "b1", 20, True, 20, 0, "e1a", 0],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__entity", "records", _ENTITY_COLUMNS, 3, "entity"),
            _table_spec("records__booking", "records", _BOOKING_COLUMNS, 1, "booking"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {"entity": {"entity_type": ["alpha", "beta"]}},
            "presentation_keys": _GUARD_PRESENTATION_KEYS,
        },
    )
    return tmp_path


def _guard_config() -> DimensionalConfig:
    return DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "presentation_id", sub_type="alpha"),
            _fact_booking(_fk_col("entity_id", "dim_entity_alpha", "reference")),
        ]
    )


def test_guard_restricted_to_own_population_spine_ignores_cross_population_dup(
    tmp_path: Path,
) -> None:
    """The guard's spine restriction (composed iff proper_subset) semi-joins
    alpha's own population only — a raw cross-population duplicate with beta
    never surfaces."""
    emit_dir = build_guard_emit(tmp_path, corrupt_cross_population=True)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _guard_config(),
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=resolve_election(emit.sidecar, {"entity": "presentation_id"}),
        )
        fact_spec = next(s for s in specs if s.table_name == "fact_booking")
        rows = emit.query_arrow(fact_spec.sql, ()).to_pydict()
    assert list(zip(rows["booking_key"], rows["entity_id"])) == [("b1", "ALPHA_001")]


def test_guard_catches_genuine_duplicate_within_population(tmp_path: Path) -> None:
    """A genuine duplicate inside alpha's own restricted spine still fails —
    the guard covers the FK relation and the dim-side leg (dim_entity_alpha
    is keyed `from: presentation_id`, the surface the inbound edge
    resolved)."""
    emit_dir = build_guard_emit(tmp_path, corrupt_within_alpha=True)
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectedKeyDuplicate):
            build_query_specs(
                emit,
                _guard_config(),
                None,
                None,
                notice_sink=discard_notice_sink,
                base_relations=None,
                election=resolve_election(emit.sidecar, {"entity": "presentation_id"}),
            )


def test_guard_per_window_labels_failure(tmp_path: Path) -> None:
    """An incremental (windowed) invocation still guards the elected key,
    labeling the failure with the window's display label."""
    emit_dir = build_guard_emit(tmp_path, corrupt_within_alpha=True)
    window = Window(index=0, start_ns=0, end_ns=1_000, label="w0")
    with open_emit(emit_dir) as emit:
        with pytest.raises(ElectedKeyDuplicate, match=r"\(w0\)"):
            build_query_specs(
                emit,
                _guard_config(),
                None,
                window,
                notice_sink=discard_notice_sink,
                base_relations=None,
                election=resolve_election(emit.sidecar, {"entity": "presentation_id"}),
            )


# ---------------------------------------------------------------------------
# Threading: the incremental driver and tier-2 shaped playback
# ---------------------------------------------------------------------------


def _star_threading_config() -> DimensionalConfig:
    return DimensionalConfig(
        tables=[
            _dim_entity("dim_entity_alpha", "presentation_id", sub_type="alpha"),
            _fact_booking(_fk_col("entity_id", "dim_entity_alpha", "reference")),
        ]
    )


def test_incremental_driver_export_window_threads_election(tmp_path: Path) -> None:
    """`incremental.driver.export_window` resolves `ExportConfig.keys` into
    an election and threads it to the dimensional compile — a keyed
    windowed export renders the elected surface."""
    emit_dir = build_star_emit(tmp_path)
    config = ExportConfig(
        mode="dimensional",
        keys=_MIXED_ELECTION_KEYS,
        dimensional=_star_threading_config(),
    )
    out = tmp_path / "out.duckdb"
    window = Window(index=None, start_ns=0, end_ns=1_000, label="w")
    with open_emit(emit_dir) as emit:
        export_window(
            emit, config, out, "duckdb", None, window, None, discard_notice_sink, None
        )

    conn = duckdb.connect(str(out), read_only=True)
    try:
        row = conn.execute(
            'SELECT "entity_id" FROM "fact_booking" WHERE "booking_key" = ?', ["b1"]
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "ALPHA_001"


def test_shaped_playback_threads_election_into_window_and_state(tmp_path: Path) -> None:
    """`playback.shaped.open_shaped_playback` resolves `config.keys` once
    and threads it to both the windowed compile and the state() compile —
    both render the elected surface."""
    emit_dir = build_star_emit(tmp_path)
    config = ExportConfig(
        mode="dimensional",
        keys=_MIXED_ELECTION_KEYS,
        dimensional=_star_threading_config(),
    )
    with open_emit(emit_dir) as emit:
        playback = open_shaped_playback(emit, config, None, discard_notice_sink)
        windowed = playback.window(0, 1_000)
        stated = playback.state(1_000)

    fact_windowed = next(t for t in windowed if t.name == "fact_booking")
    fact_stated = next(t for t in stated if t.name == "fact_booking")
    windowed_rows = dict(
        zip(
            fact_windowed.table.column("booking_key").to_pylist(),
            fact_windowed.table.column("entity_id").to_pylist(),
        )
    )
    stated_rows = dict(
        zip(
            fact_stated.table.column("booking_key").to_pylist(),
            fact_stated.table.column("entity_id").to_pylist(),
        )
    )
    assert windowed_rows["b1"] == "ALPHA_001"
    assert stated_rows["b1"] == "ALPHA_001"


def test_keys_field_changes_incremental_fingerprint(tmp_path: Path) -> None:
    """`keys` participates in the config fingerprint as an ordinary field —
    flipping it mid-drip trips the existing mismatch rule."""
    emit_dir = build_star_emit(tmp_path)
    dimensional = _star_threading_config()
    keyed = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "keys": _MIXED_ELECTION_KEYS,
            "dimensional": dimensional.model_dump(by_alias=True),
            "incremental": {"sim_period_ns": 500},
        }
    )
    unkeyed = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": dimensional.model_dump(by_alias=True),
            "incremental": {"sim_period_ns": 500},
        }
    )
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_incremental_next(
            emit, keyed, out, "duckdb", None, discard_notice_sink, None
        )

    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalFingerprintMismatch):
            export_incremental_next(
                emit, unkeyed, out, "duckdb", None, discard_notice_sink, None
            )
