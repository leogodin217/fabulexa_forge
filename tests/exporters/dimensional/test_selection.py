"""Tests for `build_query_specs(..., tables=)`: per-table compile, selection
order, projection invariance, selection-scoped notices/guards, horizon
economy, and the undeclared-name assertion (`exporters/dimensional/engine.py`).

Three declared tables drive most of this module: `dim_gadget` (type-1 dim,
'snapshot' delivery, filtered on an unobserved discriminator value),
`fact_append` (reads only horizon-invariant channels, 'append' delivery, no
filter), `fact_upsert` (reads a tracked channel, 'upsert' delivery, filtered
on the same unobserved value as `dim_gadget`). `fact_dupkey`, keyed on a
column two rows share, is used only by the WindowKeyDuplicate tests. The
dim-side-leg tests reuse `test_election_fk.py`'s guard fixtures directly,
per the sprint's test plan.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import (
    enum_options,
    identity_column,
    prop_column,
    write_emit,
)

from exporters._emit_fixtures import _create_ddl, _table_spec
from exporters.dimensional.test_election_fk import _guard_config, build_guard_emit
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    FkClause,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ElectedKeyDuplicate, ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.query_spec import QuerySpec
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Emit fixture: gadget (dim source), widget (fact_append + fact_upsert
# source), part (fact_dupkey source).
# ---------------------------------------------------------------------------

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_GADGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__category", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_WIDGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__gadget_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="gadget",
    ),
    identity_column("ref_index__gadget_id", "BIGINT"),
    prop_column(
        "prop__category", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_PART_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__category", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

#: The window every windowed compile in this module shares: [0, 100). Every
#: widget/part row is created below 100 (10, 20, 60, 70), so both horizons
#: see every row.
_WINDOW = Window(index=0, start_ns=0, end_ns=100, label="w0")


def _build_selection_emit(tmp_path: Path) -> Path:
    """Write the emit `_selection_config` / `_duplicate_key_config` compile against.

    gadget: one dim row. widget: two rows sharing a gadget reference
    (fact_append's fk edge) with distinct statuses (fact_upsert's varying
    channel). part: two rows sharing a category (fact_dupkey's duplicate
    key) with distinct statuses (making fact_dupkey 'upsert'-class).

    Args:
        tmp_path: Directory for the emit artifacts.

    Returns:
        tmp_path (the emit directory).
    """
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_create_ddl("records__gadget", _GADGET_COLUMNS))
    conn.execute(_create_ddl("records__widget", _WIDGET_COLUMNS))
    conn.execute(_create_ddl("records__part", _PART_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__gadget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "g1", 0, True, 0, 0, "A"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?),"
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "w1",
            10,
            True,
            10,
            0,
            "g1",
            0,
            "A",
            "new",
            "trunk",
            "w2",
            60,
            True,
            60,
            1,
            "g1",
            0,
            "A",
            "used",
        ],
    )
    conn.execute(
        'INSERT INTO "records__part" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?),"
        " (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        [
            "trunk",
            "p1",
            20,
            True,
            20,
            0,
            "A",
            "new",
            "trunk",
            "p2",
            70,
            True,
            70,
            1,
            "A",
            "used",
        ],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__gadget", "records", _GADGET_COLUMNS, 1, "gadget"),
            _table_spec("records__widget", "records", _WIDGET_COLUMNS, 2, "widget"),
            _table_spec("records__part", "records", _PART_COLUMNS, 2, "part"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 200}],
        extra={
            "enum_domains": {
                "gadget": {"category": enum_options("A", "B")},
                "widget": {"category": enum_options("A", "B")},
            }
        },
    )
    return tmp_path


def _from_col(name: str, src: str) -> ColumnDecl:
    return ColumnDecl(name=name, **{"from": src})


def _fk_col(name: str, to: str) -> ColumnDecl:
    return ColumnDecl(name=name, fk=FkClause(to=to, via="reference"))


def _selection_config() -> DimensionalConfig:
    """dim_gadget ('snapshot') + fact_append ('append') + fact_upsert ('upsert').

    dim_gadget and fact_upsert each filter prop__category on the unobserved
    value 'Z', so each compile of either emits one plan notice.
    """
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_gadget",
                role="dim",
                scd="type1",
                source=SourceDecl(
                    grain="records", kind="gadget", filter={"prop__category": "Z"}
                ),
                key=["record_id"],
                columns=[_from_col("record_id", "record_id")],
            ),
            TableDecl(
                name="fact_append",
                role="fact",
                source=SourceDecl(grain="records", kind="widget"),
                key=["record_id"],
                columns=[
                    _from_col("record_id", "record_id"),
                    _fk_col("gadget_ref", "dim_gadget"),
                ],
            ),
            TableDecl(
                name="fact_upsert",
                role="fact",
                source=SourceDecl(
                    grain="records", kind="widget", filter={"prop__category": "Z"}
                ),
                key=["record_id"],
                columns=[
                    _from_col("record_id", "record_id"),
                    _from_col("status", "prop__status"),
                ],
            ),
        ]
    )


def _duplicate_key_config() -> DimensionalConfig:
    """dim_gadget + fact_dupkey, keyed on a stable column two rows share."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name="dim_gadget",
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="gadget"),
                key=["record_id"],
                columns=[_from_col("record_id", "record_id")],
            ),
            TableDecl(
                name="fact_dupkey",
                role="fact",
                source=SourceDecl(grain="records", kind="part"),
                key=["category"],
                columns=[
                    _from_col("category", "prop__category"),
                    _from_col("status", "prop__status"),
                ],
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Order: tables=None (declaration order); a selection (declaration order,
# regardless of the collection's own iteration order); a repeated name.
# ---------------------------------------------------------------------------


def test_tables_none_returns_declared_order_full_export(tmp_path: Path) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _selection_config(),
            None,
            None,
            discard_notice_sink,
            base_relations=None,
            tables=None,
        )
    assert [s.table_name for s in specs] == ["dim_gadget", "fact_append", "fact_upsert"]


def test_tables_none_returns_declared_order_windowed(tmp_path: Path) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _selection_config(),
            None,
            _WINDOW,
            discard_notice_sink,
            base_relations=None,
            tables=None,
        )
    assert [s.table_name for s in specs] == ["dim_gadget", "fact_append", "fact_upsert"]


