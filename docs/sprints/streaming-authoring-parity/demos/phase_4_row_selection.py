#!/usr/bin/env python
"""
Demo: Row selection — where + membership owner sub_types over the shared spine
Sprint: streaming-authoring-parity
Phase: 4

Builds a sub-typed `worker` emit (day/night/weekend) owning a `ward`
membership table, then:

  1. Streams `worker` with `where: {region: emea}`: the narrowed feed keeps
     every event (`c`/`d`) of a satisfying record and drops every event of a
     non-satisfying one; `seq` stays dense over the survivors (no gap for the
     dropped record).
  2. Streams with a `where` on a column carrying no `enum_domains` entry and
     no matching row: the declared-but-empty topic — `events_per_topic == 0`,
     exit 0, an empty output file.
  3. Streams with a `where` value outside its column's declared
     `enum_domains`: the `discriminator-value-unobserved` notice prints via
     the stderr renderer (never an error); the topic is still empty.
  4. Streams the membership `ward` scoped by owner `sub_types` + `where`
     together: both narrow the addressed owner set independently (an owner
     excluded by either is excluded).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# The vendored fixture-sidecar authority lives under tests/_support — reused
# here (as pytest itself does) rather than hand-rolling a base.json.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.notices import discard_notice_sink  # noqa: E402
from _support.sidecar_builder import identity_column, prop_column  # noqa: E402
from _support.sidecar_builder import write_emit as _write_sidecar  # noqa: E402

from fabulexa_forge.config.models import (  # noqa: E402
    KindStream,
    MembershipStream,
    StreamConfig,
)
from fabulexa_forge.exporters.notices import Notice, render_notice_stderr  # noqa: E402
from fabulexa_forge.exporters.streaming.driver import stream_export  # noqa: E402
from fabulexa_forge.exporters.streaming.engine import iter_stream_events  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_WORKER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__worker_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__site", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_WARD_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__shift", "type": "VARCHAR"},
]

# (record_id, worker_type, region, site, active, deactivated_at)
_WORKERS: list[tuple[str, str, str, str, bool, int | None]] = [
    ("w1", "day", "emea", "hq", True, None),
    ("w2", "day", "emea", "hq", False, 5_000_000),
    ("w3", "night", "apac", "hq", True, None),
    ("w4", "weekend", "emea", "hq", True, None),
]

# (record_id, shift) — every worker holds one open ward interval.
_WARDS: list[tuple[str, str]] = [
    ("w1", "morning"),
    ("w3", "night"),
    ("w4", "weekend"),
]


def _build_demo_emit(tmp_path: Path) -> Path:
    """Write the demo's scratch emit: sub-typed `worker` + its `ward` membership.

    `w2` is created then deactivated (a `c` + `d` pair); every other worker
    stays active. `prop__region` carries a declared `enum_domains` entry
    (emea/apac only — `weekend`'s "emea" value is in-domain); `prop__site`
    carries none, so a `where` on it never draws a notice.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    worker_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WORKER_COLUMNS)
    conn.execute(f'CREATE TABLE "records__worker" ({worker_ddl})')
    conn.executemany(
        'INSERT INTO "records__worker" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            ("trunk", rid, 0, active, deactivated_at, 0, 0, wtype, region, site)
            for rid, wtype, region, site, active, deactivated_at in _WORKERS
        ],
    )

    ward_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WARD_COLUMNS)
    conn.execute(f'CREATE TABLE "membership__worker__ward" ({ward_ddl})')
    conn.executemany(
        'INSERT INTO "membership__worker__ward" VALUES (?, ?, ?, ?, ?)',
        [("trunk", rid, 0, None, shift) for rid, shift in _WARDS],
    )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__worker",
                "category": "records",
                "record_kind": "worker",
                "columns": _WORKER_COLUMNS,
                "rows": len(_WORKERS),
            },
            {
                "name": "membership__worker__ward",
                "category": "membership",
                "record_kind": "worker",
                "property": "ward",
                "columns": _WARD_COLUMNS,
                "rows": len(_WARDS),
            },
        ],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "enum_domains": {
                "worker": {
                    "worker_type": ["day", "night", "weekend"],
                    "region": ["emea", "apac"],
                }
            },
        },
    )
    return tmp_path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        # 1. where: {region: emea} — w1/w2/w4 survive, w3 is dropped whole.
        emea_stream = KindStream(
            name="region_emea",
            kind="worker",
            properties=["region"],
            where={"region": "emea"},
        )
        emea_config = StreamConfig(content="state-changes", streams=[emea_stream])
        with open_emit(emit_dir) as emit:
            emea_events = list(
                iter_stream_events(emit, emea_config, None, discard_notice_sink)
            )
        print("region_emea stream (where: {region: emea}):")
        for event in emea_events:
            print(f"  seq={event.seq} op={event.op} record_id={event.record_id}")
        survivors = {event.record_id for event in emea_events}
        assert survivors == {"w1", "w2", "w4"}, survivors
        assert [event.op for event in emea_events if event.record_id == "w2"] == [
            "c",
            "d",
        ]
        assert [event.seq for event in emea_events] == list(
            range(1, len(emea_events) + 1)
        )
        print(f"  seq dense over {len(emea_events)} survivors, w3 fully excluded\n")

        # 2. Zero-match where on a column with no enum_domains entry: no notice.
        zero_stream = KindStream(
            name="never_matches",
            kind="worker",
            properties=[],
            where={"site": "nowhere"},
        )
        zero_config = StreamConfig(content="state-changes", streams=[zero_stream])
        zero_notices: list[Notice] = []
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        with open_emit(emit_dir) as emit:
            outcome = stream_export(
                emit, zero_config, "jsonl", "file", out_dir, None, zero_notices.append
            )
        print(f"never_matches stream: events_per_topic={outcome.events_per_topic!r}")
        assert outcome.events_per_topic["never_matches"] == 0
        assert zero_notices == []
        assert (out_dir / "never_matches.jsonl").read_text() == ""
        print("  declared-but-empty topic at exit 0, no notice\n")

        # 3. Out-of-domain where value: the two-case notice, never an error.
        namer_stream = KindStream(
            name="region_namer", kind="worker", properties=[], where={"region": "namer"}
        )
        namer_config = StreamConfig(content="state-changes", streams=[namer_stream])
        namer_notices: list[Notice] = []

        def _record_and_render(notice: Notice) -> None:
            namer_notices.append(notice)
            render_notice_stderr(notice)

        with open_emit(emit_dir) as emit:
            namer_events = list(
                iter_stream_events(emit, namer_config, None, _record_and_render)
            )
        assert namer_events == []
        assert len(namer_notices) == 1
        assert namer_notices[0].code == "discriminator-value-unobserved"
        print("region_namer stream: notice printed above, topic empty\n")

        # 4. Membership stream: owner sub_types + where AND-compose.
        ward_stream = MembershipStream(
            name="day_night_emea_ward",
            membership={"kind": "worker", "property": "ward"},
            fields=["shift"],
            sub_types=["day", "night"],
            where={"region": "emea"},
        )
        ward_config = StreamConfig(content="membership-events", streams=[ward_stream])
        with open_emit(emit_dir) as emit:
            ward_events = list(
                iter_stream_events(emit, ward_config, None, discard_notice_sink)
            )
        print(
            "day_night_emea_ward stream (sub_types: [day, night],"
            " where: {region: emea}):"
        )
        for event in ward_events:
            print(f"  op={event.op} record_id={event.record_id}")
        ward_survivors = {event.record_id for event in ward_events}
        assert ward_survivors == {"w1"}, ward_survivors
        print(
            "  w3 excluded by where (apac), w4 excluded by sub_types (weekend),"
            " only w1 (day, emea) survives"
        )

    print(
        "\nSUCCESS: where-narrowed kind feed with dense seq, declared-but-empty"
        " topic, out-of-domain notice, and AND-composed membership owner"
        " sub_types/where selection all verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
