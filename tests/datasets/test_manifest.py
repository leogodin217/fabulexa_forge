"""Tests for the dataset manifest loader and listing renderer."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.datasets.manifest import load_manifest, render_dataset_listing
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

ENTRY_ONE = DatasetEntry.model_validate(
    {
        "name": "retail-week",
        "description": "One week of retail transactions.",
        "url": "https://example.com/retail-week.tar.gz",
        "sha256": "a" * 64,
        "size_bytes": 2048,
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "configs": ["dimensional.yaml"],
        "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
    }
)
ENTRY_TWO = DatasetEntry.model_validate(
    {
        "name": "clinic-visits",
        "description": "Simulated outpatient visit records.",
        "url": "https://example.com/clinic-visits.tar.gz",
        "sha256": "b" * 64,
        "size_bytes": 3_145_728,
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "configs": ["source.yaml", "streaming.yaml"],
        "commands": [
            "fabulexa-forge export {dir}/source.yaml --out out/",
            "fabulexa-forge stream {dir}/streaming.yaml",
        ],
    }
)


def test_load_manifest_returns_populated_catalog() -> None:
    """load_manifest() returns the shipped catalog, offline: populated with the
    packs rebuilt at the supported version, empty_reason omitted."""
    manifest = load_manifest()
    assert [entry.name for entry in manifest.datasets] == [
        "airline-ops",
        "saas-billing",
        "nhs",
    ]
    assert manifest.empty_reason is None


def test_empty_reason_valid_on_empty_catalog() -> None:
    """An empty catalog may declare empty_reason; the value is retained."""
    manifest = DatasetManifest.model_validate(
        {"datasets": [], "empty_reason": "packs pending rebuild"}
    )
    assert manifest.empty_reason == "packs pending rebuild"


def test_empty_reason_rejected_on_populated_catalog() -> None:
    """A populated catalog carrying empty_reason is a validation error."""
    with pytest.raises(ValidationError, match="only valid when datasets is empty"):
        DatasetManifest.model_validate(
            {
                "datasets": [ENTRY_ONE.model_dump()],
                "empty_reason": "packs pending rebuild",
            }
        )


def test_blank_or_multiline_empty_reason_rejected() -> None:
    """A blank or multi-line empty_reason is a validation error."""
    for reason in ("   ", "two\nlines"):
        with pytest.raises(ValidationError, match="non-blank single line"):
            DatasetManifest.model_validate({"datasets": [], "empty_reason": reason})


def test_render_of_shipped_catalog_lists_every_entry() -> None:
    """The shipped catalog's text render names every entry, in authored order."""
    text = render_dataset_listing(load_manifest(), "text")
    assert text.index("airline-ops") < text.index("saas-billing") < text.index("nhs")


def test_text_render_of_empty_manifest_is_no_datasets_line() -> None:
    """Text render of an empty manifest is the no-datasets line."""
    manifest = DatasetManifest.model_validate({"datasets": []})
    assert (
        render_dataset_listing(manifest, "text")
        == "no datasets published for this version"
    )


def test_text_render_carries_declared_empty_reason() -> None:
    """Text render of an empty manifest with empty_reason prints the reason
    under the no-datasets line — intentional emptiness is distinguishable."""
    manifest = DatasetManifest.model_validate(
        {"datasets": [], "empty_reason": "packs pending rebuild"}
    )
    assert (
        render_dataset_listing(manifest, "text")
        == "no datasets published for this version\npacks pending rebuild"
    )


def test_json_render_of_empty_manifest() -> None:
    """JSON render of an empty, unreasoned manifest is exactly {"datasets":[]} —
    the undeclared empty_reason is excluded, not rendered null."""
    manifest = DatasetManifest.model_validate({"datasets": []})
    assert render_dataset_listing(manifest, "json") == '{"datasets":[]}'


def test_json_render_carries_declared_empty_reason() -> None:
    """JSON render of an empty manifest with empty_reason carries it verbatim."""
    manifest = DatasetManifest.model_validate(
        {"datasets": [], "empty_reason": "packs pending rebuild"}
    )
    assert (
        render_dataset_listing(manifest, "json")
        == '{"datasets":[],"empty_reason":"packs pending rebuild"}'
    )


def test_text_render_lists_entries_in_authored_order() -> None:
    """Text render of a two-entry manifest lists both, in authored order."""
    manifest = DatasetManifest.model_validate(
        {"datasets": [ENTRY_ONE.model_dump(), ENTRY_TWO.model_dump()]}
    )
    text = render_dataset_listing(manifest, "text")
    assert text.index("retail-week") < text.index("clinic-visits")
    assert f"base_format_version={SUPPORTED_BASE_FORMAT_VERSION}" in text
    assert "dimensional.yaml" in text
    assert "source.yaml, streaming.yaml" in text
    assert "One week of retail transactions." in text
    assert "Simulated outpatient visit records." in text


def test_json_render_is_byte_stable() -> None:
    """JSON render uses sorted keys, (",", ":") separators, no trailing newline,
    authored entry order, and size_bytes as an integer."""
    manifest = DatasetManifest.model_validate(
        {"datasets": [ENTRY_ONE.model_dump(), ENTRY_TWO.model_dump()]}
    )
    rendered = render_dataset_listing(manifest, "json")
    assert not rendered.endswith("\n")
    assert ", " not in rendered
    assert ": " not in rendered
    document = json.loads(rendered)
    assert [d["name"] for d in document["datasets"]] == ["retail-week", "clinic-visits"]
    assert isinstance(document["datasets"][0]["size_bytes"], int)
    parsed_keys = list(json.loads(rendered)["datasets"][0].keys())
    assert parsed_keys == sorted(parsed_keys)


def test_json_render_is_deterministic_across_calls() -> None:
    """Rendering twice yields identical bytes."""
    manifest = DatasetManifest.model_validate({"datasets": [ENTRY_ONE.model_dump()]})
    first = render_dataset_listing(manifest, "json")
    second = render_dataset_listing(manifest, "json")
    assert first == second


def test_unknown_format_rejected() -> None:
    """An fmt other than 'text' or 'json' raises ValueError."""
    manifest = DatasetManifest.model_validate({"datasets": []})
    with pytest.raises(ValueError, match="unknown listing format"):
        render_dataset_listing(manifest, "xml")
