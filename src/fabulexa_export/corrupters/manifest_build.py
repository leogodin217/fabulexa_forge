"""Canonicalisation, id assignment, and byte-deterministic serialization of
the defect manifest.

The assembly side of the corrupter engine's one seam: operations emit
`DefectRecord`s (`manifest.py`); this module sorts them into the canonical
total order, assigns each a deterministic `defect_id`, computes the summary
counts, and writes `defects.json`. See
`docs/architecture/pending/corrupter-engine-and-manifest.md` § Determinism
and canonical ordering / § The occurrence ordinal and defect_id (normative).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path

from fabulexa_export.corrupters.manifest import (
    DEFECT_MANIFEST_VERSION,
    IDENTIFIER_PATTERN,
    ColumnLocator,
    DefectCounts,
    DefectManifest,
    DefectRecord,
    DefectSource,
    Locator,
    ManifestDefect,
    RowCategory,
    RowLocator,
    RowRef,
)
from fabulexa_export.errors import CorruptError, ExportRuntimeError

_TABLE_NAME_RE = re.compile(
    rf"^history$|^records__{IDENTIFIER_PATTERN}$"
    rf"|^membership__{IDENTIFIER_PATTERN}__{IDENTIFIER_PATTERN}$"
)


def _table_category(table: str) -> RowCategory | None:
    """Return the RowCategory a locator's table name implies, or None.

    Purely lexical: `history` -> history; `records__<seg>` -> records;
    `membership__<seg>__<seg>` -> membership; anything else is malformed.

    Args:
        table: The locator's table name.

    Returns:
        The implied RowCategory, or None when the name is malformed.
    """
    if not _TABLE_NAME_RE.match(table):
        return None
    if table == "history":
        return "history"
    if table.startswith("records__"):
        return "records"
    return "membership"


def _check_well_formed_table_name(table: str) -> None:
    """WellFormedTableName: table matches the locator table-name grammar.

    Args:
        table: The locator's table name.

    Raises:
        CorruptError: table matches no recognized shape.
    """
    if _table_category(table) is None:
        raise CorruptError(f"malformed locator table '{table}'")


def _check_rowref_category_matches_table(table: str, category: RowCategory) -> None:
    """RowRefCategoryMatchesTable: RowRef.category agrees with the table name.

    Args:
        table: The locator's table name (already well-formed).
        category: The RowRef's declared category.

    Raises:
        CorruptError: category disagrees with the category the table name
            implies.
    """
    expected = _table_category(table)
    if expected != category:
        raise CorruptError(f"row category '{category}' does not match table '{table}'")


def _locator_row_ref(location: Locator) -> RowRef | None:
    """Return the locator's RowRef, or None for a column locator (no RowRef)."""
    if isinstance(location, ColumnLocator):
        return None
    return location.row


def _locator_column(location: Locator) -> str:
    """Return the locator's column name, or "" for a row locator (none)."""
    if isinstance(location, RowLocator):
        return ""
    return location.column


def canonical_sort_key(
    record: DefectRecord,
) -> tuple[str, int, tuple[tuple[str, str], ...], str, str, str, tuple[str, ...]]:
    """The canonical total order key for one defect record.

    `(table, locator-kind rank [column < row < cell], RowRef.keys tuple,
    column-or-empty, class, rule, impact)` -- impact compares as its
    normalized (sorted, deduplicated) code sequence, so it is a stable final
    discriminator.

    Args:
        record: The defect record (pre-id).

    Returns:
        The sort key tuple.
    """
    location = record.location
    kind_rank = {"column": 0, "row": 1, "cell": 2}[location.kind]
    row_ref = _locator_row_ref(location)
    row_keys = row_ref.keys if row_ref is not None else ()
    return (
        location.table,
        kind_rank,
        row_keys,
        _locator_column(location),
        record.defect_class,
        record.rule,
        record.impact,
    )


def _group_key(
    record: DefectRecord,
) -> tuple[str, int, tuple[tuple[str, str], ...], str, str, str]:
    """The (class, rule, locator) grouping key -- the sort key minus impact,
    the axis occurrence ordinals are assigned over."""
    return canonical_sort_key(record)[:-1]


