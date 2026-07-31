#!/usr/bin/env python
"""
Demo: `init --mode source` — the self-gating candidate-config proposal
Sprint: source-declared-tables
Phase: 4

Runs `generate_source_init_config` (the engine behind `fabulexa-forge init
--mode source`) against a small fixture emit: a sub-typed, tracked `device`
kind (sensor/camera), a flat untracked `site` kind, and a
`membership__device__watchers` junction. The `presentation_keys` registry
declares a claim for `sensor` only — `camera` is left undeclared — so the
combined `device` state table's natural per-population proposal is MIXED
(sensor -> presentation_id, camera -> record_index). `init` self-gates its
own proposal through the exact machinery the export runs
(`check_identity_election`): the mixed election fails the combined table's
uniformity gate, so `device` degrades to the uniform `record_index` scalar,
with a `# NOTE: ...` comment naming the forcing gate — the emitted `keys:`
line is never a proposal that would fail its own gate.

Shows, end to end:
  1. `generate_source_init_config` prints the commented candidate: one state
     table per kind (`device` combined STI with the sub-type + split-
     alternative comments, `site` plain), one junction
     (`device_watchers`), a `versions` events stub (`device` active —
     tracked; `site` and the membership source appended commented-out —
     lifecycle-only / not yet audited), and the self-gated `keys:` block.
  2. The printed candidate loads cleanly via `load_export_config`.
  3. `build_source_plan` against the loaded config succeeds with no
     exception — the self-gating posture proven end-to-end: a candidate
     `init` proposes always plans clean, degradation comments included.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.init import generate_source_init_config
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.emit import open_emit

_FORK_PATH = "trunk"

_DEVICE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__device_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_SITE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_WATCHERS_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
]

_DEVICE_ROWS: list[tuple[object, ...]] = [
    ("trunk", "dev001", 101, 0, True, None, 0, 0, "sensor", "online"),
    ("trunk", "dev002", 201, 10, True, None, 10, 1, "camera", "offline"),
]
_SITE_ROWS: list[tuple[object, ...]] = [("trunk", "site001", 0, True, None, 0, 0, "HQ")]
_HISTORY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "device", "dev001", "status", 0, "online"),
    ("trunk", "device", "dev002", "status", 10, "offline"),
]
_WATCHERS_ROWS: list[tuple[object, ...]] = [("trunk", "dev001", 5, None, "on_call")]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _insert_all(
    conn: "duckdb.DuckDBPyConnection",
    table: str,
    cols: list[dict[str, object]],
    rows: list[tuple[object, ...]],
) -> None:
    placeholders = ", ".join("?" for _ in cols)
    for row in rows:
        conn.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', list(row))


def build_emit(emit_dir: Path) -> None:
    """Write the `init --mode source` demo emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__device", _DEVICE_COLUMNS))
    conn.execute(_ddl("records__site", _SITE_COLUMNS))
    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_ddl("membership__device__watchers", _WATCHERS_COLUMNS))

    _insert_all(conn, "records__device", _DEVICE_COLUMNS, _DEVICE_ROWS)
    _insert_all(conn, "records__site", _SITE_COLUMNS, _SITE_ROWS)
    _insert_all(conn, "history", _HISTORY_COLUMNS, _HISTORY_ROWS)
    _insert_all(conn, "membership__device__watchers", _WATCHERS_COLUMNS, _WATCHERS_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": _FORK_PATH, "parent": None, "slice_at": 999}],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "enum_domains": {"device": {"device_type": ["sensor", "camera"]}},
        # 'sensor' declares a presentation_id claim; 'camera' does not — the
        # combined device table's natural proposal is mixed, forcing init's
        # self-gate to degrade the whole kind to record_index.
        "presentation_keys": {
            "device": {
                "sub_types": {
                    "sensor": {
                        "unique_within": "branch",
                        "branch_stable": True,
                        "slice_stable": True,
                        "key_space": {
                            "class": "record_index",
                            "prefix": "SNS_",
                            "width": 4,
                        },
                    }
                },
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
            }
        },
        "tables": [
            {
                "name": "records__device",
                "category": "records",
                "record_kind": "device",
                "columns": _DEVICE_COLUMNS,
                "rows": len(_DEVICE_ROWS),
            },
            {
                "name": "records__site",
                "category": "records",
                "record_kind": "site",
                "columns": _SITE_COLUMNS,
                "rows": len(_SITE_ROWS),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_HISTORY_ROWS),
            },
            {
                "name": "membership__device__watchers",
                "category": "membership",
                "record_kind": "device",
                "property": "watchers",
                "columns": _WATCHERS_COLUMNS,
                "rows": len(_WATCHERS_ROWS),
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_emit(emit_dir)

        notices: list[Notice] = []

        # ---- 1. Generate the candidate config ------------------------------
        with open_emit(emit_dir) as emit:
            candidate = generate_source_init_config(emit, notices.append)

        print("=== candidate config (fabulexa-forge init --mode source) ===")
        print(candidate)

        if "mode: source" not in candidate:
            raise _fail("candidate does not declare mode: source")
        if "- name: device\n      kind: device" not in candidate:
            raise _fail("candidate is missing the combined 'device' state table")
        if "# - name: device_sensor" not in candidate:
            raise _fail("candidate is missing the commented split alternative")
        if "- name: device_watchers\n      membership:" not in candidate:
            raise _fail("candidate is missing the 'device_watchers' junction")
        if "- kind: device\n" not in candidate:
            raise _fail("candidate is missing the active 'device' events source")
        if "# - kind: site  # lifecycle-only" not in candidate:
            raise _fail("candidate is missing the commented lifecycle-only 'site'")
        if (
            "keys:\n  device: record_index  # NOTE: ElectionMixedIdentity"
            not in candidate
        ):
            raise _fail("candidate did not self-gate the mixed 'device' election")
        print("OK: candidate proposes device (combined + split alt), site, the")
        print("    device_watchers junction, the events stub, and a self-gated")
        print("    'device: record_index' keys line naming the forcing gate")
        print()

        # ---- 2. Load the printed candidate back -----------------------------
        config_path = tmp_path / "candidate.yaml"
        config_path.write_text(candidate, encoding="utf-8")
        config = load_export_config(config_path)
        print("OK: candidate loads cleanly via load_export_config")

        # ---- 3. Build a plan against it — must not raise --------------------
        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(
                emit.sidecar.runtime(), config.rebase, None, None
            )
            election = resolve_election(emit.sidecar, config.keys)
            plan = build_source_plan(
                emit, config, anchor, election, windowed=False, notices=notices.append
            )
        if len(plan.tables) != 3:
            raise _fail(f"expected 3 declared table units, got {len(plan.tables)}")
        if plan.events is None:
            raise _fail("expected an events unit")
        print(
            "OK: build_source_plan succeeds against the printed candidate —"
            f" {len(plan.tables)} table units + an events unit, no exception"
        )

        print()
        print(
            "SUCCESS: init --mode source proposes a self-gated candidate — a"
            " mixed registry declaration degrades cleanly to a uniform election"
            " with a forcing-gate comment, and the printed config always loads"
            " and plans clean"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
