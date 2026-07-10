"""Pure window-math functions for incremental export.

No IO, no DuckDB, no config loading — every function is a pure computation
over its arguments. Side-effect-free: tests can call any function in isolation.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import IncrementalConfig

from fabulexa_forge.errors import (
    IncrementalAnchorRequired,
    IncrementalPeriodRegimeMismatch,
    IncrementalRangeInvalid,
)


@dataclass(frozen=True)
class Window:
    """One half-open export window in sim-time ns, with its display label.

    index is the 0-based cursor position, or None for an explicit
    --from/--to range (which has no cursor position).
    """

    index: int | None
    start_ns: int
    end_ns: int
    label: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _instant_to_ns_offset(instant: datetime, start_instant: datetime) -> int:
    """Convert a tz-aware datetime to a physical-ns offset from start_instant."""
    delta = instant - start_instant
    return int(delta.total_seconds() * 1_000_000_000)


def _localize_fold0(naive: datetime, tz: tzinfo) -> datetime:
    """Localize *naive* in *tz* with fold=0 (earliest valid instant).

    DST gaps: fold=0 shifts to the gap's end — the earliest valid instant.
    DST folds (ambiguous): fold=0 selects the earlier (pre-fold) instant.

    Args:
        naive: A naive (no tzinfo) datetime.
        tz: A ZoneInfo timezone object.

    Returns:
        A tz-aware datetime localized in tz.
    """
    return naive.replace(tzinfo=tz, fold=0)


def _next_day_boundary(dt: datetime, tz: tzinfo) -> datetime:
    """Return the midnight starting the *next* civil day after *dt* in *tz*."""
    # Move to the next calendar day at midnight
    naive_date = dt.astimezone(tz).date()
    next_date = naive_date + timedelta(days=1)
    naive_midnight = datetime(next_date.year, next_date.month, next_date.day)
    return _localize_fold0(naive_midnight, tz)


def _next_week_boundary(dt: datetime, tz: tzinfo) -> datetime:
    """Return the ISO-Monday midnight strictly after *dt* in *tz*."""
    local = dt.astimezone(tz)
    naive_date = local.date()
    # ISO weekday: Monday=1 … Sunday=7
    days_until_monday = (7 - naive_date.isoweekday()) % 7 + 1
    next_monday = naive_date + timedelta(days=days_until_monday)
    naive_midnight = datetime(next_monday.year, next_monday.month, next_monday.day)
    return _localize_fold0(naive_midnight, tz)


def _next_month_boundary(dt: datetime, tz: tzinfo) -> datetime:
    """Return the 1st-of-next-month midnight strictly after *dt* in *tz*."""
    local = dt.astimezone(tz)
    naive_date = local.date()
    # Advance to next month
    if naive_date.month == 12:
        next_year = naive_date.year + 1
        next_month = 1
    else:
        next_year = naive_date.year
        next_month = naive_date.month + 1
    naive_midnight = datetime(next_year, next_month, 1)
    return _localize_fold0(naive_midnight, tz)


def _first_boundary_after(start_instant: datetime, period: str, tz: tzinfo) -> datetime:
    """Return the first civil-period boundary strictly after *start_instant* in *tz*.

    Args:
        start_instant: The anchor's start_instant (tz-aware).
        period: One of "day", "week", "month".
        tz: The anchor's ZoneInfo timezone.

    Returns:
        The first boundary datetime (tz-aware, fold=0 resolved).
    """
    if period == "day":
        return _next_day_boundary(start_instant, tz)
    if period == "week":
        return _next_week_boundary(start_instant, tz)
    if period == "month":
        return _next_month_boundary(start_instant, tz)
    raise AssertionError(f"unreachable: period={period!r}")  # pragma: no cover


def _advance_one_period(boundary: datetime, period: str, tz: tzinfo) -> datetime:
    """Advance a civil-period boundary by exactly one period.

    The input *boundary* is the midnight of a period start. We advance by one
    civil period in the given timezone, using fold=0 for DST handling.

    Args:
        boundary: A civil-period boundary midnight (tz-aware).
        period: One of "day", "week", "month".
        tz: The ZoneInfo timezone.

    Returns:
        The next period's boundary midnight (tz-aware, fold=0).
    """
    local_naive = boundary.astimezone(tz).replace(tzinfo=None)
    if period == "day":
        next_naive = local_naive + timedelta(days=1)
    elif period == "week":
        next_naive = local_naive + timedelta(weeks=1)
    elif period == "month":
        year = local_naive.year
        month = local_naive.month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        # Clamp to last day of month (handles month-end drift)
        last_day = calendar.monthrange(year, month)[1]
        day = min(local_naive.day, last_day)
        next_naive = local_naive.replace(year=year, month=month, day=day)
    else:
        raise AssertionError(f"unreachable: period={period!r}")  # pragma: no cover
    return _localize_fold0(next_naive, tz)


def _compute_calendar_boundaries(
    anchor: "EffectiveAnchor",
    period: str,
    index: int,
) -> tuple[datetime, datetime]:
    """Return (B_k, B_{k+1}) — the physical instants bounding window *index*.

    B_0 = anchor.start_instant.
    B_1 = first civil-period boundary strictly after start_instant.
    B_{k+1} = B_k advanced by one period.

    Args:
        anchor: The resolved anchor.
        period: One of "day", "week", "month".
        index: 0-based window index.

    Returns:
        (start_instant, end_instant) as tz-aware datetimes.
    """
    tz = anchor.timezone
    b0 = anchor.start_instant
    b1 = _first_boundary_after(b0, period, tz)

    if index == 0:
        return b0, b1

    # B_{index} = b1 advanced by (index - 1) periods
    b_k = b1
    for _ in range(index - 1):
        b_k = _advance_one_period(b_k, period, tz)
    b_k1 = _advance_one_period(b_k, period, tz)
    return b_k, b_k1


def _civil_date_label(instant: datetime, tz: tzinfo) -> str:
    """Return the YYYY-MM-DD civil date of *instant* in *tz*."""
    return instant.astimezone(tz).strftime("%Y-%m-%d")


def _calendar_window_label(index: int, start_instant: datetime, tz: tzinfo) -> str:
    """Format a calendar-regime window label: w{index:05d}_{civil date}."""
    date_str = _civil_date_label(start_instant, tz)
    return f"w{index:05d}_{date_str}"


def _simtime_window_label(index: int, start_ns: int) -> str:
    """Format a sim-time-regime window label: w{index:05d}_ns{start_ns}."""
    return f"w{index:05d}_ns{start_ns}"


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def derive_window(
    index: int,
    incremental: "IncrementalConfig",
    anchor: "EffectiveAnchor | None",
) -> Window:
    """Compute the index-th window's sim-time bounds and label. Pure.

    Calendar regime: boundaries are civil period starts in anchor.timezone
    (day midnight / ISO Monday midnight / 1st-of-month midnight), each
    resolved to the earliest valid instant at or after the civil time
    (fold=0; a DST gap shifts to the gap's end); bounds are physical-ns
    offsets from anchor.start_instant. Window 0 runs from the anchor instant
    to the first boundary strictly after it. Sim-time regime:
    [index * P, (index + 1) * P). Labels per design doc § Window labels.

    Args:
        index: 0-based window index.
        incremental: The validated cadence block.
        anchor: The resolved anchor; None selects the sim-time regime.

    Returns:
        The Window with bounds and label.

    Raises:
        IncrementalAnchorRequired: `period` is set but anchor is None.
        IncrementalPeriodRegimeMismatch: `sim_period_ns` is set but an
            anchor resolved.
    """
    if incremental.period is not None:
        if anchor is None:
            raise IncrementalAnchorRequired(
                "incremental.period requires a resolved anchor"
                " (set rebase.base_date or rebase.timezone, or use sim_period_ns)"
            )
        period = incremental.period
        tz = anchor.timezone
        b_k, b_k1 = _compute_calendar_boundaries(anchor, period, index)
        start_ns = _instant_to_ns_offset(b_k, anchor.start_instant)
        end_ns = _instant_to_ns_offset(b_k1, anchor.start_instant)
        label = _calendar_window_label(index, b_k, tz)
        return Window(index=index, start_ns=start_ns, end_ns=end_ns, label=label)

    # sim_period_ns regime
    if anchor is not None:
        raise IncrementalPeriodRegimeMismatch(
            "incremental.sim_period_ns is set but an anchor resolves;"
            " wallclock runs use calendar periods (use incremental.period instead)"
        )
    p = incremental.sim_period_ns
    assert p is not None  # validated by IncrementalConfig.exactly_one_cadence
    start_ns = index * p
    end_ns = (index + 1) * p
    label = _simtime_window_label(index, start_ns)
    return Window(index=index, start_ns=start_ns, end_ns=end_ns, label=label)


# ---------------------------------------------------------------------------
# Range parsing helpers
# ---------------------------------------------------------------------------

_DATETIME_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _parse_naive_civil(raw: str) -> datetime:
    """Parse *raw* as a naive civil datetime (date = midnight).

    Args:
        raw: A string in ISO date or datetime format.

    Returns:
        A naive datetime.

    Raises:
        IncrementalRangeInvalid: The string does not match any supported format.
    """
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise IncrementalRangeInvalid(
        f"cannot parse {raw!r} as a civil date or datetime"
        " (expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
    )


def _localize_strict(naive: datetime, tz: tzinfo) -> datetime:
    """Localize *naive* in *tz*, rejecting DST gaps and folds (author input).

    Args:
        naive: A naive datetime to localize.
        tz: A ZoneInfo timezone.

    Returns:
        A tz-aware datetime.

    Raises:
        IncrementalRangeInvalid: The civil time is nonexistent (DST gap) or
            ambiguous (fold).
    """
    # Localize with fold=0
    aware_fold0 = naive.replace(tzinfo=tz, fold=0)
    # Localize with fold=1
    aware_fold1 = naive.replace(tzinfo=tz, fold=1)

    # Convert both to UTC to check for ambiguity / gap
    utc0 = aware_fold0.astimezone(timezone.utc)
    utc1 = aware_fold1.astimezone(timezone.utc)

    # Check if the naive time is in a DST gap:
    # A gap means fold=0 jumps forward — verify by round-tripping
    roundtrip = aware_fold0.astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:
        raise IncrementalRangeInvalid(
            f"{naive.isoformat()!r} does not exist in {tz!r} (DST gap)"
        )

    # Check if the naive time is in a DST fold (ambiguous):
    # fold=0 and fold=1 produce different UTC instants
    if utc0 != utc1:
        raise IncrementalRangeInvalid(
            f"{naive.isoformat()!r} is ambiguous in {tz!r} (DST fold)"
        )

    return aware_fold0


def _format_range_label_segment(raw: str) -> str:
    """Format one range label segment.

    Bare date → YYYY-MM-DD. Datetime → YYYY-MM-DDTHHMMSS (colon-free).

    Args:
        raw: The raw CLI value.

    Returns:
        Filesystem-safe label segment.
    """
    stripped = raw.strip()
    # If it parses as a bare date, keep YYYY-MM-DD
    try:
        datetime.strptime(stripped, "%Y-%m-%d")
        return stripped
    except ValueError:
        pass
    # Otherwise re-parse and format as colon-free datetime
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(stripped, fmt)
            return dt.strftime("%Y-%m-%dT%H%M%S")
        except ValueError:
            continue
    # Unreachable after _parse_naive_civil validation
    return stripped


def parse_range(
    raw_from: str,
    raw_to: str,
    anchor: "EffectiveAnchor | None",
) -> Window:
    """Parse --from/--to into an explicit Window (index None). Pure.

    Anchor regime: each value is a naive civil datetime (bare date =
    midnight) localized in anchor.timezone; DST gaps and folds are rejected
    (author input → fail-fast, matching base_date); each localized instant
    converts to a physical-ns offset from anchor.start_instant — the
    derive_window conversion. A pre-anchor bound yields a negative offset:
    legal, it selects nothing (sim time starts at 0). No-anchor regime: each
    value is an integer ns offset.

    Args:
        raw_from: Inclusive start, as typed on the CLI.
        raw_to: Exclusive end, as typed on the CLI.
        anchor: The resolved anchor; None selects the sim-time regime.

    Returns:
        A Window with index=None and the range label (design doc § Window
        labels: `r_{from}_{to}` calendar / `r_ns{start_ns}_ns{end_ns}` sim-time).

    Raises:
        IncrementalRangeInvalid: A value does not parse in the active
            regime, localization hits a DST gap/fold, or from >= to.
    """
    if anchor is not None:
        tz = anchor.timezone
        # Parse and strictly localize both bounds
        naive_from = _parse_naive_civil(raw_from)
        naive_to = _parse_naive_civil(raw_to)
        aware_from = _localize_strict(naive_from, tz)
        aware_to = _localize_strict(naive_to, tz)
        start_ns = _instant_to_ns_offset(aware_from, anchor.start_instant)
        end_ns = _instant_to_ns_offset(aware_to, anchor.start_instant)
        if start_ns >= end_ns:
            raise IncrementalRangeInvalid(
                f"--from {raw_from!r} must be strictly before --to {raw_to!r}"
            )
        from_seg = _format_range_label_segment(raw_from)
        to_seg = _format_range_label_segment(raw_to)
        label = f"r_{from_seg}_{to_seg}"
        return Window(index=None, start_ns=start_ns, end_ns=end_ns, label=label)

    # Sim-time (no anchor): each value is an integer ns offset
    try:
        start_ns = int(raw_from)
    except ValueError:
        raise IncrementalRangeInvalid(
            f"--from {raw_from!r}: expected an integer ns offset (no anchor resolves)"
        ) from None
    try:
        end_ns = int(raw_to)
    except ValueError:
        raise IncrementalRangeInvalid(
            f"--to {raw_to!r}: expected an integer ns offset (no anchor resolves)"
        ) from None
    if start_ns >= end_ns:
        raise IncrementalRangeInvalid(
            f"--from {raw_from} must be strictly before --to {raw_to}"
        )
    label = f"r_ns{start_ns}_ns{end_ns}"
    return Window(index=None, start_ns=start_ns, end_ns=end_ns, label=label)
