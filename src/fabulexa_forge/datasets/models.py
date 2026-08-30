"""Pydantic models for the dataset distribution manifest.

`DatasetManifest` is the authored allowlist of published datasets; every
entry is validated at load time so the runtime never sees a malformed
manifest.
"""

from __future__ import annotations

import re

from pydantic import model_validator
from typing_extensions import Self

from fabulexa_forge.config.models import StrictBaseModel

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_MIN_VERSION = 1


class DatasetEntry(StrictBaseModel):
    """One published dataset: identity, pinned bytes, pack contents, next steps."""

    name: str
    description: str
    url: str
    sha256: str
    size_bytes: int
    base_format_version: int
    configs: list[str]
    commands: list[str]

    @model_validator(mode="after")
    def entry_well_formed(self) -> Self:
        """name matches `[a-z0-9]+(-[a-z0-9]+)*` (lowercase alphanumeric runs
        separated by single hyphens); url is https; sha256 is 64 lowercase hex;
        size_bytes and base_format_version are positive; configs and commands
        non-empty; every configs entry is a bare filename ending '.yaml' with
        no path separator ('/' or '\\'); every command contains '{dir}' at
        least once, and every brace-delimited run in it (each match of
        `\\{[^{}]*\\}`) is exactly '{dir}' — no other placeholder exists."""
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(
                f"dataset name {self.name!r} must match {_NAME_RE.pattern}"
            )
        if not self.url.startswith("https://"):
            raise ValueError(
                f"dataset {self.name}: url must be https, got {self.url!r}"
            )
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError(
                f"dataset {self.name}: sha256 must be 64 lowercase hex chars"
            )
        if self.size_bytes <= 0:
            raise ValueError(f"dataset {self.name}: size_bytes must be > 0")
        if self.base_format_version < _MIN_VERSION:
            raise ValueError(
                f"dataset {self.name}: base_format_version must be positive"
            )
        if not self.configs:
            raise ValueError(f"dataset {self.name}: configs must be non-empty")
        if not self.commands:
            raise ValueError(f"dataset {self.name}: commands must be non-empty")
        for config in self.configs:
            if "/" in config or "\\" in config:
                raise ValueError(
                    f"dataset {self.name}: configs entry {config!r} must be a bare "
                    "filename with no path separator"
                )
            if not config.endswith(".yaml"):
                raise ValueError(
                    f"dataset {self.name}: configs entry {config!r} must end '.yaml'"
                )
        for command in self.commands:
            placeholders = _PLACEHOLDER_RE.findall(command)
            if "{dir}" not in placeholders:
                raise ValueError(
                    f"dataset {self.name}: command {command!r} must contain '{{dir}}'"
                )
            foreign = [p for p in placeholders if p != "{dir}"]
            if foreign:
                raise ValueError(
                    f"dataset {self.name}: command {command!r} has foreign "
                    f"placeholder {foreign[0]!r}"
                )
        return self


class DatasetManifest(StrictBaseModel):
    """The authored allowlist of published datasets, in authored order."""

    datasets: list[DatasetEntry]

    @model_validator(mode="after")
    def names_unique(self) -> Self:
        """Manifest entry names are unique."""
        names = [entry.name for entry in self.datasets]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ValueError(f"duplicate dataset name: {name!r}")
            seen.add(name)
        return self
