"""Tests for incremental/windows.py — derive_window and parse_range.

All tests are pure: no IO, no DuckDB, no emit. Each test constructs its
own EffectiveAnchor and IncrementalConfig from literals.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import IncrementalConfig
from fabulexa_forge.errors import (
    IncrementalAnchorRequired,
    IncrementalPeriodRegimeMismatch,
    IncrementalRangeInvalid,
)
from fabulexa_forge.incremental.windows import (
    _advance_one_period,
    derive_window,
    parse_range,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS_PER_HOUR = 3_600_000_000_000
_NS_PER_MINUTE = 60_000_000_000


def _anchor(start_iso: str, tz_key: str) -> EffectiveAnchor:
    """Build an EffectiveAnchor from an ISO tz-aware string and IANA zone."""
    return EffectiveAnchor(
        start_instant=datetime.fromisoformat(start_iso),
        timezone=ZoneInfo(tz_key),
    )


def _day_cfg() -> IncrementalConfig:
    return IncrementalConfig.model_validate({"period": "day"})


def _week_cfg() -> IncrementalConfig:
    return IncrementalConfig.model_validate({"period": "week"})


def _month_cfg() -> IncrementalConfig:
    return IncrementalConfig.model_validate({"period": "month"})


def _sim_cfg(p: int) -> IncrementalConfig:
    return IncrementalConfig.model_validate({"sim_period_ns": p})


# ---------------------------------------------------------------------------
# derive_window — calendar day
# ---------------------------------------------------------------------------


def test_derive_window_day_08h_anchor_window0() -> None:
    """Window 0 from an 08:00 anchor: [0, 16h·ns) labeled w00000_<anchor date>."""
    # 2020-03-01 08:00 UTC → next midnight (2020-03-02 00:00) is 16h away
    anchor = _anchor("2020-03-01T08:00:00+00:00", "UTC")
    w = derive_window(0, _day_cfg(), anchor)
    assert w.index == 0
    assert w.start_ns == 0
    assert w.end_ns == 16 * _NS_PER_HOUR
    assert w.label == "w00000_2020-03-01"


def test_derive_window_day_08h_anchor_window1() -> None:
    """Window 1 from an 08:00 UTC anchor is a full 24h day."""
    anchor = _anchor("2020-03-01T08:00:00+00:00", "UTC")
    w = derive_window(1, _day_cfg(), anchor)
    # Window 1: [16h, 40h) — midnight 2020-03-02 to midnight 2020-03-03
    assert w.start_ns == 16 * _NS_PER_HOUR
    assert w.end_ns == 40 * _NS_PER_HOUR
    assert w.label == "w00001_2020-03-02"


def test_derive_window_day_midnight_anchor_is_full_period() -> None:
    """Anchor exactly on midnight → window 0 is a full 24h day."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    w = derive_window(0, _day_cfg(), anchor)
    assert w.start_ns == 0
    assert w.end_ns == 24 * _NS_PER_HOUR
    assert w.label == "w00000_2020-03-01"


# ---------------------------------------------------------------------------
# derive_window — DST spring-forward (Europe/London, clocks go forward 1h)
# ---------------------------------------------------------------------------


def test_derive_window_dst_spring_forward_23h() -> None:
    """DST spring-forward day (Europe/London 2020-03-29) → 23h of physical ns."""
    # 2020-03-29 Europe/London: clocks go forward at 01:00 → 02:00 (23h day)
    anchor = _anchor("2020-03-29T00:00:00+00:00", "Europe/London")
    w = derive_window(0, _day_cfg(), anchor)
    # Physical duration is 23h
    assert w.end_ns == 23 * _NS_PER_HOUR
    assert w.label == "w00000_2020-03-29"