def derive_defect_id(record: DefectRecord, ordinal: int) -> str:
    """Compute a defect's deterministic, content-derived id.

    A pure function of the record's class, rule, canonically serialized
    locator, and its occurrence ordinal within the (class, rule, locator)
    group. Stable across re-runs and collision-free within one manifest.

    Args:
        record: The defect record (pre-id).
        ordinal: The 0-based occurrence ordinal in canonical order.

    Returns:
        The defect id.
    """
    locator_json = json.dumps(
        record.location.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = "\x1f".join(
        (record.defect_class, record.rule, locator_json, str(ordinal))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_defect_manifest(
    source: DefectSource,
    config_fingerprint: str,
    code_version: str,
    records: Sequence[DefectRecord],
) -> DefectManifest:
    """Assemble a deterministic manifest from operation-declared defect records.

    Canonicalizes `records` (total order over table, locator-kind rank,
    RowRef.keys, column, class, rule, impact -- residual ties broken by
    occurrence ordinal), assigns each an occurrence ordinal and a content-
    derived `defect_id`, computes the summary counts, stamps
    `defect_manifest_version` from the module constant
    `DEFECT_MANIFEST_VERSION`, and returns the frozen manifest. Pure and
    deterministic: identical inputs yield an identical manifest, with no
    wall-clock or ambient state consulted.

    Args:
        source: Identity of the input base emit (sidecar SHA-256 + version).
        config_fingerprint: SHA-256 of the canonicalized corrupter config.
        code_version: The fabulexa-export package version string.
        records: Every defect declared by every operation in this corrupt
            run, in any order.

    Returns:
        The canonicalized, id-assigned DefectManifest.

    Raises:
        CorruptError: A driver-level build invariant fails on the assembled
            records -- a malformed locator table name (WellFormedTableName),
            a RowRef.category that disagrees with its locator's table
            category (RowRefCategoryMatchesTable), or a defect_id collision
            (UniqueDefectId).
    """
    for record in records:
        _check_well_formed_table_name(record.location.table)
        row_ref = _locator_row_ref(record.location)
        if row_ref is not None:
            _check_rowref_category_matches_table(
                record.location.table, row_ref.category
            )

    ordered = sorted(records, key=canonical_sort_key)

    ordinal_by_group: dict[
        tuple[str, int, tuple[tuple[str, str], ...], str, str, str], int
    ] = {}
    seen_ids: set[str] = set()
    manifest_defects: list[ManifestDefect] = []
    by_class: dict[str, int] = {}
    by_impact: dict[str, int] = {}

    for record in ordered:
        group = _group_key(record)
        ordinal = ordinal_by_group.get(group, 0)
        ordinal_by_group[group] = ordinal + 1

        defect_id = derive_defect_id(record, ordinal)
        if defect_id in seen_ids:
            raise CorruptError(f"duplicate defect_id {defect_id}")
        seen_ids.add(defect_id)

        manifest_defects.append(
            ManifestDefect.model_validate(
                {
                    "class": record.defect_class,
                    "rule": record.rule,
                    "impact": record.impact,
                    "location": record.location,
                    "defect_id": defect_id,
                }
            )
        )
        by_class[record.defect_class] = by_class.get(record.defect_class, 0) + 1
        for code in record.impact:
            by_impact[code] = by_impact.get(code, 0) + 1

    return DefectManifest(
        defect_manifest_version=DEFECT_MANIFEST_VERSION,
        source=source,
        config_fingerprint=config_fingerprint,
        code_version=code_version,
        counts=DefectCounts(by_class=by_class, by_impact=by_impact),
        defects=tuple(manifest_defects),
    )


def write_defect_manifest(manifest: DefectManifest, out_dir: Path) -> Path:
    """Serialize a manifest to `<out_dir>/defects.json`, byte-deterministically.

    Writes JSON with sorted object keys, fixed separators, and a trailing
    newline, so the same manifest always produces the same bytes.

    Args:
        manifest: The assembled manifest.
        out_dir: The corrupt run's output directory (holding the corrupted
            run.duckdb + base.json).

    Returns:
        The path to the written defects.json.

    Raises:
        ExportRuntimeError: Writing `<out_dir>/defects.json` fails.
    """
    path = out_dir / "defects.json"
    text = (
        json.dumps(
            manifest.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )
    try:
        path.write_text(text, encoding="utf-8")
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to write defect manifest {path}: {exc}"
        ) from exc
    return path
