"""Tests for `fabulexa-forge init` CLI verb (record_roles-driven).

Covers:
- Bare-string dimension kind, no history_tracked -> role: dim, scd: type1 stub
- Bare-string dimension kind with >= 1 history_tracked column -> scd: type2 + tracked cols
- Bare-string fact kind with no prop__<kind>_type -> role: fact + FK-candidate comment
- Bare-string fact kind with modelling prop__<kind>_type -> per-DISTINCT-value fact stubs
- Object-valued kind actor:{driver: dimension, ride: fact} -> dim_actor_driver + fact_actor_ride
- Declared-but-unobserved object sub-type still yields a stub
- Kind owning a membership__<kind>__<property> table -> membership-FK comments
- Generated YAML loads cleanly via load_export_config
- No exclude block proposed; no "likely-internal"/topology comments
- record_roles absent -> cmd_init / main(["init", ...]) prints ERROR: to stderr, returns 1
- Output deterministic across two runs of the same emit
- Non-exempt slice_only column: omitted from the SCD-2 stub's column list, one
  'slice-only-column-omitted' notice on stderr per skip, the kind itself still
  proposed (the skip is column-level)
- Exempt discriminator (subtyped kind's prop__<kind>_type, even when its own
  class is slice_only): still proposed, filter pre-fill unchanged
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.sidecar_builder import identity_column, prop_column
from _support.sidecar_builder import write_emit as _write_emit_sidecar

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.cli import cmd_init, main

# ---------------------------------------------------------------------------
# Shared column definitions
# ---------------------------------------------------------------------------

_LOCATION_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
]

_SENSOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__label", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

#: Same shape as `_SENSOR_COLUMNS` plus one non-exempt `slice_only` payload
#: column, for the init skip + notice tests.
_SENSOR_COLUMNS_WITH_SLICE_ONLY: list[dict[str, object]] = [
    *_SENSOR_COLUMNS,
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]

_EVENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {"name": "prop__location_id", "type": "VARCHAR", "references": "location"},
    identity_column("ref_index__location_id", "BIGINT"),
]

_TRIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {"name": "prop__trip_type", "type": "VARCHAR"},
    {"name": "prop__location_id", "type": "VARCHAR", "references": "location"},
    identity_column("ref_index__location_id", "BIGINT"),
]


def _actor_identity_prefix() -> list[dict[str, object]]:
    """The identity + lifecycle column prefix shared by every actor fixture."""
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
    ]


_ACTOR_COLUMNS: list[dict[str, object]] = [
    *_actor_identity_prefix(),
    {"name": "prop__actor_type", "type": "VARCHAR"},
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

#: Same shape as `_ACTOR_COLUMNS`, but the discriminator itself is declared
#: `temporal_class: slice_only` — proves the exempt-discriminator carve-out
#: survives even when the column's own class literally is slice_only.
_ACTOR_COLUMNS_SLICE_ONLY_DISCRIMINATOR: list[dict[str, object]] = [
    *_actor_identity_prefix(),
    prop_column(
        "prop__actor_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role_name", "type": "VARCHAR"},
    {"name": "member__entity__kind", "type": "VARCHAR"},
    {"name": "member__entity__id", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Emit builder helpers
# ---------------------------------------------------------------------------


_SIDECAR_TOP_LEVEL_KEYS = frozenset({"base_format_version", "branches", "tables"})


def _write_sidecar(tmp_path: Path, sidecar: dict[str, object]) -> None:
    """Write the sidecar JSON file to tmp_path via the canonical sidecar writer."""
    extra = {
        key: value
        for key, value in sidecar.items()
        if key not in _SIDECAR_TOP_LEVEL_KEYS
    }
    _write_emit_sidecar(
        tmp_path,
        tables=sidecar["tables"],  # type: ignore[arg-type]
        branches=sidecar.get("branches"),  # type: ignore[arg-type]
        extra=extra or None,
    )


def _base_sidecar(
    tables: list[dict[str, object]],
    record_roles: dict[str, object] | None,
    enum_domains: dict[str, dict[str, list[str]]] | None = None,
    sub_type_columns: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, object]:
    """Build a minimal sidecar dict with optional record_roles/enum_domains/partition."""
    result: dict[str, object] = {
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 200}],
        "tables": tables,
    }
    if record_roles is not None:
        result["record_roles"] = record_roles
    if enum_domains is not None:
        result["enum_domains"] = enum_domains
    if sub_type_columns is not None:
        result["sub_type_columns"] = sub_type_columns
    return result


def build_bare_dim_emit(tmp_path: Path) -> Path:
    """Build an emit with a bare-string dimension kind, no history_tracked."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "loc1", 10, True, 10, 0, "Depot"],
    )
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__location",
                    "records",
                    _LOCATION_COLUMNS,
                    1,
                    record_kind="location",
                ),
            ],
            record_roles={"location": "dimension"},
        ),
    )
    return tmp_path


