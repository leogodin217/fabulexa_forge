#!/usr/bin/env python
"""
Demo: Election resolution, static gates, spine, and the render-time guard
(`exporters/election.py`)

Sprint: key-election
Phase: 3

Builds one declared emit — `records__entity` (sub-typed alpha/beta/gamma,
alpha/beta registry-declared with prefix-incomparable counter spaces
`ALPHA_`/`BETA_`, gamma undeclared) plus a corrupted duplicate-then-mutated
`presentation_id` row on population alpha, and a flat `records__booking`
kind carrying no presentation_id column at all.

1. Resolves the default (no `keys` block) election and prints the total
   per-kind view: every population, its surface, and its synthesized key
   space.
2. Fires each static gate live, sidecar-only, before any data is read:
   - unknown kind -> ElectionKindUnknown
   - map on a flat kind -> ElectionSubTypeUnknown
   - undeclared presentation_id -> ElectionPresentationUndeclared
   - mixed identity across one table's populations -> ElectionMixedIdentity
   - bare-counter siblings under a uniform presentation_id election ->
     ElectionUnionUnsafe
3. Runs the render-time uniqueness guard (`check_elected_key_unique`) over
   the presentation-key derivation's own relation (the exact sibling from
   Phase 2), restricted to population alpha via the population spine, and
   shows it catching the duplicated-then-mutated `e_dup` row.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.derivations.presentation_key import (
    build_presentation_key_at_end_sql,
)
from fabulexa_forge.errors import (
    ElectedKeyDuplicate,
    ElectionKindUnknown,
    ElectionMixedIdentity,
    ElectionPresentationUndeclared,
    ElectionSubTypeUnknown,
    ElectionUnionUnsafe,
)
from fabulexa_forge.exporters.election import (
    build_population_spine_sql,
    check_elected_key_unique,
    check_identity_election,
    resolve_election,
)
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.sidecar import Sidecar

_FORK_PATH = "trunk"

_ENTITY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "prop__entity_type", "type": "VARCHAR"},
]

_BOOKING_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
]

_ENTITY_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "e1", "ALPHA_001", 0, True, None, 0, 0, "alpha"),
    (_FORK_PATH, "e2", "BETA_001", 0, True, None, 0, 1, "beta"),
    (_FORK_PATH, "e3", "ALPHA_002", 0, True, None, 0, 2, "alpha"),
    # Corrupted duplicate: same record_id, mutated presentation_id — the one
    # shape DISTINCT alone cannot collapse (the guard's reason to exist).
    (_FORK_PATH, "e_dup", "ALPHA_777", 0, True, None, 0, 3, "alpha"),
    (_FORK_PATH, "e_dup", "ALPHA_888", 0, True, None, 0, 3, "alpha"),
]

_BOOKING_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "b1", 0, True, None, 0, 0),
]

_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "ALPHA_", "width": 3},
            },
            "beta": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "BETA_", "width": 3},
            },
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_ddl("records__booking", _BOOKING_COLUMNS))

    ent_placeholders = ", ".join("?" for _ in _ENTITY_COLUMNS)
    for row in _ENTITY_ROWS:
        conn.execute(
            f'INSERT INTO "records__entity" VALUES ({ent_placeholders})', list(row)
        )
    book_placeholders = ", ".join("?" for _ in _BOOKING_COLUMNS)
    for row in _BOOKING_ROWS:
        conn.execute(
            f'INSERT INTO "records__booking" VALUES ({book_placeholders})', list(row)
        )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": _ENTITY_COLUMNS,
                "rows": len(_ENTITY_ROWS),
            },
            {
                "name": "records__booking",
                "category": "records",
                "record_kind": "booking",
                "columns": _BOOKING_COLUMNS,
                "rows": len(_BOOKING_ROWS),
            },
        ],
        "enum_domains": {"entity": {"entity_type": ["alpha", "beta", "gamma"]}},
        "presentation_keys": _PRESENTATION_KEYS,
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            sidecar: Sidecar = emit.sidecar

            # ---- 1. Total per-kind view, no keys block ----------------
            print("=== resolve_election(sidecar, None): total default view ===")
            election = resolve_election(sidecar, None)
            for kind in ("entity", "booking"):
                for pop in election.populations_for(kind):
                    print(
                        f"  {kind}.{pop.sub_type}: surface={pop.surface} "
                        f"key_space={pop.key_space}"
                    )
                if not election.is_default(kind):
                    raise _fail(f"{kind} should be default under no keys block")
            print()

            # ---- 2. Static gates, fired live ---------------------------
            print("=== Gate: unknown kind -> ElectionKindUnknown ===")
            try:
                resolve_election(sidecar, {"ghost": "record_id"})
                raise _fail("expected ElectionKindUnknown")
            except ElectionKindUnknown as exc:
                print(f"  OK: {exc}")
            print()

            print("=== Gate: map on a flat kind -> ElectionSubTypeUnknown ===")
            try:
                resolve_election(sidecar, {"booking": {"x": "record_id"}})
                raise _fail("expected ElectionSubTypeUnknown")
            except ElectionSubTypeUnknown as exc:
                print(f"  OK: {exc}")
            print()

            print(
                "=== Gate: undeclared presentation_id (gamma) -> "
                "ElectionPresentationUndeclared ==="
            )
            try:
                resolve_election(sidecar, {"entity": {"gamma": "presentation_id"}})
                raise _fail("expected ElectionPresentationUndeclared")
            except ElectionPresentationUndeclared as exc:
                print(f"  OK: {exc}")
            print()

            print(
                "=== Gate: mixed identity (alpha=presentation_id, beta=record_id) ==="
            )
            mixed_election = resolve_election(
                sidecar, {"entity": {"alpha": "presentation_id"}}
            )
            try:
                check_identity_election(
                    mixed_election, "entity", ["alpha", "beta"], "t_entity"
                )
                raise _fail("expected ElectionMixedIdentity")
            except ElectionMixedIdentity as exc:
                print(f"  OK: {exc}")
            print()

            print(
                "=== Gate: bare-counter siblings under uniform presentation_id -> "
                "ElectionUnionUnsafe ==="
            )
            bare_sidecar = Sidecar.from_raw(
                {
                    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
                    "branches": [
                        {"fork_path": _FORK_PATH, "parent": None, "slice_at": 0}
                    ],
                    "tables": [
                        {
                            "name": "records__rider",
                            "category": "records",
                            "record_kind": "rider",
                            "columns": _ENTITY_COLUMNS,
                            "rows": 0,
                        }
                    ],
                    "enum_domains": {"rider": {"rider_type": ["north", "south"]}},
                    "presentation_keys": {
                        "rider": {
                            "sub_types": {
                                "north": {
                                    "unique_within": "emit",
                                    "branch_stable": False,
                                    "slice_stable": False,
                                    "key_space": {
                                        "class": "counter",
                                        "prefix": "",
                                        "width": 3,
                                    },
                                },
                                "south": {
                                    "unique_within": "emit",
                                    "branch_stable": False,
                                    "slice_stable": False,
                                    "key_space": {
                                        "class": "counter",
                                        "prefix": "",
                                        "width": 3,
                                    },
                                },
                            },
                            "branch_stable": False,
                            "slice_stable": False,
                        }
                    },
                }
            )
            bare_election = resolve_election(bare_sidecar, {"rider": "presentation_id"})
            try:
                check_identity_election(
                    bare_election, "rider", ["north", "south"], "t_rider"
                )
                raise _fail("expected ElectionUnionUnsafe")
            except ElectionUnionUnsafe as exc:
                print(f"  OK: {exc}")
            print()

            # ---- 3. The render-time guard -------------------------------
            print("=== Guard: check_elected_key_unique over population alpha ===")
            relation_sql = build_presentation_key_at_end_sql(
                sidecar, _FORK_PATH, "entity"
            )
            spine_sql = build_population_spine_sql(
                sidecar, _FORK_PATH, "entity", ["alpha"]
            )
            try:
                check_elected_key_unique(
                    emit, relation_sql, "presentation_id", spine_sql, "t_entity(alpha)"
                )
                raise _fail("expected ElectedKeyDuplicate on the e_dup corruption")
            except ElectedKeyDuplicate as exc:
                print(f"  OK: {exc}")
            print()

        print(
            "SUCCESS: default election is total record_id; every static gate fires"
            " with its documented error; the render-time guard catches the"
            " duplicated-then-mutated elected key over the population-restricted"
            " relation"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
