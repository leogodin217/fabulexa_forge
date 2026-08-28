"""YAML loader for the export and streaming configurations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from fabulexa_forge.config.models import CorruptConfig, ExportConfig, StreamConfig
from fabulexa_forge.errors import ConfigError


class _DuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that refuses duplicate mapping keys at any depth.

    Overrides construct_mapping — invoked for every mapping node, including
    ones nested inside sequences — so the refusal applies uniformly regardless
    of depth.
    """

    def __init__(self, stream: str, label: str, path: Path) -> None:
        super().__init__(stream)
        self._label = label
        self._path = path

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                line = key_node.start_mark.line + 1
                raise ConfigError(
                    f"duplicate key '{key}' in {self._label} {self._path} "
                    f"at line {line}"
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_yaml_mapping(raw: str, label: str, path: Path) -> object:
    """Parse config YAML, refusing duplicate mapping keys.

    The shared parse step for the export, streaming, and corrupt loaders.
    Duplicate keys are refused rather than resolved last-wins.

    Args:
        raw: The file's text.
        label: The config kind, for the message ('export config',
            'stream config', 'corrupt config').
        path: The file's path, named in the message.

    Returns:
        The parsed YAML document.

    Raises:
        ConfigError: The text is not valid YAML, or a mapping carries the same
            key twice at any depth. The duplicate-key message is
            "duplicate key '{key}' in {label} {path} at line {line}".
    """
    loader = _DuplicateKeyLoader(raw, label, path)
    try:
        return loader.get_single_data()
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {label} {path}: {exc}") from exc
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]


def load_export_config(path: Path) -> ExportConfig:
    """Load and parse a YAML export config.

    Args:
        path: Path to the export-config YAML file.

    Returns:
        A validated ExportConfig.

    Raises:
        ConfigError: The file is missing, not valid YAML, has a duplicate
            mapping key, or fails Pydantic validation (unknown field, missing
            required field, or a model validator — e.g. a column with zero or
            multiple source modes).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"export config file not found: {path}") from None

    data = load_yaml_mapping(raw, "export config", path)

    try:
        return ExportConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"export config validation failed: {exc}") from exc


def load_stream_config(path: Path) -> StreamConfig:
    """Load and parse a streaming-config YAML file.

    The streaming sibling of load_export_config: read the file, parse YAML,
    validate as a StreamConfig. Hard-bound to StreamConfig — no mode dispatch
    (streaming is a sibling envelope, not a mode of ExportConfig). The
    read-YAML → StreamConfig.model_validate → ConfigError path mirrors the
    shipped loader exactly.

    Args:
        path: Path to the streaming-config YAML file.

    Returns:
        A validated StreamConfig.

    Raises:
        ConfigError: The file is missing, is not valid YAML, has a duplicate
            mapping key, or fails Pydantic validation (unknown / missing
            field, or a model validator — e.g. an empty or duplicate `kinds`,
            or a prop__-prefixed property name).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"stream config file not found: {path}") from None

    data = load_yaml_mapping(raw, "stream config", path)

    try:
        return StreamConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"stream config validation failed: {exc}") from exc


def load_corrupt_config(path: Path) -> CorruptConfig:
    """Load and parse a YAML corrupter config.

    The corrupter sibling of load_export_config / load_stream_config: read the
    file, parse YAML, validate as a CorruptConfig — the same read →
    model_validate → ConfigError shape, hard-bound to CorruptConfig (corrupting
    is not a mode).

    Args:
        path: Path to the corrupter-config YAML file.

    Returns:
        A validated CorruptConfig.

    Raises:
        ConfigError: The file is missing, is not valid YAML, has a duplicate
            mapping key, or fails Pydantic validation (unknown / missing
            field, or a model validator — e.g. an empty operations list, an
            amount with neither or both of rate / count, or a schema_drift
            with a row filter).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"corrupt config file not found: {path}") from None

    data = load_yaml_mapping(raw, "corrupt config", path)

    try:
        return CorruptConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"corrupt config validation failed: {exc}") from exc
