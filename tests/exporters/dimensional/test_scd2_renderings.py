"""Per-version value rendering tests for `scd: type2` dims
(scd2-per-version-renderings sprint, Phase 1).

Verifies the design doc's § Per-version evaluation, end-to-end through
`build_query_specs` against fixture emits: the five pure per-row value
renderings (`derived: decimal` / `json_precision` / `timestamp` /
`date_parse` / `value_map`) over a tracked source, evaluated per version;
version structure is election-invariant (version count and
`valid_from`/`valid_to` unchanged under any value rendering); export-time
guards fire on historical (non-latest) version values, not just current
state; source-class-blind rendering (a constant source renders per record,
byte-identical to the same election on a records-grain fact); the exempt
sub-typed discriminator's per-record carve-out; and the windowed
`build_scd2_rows_sql` path.

Refusal coverage for unsupported column modes on type2 (fk / correlation /
ordinal / elapsed) and the widened Scd2ColumnModeSupported gate itself live
in `test_scd2_source_filter.py`; this module covers only the new
per-version rendering surface.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime as dt
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DecimalSpec,
    DerivedSpec,
    DimensionalConfig,
    JsonPrecisionSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
    ValueMapSpec,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import RunDatabaseError

# ---------------------------------------------------------------------------
# Shared fixtures: records__actor + history
# ---------------------------------------------------------------------------

_HISTORY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    {"name": "kind", "type": "VARCHAR"},
    identity_column("record_id", "VARCHAR"),
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _actor_base_columns() -> list[dict[str, object]]:
    """The identity/lifecycle prefix every records__actor fixture shares."""
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
    ]


def _build_actor_emit(
    tmp_path: Path,
    prop_columns: list[dict[str, object]],
    actor_rows: list[tuple[object, ...]],
    history_rows: list[tuple[str, str, str, str, int, str | None]],
    enum_domains: dict[str, dict[str, list[str]]] | None = None,
) -> Path:
    """Build a records__actor + history emit for one rendering scenario.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        prop_columns: The kind-specific prop__ columns, appended after the
            shared identity/lifecycle prefix.
        actor_rows: Rows for records__actor, in column order.
        history_rows: Rows for the fixed-category history table.
        enum_domains: Optional enum_domains sidecar block (the
            sub-typed-discriminator carve-out's oracle).

    Returns:
        tmp_path (the emit directory).
    """
    columns = [*_actor_base_columns(), *prop_columns]
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", columns))
    for row in actor_rows:
        placeholders = ", ".join(["?"] * len(row))
        conn.execute(f'INSERT INTO "records__actor" VALUES ({placeholders})', list(row))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    for hist_row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(hist_row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                columns,
                len(actor_rows),
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={"enum_domains": enum_domains} if enum_domains is not None else None,
    )
    return tmp_path


def _scd2_table_decl(value_column: ColumnDecl, name: str = "dim_patient") -> TableDecl:
    """A standard scd: type2 dim over records__actor with one value column."""
    return TableDecl(
        name=name,
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            value_column,
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )


def _records_fact_table_decl(
    value_column: ColumnDecl, name: str = "fact_actor"
) -> TableDecl:
    """A records-grain fact over the same kind, for source-class-blind
    comparison against a type2 dim's constant-source rendering."""
    return TableDecl(
        name=name,
        role="fact",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            value_column,
        ],
    )


def _export_all(
    emit_dir: Path,
    table_decls: list[TableDecl],
    anchor: EffectiveAnchor | None = None,
    window: Window | None = None,
) -> list[dict[str, list[object]]]:
    """Compile and run every table_decls' query, returning one to_pydict()
    result per table, in declaration order."""
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            DimensionalConfig(tables=table_decls),
            anchor,
            window,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        return [emit.query_arrow(spec.sql, ()).to_pydict() for spec in specs]


def _export_rows(
    emit_dir: Path,
    table_decl: TableDecl,
    anchor: EffectiveAnchor | None = None,
) -> dict[str, list[object]]:
    """Compile and run one table_decl's full-export query."""
    return _export_all(emit_dir, [table_decl], anchor)[0]


