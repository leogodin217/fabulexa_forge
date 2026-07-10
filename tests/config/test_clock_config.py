"""Tests for ClockConfig parse-time validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import ClockConfig


def test_clock_config_realtime_with_speed_parses() -> None:
    """Realtime mode with speed parses cleanly."""
    cfg = ClockConfig.model_validate({"mode": "realtime", "speed": 60.0})
    assert cfg.mode == "realtime"
    assert cfg.speed == 60.0
    assert cfg.idle_cap_seconds is None


def test_clock_config_realtime_with_speed_and_cap_parses() -> None:
    """Realtime mode with speed and idle_cap_seconds parses cleanly."""
    cfg = ClockConfig.model_validate(
        {"mode": "realtime", "speed": 60.0, "idle_cap_seconds": 5.0}
    )
    assert cfg.mode == "realtime"
    assert cfg.speed == 60.0
    assert cfg.idle_cap_seconds == 5.0


def test_clock_config_realtime_without_speed_raises() -> None:
    """Realtime mode without speed raises ValueError."""
    with pytest.raises(ValidationError, match="mode='realtime' requires speed"):
        ClockConfig.model_validate({"mode": "realtime"})


def test_clock_config_fast_no_params_parses() -> None:
    """Fast mode with no optional params parses cleanly."""
    cfg = ClockConfig.model_validate({"mode": "fast"})
    assert cfg.mode == "fast"
    assert cfg.speed is None
    assert cfg.idle_cap_seconds is None


def test_clock_config_fast_with_speed_raises() -> None:
    """Fast mode with speed raises ValueError."""
    with pytest.raises(ValidationError, match="speed is forbidden under mode='fast'"):
        ClockConfig.model_validate({"mode": "fast", "speed": 10.0})


def test_clock_config_fast_with_idle_cap_raises() -> None:
    """Fast mode with idle_cap_seconds raises ValueError."""
    with pytest.raises(
        ValidationError, match="idle_cap_seconds is forbidden under mode='fast'"
    ):
        ClockConfig.model_validate({"mode": "fast", "idle_cap_seconds": 2.0})


def test_clock_config_speed_zero_raises() -> None:
    """speed=0 raises due to gt=0 constraint."""
    with pytest.raises(ValidationError):
        ClockConfig.model_validate({"mode": "realtime", "speed": 0})


def test_clock_config_speed_negative_raises() -> None:
    """speed=-1 raises due to gt=0 constraint."""
    with pytest.raises(ValidationError):
        ClockConfig.model_validate({"mode": "realtime", "speed": -1})


def test_clock_config_idle_cap_zero_raises() -> None:
    """idle_cap_seconds=0 raises due to gt=0 constraint."""
    with pytest.raises(ValidationError):
        ClockConfig.model_validate(
            {"mode": "realtime", "speed": 60.0, "idle_cap_seconds": 0}
        )


def test_clock_config_unknown_field_raises() -> None:
    """Unknown field raises (extra='forbid')."""
    with pytest.raises(ValidationError):
        ClockConfig.model_validate({"mode": "fast", "unknown": "bad"})


def test_clock_config_unknown_mode_raises() -> None:
    """A mode outside {fast, realtime} raises (closed Literal)."""
    with pytest.raises(ValidationError):
        ClockConfig.model_validate({"mode": "turbo"})
