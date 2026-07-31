"""Tests for tier-2 shaped playback open: ShapedTableDecl, open_shaped_playback,
ShapedPlayback.tables().
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    IncrementalConfig,
    RebaseConfig,
    SourceDecl,
    TableDecl,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.source.events import SourceEventLogPlan
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourceStateTablePlan,
)
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import (
    ShapedTableDecl,
    _dimensional_window_delivery,
    _source_window_delivery,
    open_shaped_playback,
)
from fabulexa_forge.reader.emit import open_emit

from ._shaped_fixtures import (
    build_shaped_test_emit,
    dimensional_shape_config,
    source_last_mutation_named_shape_config,
    source_shape_config,
)

if TYPE_CHECKING:
    from pathlib import Path


def _from_col(name: str, src: str) -> ColumnDecl:
    return ColumnDecl(name=name, **{"from": src})


def _make_table_decl(
    *,
    role: Literal["dim", "fact"],
    grain: Literal["records", "history_point", "history_interval", "membership"],
    scd: Literal["type1", "type2"] | None = None,
    kind: str = "widget",
    property_name: str | None = None,
) -> TableDecl:
    """Build a standalone TableDecl for the pure classification helper —
    never opened against an emit; only the class-shaping fields matter."""
    return TableDecl(
        name="t",
        role=role,
        scd=scd,
        source=SourceDecl(grain=grain, kind=kind, property=property_name),
        key=["id"],
        columns=[_from_col("id", "record_id")],
    )


def _make_state_unit() -> SourceStateTablePlan:
    """Build a standalone SourceStateTablePlan for the pure classification
    helper — never compiled; only the type discriminates."""
    return SourceStateTablePlan(
        name="t",
        kind="k",
        populations=(Population(kind="k", sub_type=None),),
        columns=(),
        identity_surface="record_id",
        edge_surfaces=(),
        keys=None,
    )


def _make_junction_unit() -> SourceJunctionTablePlan:
    """Build a standalone SourceJunctionTablePlan for the pure classification
    helper — never compiled; only the type discriminates."""
    return SourceJunctionTablePlan(
        name="t",
        owner_kind="k",
        property="p",
        source_table="membership__k__p",
        columns=(),
        edge_surfaces=(),
    )


def _make_event_log_unit() -> SourceEventLogPlan:
    """Build a standalone SourceEventLogPlan for the pure classification
    helper — never compiled; only the type discriminates."""
    return SourceEventLogPlan(name="t", sources=(), item_id_type="VARCHAR")


# ---------------------------------------------------------------------------
# Static per-class / per-unit classification (pure functions, no emit)
# ---------------------------------------------------------------------------


def test_dimensional_records_fact_appends() -> None:
    decl = _make_table_decl(role="fact", grain="records")
    assert _dimensional_window_delivery(decl) == "append"


def test_dimensional_history_point_fact_appends() -> None:
    decl = _make_table_decl(role="fact", grain="history_point", property_name="state")
    assert _dimensional_window_delivery(decl) == "append"


def test_dimensional_scd2_dim_appends() -> None:
    decl = _make_table_decl(role="dim", grain="records", scd="type2")
    assert _dimensional_window_delivery(decl) == "append"


def test_dimensional_type1_dim_snapshots() -> None:
    decl = _make_table_decl(role="dim", grain="records", scd="type1")
    assert _dimensional_window_delivery(decl) == "snapshot"


def test_dimensional_history_interval_grain_is_none() -> None:
    decl = _make_table_decl(role="fact", grain="history_interval", property_name="s")
    assert _dimensional_window_delivery(decl) is None


def test_dimensional_membership_grain_is_none() -> None:
    decl = _make_table_decl(role="fact", grain="membership", property_name="p")
    assert _dimensional_window_delivery(decl) is None


def test_source_state_table_snapshots() -> None:
    assert _source_window_delivery(_make_state_unit()) == "snapshot"


def test_source_junction_table_appends() -> None:
    assert _source_window_delivery(_make_junction_unit()) == "append"


def test_source_event_log_appends() -> None:
    assert _source_window_delivery(_make_event_log_unit()) == "append"


# ---------------------------------------------------------------------------
# open_shaped_playback: validation, the anchor gate, tables()
# ---------------------------------------------------------------------------


def test_dimensional_opens_with_anchor_none(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = open_shaped_playback(
            emit, dimensional_shape_config(), None, discard_notice_sink
        )
        assert head.tables() == (
            ShapedTableDecl(name="dim_gadget", window_delivery="snapshot"),
            ShapedTableDecl(name="fact_shipment", window_delivery="append"),
            ShapedTableDecl(name="mem_widget_parts", window_delivery=None),
        )


def test_dimensional_tables_in_config_declaration_order(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = open_shaped_playback(
            emit, dimensional_shape_config(), None, discard_notice_sink
        )
        names = tuple(decl.name for decl in head.tables())
        assert names == ("dim_gadget", "fact_shipment", "mem_widget_parts")


def test_source_shape_requires_anchor(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        with pytest.raises(PlaybackError):
            open_shaped_playback(emit, source_shape_config(), None, discard_notice_sink)


def test_source_shape_opens_with_resolved_anchor_and_enumerates_tables_then_log(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(
            emit, source_shape_config(), anchor, discard_notice_sink
        )
        assert head.tables() == (
            ShapedTableDecl(name="gadget", window_delivery="snapshot"),
            ShapedTableDecl(name="shipment", window_delivery="snapshot"),
            ShapedTableDecl(name="widget", window_delivery="snapshot"),
            ShapedTableDecl(name="widget_parts", window_delivery="append"),
            ShapedTableDecl(name="widget_versions", window_delivery="append"),
        )


def test_source_shape_last_mutation_sim_time_opens_but_window_refuses(
    tmp_path: "Path",
) -> None:
    """A `columns` entry naming `last_mutation_sim_time` validates against
    the full-export shape at open — `updated_at` is reconstructible for a
    full export — but the first `window()` ask rebuilds the plan against
    the windowed shape and refuses."""
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(
            emit, source_last_mutation_named_shape_config(), anchor, discard_notice_sink
        )
        assert head.tables() == (
            ShapedTableDecl(name="widget", window_delivery="snapshot"),
        )
        with pytest.raises(ExportError):
            head.window(0, 100)


def test_reserved_presentation_name_refused_at_open(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    table_decl = TableDecl(
        name="dim_gadget",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="gadget"),
        key=["id"],
        columns=[
            _from_col("id", "record_id"),
            ColumnDecl(name="last_mutation_sim_time", **{"from": "record_id"}),
        ],
    )
    config = ExportConfig(
        mode="dimensional", dimensional=DimensionalConfig(tables=[table_decl])
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="last_mutation_sim_time"):
            open_shaped_playback(emit, config, None, discard_notice_sink)


def test_invalid_dimensional_config_export_error_passes_through(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    table_decl = TableDecl(
        name="dim_unknown",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="does_not_exist"),
        key=["id"],
        columns=[_from_col("id", "record_id")],
    )
    config = ExportConfig(
        mode="dimensional", dimensional=DimensionalConfig(tables=[table_decl])
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError):
            open_shaped_playback(emit, config, None, discard_notice_sink)


def test_rebase_and_incremental_blocks_not_read(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    plain = dimensional_shape_config()
    with_extras = ExportConfig(
        mode="dimensional",
        dimensional=plain.dimensional,
        rebase=RebaseConfig(timezone="UTC"),
        incremental=IncrementalConfig(sim_period_ns=10),
    )
    with open_emit(emit_dir) as emit:
        head_plain = open_shaped_playback(emit, plain, None, discard_notice_sink)
        head_extras = open_shaped_playback(emit, with_extras, None, discard_notice_sink)
        assert head_plain.tables() == head_extras.tables()
