"""Tests for stream_export in driver.py.

Covers the ExportRuntimeError precondition raises, the ExportError business
rules (DebeziumRequiresConfig, DebeziumRequiresAnchor), the debezium dispatch
path (schemas_enable true/false, file/stdout), the empty-file creation
path for a zero-event kind, paced vs unpaced byte-identity, and membership-events
end-to-end (JSONL file sink and Debezium stdout/file/kafka).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    DebeziumConfig,
    DebeziumSourceIdentity,
    KindStream,
    MembershipRef,
    MembershipStream,
    StreamConfig,
)
from fabulexa_forge.errors import (
    ExportError,
    ExportRuntimeError,
)
from fabulexa_forge.exporters.streaming.driver import stream_export
from fabulexa_forge.exporters.streaming.pacer import ResolvedClock
from fabulexa_forge.exporters.streaming.types import StreamEvent, StreamOutcome
from fabulexa_forge.playback.stream_render import StreamRender, resolve_stream_render
from fabulexa_forge.reader.emit import open_emit

from ._helpers import _ddl, _membership_table_spec, make_anchor

# ---------------------------------------------------------------------------
# Column / sidecar helpers (minimal, mirrors test_engine.py patterns)
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_HISTORY_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    {"name": "kind", "type": "VARCHAR"},
    identity_column("record_id", "VARCHAR"),
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    return spec


def _build_emit(
    tmp_path: Path,
    kind: str,
    record_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal emit with one kind."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))

    ph = ", ".join("?" for _ in _RECORD_COLS)
    for row in record_rows:
        conn.execute(f'INSERT INTO "records__{kind}" VALUES ({ph})', list(row))
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                f"records__{kind}",
                "records",
                _RECORD_COLS,
                len(record_rows),
                record_kind=kind,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _make_config(kind: str, properties: list[str] | None = None) -> StreamConfig:
    """Build a minimal StreamConfig for one kind."""
    return StreamConfig(
        content="state-changes",
        streams=[KindStream(name=kind, kind=kind, properties=properties or [])],
    )


def _make_debezium_source() -> DebeziumSourceIdentity:
    """Build a minimal DebeziumSourceIdentity for tests."""
    return DebeziumSourceIdentity(
        connector="postgresql",
        name="myserver",
        db="testdb",
        **{"schema": "public"},
        version="1.9.0.Final",
    )


def _make_debezium_config(schemas_enable: bool = True) -> DebeziumConfig:
    """Build a DebeziumConfig for tests."""
    return DebeziumConfig(source=_make_debezium_source(), schemas_enable=schemas_enable)


def _make_debezium_stream_config(
    kind: str,
    properties: list[str] | None = None,
    schemas_enable: bool = True,
) -> StreamConfig:
    """Build a StreamConfig with a debezium block for one kind."""
    return StreamConfig(
        content="state-changes",
        streams=[KindStream(name=kind, kind=kind, properties=properties or ["status"])],
        debezium=_make_debezium_config(schemas_enable=schemas_enable),
    )


def _make_anchor() -> EffectiveAnchor:
    """Build a fixed EffectiveAnchor for tests."""
    return make_anchor()


# ---------------------------------------------------------------------------
# Precondition raises — three ExportRuntimeError guards in stream_export
# ---------------------------------------------------------------------------


class TestStreamExportPreconditions:
    """stream_export raises ExportRuntimeError for invalid fmt/sink/out combos."""

    def test_unsupported_fmt_raises_with_message(self, tmp_path: Path) -> None:
        """An unsupported fmt value raises ExportRuntimeError naming the bad value."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="unsupported format"):
                stream_export(
                    emit,
                    config,
                    fmt="csv",  # type: ignore[arg-type]
                    sink="stdout",
                    out=None,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )

    def test_file_sink_with_out_none_raises_with_message(self, tmp_path: Path) -> None:
        """sink='file' and out=None raises ExportRuntimeError naming the requirement."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="output directory"):
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="file",
                    out=None,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )

    def test_stdout_sink_with_out_set_raises_with_message(self, tmp_path: Path) -> None:
        """sink='stdout' and out set raises ExportRuntimeError naming the constraint."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="out=None"):
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="stdout",
                    out=out_dir,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )

    def test_file_sink_with_missing_out_dir_raises_naming_it(
        self, tmp_path: Path
    ) -> None:
        """A missing out directory raises ExportRuntimeError naming the path.

        The driver refuses rather than creating the directory (matching `export`),
        and refuses up front so nothing is written before the failure.
        """
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        missing = tmp_path / "not-there"
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="no such directory") as exc:
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="file",
                    out=missing,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )
        assert str(missing) in str(exc.value)
        assert not missing.exists(), "the driver must not create the output directory"

    def test_file_sink_with_out_pointing_at_a_file_raises(self, tmp_path: Path) -> None:
        """An `out` that exists but is a regular file raises, saying so."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        not_a_dir = tmp_path / "a-file"
        not_a_dir.write_text("", encoding="utf-8")
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportRuntimeError, match="not a directory"):
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="file",
                    out=not_a_dir,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )


# ---------------------------------------------------------------------------
# Empty-file creation path — zero-event kind produces an empty <kind>.jsonl
# ---------------------------------------------------------------------------


class TestStreamExportEmptyFile:
    """A selected kind with zero records produces an empty <kind>.jsonl file."""

    def test_zero_record_kind_produces_empty_jsonl(self, tmp_path: Path) -> None:
        """stream_export creates an empty <kind>.jsonl for a kind with no records."""
        emit_dir = _build_emit(tmp_path, "item", record_rows=[], history_rows=[])
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        empty_file = out_dir / "item.jsonl"
        assert empty_file.exists(), "empty item.jsonl must be created"
        assert empty_file.read_text(encoding="utf-8") == "", "empty file must be empty"
        assert outcome.events_per_topic == {"item": 0}
        assert outcome.total_events == 0


# ---------------------------------------------------------------------------
# Business rules — DebeziumRequiresConfig and DebeziumRequiresAnchor
# ---------------------------------------------------------------------------

_DAY = 86_400_000_000_000


class TestDebeziumBusinessRules:
    """stream_export raises ExportError for missing debezium config or anchor."""

    def test_debezium_requires_config_raises(self, tmp_path: Path) -> None:
        """fmt='debezium' with no debezium block raises ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")  # no debezium block
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=anchor,
                    notice_sink=discard_notice_sink,
                )

    def test_debezium_requires_anchor_raises(self, tmp_path: Path) -> None:
        """fmt='debezium' with anchor=None raises ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_debezium_stream_config("item")
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolved effective anchor"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )

    def test_debezium_requires_config_checked_before_anchor(
        self, tmp_path: Path
    ) -> None:
        """DebeziumRequiresConfig is checked before DebeziumRequiresAnchor."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")  # no debezium block
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )


