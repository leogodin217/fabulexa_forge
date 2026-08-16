#!/usr/bin/env python
"""
Demo: Temporal renderer generalization (`render_anchor_temporal_expr`)
Sprint: temporal-elections
Phase: 1

`render_anchor_temporal_expr` generalizes the single-rendering predecessor
into the four-member election family (`timestamp` / `date` / `time` /
`timestamptz`) sharing one pinned zone/origin interpolation. No config
surface exists yet (Phase 3) — this phase proves the renderer itself.

Shows:
  1. The four elections' SQL fragments for one anchor.
  2. `timestamp` reproduces the pre-sprint expression byte-for-byte.
  3. Family identity, executed against an in-memory DuckDB: `date` is the
     naive timestamp's date part, `time` its time-of-day, `timestamptz` the
     same absolute instant — in the anchor zone.
  4. `anchor=None` + `render="timestamp"`: the raw source column passes
     through unchanged.
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge.anchor import (
    EffectiveAnchor,
    TemporalRender,
    render_anchor_temporal_expr,
)


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _strip_alias(expr: str, out_name: str) -> str:
    """Drop the trailing `AS "<out_name>"` from a rendered SQL fragment."""
    return expr.removesuffix(f' AS "{out_name}"')


def main() -> int:
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-06-01T12:00:00-04:00"),
        timezone=ZoneInfo("America/New_York"),
    )
    qualified_source = '"_grain"."created_sim_time"'

    print(f"Anchor: {anchor.start_instant.isoformat()} ({anchor.timezone})")
    print()
    print("1. The four elections' SQL fragments:")
    renders: tuple[TemporalRender, ...] = ("timestamp", "date", "time", "timestamptz")
    fragments: dict[TemporalRender, str] = {}
    for render in renders:
        expr = render_anchor_temporal_expr(anchor, qualified_source, "v", render)
        fragments[render] = expr
        print(f"  {render:>11}: {expr}")
    print()

    print("2. 'timestamp' reproduces the pre-sprint expression byte-for-byte:")
    zone = str(anchor.timezone)
    origin = anchor.start_instant.isoformat()
    predecessor_expr = (
        f"timezone('{zone}', TIMESTAMPTZ '{origin}'"
        f" + to_microseconds(CAST({qualified_source} AS BIGINT) // 1000))"
        f' AS "v"'
    )
    if fragments["timestamp"] != predecessor_expr:
        raise _fail(
            "the 'timestamp' election drifted from the pre-sprint expression:"
            f" {fragments['timestamp']!r} != {predecessor_expr!r}"
        )
    print("  OK: byte-identical")
    print()

    print("3. Family identity (a concrete +3h instant, executed via DuckDB):")
    con = duckdb.connect()
    ns = 3 * 3_600 * 1_000_000_000  # +3 hours from the anchor origin
    ts = _strip_alias(
        render_anchor_temporal_expr(anchor, str(ns), "v", "timestamp"), "v"
    )
    date_ = _strip_alias(render_anchor_temporal_expr(anchor, str(ns), "v", "date"), "v")
    time_ = _strip_alias(render_anchor_temporal_expr(anchor, str(ns), "v", "time"), "v")
    tz_ = _strip_alias(
        render_anchor_temporal_expr(anchor, str(ns), "v", "timestamptz"), "v"
    )
    row = con.sql(
        f"SELECT ({ts}) AS ts, ({date_}) AS d, ({time_}) AS t,"
        f" typeof({tz_}) AS tz_type,"
        f" ({date_}) = CAST(({ts}) AS DATE) AS date_matches,"
        f" ({time_}) = CAST(({ts}) AS TIME) AS time_matches,"
        f" ({tz_}) = TIMESTAMPTZ '2024-06-01T15:00:00-04:00' AS instant_matches"
    ).fetchone()
    if row is None:
        raise _fail("query returned no row")
    ts_val, date_val, time_val, tz_type, date_matches, time_matches, instant_matches = (
        row
    )
    print(f"  timestamp:   {ts_val}")
    print(f"  date:        {date_val}")
    print(f"  time:        {time_val}")
    print(f"  timestamptz: type={tz_type}")
    if not (date_matches and time_matches and instant_matches):
        raise _fail(
            "family identity broken:"
            f" date_matches={date_matches} time_matches={time_matches}"
            f" instant_matches={instant_matches}"
        )
    print(
        "  OK: date == timestamp's date part, time == timestamp's time-of-day,"
        " timestamptz == the same absolute instant"
    )
    print()

    print("4. anchor=None + render='timestamp': raw source passes through:")
    no_anchor_expr = render_anchor_temporal_expr(
        None, qualified_source, "created_at", "timestamp"
    )
    expected_no_anchor = f'{qualified_source} AS "created_at"'
    if no_anchor_expr != expected_no_anchor:
        raise _fail(f"expected {expected_no_anchor!r}, got {no_anchor_expr!r}")
    print(f"  {no_anchor_expr}")
    print("  OK: unchanged passthrough")
    print()

    print(
        "SUCCESS: render_anchor_temporal_expr generalizes the shared wallclock"
        " renderer to the four-election family; 'timestamp' is byte-identical"
        " to the pre-sprint expression, and date/time/timestamptz share one"
        " consistent local-instant family"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