# ---------------------------------------------------------------------------
# derived: decimal — per-version, election-invariant, never-changed, overflow
# ---------------------------------------------------------------------------


def _build_decimal_scenario_emit(tmp_path: Path) -> Path:
    """a1: genesis-null then two colliding noisy DOUBLE values (4.801 /
    4.804 -> both DECIMAL(5,2) 4.80). a2: a single genesis value, never
    changed post-creation (one reconstructed version)."""
    return _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__engagement_score",
                "DOUBLE",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[
            ("trunk", "a1", 0, True, None, 20, 0, 4.804),
            ("trunk", "a2", 100, True, None, 100, 1, 7.5),
        ],
        history_rows=[
            ("trunk", "actor", "a1", "engagement_score", 0, None),
            ("trunk", "actor", "a1", "engagement_score", 10, "4.801"),
            ("trunk", "actor", "a1", "engagement_score", 20, "4.804"),
            ("trunk", "actor", "a2", "engagement_score", 100, "7.5"),
        ],
    )


def test_decimal_per_version_rendering_and_election_invariant(tmp_path: Path) -> None:
    """Each version row carries the rounded DECIMAL(p, s) value; a genesis-
    null pre-first-assignment version renders NULL; adjacent versions whose
    rendered values collide (4.801 / 4.804 -> 4.80) both stay distinct
    version rows. Version count and valid_from/valid_to are identical to
    the same table exported with `from` instead (version structure is
    election-invariant)."""
    emit_dir = _build_decimal_scenario_emit(tmp_path)
    decimal_decl = _scd2_table_decl(
        ColumnDecl(
            name="score",
            derived=DerivedSpec(
                decimal=DecimalSpec(**{"from": "prop__engagement_score", "as": [5, 2]})
            ),
        )
    )
    from_decl = _scd2_table_decl(
        ColumnDecl(name="score", **{"from": "prop__engagement_score"})
    )

    decimal_rows = _export_rows(emit_dir, decimal_decl)
    from_rows = _export_rows(emit_dir, from_decl)

    assert decimal_rows["id"] == from_rows["id"] == ["a1", "a1", "a1", "a2"]
    assert decimal_rows["valid_from"] == from_rows["valid_from"] == [0, 10, 20, 100]
    assert decimal_rows["valid_to"] == from_rows["valid_to"] == [10, 20, None, None]
    assert decimal_rows["score"] == [
        None,
        Decimal("4.80"),
        Decimal("4.80"),
        Decimal("7.50"),
    ]


def test_decimal_tracked_never_changed_single_version(tmp_path: Path) -> None:
    """A tracked prop whose value never changed post-creation reconstructs
    to one version row, rendered once."""
    emit_dir = _build_decimal_scenario_emit(tmp_path)
    decimal_decl = _scd2_table_decl(
        ColumnDecl(
            name="score",
            derived=DerivedSpec(
                decimal=DecimalSpec(**{"from": "prop__engagement_score", "as": [5, 2]})
            ),
        )
    )
    rows = _export_rows(emit_dir, decimal_decl)
    a2_index = rows["id"].index("a2")
    assert rows["id"].count("a2") == 1
    assert rows["score"][a2_index] == Decimal("7.50")
    assert rows["valid_to"][a2_index] is None


