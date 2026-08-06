#!/usr/bin/env python
"""
Demo: source-domain-vocabulary config grammar (kind_labels, item_type, rename)
Sprint: source-domain-vocabulary
Phase: 1

Parses a full vocabulary config (`kind_labels` + an events-source
`item_type` override + a `rename` map), prints the loaded model, then shows
each parse-time rejection with its ValueError message. Nothing consumes
these fields yet -- plan resolution lands in Phases 2-3.
"""

from __future__ import annotations

from pydantic import ValidationError

from fabulexa_forge.config.models import ExportConfig

FULL_VOCABULARY_CONFIG = {
    "mode": "source",
    "source": {
        "kind_labels": {"actor": "patient", "resource": "consultant"},
        "tables": [
            {"name": "patient", "kind": "actor"},
            {"name": "consultant", "kind": "resource"},
        ],
        "events": {
            "name": "audit_log",
            "sources": [
                {"kind": "actor", "rename": {"full_name": "name"}},
                {
                    "membership": {"kind": "resource", "property": "holders"},
                    "item_type": "consultant_allocation",
                },
            ],
        },
    },
}

REJECTIONS: tuple[tuple[str, dict[str, object]], ...] = (
    (
        "empty item_type",
        {
            "mode": "source",
            "source": {
                "tables": [{"name": "patient", "kind": "actor"}],
                "events": {
                    "name": "audit_log",
                    "sources": [{"kind": "actor", "item_type": ""}],
                },
            },
        },
    ),
    (
        "empty rename",
        {
            "mode": "source",
            "source": {
                "tables": [{"name": "patient", "kind": "actor"}],
                "events": {
                    "name": "audit_log",
                    "sources": [{"kind": "actor", "rename": {}}],
                },
            },
        },
    ),
    (
        "duplicate rename targets",
        {
            "mode": "source",
            "source": {
                "tables": [{"name": "patient", "kind": "actor"}],
                "events": {
                    "name": "audit_log",
                    "sources": [
                        {
                            "kind": "actor",
                            "rename": {"full_name": "name", "nickname": "name"},
                        }
                    ],
                },
            },
        },
    ),
    (
        "empty kind_labels",
        {
            "mode": "source",
            "source": {
                "kind_labels": {},
                "tables": [{"name": "patient", "kind": "actor"}],
            },
        },
    ),
    (
        "duplicate kind_labels values",
        {
            "mode": "source",
            "source": {
                "kind_labels": {"actor": "patient", "resource": "patient"},
                "tables": [{"name": "patient", "kind": "actor"}],
            },
        },
    ),
)


def main() -> int:
    config = ExportConfig.model_validate(FULL_VOCABULARY_CONFIG)
    assert config.source is not None
    print("Parsed source.kind_labels:", config.source.kind_labels)
    for source in config.source.events.sources if config.source.events else ():
        print(
            "events source:",
            f"item_type={source.item_type!r}",
            f"rename={source.rename!r}",
        )

    for label, bad_config in REJECTIONS:
        try:
            ExportConfig.model_validate(bad_config)
        except ValidationError as exc:
            print(f"REJECTED ({label}): {exc.errors()[0]['msg']}")
        else:
            raise AssertionError(f"expected rejection for: {label}")

    print("SUCCESS: config grammar parses and validates as specified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
