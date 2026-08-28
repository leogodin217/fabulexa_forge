"""Tests for pin_session_timezone.

Verifies: pins the connection's session TimeZone to the anchor's IANA zone,
visible on both reader query surfaces (query and query_arrow); is a pure
function of the anchor (idempotent, independent of the host process's TZ).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.reader.emit import open_emit, pin_session_timezone

from ._emit_helpers import write_emit


def _anchor(zone_key: str) -> EffectiveAnchor:
    """Build an EffectiveAnchor pinned to the given IANA zone for test use."""
    return EffectiveAnchor(
        start_instant=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        timezone=ZoneInfo(zone_key),
    )


def test_pin_session_timezone_sets_current_setting(tmp_path: Path) -> None:
    """After pinning, current_setting('TimeZone') reflects the anchor zone."""
    emit_dir = write_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        pin_session_timezone(emit, _anchor("America/New_York"))
        rows = emit.query("SELECT current_setting('TimeZone')", ())
    assert rows[0][0] == "America/New_York"


def test_pin_session_timezone_covers_query_arrow_surface(tmp_path: Path) -> None:
    """The pin covers query_arrow, not just the row-tuple query surface."""
    emit_dir = write_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        pin_session_timezone(emit, _anchor("Asia/Tokyo"))
        table = emit.query_arrow("SELECT current_setting('TimeZone') AS tz", ())
    assert table.column("tz")[0].as_py() == "Asia/Tokyo"


def test_pin_session_timezone_is_pure_function_of_anchor(tmp_path: Path) -> None:
    """Pinning the same anchor twice leaves the session state unchanged."""
    emit_dir = write_emit(tmp_path)
    anchor = _anchor("Europe/London")
    with open_emit(emit_dir) as emit:
        pin_session_timezone(emit, anchor)
        pin_session_timezone(emit, anchor)
        rows = emit.query("SELECT current_setting('TimeZone')", ())
    assert rows[0][0] == "Europe/London"


def test_pin_session_timezone_independent_of_process_tz(tmp_path: Path) -> None:
    """A different process TZ env var does not change the pinned session zone."""
    emit_dir = write_emit(tmp_path)
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Auckland"
    try:
        with open_emit(emit_dir) as emit:
            pin_session_timezone(emit, _anchor("Europe/London"))
            rows = emit.query("SELECT current_setting('TimeZone')", ())
    finally:
        if previous_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous_tz
    assert rows[0][0] == "Europe/London"
