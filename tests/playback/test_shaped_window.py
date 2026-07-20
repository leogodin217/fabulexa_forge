"""Tests for tier-2 shaped playback window: ShapedPlayback.window()."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.query_spec import query_spec_output_name
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import ShapedTable, open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

from ._shaped_fixtures import (
    build_shaped_test_emit,
    dimensional_shape_config,
    source_shape_config,
    source_snapshot_delivery_shape_config,
    windowable_dimensional_shape_config,
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


def _direct_dimensional_specs(
    emit: "Emit", config: "ExportConfig", start_ns: int, end_ns: int
):
    """Compile the same window directly through the engine (the reference)."""
    assert config.dimensional is not None
    window = Window(index=None, start_ns=start_ns, end_ns=end_ns, label="")
    return build_query_specs(
        emit, config.dimensional, None, window, discard_notice_sink, base_relations=None
    )


def _direct_source_specs(
    emit: "Emit", config: "ExportConfig", start_ns: int, end_ns: int
):
    """Compile the same window directly through the engine (the reference)."""
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    window = Window(index=None, start_ns=start_ns, end_ns=end_ns, label="")
    return build_source_query_specs(
        emit, config, anchor, window, discard_notice_sink, base_relations=None
    )


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_negative_start_raises_playback_error(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        with pytest.raises(PlaybackError):
            head.window(-1, 10)


def test_start_greater_than_end_raises_playback_error(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        with pytest.raises(PlaybackError):
            head.window(10, 5)


def test_empty_window_start_equals_end_is_legal(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        tables = head.window(5, 5)
        by_name = _tables_by_name(tables)
        assert by_name["fact_shipment"].table.num_rows == 0


# ---------------------------------------------------------------------------
# Windowed business rule: whole-shape rejection naming the offending table
# ---------------------------------------------------------------------------


def test_membership_grain_shape_rejects_naming_the_table(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, dimensional_shape_config())
        with pytest.raises(ExportError, match="mem_widget_parts"):
            head.window(0, 100)


# ---------------------------------------------------------------------------
# Promotion equality: window() content equals the incremental driver's own
# windowed compile for the same window (dimensional and source).
# ---------------------------------------------------------------------------


def test_window_promotes_dimensional_engine_compile_verbatim(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    config = windowable_dimensional_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, config)
        tables = head.window(0, 12)
        expected_specs = _direct_dimensional_specs(emit, config, 0, 12)
        expected_by_name = {
            query_spec_output_name(spec): emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in expected_specs
        }
        for table in tables:
            assert table.table.to_pydict() == expected_by_name[table.name]


def test_window_promotes_source_engine_compile_verbatim(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    config = source_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, config)
        tables = head.window(0, 12)
        expected_specs = _direct_source_specs(emit, config, 0, 12)
        expected_by_name = {
            query_spec_output_name(spec): emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in expected_specs
        }
        for table in tables:
            assert table.table.to_pydict() == expected_by_name[table.name]


# ---------------------------------------------------------------------------
# Per-class / per-genre window membership
# ---------------------------------------------------------------------------


def test_records_fact_windows_on_last_mutation_sim_time(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        # fact_shipment: last_mutation_sim_time = 1
        included = _tables_by_name(head.window(0, 2))["fact_shipment"]
        excluded = _tables_by_name(head.window(2, 4))["fact_shipment"]
    assert included.table.column("id").to_pylist() == ["s1"]
    assert excluded.table.num_rows == 0


def test_history_point_fact_windows_on_sim_time(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        # history rows at sim_time 0 and 10
        first = _tables_by_name(head.window(0, 10))["fact_widget_status"]
        second = _tables_by_name(head.window(10, 20))["fact_widget_status"]
    assert first.table.column("sim_time").to_pylist() == [0]
    assert second.table.column("sim_time").to_pylist() == [10]


def test_scd2_dim_physical_projection_no_valid_to(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        table = _tables_by_name(head.window(0, 12))["dim_widget_status"]
    assert table.delivery == "append"
    col_names = table.table.schema.names
    assert "__valid_from_ns" in col_names
    assert "valid_to" not in col_names
    assert sorted(table.table.column("__valid_from_ns").to_pylist()) == [0, 10]


def test_type1_dim_full_every_window(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        first = _tables_by_name(head.window(0, 2))["dim_gadget"]
        second = _tables_by_name(head.window(50, 60))["dim_gadget"]
    assert first.delivery == "snapshot"
    assert first.table.to_pydict() == second.table.to_pydict()
    assert first.table.column("id").to_pylist() == ["g1"]


def test_source_transaction_windows_on_last_mutation_sim_time(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        # shipment: last_mutation_sim_time = 1
        included = _tables_by_name(head.window(0, 2))["shipment"]
        excluded = _tables_by_name(head.window(2, 4))["shipment"]
    assert included.table.column("id").to_pylist() == ["s1"]
    assert excluded.table.num_rows == 0


def test_source_changelog_windows_on_event_sim_time(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        first = _tables_by_name(head.window(0, 10))["widget"]
        second = _tables_by_name(head.window(10, 20))["widget"]
    assert first.delivery == "append"
    assert first.table.num_rows == 1
    assert second.table.num_rows == 1


def test_source_reference_full_every_window(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        first = _tables_by_name(head.window(0, 2))["gadget"]
        second = _tables_by_name(head.window(50, 60))["gadget"]
    assert first.delivery == "snapshot"
    assert first.table.to_pydict() == second.table.to_pydict()
    assert first.table.column("id").to_pylist() == ["g1"]


def test_source_junction_extract_on_change_left_at_masked(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        # window [0, 6): contains bolt's join (5) and nut's join (2); nut's
        # leave (8) is not < 6, so nut's left_at is masked NULL.
        window_a = _tables_by_name(head.window(0, 6))["widget_parts"]
        # window [6, 10): contains nut's leave (8), not bolt's join (5) —
        # bolt is absent, nut's left_at renders 8 (8 < 10).
        window_b = _tables_by_name(head.window(6, 10))["widget_parts"]

    rows_a = {row["name"]: row["left_at"] for row in window_a.table.to_pylist()}
    assert rows_a == {"bolt": None, "nut": None}

    rows_b = {row["name"]: row["left_at"] for row in window_b.table.to_pylist()}
    assert "bolt" not in rows_b
    assert rows_b["nut"] is not None


def test_source_snapshot_delivery_reconstructs_at_horizon_end(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    config = source_snapshot_delivery_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, config)
        early = _tables_by_name(head.window(0, 5))["widget"]
        late = _tables_by_name(head.window(0, 15))["widget"]
    assert early.delivery == "snapshot"
    assert late.delivery == "snapshot"
    early_status = early.table.column("status").to_pylist()
    late_status = late.table.column("status").to_pylist()
    # Different reconstruction horizons yield different reconstructed content.
    assert early_status != late_status


# ---------------------------------------------------------------------------
# Select-not-recompute: window rows equal the full export's rows.
# ---------------------------------------------------------------------------


def test_window_values_equal_full_export_values(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    config = windowable_dimensional_shape_config()
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, config)
        windowed = _tables_by_name(head.window(0, 2))["fact_shipment"]
        assert config.dimensional is not None
        full_specs = build_query_specs(
            emit,
            config.dimensional,
            None,
            None,
            discard_notice_sink,
            base_relations=None,
        )
    full_spec = next(s for s in full_specs if s.table_name == "fact_shipment")
    with open_emit(emit_dir) as emit:
        full_rows = emit.query_arrow(full_spec.sql, ()).to_pydict()
    windowed_rows = windowed.table.to_pydict()
    full_by_id = dict(zip(full_rows["id"], full_rows["amount"]))
    for i, rid in enumerate(windowed_rows["id"]):
        assert windowed_rows["amount"][i] == full_by_id[rid]


# ---------------------------------------------------------------------------
# Declared-but-empty and adjacent-window union
# ---------------------------------------------------------------------------


def test_empty_window_returns_every_declared_table_zero_row_typed(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        by_name = _tables_by_name(head.window(1000, 1001))
    assert set(by_name) == {
        "dim_gadget",
        "fact_shipment",
        "fact_widget_status",
        "dim_widget_status",
    }
    # Append classes: no rows fall in the far-future window.
    assert by_name["fact_shipment"].table.num_rows == 0
    assert by_name["fact_widget_status"].table.num_rows == 0
    assert by_name["dim_widget_status"].table.num_rows == 0
    # Type-1 dim: still a full snapshot regardless of the window.
    assert by_name["dim_gadget"].table.num_rows == 1


def test_adjacent_windows_union_has_no_duplicates_or_gaps(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        window_a = _tables_by_name(head.window(0, 5))["fact_widget_status"]
        window_b = _tables_by_name(head.window(5, 15))["fact_widget_status"]
        wide = _tables_by_name(head.window(0, 15))["fact_widget_status"]

    union_sim_times = sorted(
        window_a.table.column("sim_time").to_pylist()
        + window_b.table.column("sim_time").to_pylist()
    )
    assert union_sim_times == sorted(wide.table.column("sim_time").to_pylist())
    assert len(union_sim_times) == len(set(union_sim_times))