def build_bare_dim_scd2_emit(
    tmp_path: Path,
    columns: list[dict[str, object]] | None = None,
    extra_row_values: list[object] | None = None,
) -> Path:
    """Build an emit with a bare-string dimension kind with history_tracked.

    `columns` overrides `_SENSOR_COLUMNS` (e.g. to add a non-exempt slice_only
    payload column); `extra_row_values` supplies the row value(s) for any
    columns appended past the base eight.
    """
    sensor_columns = columns if columns is not None else _SENSOR_COLUMNS
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__sensor", sensor_columns))
    row_values: list[object] = [
        "trunk",
        "s1",
        0,
        True,
        None,
        10,
        0,
        "SensorA",
        "online",
    ]
    if extra_row_values:
        row_values += extra_row_values
    placeholders = ", ".join(["?"] * len(row_values))
    conn.execute(f'INSERT INTO "records__sensor" VALUES ({placeholders})', row_values)
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__sensor",
                    "records",
                    sensor_columns,
                    1,
                    record_kind="sensor",
                ),
            ],
            record_roles={"sensor": "dimension"},
        ),
    )
    return tmp_path


def build_bare_dim_scd2_with_slice_only_column_emit(tmp_path: Path) -> Path:
    """Build a bare-string SCD-2 dim kind with one non-exempt slice_only column."""
    return build_bare_dim_scd2_emit(
        tmp_path, columns=_SENSOR_COLUMNS_WITH_SLICE_ONLY, extra_row_values=["north"]
    )


def build_bare_fact_no_discriminator_emit(tmp_path: Path) -> Path:
    """Build an emit with a bare-string fact kind and no prop__<kind>_type."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(_create_ddl("records__event", _EVENT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "loc1", 10, True, 10, 0, "Depot"],
    )
    conn.execute(
        'INSERT INTO "records__event" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "ev1", 10, True, 10, 0, "loc1", 0],
    )
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__location",
                    "records",
                    _LOCATION_COLUMNS,
                    1,
                    record_kind="location",
                ),
                _table_spec(
                    "records__event",
                    "records",
                    _EVENT_COLUMNS,
                    1,
                    record_kind="event",
                ),
            ],
            record_roles={"event": "fact", "location": "dimension"},
        ),
    )
    return tmp_path


def build_bare_fact_with_discriminator_emit(tmp_path: Path) -> Path:
    """Build an emit with a bare-string fact kind and a modelling prop__<kind>_type."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(_create_ddl("records__trip", _TRIP_COLUMNS))
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "loc1", 10, True, 10, 0, "Depot"],
    )
    conn.execute(
        'INSERT INTO "records__trip" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "t1", 10, True, 10, 0, "delivery", "loc1", 0],
    )
    conn.execute(
        'INSERT INTO "records__trip" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "t2", 20, True, 20, 1, "pickup", "loc1", 0],
    )
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__location",
                    "records",
                    _LOCATION_COLUMNS,
                    1,
                    record_kind="location",
                ),
                _table_spec(
                    "records__trip",
                    "records",
                    _TRIP_COLUMNS,
                    2,
                    record_kind="trip",
                ),
            ],
            record_roles={"location": "dimension", "trip": "fact"},
        ),
    )
    return tmp_path


def build_object_valued_actor_emit(
    tmp_path: Path, columns: list[dict[str, object]] | None = None
) -> Path:
    """Build an emit with an object-valued actor:{driver: dimension, ride: fact} kind.

    `columns` overrides `_ACTOR_COLUMNS` (e.g. to mark the discriminator
    itself `temporal_class: slice_only` for the exempt-discriminator test).
    """
    actor_columns = columns if columns is not None else _ACTOR_COLUMNS
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", actor_columns))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "a1", 10, True, 10, 0, "driver", "Alice", "active"],
    )
    # "bus" sub-type is declared but has no rows - tests unobserved sub-type stub
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__actor",
                    "records",
                    actor_columns,
                    1,
                    record_kind="actor",
                ),
            ],
            record_roles={
                "actor": {"driver": "dimension", "ride": "fact", "bus": "dimension"}
            },
            enum_domains={"actor": {"actor_type": ["driver", "ride", "bus"]}},
        ),
    )
    return tmp_path


