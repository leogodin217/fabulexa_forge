"""Wire models for the mixer control API.

Plain Pydantic BaseModel (NOT the config StrictBaseModel). Request models set
extra="ignore" so a client echoing a full GET response body is accepted without
validation error.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TransportUpdate(BaseModel):
    """PUT /api/transport request body."""

    model_config = ConfigDict(extra="ignore")
    playing: bool
    speed: float = Field(ge=0.1, le=1000.0)


class TopicDialsUpdate(BaseModel):
    """PUT /api/topics/{topic} request body."""

    model_config = ConfigDict(extra="ignore")
    rate: float = Field(ge=0.0, le=4.0)
    lag_ms: int = Field(ge=0, le=300_000)
    mute: bool


class TransportOut(BaseModel):
    """PUT /api/transport response body."""

    playing: bool
    speed: float


class TopicDialsOut(BaseModel):
    """One topic channel strip in the GET /api/state response and PUT echo."""

    topic: str
    content: Literal["state-changes", "membership-events"]
    rate: float
    lag_ms: int
    mute: bool


class ControlStateOut(BaseModel):
    """GET /api/state response body."""

    transport: TransportOut
    topics: list[TopicDialsOut]


class TopicMeterOut(BaseModel):
    """One topic's producer-side meter reading."""

    topic: str
    backlog: int
    delivery_lag_ms: int | None
    delivery_edge_sim_time: str | None


class MetersOut(BaseModel):
    """GET /api/meters response body."""

    frontier_sim_time: str | None
    wall_elapsed_ms: int
    topics: list[TopicMeterOut]


class ConsumerTopicDialsUpdate(BaseModel):
    """PUT /api/consumer/topics/{topic} request body."""

    model_config = ConfigDict(extra="ignore")
    ingest_rate: float = Field(ge=0.0, le=10000.0)


class ConsumerTopicDialsOut(BaseModel):
    """One consumer channel strip in GET /api/consumer/state and the PUT echo."""

    topic: str
    content: Literal["state-changes", "membership-events"]
    ingest_rate: float


class ConsumerControlStateOut(BaseModel):
    """GET /api/consumer/state response body."""

    topics: list[ConsumerTopicDialsOut]


class ConsumerTopicMeterOut(BaseModel):
    """One topic's consumer-side meter reading."""

    topic: str
    watermark_sim_time: str | None
    consumer_lag: int


class WindowMeterOut(BaseModel):
    """One declared window's firing summary."""

    size_ms: int
    fired_count: int
    latest_window_end_sim_time: str | None


class JoinMeterOut(BaseModel):
    """One declared fact/dimension join's null health."""

    fact_topic: str
    dimension_topic: str
    fact_count: int
    null_count: int
    null_rate: float | None


class ConsumerMetersOut(BaseModel):
    """GET /api/consumer/meters response body."""

    global_watermark_sim_time: str | None
    topics: list[ConsumerTopicMeterOut]
    windows: list[WindowMeterOut]
    joins: list[JoinMeterOut]


class CapabilitiesOut(BaseModel):
    """GET /api/capabilities response body."""

    consumer_enabled: bool
