"""Tests for the shaped head's `tables` selection: `ShapedPlayback.window()` /
`.state()`'s three gates (`_resolve_selection`), ask order, projection
invariance in both modes, horizon economy, and atomicity
(`playback/shaped.py`).

Gate/order/invariance cases share the sprint's `_shaped_fixtures.py` scaffold
(`build_shaped_test_emit`, `windowable_dimensional_shape_config`,
`source_shape_config`) — no membership-grain table, so both modes window()
cleanly over it. Horizon-economy and atomicity cases need a data-dependent
plan notice or a duplicate key to observe, so they reuse the dimensional
engine's own selection fixture (`exporters.dimensional.test_selection`'s
`_build_selection_emit` / `_selection_config` / `_duplicate_key_config`) for
the dimensional side and `exporters.source.test_where_plan`'s
`_write_sensor_emit` (a `where` value outside its declared enum_domains) for
the source side, rather than duplicating that scaffolding here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink

from exporters.dimensional.test_selection import (
    _build_selection_emit,
    _duplicate_key_config,
    _selection_config,
)
from exporters.source.test_engine import _SENSOR_EVENTS, _SENSOR_STATE_TABLE
from exporters.source.test_where_plan import _write_sensor_emit
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import ExportConfig, SourceConfig
from fabulexa_forge.errors import ExportError
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import ShapedTable, open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

from ._shaped_fixtures import (
    build_shaped_test_emit,
    source_shape_config,
    windowable_dimensional_shape_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.playback.shaped import ShapedPlayback
    from fabulexa_forge.reader.emit import Emit


def _tables_by_name(tables: tuple[ShapedTable, ...]) -> dict[str, ShapedTable]:
    return {t.name: t for t in tables}


def _open_dimensional(emit: "Emit", config: "ExportConfig"):
    return open_shaped_playback(emit, config, None, discard_notice_sink)


def _open_source(emit: "Emit", config: "ExportConfig"):
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    return open_shaped_playback(emit, config, anchor, discard_notice_sink)


def _assert_selected_matches_whole(
    selected: tuple[ShapedTable, ...], whole_by_name: dict[str, ShapedTable]
) -> None:
    """Every selected table's name, delivery, and content equal the same
    table's under the whole-shape (`tables=None`) answer."""
    for table in selected:
        ref = whole_by_name[table.name]
        assert table.name == ref.name
        assert table.delivery == ref.delivery
        assert table.table.equals(ref.table)


_SENSOR_EVENTS_UNIT = _SENSOR_EVENTS.name


