"""Shared StreamConfig-builder helpers for stream-playback head/seek tests.

Thin construction wrappers over KindStream / MembershipStream / StreamConfig
— no behavior of their own — shared by test_stream_head.py and
test_stream_seek.py to avoid duplicating the same declaration boilerplate
`tests/exporters/streaming/test_engine.py` already carries for its own
suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fabulexa_forge.config.models import KindStream, MembershipStream, StreamConfig

if TYPE_CHECKING:
    from fabulexa_forge.config.models import PredicateValue


def kind_stream(
    name: str,
    kind: str,
    properties: list[str],
    *,
    sub_types: list[str] | None = None,
    where: "dict[str, PredicateValue] | None" = None,
    only: list[str] | None = None,
    ignore: list[str] | None = None,
) -> KindStream:
    """Build one KindStream declaration."""
    return KindStream(
        name=name,
        kind=kind,
        properties=properties,
        sub_types=sub_types,
        where=where,
        only=only,
        ignore=ignore,
    )


def state_changes_config(streams: list[KindStream]) -> StreamConfig:
    """Build a content='state-changes' StreamConfig from KindStream declarations."""
    return StreamConfig(content="state-changes", streams=streams)


def membership_stream(
    name: str,
    owner_kind: str,
    property_name: str,
    fields: list[str],
) -> MembershipStream:
    """Build one MembershipStream declaration."""
    return MembershipStream(
        name=name,
        membership={"kind": owner_kind, "property": property_name},
        fields=fields,
    )


def membership_events_config(streams: list[MembershipStream]) -> StreamConfig:
    """Build a content='membership-events' StreamConfig from declarations."""
    return StreamConfig(content="membership-events", streams=streams)
