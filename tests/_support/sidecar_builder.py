"""The one sidecar authority for fixture-building test code.

Every fixture sidecar in the test tree is built through `write_emit`; every
value-carrying `prop__` column through `prop_column`. No fixture module writes
or rewrites a `base.json` by hand, and no fixture module fabricates a defective
column pairing except through the deliberate negative form the constructors
expose (a ValueError at construction, never a silently-written defect).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import jsonschema

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader._schema import _load_vendored_schema

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.reader import TemporalClass

UNSUPPORTED_VERSION_SENTINEL = 99
"""The never-valid stand-in every version-gate negative fixture uses.

Never a neighbouring real version — a value the vendored contract has never
defined and never will, so a version-gate test can never be confused with a
future-version compatibility test.
"""

_DEFAULT_BRANCHES: list[dict[str, object]] = [
    {"fork_path": "trunk", "parent": None, "slice_at": 0}
]


def prop_column(
    name: str,
    type: str,
    *,
    history_tracked: bool,
    temporal_class: "TemporalClass",
    references: str | None = None,
) -> dict[str, object]:
    """Build one value-carrying sidecar column.

    The sole constructor for a prop__ column across every fixture builder. Both
    temporal attributes are required and passed together, because the contract
    pairs them: a column carries history_tracked iff it carries temporal_class.
    A future paired attribute changes this one signature, and every call site
    with it.

    The constructor builds only conformant columns — temporal_class is typed to
    the enum, and the contract's implication clauses are validated: 'tracked'
    requires history_tracked True, 'slice_only' requires history_tracked False.
    A negative variant that breaks the pairing, the enum, or an implication
    mutates the returned dict; a defect is never expressible through the
    constructor.

    Args:
        name: Column name, including its prop__ prefix.
        type: DuckDB type literal.
        history_tracked: The column's SCD class (True = type-2, False = type-1).
        temporal_class: The column's point-in-time contract.
        references: The record kind this column's value equality-joins against,
            when the column is a foreign-key projection. Omitted when None.

    Returns:
        A column dict suitable for a table's `columns` list.

    Raises:
        ValueError: temporal_class 'tracked' with history_tracked False, or
            'slice_only' with history_tracked True.
    """
    if temporal_class == "tracked" and not history_tracked:
        raise ValueError(
            f"prop_column {name!r}: temporal_class 'tracked' requires "
            "history_tracked=True"
        )
    if temporal_class == "slice_only" and history_tracked:
        raise ValueError(
            f"prop_column {name!r}: temporal_class 'slice_only' requires "
            "history_tracked=False"
        )

    column: dict[str, object] = {"name": name, "type": type}
    if references is not None:
        column["references"] = references
    column["history_tracked"] = history_tracked
    column["temporal_class"] = temporal_class
    return column


def _validate_against_schema(sidecar: dict[str, object]) -> None:
    """Validate a constructed sidecar against the vendored v5 JSON Schema.

    Args:
        sidecar: The full sidecar dict about to be written.

    Raises:
        ValueError: The sidecar fails schema validation. The message carries
            the failing path and the schema's complaint, naming the field
            rather than surfacing as an unrelated C1 failure at read time.
    """
    schema = _load_vendored_schema()
    try:
        jsonschema.validate(instance=sidecar, schema=schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(part) for part in exc.path) or "<root>"
        raise ValueError(
            f"fixture sidecar failed schema validation at {path!r}: {exc.message}"
        ) from exc


def write_emit(
    dest: "Path",
    *,
    tables: list[dict[str, object]],
    branches: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
    base_format_version: int | None = None,
    schema_valid: bool = True,
) -> None:
    """Write one fixture emit's base.json.

    The sole writer of a fixture sidecar — the wrong-version negative fixture
    included, via the override below; no fixture writes or rewrites a sidecar
    by hand.

    Args:
        dest: The emit directory; base.json is written inside it.
        tables: The sidecar's tables list; value-carrying columns built via
            prop_column.
        branches: The branches list. Defaults to the single-trunk entry.
        extra: Optional top-level sidecar blocks (runtime, pinned_ids,
            enum_domains, record_roles), carried verbatim.
        base_format_version: The version to stamp. None (the default) stamps
            SUPPORTED_BASE_FORMAT_VERSION — the supported version appears as a
            literal nowhere in the test tree. An explicit value exists for the
            version-gate negative fixture alone, which passes
            UNSUPPORTED_VERSION_SENTINEL, composed with schema_valid=False:
            the vendored schema pins the version, so any override is
            schema-invalid by construction.
        schema_valid: When True (the default), validate the result against the
            vendored contract/base-format.schema.json before writing, so a
            fixture that has not learned a new required field fails at
            construction, naming the field, rather than surfacing as an
            unrelated C1 failure at read time. False is reserved for negative
            fixtures whose declared defect is schema-level (a wrong version,
            an out-of-enum class) — they must remain writable, and their
            expectations name the C1 failure.
    """
    version = (
        SUPPORTED_BASE_FORMAT_VERSION
        if base_format_version is None
        else base_format_version
    )
    sidecar: dict[str, object] = {
        "base_format_version": version,
        "branches": branches if branches is not None else _DEFAULT_BRANCHES,
        "tables": tables,
    }
    if extra:
        sidecar.update(extra)

    if schema_valid:
        _validate_against_schema(sidecar)

    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
