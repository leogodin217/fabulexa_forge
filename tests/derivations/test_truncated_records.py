"""Tests for derivations.truncated_tape.build_truncated_records_sql (Phase 4).

Builds a two-kind fixture: records__widget (the reference target, sub-typed,
carrying a tracked property, a corrupted-tracked property, a non-exempt
slice_only property, and a constant property) and records__container (the
reference source, carrying a tracked reference property and a constant
reference property, each paired with its ref_index__ sibling).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.derivations.truncated_tape import (
    build_truncated_records_sql,
    build_truncated_sidecar,
)
from fabulexa_forge.reader.emit import open_emit

FORK_PATH = "trunk"
AT_SIM_TIME = 100

# ---------------------------------------------------------------------------
# Column shapes
# ---------------------------------------------------------------------------

_WIDGET_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__widget_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__count", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__note", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
    prop_column(
        "prop__color", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_CONTAINER_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__label", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__owner",
        "VARCHAR",
        history_tracked=True,
        temporal_class="tracked",
        references="widget",
    ),
    identity_column("ref_index__owner", "BIGINT"),
    prop_column(
        "prop__backup",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="widget",
    ),
    identity_column("ref_index__backup", "BIGINT"),
]

_HISTORY_COLS: list[dict[str, Any]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# fork_path, record_id, created_sim_time, active, deactivated_at,
# last_mutation_sim_time, record_index, widget_type, status, count, note, color
_WIDGET_ROWS: list[tuple[Any, ...]] = [
    (
        FORK_PATH,
        "w1",
        10,
        True,
        None,
        999,
        0,
        "alpha",
        "ignored",
        999,
        "secret",
        "blue",
    ),
    (FORK_PATH, "w2", 200, True, None, 999, 1, "beta", "ignored", 999, "hidden", "red"),
    (FORK_PATH, "w3", 5, True, None, 999, 2, "alpha", "ignored", 999, "n3", "green"),
]

# fork_path, record_id, created_sim_time, active, deactivated_at,
# last_mutation_sim_time, record_index, label, owner, ref_index__owner,
# backup, ref_index__backup
_CONTAINER_ROWS: list[tuple[Any, ...]] = [
    (FORK_PATH, "c1", 30, True, 90, 999, 0, "ignored", "ignored", None, "w1", None),
    (FORK_PATH, "c2", 40, True, 120, 999, 1, "ignored", "ignored", None, None, None),
    (FORK_PATH, "c3", 50, True, None, 999, 2, "ignored", "ignored", None, None, None),
    (FORK_PATH, "c4", 60, True, None, 999, 3, "ignored", "ignored", None, None, None),
    (FORK_PATH, "c5", 150, True, None, 999, 4, "ignored", "ignored", None, None, None),
]

_HISTORY_ROWS: list[tuple[Any, ...]] = [
    (FORK_PATH, "widget", "w1", "status", 20, "on"),
    (FORK_PATH, "widget", "w1", "status", 80, "off"),
    (FORK_PATH, "widget", "w1", "status", 150, "on"),  # after T — must not count
    (FORK_PATH, "widget", "w1", "count", 20, "5"),
    (FORK_PATH, "widget", "w3", "status", 10, "on"),
    (FORK_PATH, "widget", "w3", "count", 15, "oops-not-int"),  # corrupted
    (FORK_PATH, "container", "c1", "label", 20, "lab_a"),
    (FORK_PATH, "container", "c1", "label", 95, "lab_b"),
    (FORK_PATH, "container", "c1", "owner", 25, "w1"),
    (FORK_PATH, "container", "c2", "owner", 35, "w404"),  # dangling
    (FORK_PATH, "container", "c3", "owner", 45, "w2"),  # target created after T
    # c4 owner: no history rows at all
]


def _ddl(table: str, cols: list[dict[str, Any]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _table_spec(
    name: str,
    cols: list[dict[str, Any]],
    rows: int,
    record_kind: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "category": "records",
        "record_kind": record_kind,
        "columns": cols,
        "rows": rows,
    }


def _history_table_spec(rows: int) -> dict[str, Any]:
    return {
        "name": "history",
        "category": "fixed",
        "columns": _HISTORY_COLS,
        "rows": rows,
    }


def _build_emit(tmp_path: Path) -> Path:
    """Write the widget/container fixture emit."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("history", _HISTORY_COLS))
    conn.execute(_ddl("records__widget", _WIDGET_COLS))
    conn.execute(_ddl("records__container", _CONTAINER_COLS))

    for row in _HISTORY_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    widget_ph = ", ".join("?" for _ in _WIDGET_COLS)
    for row in _WIDGET_ROWS:
        conn.execute(f'INSERT INTO "records__widget" VALUES ({widget_ph})', list(row))
    container_ph = ", ".join("?" for _ in _CONTAINER_COLS)
    for row in _CONTAINER_ROWS:
        conn.execute(
            f'INSERT INTO "records__container" VALUES ({container_ph})', list(row)
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _history_table_spec(len(_HISTORY_ROWS)),
            _table_spec("records__widget", _WIDGET_COLS, len(_WIDGET_ROWS), "widget"),
            _table_spec(
                "records__container",
                _CONTAINER_COLS,
                len(_CONTAINER_ROWS),
                "container",
            ),
        ],
        extra={"enum_domains": {"widget": {"widget_type": ["alpha", "beta"]}}},
    )
    return tmp_path