def test_selection_returns_declared_order_regardless_of_iteration_order(
    tmp_path: Path,
) -> None:
    """A selection list ordered opposite to declaration order still yields
    the selected specs in declaration order."""
    emit_dir = _build_selection_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _selection_config(),
            None,
            None,
            discard_notice_sink,
            base_relations=None,
            tables=["fact_upsert", "dim_gadget"],
        )
    assert [s.table_name for s in specs] == ["dim_gadget", "fact_upsert"]


def test_selection_repeated_name_yields_one_spec(tmp_path: Path) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _selection_config(),
            None,
            None,
            discard_notice_sink,
            base_relations=None,
            tables=["fact_upsert", "fact_upsert"],
        )
    assert [s.table_name for s in specs] == ["fact_upsert"]


# ---------------------------------------------------------------------------
# Projection invariance: a singleton selection's spec equals the same
# table's spec under tables=None, full export and windowed.
# ---------------------------------------------------------------------------


def _assert_spec_equal(selected: QuerySpec, whole: QuerySpec) -> None:
    """Compare the fields projection invariance covers: sql, write_mode,
    upsert_key, provenance."""
    assert selected.sql == whole.sql
    assert selected.write_mode == whole.write_mode
    assert selected.upsert_key == whole.upsert_key
    assert selected.provenance == whole.provenance


def test_projection_invariance_full_export(tmp_path: Path) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = _selection_config()
    with open_emit(emit_dir) as emit:
        whole_specs = build_query_specs(
            emit,
            config,
            None,
            None,
            discard_notice_sink,
            base_relations=None,
            tables=None,
        )
    for whole in whole_specs:
        with open_emit(emit_dir) as emit:
            selected = build_query_specs(
                emit,
                config,
                None,
                None,
                discard_notice_sink,
                base_relations=None,
                tables={whole.table_name},
            )
        assert len(selected) == 1
        _assert_spec_equal(selected[0], whole)


def test_projection_invariance_windowed(tmp_path: Path) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = _selection_config()
    with open_emit(emit_dir) as emit:
        whole_specs = build_query_specs(
            emit,
            config,
            None,
            _WINDOW,
            discard_notice_sink,
            base_relations=None,
            tables=None,
        )
    for whole in whole_specs:
        with open_emit(emit_dir) as emit:
            selected = build_query_specs(
                emit,
                config,
                None,
                _WINDOW,
                discard_notice_sink,
                base_relations=None,
                tables={whole.table_name},
            )
        assert len(selected) == 1
        _assert_spec_equal(selected[0], whole)


# ---------------------------------------------------------------------------
# Unselected notices/guards are skipped.
# ---------------------------------------------------------------------------


