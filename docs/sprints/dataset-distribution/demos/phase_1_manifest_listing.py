#!/usr/bin/env python
"""
Demo: Dataset manifest models, loader, and listing renderer
Sprint: dataset-distribution
Phase: 1

Loads the shipped (empty) manifest offline, renders text and JSON for it,
constructs a populated manifest in memory and renders both formats, then
shows the parse-time validator refusing a handful of malformed entries.
"""

from __future__ import annotations

from pydantic import ValidationError

from fabulexa_forge.datasets.manifest import load_manifest, render_dataset_listing
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

POPULATED_ENTRY_ONE = {
    "name": "retail-week",
    "description": "One week of retail transactions.",
    "url": "https://example.com/retail-week.tar.gz",
    "sha256": "a" * 64,
    "size_bytes": 2_097_152,
    "base_format_version": 8,
    "configs": ["dimensional.yaml"],
    "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
}
POPULATED_ENTRY_TWO = {
    "name": "clinic-visits",
    "description": "Simulated outpatient visit records.",
    "url": "https://example.com/clinic-visits.tar.gz",
    "sha256": "b" * 64,
    "size_bytes": 3_145_728,
    "base_format_version": 8,
    "configs": ["source.yaml", "streaming.yaml"],
    "commands": [
        "fabulexa-forge export {dir}/source.yaml --out out/",
        "fabulexa-forge stream --config={dir}/streaming.yaml",
    ],
}


def demo_shipped_manifest() -> None:
    """Load the shipped manifest offline and render the empty catalog."""
    manifest = load_manifest()
    assert manifest.datasets == [], (
        "shipped manifest is expected to ship empty this sprint"
    )

    print("--- shipped manifest: text ---")
    print(render_dataset_listing(manifest, "text"))
    print("--- shipped manifest: json ---")
    print(render_dataset_listing(manifest, "json"))


def demo_populated_manifest() -> None:
    """Construct a populated manifest in memory and render both formats."""
    manifest = DatasetManifest.model_validate(
        {"datasets": [POPULATED_ENTRY_ONE, POPULATED_ENTRY_TWO]}
    )

    print("--- populated manifest: text ---")
    print(render_dataset_listing(manifest, "text"))
    print("--- populated manifest: json ---")
    print(render_dataset_listing(manifest, "json"))


def demo_validator_refusals() -> None:
    """Show the DatasetEntry validator refusing malformed entries."""
    cases = {
        "bad slug": {**POPULATED_ENTRY_ONE, "name": "Retail_Week"},
        "http url": {**POPULATED_ENTRY_ONE, "url": "http://example.com/x.tar.gz"},
        "path-separator config": {
            **POPULATED_ENTRY_ONE,
            "configs": ["sub/dimensional.yaml"],
        },
        "foreign placeholder": {
            **POPULATED_ENTRY_ONE,
            "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out {out}"],
        },
    }
    print("--- validator refusals ---")
    for label, entry in cases.items():
        try:
            DatasetEntry.model_validate(entry)
        except ValidationError as exc:
            print(f"{label}: refused ({exc.error_count()} error(s))")
        else:
            raise AssertionError(f"{label}: expected a ValidationError")


def main() -> int:
    demo_shipped_manifest()
    demo_populated_manifest()
    demo_validator_refusals()
    print("SUCCESS: manifest models, loader, and listing renderer behave as specified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