def _build_absent_target_emit(tmp_path: Path) -> Path:
    """Write a fixture emit with records__container only: its reference
    properties still declare `references: widget`, but records__widget is
    omitted (contract-legal — zero widgets in the slice), so every
    reference value is NULL."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("history", _HISTORY_COLS))
    conn.execute(_ddl("records__container", _CONTAINER_COLS))
    rows: list[tuple[Any, ...]] = [
        (FORK_PATH, "c1", 30, True, None, 30, 0, None, None, None, None, None),
        (FORK_PATH, "c2", 40, True, None, 40, 1, None, None, None, None, None),
    ]
    ph = ", ".join("?" for _ in _CONTAINER_COLS)
    for row in rows:
        conn.execute(f'INSERT INTO "records__container" VALUES ({ph})', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _history_table_spec(0),
            _table_spec("records__container", _CONTAINER_COLS, len(rows), "container"),
        ],
    )
    return tmp_path


def _rows_by_record_id(
    rows: list[tuple[Any, ...]], cols: list[str]
) -> dict[str, dict[str, Any]]:
    idx = {name: i for i, name in enumerate(cols)}
    return {row[idx["record_id"]]: dict(zip(cols, row)) for row in rows}


class TestBuildTruncatedRecordsSql:
    """Tests for build_truncated_records_sql."""

    def test_row_filter_created_after_t_excluded(self, tmp_path: Path) -> None:
        """A record created after T is entirely absent."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "widget", AT_SIM_TIME
            )
            cols = [c.name for c in emit.sidecar.columns("records__widget")]
            cols = [
                c for c in cols if c not in {"prop__note"}
            ]  # dropped non-exempt slice_only
            rows = emit.query(sql, ())
        by_id = _rows_by_record_id(rows, cols)
        assert set(by_id) == {"w1", "w3"}  # w2 (created=200) excluded

        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "container", AT_SIM_TIME
            )
            rows = emit.query(sql, ())
        assert len(rows) == 4  # c5 (created=150) excluded

    def test_identity_and_record_index_verbatim(self, tmp_path: Path) -> None:
        """fork_path, record_id, record_index are projected verbatim."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "widget", AT_SIM_TIME
            )
            cols = [
                c.name
                for c in emit.sidecar.columns("records__widget")
                if c.name != "prop__note"
            ]
            rows = emit.query(sql, ())
        by_id = _rows_by_record_id(rows, cols)
        assert by_id["w1"]["fork_path"] == FORK_PATH
        assert by_id["w1"]["record_id"] == "w1"
        assert by_id["w1"]["record_index"] == 0
        assert by_id["w3"]["record_index"] == 2

    def test_active_and_deactivated_at_horizon_rendered(self, tmp_path: Path) -> None:
        """deactivated_at <= T renders inactive; > T renders still active."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "container", AT_SIM_TIME
            )
            cols = [c.name for c in emit.sidecar.columns("records__container")]
            rows = emit.query(sql, ())
        by_id = _rows_by_record_id(rows, cols)
        assert by_id["c1"]["active"] is False
        assert by_id["c1"]["deactivated_at"] == 90
        assert by_id["c2"]["active"] is True
        assert by_id["c2"]["deactivated_at"] is None

    def test_tracked_property_reconstructed_and_try_cast(self, tmp_path: Path) -> None:
        """A tracked property reconstructs its most-recent value <= T, cast to
        its declared type; a corrupted non-parsing history value reconstructs
        NULL rather than erroring."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "widget", AT_SIM_TIME
            )
            cols = [
                c.name
                for c in emit.sidecar.columns("records__widget")
                if c.name != "prop__note"
            ]
            rows = emit.query(sql, ())
        by_id = _rows_by_record_id(rows, cols)
        assert by_id["w1"]["prop__status"] == "off"  # last <= 100 (150 excluded)
        assert by_id["w1"]["prop__count"] == 5
        assert by_id["w3"]["prop__status"] == "on"
        assert by_id["w3"]["prop__count"] is None  # corrupted -> TRY_CAST NULL

    def test_constant_property_verbatim(self, tmp_path: Path) -> None:
        """A constant property is projected verbatim, untouched by history."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "widget", AT_SIM_TIME
            )
            cols = [
                c.name
                for c in emit.sidecar.columns("records__widget")
                if c.name != "prop__note"
            ]
            rows = emit.query(sql, ())
        by_id = _rows_by_record_id(rows, cols)
        assert by_id["w1"]["prop__color"] == "blue"
        assert by_id["w3"]["prop__color"] == "green"

    def test_slice_only_dropped_discriminator_kept(self, tmp_path: Path) -> None:
        """The non-exempt slice_only prop__note is absent; the sub-typed
        kind's slice_only discriminator prop__widget_type is carried
        verbatim."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "widget", AT_SIM_TIME
            )
            physical_cols = [c.name for c in emit.sidecar.columns("records__widget")]
            projected_cols = [c for c in physical_cols if c != "prop__note"]
            rows = emit.query(sql, ())
        assert len(rows[0]) == len(projected_cols)
        by_id = _rows_by_record_id(rows, projected_cols)
        assert by_id["w1"]["prop__widget_type"] == "alpha"

    def test_recorded_trail(self, tmp_path: Path) -> None:
        """last_mutation_sim_time is the recorded trail: greatest(created,
        latest tracked history <= T, deactivated_at when <= T)."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "widget", AT_SIM_TIME
            )
            cols = [
                c.name
                for c in emit.sidecar.columns("records__widget")
                if c.name != "prop__note"
            ]
            widget_rows = emit.query(sql, ())

            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "container", AT_SIM_TIME
            )
            container_cols = [
                c.name for c in emit.sidecar.columns("records__container")
            ]
            container_rows = emit.query(sql, ())

        widget_by_id = _rows_by_record_id(widget_rows, cols)
        assert widget_by_id["w1"]["last_mutation_sim_time"] == 80  # max(10, 80)
        assert widget_by_id["w3"]["last_mutation_sim_time"] == 15  # max(5, 10, 15)

        container_by_id = _rows_by_record_id(container_rows, container_cols)
        # max(created=30, label/owner history<=100 max=95, deactivated_at=90)
        assert container_by_id["c1"]["last_mutation_sim_time"] == 95
        # deactivated_at=120 > T excluded; max(created=40, owner=35)
        assert container_by_id["c2"]["last_mutation_sim_time"] == 40
        # no history <= T at all; created_sim_time alone
        assert container_by_id["c4"]["last_mutation_sim_time"] == 60

    def test_ref_index_re_derived(self, tmp_path: Path) -> None:
        """ref_index__<name> re-derives via the truncated target spine:
        correct index for an intact reference; NULL beside a NULL reference
        (tracked and constant alike); NULL beside a dangling reference; NULL
        beside a reference naming a record created after T."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "container", AT_SIM_TIME
            )
            cols = [c.name for c in emit.sidecar.columns("records__container")]
            rows = emit.query(sql, ())
        by_id = _rows_by_record_id(rows, cols)

        # c1: intact tracked ("owner" -> w1) and constant ("backup" -> w1)
        # references, both resolving to widget w1's record_index (0).
        assert by_id["c1"]["prop__owner"] == "w1"
        assert by_id["c1"]["ref_index__owner"] == 0
        assert by_id["c1"]["prop__backup"] == "w1"
        assert by_id["c1"]["ref_index__backup"] == 0

        # c2: dangling tracked reference ("w404" does not exist); NULL
        # constant reference.
        assert by_id["c2"]["prop__owner"] == "w404"
        assert by_id["c2"]["ref_index__owner"] is None
        assert by_id["c2"]["prop__backup"] is None
        assert by_id["c2"]["ref_index__backup"] is None

        # c3: tracked reference names a record (w2) created after T.
        assert by_id["c3"]["prop__owner"] == "w2"
        assert by_id["c3"]["ref_index__owner"] is None

        # c4: no history at all for "owner" -> NULL reference -> NULL index.
        assert by_id["c4"]["prop__owner"] is None
        assert by_id["c4"]["ref_index__owner"] is None

    def test_ref_index_absent_target_table_projects_null(self, tmp_path: Path) -> None:
        """A reference property whose target kind has no records table in
        the sidecar (contract-legal: zero records of that kind in the
        slice) projects ref_index__<name> as a typed NULL — no JOIN naming
        the nonexistent table, no binder error, tracked and constant
        references alike."""
        emit_dir = _build_absent_target_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            sql = build_truncated_records_sql(
                emit.sidecar, FORK_PATH, "container", AT_SIM_TIME
            )
            assert "records__widget" not in sql
            cols = [c.name for c in emit.sidecar.columns("records__container")]
            rows = emit.query(sql, ())
            described = emit.query(f"DESCRIBE ({sql})", ())
        by_id = _rows_by_record_id(rows, cols)
        assert set(by_id) == {"c1", "c2"}
        assert by_id["c1"]["ref_index__owner"] is None
        assert by_id["c1"]["ref_index__backup"] is None
        assert by_id["c2"]["ref_index__owner"] is None
        # The NULL projection keeps the column's sidecar-declared type.
        types_by_col = {row[0]: row[1] for row in described}
        assert types_by_col["ref_index__owner"] == "BIGINT"
        assert types_by_col["ref_index__backup"] == "BIGINT"

    def test_column_list_agrees_with_truncated_sidecar(self, tmp_path: Path) -> None:
        """The SELECT's column names/order agree with build_truncated_sidecar's
        declared column list, for every fixture kind."""
        emit_dir = _build_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            truncated_sidecar = build_truncated_sidecar(emit.sidecar)
            for kind, table_name in (
                ("widget", "records__widget"),
                ("container", "records__container"),
            ):
                sql = build_truncated_records_sql(
                    emit.sidecar, FORK_PATH, kind, AT_SIM_TIME
                )
                described = emit.query(f"DESCRIBE ({sql})", ())
                produced_cols = [row[0] for row in described]
                expected_cols = [c.name for c in truncated_sidecar.columns(table_name)]
                assert produced_cols == expected_cols