def test_decimal_overflow_in_historical_version_raises(tmp_path: Path) -> None:
    """A decimal overflow in a historical (non-latest) version's value
    raises the loud export-time error, naming table, column, and the
    offending value — not just current state."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__amount", "DOUBLE", history_tracked=True, temporal_class="tracked"
            )
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 20, 0, 2.5)],
        history_rows=[
            ("trunk", "actor", "a1", "amount", 0, "1.00"),
            ("trunk", "actor", "a1", "amount", 10, "1234.5"),
            ("trunk", "actor", "a1", "amount", 20, "2.50"),
        ],
    )
    decimal_decl = _scd2_table_decl(
        ColumnDecl(
            name="amount",
            derived=DerivedSpec(
                decimal=DecimalSpec(**{"from": "prop__amount", "as": [4, 2]})
            ),
        )
    )
    with pytest.raises(RunDatabaseError, match="dim_patient") as exc_info:
        _export_rows(emit_dir, decimal_decl)
    assert "amount" in str(exc_info.value)


# ---------------------------------------------------------------------------
# derived: value_map — per-version, unmapped historical value is typed NULL
# ---------------------------------------------------------------------------


def test_value_map_tracked_per_version_unmapped_historical_is_null(
    tmp_path: Path,
) -> None:
    """A tracked code prop's mapped values render per version; an unmapped
    historical (non-latest) value renders typed NULL."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__status_code",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 20, 0, "B")],
        history_rows=[
            ("trunk", "actor", "a1", "status_code", 0, "A"),
            ("trunk", "actor", "a1", "status_code", 10, "C"),
            ("trunk", "actor", "a1", "status_code", 20, "B"),
        ],
    )
    decl = _scd2_table_decl(
        ColumnDecl(
            name="code",
            derived=DerivedSpec(
                value_map=ValueMapSpec(
                    **{"from": "prop__status_code"}, map={"A": 1, "B": 2}
                )
            ),
        )
    )
    rows = _export_rows(emit_dir, decl)
    assert rows["code"] == [1, None, 2]


# ---------------------------------------------------------------------------
# derived: date_parse — per-version, historical strict-parse failure
# ---------------------------------------------------------------------------


def test_date_parse_tracked_per_version(tmp_path: Path) -> None:
    """A tracked VARCHAR date prop's per-version values parse to the
    declared format's denoted type."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__event_date",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 10, 0, "2024-02-15")],
        history_rows=[
            ("trunk", "actor", "a1", "event_date", 0, "2024-01-01"),
            ("trunk", "actor", "a1", "event_date", 10, "2024-02-15"),
        ],
    )
    decl = _scd2_table_decl(
        ColumnDecl(
            name="parsed",
            derived=DerivedSpec(
                date_parse=DateParseSpec(
                    **{"from": "prop__event_date", "format": "%Y-%m-%d"}
                )
            ),
        )
    )
    rows = _export_rows(emit_dir, decl)
    assert rows["parsed"] == [date(2024, 1, 1), date(2024, 2, 15)]


def test_date_parse_historical_value_fails_format_raises(tmp_path: Path) -> None:
    """A historical (non-latest) value that fails the declared format fails
    the export loudly — not just the current one."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__event_date",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 10, 0, "2024-03-01")],
        history_rows=[
            ("trunk", "actor", "a1", "event_date", 0, "not-a-date"),
            ("trunk", "actor", "a1", "event_date", 10, "2024-03-01"),
        ],
    )
    decl = _scd2_table_decl(
        ColumnDecl(
            name="parsed",
            derived=DerivedSpec(
                date_parse=DateParseSpec(
                    **{"from": "prop__event_date", "format": "%Y-%m-%d"}
                )
            ),
        )
    )
    with pytest.raises(RunDatabaseError, match="does not match format"):
        _export_rows(emit_dir, decl)


# ---------------------------------------------------------------------------
# derived: timestamp — per-version anchored rendering, no-anchor raw ns
# ---------------------------------------------------------------------------


def _build_timestamp_scenario_emit(tmp_path: Path) -> Path:
    """A tracked BIGINT sim-instant payload prop with two versions: 1h and
    25h past created_sim_time=0 (crossing a day boundary once anchored)."""
    return _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__activity_ns",
                "BIGINT",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 10, 0, 90_000_000_000_000)],
        history_rows=[
            ("trunk", "actor", "a1", "activity_ns", 0, "3600000000000"),
            ("trunk", "actor", "a1", "activity_ns", 10, "90000000000000"),
        ],
    )


