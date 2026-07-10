"""Tests for build_app — the mixer FastAPI control-plane app."""

from __future__ import annotations

from collections import deque

from fastapi.testclient import TestClient

from fabulexa_forge.exporters.streaming.mixer.app import build_app, derive_meters
from fabulexa_forge.exporters.streaming.mixer.run_state import MixerRunState
from fabulexa_forge.exporters.streaming.mixer.scheduler import (
    ControlState,
    FrontierState,
    TopicDials,
    Transport,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent

from .._helpers import make_anchor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dial(
    topic: str,
    rate: float = 1.0,
    lag_ms: int = 0,
    mute: bool = False,
) -> TopicDials:
    return TopicDials(
        topic=topic,
        content="state-changes",
        rate=rate,
        lag_ms=lag_ms,
        mute=mute,
    )


def _make_run_state(
    topics: list[str] | None = None,
    playing: bool = False,
    speed: float = 1.0,
    play_origin_monotonic: float | None = None,
    monotonic_val: float = 100.0,
) -> MixerRunState:
    if topics is None:
        topics = ["orders", "customers"]
    anchor = make_anchor()
    dials = [_make_dial(t) for t in topics]
    control = ControlState(
        transport=Transport(playing=playing, speed=speed),
        topics=dials,
    )
    frontier = FrontierState(
        frontier_sim_time=None,
        edges={t: None for t in topics},
        delivery_edges={t: None for t in topics},
    )
    buffers: dict[str, deque[StreamEvent]] = {t: deque() for t in topics}
    return MixerRunState(
        control=control,
        frontier=frontier,
        buffers=buffers,
        anchor=anchor,
        monotonic=lambda: monotonic_val,
        play_origin_monotonic=play_origin_monotonic,
    )


# ---------------------------------------------------------------------------
# GET /api/state
# ---------------------------------------------------------------------------


class TestGetState:
    def test_returns_control_state_out(self) -> None:
        state = _make_run_state(topics=["orders", "customers"])
        app = build_app(state)
        client = TestClient(app)

        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["transport"]["playing"] is False
        assert data["transport"]["speed"] == 1.0
        topic_names = [t["topic"] for t in data["topics"]]
        assert topic_names == ["orders", "customers"]

    def test_reflects_live_control_state(self) -> None:
        state = _make_run_state(topics=["t1"])
        app = build_app(state)
        client = TestClient(app)

        # Mutate the state directly
        state.control.transport.playing = True
        state.control.transport.speed = 2.5

        resp = client.get("/api/state")
        data = resp.json()
        assert data["transport"]["playing"] is True
        assert data["transport"]["speed"] == 2.5


# ---------------------------------------------------------------------------
# GET /api/meters
# ---------------------------------------------------------------------------


class TestGetMeters:
    def test_returns_meters_out(self) -> None:
        state = _make_run_state(topics=["t1"])
        app = build_app(state)
        client = TestClient(app)

        resp = client.get("/api/meters")
        assert resp.status_code == 200
        data = resp.json()
        assert "frontier_sim_time" in data
        assert "wall_elapsed_ms" in data
        assert "topics" in data

    def test_matches_derive_meters(self) -> None:
        state = _make_run_state(topics=["t1", "t2"])
        app = build_app(state)
        client = TestClient(app)

        expected = derive_meters(state).model_dump()
        resp = client.get("/api/meters")
        assert resp.status_code == 200
        assert resp.json() == expected


# ---------------------------------------------------------------------------
# PUT /api/transport
# ---------------------------------------------------------------------------


class TestPutTransport:
    def test_sets_transport_and_echoes(self) -> None:
        state = _make_run_state(playing=False, speed=1.0)
        app = build_app(state)
        client = TestClient(app)

        resp = client.put("/api/transport", json={"playing": True, "speed": 2.0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["playing"] is True
        assert data["speed"] == 2.0
        assert state.control.transport.playing is True
        assert state.control.transport.speed == 2.0

    def test_stamps_play_origin_on_first_false_to_true(self) -> None:
        state = _make_run_state(playing=False, monotonic_val=50.0)
        assert state.play_origin_monotonic is None
        app = build_app(state)
        client = TestClient(app)

        client.put("/api/transport", json={"playing": True, "speed": 1.0})
        assert state.play_origin_monotonic == 50.0

    def test_does_not_restamp_play_origin_on_subsequent_play(self) -> None:
        state = _make_run_state(playing=False, monotonic_val=50.0)
        app = build_app(state)
        client = TestClient(app)

        client.put("/api/transport", json={"playing": True, "speed": 1.0})
        first_origin = state.play_origin_monotonic

        # Pause then play again — origin must not change
        client.put("/api/transport", json={"playing": False, "speed": 1.0})
        client.put("/api/transport", json={"playing": True, "speed": 1.0})
        assert state.play_origin_monotonic == first_origin

    def test_out_of_bounds_body_returns_422_no_mutation(self) -> None:
        state = _make_run_state(playing=False, speed=1.0)
        app = build_app(state)
        client = TestClient(app)

        resp = client.put("/api/transport", json={"playing": True, "speed": 9999.9})
        assert resp.status_code == 422
        # State must be unchanged
        assert state.control.transport.playing is False
        assert state.control.transport.speed == 1.0


# ---------------------------------------------------------------------------
# PUT /api/topics/{topic}
# ---------------------------------------------------------------------------


class TestPutTopics:
    def test_mutates_dial_and_echoes(self) -> None:
        state = _make_run_state(topics=["orders", "customers"])
        app = build_app(state)
        client = TestClient(app)

        resp = client.put(
            "/api/topics/orders",
            json={"rate": 2.0, "lag_ms": 5000, "mute": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["topic"] == "orders"
        assert data["rate"] == 2.0
        assert data["lag_ms"] == 5000
        assert data["mute"] is True

        # Verify state was actually mutated
        orders_dial = next(d for d in state.control.topics if d.topic == "orders")
        assert orders_dial.rate == 2.0
        assert orders_dial.lag_ms == 5000
        assert orders_dial.mute is True

    def test_extra_fields_topic_and_content_ignored(self) -> None:
        """A client echoing topic/content from a GET response is accepted."""
        state = _make_run_state(topics=["orders"])
        app = build_app(state)
        client = TestClient(app)

        resp = client.put(
            "/api/topics/orders",
            json={
                "rate": 1.0,
                "lag_ms": 0,
                "mute": False,
                "topic": "ignored",
                "content": "ignored",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["topic"] == "orders"

    def test_unknown_topic_returns_404(self) -> None:
        state = _make_run_state(topics=["orders"])
        app = build_app(state)
        client = TestClient(app)

        resp = client.put(
            "/api/topics/nonexistent",
            json={"rate": 1.0, "lag_ms": 0, "mute": False},
        )
        assert resp.status_code == 404

    def test_out_of_bounds_body_returns_422(self) -> None:
        state = _make_run_state(topics=["orders"])
        app = build_app(state)
        client = TestClient(app)

        resp = client.put(
            "/api/topics/orders",
            json={"rate": 99.9, "lag_ms": 0, "mute": False},
        )
        assert resp.status_code == 422
