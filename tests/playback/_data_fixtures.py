"""Data-bearing emit scaffold for playback event-stream tests.

`_fixtures.py` is sidecar-only (resolve_selection needs no data); the event
stream reads real rows, so this module builds an in-process emit with actual
`records__<kind>` / `membership__<owner>__<property>` / `history` tables —
generic over an arbitrary set of record kinds and membership tables so one
builder serves the whole Phase 6 test matrix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
from _support.sidecar_builder import write_emit

FORK_PATH = "trunk"


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    """Return a CREATE TABLE DDL statement for the given table and columns."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _record_table_spec(
    kind: str, cols: list[dict[str, object]], rows: list[tuple[Any, ...]]
) -> dict[str, object]:
    """Build a records-category table spec for the sidecar."""
    return {
        "name": f"records__{kind}",
        "category": "records",
        "columns": cols,
        "rows": len(rows),
        "record_kind": kind,
    }


def _membership_table_spec(
    owner_kind: str,
    property_name: str,
    cols: list[dict[str, object]],
    rows: list[tuple[Any, ...]],
) -> dict[str, object]:
    """Build a membership-category table spec for the sidecar."""
    return {
        "name": f"membership__{owner_kind}__{property_name}",
        "category": "membership",
        "columns": cols,
        "rows": len(rows),
        "record_kind": owner_kind,
        "property": property_name,
    }


_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


class RecordSpec:
    """One records__<kind> table's columns and rows."""

    def __init__(
        self, kind: str, cols: list[dict[str, object]], rows: list[tuple[Any, ...]]
    ) -> None:
        self.kind = kind
        self.cols = cols
        self.rows = rows


class MembershipSpec:
    """One membership__<owner_kind>__<property> table's columns and rows."""

    def __init__(
        self,
        owner_kind: str,
        property_name: str,
        cols: list[dict[str, object]],
        rows: list[tuple[Any, ...]],
    ) -> None:
        self.owner_kind = owner_kind
        self.property_name = property_name
        self.cols = cols
        self.rows = rows


def build_data_emit(
    tmp_path: Path,
    records: list[RecordSpec],
    memberships: list[MembershipSpec] | None = None,
    history_rows: list[tuple[Any, ...]] | None = None,
    branches: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    """Build a multi-table emit: N record kinds, N membership tables, history.

    Args:
        tmp_path: pytest tmp_path — both run.duckdb and base.json land here.
        records: One entry per records__<kind> table.
        memberships: One entry per membership__<owner>__<property> table.
        history_rows: Rows for the shared 'history' table (always created,
            possibly empty).
        branches: Sidecar branches list; defaults to a single trunk branch.
        extra: Extra top-level sidecar fields (e.g. enum_domains).

    Returns:
        tmp_path, ready for open_emit.
    """
    memberships = memberships or []
    history_rows = history_rows or []

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl("history", _HISTORY_COLS))
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))

    for record in records:
        table_name = f"records__{record.kind}"
        conn.execute(_ddl(table_name, record.cols))
        placeholders = ", ".join("?" for _ in record.cols)
        for row in record.rows:
            conn.execute(
                f'INSERT INTO "{table_name}" VALUES ({placeholders})', list(row)
            )

    for membership in memberships:
        table_name = f"membership__{membership.owner_kind}__{membership.property_name}"
        conn.execute(_ddl(table_name, membership.cols))
        placeholders = ", ".join("?" for _ in membership.cols)
        for row in membership.rows:
            conn.execute(
                f'INSERT INTO "{table_name}" VALUES ({placeholders})', list(row)
            )

    conn.close()

    tables: list[dict[str, object]] = [
        {"name": "history", "category": "fixed", "columns": _HISTORY_COLS, "rows": 0}
    ]
    tables.extend(_record_table_spec(r.kind, r.cols, r.rows) for r in records)
    tables.extend(
        _membership_table_spec(m.owner_kind, m.property_name, m.cols, m.rows)
        for m in memberships
    )

    write_emit(
        tmp_path,
        tables=tables,
        branches=branches
        if branches is not None
        else [{"fork_path": FORK_PATH, "parent": None, "slice_at": 9999}],
        extra=extra,
    )
    return tmp_path
