"""Shared test helpers for exporters/streaming tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fabulexa_forge.anchor import EffectiveAnchor

_UTC = timezone.utc


def make_anchor(
    start_instant: datetime | None = None,
) -> EffectiveAnchor:
    """Build an EffectiveAnchor with a known start_instant."""
    if start_instant is None:
        start_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=_UTC)
    return EffectiveAnchor(
        start_instant=start_instant,
        timezone=ZoneInfo("UTC"),
    )


def _ddl(table: str, cols: list[dict[str, Any]]) -> str:
    """Return a CREATE TABLE DDL statement for the given table and columns."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _membership_table_spec(
    name: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str,
    property_name: str,
) -> dict[str, object]:
    """Build a membership-category table spec for the sidecar."""
    return {
        "name": name,
        "category": "membership",
        "columns": cols,
        "rows": rows,
        "record_kind": record_kind,
        "property": property_name,
    }