def build_actor_emit_with_sub_type_columns(tmp_path: Path) -> Path:
    """Object-valued actor emit carrying a `sub_type_columns` partition.

    driver owns `prop__name` only; bus owns `prop__status` only; ride (fact)
    owns nothing. The union of the lists is {prop__name, prop__status} — exactly
    the value columns minus the discriminator (C14-consistent). Exercises
    per-sub-type column pruning in the generated stubs.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "a1", 10, True, 10, 0, "driver", "Alice", "active"],
    )
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__actor",
                    "records",
                    _ACTOR_COLUMNS,
                    1,
                    record_kind="actor",
                ),
            ],
            record_roles={
                "actor": {"driver": "dimension", "ride": "fact", "bus": "dimension"}
            },
            enum_domains={"actor": {"actor_type": ["driver", "ride", "bus"]}},
            sub_type_columns={
                "actor": {
                    "driver": ["prop__name"],
                    "ride": [],
                    "bus": ["prop__status"],
                }
            },
        ),
    )
    return tmp_path


def build_membership_emit(tmp_path: Path) -> Path:
    """Build an emit with a kind that owns a membership table."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(_create_ddl("membership__location__zones", _MEMBERSHIP_COLUMNS))
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "loc1", 10, True, 10, 0, "Depot"],
    )
    conn.execute(
        'INSERT INTO "membership__location__zones" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "loc1", 5, "loader", "zone", "z001"],
    )
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__location",
                    "records",
                    _LOCATION_COLUMNS,
                    1,
                    record_kind="location",
                ),
                _table_spec(
                    "membership__location__zones",
                    "membership",
                    _MEMBERSHIP_COLUMNS,
                    1,
                    record_kind="location",
                    property_name="zones",
                ),
            ],
            record_roles={"location": "dimension"},
        ),
    )
    return tmp_path


def build_no_record_roles_emit(tmp_path: Path) -> Path:
    """Build an emit whose sidecar omits record_roles entirely."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "loc1", 10, True, 10, 0, "Depot"],
    )
    conn.close()
    _write_sidecar(
        tmp_path,
        _base_sidecar(
            tables=[
                _table_spec(
                    "records__location",
                    "records",
                    _LOCATION_COLUMNS,
                    1,
                    record_kind="location",
                ),
            ],
            record_roles=None,
        ),
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests: basic dispatch
# ---------------------------------------------------------------------------


def test_cmd_init_writes_to_file(tmp_path: Path) -> None:
    """cmd_init writes a candidate config to out_path and returns 0."""
    emit_dir = build_bare_dim_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"

    exit_code = cmd_init(emit_dir, out_path)
    assert exit_code == 0
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "mode: dimensional" in content
    assert "dim_location" in content


def test_cmd_init_writes_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_init writes to stdout when out_path is None and returns 0."""
    emit_dir = build_bare_dim_emit(tmp_path / "emit")

    exit_code = cmd_init(emit_dir, None)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert len(captured.out) > 0
    assert "dimensional" in captured.out


