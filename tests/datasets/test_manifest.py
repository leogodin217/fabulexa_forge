"""Tests for the dataset manifest loader and listing renderer."""

from __future__ import annotations

import json

import pytest

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


def test_load_manifest_returns_shipped_empty_manifest() -> None:
    """load_manifest() returns the shipped empty manifest, offline."""
    manifest = load_manifest()
    assert manifest.datasets == []


def test_text_render_of_empty_manifest_is_no_datasets_line() -> None:
    """Text render of an empty manifest is the no-datasets line."""
    manifest = DatasetManifest.model_validate({"datasets": []})
    assert (
        render_dataset_listing(manifest, "text")
        == "no datasets published for this version"
    )


def test_json_render_of_empty_manifest() -> None:
    """JSON render of an empty manifest is exactly {"datasets":[]}."""
    manifest = DatasetManifest.model_validate({"datasets": []})
    assert render_dataset_listing(manifest, "json") == '{"datasets":[]}'


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
