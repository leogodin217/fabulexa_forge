"""Tests for the generated calendar on a shaped-playback head (Phase 5 of the
date-dimension sprint): `dim_date`'s position in `tables()`, its snapshot
identity across window bounds and state instants, a `date_ref` column's
values against the direct engine compile (the incremental driver's own
compile surface) and against a full export at the tape's own end, the
per-ask range guard (`window()` / `state()` both, atomically), the unknown-
table gate when no `date_dimension` block is declared, and a source shape's
unchanged `tables()`.

Reuses the `date-dimension` recipe's own config and emit
(`recipes._recipe_fixture.build_recipe_emit`, `_support.slice_emit.slice_emit`
— the same fixture `tests/incremental/test_date_dimension.py` sliced at
`_TAPE_END`) for every date_ref-bearing case, and
`tests/playback/_shaped_fixtures.py`'s plain dimensional/source shapes (no
`date_dimension` declared) for the absent-block cases.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.slice_emit import slice_emit
from recipes._recipe_fixture import DAY, build_recipe_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import DateDimensionConfig
from fabulexa_forge.errors import DateRefOutOfRange
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import ShapedTable, open_shaped_playback
from fabulexa_forge.reader.emit import open_emit

from ._shaped_fixtures import (
    build_shaped_test_emit,
    dimensional_shape_config,
    source_shape_config,
)

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.playback.shaped import ShapedPlayback
    from fabulexa_forge.reader.emit import Emit

_RECIPE_CONFIG_PATH = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "recipes"
    / "date-dimension"
    / "config.yaml"
)

#: The recipe emit's data spans days 1..3 (1*DAY..3*DAY); a slice at the end
#: of day 4 bounds every instant (§ tests/incremental/test_date_dimension.py).
_TAPE_END = 4 * DAY - 1


def _sliced_recipe_emit(tmp_path: Path) -> Path:
    """The date-dimension recipe's own emit, sliced at `_TAPE_END`."""
    raw = tmp_path / "raw"
    build_recipe_emit(raw)
    return slice_emit(raw, tmp_path / "emit", _TAPE_END)


def _recipe_config(date_dimension: DateDimensionConfig | None = None) -> "ExportConfig":
    """The recipe's own config, optionally with a narrowed range."""
    config = load_export_config(_RECIPE_CONFIG_PATH)
    if date_dimension is None:
        return config
    return config.model_copy(update={"date_dimension": date_dimension})


def _out_of_range_date_dimension() -> DateDimensionConfig:
    """A range excluding the third day's event date (2024-01-04)."""
    return DateDimensionConfig.model_validate(
        {"from": "1985-01-01", "to": "2024-01-03"}
    )


def _anchor_for(emit: "Emit") -> "EffectiveAnchor":
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    assert anchor is not None
    return anchor


def _open_head(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None" = None,
    sink: "NoticeSink" = discard_notice_sink,
) -> "ShapedPlayback":
    return open_shaped_playback(emit, config, anchor, sink, supplements=())


def _tables_by_name(tables: tuple[ShapedTable, ...]) -> dict[str, ShapedTable]:
    return {t.name: t for t in tables}


def _direct_window_specs(
    emit: "Emit", config: "ExportConfig", start_ns: int, end_ns: int
) -> list:
    """Compile the same window directly through the engine — the same
    compile the incremental driver's own windowed export runs."""
    assert config.dimensional is not None
    window = Window(index=None, start_ns=start_ns, end_ns=end_ns, label="")
    return build_query_specs(
        emit,
        config.dimensional,
        _anchor_for(emit),
        window,
        discard_notice_sink,
        base_relations=None,
        tables=None,
    )


# ---------------------------------------------------------------------------
# tables(): position, presence, and absence
# ---------------------------------------------------------------------------


def test_dim_date_sits_after_declared_tables(tmp_path: Path) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit))
    names = [decl.name for decl in head.tables()]
    assert names == [
        "fact_status_event",
        "dim_patient",
        "dim_patient_status",
        "dim_date",
    ]
    dim_date_decl = next(d for d in head.tables() if d.name == "dim_date")
    assert dim_date_decl.window_delivery == "snapshot"


def test_without_date_dimension_block_dim_date_is_absent(tmp_path: Path) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config())
    names = [decl.name for decl in head.tables()]
    assert "dim_date" not in names
    assert names == ["dim_gadget", "fact_shipment", "mem_widget_parts"]