# ---------------------------------------------------------------------------
# Debezium stdout delivery
# ---------------------------------------------------------------------------


class TestDebeziumStdoutDelivery:
    """stream_export dispatches to write_debezium_stream for fmt='debezium'."""

    def test_debezium_stdout_delivers_messages(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """fmt='debezium' with stdout sink writes Debezium messages to stdout."""
        record_rows = [
            ("trunk", "r001", 1 * _DAY, True, None, 1 * _DAY, 0, "active"),
        ]
        history_rows = [
            ("trunk", "item", "r001", "status", 1 * _DAY, "active"),
        ]
        emit_dir = _build_emit(tmp_path, "item", record_rows, history_rows)
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="debezium",
                sink="stdout",
                out=None,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 1
        for line in lines:
            msg = json.loads(line)
            assert "payload" in msg  # schemas_enable=True wraps {schema, payload}
        assert outcome.events_per_topic["item"] == len(lines)

    def test_debezium_stdout_bare_payload_when_schemas_disabled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schemas_enable=false produces bare envelope payloads (no schema wrapper)."""
        record_rows = [
            ("trunk", "r001", 1 * _DAY, True, None, 1 * _DAY, 0, "active"),
        ]
        history_rows = [
            ("trunk", "item", "r001", "status", 1 * _DAY, "active"),
        ]
        emit_dir = _build_emit(tmp_path, "item", record_rows, history_rows)
        config = _make_debezium_stream_config("item", schemas_enable=False)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="stdout",
                out=None,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 1
        for line in lines:
            msg = json.loads(line)
            # bare payload has "op", "before", "after" keys — not wrapped
            assert "schema" not in msg
            assert "op" in msg


# ---------------------------------------------------------------------------
# Debezium file delivery
# ---------------------------------------------------------------------------


class TestDebeziumFileDelivery:
    """stream_export file sink writes one <kind>.jsonl per selected kind."""

    def test_debezium_file_writes_kind_jsonl(self, tmp_path: Path) -> None:
        """fmt='debezium' with file sink writes <kind>.jsonl."""
        record_rows = [
            ("trunk", "r001", 1 * _DAY, True, None, 1 * _DAY, 0, "active"),
        ]
        history_rows = [
            ("trunk", "item", "r001", "status", 1 * _DAY, "active"),
        ]
        emit_dir = _build_emit(tmp_path, "item", record_rows, history_rows)
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        out_file = out_dir / "item.jsonl"
        assert out_file.exists()
        lines = [
            ln for ln in out_file.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        assert len(lines) == 1
        assert outcome.events_per_topic["item"] == len(lines)

    def test_debezium_file_empty_kind_creates_empty_file(self, tmp_path: Path) -> None:
        """A selected kind with zero events gets an empty <kind>.jsonl."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        out_file = out_dir / "item.jsonl"
        assert out_file.exists()
        assert out_file.read_text(encoding="utf-8") == ""
        assert outcome.events_per_topic == {"item": 0}
        assert outcome.total_events == 0


# ---------------------------------------------------------------------------
# Paced vs unpaced byte-identity (Phase 4)
# ---------------------------------------------------------------------------


def _make_clock(
    speed: float = 1e9, idle_cap_seconds: float | None = None
) -> ResolvedClock:
    """Build a ResolvedClock with a very high speed so tests don't actually sleep."""
    return ResolvedClock(speed=speed, idle_cap_seconds=idle_cap_seconds)


def _build_emit_with_events(tmp_path: Path, kind: str) -> Path:
    """Build a minimal emit with one record and one history row."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _DAY = 86_400_000_000_000
    record_rows = [
        ("trunk", "r001", 1 * _DAY, True, None, 2 * _DAY, 0, "active"),
    ]
    history_rows = [
        ("trunk", kind, "r001", "status", 1 * _DAY, "initial"),
        ("trunk", kind, "r001", "status", 2 * _DAY, "active"),
    ]
    return _build_emit(tmp_path, kind, record_rows, history_rows)


class TestStreamExportPacedByteIdentity:
    """Paced and unpaced stream_export produce byte-identical output."""

    def test_jsonl_file_paced_byte_identical_to_unpaced(self, tmp_path: Path) -> None:
        """Paced jsonl file output equals unpaced output byte-for-byte."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")

        out_unpaced = tmp_path / "out_unpaced"
        out_unpaced.mkdir()
        out_paced = tmp_path / "out_paced"
        out_paced.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "jsonl",
                "file",
                out_unpaced,
                anchor=None,
                notice_sink=discard_notice_sink,
            )
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "jsonl",
                "file",
                out_paced,
                anchor=None,
                clock=_make_clock(),
                notice_sink=discard_notice_sink,
            )

        unpaced_text = (out_unpaced / "item.jsonl").read_text(encoding="utf-8")
        paced_text = (out_paced / "item.jsonl").read_text(encoding="utf-8")
        assert paced_text == unpaced_text

    def test_jsonl_stdout_paced_byte_identical_to_unpaced(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Paced jsonl stdout output equals unpaced output byte-for-byte."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "jsonl",
                "stdout",
                None,
                anchor=None,
                notice_sink=discard_notice_sink,
            )
        unpaced_out = capsys.readouterr().out

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "jsonl",
                "stdout",
                None,
                anchor=None,
                clock=_make_clock(),
                notice_sink=discard_notice_sink,
            )
        paced_out = capsys.readouterr().out

        assert paced_out == unpaced_out

    def test_debezium_file_paced_byte_identical_to_unpaced(
        self, tmp_path: Path
    ) -> None:
        """Paced debezium file output equals unpaced output byte-for-byte."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()

        out_unpaced = tmp_path / "out_unpaced"
        out_unpaced.mkdir()
        out_paced = tmp_path / "out_paced"
        out_paced.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "debezium",
                "file",
                out_unpaced,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )
        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                "debezium",
                "file",
                out_paced,
                anchor=anchor,
                clock=_make_clock(),
                notice_sink=discard_notice_sink,
            )

        unpaced_text = (out_unpaced / "item.jsonl").read_text(encoding="utf-8")
        paced_text = (out_paced / "item.jsonl").read_text(encoding="utf-8")
        assert paced_text == unpaced_text

    def test_anchorless_paced_jsonl_is_valid(self, tmp_path: Path) -> None:
        """A realtime clock with anchor=None (raw-ns ts) paces without error."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                "jsonl",
                "file",
                out_dir,
                anchor=None,
                clock=_make_clock(),
                notice_sink=discard_notice_sink,
            )

        assert outcome.total_events > 0
        lines = (out_dir / "item.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == outcome.total_events
        for line in lines:
            obj = json.loads(line)
            assert isinstance(obj["ts"], int)  # raw ns when no anchor

    def test_clock_none_regression(self, tmp_path: Path) -> None:
        """clock=None delivers unpaced, exactly the original path."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                "jsonl",
                "file",
                out_dir,
                anchor=None,
                clock=None,
                notice_sink=discard_notice_sink,
            )

        assert outcome.total_events > 0


# ---------------------------------------------------------------------------
# Helpers for kafka sink tests — fake write_kafka_stream
# ---------------------------------------------------------------------------


def _fake_write_kafka_stream(
    captured: list[dict[str, Any]],
) -> Callable[..., StreamOutcome]:
    """Return a fake write_kafka_stream that captures calls."""

    def _fake(
        events: Any,
        render_value: Callable[[StreamEvent], bytes],
        render_key: Callable[[StreamEvent], bytes],
        render_timestamp: Callable[[StreamEvent], int],
        bootstrap_servers: str,
        topic_set: tuple[str, ...],
        *,
        paced: bool = False,
    ) -> StreamOutcome:
        captured.append(
            {
                "render_value": render_value,
                "render_key": render_key,
                "render_timestamp": render_timestamp,
                "bootstrap_servers": bootstrap_servers,
                "topic_set": topic_set,
                "paced": paced,
                "events": list(events),
            }
        )
        counts = {t: 0 for t in topic_set}
        return StreamOutcome(total_events=0, events_per_topic=counts)

    return _fake


def _resolve_oracle_render(
    emit_dir: Path,
    config: StreamConfig,
    fmt: Literal["jsonl", "debezium"],
    anchor: EffectiveAnchor | None,
) -> StreamRender:
    """Resolve an independent StreamRender oracle for byte-parity assertions.

    A second, independent resolution of the same (emit, config, fmt, anchor) —
    render purity guarantees it agrees with whatever render_value/render_key
    the driver threaded through to write_kafka_stream.
    """
    with open_emit(emit_dir) as emit:
        return resolve_stream_render(emit, config, fmt, anchor, discard_notice_sink)


# ---------------------------------------------------------------------------
# write_line_stream — paced file-sink abort cleanup
# ---------------------------------------------------------------------------


def _line_event(seq: int, kind: str, record_id: str) -> StreamEvent:
    """Build a minimal StreamEvent for write_line_stream tests."""
    return StreamEvent(
        seq=seq,
        op="c",
        kind=kind,
        record_id=record_id,
        event_sim_time=0,
        ts="2026-01-01T00:00:00+00:00",
        after={"record_id": record_id},
        topic=kind,
        route_table=kind,
        key_column="record_id",
        key_value=record_id,
    )


class TestWriteLineStreamPacedAbort:
    """write_line_stream's paced file-sink cleanup on a mid-stream failure."""

    def test_paced_abort_closes_all_open_handles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception mid-stream closes every per-topic handle (finally cleanup).

        Covers _write_line_file_paced's ``finally: for handle in
        handles.values(): handle.close()`` abort path: when the event source
        raises mid-run (e.g. the pacer's clock fails), the exception propagates
        AND every lazily-opened per-topic handle is closed — no leaked open
        file objects. Lines flushed before the abort remain on disk.
        """
        import builtins
        from typing import IO, Iterator

        from fabulexa_forge.exporters.streaming.driver import write_line_stream

        opened: list[IO[Any]] = []
        real_open = builtins.open

        def _tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            handle = real_open(file, *args, **kwargs)
            if str(tmp_path) in str(file):
                opened.append(handle)
            return handle

        monkeypatch.setattr(builtins, "open", _tracking_open)

        class _StreamAbort(RuntimeError):
            """Sentinel raised by the event source mid-run."""

        def _events() -> Iterator[StreamEvent]:
            yield _line_event(seq=1, kind="alpha", record_id="a1")
            yield _line_event(seq=2, kind="beta", record_id="b1")
            raise _StreamAbort("event source failed mid-run")

        def _render_value(event: StreamEvent) -> bytes:
            return json.dumps({"seq": event.seq}).encode("utf-8")

        with pytest.raises(_StreamAbort):
            write_line_stream(
                _events(),
                _render_value,
                "file",
                tmp_path,
                topic_set=("alpha", "beta"),
                paced=True,
            )

        # Both per-topic handles were opened, and the finally closed each one.
        assert len(opened) == 2
        assert all(handle.closed for handle in opened)
        # Events written before the abort were flushed and survive on disk.
        alpha_lines = (
            (tmp_path / "alpha.jsonl").read_bytes().decode("utf-8").splitlines()
        )
        beta_lines = (tmp_path / "beta.jsonl").read_bytes().decode("utf-8").splitlines()
        assert [json.loads(ln)["seq"] for ln in alpha_lines] == [1]
        assert [json.loads(ln)["seq"] for ln in beta_lines] == [2]


# ---------------------------------------------------------------------------
# Kafka sink: KafkaRequiresAnchor
# ---------------------------------------------------------------------------


class TestKafkaRequiresAnchor:
    """sink='kafka' with anchor=None raises ExportError for both formats."""

    def test_kafka_jsonl_no_anchor_raises(self, tmp_path: Path) -> None:
        """sink='kafka', fmt='jsonl', anchor=None → ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(
                    ExportError, match="kafka.*resolved effective anchor"
                ):
                    stream_export(
                        emit,
                        config,
                        fmt="jsonl",
                        sink="kafka",
                        out=None,
                        anchor=None,
                        bootstrap_servers="localhost:9092",
                        notice_sink=discard_notice_sink,
                    )

        assert len(captured) == 0

    def test_kafka_debezium_no_anchor_raises(self, tmp_path: Path) -> None:
        """sink='kafka', fmt='debezium', anchor=None → ExportError."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_debezium_stream_config("item")
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(
                    ExportError, match="kafka.*resolved effective anchor"
                ):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=None,
                        bootstrap_servers="localhost:9092",
                        notice_sink=discard_notice_sink,
                    )

        assert len(captured) == 0


# ---------------------------------------------------------------------------
# Kafka sink: jsonl render_value produces byte-identical output
# ---------------------------------------------------------------------------


class TestKafkaJsonlRenderValue:
    """sink='kafka', fmt='jsonl': render_value output == jsonl file line minus newline."""

    def test_render_value_byte_identical_to_jsonl_line(self, tmp_path: Path) -> None:
        """write_kafka_stream gets render_value whose bytes == jsonl line without \\n."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                    notice_sink=discard_notice_sink,
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        # _make_config uses properties=[] → the event set is payload-independent
        # (change_scope = the kind's full tracked property set): a 'c' at creation
        # plus a 'u' at the tracked status change from _build_emit_with_events.
        assert len(events) == 2

        # Compare to what the file sink would write (minus the newline)
        from fabulexa_forge.exporters.streaming.encoding import encode_pinned
        from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object

        for event in events:
            expected = encode_pinned(render_jsonl_object(event)).encode("utf-8")
            assert render_value(event) == expected

    def test_topic_set_matches_build_topic_set(self, tmp_path: Path) -> None:
        """write_kafka_stream receives topic_set == build_topic_set(config)."""
        from fabulexa_forge.exporters.streaming.engine import build_topic_set

        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                expected_topic_set = build_topic_set(config)
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                    notice_sink=discard_notice_sink,
                )

        assert captured[0]["topic_set"] == expected_topic_set


# ---------------------------------------------------------------------------
# Kafka sink: debezium rules still fire
# ---------------------------------------------------------------------------


class TestKafkaDebeziumRules:
    """sink='kafka', fmt='debezium': debezium business rules apply."""

    def test_debezium_requires_config_fires_for_kafka(self, tmp_path: Path) -> None:
        """DebeziumRequiresConfig fires on sink='kafka' when debezium block absent."""
        emit_dir = _build_emit(tmp_path, "item", [], [])
        config = _make_config("item")  # no debezium block
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(ExportError, match="debezium.*config block"):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=anchor,
                        bootstrap_servers="localhost:9092",
                        notice_sink=discard_notice_sink,
                    )

        assert len(captured) == 0

    def test_debezium_render_value_byte_identical_to_file_line(
        self, tmp_path: Path
    ) -> None:
        """Debezium render_value output == debezium file line without \\n."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_debezium_stream_config("item", schemas_enable=True)
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                    notice_sink=discard_notice_sink,
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        assert len(events) == 2

        debezium_cfg = config.debezium
        assert debezium_cfg is not None and debezium_cfg.schemas_enable is True

        # Independent oracle: a second resolve_stream_render over the same
        # (emit, config, fmt, anchor) — render purity guarantees agreement
        # with the render the driver threaded into write_kafka_stream.
        oracle = _resolve_oracle_render(emit_dir, config, "debezium", anchor)
        for event in events:
            assert render_value(event) == oracle.render_bytes(event)


# ---------------------------------------------------------------------------
# Kafka sink: paced vs unpaced
# ---------------------------------------------------------------------------


class TestKafkaPacedDelivery:
    """Paced sink='kafka' passes paced=True and produces identical results."""

    def test_paced_passes_paced_true_to_write_kafka_stream(
        self, tmp_path: Path
    ) -> None:
        """A non-None clock causes paced=True to be passed to write_kafka_stream."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        clock = _make_clock()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    clock=clock,
                    bootstrap_servers="localhost:9092",
                    notice_sink=discard_notice_sink,
                )

        assert captured[0]["paced"] is True

    def test_unpaced_passes_paced_false_to_write_kafka_stream(
        self, tmp_path: Path
    ) -> None:
        """clock=None causes paced=False to be passed to write_kafka_stream."""
        emit_dir = _build_emit_with_events(tmp_path / "emit", "item")
        config = _make_config("item")
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="jsonl",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    clock=None,
                    bootstrap_servers="localhost:9092",
                    notice_sink=discard_notice_sink,
                )

        assert captured[0]["paced"] is False


