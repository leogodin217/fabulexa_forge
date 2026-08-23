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


def test_non_https_url_rejected() -> None:
    """A non-https url is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(url="http://example.com/x.tar.gz"))


def test_sha256_wrong_length_rejected() -> None:
    """A sha256 not 64 hex chars is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(sha256="a" * 63))


def test_sha256_uppercase_rejected() -> None:
    """An uppercase sha256 is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(sha256="A" * 64))


def test_size_bytes_zero_rejected() -> None:
    """size_bytes of 0 is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(size_bytes=0))


def test_size_bytes_negative_rejected() -> None:
    """A negative size_bytes is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(size_bytes=-1))


def test_base_format_version_zero_rejected() -> None:
    """A non-positive base_format_version is rejected."""
    non_positive = 0
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(base_format_version=non_positive))


def test_empty_configs_rejected() -> None:
    """An empty configs list is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(configs=[]))


def test_empty_commands_rejected() -> None:
    """An empty commands list is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(commands=[]))


@pytest.mark.parametrize(
    "bad_config", ["sub/dimensional.yaml", "sub\\dimensional.yaml"]
)
def test_configs_path_separator_rejected(bad_config: str) -> None:
    """A configs entry with a path separator is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(configs=[bad_config]))


def test_configs_not_yaml_suffix_rejected() -> None:
    """A configs entry not ending '.yaml' is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(configs=["dimensional.yml"]))


def test_command_without_dir_placeholder_rejected() -> None:
    """A command missing the {dir} placeholder is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(commands=["fabulexa-forge export x.yaml"]))


def test_command_with_foreign_placeholder_rejected() -> None:
    """A command with a placeholder other than {dir} is rejected."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(
            _entry(commands=["fabulexa-forge export {dir}/x.yaml --out {out}"])
        )


def test_duplicate_entry_names_rejected() -> None:
    """Duplicate entry names across the manifest are rejected."""
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate({"datasets": [VALID_ENTRY, VALID_ENTRY]})


def test_unknown_field_rejected() -> None:
    """An unknown field on an entry is rejected (strict model)."""
    with pytest.raises(ValidationError):
        DatasetEntry.model_validate(_entry(extra_field="nope"))