def _sensor_config() -> "ExportConfig":
    """`exporters.source.test_engine`'s sensor state unit (a `where` value
    outside its declared enum_domains — one 'discriminator-value-unobserved'
    plan notice per plan build) plus its event log, for the source
    horizon-economy cases."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(tables=(_SENSOR_STATE_TABLE,), events=_SENSOR_EVENTS),
    )


# ---------------------------------------------------------------------------
# Gate 1: a bare str is refused before any name is read.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("open_head", "shape_config", "call"),
    [
        pytest.param(
            _open_dimensional,
            windowable_dimensional_shape_config,
            lambda head: head.window(0, 10, tables="booking"),
            id="window-dimensional",
        ),
        pytest.param(
            _open_dimensional,
            windowable_dimensional_shape_config,
            lambda head: head.state(0, tables="booking"),
            id="state-dimensional",
        ),
        pytest.param(
            _open_source,
            source_shape_config,
            lambda head: head.window(0, 10, tables="booking"),
            id="window-source",
        ),
        pytest.param(
            _open_source,
            source_shape_config,
            lambda head: head.state(0, tables="booking"),
            id="state-source",
        ),
    ],
)
def test_tables_bare_str_raises_playback_error(
    tmp_path: "Path",
    open_head: "Callable[[Emit, ExportConfig], ShapedPlayback]",
    shape_config: "Callable[[], ExportConfig]",
    call: "Callable[[ShapedPlayback], object]",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = open_head(emit, shape_config())
        with pytest.raises(PlaybackError, match="not a str") as exc_info:
            call(head)
    assert "booking" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Gate 2: empty (set semantics — set() and [] both empty).
# ---------------------------------------------------------------------------


def test_window_tables_empty_set_raises_playback_error(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        with pytest.raises(PlaybackError, match="at least one declared table"):
            head.window(0, 10, tables=set())


def test_window_tables_empty_list_raises_playback_error(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        with pytest.raises(PlaybackError, match="at least one declared table"):
            head.window(0, 10, tables=[])


# ---------------------------------------------------------------------------
# Gate 3: unknown names, naming the sorted unknowns and the declared names in
# tables() order; nothing compiles.
# ---------------------------------------------------------------------------


def test_unknown_names_name_sorted_unknowns_and_declared_order(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(emit, source_shape_config(), anchor, sink)
        with pytest.raises(PlaybackError) as exc_info:
            head.window(0, 10, tables={"nope", "also_nope", "gadget"})
    message = str(exc_info.value)
    assert "also_nope, nope" in message
    assert "gadget, shipment, widget, widget_parts, widget_versions" in message
    assert sink.notices == []


# ---------------------------------------------------------------------------
# Gate order: invalid bounds refuse before a bad selection.
# ---------------------------------------------------------------------------


def test_window_invalid_bounds_refuse_before_bad_selection(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        with pytest.raises(PlaybackError, match="invalid window bounds"):
            head.window(-1, 10, tables="nonexistent")


def test_state_invalid_bounds_refuse_before_bad_selection(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        with pytest.raises(PlaybackError, match="invalid state position"):
            head.state(-1, tables="nonexistent")


# ---------------------------------------------------------------------------
# Ask order is irrelevant: answers land in tables() order; a repeated name
# answers once.
# ---------------------------------------------------------------------------


def test_window_answers_in_tables_order_regardless_of_ask_order(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        tables = head.window(0, 12, tables=["shipment", "gadget"])
    assert [t.name for t in tables] == ["gadget", "shipment"]


def test_window_repeated_name_answers_once(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        tables = head.window(0, 12, tables=["gadget", "gadget"])
    assert [t.name for t in tables] == ["gadget"]


# ---------------------------------------------------------------------------
# Projection invariance, dimensional: every singleton and one pair, window()
# and state(), over a shape with a snapshot table (dim_gadget) and a delta
# table (fact_shipment) both carrying data in range.
# ---------------------------------------------------------------------------


def test_projection_invariance_dimensional_window(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        whole = head.window(0, 12)
        whole_by_name = _tables_by_name(whole)
        for name in whole_by_name:
            selected = head.window(0, 12, tables={name})
            assert [t.name for t in selected] == [name]
            _assert_selected_matches_whole(selected, whole_by_name)
        pair = head.window(0, 12, tables={"dim_gadget", "fact_shipment"})
    assert {t.name for t in pair} == {"dim_gadget", "fact_shipment"}
    _assert_selected_matches_whole(pair, whole_by_name)


def test_projection_invariance_dimensional_state(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        whole = head.state(12)
        whole_by_name = _tables_by_name(whole)
        for name in whole_by_name:
            selected = head.state(12, tables={name})
            assert [t.name for t in selected] == [name]
            _assert_selected_matches_whole(selected, whole_by_name)
        pair = head.state(12, tables={"dim_gadget", "fact_shipment"})
    assert {t.name for t in pair} == {"dim_gadget", "fact_shipment"}
    _assert_selected_matches_whole(pair, whole_by_name)


# ---------------------------------------------------------------------------
# Projection invariance, source: state tables, the junction, and the event
# log, window() and state().
# ---------------------------------------------------------------------------


def test_projection_invariance_source_window(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        whole = head.window(0, 12)
        whole_by_name = _tables_by_name(whole)
        for name in whole_by_name:
            selected = head.window(0, 12, tables={name})
            assert [t.name for t in selected] == [name]
            _assert_selected_matches_whole(selected, whole_by_name)
        pair = head.window(0, 12, tables={"gadget", "widget_parts"})
    assert {t.name for t in pair} == {"gadget", "widget_parts"}
    _assert_selected_matches_whole(pair, whole_by_name)


def test_projection_invariance_source_state(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        whole = head.state(12)
        whole_by_name = _tables_by_name(whole)
        for name in whole_by_name:
            selected = head.state(12, tables={name})
            assert [t.name for t in selected] == [name]
            _assert_selected_matches_whole(selected, whole_by_name)
        pair = head.state(12, tables={"gadget", "widget_parts"})
    assert {t.name for t in pair} == {"gadget", "widget_parts"}
    _assert_selected_matches_whole(pair, whole_by_name)


# ---------------------------------------------------------------------------
# Horizon economy: a snapshot-only selection opens one horizon (each notice
# once); a selection that forces the start horizon open (dimensional: an
# 'append' sibling; source: the event log) delivers it twice. `open()` runs
# its own full-config validation against the same sink (§ open_shaped_playback,
# every declared table, unconditional on the ask's selection), so each case
# counts the delta the window() call itself adds, not the sink's raw total.
# ---------------------------------------------------------------------------


def test_horizon_economy_dimensional_snapshot_only_delivers_notice_once(
    tmp_path: "Path",
) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_selection_config())
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        head = open_shaped_playback(emit, config, None, sink)
        before = len(sink.notices)
        head.window(0, 100, tables={"dim_gadget"})
    assert len(sink.notices) - before == 1


def test_horizon_economy_dimensional_append_sibling_delivers_notice_twice(
    tmp_path: "Path",
) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_selection_config())
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        head = open_shaped_playback(emit, config, None, sink)
        before = len(sink.notices)
        head.window(0, 100, tables={"dim_gadget", "fact_append"})
    assert len(sink.notices) - before == 2


def test_horizon_economy_source_snapshot_only_delivers_notice_once(
    tmp_path: "Path",
) -> None:
    emit_dir = _write_sensor_emit(tmp_path)
    config = _sensor_config()
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(emit, config, anchor, sink)
        before = len(sink.notices)
        head.window(0, 100, tables={"sensor_state"})
    assert len(sink.notices) - before == 1


def test_horizon_economy_source_event_log_included_delivers_notice_twice(
    tmp_path: "Path",
) -> None:
    emit_dir = _write_sensor_emit(tmp_path)
    config = _sensor_config()
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(emit, config, anchor, sink)
        before = len(sink.notices)
        head.window(0, 100, tables={"sensor_state", _SENSOR_EVENTS_UNIT})
    assert len(sink.notices) - before == 2


# ---------------------------------------------------------------------------
# Atomicity: a duplicate-key 'upsert' table refuses whether alone or
# selected alongside a clean sibling; the clean sibling alone succeeds.
# ---------------------------------------------------------------------------


def test_atomicity_selecting_dup_with_clean_refuses_naming_dup(
    tmp_path: "Path",
) -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_duplicate_key_config())
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, config)
        with pytest.raises(ExportError, match="fact_dupkey"):
            head.window(0, 100, tables={"dim_gadget", "fact_dupkey"})


def test_atomicity_clean_selection_alone_succeeds(tmp_path: "Path") -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_duplicate_key_config())
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, config)
        tables = head.window(0, 100, tables={"dim_gadget"})
    assert [t.name for t in tables] == ["dim_gadget"]


def test_atomicity_dup_selection_alone_refuses(tmp_path: "Path") -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_duplicate_key_config())
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, config)
        with pytest.raises(ExportError, match="fact_dupkey"):
            head.window(0, 100, tables={"fact_dupkey"})


# ---------------------------------------------------------------------------
# state() delivery is 'snapshot' on every selected table, either mode.
# ---------------------------------------------------------------------------


def test_state_delivery_is_snapshot_on_every_selected_table_dimensional(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_dimensional(emit, windowable_dimensional_shape_config())
        tables = head.state(12, tables={"dim_gadget", "fact_shipment"})
    assert {t.name for t in tables} == {"dim_gadget", "fact_shipment"}
    assert all(t.delivery == "snapshot" for t in tables)


def test_state_delivery_is_snapshot_on_every_selected_table_source(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_source(emit, source_shape_config())
        tables = head.state(12, tables={"widget_parts", "widget_versions"})
    assert {t.name for t in tables} == {"widget_parts", "widget_versions"}
    assert all(t.delivery == "snapshot" for t in tables)
