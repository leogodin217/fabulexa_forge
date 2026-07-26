#!/usr/bin/env python
"""
Demo: Base exporter key columns (self key + edge key)

Sprint: record-index-keys
Phase: 2

Builds a minimal standalone emit (run.duckdb + base.json) with two records
kinds:

  - `entity` — the reference target, three records: e0 (index 0, created at
    sim_time 0), e1 (index 1, created at sim_time 5), e2 (index 2, created at
    sim_time 1, deactivated at sim_time 2 — before every horizon this demo
    renders at, so it stays a resolvable reference target).
  - `actor` — carries `prop__group`, a reference-annotated property pointing
    at `entity`. Its physical `ref_index__group` sibling is seeded with a
    deliberately *wrong* value on one row, to demonstrate that the render
    never reads it: the edge key is always re-derived from `prop__group`
    against the entity record-index relation at the render's own horizon.

      - a0 (created 0): prop__group = "e0"      -> resolves to e0 (index 0)
      - a1 (created 1): prop__group = "missing" -> dangling: id present, key NULL
      - a2 (created 2): prop__group = NULL      -> absent property: id NULL, key NULL
      - a3 (created 3): prop__group = "e1"      -> e1 created at 5: NULL at
        the mid-tape horizon (4), resolves at the tape's end
      - a4 (created 3): prop__group = "e2"      -> e2 deactivated before the
        horizon; still resolves at both horizons

Renders `mode: base` at a mid-tape horizon (slice_at: 3, horizon_ns=4) and at
the tape's end, and checks:

  1. `actor_key` is the first column of every row, never NULL.
  2. `group_key` sits immediately after `prop__group` (the id-space column;
     base keeps the `prop__` prefix on its output name, as it does today).
  3. The dangling and absent-property rows carry id present-or-null with
     `group_key` NULL in both cases.
  4. Horizon binding: a3's edge resolves at the tape's end but not at the
     mid-tape horizon — the same emit, two different key populations.
  5. The deactivated target (e2) still resolves a4's edge at every horizon.
  6. The integer join `actor JOIN entity ON actor.group_key =
     entity.entity_key` returns exactly the same (actor id, entity id) pairs
     as the id-space join `actor.prop__group = entity.id`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.exporters.base.plan import BasePlan, BaseTableSpec, build_base_plan
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"
_ENTITY_KIND = "entity"
_ACTOR_KIND = "actor"

_ENTITY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
]

_ACTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__group",
        "type": "VARCHAR",
        "references": _ENTITY_KIND,
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__group", "type": "BIGINT"},
]

_ENTITY_ROWS: list[tuple[object, ...]] = [
    (_FORK_PATH, "e0", 0, True, None, 0, 0),
    (_FORK_PATH, "e1", 5, True, None, 5, 1),
    # e2 deactivated at 2 -- before every horizon this demo renders at.
    (_FORK_PATH, "e2", 1, False, 2, 2, 2),
]

_ACTOR_ROWS: list[tuple[object, ...]] = [
    # ref_index__group deliberately wrong (9): the render must never read it.
    (_FORK_PATH, "a0", 0, True, None, 0, 0, "e0", 9),
    (_FORK_PATH, "a1", 1, True, None, 1, 1, "missing", None),
    (_FORK_PATH, "a2", 2, True, None, 2, 2, None, None),
    (_FORK_PATH, "a3", 3, True, None, 3, 3, "e1", 1),
    (_FORK_PATH, "a4", 3, True, None, 3, 4, "e2", 2),
]

#: slice_at: 3 -> exclusive horizon 4 (every actor row created before it).
_MID_HORIZON = 4


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_emit(emit_dir: Path) -> None:
    """Write the two-kind, reference-carrying emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl(f"records__{_ENTITY_KIND}", _ENTITY_COLUMNS))
    conn.execute(_ddl(f"records__{_ACTOR_KIND}", _ACTOR_COLUMNS))

    entity_placeholders = ", ".join("?" for _ in _ENTITY_COLUMNS)
    for row in _ENTITY_ROWS:
        conn.execute(
            f'INSERT INTO "records__{_ENTITY_KIND}" VALUES ({entity_placeholders})',
            list(row),
        )
    actor_placeholders = ", ".join("?" for _ in _ACTOR_COLUMNS)
    for row in _ACTOR_ROWS:
        conn.execute(
            f'INSERT INTO "records__{_ACTOR_KIND}" VALUES ({actor_placeholders})',
            list(row),
        )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": f"records__{_ENTITY_KIND}",
                "category": "records",
                "columns": _ENTITY_COLUMNS,
                "rows": len(_ENTITY_ROWS),
                "record_kind": _ENTITY_KIND,
            },
            {
                "name": f"records__{_ACTOR_KIND}",
                "category": "records",
                "columns": _ACTOR_COLUMNS,
                "rows": len(_ACTOR_ROWS),
                "record_kind": _ACTOR_KIND,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _plan(emit_dir: Path) -> BasePlan:
    notices: list[Notice] = []
    with open_emit(emit_dir) as emit:
        plan = build_base_plan(emit.sidecar, None, notices.append)
    if notices:
        print(f"FAIL: expected no notices, got {notices}", file=sys.stderr)
        raise SystemExit(1)
    return plan


def _spec_for(plan: BasePlan, kind: str) -> BaseTableSpec:
    return next(t for t in plan.tables if t.kind == kind)


def _render(emit_dir: Path, spec: BaseTableSpec, horizon_ns: int | None) -> str:
    with open_emit(emit_dir) as emit:
        return build_base_render_sql(emit.sidecar, _FORK_PATH, spec, None, horizon_ns)


def _query(emit_dir: Path, sql: str) -> list[tuple[object, ...]]:
    with open_emit(emit_dir) as emit:
        return emit.query(sql, ())


def _actor_row_by_id(
    rows: list[tuple[object, ...]], columns: list[str], record_id: str
) -> dict[str, object]:
    id_idx = columns.index("id")
    row = next(r for r in rows if r[id_idx] == record_id)
    return dict(zip(columns, row, strict=True))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        plan = _plan(emit_dir)
        actor_spec = _spec_for(plan, _ACTOR_KIND)
        entity_spec = _spec_for(plan, _ENTITY_KIND)

        print(f"actor.reference_keys: {actor_spec.reference_keys}")
        if [rk.property_name for rk in actor_spec.reference_keys] != ["group"]:
            print("FAIL: expected exactly one reference key, 'group'", file=sys.stderr)
            return 1

        # --- Column shape: actor_key first, group_key beside group ---
        actor_end_sql = _render(emit_dir, actor_spec, None)
        actor_columns = [
            part.split(" AS ")[-1].strip('"')
            for part in actor_end_sql.split("SELECT ", 1)[1]
            .split(" FROM ", 1)[0]
            .split(", ")
        ]
        print(f"actor columns (end-of-tape): {actor_columns}")
        if actor_columns[0] != "actor_key":
            print(
                f"FAIL: expected 'actor_key' first, got {actor_columns}",
                file=sys.stderr,
            )
            return 1
        group_idx = actor_columns.index("prop__group")
        if actor_columns[group_idx + 1] != "group_key":
            print(
                f"FAIL: expected 'group_key' immediately after 'group',"
                f" got {actor_columns}",
                file=sys.stderr,
            )
            return 1

        # --- Mid-tape horizon vs tape's end: horizon binding ---
        actor_mid_sql = _render(emit_dir, actor_spec, _MID_HORIZON)
        mid_rows = _query(emit_dir, actor_mid_sql)
        end_rows = _query(emit_dir, actor_end_sql)

        print("--- actor at mid-tape horizon (4) ---")
        for row in mid_rows:
            print(f"  {dict(zip(actor_columns, row, strict=True))}")
        print("--- actor at the tape's end ---")
        for row in end_rows:
            print(f"  {dict(zip(actor_columns, row, strict=True))}")

        a0_mid = _actor_row_by_id(mid_rows, actor_columns, "a0")
        if a0_mid["group_key"] != 0:
            print(
                f"FAIL: a0.group_key should resolve to 0, got {a0_mid}", file=sys.stderr
            )
            return 1

        a1_mid = _actor_row_by_id(mid_rows, actor_columns, "a1")
        if a1_mid["prop__group"] != "missing" or a1_mid["group_key"] is not None:
            print(
                f"FAIL: a1 dangling edge should be id present, key NULL: {a1_mid}",
                file=sys.stderr,
            )
            return 1

        a2_mid = _actor_row_by_id(mid_rows, actor_columns, "a2")
        if a2_mid["prop__group"] is not None or a2_mid["group_key"] is not None:
            print(
                f"FAIL: a2 absent property should be id NULL, key NULL: {a2_mid}",
                file=sys.stderr,
            )
            return 1

        a3_mid = _actor_row_by_id(mid_rows, actor_columns, "a3")
        a3_end = _actor_row_by_id(end_rows, actor_columns, "a3")
        if a3_mid["group_key"] is not None:
            print(
                f"FAIL: a3.group_key should be NULL at the mid-tape horizon"
                f" (target created at-or-after it): {a3_mid}",
                file=sys.stderr,
            )
            return 1
        if a3_end["group_key"] != 1:
            print(
                f"FAIL: a3.group_key should resolve to 1 at the tape's end: {a3_end}",
                file=sys.stderr,
            )
            return 1

        a4_mid = _actor_row_by_id(mid_rows, actor_columns, "a4")
        a4_end = _actor_row_by_id(end_rows, actor_columns, "a4")
        if a4_mid["group_key"] != 2 or a4_end["group_key"] != 2:
            print(
                f"FAIL: a4's deactivated target should still resolve at every"
                f" horizon: mid={a4_mid}, end={a4_end}",
                file=sys.stderr,
            )
            return 1

        # --- The integer join resolves identically to the id-space join ---
        entity_end_sql = _render(emit_dir, entity_spec, None)
        id_join_sql = (
            f'SELECT "_a"."id", "_e"."id" FROM ({actor_end_sql}) AS "_a"'
            f' JOIN ({entity_end_sql}) AS "_e" ON "_a"."prop__group" = "_e"."id"'
            " ORDER BY 1"
        )
        key_join_sql = (
            f'SELECT "_a"."id", "_e"."id" FROM ({actor_end_sql}) AS "_a"'
            f' JOIN ({entity_end_sql}) AS "_e" ON "_a"."group_key" = "_e"."entity_key"'
            " ORDER BY 1"
        )
        id_join_rows = _query(emit_dir, id_join_sql)
        key_join_rows = _query(emit_dir, key_join_sql)
        print(f"id-space join pairs:  {id_join_rows}")
        print(f"key-space join pairs: {key_join_rows}")
        if id_join_rows != key_join_rows:
            print(
                f"FAIL: integer join must resolve identically to the id-space"
                f" join: {key_join_rows} != {id_join_rows}",
                file=sys.stderr,
            )
            return 1
        if not id_join_rows:
            print("FAIL: expected at least one resolved join pair", file=sys.stderr)
            return 1

        print(
            "SUCCESS: actor_key leads every row, group_key sits beside group,"
            " dangling/absent edges are id-present-or-null with key NULL,"
            " horizon binding resolves a3 only at the tape's end, the"
            " deactivated target still resolves, and the integer join matches"
            " the id-space join exactly"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