def test_source_shape_tables_is_unchanged(tmp_path: Path) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, source_shape_config(), _anchor_for(emit))
    names = [decl.name for decl in head.tables()]
    assert "dim_date" not in names
    assert names == ["gadget", "shipment", "widget", "widget_parts", "widget_versions"]


# ---------------------------------------------------------------------------
# Horizon economy: a dim_date-only ask opens no horizon and emits no notice
# ---------------------------------------------------------------------------


def test_window_dim_date_only_opens_no_horizon_and_emits_no_notice(
    tmp_path: Path,
) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config()
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit), sink)
        before = len(sink.notices)
        tables = head.window(0, _TAPE_END, tables={"dim_date"})
    assert [t.name for t in tables] == ["dim_date"]
    assert len(sink.notices) - before == 0


def test_state_dim_date_only_opens_no_truncated_tape_and_emits_no_notice(
    tmp_path: Path,
) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config()
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit), sink)
        before = len(sink.notices)
        tables = head.state(_TAPE_END, tables={"dim_date"})
    assert [t.name for t in tables] == ["dim_date"]
    assert len(sink.notices) - before == 0


# ---------------------------------------------------------------------------
# dim_date: identical rows across bounds / instants
# ---------------------------------------------------------------------------


def test_dim_date_identical_across_two_windows(tmp_path: Path) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit))
        first = head.window(0, 1 * DAY, tables={"dim_date"})[0]
        second = head.window(0, _TAPE_END, tables={"dim_date"})[0]
    assert first.table.equals(second.table)


def test_dim_date_identical_across_two_states(tmp_path: Path) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit))
        early = head.state(1 * DAY, tables={"dim_date"})[0]
        late = head.state(_TAPE_END, tables={"dim_date"})[0]
    assert early.table.equals(late.table)


# ---------------------------------------------------------------------------
# date_ref values: window() matches the direct engine compile, state()
# matches the full export at the tape's own end
# ---------------------------------------------------------------------------


def test_window_date_ref_matches_direct_engine_compile(tmp_path: Path) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit))
        stated = _tables_by_name(head.window(0, _TAPE_END))
        direct = {
            spec.table_name: emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in _direct_window_specs(emit, config, 0, _TAPE_END)
        }
    assert (
        stated["fact_status_event"].table.column("event_date_key").to_pylist()
        == direct["fact_status_event"]["event_date_key"]
    )


def test_state_date_ref_matches_full_export_at_tape_end(tmp_path: Path) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config()
    with open_emit(emit_dir) as emit:
        anchor = _anchor_for(emit)
        head = _open_head(emit, config, anchor)
        stated = _tables_by_name(head.state(_TAPE_END))
        assert config.dimensional is not None
        full_specs = build_query_specs(
            emit,
            config.dimensional,
            anchor,
            None,
            discard_notice_sink,
            base_relations=None,
            tables=None,
        )
        full_by_name = {
            spec.table_name: emit.query_arrow(spec.sql, ()).to_pydict()
            for spec in full_specs
        }
    assert (
        stated["dim_patient"].table.column("birth_date_key").to_pylist()
        == full_by_name["dim_patient"]["birth_date_key"]
    )


# ---------------------------------------------------------------------------
# The range guard: window() and state() both raise, atomically
# ---------------------------------------------------------------------------


def test_window_out_of_range_key_raises_date_ref_out_of_range(tmp_path: Path) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config(_out_of_range_date_dimension())
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit))
        with pytest.raises(DateRefOutOfRange):
            head.window(0, _TAPE_END)


def test_state_out_of_range_key_raises_date_ref_out_of_range(tmp_path: Path) -> None:
    emit_dir = _sliced_recipe_emit(tmp_path)
    config = _recipe_config(_out_of_range_date_dimension())
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _anchor_for(emit))
        with pytest.raises(DateRefOutOfRange):
            head.state(_TAPE_END)


# ---------------------------------------------------------------------------
# The unknown-table gate still refuses "dim_date" when no block is declared
# ---------------------------------------------------------------------------


def test_dim_date_selection_without_block_is_unknown_table(tmp_path: Path) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config())
        with pytest.raises(PlaybackError):
            head.window(0, 100, tables={"dim_date"})
