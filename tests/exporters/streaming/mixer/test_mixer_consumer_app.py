"""Tests for the consumer-side build_app extensions."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from fabulexa_forge.errors import MixerExtraUnavailable
from fabulexa_forge.exporters.streaming.mixer.app import (
    build_app,
    derive_consumer_meters,
)

from ._helpers import _make_consumer_run_state, _make_run_state

# ---------------------------------------------------------------------------
# GET /api/capabilities
# ---------------------------------------------------------------------------


class TestGetCapabilities:
    def test_consumer_enabled_true_when_consumer_set(self) -> None:
        consumer = _make_consumer_run_state(["t1"])
        state = _make_run_state(consumer=consumer)
        client = TestClient(build_app(state))

        resp = client.get("/api/capabilities")
        assert resp.status_code == 200
        assert resp.json()["consumer_enabled"] is True

    def test_consumer_enabled_false_when_consumer_none(self) -> None:
        state = _make_run_state(consumer=None)
        client = TestClient(build_app(state))

        resp = client.get("/api/capabilities")
        assert resp.status_code == 200
        assert resp.json()["consumer_enabled"] is False


# ---------------------------------------------------------------------------
# Consumer routes when consumer is set
# ---------------------------------------------------------------------------


class TestConsumerRoutesEnabled:
    def test_get_consumer_state_echoes_dials(self) -> None:
        consumer = _make_consumer_run_state(["facts", "dims"])
        state = _make_run_state(consumer=consumer)
        client = TestClient(build_app(state))

        resp = client.get("/api/consumer/state")
        assert resp.status_code == 200
        data = resp.json()
        topics = [t["topic"] for t in data["topics"]]
        assert topics == ["facts", "dims"]
        assert all(t["ingest_rate"] == 1.0 for t in data["topics"])

    def test_get_consumer_meters_returns_derive_consumer_meters(self) -> None:
        consumer = _make_consumer_run_state(["t1"])
        state = _make_run_state(consumer=consumer)
        client = TestClient(build_app(state))

        expected = derive_consumer_meters(consumer, state.anchor).model_dump()
        resp = client.get("/api/consumer/meters")
        assert resp.status_code == 200
        assert resp.json() == expected

    def test_put_consumer_topic_mutates_ingest_rate_and_echoes(self) -> None:
        consumer = _make_consumer_run_state(["facts", "dims"])
        state = _make_run_state(consumer=consumer)
        client = TestClient(build_app(state))

        resp = client.put("/api/consumer/topics/facts", json={"ingest_rate": 50.0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["topic"] == "facts"
        assert data["ingest_rate"] == 50.0

        facts_dial = next(d for d in consumer.control.topics if d.topic == "facts")
        assert facts_dial.ingest_rate == 50.0

    def test_put_consumer_topic_unknown_returns_404(self) -> None:
        consumer = _make_consumer_run_state(["facts"])
        state = _make_run_state(consumer=consumer)
        client = TestClient(build_app(state))

        resp = client.put("/api/consumer/topics/nonexistent", json={"ingest_rate": 1.0})
        assert resp.status_code == 404

    def test_put_consumer_topic_out_of_bounds_returns_422(self) -> None:
        consumer = _make_consumer_run_state(["facts"])
        state = _make_run_state(consumer=consumer)
        client = TestClient(build_app(state))

        resp = client.put("/api/consumer/topics/facts", json={"ingest_rate": 99999.9})
        assert resp.status_code == 422

    def test_put_consumer_topic_extra_fields_ignored(self) -> None:
        consumer = _make_consumer_run_state(["facts"])
        state = _make_run_state(consumer=consumer)
        client = TestClient(build_app(state))

        resp = client.put(
            "/api/consumer/topics/facts",
            json={"ingest_rate": 2.0, "topic": "ignored", "extra": "ignored"},
        )
        assert resp.status_code == 200
        assert resp.json()["topic"] == "facts"


# ---------------------------------------------------------------------------
# Consumer routes when consumer is None (producer-only)
# ---------------------------------------------------------------------------


class TestConsumerRoutesDisabled:
    def test_consumer_state_unregistered_returns_404(self) -> None:
        state = _make_run_state(consumer=None)
        client = TestClient(build_app(state))
        assert client.get("/api/consumer/state").status_code == 404

    def test_consumer_meters_unregistered_returns_404(self) -> None:
        state = _make_run_state(consumer=None)
        client = TestClient(build_app(state))
        assert client.get("/api/consumer/meters").status_code == 404

    def test_consumer_put_topic_unregistered_returns_404(self) -> None:
        state = _make_run_state(consumer=None)
        client = TestClient(build_app(state))
        resp = client.put("/api/consumer/topics/any", json={"ingest_rate": 1.0})
        assert resp.status_code == 404

    def test_producer_routes_still_work(self) -> None:
        state = _make_run_state(topics=["orders", "customers"], consumer=None)
        client = TestClient(build_app(state))

        assert client.get("/api/state").status_code == 200
        assert client.get("/api/meters").status_code == 200

        resp = client.put("/api/transport", json={"playing": True, "speed": 1.0})
        assert resp.status_code == 200

        resp = client.put(
            "/api/topics/orders",
            json={"rate": 0.5, "lag_ms": 0, "mute": False},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# build_app raises MixerExtraUnavailable when FastAPI is not importable
# ---------------------------------------------------------------------------


class TestBuildAppMixerExtraUnavailable:
    def test_raises_when_fastapi_not_importable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_app raises MixerExtraUnavailable when FastAPI is not importable."""
        monkeypatch.setitem(sys.modules, "fastapi", None)  # type: ignore[misc]
        state = _make_run_state()
        with pytest.raises(MixerExtraUnavailable):
            build_app(state)
