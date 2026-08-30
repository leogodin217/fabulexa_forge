"""Hygiene tests for the shipped dataset manifest.

Business rules enforced by tests, not the runtime — the runtime never sees a
manifest that violates them, since the manifest ships pre-validated in the
wheel. Each check is exercised against the shipped manifest and against
constructed violating manifests, so the checks are proven non-vacuous.
"""

from __future__ import annotations

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.datasets.manifest import load_manifest
from fabulexa_forge.datasets.models import DatasetManifest

BASE_ENTRY = {
    "name": "retail-week",
    "description": "One week of retail transactions.",
    "url": "https://example.com/retail-week.tar.gz",
    "sha256": "a" * 64,
    "size_bytes": 2048,
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "configs": ["dimensional.yaml"],
    "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
}


def _manifest_with(**overrides: object) -> DatasetManifest:
    entry = {**BASE_ENTRY, **overrides}
    return DatasetManifest.model_validate({"datasets": [entry]})


def _check_version_agreement(manifest: DatasetManifest) -> list[str]:
    """Every entry's base_format_version equals SUPPORTED_BASE_FORMAT_VERSION."""
    errors = []
    for entry in manifest.datasets:
        if entry.base_format_version != SUPPORTED_BASE_FORMAT_VERSION:
            errors.append(
                f"dataset {entry.name} is stale: pack is v{entry.base_format_version}, "
                f"wheel supports v{SUPPORTED_BASE_FORMAT_VERSION}"
            )
    return errors


def _dir_relative_yaml_refs(command: str) -> list[str]:
    """Every '{dir}/'-prefixed path run in a command whose final segment ends '.yaml'.

    Covers '='-attached forms (e.g. '--config={dir}/x.yaml') by starting the
    path run at '{dir}/', not at the maximal non-whitespace token.
    """
    refs = []
    for token in command.split():
        start = token.find("{dir}/")
        while start != -1:
            path_run = token[start:]
            if path_run.endswith(".yaml"):
                refs.append(path_run.rsplit("/", 1)[-1])
            start = token.find("{dir}/", start + 1)
    return refs


def _check_command_config_coherence(manifest: DatasetManifest) -> list[str]:
    """Every command's {dir}/-relative .yaml reference names a configs entry."""
    errors = []
    for entry in manifest.datasets:
        for command in entry.commands:
            for file in _dir_relative_yaml_refs(command):
                if file not in entry.configs:
                    errors.append(
                        f"dataset {entry.name}: command references {file} not in configs"
                    )
    return errors


def test_shipped_manifest_version_agreement() -> None:
    """Every shipped-manifest entry's base_format_version matches the supported version."""
    assert _check_version_agreement(load_manifest()) == []


def test_shipped_manifest_command_config_coherence() -> None:
    """Every shipped-manifest command's {dir}/ .yaml reference is in that entry's configs."""
    assert _check_command_config_coherence(load_manifest()) == []


def test_version_agreement_passes_when_matching() -> None:
    """A constructed manifest with a matching version passes the check."""
    manifest = _manifest_with(base_format_version=SUPPORTED_BASE_FORMAT_VERSION)
    assert _check_version_agreement(manifest) == []


def test_version_agreement_detects_stale_entry() -> None:
    """A constructed manifest with a stale version is caught, naming the entry."""
    increment = 1
    stale_version = SUPPORTED_BASE_FORMAT_VERSION + increment
    manifest = _manifest_with(base_format_version=stale_version)
    errors = _check_version_agreement(manifest)
    assert len(errors) == 1
    assert "retail-week is stale" in errors[0]
    assert f"pack is v{stale_version}" in errors[0]
    assert f"wheel supports v{SUPPORTED_BASE_FORMAT_VERSION}" in errors[0]


def test_command_config_coherence_passes_for_covered_reference() -> None:
    """A command referencing a config that is listed passes the check."""
    manifest = _manifest_with(
        configs=["dimensional.yaml"],
        commands=["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
    )
    assert _check_command_config_coherence(manifest) == []


def test_command_config_coherence_covers_equals_attached_form() -> None:
    """An '='-attached {dir}/ reference (--config={dir}/x.yaml) is covered."""
    manifest = _manifest_with(
        configs=["dimensional.yaml"],
        commands=["fabulexa-forge export --config={dir}/dimensional.yaml"],
    )
    assert _check_command_config_coherence(manifest) == []


def test_command_config_coherence_detects_uncovered_reference() -> None:
    """A command referencing a .yaml file absent from configs is caught."""
    manifest = _manifest_with(
        configs=["dimensional.yaml"],
        commands=["fabulexa-forge export {dir}/other.yaml --out out/"],
    )
    errors = _check_command_config_coherence(manifest)
    assert errors == [
        "dataset retail-week: command references other.yaml not in configs"
    ]