def test_timestamp_tracked_per_version_with_anchor(tmp_path: Path) -> None:
    """A tracked BIGINT sim-instant payload renders anchored, per version."""
    emit_dir = _build_timestamp_scenario_emit(tmp_path)
    decl = _scd2_table_decl(
        ColumnDecl(
            name="activity_date",
            derived=DerivedSpec(
                timestamp=TimestampSpec(source="prop__activity_ns", **{"as": "date"})
            ),
        )
    )
    anchor = EffectiveAnchor(
        start_instant=dt.fromisoformat("2024-06-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    rows = _export_rows(emit_dir, decl, anchor)
    assert rows["activity_date"] == [date(2024, 6, 1), date(2024, 6, 2)]


def test_timestamp_tracked_no_anchor_renders_raw_ns(tmp_path: Path) -> None:
    """With no resolved anchor, the unelected shorthand renders raw ns per
    version — reachable from a tracked source."""
    emit_dir = _build_timestamp_scenario_emit(tmp_path)
    decl = _scd2_table_decl(
        ColumnDecl(
            name="activity_raw",
            derived=DerivedSpec(timestamp=TimestampSpec(source="prop__activity_ns")),
        )
    )
    rows = _export_rows(emit_dir, decl, anchor=None)
    assert rows["activity_raw"] == [3_600_000_000_000, 90_000_000_000_000]


# ---------------------------------------------------------------------------
# derived: json_precision — per-version, byte preservation, invalid payload
# ---------------------------------------------------------------------------


def test_json_precision_tracked_per_version_rounds_leaf_preserves_bytes(
    tmp_path: Path,
) -> None:
    """A tracked VARCHAR JSON payload's named leaf is rounded per version;
    every other byte is preserved."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__metrics",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[
            ("trunk", "a1", 0, True, None, 10, 0, '{"amount": 20.5, "flag": false}')
        ],
        history_rows=[
            ("trunk", "actor", "a1", "metrics", 0, '{"amount": 10.128, "flag": true}'),
            ("trunk", "actor", "a1", "metrics", 10, '{"amount": 20.5, "flag": false}'),
        ],
    )
    decl = _scd2_table_decl(
        ColumnDecl(
            name="metrics",
            derived=DerivedSpec(
                json_precision=JsonPrecisionSpec(
                    **{"from": "prop__metrics"}, leaves={"amount": 2}
                )
            ),
        )
    )
    rows = _export_rows(emit_dir, decl)
    assert rows["metrics"] == [
        '{"amount": 10.13, "flag": true}',
        '{"amount": 20.50, "flag": false}',
    ]


def test_json_precision_invalid_json_historical_raises(tmp_path: Path) -> None:
    """Invalid JSON in a historical (non-latest) version fails the export
    loudly, naming table and column."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__metrics",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 10, 0, '{"amount": 1.0}')],
        history_rows=[
            ("trunk", "actor", "a1", "metrics", 0, "not-json"),
            ("trunk", "actor", "a1", "metrics", 10, '{"amount": 1.0}'),
        ],
    )
    decl = _scd2_table_decl(
        ColumnDecl(
            name="metrics",
            derived=DerivedSpec(
                json_precision=JsonPrecisionSpec(
                    **{"from": "prop__metrics"}, leaves={"amount": 2}
                )
            ),
        )
    )
    with pytest.raises(
        RunDatabaseError, match="json_precision requires a JSON object payload"
    ) as exc_info:
        _export_rows(emit_dir, decl)
    assert "metrics" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Source-class-blind: constant source renders per record, identically on
# type2 and on a records-grain fact
# ---------------------------------------------------------------------------