def test_main_init_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['init', emit_dir, out_path]) dispatches to cmd_init correctly."""
    emit_dir = build_bare_dim_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"

    exit_code = main(["init", str(emit_dir), str(out_path)])
    assert exit_code == 0
    assert out_path.exists()


def test_main_init_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """main(['init', emit_dir]) with no out_path writes to stdout."""
    emit_dir = build_bare_dim_emit(tmp_path / "emit")

    exit_code = main(["init", str(emit_dir)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "mode: dimensional" in captured.out
    assert "dim_location" in captured.out


# ---------------------------------------------------------------------------
# Tests: bare-string dimension kind, no history_tracked -> scd: type1
# ---------------------------------------------------------------------------


def test_bare_dim_no_history_tracked_proposes_type1(tmp_path: Path) -> None:
    """Bare-string dimension kind, no history_tracked -> one role: dim, scd: type1 stub."""
    emit_dir = build_bare_dim_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "dim_location" in content
    assert "role: dim" in content
    assert "scd: type1" in content


# ---------------------------------------------------------------------------
# Tests: bare-string dimension kind with history_tracked -> scd: type2
# ---------------------------------------------------------------------------


def test_bare_dim_with_history_tracked_proposes_type2(tmp_path: Path) -> None:
    """Bare-string dim kind with >= 1 history_tracked -> role: dim, scd: type2."""
    emit_dir = build_bare_dim_scd2_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "dim_sensor" in content
    assert "role: dim" in content
    assert "scd: type2" in content
    # tracked column appears with marker
    assert "{name: status, from: prop__status}  # tracked -> per-version" in content
    # untracked column appears without marker
    assert "{name: label, from: prop__label}" in content
    assert "{name: label, from: prop__label}  # tracked -> per-version" not in content
    # scd_window columns present
    assert "valid_from" in content
    assert "valid_to" in content
    # role-scoped: identity/lifecycle columns are never proposed
    assert "created_sim_time" not in content
    assert "last_mutation_sim_time" not in content
    assert "from: active" not in content
    assert "from: deactivated_at" not in content


# ---------------------------------------------------------------------------
# Tests: non-exempt slice_only column skipped from SCD-2 stub, with notice
# ---------------------------------------------------------------------------


def test_slice_only_column_omitted_from_scd2_columns(tmp_path: Path) -> None:
    """A non-exempt slice_only column is absent from the SCD-2 stub's columns."""
    emit_dir = build_bare_dim_scd2_with_slice_only_column_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "{name: region, from: prop__region}" not in content
    # Its unaffected sibling columns still appear
    assert "{name: label, from: prop__label}" in content
    assert "{name: status, from: prop__status}  # tracked -> per-version" in content


def test_slice_only_skip_does_not_suppress_the_kind(tmp_path: Path) -> None:
    """The kind is still proposed (as dim/type2) — the skip is column-level."""
    emit_dir = build_bare_dim_scd2_with_slice_only_column_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "dim_sensor" in content
    assert "role: dim" in content
    assert "scd: type2" in content


def test_slice_only_skip_emits_stderr_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One 'slice-only-column-omitted' notice per skip is written to stderr,
    naming the kind and column, via cmd_init's render_notice_stderr sink."""
    emit_dir = build_bare_dim_scd2_with_slice_only_column_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    captured = capsys.readouterr()
    assert "notice:" in captured.err
    assert "kind 'sensor'" in captured.err
    assert "column 'prop__region'" in captured.err
    assert "temporal_class: slice_only" in captured.err
    assert "omitted from the SCD-2 stub's column proposal" in captured.err


def test_slice_only_skip_notice_via_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['init', ...]) surfaces the same skip notice on stderr."""
    emit_dir = build_bare_dim_scd2_with_slice_only_column_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    exit_code = main(["init", str(emit_dir), str(out_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "notice:" in captured.err
    assert "column 'prop__region'" in captured.err


# ---------------------------------------------------------------------------
# Tests: exempt discriminator remains proposable, filter pre-fill unchanged
# ---------------------------------------------------------------------------


def test_exempt_discriminator_slice_only_class_still_proposed(tmp_path: Path) -> None:
    """A subtyped kind's prop__<kind>_type is exempt at any class — even
    temporal_class: slice_only — and remains in the SCD-2 stub's columns."""
    emit_dir = build_object_valued_actor_emit(
        tmp_path / "emit", columns=_ACTOR_COLUMNS_SLICE_ONLY_DISCRIMINATOR
    )
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "dim_actor_driver" in content
    assert "fact_actor_ride" in content
    assert "{name: actor_type, from: prop__actor_type}" in content


def test_exempt_discriminator_filter_prefill_unchanged(tmp_path: Path) -> None:
    """The filter pre-fill per sub-type is unaffected by the discriminator's
    own temporal_class."""
    emit_dir = build_object_valued_actor_emit(
        tmp_path / "emit", columns=_ACTOR_COLUMNS_SLICE_ONLY_DISCRIMINATOR
    )
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "prop__actor_type: driver" in content
    assert "prop__actor_type: ride" in content


