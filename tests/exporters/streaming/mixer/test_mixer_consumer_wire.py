"""Tests for consumer wire models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_export.exporters.streaming.mixer.wire import (
    ConsumerMetersOut,
    ConsumerTopicDialsUpdate,
    JoinMeterOut,
    WindowMeterOut,
)


class TestConsumerTopicDialsUpdate:
    def test_accepts_lower_bound(self) -> None:
        m = ConsumerTopicDialsUpdate(ingest_rate=0.0)
        assert m.ingest_rate == 0.0

    def test_accepts_upper_bound(self) -> None:
        m = ConsumerTopicDialsUpdate(ingest_rate=10000.0)
        assert m.ingest_rate == 10000.0

    def test_accepts_mid_range(self) -> None:
        m = ConsumerTopicDialsUpdate(ingest_rate=42.5)
        assert m.ingest_rate == 42.5

    def test_rejects_below_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            ConsumerTopicDialsUpdate(ingest_rate=-0.1)

    def test_rejects_above_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            ConsumerTopicDialsUpdate(ingest_rate=10000.1)

    def test_ignores_extra_fields(self) -> None:
        m = ConsumerTopicDialsUpdate.model_validate(
            {"ingest_rate": 1.0, "extra_field": "ignored"}
        )
        assert m.ingest_rate == 1.0
        assert not hasattr(m, "extra_field")


class TestNullableFields:
    def test_consumer_meters_out_null_global_watermark_serializes(self) -> None:
        m = ConsumerMetersOut(
            global_watermark_sim_time=None, topics=[], windows=[], joins=[]
        )
        data = m.model_dump()
        assert data["global_watermark_sim_time"] is None

    def test_window_meter_out_null_latest_end_serializes(self) -> None:
        m = WindowMeterOut(size_ms=1000, fired_count=0, latest_window_end_sim_time=None)
        data = m.model_dump()
        assert data["latest_window_end_sim_time"] is None

    def test_join_meter_out_null_null_rate_serializes(self) -> None:
        m = JoinMeterOut(
            fact_topic="facts",
            dimension_topic="dims",
            fact_count=0,
            null_count=0,
            null_rate=None,
        )
        data = m.model_dump()
        assert data["null_rate"] is None

    def test_fields_serialize_correctly_when_set(self) -> None:
        m = ConsumerMetersOut(
            global_watermark_sim_time="2026-01-01T00:00:00+00:00",
            topics=[],
            windows=[
                WindowMeterOut(
                    size_ms=500,
                    fired_count=3,
                    latest_window_end_sim_time="2026-01-01T01:00:00+00:00",
                )
            ],
            joins=[
                JoinMeterOut(
                    fact_topic="facts",
                    dimension_topic="dims",
                    fact_count=10,
                    null_count=5,
                    null_rate=0.5,
                )
            ],
        )
        data = m.model_dump()
        assert data["global_watermark_sim_time"] == "2026-01-01T00:00:00+00:00"
        assert (
            data["windows"][0]["latest_window_end_sim_time"]
            == "2026-01-01T01:00:00+00:00"
        )
        assert data["joins"][0]["null_rate"] == 0.5
