#!/usr/bin/env python
"""
Demo: Stream-declaration config surface — new fields + parse-time rejections
Sprint: streaming-authoring-parity
Phase: 1

Parses the design doc's two Configuration examples (a state-changes stream
with where/only/rename/kind_label and a membership-events stream with
sub_types/where/rename) into typed models, then shows five parse-time
rejections: empty rename, colliding rename targets, only+ignore together,
duplicate kind_labels labels, and empty sub_types.
"""

from __future__ import annotations

from pydantic import ValidationError

from fabulexa_forge.config.models import KindStream, MembershipStream, StreamConfig

STATE_CHANGES_EXAMPLE: dict[str, object] = {
    "content": "state-changes",
    "streams": [
        {
            "name": "security_events",
            "kind": "tick_decision",
            "sub_types": ["login", "logout", "access_denied"],
            "where": {"region": ["emea", "apac"]},
            "only": ["decision_type", "context"],
            "properties": ["journey_instance", "decision_type", "context"],
            "rename": {
                "journey_instance": "session_id",
                "decision_type": "event_type",
            },
            "kind_label": "security_event",
        }
    ],
    "kind_labels": {"entity": "user"},
}

MEMBERSHIP_EVENTS_EXAMPLE: dict[str, object] = {
    "content": "membership-events",
    "streams": [
        {
            "name": "ward_occupancy",
            "membership": {"kind": "ward", "property": "occupants"},
            "sub_types": ["icu", "general"],
            "where": {"site": "north_campus"},
            "fields": ["bed", "admitted_by"],
            "rename": {"admitted_by": "clinician"},
        }
    ],
    "kind_labels": {"ward": "ward", "patient": "patient"},
}


def demo_parse_design_doc_examples() -> None:
    """Parse both design-doc Configuration examples into typed models."""
    print("--- design-doc Configuration examples ---")

    state_cfg = StreamConfig.model_validate(STATE_CHANGES_EXAMPLE)
    stream = state_cfg.streams[0]
    assert isinstance(stream, KindStream)
    print(f"state-changes: stream {stream.name!r} kind_label={stream.kind_label!r}")
    print(f"  where={stream.where!r} only={stream.only!r} rename={stream.rename!r}")
    print(f"  kind_labels={state_cfg.kind_labels!r}")

    membership_cfg = StreamConfig.model_validate(MEMBERSHIP_EVENTS_EXAMPLE)
    membership_stream = membership_cfg.streams[0]
    assert isinstance(membership_stream, MembershipStream)
    print(
        f"membership-events: stream {membership_stream.name!r}"
        f" sub_types={membership_stream.sub_types!r}"
    )
    print(f"  where={membership_stream.where!r} rename={membership_stream.rename!r}")
    print(f"  kind_labels={membership_cfg.kind_labels!r}")


def demo_parse_time_rejections() -> None:
    """Show the five parse-time rejections a misconfigured stream draws."""
    print("--- parse-time rejections ---")

    try:
        KindStream.model_validate(
            {"name": "s", "kind": "k", "properties": ["x"], "rename": {}}
        )
    except ValidationError as exc:
        print(f"empty rename map: refused ({exc.errors()[0]['msg']})")
    else:
        raise AssertionError("expected ValidationError for empty rename")

    try:
        KindStream.model_validate(
            {
                "name": "s",
                "kind": "k",
                "properties": ["x", "y"],
                "rename": {"x": "out", "y": "out"},
            }
        )
    except ValidationError as exc:
        print(f"colliding rename targets: refused ({exc.errors()[0]['msg']})")
    else:
        raise AssertionError("expected ValidationError for colliding rename targets")

    try:
        KindStream.model_validate(
            {
                "name": "s",
                "kind": "k",
                "properties": ["x"],
                "only": ["x"],
                "ignore": ["x"],
            }
        )
    except ValidationError as exc:
        print(f"only + ignore together: refused ({exc.errors()[0]['msg']})")
    else:
        raise AssertionError("expected ValidationError for only+ignore")

    try:
        StreamConfig.model_validate(
            {
                "content": "state-changes",
                "streams": [
                    {"name": "s", "kind": "k", "properties": []},
                ],
                "kind_labels": {"actor": "patient", "resource": "patient"},
            }
        )
    except ValidationError as exc:
        print(f"duplicate kind_labels labels: refused ({exc.errors()[0]['msg']})")
    else:
        raise AssertionError("expected ValidationError for duplicate kind_labels")

    try:
        MembershipStream.model_validate(
            {
                "name": "s",
                "membership": {"kind": "ward", "property": "occupants"},
                "fields": ["bed"],
                "sub_types": [],
            }
        )
    except ValidationError as exc:
        print(f"empty sub_types: refused ({exc.errors()[0]['msg']})")
    else:
        raise AssertionError("expected ValidationError for empty sub_types")


def main() -> int:
    demo_parse_design_doc_examples()
    demo_parse_time_rejections()
    print("SUCCESS: stream config surface parses and rejects as specified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
