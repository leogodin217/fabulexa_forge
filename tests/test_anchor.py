"""Tests for EffectiveAnchor resolution in fabulexa_forge.anchor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb
import pytest

from fabulexa_forge.anchor import (
    EffectiveAnchor,
    TemporalRender,
    render_anchor_temporal_expr,
    resolve_effective_anchor,
)
from fabulexa_forge.config.models import RebaseConfig
from fabulexa_forge.errors import (
    RebaseDateNotNaive,
    RebaseDateUnresolvable,
    RebaseInvalidRuntimeAnchor,
    RebaseOriginUnresolvable,
    RebaseTimezoneUnresolvable,
    RebaseUnknownTimezone,
)
from fabulexa_forge.reader.sidecar import RuntimeAnchor

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


def _strip_alias(expr: str, out_name: str) -> str:
    """Drop the trailing `AS "<out_name>"` from a rendered SQL fragment."""
    return expr.removesuffix(f' AS "{out_name}"')


def _rendered(
    anchor: EffectiveAnchor | None,
    sim_time_ns: int,
    render: TemporalRender,
    out_name: str = "v",
) -> str:
    """Render one election over a bare ns-literal source, alias stripped."""
    expr = render_anchor_temporal_expr(anchor, str(sim_time_ns), out_name, render)
    return _strip_alias(expr, out_name)


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


# ---------------------------------------------------------------------------
# render_anchor_temporal_expr: election family
# ---------------------------------------------------------------------------


def test_timestamp_election_byte_identical_to_predecessor() -> None:
    """`timestamp` election reproduces the pre-sprint expression verbatim."""
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2020-03-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    qualified_source = '"_grain"."sim_time"'
    expr = render_anchor_temporal_expr(
        anchor, qualified_source, "created_at", "timestamp"
    )
    zone = str(anchor.timezone)
    origin = anchor.start_instant.isoformat()
    expected = (
        f"timezone('{zone}', TIMESTAMPTZ '{origin}'"
        f" + to_microseconds(CAST({qualified_source} AS BIGINT) // 1000))"
        f' AS "created_at"'
    )
    assert expr == expected


def test_elections_materialize_to_the_expected_duckdb_types() -> None:
    """Each election's SELECT fragment executes to the named DuckDB type."""
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2020-03-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    con = duckdb.connect()
    expected_types: dict[TemporalRender, str] = {
        "timestamp": "TIMESTAMP",
        "date": "DATE",
        "time": "TIME",
        "timestamptz": "TIMESTAMP WITH TIME ZONE",
    }
    for render, expected_type in expected_types.items():
        expr = render_anchor_temporal_expr(anchor, "0", "v", render)
        row = con.sql(f'SELECT typeof("v") AS t FROM (SELECT {expr})').fetchone()
        assert row is not None
        assert row[0] == expected_type


def test_family_identity_date_and_time_match_the_naive_timestamp() -> None:
    """`date` == the naive timestamp's date part, `time` its time-of-day."""
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-06-01T12:00:00-04:00"),
        timezone=ZoneInfo("America/New_York"),
    )
    con = duckdb.connect()
    ns = 3 * 3_600 * 1_000_000_000  # +3 hours from the origin
    ts = _rendered(anchor, ns, "timestamp")
    date_ = _rendered(anchor, ns, "date")
    time_ = _rendered(anchor, ns, "time")
    row = con.sql(
        f"SELECT ({date_}) = CAST(({ts}) AS DATE) AS date_eq,"
        f" ({time_}) = CAST(({ts}) AS TIME) AS time_eq"
    ).fetchone()
    assert row == (True, True)


def test_timestamptz_renders_the_absolute_instant() -> None:
    """`timestamptz` equals the anchor origin plus the elapsed physical delta."""
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-06-01T12:00:00-04:00"),
        timezone=ZoneInfo("America/New_York"),
    )
    con = duckdb.connect()
    ns = 90 * 60 * 1_000_000_000  # +90 minutes
    tz_expr = _rendered(anchor, ns, "timestamptz")
    row = con.sql(
        f"SELECT ({tz_expr}) = TIMESTAMPTZ '2024-06-01T13:30:00-04:00' AS eq"
    ).fetchone()
    assert row == (True,)


def test_dst_fold_naive_steps_back_but_timestamptz_strictly_increases() -> None:
    """Across a DST fold, naive renderings may repeat/step back (existing
    accepted behavior); `timestamptz` stays strictly increasing."""
    anchor = EffectiveAnchor(
        # America/New_York falls back at 2024-11-03 02:00 EDT -> 01:00 EST.
        start_instant=datetime.fromisoformat("2024-11-03T05:00:00+00:00"),
        timezone=ZoneInfo("America/New_York"),
    )
    con = duckdb.connect()
    ns_before = 0
    ns_after = 3_600 * 1_000_000_000  # +1 physical hour, crosses the fold
    ts_before = _rendered(anchor, ns_before, "timestamp")
    ts_after = _rendered(anchor, ns_after, "timestamp")
    tz_before = _rendered(anchor, ns_before, "timestamptz")
    tz_after = _rendered(anchor, ns_after, "timestamptz")
    row = con.sql(
        f"SELECT ({ts_after}) = ({ts_before}) AS naive_same,"
        f" ({tz_after}) > ({tz_before}) AS tz_increases"
    ).fetchone()
    assert row == (True, True)


def test_no_anchor_default_timestamp_aliases_raw_source() -> None:
    """`anchor=None` + `render='timestamp'`: raw source aliased through unchanged."""
    expr = render_anchor_temporal_expr(
        None, '"_grain"."sim_time"', "created_at", "timestamp"
    )
    assert expr == '"_grain"."sim_time" AS "created_at"'


def test_ns_truncates_to_microseconds_identically_across_all_elections() -> None:
    """A sub-microsecond ns remainder truncates identically across all four
    elections (Python datetime precision, the shipped rule)."""
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    con = duckdb.connect()
    renders: tuple[TemporalRender, ...] = ("timestamp", "date", "time", "timestamptz")
    for render in renders:
        one_and_a_half_us = _rendered(anchor, 1_500, render)
        exactly_one_us = _rendered(anchor, 1_000, render)
        row = con.sql(
            f"SELECT ({one_and_a_half_us}) = ({exactly_one_us}) AS eq"
        ).fetchone()
        assert row == (True,)
