"""Effective anchor resolution for timestamp rebasing.

The single authority that resolves the wallclock anchor for an export invocation.
All modes read through the one EffectiveAnchor produced here; no mode resolves
its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fabulexa_export.errors import (
    RebaseDateNotNaive,
    RebaseDateUnresolvable,
    RebaseInvalidRuntimeAnchor,
    RebaseOriginUnresolvable,
    RebaseTimezoneUnresolvable,
    RebaseUnknownTimezone,
)

if TYPE_CHECKING:
    from fabulexa_export.config.models import RebaseConfig
    from fabulexa_export.reader.sidecar import RuntimeAnchor


@dataclass(frozen=True)
class EffectiveAnchor:
    """The resolved wallclock anchor a single export invocation renders through.

    start_instant is the tz-aware datetime that sim_time = 0 maps to; timezone
    localizes it and governs the zone of every rendered timestamp.
    """

    start_instant: datetime  # tz-aware
    timezone: ZoneInfo


def _resolve_zone(zone_str: str) -> ZoneInfo:
    """Parse an IANA zone string into a ZoneInfo.

    Args:
        zone_str: An IANA timezone string.

    Returns:
        The corresponding ZoneInfo.

    Raises:
        RebaseUnknownTimezone: The zone string is not a known IANA zone.
    """
    try:
        return ZoneInfo(zone_str)
    except (ZoneInfoNotFoundError, KeyError):
        raise RebaseUnknownTimezone(f"unknown IANA timezone: {zone_str!r}") from None


def _parse_sidecar_instant(start_datetime: str) -> datetime:
    """Parse the sidecar's raw start_datetime string to a tz-aware datetime.

    Args:
        start_datetime: Raw ISO-8601 string from the sidecar.

    Returns:
        A tz-aware datetime.

    Raises:
        RebaseInvalidRuntimeAnchor: The string is not a parseable ISO-8601
            tz-aware datetime.
    """
    try:
        dt = datetime.fromisoformat(start_datetime)
    except (ValueError, TypeError):
        raise RebaseInvalidRuntimeAnchor(
            f"sidecar start_datetime is not a parseable ISO-8601 datetime:"
            f" {start_datetime!r}"
        ) from None
    if dt.tzinfo is None:
        raise RebaseInvalidRuntimeAnchor(
            f"sidecar start_datetime is naive (no timezone): {start_datetime!r}"
        )
    return dt


def _localize(base_date: datetime, zone: ZoneInfo) -> datetime:
    """Localize a naive datetime into a zone, rejecting DST gaps and folds.

    Args:
        base_date: A naive datetime.
        zone: The target zone.

    Returns:
        A tz-aware datetime in the given zone.

    Raises:
        RebaseDateUnresolvable: The datetime is nonexistent (DST gap) or
            ambiguous (DST fold).
    """
    # fold=0 for the first occurrence, fold=1 for the second (DST fold)
    candidate = base_date.replace(tzinfo=zone)
    # Normalize through UTC and back to detect gaps: a gap produces a different
    # local time when converted back.
    utc_candidate = candidate.astimezone(timezone.utc)
    back = utc_candidate.astimezone(zone)
    # Strip tzinfo for wall-clock comparison
    if back.replace(tzinfo=None) != base_date:
        raise RebaseDateUnresolvable(
            f"base_date {base_date.isoformat()!r} is nonexistent (DST gap)"
            f" in zone {str(zone)!r}"
        )
    # Detect DST fold: fold=1 gives a different UTC offset than fold=0
    fold0 = base_date.replace(tzinfo=zone, fold=0)
    fold1 = base_date.replace(tzinfo=zone, fold=1)
    if fold0.utcoffset() != fold1.utcoffset():
        raise RebaseDateUnresolvable(
            f"base_date {base_date.isoformat()!r} is ambiguous (DST fold)"
            f" in zone {str(zone)!r}"
        )
    return candidate


def resolve_effective_anchor(
    sidecar_runtime: "RuntimeAnchor | None",
    config_rebase: "RebaseConfig | None",
    cli_base_date: datetime | None,
    cli_timezone: str | None,
) -> EffectiveAnchor | None:
    """Resolve the one effective wallclock anchor for an export invocation.

    Applies CLI-wins precedence to each of base_date and timezone independently,
    then resolves against the sidecar anchor. The single authority that parses
    the sidecar's raw `timezone` / `start_datetime` strings.

    Args:
        sidecar_runtime: The reader's typed `runtime` block, or None when the
            emit's scenario declared no `runtime:` block.
        config_rebase: The export config's `rebase` block, or None when absent.
        cli_base_date: `--base-date` parsed to a datetime, or None when unset.
        cli_timezone: `--timezone` IANA string, or None when unset.

    Returns:
        An EffectiveAnchor when an origin and zone resolve, or None when no
        anchor is determinable (no sidecar runtime and no rebase input) — the
        caller then renders raw sim_time integers.

    Raises:
        RebaseTimezoneUnresolvable: A base_date resolves but no timezone does
            (from CLI, config, or the sidecar).
        RebaseOriginUnresolvable: A timezone override resolves but there is no
            origin to apply it to (no base_date and no sidecar anchor).
        RebaseDateNotNaive: The winning base_date carries tzinfo/offset.
        RebaseDateUnresolvable: localize(base_date, timezone) is nonexistent (DST
            gap) or ambiguous (DST fold).
        RebaseUnknownTimezone: A supplied IANA zone string is not a known zone.
        RebaseInvalidRuntimeAnchor: The sidecar `start_datetime` is needed but is
            not a parseable ISO-8601 tz-aware datetime.
    """
    # No inputs at all → no anchor
    if (
        sidecar_runtime is None
        and config_rebase is None
        and cli_base_date is None
        and cli_timezone is None
    ):
        return None

    # Resolve the winning base_date (CLI wins over config)
    winning_base_date: datetime | None = cli_base_date
    if winning_base_date is None and config_rebase is not None:
        winning_base_date = config_rebase.base_date

    # Resolve the winning timezone string (CLI wins over config, config over sidecar)
    winning_tz_str: str | None = cli_timezone
    if winning_tz_str is None and config_rebase is not None:
        winning_tz_str = config_rebase.timezone
    if winning_tz_str is None and sidecar_runtime is not None:
        winning_tz_str = sidecar_runtime.timezone

    # Validate base_date is naive
    if winning_base_date is not None and winning_base_date.tzinfo is not None:
        raise RebaseDateNotNaive(
            "base_date must be naive (no timezone info);"
            f" got {winning_base_date.isoformat()!r}"
        )

    # Case: no base_date override — use sidecar origin or detect conflicts
    if winning_base_date is None:
        if sidecar_runtime is None:
            # No origin anywhere — a timezone override without origin is an error.
            # (Both winning_base_date and sidecar_runtime are None, so only
            # winning_tz_str can be non-None; all-None inputs are handled above.)
            raise RebaseOriginUnresolvable(
                "a timezone override was supplied but there is no origin"
                " (no base_date and no sidecar anchor)"
            )

        # Sidecar present — parse its instant
        sidecar_instant = _parse_sidecar_instant(sidecar_runtime.start_datetime)

        # Re-zone only (or identity when winning_tz_str is the sidecar's own zone):
        # winning_tz_str is guaranteed non-None here: line 178 assigns
        # sidecar_runtime.timezone (typed str) when winning_tz_str was still None.
        assert winning_tz_str is not None
        zone = _resolve_zone(winning_tz_str)
        rebased_instant = sidecar_instant.astimezone(zone)
        return EffectiveAnchor(start_instant=rebased_instant, timezone=zone)

    # Case: base_date is set — need a zone
    if winning_tz_str is None:
        raise RebaseTimezoneUnresolvable(
            "base_date resolves but no timezone is determinable"
            " (set rebase.timezone, --timezone, or add a sidecar runtime)"
        )

    zone = _resolve_zone(winning_tz_str)
    start_instant = _localize(winning_base_date, zone)
    return EffectiveAnchor(start_instant=start_instant, timezone=zone)


def render_anchor_timestamp_expr(
    anchor: EffectiveAnchor | None,
    qualified_source: str,
    out_name: str,
) -> str:
    """Render the SQL SELECT fragment for a wallclock TIMESTAMP derived from a
    nanosecond sim_time column through the effective anchor.

    When `anchor` is None, returns the raw sim_time column aliased to out_name
    (no conversion). When present, returns the pinned projection that fixes the
    absolute origin, adds physical elapsed microseconds, and projects to the
    local wall clock in the effective zone with DST resolved by DuckDB's bundled
    tz database. The two interpolations are pinned (design doc § Serialization):
    the zone is `str(anchor.timezone)` (the IANA key) and the origin literal is
    `anchor.start_instant.isoformat()`.

    Args:
        anchor: The resolved EffectiveAnchor, or None for the no-anchor path.
        qualified_source: The fully table-qualified BIGINT-ns source column SQL
            (e.g. `"_grain"."sim_time"` or `"_versions"."version_start"`).
        out_name: The output column name (the `AS "<out_name>"` alias).

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """
    if anchor is None:
        return f'{qualified_source} AS "{out_name}"'

    zone = str(anchor.timezone)
    origin = anchor.start_instant.isoformat()
    return (
        f"timezone('{zone}', TIMESTAMPTZ '{origin}'"
        f" + to_microseconds(CAST({qualified_source} AS BIGINT) // 1000))"
        f' AS "{out_name}"'
    )
