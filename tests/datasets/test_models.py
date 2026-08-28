"""Tests for dataset manifest model parse-time validators.

Each test asserts on model behavior (structural constraints), not that
Pydantic parses successfully — the invariants are tested, not the library.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.datasets.models import DatasetEntry, DatasetManifest

VALID_ENTRY: dict[str, Any] = {
    "name": "retail-week",
    "description": "One week of retail transactions.",
    "url": "https://example.com/retail-week.tar.gz",
    "sha256": "a" * 64,
    "size_bytes": 1024,
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "configs": ["dimensional.yaml"],
    "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
}


def _entry(**overrides: Any) -> dict[str, Any]:
    return {**VALID_ENTRY, **overrides}


_NON_POSITIVE_VERSION = SUPPORTED_BASE_FORMAT_VERSION - SUPPORTED_BASE_FORMAT_VERSION


def test_valid_entry_passes() -> None:
    """A fully well-formed entry passes validation."""
    entry = DatasetEntry.model_validate(VALID_ENTRY)
    assert entry.name == "retail-week"


@pytest.mark.parametrize(
    "name", ["Retail-Week", "retail_week", "-retail-week", "retail-week-"]
)
def test_bad_name_rejected(name: str) -> None:
    """Uppercase, underscore, and leading/trailing hyphen names are rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(name=name))


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"url": "http://example.com/x.tar.gz"}, id="non_https_url"),
        pytest.param({"sha256": "a" * 63}, id="sha256_wrong_length"),
        pytest.param({"sha256": "A" * 64}, id="sha256_uppercase"),
        pytest.param({"size_bytes": 0}, id="size_bytes_zero"),
        pytest.param({"size_bytes": -1}, id="size_bytes_negative"),
        pytest.param(
            {"base_format_version": _NON_POSITIVE_VERSION},
            id="base_format_version_zero",
        ),
        pytest.param({"configs": []}, id="empty_configs"),
        pytest.param({"commands": []}, id="empty_commands"),
        pytest.param({"configs": ["dimensional.yml"]}, id="configs_not_yaml_suffix"),
        pytest.param(
            {"commands": ["fabulexa-forge export x.yaml"]},
            id="command_without_dir_placeholder",
        ),
        pytest.param(
            {"commands": ["fabulexa-forge export {dir}/x.yaml --out {out}"]},
            id="command_with_foreign_placeholder",
        ),
    ],
)
def test_bad_field_rejected(override: dict[str, Any]) -> None:
    """One field broken (all others valid) is rejected, one rule per case."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(**override))


@pytest.mark.parametrize(
    "bad_config", ["sub/dimensional.yaml", "sub\\dimensional.yaml"]
)
def test_configs_path_separator_rejected(bad_config: str) -> None:
    """A configs entry with a path separator is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(configs=[bad_config]))


def test_duplicate_entry_names_rejected() -> None:
    """Duplicate entry names across the manifest are rejected."""
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate({"datasets": [VALID_ENTRY, VALID_ENTRY]})


def test_unknown_field_rejected() -> None:
    """An unknown field on an entry is rejected (strict model)."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(extra_field="nope"))
