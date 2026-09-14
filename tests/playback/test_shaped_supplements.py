"""Tests for supplements bound on a shaped-playback head: `tables()`'s union
tail, `state()` / `window()` snapshot identity across T, the per-ask
selection over the union, horizon economy, and the open-time gates
(`playback/shaped.py`, `exporters/supplements.py`).

Supplements are bound directly as `ResolvedSupplement` tuples (the same shape
`load_supplements` produces) — no config loader or filesystem involved, since
`open_shaped_playback` takes `supplements` as its own parameter. `dim_gadget` /
`fact_shipment` / `mem_widget_parts` (`_shaped_fixtures.dimensional_shape_config`)
drive the `tables()` and `state()` cases; the membership-grain `mem_widget_parts`
table is windowed-grain-rejected under `window()` (§ `_shaped_fixtures`), so the
`window()` cases use `windowable_dimensional_shape_config` instead (no
membership table, every declared class windowable). Horizon-economy cases
reuse the dimensional engine's own unobserved-value fixture
(`exporters.dimensional.test_selection`'s `_build_selection_emit` /
`_selection_config`) rather than duplicating that scaffolding here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from pydantic import ValidationError

from exporters.dimensional.test_selection import (
    _build_selection_emit,
    _selection_config,
)
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import ExportConfig, SupplementDecl
from fabulexa_forge.errors import (
    ExportError,
    SupplementValueInvalid,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.supplements import ResolvedSupplement
from fabulexa_forge.playback import shaped as shaped_module
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.shaped import ShapedTableDecl, open_shaped_playback
from fabulexa_forge.reader.emit import Emit, open_emit

from ._shaped_fixtures import (
    build_shaped_test_emit,
    dimensional_shape_config,
    source_shape_config,
    windowable_dimensional_shape_config,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pyarrow as pa

    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.playback.shaped import ShapedPlayback


# ---------------------------------------------------------------------------
# Supplement fixtures — bound directly, bypassing the config loader.
# ---------------------------------------------------------------------------


def _region_code_decl() -> SupplementDecl:
    """A file-shaped supplement declaration, resolved inline for the test."""
    return SupplementDecl(name="region_code", columns={"code": "VARCHAR"}, rows=[])


def _rate_tier_decl() -> SupplementDecl:
    """An inline-shaped supplement declaration."""
    return SupplementDecl(name="rate_tier", columns={"tier": "VARCHAR"}, rows=[])


def _two_supplements() -> tuple[ResolvedSupplement, ...]:
    """`region_code` (2 rows) then `rate_tier` (2 rows), declaration order."""
    return (
        ResolvedSupplement(
            decl=_region_code_decl(), path=None, sha256=None, rows=(("US",), ("CA",))
        ),
        ResolvedSupplement(
            decl=_rate_tier_decl(),
            path=None,
            sha256=None,
            rows=(("standard",), ("premium",)),
        ),
    )


def _open_head(
    emit: "Emit",
    config: "ExportConfig",
    supplements: "Sequence[ResolvedSupplement]",
    sink: "NoticeSink" = discard_notice_sink,
) -> "ShapedPlayback":
    """Open a shaped head with an anchor of None, threading `supplements`."""
    return open_shaped_playback(emit, config, None, sink, supplements)


# ---------------------------------------------------------------------------
# tables(): declared tables then supplements, declaration order.
# ---------------------------------------------------------------------------


def test_tables_lists_declared_then_supplements_in_declaration_order(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config(), _two_supplements())
    assert head.tables() == (
        ShapedTableDecl(name="dim_gadget", window_delivery="snapshot"),
        ShapedTableDecl(name="fact_shipment", window_delivery="upsert"),
        ShapedTableDecl(name="mem_widget_parts", window_delivery="append"),
        ShapedTableDecl(name="region_code", window_delivery="snapshot"),
        ShapedTableDecl(name="rate_tier", window_delivery="snapshot"),
    )


def test_source_shape_with_no_supplements_is_unchanged(tmp_path: "Path") -> None:
    """A source shape can declare no supplements — supplements=() leaves
    tables() exactly as before Phase 4."""
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        head = open_shaped_playback(
            emit, source_shape_config(), anchor, discard_notice_sink, supplements=()
        )
    assert head.tables() == (
        ShapedTableDecl(name="gadget", window_delivery="snapshot"),
        ShapedTableDecl(name="shipment", window_delivery="snapshot"),
        ShapedTableDecl(name="widget", window_delivery="snapshot"),
        ShapedTableDecl(name="widget_parts", window_delivery="snapshot"),
        ShapedTableDecl(name="widget_versions", window_delivery="append"),
    )


# ---------------------------------------------------------------------------
# state(T): every supplement 'snapshot', identical relation at every T.
# ---------------------------------------------------------------------------


def test_state_supplements_are_snapshot_and_identical_across_instants(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config(), _two_supplements())
        state_zero = {t.name: t for t in head.state(0)}
        state_mid = {t.name: t for t in head.state(50)}
        state_late = {t.name: t for t in head.state(100)}

    for name in ("region_code", "rate_tier"):
        assert state_zero[name].delivery == "snapshot"
        assert state_zero[name].table.equals(state_mid[name].table)
        assert state_mid[name].table.equals(state_late[name].table)
    assert state_zero["region_code"].table.column("code").to_pylist() == ["US", "CA"]
    assert state_zero["rate_tier"].table.column("tier").to_pylist() == [
        "standard",
        "premium",
    ]


# ---------------------------------------------------------------------------
# window(T1, T2): every supplement delivered whole; an empty window too.
# ---------------------------------------------------------------------------


def test_window_includes_supplements_whole_even_for_an_empty_window(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(
            emit, windowable_dimensional_shape_config(), _two_supplements()
        )
        tables = {t.name: t for t in head.window(50, 50)}

    assert tables["region_code"].delivery == "snapshot"
    assert tables["region_code"].table.column("code").to_pylist() == ["US", "CA"]
    assert tables["rate_tier"].table.column("tier").to_pylist() == [
        "standard",
        "premium",
    ]


# ---------------------------------------------------------------------------
# Selection over the union; projection invariance.
# ---------------------------------------------------------------------------


def test_window_selection_supplement_only_returns_just_the_supplement(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(
            emit, windowable_dimensional_shape_config(), _two_supplements()
        )
        tables = head.window(0, 100, tables={"region_code"})
    assert [t.name for t in tables] == ["region_code"]


def test_state_selection_dim_and_supplement_return_in_shape_order(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config(), _two_supplements())
        tables = head.state(50, tables={"rate_tier", "dim_gadget"})
    assert [t.name for t in tables] == ["dim_gadget", "rate_tier"]


def test_projection_invariance_window_supplement_singleton_equals_whole(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(
            emit, windowable_dimensional_shape_config(), _two_supplements()
        )
        whole = {t.name: t for t in head.window(0, 100)}
        for name in ("region_code", "rate_tier"):
            (selected,) = head.window(0, 100, tables={name})
            assert selected.delivery == whole[name].delivery
            assert selected.table.equals(whole[name].table)


def test_projection_invariance_state_supplement_singleton_equals_whole(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config(), _two_supplements())
        whole = {t.name: t for t in head.state(50)}
        for name in ("region_code", "rate_tier"):
            (selected,) = head.state(50, tables={name})
            assert selected.delivery == whole[name].delivery
            assert selected.table.equals(whole[name].table)


# ---------------------------------------------------------------------------
# Horizon economy: a supplement-only ask opens no horizon and emits no
# notice; a mixed ask emits exactly the dimensional part's notices.
# ---------------------------------------------------------------------------


def _one_supplement() -> tuple[ResolvedSupplement, ...]:
    return (
        ResolvedSupplement(
            decl=_region_code_decl(), path=None, sha256=None, rows=(("US",),)
        ),
    )


def test_supplement_only_window_emits_no_notices(tmp_path: "Path") -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_selection_config())
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _one_supplement(), sink)
        before = len(sink.notices)
        head.window(0, 100, tables={"region_code"})
    assert len(sink.notices) - before == 0


def test_supplement_only_state_emits_no_notices(tmp_path: "Path") -> None:
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_selection_config())
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _one_supplement(), sink)
        before = len(sink.notices)
        head.state(50, tables={"region_code"})
    assert len(sink.notices) - before == 0


def test_mixed_selection_emits_dimensional_part_notices_only(tmp_path: "Path") -> None:
    """`dim_gadget` filters on an unobserved value (one notice per compile,
    § test_selection); mixing it with a supplement still delivers exactly
    that one notice, not two and not zero."""
    emit_dir = _build_selection_emit(tmp_path)
    config = ExportConfig(mode="dimensional", dimensional=_selection_config())
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, config, _one_supplement(), sink)
        before = len(sink.notices)
        head.window(0, 100, tables={"region_code", "dim_gadget"})
    assert len(sink.notices) - before == 1


# ---------------------------------------------------------------------------
# Selection gates: unknown name, bare str, empty — declared-names list
# includes the supplements.
# ---------------------------------------------------------------------------


def test_unknown_name_selection_lists_declared_names_including_supplements(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config(), _two_supplements())
        with pytest.raises(PlaybackError) as exc_info:
            head.window(0, 100, tables={"nope", "region_code"})
    message = str(exc_info.value)
    assert "nope" in message
    assert (
        "dim_gadget, fact_shipment, mem_widget_parts, region_code, rate_tier" in message
    )


def test_bare_str_selection_refused(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config(), _two_supplements())
        with pytest.raises(PlaybackError, match="not a str"):
            head.window(0, 100, tables="region_code")


def test_empty_selection_refused(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        head = _open_head(emit, dimensional_shape_config(), _two_supplements())
        with pytest.raises(PlaybackError, match="at least one declared table"):
            head.state(0, tables=set())


# ---------------------------------------------------------------------------
# Open refusals: a bad cell, a reserved name, a TIMESTAMPTZ column with no
# anchor, and a name colliding with a declared table (never reaches open).
# ---------------------------------------------------------------------------


def test_bad_cell_supplement_refused_at_open(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    decl = SupplementDecl(name="rate_tier", columns={"weight": "DOUBLE"}, rows=[])
    resolved = (
        ResolvedSupplement(
            decl=decl, path=None, sha256=None, rows=(("not-a-number",),)
        ),
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementValueInvalid):
            _open_head(emit, dimensional_shape_config(), resolved)


def test_reserved_name_supplement_refused_at_open(tmp_path: "Path") -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    decl = SupplementDecl(name="_export_meta", columns={"a": "VARCHAR"}, rows=[])
    resolved = (ResolvedSupplement(decl=decl, path=None, sha256=None, rows=(("x",),)),)
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="reserved"):
            _open_head(emit, dimensional_shape_config(), resolved)


def test_timestamptz_supplement_without_anchor_refused_at_open(
    tmp_path: "Path",
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    decl = SupplementDecl(
        name="events", columns={"occurred_at": "TIMESTAMPTZ"}, rows=[]
    )
    resolved = (
        ResolvedSupplement(
            decl=decl,
            path=None,
            sha256=None,
            rows=(("2024-01-15 12:00:00+00:00",),),
        ),
    )
    with open_emit(emit_dir) as emit:
        with pytest.raises(TemporalRenderRequiresAnchor):
            _open_head(emit, dimensional_shape_config(), resolved)


def test_supplement_colliding_with_declared_table_never_reaches_open(
    tmp_path: "Path",
) -> None:
    """The collision is a parse-time `ExportConfig` refusal (`config/models.py`
    `supplements_names_unique`) — constructing the config raises before any
    emit is opened or `open_shaped_playback` is called."""
    plain = dimensional_shape_config()
    assert plain.dimensional is not None
    colliding = SupplementDecl(name="dim_gadget", columns={"a": "VARCHAR"}, rows=[])
    with pytest.raises(ValidationError, match="collides with declared table"):
        ExportConfig(
            mode="dimensional",
            dimensional=plain.dimensional,
            supplements=[colliding],
        )


# ---------------------------------------------------------------------------
# Open reads no emit data: a supplement's cell probe queries only its VALUES
# relation — open makes no base-table read and opens no truncated tape.
# ---------------------------------------------------------------------------


def test_open_with_supplement_reads_no_base_table_data(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    emit_dir = build_shaped_test_emit(tmp_path)
    query_arrow_calls: list[int] = []
    tape_opens: list[int] = []
    original_query_arrow = Emit.query_arrow
    original_open_truncated_tape = shaped_module.open_truncated_tape

    def _counting_query_arrow(
        self: "Emit", sql: str, parameters: tuple[object, ...]
    ) -> "pa.Table":
        query_arrow_calls.append(1)
        return original_query_arrow(self, sql, parameters)

    def _counting_open_truncated_tape(*args: object, **kwargs: object) -> object:
        tape_opens.append(1)
        return original_open_truncated_tape(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Emit, "query_arrow", _counting_query_arrow)
    monkeypatch.setattr(
        shaped_module, "open_truncated_tape", _counting_open_truncated_tape
    )

    with open_emit(emit_dir) as emit:
        _open_head(emit, dimensional_shape_config(), _one_supplement())

    assert query_arrow_calls == []
    assert tape_opens == []