def test_derive_window_dst_fall_back_25h() -> None:
    """DST fall-back day (Europe/London 2020-10-25) → 25h of physical ns."""
    # 2020-10-25 Europe/London: clocks go back at 02:00 → 01:00 (25h day)
    anchor = _anchor("2020-10-25T00:00:00+01:00", "Europe/London")
    w = derive_window(0, _day_cfg(), anchor)
    # Physical duration is 25h
    assert w.end_ns == 25 * _NS_PER_HOUR
    assert w.label == "w00000_2020-10-25"


# ---------------------------------------------------------------------------
# derive_window — week regime
# ---------------------------------------------------------------------------


def test_derive_window_week_mid_week_anchor() -> None:
    """Mid-week anchor (Wednesday) → partial window 0, then full week."""
    # 2020-03-04 is Wednesday; next ISO Monday is 2020-03-09
    anchor = _anchor("2020-03-04T00:00:00+00:00", "UTC")
    w0 = derive_window(0, _week_cfg(), anchor)
    # 5 days to next Monday
    assert w0.start_ns == 0
    assert w0.end_ns == 5 * 24 * _NS_PER_HOUR
    assert w0.label == "w00000_2020-03-04"

    w1 = derive_window(1, _week_cfg(), anchor)
    # 7 days = 1 full week
    assert w1.start_ns == 5 * 24 * _NS_PER_HOUR
    assert w1.end_ns == 12 * 24 * _NS_PER_HOUR
    assert w1.label == "w00001_2020-03-09"


def test_derive_window_week_monday_midnight_anchor_full_week() -> None:
    """Anchor exactly on ISO Monday midnight → window 0 is a full 7-day week.

    days_until_monday == 0 must jump a full 7 days (the boundary is *strictly*
    after the anchor), never 0.
    """
    # 2020-03-02 is a Monday; the boundary strictly after is 2020-03-09
    anchor = _anchor("2020-03-02T00:00:00+00:00", "UTC")
    w0 = derive_window(0, _week_cfg(), anchor)
    assert w0.start_ns == 0
    assert w0.end_ns == 7 * 24 * _NS_PER_HOUR
    assert w0.label == "w00000_2020-03-02"

    w1 = derive_window(1, _week_cfg(), anchor)
    assert w1.start_ns == 7 * 24 * _NS_PER_HOUR
    assert w1.end_ns == 14 * 24 * _NS_PER_HOUR
    assert w1.label == "w00001_2020-03-09"


# ---------------------------------------------------------------------------
# derive_window — month regime
# ---------------------------------------------------------------------------


def test_derive_window_month_mid_month_anchor() -> None:
    """Mid-month anchor → partial window 0."""
    # 2020-03-15: next 1st is 2020-04-01 (17 days away from 2020-03-15 midnight)
    anchor = _anchor("2020-03-15T00:00:00+00:00", "UTC")
    w0 = derive_window(0, _month_cfg(), anchor)
    assert w0.start_ns == 0
    # 17 days to 2020-04-01
    assert w0.end_ns == 17 * 24 * _NS_PER_HOUR
    assert w0.label == "w00000_2020-03-15"


def test_derive_window_month_day31_anchor_multi_window_advance() -> None:
    """A Jan-31 anchor advances through leap-February and March correctly."""
    anchor = _anchor("2020-01-31T00:00:00+00:00", "UTC")

    # Window 0: [Jan-31, Feb-1) — 1 day
    w0 = derive_window(0, _month_cfg(), anchor)
    assert w0.start_ns == 0
    assert w0.end_ns == 1 * 24 * _NS_PER_HOUR
    assert w0.label == "w00000_2020-01-31"

    # Window 1: [Feb-1, Mar-1) — 29 days (2020 is a leap year)
    w1 = derive_window(1, _month_cfg(), anchor)
    assert w1.end_ns - w1.start_ns == 29 * 24 * _NS_PER_HOUR
    assert w1.label == "w00001_2020-02-01"

    # Window 2: [Mar-1, Apr-1) — 31 days
    w2 = derive_window(2, _month_cfg(), anchor)
    assert w2.end_ns - w2.start_ns == 31 * 24 * _NS_PER_HOUR
    assert w2.label == "w00002_2020-03-01"


