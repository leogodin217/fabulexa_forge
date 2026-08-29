"""Companion manifest builder: the deterministic `<prefix>-manifest.json` document.

Assembles the manifest's field set
(`docs/architecture/companion-artifacts.md` § The manifest) from the emit,
config, resolved anchor, and one invocation's
`ExportReport`, and owns the pinned byte serialization every companion write
renders through. Format version 2 adds the machine-readable documentation
mirror -- top-level `scenario_description`, per-table `description`,
per-column `description` / `unit` / `enum_options` -- resolved through
`emit.sidecar.documentation()` via the report's carried provenance
(`companion/dictionary.py`), the same resolution the README renders.
Absent -> JSON `null` (the manifest's stable-field-set posture).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fabulexa_forge import __version__
from fabulexa_forge.anchor import anchor_to_json
from fabulexa_forge.exporters.companion.dictionary import (
    resolve_column_doc,
    resolve_column_enum_options,
    resolve_table_description,
)
from fabulexa_forge.reader.emit import compute_sidecar_sha256

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.companion.artifacts import WindowedArtifactState
    from fabulexa_forge.exporters.query_spec import ExportReport, TableKeys, TableReport
    from fabulexa_forge.reader.documentation import Documentation
    from fabulexa_forge.reader.emit import Emit

_MANIFEST_FORMAT_VERSION = 2
"""The manifest's own format version -- mode-definitional, like the event
log's dense first id: every manifest of this design renders it identically."""


@dataclass(frozen=True)
class RuntimeIdentity:
    """The sidecar's wallclock anchor block, as reported by `EmitIdentity`."""

    timezone: str
    start_datetime: str


@dataclass(frozen=True)
class EmitIdentity:
    """The manifest's `emit` block, also reused by the README's
    emit-identity section.

    `sidecar_sha256` is base.json's SHA-256 hex digest; `fork_path` /
    `slice_at` are the emit's sole branch's; `runtime` is the sidecar's
    wallclock anchor block, or None when the emit declares none.
    """

    base_format_version: int
    sidecar_sha256: str
    fork_path: str
    slice_at: int
    runtime: RuntimeIdentity | None


def build_emit_identity(emit: "Emit") -> EmitIdentity:
    """Compute one emit's identity facts, shared verbatim by the manifest's
    `emit` block and the README's emit-identity section.

    Args:
        emit: The open emit.

    Returns:
        The emit's identity facts.
    """
    sidecar = emit.sidecar
    branch = sidecar.branches()[0]
    runtime = sidecar.runtime()
    return EmitIdentity(
        base_format_version=sidecar.base_format_version,
        sidecar_sha256=compute_sidecar_sha256(emit),
        fork_path=branch.fork_path,
        slice_at=branch.slice_at,
        runtime=(
            None
            if runtime is None
            else RuntimeIdentity(
                timezone=runtime.timezone, start_datetime=runtime.start_datetime
            )
        ),
    )


def _emit_identity_json(identity: EmitIdentity) -> dict[str, object]:
    """The manifest's `emit` block as a JSON-serializable mapping."""
    return {
        "base_format_version": identity.base_format_version,
        "sidecar_sha256": identity.sidecar_sha256,
        "fork_path": identity.fork_path,
        "slice_at": identity.slice_at,
        "runtime": (
            None
            if identity.runtime is None
            else {
                "timezone": identity.runtime.timezone,
                "start_datetime": identity.runtime.start_datetime,
            }
        ),
    }


def _keys_json(
    keys: "TableKeys | None",
) -> tuple[list[str] | None, list[list[str]] | None]:
    """The manifest's `(primary_key, unique)` pair for one table's declared keys.

    Args:
        keys: The table's declared TableKeys, or None when nothing was
            declared or the declaration was CSV-dropped.

    Returns:
        `(None, None)` when `keys` is None; otherwise the primary key and
        unique-constraint column lists, list-of-lists for `unique` since each
        entry may be composite.
    """
    if keys is None:
        return None, None
    return list(keys.primary_key), [list(columns) for columns in keys.unique]


def _column_json(
    doc: "Documentation", table: "TableReport", name: str, type_text: str
) -> dict[str, object]:
    """One `columns` entry: name, type, and its resolved documentation mirror."""
    column_doc = resolve_column_doc(doc, table, name, type_text)
    enum_options = [
        {"value": option.value, "description": option.description}
        for option in resolve_column_enum_options(doc, table, name)
    ]
    return {
        "name": name,
        "type": type_text,
        "description": None if column_doc is None else column_doc.description,
        "unit": None if column_doc is None else column_doc.unit,
        "enum_options": enum_options or None,
    }


def _table_json(doc: "Documentation", table: "TableReport") -> dict[str, object]:
    """One `tables` entry: name, forwarded description, documented columns,
    declared keys, row count."""
    primary_key, unique = _keys_json(table.keys)
    return {
        "name": table.name,
        "description": resolve_table_description(doc, table),
        "columns": [
            _column_json(doc, table, name, type_text)
            for name, type_text in table.columns
        ],
        "primary_key": primary_key,
        "unique": unique,
        "row_count": table.row_count,
    }


def _incremental_json(
    windowed: "WindowedArtifactState | None",
) -> dict[str, object] | None:
    """The manifest's `incremental` block, or None on a full export."""
    if windowed is None:
        return None
    return {
        "regime": windowed.regime,
        "label": windowed.label,
        "next_window_index": windowed.next_window_index,
    }


def build_manifest_document(
    emit: "Emit",
    config: "ExportConfig",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    report: "ExportReport",
    windowed: "WindowedArtifactState | None",
) -> dict[str, object]:
    """Assemble the manifest's top-level JSON document.

    Args:
        emit: The open emit (sidecar identity, base.json bytes for hashing).
        config: The validated export config.
        fmt: The resolved output format.
        anchor: The resolved effective anchor, or None.
        report: The invocation's per-table report.
        windowed: Windowed invocation facts, or None for a full export.

    Returns:
        A JSON-serializable mapping of the manifest's field set, ready
        for `render_manifest_bytes`.
    """
    doc = emit.sidecar.documentation()
    return {
        "manifest_format_version": _MANIFEST_FORMAT_VERSION,
        "mode": config.mode,
        "format": fmt,
        "forge_version": __version__,
        "scenario_description": doc.scenario_description(),
        "emit": _emit_identity_json(build_emit_identity(emit)),
        "anchor": anchor_to_json(anchor),
        "config": config.model_dump(mode="json"),
        "incremental": _incremental_json(windowed),
        "tables": [_table_json(doc, table) for table in report.tables],
    }


def render_manifest_bytes(document: dict[str, object]) -> bytes:
    """Serialize a manifest document to its pinned byte form.

    UTF-8, `ensure_ascii` off (non-ASCII survives verbatim), two-space
    indent, sorted object keys, list order preserved (tables, columns), one
    trailing newline. Two renders of the same document are byte-identical.

    Args:
        document: A manifest document from `build_manifest_document`.

    Returns:
        The document's pinned UTF-8 byte encoding.
    """
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    return f"{text}\n".encode("utf-8")
