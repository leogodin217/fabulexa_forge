"""Phase 2: required notice_sink on the stream entry points.

Covers only the signature change itself (source step) — `iter_stream_events`,
`stream_export`, and `seed_mixer_run` all require a trailing/positional
notice_sink argument, and thread it through without altering the event set.
Existing call-site suites migrate in a later step (spec Phase 2 "migrate"
step); this file is new and self-contained.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.config.models import KindStream, StreamConfig
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.mixer.scheduler import (
    Transport,
    seed_mixer_run,
)
from fabulexa_forge.reader.emit import open_emit

from ._helpers import make_anchor

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
        "history_tracked": False,
        "temporal_class": "constant",
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


def _build_single_record_emit(tmp_path: Path) -> Path:
    """Build a minimal one-kind, one-record emit: a single 'c' event."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    columns_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _RECORD_COLS)
    conn.execute(f'CREATE TABLE "records__widget" ({columns_ddl})')
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "w1", 0, True, None, 0, 0, "pending"],
    )
    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "columns": _RECORD_COLS,
                "rows": 1,
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": 0,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


@pytest.fixture
def emit_dir(tmp_path: Path) -> Path:
    return _build_single_record_emit(tmp_path)


@pytest.fixture
def config() -> StreamConfig:
    return StreamConfig(
        content="state-changes",
        streams=[KindStream(name="widgets", kind="widget", properties=["status"])],
    )


def test_iter_stream_events_requires_notice_sink(
    emit_dir: Path, config: StreamConfig
) -> None:
    """notice_sink is a required parameter — omitting it is a TypeError."""
    with open_emit(emit_dir) as emit:
        anchor = make_anchor()
        with pytest.raises(TypeError):
            iter_stream_events(emit, config, anchor)  # type: ignore[call-arg]


def test_iter_stream_events_threads_notice_sink(
    emit_dir: Path, config: StreamConfig
) -> None:
    """The supplied sink receives no notices today; the event set is unchanged."""
    with open_emit(emit_dir) as emit:
        anchor = make_anchor()
        sink = RecordingNoticeSink()
        events = list(iter_stream_events(emit, config, anchor, sink))

    assert sink.notices == []
    assert [e.op for e in events] == ["c"]


def test_stream_export_requires_notice_sink(
    emit_dir: Path, config: StreamConfig
) -> None:
    """notice_sink is a required parameter — omitting it is a TypeError."""
    with open_emit(emit_dir) as emit:
        anchor = make_anchor()
        with pytest.raises(TypeError):
            stream_export(emit, config, "jsonl", "stdout", None, anchor)  # type: ignore[call-arg]


def test_stream_export_threads_notice_sink(
    emit_dir: Path, config: StreamConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """A discarding sink runs stream_export end to end with the event count unchanged."""
    with open_emit(emit_dir) as emit:
        anchor = make_anchor()
        outcome = stream_export(
            emit, config, "jsonl", "stdout", None, anchor, discard_notice_sink
        )

    assert outcome.events_per_topic == {"widgets": 1}


def test_seed_mixer_run_requires_notice_sink(
    emit_dir: Path, config: StreamConfig
) -> None:
    """notice_sink is a required parameter — omitting it is a TypeError."""
    with open_emit(emit_dir) as emit:
        anchor = make_anchor()
        transport = Transport(playing=False, speed=1.0)
        with pytest.raises(TypeError):
            seed_mixer_run(  # type: ignore[call-arg]
                emit, config, anchor, emit.sidecar, transport
            )


def test_seed_mixer_run_threads_notice_sink(
    emit_dir: Path, config: StreamConfig
) -> None:
    """A discarding sink seeds the mixer with the same event set iter_stream_events yields."""
    with open_emit(emit_dir) as emit:
        anchor = make_anchor()
        transport = Transport(playing=False, speed=1.0)
        buffers, _control, _frontier = seed_mixer_run(
            emit, config, anchor, emit.sidecar, transport, discard_notice_sink
        )

    assert [e.op for e in buffers["widgets"]] == ["c"]