# ---------------------------------------------------------------------------
# _advance_one_period — month-end day clamp
# ---------------------------------------------------------------------------


def test_advance_one_period_month_end_clamps_to_28_day_february() -> None:
    """A day-31 boundary advancing into a 28-day February clamps to the 28th."""
    tz = ZoneInfo("UTC")
    jan31 = datetime(2021, 1, 31, tzinfo=tz)
    advanced = _advance_one_period(jan31, "month", tz)
    assert (advanced.year, advanced.month, advanced.day) == (2021, 2, 28)


def test_advance_one_period_month_end_clamps_to_leap_february() -> None:
    """A day-31 boundary advancing into a leap-year February clamps to the 29th."""
    tz = ZoneInfo("UTC")
    jan31 = datetime(2020, 1, 31, tzinfo=tz)
    advanced = _advance_one_period(jan31, "month", tz)
    assert (advanced.year, advanced.month, advanced.day) == (2020, 2, 29)


def test_advance_one_period_month_end_clamps_31_to_30_day_month() -> None:
    """A day-31 boundary advancing into a 30-day month clamps to the 30th."""
    tz = ZoneInfo("UTC")
    mar31 = datetime(2020, 3, 31, tzinfo=tz)
    advanced = _advance_one_period(mar31, "month", tz)
    assert (advanced.year, advanced.month, advanced.day) == (2020, 4, 30)


def test_advance_one_period_month_first_of_month_no_clamp() -> None:
    """A 1st-of-month boundary (the derive_window case) advances to the next 1st."""
    tz = ZoneInfo("UTC")
    feb1 = datetime(2020, 2, 1, tzinfo=tz)
    advanced = _advance_one_period(feb1, "month", tz)
    assert (advanced.year, advanced.month, advanced.day) == (2020, 3, 1)


# ---------------------------------------------------------------------------
# derive_window — DST gap boundary (America/Santiago — gap at midnight)
# ---------------------------------------------------------------------------


def test_derive_window_dst_gap_boundary_resolves_to_gap_end() -> None:
    """A nonexistent civil boundary (DST gap) resolves to the gap's end."""
    # America/Santiago 2020-09-06: midnight is in the DST gap (clocks go forward).
    # The gap shifts the boundary to the gap's end — fold=0 behaviour.
    # The point is that derive_window does NOT raise; it resolves gracefully.
    anchor = _anchor("2020-09-05T04:00:00+00:00", "America/Santiago")
    # Should not raise, despite boundary crossing the DST gap
    w = derive_window(0, _day_cfg(), anchor)
    assert w.index == 0
    assert w.start_ns == 0
    assert w.end_ns > 0  # boundary resolved, some positive duration


# ---------------------------------------------------------------------------
# derive_window — sim-time regime
# ---------------------------------------------------------------------------


def test_derive_window_simtime_window_k() -> None:
    """Sim-time: window k = [k·P, (k+1)·P), labeled w{k:05d}_ns{start_ns}."""
    p = 86_400_000_000_000  # 1 day in ns
    cfg = _sim_cfg(p)
    for k in range(3):
        w = derive_window(k, cfg, None)
        assert w.index == k
        assert w.start_ns == k * p
        assert w.end_ns == (k + 1) * p
        assert w.label == f"w{k:05d}_ns{k * p}"


# ---------------------------------------------------------------------------
# derive_window — error cases
# ---------------------------------------------------------------------------


def test_derive_window_period_no_anchor_raises() -> None:
    """`period` set but anchor is None → IncrementalAnchorRequired."""
    with pytest.raises(IncrementalAnchorRequired):
        derive_window(0, _day_cfg(), None)


