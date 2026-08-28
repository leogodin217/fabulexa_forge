"""Tests for change scope: `only` / `ignore` narrowing of the row-state-events
fold's `u`-event membership (design doc § Change scope).

Materialized against one shared `item` kind carrying two tracked properties
(`status`, `level`), one constant property (`label`), and one non-exempt
`slice_only` property (`secret`); `iter_stream_events` drives the fold
directly. Covers the byte-identical default, `only` / `ignore` narrowing,
projected-vs-scoped independence, the constant-class-in-scope no-op, the
lifecycle-only feed, and the two refusal gates
(`StreamChangeScopeUnresolvable`, the slice_only extension).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.config.models import KindStream, StreamConfig
from fabulexa_forge.errors import ExportError, StreamChangeScopeUnresolvable
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__level",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__secret",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# r1: created t=10, status/level change independently at t=100/t=200, then
#     coincide at t=300 -- the scope-narrowing matrix.
# r2: created t=5, deactivated t=50, no property changes -- the c/d-never-
#     affected control.
_RECORD_ROWS: list[tuple[Any, ...]] = [
    ("trunk", "r1", 10, True, None, 10, 0, "a0", "l0", "const", "s0"),
    ("trunk", "r2", 5, False, 50, 50, 1, "b0", "m0", "const2", "s1"),
]

_HISTORY_ROWS: list[tuple[Any, ...]] = [
    ("trunk", "item", "r1", "status", 100, "a1"),
    ("trunk", "item", "r1", "level", 200, "l1"),
    ("trunk", "item", "r1", "status", 300, "a2"),
    ("trunk", "item", "r1", "level", 300, "l2"),
]


# ---------------------------------------------------------------------------
# Emit + config builder helpers
# ---------------------------------------------------------------------------


def _build_emit(tmp_path: Path) -> Path:
    """Build the shared `item`-kind emit: two records, four history rows."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__item", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))

    placeholders = ", ".join("?" for _ in _RECORD_COLS)
    for row in _RECORD_ROWS:
        conn.execute(f'INSERT INTO "records__item" VALUES ({placeholders})', list(row))
    for row in _HISTORY_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__item",
                "category": "records",
                "columns": _RECORD_COLS,
                "rows": len(_RECORD_ROWS),
                "record_kind": "item",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(_HISTORY_ROWS),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _kind_stream(
    properties: list[str],
    only: list[str] | None = None,
    ignore: list[str] | None = None,
) -> KindStream:
    """Build one `item`-kind KindStream declaration named 'items'."""
    return KindStream(
        name="items", kind="item", properties=properties, only=only, ignore=ignore
    )


def _config(stream: KindStream) -> StreamConfig:
    """Build a single-stream content='state-changes' StreamConfig."""
    return StreamConfig(content="state-changes", streams=[stream])


# ---------------------------------------------------------------------------
# Byte-identical default
# ---------------------------------------------------------------------------


class TestDefaultScope:
    """Both fields absent -> byte-identical to the full-property-set invocation."""

    def test_absent_scope_matches_explicit_full_audited_only(
        self, tmp_path: Path
    ) -> None:
        """No `only` / `ignore` produces the same event stream as `only` set to
        the kind's full audited property set."""
        emit_dir = _build_emit(tmp_path)
        default_config = _config(_kind_stream(["status", "level", "label"]))
        explicit_config = _config(
            _kind_stream(
                ["status", "level", "label"], only=["status", "level", "label"]
            )
        )
        with open_emit(emit_dir) as emit:
            default_events = list(
                iter_stream_events(
                    emit, default_config, None, notice_sink=discard_notice_sink
                )
            )
        with open_emit(emit_dir) as emit:
            explicit_events = list(
                iter_stream_events(
                    emit, explicit_config, None, notice_sink=discard_notice_sink
                )
            )
        assert default_events == explicit_events

    def test_absent_scope_fires_u_at_every_tracked_change_point(
        self, tmp_path: Path
    ) -> None:
        """c@10, u@100 (status), u@200 (level), u@300 (coincide) -- 4 events."""
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status", "level", "label"]))
        with open_emit(emit_dir) as emit:
            events = [
                e
                for e in iter_stream_events(
                    emit, config, None, notice_sink=discard_notice_sink
                )
                if e.record_id == "r1"
            ]
        assert [e.op for e in events] == ["c", "u", "u", "u"]
        assert [e.event_sim_time for e in events] == [10, 100, 200, 300]


# ---------------------------------------------------------------------------
# `only` narrowing
# ---------------------------------------------------------------------------


