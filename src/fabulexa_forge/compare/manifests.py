"""Companion-manifest row-count diff: the compare surface's second mode.

`diff_manifest_row_counts()` reads two `<prefix>-manifest.json` companion
documents — the previously published render's and a fresh render's — and
reports, per table, how the row count moved: unchanged, changed (by how
much), appeared, disappeared, or *zeroed* (rows before, none now). It is the
QA round's first look at a re-export before anyone opens the data: a delta
is information for a human to judge; a table going to zero is a verdict.

Like `compare_datasets`, this is a pure two-input surface — two JSON files,
no emit, no bundle, no DuckDB session. It reads only `tables[].name` and
`tables[].row_count` (`docs/architecture/companion-artifacts.md` § The
manifest), so a manifest of any companion format version that carries those
two fields diffs cleanly.

See `docs/architecture/compare.md` § The manifest row-count diff for the
semantic authority.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.compare.errors import CompareInputError

if TYPE_CHECKING:
    from pathlib import Path


RowCountStatus = Literal["unchanged", "changed", "appeared", "disappeared", "zeroed"]


@dataclass(frozen=True)
class RowCountDelta:
    """One table's row-count movement between the previous and fresh manifests.

    `previous` / `fresh` are None when the table is absent from that side;
    `delta` is `fresh - previous` when both are present, else None. `zeroed`
    is the one status that is a failure: rows before, none now.
    """

    table: str
    previous: int | None
    fresh: int | None
    delta: int | None
    status: RowCountStatus


@dataclass(frozen=True)
class RowCountDiff:
    """The row-count diff for one manifest pair.

    `zeroed` is True iff any table's status is `zeroed` — the verdict the CLI
    exit code carries. Every other movement is informational.
    """

    previous: str
    fresh: str
    tables: tuple[RowCountDelta, ...]  # ordered by table name
    zeroed: bool


def _load_row_counts(path: "Path", side: str) -> dict[str, int]:
    """Read `{table: row_count}` from one companion manifest.

    Raises:
        CompareInputError: the file is unreadable or not JSON, or its
            `tables` entries do not each carry a string `name` and an
            integer `row_count`.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompareInputError(
            f"{side} manifest is not readable JSON: {path}"
        ) from exc
    tables = document.get("tables") if isinstance(document, dict) else None
    if not isinstance(tables, list):
        raise CompareInputError(f"{side} manifest has no `tables` list: {path}")
    counts: dict[str, int] = {}
    for entry in tables:
        name = entry.get("name") if isinstance(entry, dict) else None
        row_count = entry.get("row_count") if isinstance(entry, dict) else None
        if not isinstance(name, str) or type(row_count) is not int:
            raise CompareInputError(
                f"{side} manifest has a `tables` entry without a string `name`"
                f" and integer `row_count`: {path}"
            )
        counts[name] = row_count
    return counts


def _delta(table: str, previous: int | None, fresh: int | None) -> RowCountDelta:
    if previous is None:
        assert fresh is not None
        return RowCountDelta(table, None, fresh, None, "appeared")
    if fresh is None:
        return RowCountDelta(table, previous, None, None, "disappeared")
    delta = fresh - previous
    if delta == 0:
        status: RowCountStatus = "unchanged"
    elif fresh == 0:
        status = "zeroed"
    else:
        status = "changed"
    return RowCountDelta(table, previous, fresh, delta, status)


def diff_manifest_row_counts(previous: "Path", fresh: "Path") -> RowCountDiff:
    """Diff per-table row counts between two companion manifests.

    Args:
        previous: The previously published render's `<prefix>-manifest.json`.
        fresh: The fresh render's `<prefix>-manifest.json`.

    Returns:
        One `RowCountDelta` per table in the union of both manifests'
        table names, ordered by name, plus the `zeroed` verdict.

    Raises:
        CompareInputError: Either manifest is unreadable or lacks the
            `tables[].name` / `tables[].row_count` fields.
    """
    before = _load_row_counts(previous, "previous")
    after = _load_row_counts(fresh, "fresh")
    tables = tuple(
        _delta(name, before.get(name), after.get(name))
        for name in sorted(before.keys() | after.keys())
    )
    return RowCountDiff(
        previous=str(previous),
        fresh=str(fresh),
        tables=tables,
        zeroed=any(t.status == "zeroed" for t in tables),
    )


def render_row_count_diff_text(diff: RowCountDiff) -> str:
    """Render a `RowCountDiff` as the CLI's human-readable report.

    One verdict line, then one line per table: name, previous -> fresh,
    signed delta where both sides exist, and the status.
    """
    lines = ["ZEROED" if diff.zeroed else "NO TABLE WENT TO ZERO"]
    for t in diff.tables:
        previous = "-" if t.previous is None else str(t.previous)
        fresh = "-" if t.fresh is None else str(t.fresh)
        delta = "" if t.delta is None else f" ({t.delta:+d})"
        lines.append(f"  {t.table}: {previous} -> {fresh}{delta} {t.status}")
    return "\n".join(lines)


def render_row_count_diff_json(diff: RowCountDiff) -> str:
    """Render a `RowCountDiff` as byte-stable JSON (sorted keys, fixed
    separators; `null` for None, arrays for tuples)."""
    return json.dumps(asdict(diff), sort_keys=True, separators=(",", ":"))