def test_exempt_discriminator_slice_only_no_skip_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exempt discriminator never triggers a slice-only-column-omitted
    notice, regardless of its own class."""
    emit_dir = build_object_valued_actor_emit(
        tmp_path / "emit", columns=_ACTOR_COLUMNS_SLICE_ONLY_DISCRIMINATOR
    )
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    captured = capsys.readouterr()
    assert "slice-only-column-omitted" not in captured.err
    assert "actor_type" not in captured.err


# ---------------------------------------------------------------------------
# Tests: bare-string fact kind with no prop__<kind>_type -> single fact stub
# ---------------------------------------------------------------------------


def test_bare_fact_no_discriminator_proposes_single_stub(tmp_path: Path) -> None:
    """Bare-string fact kind with no prop__<kind>_type -> one role: fact stub."""
    emit_dir = build_bare_fact_no_discriminator_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "fact_event" in content
    assert "role: fact" in content


def test_bare_fact_no_discriminator_has_fk_candidate_comment(tmp_path: Path) -> None:
    """Bare-string fact kind includes FK-candidate comment per reference column."""
    emit_dir = build_bare_fact_no_discriminator_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    # event has prop__location_id referencing location -> FK candidate comment
    assert "FK candidate" in content
    assert "location" in content


# ---------------------------------------------------------------------------
# Tests: bare-string fact kind with modelling prop__<kind>_type -> per-DISTINCT stubs
# ---------------------------------------------------------------------------


def test_bare_fact_with_discriminator_proposes_per_value_stubs(tmp_path: Path) -> None:
    """Bare-string fact kind with prop__<kind>_type -> per-DISTINCT-value stubs."""
    emit_dir = build_bare_fact_with_discriminator_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    # Two observed values
    assert "fact_trip_delivery" in content
    assert "fact_trip_pickup" in content
    assert "role: fact" in content
    # Filter pre-filled for each
    assert "prop__trip_type: delivery" in content
    assert "prop__trip_type: pickup" in content
    # Marks as SELECT DISTINCT observed value
    assert "SELECT DISTINCT observed value" in content


# ---------------------------------------------------------------------------
# Tests: object-valued kind actor:{driver: dimension, ride: fact}
# ---------------------------------------------------------------------------


def test_object_valued_kind_splits_per_subtype(tmp_path: Path) -> None:
    """Object-valued kind produces dim_actor_driver, fact_actor_ride, dim_actor_bus."""
    emit_dir = build_object_valued_actor_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    # Each sub-type gets its own stub
    assert "dim_actor_driver" in content
    assert "fact_actor_ride" in content


def test_object_valued_kind_filters_per_subtype(tmp_path: Path) -> None:
    """Each sub-type stub has filter:{prop__actor_type: <sub_type>}."""
    emit_dir = build_object_valued_actor_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "prop__actor_type: driver" in content
    assert "prop__actor_type: ride" in content


def test_object_valued_kind_driver_is_scd2(tmp_path: Path) -> None:
    """Object-valued dim sub-type with history_tracked columns gets scd: type2."""
    emit_dir = build_object_valued_actor_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    # driver is a dimension; actor has history_tracked -> scd: type2
    assert "scd: type2" in content


def test_object_valued_kind_unobserved_subtype_yields_stub(tmp_path: Path) -> None:
    """Declared-but-unobserved sub-type (bus) still yields a stub."""
    emit_dir = build_object_valued_actor_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    # bus is declared but has no rows in the table; stub still proposed
    assert "dim_actor_bus" in content


# ---------------------------------------------------------------------------
# Tests: sub_type_columns partition prunes per-sub-type stub columns
# ---------------------------------------------------------------------------


def _actor_stub_block(content: str, name: str) -> str:
    """The generated YAML for one table stub, from its `- name:` line to the
    start of the next stub / comment header (for scoped per-sub-type asserts)."""
    marker = f"- name: {name}\n"
    start = content.index(marker)
    rest = content[start + len(marker) :]
    # Next stub begins at the next "    - name:" or "    # ---" header.
    ends = [rest.index(m) for m in ("    - name:", "    # ---") if m in rest]
    return rest[: min(ends)] if ends else rest


def test_sub_type_columns_prunes_driver_to_owned_column(tmp_path: Path) -> None:
    """With a partition, dim_actor_driver proposes its owned prop__name and NOT
    prop__status, which belongs to a different sub-type."""
    emit_dir = build_actor_emit_with_sub_type_columns(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    driver = _actor_stub_block(out_path.read_text(encoding="utf-8"), "dim_actor_driver")
    assert "from: prop__name" in driver
    assert "from: prop__status" not in driver


def test_sub_type_columns_prunes_bus_to_owned_column(tmp_path: Path) -> None:
    """With a partition, dim_actor_bus proposes its owned prop__status and NOT
    prop__name."""
    emit_dir = build_actor_emit_with_sub_type_columns(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    bus = _actor_stub_block(out_path.read_text(encoding="utf-8"), "dim_actor_bus")
    assert "from: prop__status" in bus
    assert "from: prop__name" not in bus


def test_sub_type_columns_discriminator_survives_pruning(tmp_path: Path) -> None:
    """The kind-wide discriminator is never pruned — it belongs to no sub-type's
    list yet stays proposable in every per-sub-type stub (contract carve-out)."""
    emit_dir = build_actor_emit_with_sub_type_columns(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    driver = _actor_stub_block(out_path.read_text(encoding="utf-8"), "dim_actor_driver")
    assert "from: prop__actor_type" in driver


def test_no_partition_keeps_full_union_columns(tmp_path: Path) -> None:
    """Fallback: without sub_type_columns, every sub-type stub proposes the whole
    union — driver lists both prop__name and prop__status (no pruning)."""
    emit_dir = build_object_valued_actor_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    driver = _actor_stub_block(out_path.read_text(encoding="utf-8"), "dim_actor_driver")
    assert "from: prop__name" in driver
    assert "from: prop__status" in driver


# ---------------------------------------------------------------------------
# Tests: membership FK candidate comments
# ---------------------------------------------------------------------------


def test_membership_kind_appends_fk_comments(tmp_path: Path) -> None:
    """Kind owning a membership__<kind>__<property> table gets FK candidate comments."""
    emit_dir = build_membership_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    # Membership FK candidate comment appears
    assert "Membership FK" in content
    assert "role_name" in content


# ---------------------------------------------------------------------------
# Tests: no exclude block, no topology/likely-internal comments
# ---------------------------------------------------------------------------


def test_no_exclude_block_proposed(tmp_path: Path) -> None:
    """No exclude block is proposed in the generated config."""
    emit_dir = build_bare_dim_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "exclude" not in content


def test_no_likely_internal_comment(tmp_path: Path) -> None:
    """No 'likely-internal' or topology comments appear in the generated config."""
    emit_dir = build_bare_dim_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    content = out_path.read_text(encoding="utf-8")
    assert "likely-internal" not in content
    assert "topology" not in content


# ---------------------------------------------------------------------------
# Tests: generated YAML is loadable
# ---------------------------------------------------------------------------


def test_generated_yaml_loadable_by_load_export_config(tmp_path: Path) -> None:
    """Generated candidate config is valid YAML that load_export_config accepts."""
    from fabulexa_forge.config.loader import load_export_config

    emit_dir = build_bare_dim_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    # Must not raise ConfigError
    load_export_config(out_path)


def test_generated_yaml_loadable_scd2(tmp_path: Path) -> None:
    """SCD-2 candidate config is valid YAML that load_export_config accepts."""
    from fabulexa_forge.config.loader import load_export_config

    emit_dir = build_bare_dim_scd2_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    load_export_config(out_path)


def test_generated_yaml_loadable_fact(tmp_path: Path) -> None:
    """Fact candidate config is valid YAML that load_export_config accepts."""
    from fabulexa_forge.config.loader import load_export_config

    emit_dir = build_bare_fact_no_discriminator_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    load_export_config(out_path)


def test_generated_yaml_loadable_object_valued(tmp_path: Path) -> None:
    """Object-valued kind candidate config is valid YAML that load_export_config accepts."""
    from fabulexa_forge.config.loader import load_export_config

    emit_dir = build_object_valued_actor_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"
    cmd_init(emit_dir, out_path)
    load_export_config(out_path)


# ---------------------------------------------------------------------------
# Tests: record_roles absent -> ERROR on stderr, return 1
# ---------------------------------------------------------------------------


def test_cmd_init_no_record_roles_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """record_roles absent -> cmd_init prints ERROR: to stderr and returns 1."""
    emit_dir = build_no_record_roles_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"

    exit_code = cmd_init(emit_dir, out_path)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR:" in captured.err
    # No traceback escapes
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_main_init_no_record_roles_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['init', ...]) with absent record_roles prints ERROR: to stderr, returns 1."""
    emit_dir = build_no_record_roles_emit(tmp_path / "emit")
    out_path = tmp_path / "candidate.yaml"

    exit_code = main(["init", str(emit_dir), str(out_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ERROR:" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# Tests: determinism
# ---------------------------------------------------------------------------


def test_output_deterministic_across_two_runs(tmp_path: Path) -> None:
    """Output is deterministic across two runs of the same emit."""
    emit_dir = build_object_valued_actor_emit(tmp_path / "emit")
    out1 = tmp_path / "run1.yaml"
    out2 = tmp_path / "run2.yaml"
    cmd_init(emit_dir, out1)
    cmd_init(emit_dir, out2)
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")
