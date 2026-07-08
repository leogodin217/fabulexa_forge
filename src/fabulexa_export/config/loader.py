"""YAML loader for the export and streaming configurations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from fabulexa_export.config.models import CorruptConfig, ExportConfig, StreamConfig
from fabulexa_export.errors import ConfigError


def load_export_config(path: Path) -> ExportConfig:
    """Load and parse a YAML export config.

    Args:
        path: Path to the export-config YAML file.

    Returns:
        A validated ExportConfig.

    Raises:
        ConfigError: The file is missing, not valid YAML, or fails Pydantic
            validation (unknown field, missing required field, or a model
            validator — e.g. a column with zero or multiple source modes).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"export config file not found: {path}") from None

    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in export config {path}: {exc}") from exc

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
        ConfigError: The file is missing, is not valid YAML, or fails Pydantic
            validation (unknown / missing field, or a model validator — e.g. an
            empty or duplicate `kinds`, or a prop__-prefixed property name).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"stream config file not found: {path}") from None

    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in stream config {path}: {exc}") from exc

    try:
        return StreamConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"stream config validation failed: {exc}") from exc


def load_corrupt_config(path: Path) -> CorruptConfig:
    """Load and parse a YAML corrupter config.

    The corrupter sibling of load_export_config / load_stream_config: read the file,
    parse YAML, validate as a CorruptConfig — the same read → model_validate →
    ConfigError shape, hard-bound to CorruptConfig (corrupting is not a mode).

    Args:
        path: Path to the corrupter-config YAML file.

    Returns:
        A validated CorruptConfig.

    Raises:
        ConfigError: The file is missing, is not valid YAML, or fails Pydantic
            validation (unknown / missing field, or a model validator — e.g. an
            empty operations list, an amount with neither or both of rate / count,
            or a schema_drift with a row filter).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"corrupt config file not found: {path}") from None

    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in corrupt config {path}: {exc}") from exc

    try:
        return CorruptConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"corrupt config validation failed: {exc}") from exc
