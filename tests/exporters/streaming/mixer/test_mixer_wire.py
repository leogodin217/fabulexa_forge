"""Tests for the mixer control-API wire models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_export.exporters.streaming.mixer.wire import (
    TopicDialsUpdate,
    TransportUpdate,
)


class TestTransportUpdate:
    """TransportUpdate validation rules."""

    def test_speed_at_lower_bound_is_valid(self) -> None:
        t = TransportUpdate(playing=False, speed=0.1)
        assert t.speed == 0.1

    def test_speed_at_upper_bound_is_valid(self) -> None:
        t = TransportUpdate(playing=True, speed=1000.0)
        assert t.speed == 1000.0

    def test_speed_below_lower_bound_raises(self) -> None:
        with pytest.raises(ValidationError):
            TransportUpdate(playing=True, speed=0.09)

    def test_speed_above_upper_bound_raises(self) -> None:
        with pytest.raises(ValidationError):
            TransportUpdate(playing=True, speed=1000.1)

    def test_extra_fields_are_silently_dropped(self) -> None:
        """A client echoing a full GET response (with extra keys) is accepted."""
        t = TransportUpdate(
            **{
                "playing": True,
                "speed": 1.0,
                "topic": "orders",
                "content": "state-changes",
            }
        )
        assert t.playing is True
        assert not hasattr(t, "topic")


class TestTopicDialsUpdate:
    """TopicDialsUpdate validation rules."""

    def test_rate_at_bounds_is_valid(self) -> None:
        t = TopicDialsUpdate(rate=0.0, lag_ms=0, mute=False)
        assert t.rate == 0.0
        t2 = TopicDialsUpdate(rate=4.0, lag_ms=0, mute=False)
        assert t2.rate == 4.0

    def test_rate_below_lower_bound_raises(self) -> None:
        with pytest.raises(ValidationError):
            TopicDialsUpdate(rate=-0.01, lag_ms=0, mute=False)

    def test_rate_above_upper_bound_raises(self) -> None:
        with pytest.raises(ValidationError):
            TopicDialsUpdate(rate=4.01, lag_ms=0, mute=False)

    def test_lag_ms_at_bounds_is_valid(self) -> None:
        t = TopicDialsUpdate(rate=1.0, lag_ms=0, mute=False)
        assert t.lag_ms == 0
        t2 = TopicDialsUpdate(rate=1.0, lag_ms=300_000, mute=False)
        assert t2.lag_ms == 300_000

    def test_lag_ms_below_lower_bound_raises(self) -> None:
        with pytest.raises(ValidationError):
            TopicDialsUpdate(rate=1.0, lag_ms=-1, mute=False)

    def test_lag_ms_above_upper_bound_raises(self) -> None:
        with pytest.raises(ValidationError):
            TopicDialsUpdate(rate=1.0, lag_ms=300_001, mute=False)

    def test_extra_fields_are_silently_dropped(self) -> None:
        """A client echoing `topic` / `content` from a GET response is accepted."""
        t = TopicDialsUpdate(
            **{
                "rate": 1.0,
                "lag_ms": 0,
                "mute": False,
                "topic": "orders",
                "content": "state-changes",
            }
        )
        assert t.rate == 1.0
        assert not hasattr(t, "topic")
