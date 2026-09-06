"""A test-side producer slice: the emit as it would have been written at T.

`slice_emit` copies a fixture emit and rewrites it, in plain SQL over the
physical tables, to what the producer would have emitted had the run's slice
ended at `at_sim_time` (inclusive). It is an independent oracle for every
"honest at T" claim in the test tree: it shares no code with the reader's
truncated-tape builders or any exporter, so a windowed export that agrees
with a one-shot export of the sliced emit agrees with the producer's own
notion of a slice, not with itself.

The rewrite, per table category (contract § Base tables):

- fixed (`history`): rows with `sim_time <= T`.
- membership: intervals with `joined_sim_time <= T`; `left_sim_time` beyond T
  becomes NULL (still open at T).
- records: rows with `created_sim_time <= T`; a deactivation beyond T is
  undone (`active = TRUE`, `deactivated_at = NULL`); every `history_tracked`
  property takes its latest history value at or before T, cast to the
  column's sidecar type; `last_mutation_sim_time` becomes the recorded trail
  (`greatest(created_sim_time, latest tracked history <= T, deactivated_at
  when <= T)`); identity, `record_index`, constant and `slice_only` columns
  are verbatim. The contract's no-forward-references guarantee means no
  reference pair needs re-deriving: a record created by T references only
  records created by T.

The sidecar is rewritten through `write_emit` — the one fixture sidecar
authority — with every table's `rows` recounted and the sole branch's
`slice_at` set to T; every other field is carried verbatim.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import duckdb

from _support.sidecar_builder import write_emit

if TYPE_CHECKING:
    from pathlib import Path

_SIDECAR_TOP_LEVEL = ("base_format_version", "branches", "tables", "surface")


def slice_emit(src: "Path", dest: "Path", at_sim_time: int) -> "Path":
    """Write `src` sliced at `at_sim_time` into `dest` and return `dest`.

    Args:
        src: An emit directory (run.duckdb + base.json) with exactly one branch.
        dest: The directory to create; must not exist.
        at_sim_time: The inclusive slice position (ns).

    Returns:
        `dest`.
    """
    dest.mkdir(parents=True)
    shutil.copy(src / "run.duckdb", dest / "run.duckdb")
    sidecar = json.loads((src / "base.json").read_text())
    tables: list[dict[str, object]] = sidecar["tables"]
    (branch,) = sidecar["branches"]

    conn = duckdb.connect(str(dest / "run.duckdb"))
    try:
        for table in tables:
            _slice_table(conn, table, at_sim_time)
        for table in tables:
            name = str(table["name"])
            (count,) = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()
            table["rows"] = int(count)
        conn.close()
    except Exception:
        conn.close()
        raise

    extra = {k: v for k, v in sidecar.items() if k not in _SIDECAR_TOP_LEVEL}
    write_emit(
        dest,
        tables=tables,
        branches=[{**branch, "slice_at": at_sim_time}],
        extra=extra,
        base_format_version=sidecar["base_format_version"],
        surface=sidecar["surface"],
    )
    return dest


def _slice_table(
    conn: duckdb.DuckDBPyConnection, table: dict[str, object], t: int
) -> None:
    name = str(table["name"])
    category = table["category"]
    if category == "fixed":
        conn.execute(f'DELETE FROM "{name}" WHERE sim_time > {t}')
        return
    if category == "membership":
        conn.execute(f'DELETE FROM "{name}" WHERE joined_sim_time > {t}')
        conn.execute(
            f'UPDATE "{name}" SET left_sim_time = NULL WHERE left_sim_time > {t}'
        )
        return
    assert category == "records"
    kind = str(table["record_kind"])
    conn.execute(f'DELETE FROM "{name}" WHERE created_sim_time > {t}')
    conn.execute(
        f'UPDATE "{name}" SET active = TRUE, deactivated_at = NULL'
        f" WHERE deactivated_at > {t}"
    )
    columns: list[dict[str, object]] = table["columns"]  # type: ignore[assignment]
    tracked = [c for c in columns if c.get("history_tracked") is True]
    for column in tracked:
        col = str(column["name"])
        prop = col.removeprefix("prop__")
        conn.execute(
            f'UPDATE "{name}" AS r SET "{col}" = ('
            f"  SELECT CAST(h.value AS {column['type']}) FROM history AS h"
            f"  WHERE h.kind = '{kind}' AND h.record_id = r.record_id"
            f"    AND h.property = '{prop}' AND h.sim_time <= {t}"
            f"  ORDER BY h.sim_time DESC LIMIT 1)"
        )
    conn.execute(
        f'UPDATE "{name}" AS r SET last_mutation_sim_time = greatest('
        f"  r.created_sim_time,"
        f"  coalesce((SELECT max(h.sim_time) FROM history AS h"
        f"            WHERE h.kind = '{kind}' AND h.record_id = r.record_id"
        f"              AND h.sim_time <= {t}), r.created_sim_time),"
        f"  coalesce(r.deactivated_at, r.created_sim_time))"
    )