def test_unselected_table_notice_not_emitted(tmp_path: Path) -> None:
    """dim_gadget's discriminator-value-unobserved notice fires under
    tables=None; a sibling-only selection that never compiles dim_gadget
    emits nothing."""
    emit_dir = _build_selection_emit(tmp_path)
    config = _selection_config()

    whole_sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit, config, None, None, whole_sink, base_relations=None, tables=None
        )
    assert any(n.code == "discriminator-value-unobserved" for n in whole_sink.notices)

    sibling_sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit,
            config,
            None,
            None,
            sibling_sink,
            base_relations=None,
            tables={"fact_append"},
        )
    assert sibling_sink.notices == []


def test_unselected_upsert_duplicate_key_does_not_refuse_sibling(
    tmp_path: Path,
) -> None:
    """fact_dupkey's duplicate key never runs WindowKeyDuplicate when it is
    not the selection — a sibling-only windowed compile succeeds."""
    emit_dir = _build_selection_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _duplicate_key_config(),
            None,
            _WINDOW,
            discard_notice_sink,
            base_relations=None,
            tables={"dim_gadget"},
        )
    assert [s.table_name for s in specs] == ["dim_gadget"]


def test_selecting_duplicate_key_table_refuses(tmp_path: Path) -> None:
    """Selecting fact_dupkey runs its per-table compile and WindowKeyDuplicate
    refuses the duplicate key."""
    emit_dir = _build_selection_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError):
            build_query_specs(
                emit,
                _duplicate_key_config(),
                None,
                _WINDOW,
                discard_notice_sink,
                base_relations=None,
                tables={"fact_dupkey"},
            )


# ---------------------------------------------------------------------------
# Horizon economy: a snapshot-only selection opens one horizon (each notice
# once); a selection containing an upsert or append table opens both (each
# notice twice) — a selection-wide decision, not a per-table one.
# ---------------------------------------------------------------------------


def test_horizon_economy_snapshot_only_selection_delivers_notice_once(
    tmp_path: Path,
) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit,
            _selection_config(),
            None,
            _WINDOW,
            sink,
            base_relations=None,
            tables={"dim_gadget"},
        )
    assert len(sink.notices) == 1


def test_horizon_economy_upsert_selection_delivers_notice_twice(tmp_path: Path) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit,
            _selection_config(),
            None,
            _WINDOW,
            sink,
            base_relations=None,
            tables={"fact_upsert"},
        )
    assert len(sink.notices) == 2


def test_horizon_economy_append_sibling_delivers_dim_notice_twice(
    tmp_path: Path,
) -> None:
    """fact_append itself never notices; joining it to the selection still
    forces the second horizon, doubling dim_gadget's notice count."""
    emit_dir = _build_selection_emit(tmp_path)
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        build_query_specs(
            emit,
            _selection_config(),
            None,
            _WINDOW,
            sink,
            base_relations=None,
            tables={"dim_gadget", "fact_append"},
        )
    assert len(sink.notices) == 2


# ---------------------------------------------------------------------------
# Dim-side leg locality: reuses test_election_fk.py's guard fixtures.
# `_guard_fk_columns` resolves the fk target through `config`, not the
# compiled selection, so a selected fact still catches a corrupted
# presentation_id on the dim it reaches even when the dim itself is not
# selected; the dim compiled alone never runs the check (`dim_decls` /
# `dim_surfaces` only ever accumulate from a compiled fact's edges).
# ---------------------------------------------------------------------------


def test_dim_side_leg_guard_runs_when_fact_selected_dim_is_not(tmp_path: Path) -> None:
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
                tables={"fact_booking"},
            )


def test_dim_side_leg_guard_skipped_when_dim_selected_alone(tmp_path: Path) -> None:
    emit_dir = build_guard_emit(tmp_path, corrupt_within_alpha=True)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _guard_config(),
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            election=resolve_election(emit.sidecar, {"entity": "presentation_id"}),
            tables={"dim_entity_alpha"},
        )
    assert [s.table_name for s in specs] == ["dim_entity_alpha"]


# ---------------------------------------------------------------------------
# An undeclared name in `tables` is a programming error, not a config refusal.
# ---------------------------------------------------------------------------


def test_undeclared_selection_name_asserts(tmp_path: Path) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        with pytest.raises(AssertionError):
            build_query_specs(
                emit,
                _selection_config(),
                None,
                None,
                discard_notice_sink,
                base_relations=None,
                tables={"nonexistent_table"},
            )