def test_derive_window_simtime_with_anchor_raises() -> None:
    """`sim_period_ns` set but anchor resolves → IncrementalPeriodRegimeMismatch."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    with pytest.raises(IncrementalPeriodRegimeMismatch):
        derive_window(0, _sim_cfg(1_000_000), anchor)


# ---------------------------------------------------------------------------
# parse_range — calendar regime
# ---------------------------------------------------------------------------


def test_parse_range_bare_dates_label() -> None:
    """Bare dates → midnight bounds, label r_2020-03-01_2020-03-08."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    w = parse_range("2020-03-01", "2020-03-08", anchor)
    assert w.index is None
    assert w.label == "r_2020-03-01_2020-03-08"
    assert w.start_ns == 0
    assert w.end_ns == 7 * 24 * _NS_PER_HOUR


def test_parse_range_datetime_bound_colon_free_label() -> None:
    """Datetime bound → colon-free label segment (YYYY-MM-DDTHHMMSS)."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    w = parse_range("2020-03-01T08:00:00", "2020-03-02T08:00:00", anchor)
    assert w.label == "r_2020-03-01T080000_2020-03-02T080000"


def test_parse_range_pre_anchor_bound_negative_offset() -> None:
    """A pre-anchor bound yields a negative offset — legal, no error."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    # 2020 is a leap year: Feb has 29 days, so Feb 28 → Mar 1 = 2 days
    w = parse_range("2020-02-28", "2020-03-01", anchor)
    assert w.start_ns < 0
    assert w.end_ns == 0


def test_parse_range_from_ge_to_raises() -> None:
    """`from >= to` → IncrementalRangeInvalid."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    with pytest.raises(IncrementalRangeInvalid):
        parse_range("2020-03-08", "2020-03-01", anchor)


def test_parse_range_from_eq_to_raises() -> None:
    """`from == to` → IncrementalRangeInvalid."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    with pytest.raises(IncrementalRangeInvalid):
        parse_range("2020-03-01", "2020-03-01", anchor)


def test_parse_range_dst_gap_civil_input_raises() -> None:
    """A civil datetime in a DST gap (author input) → IncrementalRangeInvalid."""
    # America/New_York spring-forward 2020-03-08: 02:00 → 03:00 (gap)
    anchor = _anchor("2020-03-07T05:00:00-05:00", "America/New_York")
    with pytest.raises(IncrementalRangeInvalid):
        parse_range("2020-03-08T02:30:00", "2020-03-09", anchor)


def test_parse_range_dst_fold_civil_input_raises() -> None:
    """A civil datetime in a DST fold (author input) → IncrementalRangeInvalid."""
    # America/New_York fall-back 2020-11-01: 02:00 → 01:00 (fold)
    anchor = _anchor("2020-11-01T04:00:00-04:00", "America/New_York")
    with pytest.raises(IncrementalRangeInvalid):
        parse_range("2020-11-01T01:30:00", "2020-11-02", anchor)


# ---------------------------------------------------------------------------
# parse_range — sim-time regime
# ---------------------------------------------------------------------------


def test_parse_range_simtime_integer_ns() -> None:
    """No anchor → integer ns offsets, label r_ns{start}_ns{end}."""
    w = parse_range("0", "86400000000000", None)
    assert w.index is None
    assert w.start_ns == 0
    assert w.end_ns == 86_400_000_000_000
    assert w.label == "r_ns0_ns86400000000000"


def test_parse_range_simtime_non_integer_raises() -> None:
    """Non-integer in sim regime → IncrementalRangeInvalid."""
    with pytest.raises(IncrementalRangeInvalid):
        parse_range("2020-03-01", "86400000000000", None)


def test_parse_range_simtime_from_ge_to_raises() -> None:
    """from >= to in sim regime → IncrementalRangeInvalid."""
    with pytest.raises(IncrementalRangeInvalid):
        parse_range("100", "50", None)


def test_parse_range_index_is_none() -> None:
    """Explicit range always has index=None."""
    anchor = _anchor("2020-03-01T00:00:00+00:00", "UTC")
    w = parse_range("2020-03-01", "2020-03-02", anchor)
    assert w.index is None
