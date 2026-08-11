#!/usr/bin/env python
"""
Demo: `init --mode streaming` -- the sidecar-driven proposal engine
Sprint: streaming-declared-streams
Phase: 4

Builds a fixture emit carrying:
  - a sub-typed kind (`entity`, sub-types `product` / `infrastructure`) --
    one live stream proposed per sub-type, `properties` from the
    `sub_type_columns` partition
  - a flat kind (`location`) -- one live stream, `name: location`
  - a membership table (`membership__entity__zones`) -- proposed only inside
    the fully-commented `content: membership-events` alternative
  - a partial `presentation_keys` block covering `product` only, so the
    proposed `keys:` block elects `presentation_id` for `product` and the
    natural `record_index` fallback for `infrastructure` / `location`

then runs `generate_stream_init_config`, prints the candidate YAML, parses it
with `load_stream_config`, and runs `iter_stream_events` against the same
emit to prove the self-gate live: the emitted text is not just YAML-valid,
it streams clean.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.config.loader import load_stream_config  # noqa: E402
from fabulexa_forge.exporters.streaming.engine import iter_stream_events  # noqa: E402
from fabulexa_forge.exporters.streaming.init import (  # noqa: E402
    generate_stream_init_config,
)
from fabulexa_forge.reader.emit import open_emit  # noqa: E402


def _identity_prefix() -> list[dict[str, object]]:
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
    ]


# presentation_id occupies the slot right after record_id (contract position);
# _identity_prefix already places record_index at the tail, so splice it in.
_ENTITY_COLS: list[dict[str, object]] = [
    _identity_prefix()[0],
    _identity_prefix()[1],
    {"name": "presentation_id", "type": "VARCHAR"},
    *_identity_prefix()[2:],
    {"name": "prop__entity_type", "type": "VARCHAR"},
    prop_column(
        "prop__category", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_LOCATION_COLS: list[dict[str, object]] = [
    *_identity_prefix(),
    prop_column(
        "prop__label", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_MEMBERSHIP_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__zone_name", "type": "VARCHAR"},
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "product": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "PRD_", "width": 3},
            }
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_demo_emit(emit_dir: Path) -> None:
    """Write the entity/location/membership emit to `emit_dir`."""
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__entity", _ENTITY_COLS))
    conn.execute(_ddl("records__location", _LOCATION_COLS))
    conn.execute(_ddl("membership__entity__zones", _MEMBERSHIP_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))

    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e1", "PRD_001", 0, True, 10, 0, "product", "widget", "active"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e2", "INF_001", 5, True, 10, 5, "infrastructure", "power", "up"],
    )
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "l1", 0, True, 0, 0, "Depot"],
    )
    conn.execute(
        'INSERT INTO "membership__entity__zones" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "e1", 0, "north"],
    )
    for row in [
        ("trunk", "entity", "e1", "status", 0, "active"),
        ("trunk", "entity", "e2", "status", 5, "up"),
    ]:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__entity",
                "category": "records",
                "columns": _ENTITY_COLS,
                "rows": 2,
                "record_kind": "entity",
            },
            {
                "name": "records__location",
                "category": "records",
                "columns": _LOCATION_COLS,
                "rows": 1,
                "record_kind": "location",
            },
            {
                "name": "membership__entity__zones",
                "category": "membership",
                "columns": _MEMBERSHIP_COLS,
                "rows": 1,
                "record_kind": "entity",
                "property": "zones",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": 2,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        extra={
            "enum_domains": {"entity": {"entity_type": ["product", "infrastructure"]}},
            "sub_type_columns": {
                "entity": {
                    "product": ["prop__category", "prop__status"],
                    "infrastructure": ["prop__status"],
                }
            },
            "presentation_keys": _PRESENTATION_KEYS,
        },
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_demo_emit(emit_dir)

        notices: list[object] = []
        with open_emit(emit_dir) as emit:
            candidate = generate_stream_init_config(emit, notices.append)

        print(candidate)
        print(f"notices: {notices}")

        # -- The self-gate proved live: parse, then stream. --
        cfg_path = emit_dir / "candidate.yaml"
        cfg_path.write_text(candidate, encoding="utf-8")
        config = load_stream_config(cfg_path)
        assert config.content == "state-changes"
        stream_names = [s.name for s in config.streams]
        assert stream_names == ["product", "infrastructure", "location"]
        assert config.keys == {
            "entity": {"product": "presentation_id", "infrastructure": "record_index"},
            "location": "record_index",
        }

        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))
        assert len(events) > 0

    print(
        "\nSUCCESS: init --mode streaming proposed a per-sub-type live config"
        " with a self-gated keys: block, the membership table folded into the"
        " fully-commented alternative, and the emitted text streamed clean"
        " against the same emit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