def test_decimal_source_class_blind_constant_matches_records_grain(
    tmp_path: Path,
) -> None:
    """A derived: decimal over a constant (untracked) DOUBLE prop on type2
    renders per record, byte-identical to the same election on a
    records-grain fact over the same emit."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__balance",
                "DOUBLE",
                history_tracked=False,
                temporal_class="constant",
            ),
            prop_column(
                "prop__engagement_score",
                "DOUBLE",
                history_tracked=True,
                temporal_class="tracked",
            ),
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 10, 0, 42.567, 2.0)],
        history_rows=[
            ("trunk", "actor", "a1", "engagement_score", 0, "1.0"),
            ("trunk", "actor", "a1", "engagement_score", 10, "2.0"),
        ],
    )
    balance_col = ColumnDecl(
        name="balance",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__balance", "as": [5, 2]})
        ),
    )
    dim_decl = _scd2_table_decl(balance_col)
    fact_decl = _records_fact_table_decl(balance_col)

    dim_rows, fact_rows = _export_all(emit_dir, [dim_decl, fact_decl])

    # The tracked engagement_score generates 2 versions for a1; the constant
    # balance repeats identically across both, and matches the fact's one
    # per-record value byte-for-byte.
    assert dim_rows["balance"] == [Decimal("42.57"), Decimal("42.57")]
    assert fact_rows["balance"] == [Decimal("42.57")]
    assert dim_rows["balance"][0] == fact_rows["balance"][0]


# ---------------------------------------------------------------------------
# Exempt sub-typed discriminator: value_map legal, renders per record
# ---------------------------------------------------------------------------


def test_value_map_exempt_discriminator_renders_per_record(tmp_path: Path) -> None:
    """A derived: value_map over prop__<K>_type (non-empty subtype_values)
    is legal on type2 and renders per record from the current
    classification value — constant across the tracked prop's versions."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__actor_type",
                "VARCHAR",
                history_tracked=False,
                temporal_class="slice_only",
            ),
            prop_column(
                "prop__engagement_score",
                "DOUBLE",
                history_tracked=True,
                temporal_class="tracked",
            ),
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 10, 0, "patient", 2.0)],
        history_rows=[
            ("trunk", "actor", "a1", "engagement_score", 0, "1.0"),
            ("trunk", "actor", "a1", "engagement_score", 10, "2.0"),
        ],
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    decl = _scd2_table_decl(
        ColumnDecl(
            name="classification",
            derived=DerivedSpec(
                value_map=ValueMapSpec(
                    **{"from": "prop__actor_type"}, map={"patient": 1, "staff": 2}
                )
            ),
        )
    )
    rows = _export_rows(emit_dir, decl)
    assert rows["classification"] == [1, 1]


# ---------------------------------------------------------------------------
# Windowed: build_scd2_rows_sql renders per version inside the window;
# __valid_from_ns and window membership read raw bounds
# ---------------------------------------------------------------------------


def test_windowed_rows_render_per_version_and_read_raw_bounds(tmp_path: Path) -> None:
    """The windowed __rows path emits per-version rendered values inside
    the window; __valid_from_ns and window membership use the raw,
    unrendered version bounds."""
    emit_dir = _build_actor_emit(
        tmp_path,
        prop_columns=[
            prop_column(
                "prop__engagement_score",
                "DOUBLE",
                history_tracked=True,
                temporal_class="tracked",
            )
        ],
        actor_rows=[("trunk", "a1", 0, True, None, 20, 0, 4.804)],
        history_rows=[
            ("trunk", "actor", "a1", "engagement_score", 0, None),
            ("trunk", "actor", "a1", "engagement_score", 10, "4.801"),
            ("trunk", "actor", "a1", "engagement_score", 20, "4.804"),
        ],
    )
    decl = _scd2_table_decl(
        ColumnDecl(
            name="score",
            derived=DerivedSpec(
                decimal=DecimalSpec(**{"from": "prop__engagement_score", "as": [5, 2]})
            ),
        )
    )
    window = Window(index=0, start_ns=0, end_ns=15, label="w0")
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            DimensionalConfig(tables=[decl]),
            None,
            window,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        assert specs[0].table_name == "dim_patient__rows"
        rows = emit.query_arrow(specs[0].sql, ()).to_pydict()

    # Window [0, 15): the version starting at 20 falls outside and is absent.
    assert rows["__valid_from_ns"] == [0, 10]
    assert rows["valid_from"] == [0, 10]
    assert rows["score"] == [None, Decimal("4.80")]
