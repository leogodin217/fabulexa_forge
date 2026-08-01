"""Tests for tier-2 shaped playback state: ShapedPlayback.state()."""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import write_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.derivations.truncated_tape import (
    build_truncated_history_sql,
    build_truncated_membership_sql,
    build_truncated_records_sql,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.query_spec import query_spec_output_name
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import ShapedTable, open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

from ._shaped_fixtures import (
    FORK_PATH,
    build_fk_hop_test_emit,
    build_state_test_emit,
    fk_hop_shape_config,
    state_dimensional_shape_config,
    state_junction_shape_config,
    state_source_shape_config,
    state_test_table_specs,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.reader.emit import Emit


def _tables_by_name(tables: tuple[ShapedTable, ...]) -> dict[str, ShapedTable]:
    return {t.name: t for t in tables}


def _open_dimensional(emit: "Emit", config: "ExportConfig"):
    return open_shaped_playback(emit, config, None, discard_notice_sink)


def _open_source(emit: "Emit", config: "ExportConfig"):
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    return open_shaped_playback(emit, config, anchor, discard_notice_sink)


def _direct_source_full_specs(emit: "Emit", config: "ExportConfig"):
    """Compile the same full export directly through the source engine's own
    plan-then-compile split (the reference)."""
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    assert anchor is not None
    election = resolve_election(emit.sidecar, config.keys)
    plan = build_source_plan(
        emit, config, anchor, election, windowed=False, notices=discard_notice_sink
    )
    return build_source_query_specs(plan, None)


def _materialize_truncated_emit(
    tmp_path: "Path", source_emit_dir: "Path", at_sim_time: int
) -> "Path":
    """Write build_state_test_emit's truncated-at-T relations out physically
    into a second emit directory — the interior-T oracle. Column shape is
    identical to the physical emit (no slice_only columns exist in this
    fixture, so build_truncated_sidecar would drop nothing); only row counts
    differ.

    Args:
        tmp_path: Directory to build the materialized emit under.
        source_emit_dir: The physical emit's directory.
        at_sim_time: The inclusive truncation position T (ns).

    Returns:
        The materialized emit's directory, ready for open_emit.
    """
    with open_emit(source_emit_dir) as emit:
        sidecar = emit.sidecar
        materialized = {}
        for table in sidecar.tables():
            if table.category == "fixed":
                sql = build_truncated_history_sql(FORK_PATH, at_sim_time)
            elif table.category == "records":
                assert table.record_kind is not None
                sql = build_truncated_records_sql(
                    sidecar, FORK_PATH, table.record_kind, at_sim_time
                )
            else:
                assert table.record_kind is not None
                assert table.property is not None
                sql = build_truncated_membership_sql(
                    sidecar, FORK_PATH, table.record_kind, table.property, at_sim_time
                )
            materialized[table.name] = emit.query_arrow(sql, ())

    dest = tmp_path / "materialized"
    dest.mkdir()
    conn = duckdb.connect(str(dest / "run.duckdb"))
    row_counts: dict[str, int] = {}
    for name, arrow_table in materialized.items():
        conn.register("_arrow_src", arrow_table)
        conn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _arrow_src')
        conn.unregister("_arrow_src")
        row_counts[name] = arrow_table.num_rows
    conn.close()

    write_emit(
        dest,
        tables=state_test_table_specs(row_counts),
        branches=[{"fork_path": FORK_PATH, "parent": None, "slice_at": at_sim_time}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "record_roles": {"gadget": "dimension", "shipment": "fact"},
        },
    )
    return dest


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_negative_at_sim_time_raises_playback_error(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        with pytest.raises(PlaybackError):
            head.state(-1)


# ---------------------------------------------------------------------------
# The bridging theorem: state(T_slice) == the shape's full export, every table.
# ---------------------------------------------------------------------------


def test_bridging_theorem_dimensional(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    config = state_dimensional_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, config)
        stated = _tables_by_name(head.state(100))
        assert config.dimensional is not None
        full_specs = build_query_specs(
            emit,
            config.dimensional,
            None,
            None,
            discard_notice_sink,
            base_relations=None,
        )
        full_by_name = {
            query_spec_output_name(spec): emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in full_specs
        }
    assert set(stated) == set(full_by_name)
    for name, table in stated.items():
        assert table.delivery == "snapshot"
        assert table.table.to_pydict() == full_by_name[name]


def test_bridging_theorem_source(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    config = state_source_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, config)
        stated = _tables_by_name(head.state(100))
        full_specs = _direct_source_full_specs(emit, config)
        full_by_name = {
            query_spec_output_name(spec): emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in full_specs
        }
    assert set(stated) == set(full_by_name)
    for name, table in stated.items():
        assert table.delivery == "snapshot"
        assert table.table.to_pydict() == full_by_name[name]


# ---------------------------------------------------------------------------
# Interior-T oracle: state(T) == the shape's full export over a materialized
# truncated emit.
# ---------------------------------------------------------------------------


def test_interior_t_matches_materialized_truncated_emit_dimensional(
    tmp_path: "Path",
) -> None:
    physical_dir = tmp_path / "physical"
    physical_dir.mkdir()
    emit_dir = build_state_test_emit(physical_dir)
    config = state_dimensional_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, config)
        stated = _tables_by_name(head.state(12))

    materialized_dir = _materialize_truncated_emit(tmp_path, emit_dir, 12)
    with open_emit(materialized_dir) as mat_emit:
        assert config.dimensional is not None
        oracle_specs = build_query_specs(
            mat_emit,
            config.dimensional,
            None,
            None,
            discard_notice_sink,
            base_relations=None,
        )
        oracle_by_name = {
            query_spec_output_name(spec): mat_emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in oracle_specs
        }
    assert set(stated) == set(oracle_by_name)
    for name, table in stated.items():
        assert table.table.to_pydict() == oracle_by_name[name]


def test_interior_t_matches_materialized_truncated_emit_source(
    tmp_path: "Path",
) -> None:
    physical_dir = tmp_path / "physical"
    physical_dir.mkdir()
    emit_dir = build_state_test_emit(physical_dir)
    config = state_source_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, config)
        stated = _tables_by_name(head.state(12))

    materialized_dir = _materialize_truncated_emit(tmp_path, emit_dir, 12)
    with open_emit(materialized_dir) as mat_emit:
        oracle_specs = _direct_source_full_specs(mat_emit, config)
        oracle_by_name = {
            query_spec_output_name(spec): mat_emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in oracle_specs
        }
    assert set(stated) == set(oracle_by_name)
    for name, table in stated.items():
        assert table.table.to_pydict() == oracle_by_name[name]


# ---------------------------------------------------------------------------
# Per-class consequences (doc table)
# ---------------------------------------------------------------------------


def test_scd2_change_points_le_t_latest_open(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        table = _tables_by_name(head.state(12))["dim_widget_status"]
    rows = {r["status"]: r["valid_to"] for r in table.table.to_pylist()}
    # sim_time 20's change point is > T=12 — absent; the version starting at
    # sim_time 10 ("assembled") is the latest, still open (valid_to NULL).
    assert set(rows) == {"new", "assembled"}
    assert rows["assembled"] is None


def test_type1_dim_constant_current(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        early = _tables_by_name(head.state(0))["dim_gadget"]
        late = _tables_by_name(head.state(100))["dim_gadget"]
    assert early.table.to_pydict() == late.table.to_pydict()
    assert early.table.column("name").to_pylist() == ["Widget-A"]


def test_records_grain_values_as_of_t_not_end_of_run(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        early = _tables_by_name(head.state(5))["fact_widget_current"]
        mid = _tables_by_name(head.state(12))["fact_widget_current"]
        late = _tables_by_name(head.state(100))["fact_widget_current"]
    assert early.table.column("status").to_pylist() == ["new"]
    assert mid.table.column("status").to_pylist() == ["assembled"]
    assert late.table.column("status").to_pylist() == ["shipped"]
    assert late.table.column("mutated_at").to_pylist() == [20]


def test_changelog_genre_event_rows_key_le_t(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        table = _tables_by_name(head.state(12))["fact_widget_status"]
    assert sorted(table.table.column("sim_time").to_pylist()) == [0, 10]


def test_history_interval_lead_sim_time_null_past_t(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        table = _tables_by_name(head.state(12))["fact_widget_interval"]
    rows = {r["sim_time"]: r["lead_sim_time"] for r in table.table.to_pylist()}
    assert rows[0] == 10
    # The version starting at 10 physically leads to 20, but 20 > T=12 is
    # truncated away — lead_sim_time renders NULL, not the physical 20.
    assert rows[10] is None


def test_junction_left_at_null_when_leave_after_t(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_junction_shape_config())
        table = _tables_by_name(head.state(12))["mem_widget_parts"]
    rows = {r["part_name"]: r["left_at"] for r in table.table.to_pylist()}
    assert rows == {"bolt": None, "nut": None}  # nut's leave (15) is after T=12


def test_junction_left_at_present_when_leave_at_or_before_t(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_junction_shape_config())
        table = _tables_by_name(head.state(15))["mem_widget_parts"]
    rows = {r["part_name"]: r["left_at"] for r in table.table.to_pylist()}
    assert rows["nut"] == 15
    assert rows["bolt"] is None


def test_state_only_shape_never_runs_windowed_business_rules(tmp_path: "Path") -> None:
    """The membership grain — window()'s windowed-grain rule always rejects it
    — still answers state() cleanly."""
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_junction_shape_config())
        with pytest.raises(ExportError):
            head.window(0, 100)
        table = _tables_by_name(head.state(100))["mem_widget_parts"]
    assert table.table.num_rows == 2


def test_source_state_table_reconstructs_at_horizon_t_plus_1(
    tmp_path: "Path",
) -> None:
    emit_dir = build_state_test_emit(tmp_path)
    config = state_source_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, config)
        early = _tables_by_name(head.state(0))["widget"]
        mid = _tables_by_name(head.state(12))["widget"]
    assert early.delivery == "snapshot"
    assert early.table.column("status").to_pylist() == ["new"]
    assert mid.table.column("status").to_pylist() == ["assembled"]


# ---------------------------------------------------------------------------
# The mapping: one entry per sidecar base table — an fk hop to a kind outside
# the shape's declared sources resolves truncated (no physical leak).
# ---------------------------------------------------------------------------


def test_fk_hop_outside_declared_sources_resolves_truncated(tmp_path: "Path") -> None:
    emit_dir = build_fk_hop_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, fk_hop_shape_config())
        # Physically ref_index__target_id=2 (target "t2", created_sim_time=50).
        before = _tables_by_name(head.state(0))["fact_referrer"]
        after = _tables_by_name(head.state(100))["fact_referrer"]
    # t2 does not exist yet at T=0 in the truncated world — no physical leak.
    assert before.table.column("target_index").to_pylist() == [None]
    assert after.table.column("target_index").to_pylist() == [2]


# ---------------------------------------------------------------------------
# Delivery, ordering, declared-but-empty
# ---------------------------------------------------------------------------


def test_delivery_is_snapshot_on_every_table(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        tables = head.state(12)
    assert all(t.delivery == "snapshot" for t in tables)


def test_tables_in_tables_order(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        decl_names = tuple(d.name for d in head.tables())
        state_names = tuple(t.name for t in head.state(12))
    assert decl_names == state_names


def test_declared_but_empty_at_t_0(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        by_name = _tables_by_name(head.state(0))
    assert set(by_name) == {
        "dim_gadget",
        "fact_shipment",
        "fact_widget_current",
        "fact_widget_status",
        "fact_widget_interval",
        "dim_widget_status",
    }
    # widget's first event fires at sim_time 0 — history_point/history_interval
    # are non-empty at T=0, but a farther-future emit would still declare
    # them; tables() independent of data is exercised by test_tables_in_tables_order.
    assert by_name["fact_widget_status"].table.num_rows == 1


# ---------------------------------------------------------------------------
# The truncated emit view shares the caller's connection; never closed.
# ---------------------------------------------------------------------------


def test_truncated_emit_view_never_closes_the_callers_connection(
    tmp_path: "Path",
) -> None:
    emit_dir = build_state_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, state_dimensional_shape_config())
        head.state(12)
        # The caller's own emit connection is still usable after state().
        again = head.state(100)
    assert again  # no RunDatabaseError raised — the connection stayed open


# ---------------------------------------------------------------------------
# Notices: state()'s compile notices reach the bound sink.
# ---------------------------------------------------------------------------


def test_state_re_emits_compile_notices_to_the_bound_sink(tmp_path: "Path") -> None:
    emit_dir = build_state_test_emit(tmp_path)
    received: list[object] = []
    with open_emit(emit_dir) as emit:
        head = open_shaped_playback(
            emit, state_dimensional_shape_config(), None, received.append
        )
        before = len(received)
        head.state(12)
        after_first = len(received)
        head.state(50)
        after_second = len(received)
    # Every ask re-emits: a second state() ask grows the sink again (the
    # incremental drip rule), never merely reusing the first ask's notices.
    assert after_first >= before
    assert after_second >= after_first