# ---------------------------------------------------------------------------
# Membership emit builder helpers
# ---------------------------------------------------------------------------

_MEMBERSHIP_WAITERS_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
]

_MEMBERSHIP_MEMBERS_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
]

#: Election resolution requires the owner kind to carry a declared records
#: table, even under the no-`keys` default (see test_engine.py's
#: `_owner_records_table_spec`).
_OWNER_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]


def _build_membership_emit(
    tmp_path: Path,
    waiters_rows: list[tuple[Any, ...]],
    members_rows: list[tuple[Any, ...]],
) -> Path:
    """Build a minimal emit with two membership tables.

    Table membership__queue__waiters has elem__priority; membership__team__members
    has no element fields. waiters_rows is (fork_path, record_id, joined_sim_time,
    left_sim_time, elem__priority); members_rows is (fork_path, record_id,
    joined_sim_time, left_sim_time).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl("membership__queue__waiters", _MEMBERSHIP_WAITERS_COLS))
    ph_w = ", ".join("?" for _ in _MEMBERSHIP_WAITERS_COLS)
    for row in waiters_rows:
        conn.execute(
            f'INSERT INTO "membership__queue__waiters" VALUES ({ph_w})', list(row)
        )

    conn.execute(_ddl("membership__team__members", _MEMBERSHIP_MEMBERS_COLS))
    ph_m = ", ".join("?" for _ in _MEMBERSHIP_MEMBERS_COLS)
    for row in members_rows:
        conn.execute(
            f'INSERT INTO "membership__team__members" VALUES ({ph_m})', list(row)
        )

    for owner_kind in ("queue", "team"):
        conn.execute(_ddl(f"records__{owner_kind}", _OWNER_RECORD_COLS))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _membership_table_spec(
                "membership__queue__waiters",
                _MEMBERSHIP_WAITERS_COLS,
                len(waiters_rows),
                "queue",
                "waiters",
            ),
            _membership_table_spec(
                "membership__team__members",
                _MEMBERSHIP_MEMBERS_COLS,
                len(members_rows),
                "team",
                "members",
            ),
            {
                "name": "records__queue",
                "category": "records",
                "record_kind": "queue",
                "columns": _OWNER_RECORD_COLS,
                "rows": 0,
            },
            {
                "name": "records__team",
                "category": "records",
                "record_kind": "team",
                "columns": _OWNER_RECORD_COLS,
                "rows": 0,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def _make_membership_config(
    waiters_fields: list[str] | None = None,
    members_fields: list[str] | None = None,
) -> StreamConfig:
    """Build a StreamConfig for content='membership-events' with two tables."""
    return StreamConfig(
        content="membership-events",
        streams=[
            MembershipStream(
                name="queue-waiters",
                membership=MembershipRef(kind="queue", property="waiters"),
                fields=waiters_fields if waiters_fields is not None else ["priority"],
            ),
            MembershipStream(
                name="team-members",
                membership=MembershipRef(kind="team", property="members"),
                fields=members_fields if members_fields is not None else [],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Phase 3: Membership JSONL file sink
# ---------------------------------------------------------------------------

_NS = 1_000_000_000  # one second in nanoseconds


class TestMembershipJsonlFileSink:
    """stream_export content='membership-events', fmt='jsonl', sink='file' end-to-end."""

    def test_membership_jsonl_writes_per_topic_files(self, tmp_path: Path) -> None:
        """stream_export writes one .jsonl file per membership topic."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
            ("trunk", "r2", 2 * _NS, None, "low"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        assert (out_dir / "queue-waiters.jsonl").exists()
        assert (out_dir / "team-members.jsonl").exists()
        assert outcome.events_per_topic["team-members"] == 0
        assert outcome.events_per_topic["queue-waiters"] > 0

    def test_membership_jsonl_lines_parse_to_valid_objects(
        self, tmp_path: Path
    ) -> None:
        """Each line in the membership JSONL output parses to a valid event object."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        lines = [
            json.loads(ln)
            for ln in (out_dir / "queue-waiters.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        assert len(lines) >= 1
        for obj in lines:
            assert "seq" in obj
            assert "op" in obj
            assert "ts" in obj
            assert obj["op"] in ("join", "leave")

    def test_membership_jsonl_closed_interval_emits_join_and_leave(
        self, tmp_path: Path
    ) -> None:
        """A closed membership interval (left_sim_time non-null) emits join + leave."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        lines = [
            json.loads(ln)
            for ln in (out_dir / "queue-waiters.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        ops = [obj["op"] for obj in lines]
        assert "join" in ops
        assert "leave" in ops

    def test_membership_jsonl_open_interval_emits_join_only(
        self, tmp_path: Path
    ) -> None:
        """An open membership interval (left_sim_time IS NULL) emits join only."""
        waiters_rows = [
            ("trunk", "r2", 2 * _NS, None, "low"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        lines = [
            json.loads(ln)
            for ln in (out_dir / "queue-waiters.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        ops = [obj["op"] for obj in lines]
        assert ops == ["join"]

    def test_membership_jsonl_outcome_counts(self, tmp_path: Path) -> None:
        """StreamOutcome counts match events written (join+leave + empty topic)."""
        waiters_rows = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
            ("trunk", "r2", 2 * _NS, None, "low"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        # r1 closed: join + leave = 2; r2 open: join = 1; total = 3
        assert outcome.total_events == 3
        assert outcome.events_per_topic["queue-waiters"] == 3
        assert outcome.events_per_topic["team-members"] == 0

    def test_membership_jsonl_declared_but_empty_topic_creates_empty_file(
        self, tmp_path: Path
    ) -> None:
        """A selected membership table with no rows produces an empty .jsonl file."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="jsonl",
                sink="file",
                out=out_dir,
                anchor=None,
                notice_sink=discard_notice_sink,
            )

        waiters_file = out_dir / "queue-waiters.jsonl"
        members_file = out_dir / "team-members.jsonl"
        assert waiters_file.exists()
        assert members_file.exists()
        assert waiters_file.read_text(encoding="utf-8") == ""
        assert members_file.read_text(encoding="utf-8") == ""
        assert outcome.total_events == 0
        assert outcome.events_per_topic == {"queue-waiters": 0, "team-members": 0}


# ---------------------------------------------------------------------------
# Membership Debezium helpers
# ---------------------------------------------------------------------------


def _make_membership_debezium_config(
    schemas_enable: bool = True,
    waiters_fields: list[str] | None = None,
    members_fields: list[str] | None = None,
) -> StreamConfig:
    """Build a StreamConfig for membership-events with a debezium block."""
    return StreamConfig(
        content="membership-events",
        streams=[
            MembershipStream(
                name="queue-waiters",
                membership=MembershipRef(kind="queue", property="waiters"),
                fields=waiters_fields if waiters_fields is not None else ["priority"],
            ),
            MembershipStream(
                name="team-members",
                membership=MembershipRef(kind="team", property="members"),
                fields=members_fields if members_fields is not None else [],
            ),
        ],
        debezium=_make_debezium_config(schemas_enable=schemas_enable),
    )


def _one_waiter_emit(tmp_path: Path) -> Path:
    """Build a membership emit with one waiter row and no members."""
    waiters_rows: list[tuple[Any, ...]] = [
        ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
    ]
    return _build_membership_emit(tmp_path / "emit", waiters_rows, [])


# ---------------------------------------------------------------------------
# Membership Debezium: stdout sink
# ---------------------------------------------------------------------------


class TestMembershipDebeziumStdout:
    """membership-events + fmt='debezium' + sink='stdout' renders Debezium envelopes."""

    def test_membership_debezium_stdout_renders_events(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """fmt='debezium' over membership content writes lines to stdout (op='c')."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="debezium",
                sink="stdout",
                out=None,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            msg = json.loads(line)
            payload = msg["payload"]
            assert payload["op"] == "c"
            assert "event" in payload["after"]
        assert outcome.total_events == len(lines)

    def test_membership_debezium_stdout_schemas_wrapped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schemas_enable=True wraps each message in {schema, payload}."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="stdout",
                out=None,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            msg = json.loads(line)
            assert "schema" in msg
            assert "payload" in msg

    def test_membership_debezium_stdout_bare_when_schemas_disabled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schemas_enable=False produces bare envelopes without schema wrapper."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=False)
        anchor = _make_anchor()

        with open_emit(emit_dir) as emit:
            stream_export(
                emit,
                config,
                fmt="debezium",
                sink="stdout",
                out=None,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) >= 1
        for line in lines:
            msg = json.loads(line)
            assert "schema" not in msg
            assert "op" in msg
            assert msg["op"] == "c"


# ---------------------------------------------------------------------------
# Membership Debezium: file sink
# ---------------------------------------------------------------------------


class TestMembershipDebeziumFileSink:
    """membership-events + fmt='debezium' + sink='file' writes per-topic JSONL."""

    def test_membership_debezium_file_writes_per_topic_files(
        self, tmp_path: Path
    ) -> None:
        """fmt='debezium' file sink writes one <topic>.jsonl per membership topic."""
        emit_dir = _one_waiter_emit(tmp_path)
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        waiters_file = out_dir / "queue-waiters.jsonl"
        members_file = out_dir / "team-members.jsonl"
        assert waiters_file.exists()
        assert members_file.exists()
        lines = [
            ln
            for ln in waiters_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 2
        for line in lines:
            msg = json.loads(line)
            assert msg["payload"]["op"] == "c"
            assert "event" in msg["payload"]["after"]
        assert outcome.events_per_topic["queue-waiters"] == len(lines)

    def test_membership_debezium_file_empty_table_creates_empty_file(
        self, tmp_path: Path
    ) -> None:
        """A selected membership table with no rows produces an empty .jsonl file."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit,
                config,
                fmt="debezium",
                sink="file",
                out=out_dir,
                anchor=anchor,
                notice_sink=discard_notice_sink,
            )

        assert (out_dir / "queue-waiters.jsonl").read_text(encoding="utf-8") == ""
        assert (out_dir / "team-members.jsonl").read_text(encoding="utf-8") == ""
        assert outcome.total_events == 0


# ---------------------------------------------------------------------------
# Membership Debezium: DebeziumRequiresConfig + DebeziumRequiresAnchor
# ---------------------------------------------------------------------------


class TestMembershipDebeziumBusinessRules:
    """membership-events debezium business rules fire in the correct order."""

    def test_membership_debezium_requires_config_stdout(self, tmp_path: Path) -> None:
        """membership + fmt='debezium' + no debezium block → ExportError (config)."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()  # no debezium block
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=anchor,
                    notice_sink=discard_notice_sink,
                )

    def test_membership_debezium_requires_config_file(self, tmp_path: Path) -> None:
        """membership + fmt='debezium' + no debezium block fires on file sink too."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()
        anchor = _make_anchor()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="file",
                    out=out_dir,
                    anchor=anchor,
                    notice_sink=discard_notice_sink,
                )

    def test_membership_debezium_requires_anchor_stdout(self, tmp_path: Path) -> None:
        """membership + fmt='debezium' + anchor=None → ExportError (anchor)."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_debezium_config()
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="resolved effective anchor"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )

    def test_membership_debezium_config_checked_before_anchor(
        self, tmp_path: Path
    ) -> None:
        """DebeziumRequiresConfig fires before DebeziumRequiresAnchor for membership."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()  # no debezium block; anchor also missing
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="debezium.*config block"):
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="stdout",
                    out=None,
                    anchor=None,
                    notice_sink=discard_notice_sink,
                )


# ---------------------------------------------------------------------------
# Membership Debezium: kafka sink
# ---------------------------------------------------------------------------


class TestMembershipDebeziumKafka:
    """membership-events + fmt='debezium' + sink='kafka' dispatches correctly."""

    def test_membership_kafka_requires_anchor_first(self, tmp_path: Path) -> None:
        """sink='kafka' with anchor=None raises KafkaRequiresAnchor before debezium rules."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_debezium_config()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(
                    ExportError, match="kafka.*resolved effective anchor"
                ):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=None,
                        bootstrap_servers="localhost:9092",
                        notice_sink=discard_notice_sink,
                    )

        assert len(captured) == 0

    def test_membership_kafka_requires_config(self, tmp_path: Path) -> None:
        """membership + sink='kafka' + no debezium block → DebeziumRequiresConfig."""
        emit_dir = _build_membership_emit(tmp_path / "emit", [], [])
        config = _make_membership_config()  # no debezium block
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                with pytest.raises(ExportError, match="debezium.*config block"):
                    stream_export(
                        emit,
                        config,
                        fmt="debezium",
                        sink="kafka",
                        out=None,
                        anchor=anchor,
                        bootstrap_servers="localhost:9092",
                        notice_sink=discard_notice_sink,
                    )

        assert len(captured) == 0

    def test_membership_kafka_render_value_matches_file_line(
        self, tmp_path: Path
    ) -> None:
        """Debezium render_value for membership kafka == file line without newline."""
        waiters_rows: list[tuple[Any, ...]] = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_debezium_config(schemas_enable=True)
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                    notice_sink=discard_notice_sink,
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        assert len(events) >= 1

        # Independent oracle: a second resolve_stream_render over the same
        # (emit, config, fmt, anchor) — render purity guarantees agreement
        # with the render the driver threaded into write_kafka_stream.
        oracle = _resolve_oracle_render(emit_dir, config, "debezium", anchor)
        for event in events:
            assert render_value(event) == oracle.render_bytes(event)

    def test_membership_kafka_schemas_disabled_bare_render(
        self, tmp_path: Path
    ) -> None:
        """schemas_enable=False produces bare-envelope render_value bytes."""
        waiters_rows: list[tuple[Any, ...]] = [
            ("trunk", "r1", 1 * _NS, 3 * _NS, "high"),
        ]
        emit_dir = _build_membership_emit(tmp_path / "emit", waiters_rows, [])
        config = _make_membership_debezium_config(schemas_enable=False)
        anchor = _make_anchor()
        captured: list[dict[str, Any]] = []

        with patch(
            "fabulexa_forge.exporters.streaming.kafka_sink.write_kafka_stream",
            side_effect=_fake_write_kafka_stream(captured),
        ):
            with open_emit(emit_dir) as emit:
                stream_export(
                    emit,
                    config,
                    fmt="debezium",
                    sink="kafka",
                    out=None,
                    anchor=anchor,
                    bootstrap_servers="localhost:9092",
                    notice_sink=discard_notice_sink,
                )

        assert len(captured) == 1
        call = captured[0]
        events = call["events"]
        render_value: Callable[[StreamEvent], bytes] = call["render_value"]
        for event in events:
            msg = json.loads(render_value(event).decode("utf-8"))
            assert "schema" not in msg
            assert msg["op"] == "c"
