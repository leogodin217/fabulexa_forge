"""Tests for EffectiveAnchor resolution in fabulexa_export.anchor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fabulexa_export.anchor import resolve_effective_anchor
from fabulexa_export.config.models import RebaseConfig
from fabulexa_export.errors import (
    RebaseDateNotNaive,
    RebaseDateUnresolvable,
    RebaseInvalidRuntimeAnchor,
    RebaseOriginUnresolvable,
    RebaseTimezoneUnresolvable,
    RebaseUnknownTimezone,
)
from fabulexa_export.reader.sidecar import RuntimeAnchor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC_RUNTIME = RuntimeAnchor(
    timezone="UTC",
    start_datetime="2020-03-01T00:00:00+00:00",
)

NY_RUNTIME = RuntimeAnchor(
    timezone="America/New_York",
    start_datetime="2020-03-01T05:00:00-05:00",
)


def _rebase(
    *,
    base_date: datetime | None = None,
    timezone_str: str | None = None,
) -> RebaseConfig:
    """Build a RebaseConfig with the given knobs."""
    return RebaseConfig.model_validate(
        {
            k: v
            for k, v in [
                ("base_date", base_date.isoformat() if base_date else None),
                ("timezone", timezone_str),
            ]
            if v is not None
        }
    )


# ---------------------------------------------------------------------------
# Identity: sidecar present, no rebase, no CLI
# ---------------------------------------------------------------------------


def test_identity_sidecar_utc() -> None:
    """Sidecar present, no rebase, no CLI → anchor from sidecar (UTC)."""
    anchor = resolve_effective_anchor(UTC_RUNTIME, None, None, None)
    assert anchor is not None
    expected_dt = datetime.fromisoformat("2020-03-01T00:00:00+00:00")
    assert anchor.start_instant == expected_dt
    assert anchor.timezone == ZoneInfo("UTC")


def test_identity_sidecar_new_york() -> None:
    """Sidecar with America/New_York → timezone parsed correctly."""
    anchor = resolve_effective_anchor(NY_RUNTIME, None, None, None)
    assert anchor is not None
    assert anchor.timezone == ZoneInfo("America/New_York")
    assert anchor.start_instant == datetime.fromisoformat("2020-03-01T05:00:00-05:00")


# ---------------------------------------------------------------------------
# Rebase-only: base_date set, no zone override → sidecar zone kept
# ---------------------------------------------------------------------------


def test_rebase_base_date_only_uses_sidecar_zone() -> None:
    """base_date set, no zone → sidecar zone kept."""
    new_date = datetime(2026, 1, 1, 0, 0, 0)
    rebase = _rebase(base_date=new_date)
    anchor = resolve_effective_anchor(UTC_RUNTIME, rebase, None, None)
    assert anchor is not None
    assert anchor.timezone == ZoneInfo("UTC")
    # The new origin is 2026-01-01 in UTC
    assert anchor.start_instant == new_date.replace(tzinfo=ZoneInfo("UTC"))


# ---------------------------------------------------------------------------
# Re-zone only: timezone set, no base_date → astimezone of sidecar instant
# ---------------------------------------------------------------------------


def test_rezone_only_no_base_date() -> None:
    """timezone set, no base_date → astimezone of sidecar instant."""
    rebase = _rebase(timezone_str="America/Chicago")
    anchor = resolve_effective_anchor(UTC_RUNTIME, rebase, None, None)
    assert anchor is not None
    assert anchor.timezone == ZoneInfo("America/Chicago")
    expected = datetime.fromisoformat("2020-03-01T00:00:00+00:00").astimezone(
        ZoneInfo("America/Chicago")
    )
    assert anchor.start_instant == expected


# ---------------------------------------------------------------------------
# Both set: localize(base_date, timezone)
# ---------------------------------------------------------------------------


def test_both_set_localizes() -> None:
    """Both base_date and timezone set → localized origin."""
    new_date = datetime(2026, 6, 1, 9, 0, 0)
    rebase = _rebase(base_date=new_date, timezone_str="America/New_York")
    anchor = resolve_effective_anchor(None, rebase, None, None)
    assert anchor is not None
    assert anchor.timezone == ZoneInfo("America/New_York")
    assert anchor.start_instant == new_date.replace(tzinfo=ZoneInfo("America/New_York"))


# ---------------------------------------------------------------------------
# No sidecar, no rebase, no CLI → None
# ---------------------------------------------------------------------------


def test_no_inputs_returns_none() -> None:
    """No sidecar runtime, no rebase, no CLI → None."""
    assert resolve_effective_anchor(None, None, None, None) is None


# ---------------------------------------------------------------------------
# Precedence tests
# ---------------------------------------------------------------------------


def test_cli_base_date_beats_rebase_base_date() -> None:
    """--base-date beats rebase.base_date."""
    config_date = datetime(2020, 1, 1)
    cli_date = datetime(2025, 6, 1)
    rebase = _rebase(base_date=config_date)
    anchor = resolve_effective_anchor(UTC_RUNTIME, rebase, cli_date, None)
    assert anchor is not None
    assert anchor.start_instant.year == 2025


def test_cli_timezone_beats_rebase_timezone() -> None:
    """--timezone beats rebase.timezone."""
    rebase = _rebase(timezone_str="America/Chicago")
    anchor = resolve_effective_anchor(UTC_RUNTIME, rebase, None, "America/Los_Angeles")
    assert anchor is not None
    assert anchor.timezone == ZoneInfo("America/Los_Angeles")


def test_rebase_timezone_beats_sidecar_timezone() -> None:
    """rebase.timezone beats sidecar.timezone."""
    rebase = _rebase(timezone_str="America/Chicago")
    anchor = resolve_effective_anchor(UTC_RUNTIME, rebase, None, None)
    assert anchor is not None
    assert anchor.timezone == ZoneInfo("America/Chicago")


# ---------------------------------------------------------------------------
# Error: RebaseTimezoneUnresolvable
# ---------------------------------------------------------------------------


def test_rebase_timezone_unresolvable_no_zone() -> None:
    """base_date resolves but no timezone anywhere → RebaseTimezoneUnresolvable."""
    rebase = _rebase(base_date=datetime(2026, 1, 1))
    with pytest.raises(RebaseTimezoneUnresolvable):
        resolve_effective_anchor(None, rebase, None, None)


# ---------------------------------------------------------------------------
# Error: RebaseOriginUnresolvable
# ---------------------------------------------------------------------------


def test_rebase_origin_unresolvable_no_base_date_no_sidecar() -> None:
    """timezone override, no base_date, no sidecar anchor → RebaseOriginUnresolvable."""
    with pytest.raises(RebaseOriginUnresolvable):
        resolve_effective_anchor(None, None, None, "America/New_York")


def test_rebase_origin_unresolvable_via_config() -> None:
    """Config timezone-only, no sidecar → RebaseOriginUnresolvable."""
    rebase = _rebase(timezone_str="America/New_York")
    with pytest.raises(RebaseOriginUnresolvable):
        resolve_effective_anchor(None, rebase, None, None)


# ---------------------------------------------------------------------------
# Error: RebaseDateNotNaive
# ---------------------------------------------------------------------------


def test_rebase_date_not_naive_from_config() -> None:
    """tz-aware base_date from config → RebaseDateNotNaive."""
    aware_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rebase = RebaseConfig(base_date=aware_dt, timezone="UTC")
    with pytest.raises(RebaseDateNotNaive):
        resolve_effective_anchor(UTC_RUNTIME, rebase, None, None)


def test_rebase_date_not_naive_from_cli() -> None:
    """tz-aware base_date from CLI → RebaseDateNotNaive."""
    aware_dt = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5)))
    with pytest.raises(RebaseDateNotNaive):
        resolve_effective_anchor(UTC_RUNTIME, None, aware_dt, None)


# ---------------------------------------------------------------------------
# Error: RebaseDateUnresolvable — DST gap
# ---------------------------------------------------------------------------


def test_rebase_date_unresolvable_dst_gap() -> None:
    """Nonexistent time in DST gap → RebaseDateUnresolvable."""
    # America/New_York spring-forward: 2026-03-08 02:30 does not exist
    gap_dt = datetime(2026, 3, 8, 2, 30, 0)
    rebase = _rebase(base_date=gap_dt, timezone_str="America/New_York")
    with pytest.raises(RebaseDateUnresolvable):
        resolve_effective_anchor(None, rebase, None, None)


def test_rebase_date_unresolvable_dst_fold() -> None:
    """Ambiguous time in DST fold → RebaseDateUnresolvable (no silent pick)."""
    # America/New_York fall-back: 2026-11-01 01:30 is ambiguous
    fold_dt = datetime(2026, 11, 1, 1, 30, 0)
    rebase = _rebase(base_date=fold_dt, timezone_str="America/New_York")
    with pytest.raises(RebaseDateUnresolvable):
        resolve_effective_anchor(None, rebase, None, None)


# ---------------------------------------------------------------------------
# Error: RebaseUnknownTimezone
# ---------------------------------------------------------------------------


def test_rebase_unknown_timezone() -> None:
    """Bogus IANA string → RebaseUnknownTimezone."""
    with pytest.raises(RebaseUnknownTimezone):
        resolve_effective_anchor(UTC_RUNTIME, None, None, "Not/AZone")


# ---------------------------------------------------------------------------
# Error: RebaseInvalidRuntimeAnchor
# ---------------------------------------------------------------------------


def test_rebase_invalid_runtime_anchor_unparseable() -> None:
    """Unparseable start_datetime → RebaseInvalidRuntimeAnchor."""
    bad_runtime = RuntimeAnchor(timezone="UTC", start_datetime="not-a-datetime")
    with pytest.raises(RebaseInvalidRuntimeAnchor):
        resolve_effective_anchor(bad_runtime, None, None, None)


def test_rebase_invalid_runtime_anchor_naive() -> None:
    """Naive start_datetime → RebaseInvalidRuntimeAnchor."""
    naive_runtime = RuntimeAnchor(timezone="UTC", start_datetime="2020-03-01T00:00:00")
    with pytest.raises(RebaseInvalidRuntimeAnchor):
        resolve_effective_anchor(naive_runtime, None, None, None)