class TestOnlyNarrowing:
    """`only` narrows change-scope membership to its entries."""

    def test_out_of_scope_only_instant_produces_no_event(self, tmp_path: Path) -> None:
        """The status-only change at t=100 fires no event and consumes no seq;
        level's independent change at t=200 and the t=300 coincidence each
        fire one `u`."""
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status", "level"], only=["level"]))
        with open_emit(emit_dir) as emit:
            all_events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )
        r1_events = [e for e in all_events if e.record_id == "r1"]
        assert [e.op for e in r1_events] == ["c", "u", "u"]
        assert [e.event_sim_time for e in r1_events] == [10, 200, 300]
        # No event for the out-of-scope-only t=100 instant: seq is contiguous
        # across the whole (r1 + r2) merged stream, with no gap for it.
        assert [e.seq for e in all_events] == list(range(1, len(all_events) + 1))
        assert len(all_events) == len(r1_events) + 2  # r2's c and d

    def test_projected_not_scoped_rides_surviving_after_image(
        self, tmp_path: Path
    ) -> None:
        """`status` is projected but out of scope: its t=100 change fires no
        `u`, but its as-of value still rides the t=200 `u`'s after-image."""
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status", "level"], only=["level"]))
        with open_emit(emit_dir) as emit:
            events = [
                e
                for e in iter_stream_events(
                    emit, config, None, notice_sink=discard_notice_sink
                )
                if e.record_id == "r1" and e.event_sim_time == 200
            ]
        assert len(events) == 1
        assert events[0].after is not None
        assert events[0].after["status"] == "a1"
        assert events[0].after["level"] == "l1"

    def test_scoped_not_projected_fires_u_after_image_omits_it(
        self, tmp_path: Path
    ) -> None:
        """`level` is in scope but not projected: its changes still fire `u`,
        but the after-image carries no `level` key."""
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status"], only=["level"]))
        with open_emit(emit_dir) as emit:
            events = [
                e
                for e in iter_stream_events(
                    emit, config, None, notice_sink=discard_notice_sink
                )
                if e.record_id == "r1"
            ]
        assert [e.op for e in events] == ["c", "u", "u"]
        assert [e.event_sim_time for e in events] == [10, 200, 300]
        for event in events[1:]:
            assert event.after is not None
            assert "level" not in event.after

    def test_constant_class_in_scope_is_legal_and_inert(self, tmp_path: Path) -> None:
        """A constant-class name in scope is legal and contributes no change
        points -- only the `c` (and, for r2, `d`) events survive."""
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status", "level", "label"], only=["label"]))
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )
        assert [e.op for e in events] == ["c", "c", "d"]


# ---------------------------------------------------------------------------
# `ignore` narrowing
# ---------------------------------------------------------------------------


class TestIgnoreNarrowing:
    """`ignore` subtracts its entries from the audited default."""

    def test_ignore_covering_every_tracked_property_is_lifecycle_only(
        self, tmp_path: Path
    ) -> None:
        """`ignore` naming both tracked properties leaves a lifecycle-only
        feed: `c` / `d` only, despite r1's tracked history."""
        emit_dir = _build_emit(tmp_path)
        config = _config(
            _kind_stream(["status", "level", "label"], ignore=["status", "level"])
        )
        with open_emit(emit_dir) as emit:
            events = list(
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
            )
        assert [e.op for e in events] == ["c", "c", "d"]

    def test_ignore_one_property_still_fires_the_other(self, tmp_path: Path) -> None:
        """`ignore` naming only `status` leaves `level`'s changes audited."""
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status", "level"], ignore=["status"]))
        with open_emit(emit_dir) as emit:
            events = [
                e
                for e in iter_stream_events(
                    emit, config, None, notice_sink=discard_notice_sink
                )
                if e.record_id == "r1"
            ]
        assert [e.op for e in events] == ["c", "u", "u"]
        assert [e.event_sim_time for e in events] == [10, 200, 300]


# ---------------------------------------------------------------------------
# StreamChangeScopeUnresolvable
# ---------------------------------------------------------------------------


class TestStreamChangeScopeUnresolvable:
    """An `only` / `ignore` entry naming no `prop__` column is refused."""

    def test_only_entry_unresolvable_raises_before_next(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status"], only=["nope"]))
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                StreamChangeScopeUnresolvable,
                match=(
                    "stream 'items': only entry 'nope' has no prop__nope column"
                    " on kind 'item'"
                ),
            ):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_ignore_entry_unresolvable_raises_before_next(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status"], ignore=["nope"]))
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                StreamChangeScopeUnresolvable,
                match=(
                    "stream 'items': ignore entry 'nope' has no prop__nope column"
                    " on kind 'item'"
                ),
            ):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)


# ---------------------------------------------------------------------------
# StreamPropertySliceOnly (extended, over `only` / `ignore`)
# ---------------------------------------------------------------------------


class TestChangeScopeSliceOnly:
    """A non-exempt `slice_only` `only` / `ignore` entry is refused, naming
    the entry's field."""

    def test_only_entry_slice_only_raises_before_next(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status"], only=["secret"]))
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "stream 'items': stream kind 'item': only entry 'secret' is"
                    " temporal_class: slice_only; it cannot ride the"
                    " state-changes after-image"
                ),
            ):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)

    def test_ignore_entry_slice_only_raises_before_next(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(tmp_path)
        config = _config(_kind_stream(["status"], ignore=["secret"]))
        with open_emit(emit_dir) as emit:
            with pytest.raises(
                ExportError,
                match=(
                    "stream 'items': stream kind 'item': ignore entry 'secret' is"
                    " temporal_class: slice_only; it cannot ride the"
                    " state-changes after-image"
                ),
            ):
                iter_stream_events(emit, config, None, notice_sink=discard_notice_sink)
