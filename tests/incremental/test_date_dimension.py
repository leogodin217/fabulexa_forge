"""Tests for incremental delivery of the generated calendar (Phase 4 of the
date-dimension sprint): `dim_date` compiled `snapshot`/`replace` in every
emitting window (first, later, empty, and an explicit `--from/--to` range),
its position in the window's report, the range guard as the last pre-write
gate (after the source-is-output gate and the overlay check), and the
fingerprint's sensitivity to `date_dimension.to`.

Uses the shared recipe fixture (`recipes._recipe_fixture.build_recipe_emit`)
sliced at a fixed tape end (`recipes._recipe_fixture` + `_support.slice_emit`,
the same oracle `tests/incremental/test_horizon_acceptance.py` uses) and the
`date-dimension` recipe's own config (`examples/recipes/date-dimension`) —
the emit backing both.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.slice_emit import slice_emit
from recipes._recipe_fixture import DAY, build_recipe_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateDimensionConfig,
    DateRefSpec,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    IncrementalConfig,
    SourceDecl,
    SupplementDecl,
    TableDecl,
)
from fabulexa_forge.errors import (
    DateRefOutOfRange,
    IncrementalFingerprintMismatch,
    SupplementSourceIsOutput,
)
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.dimensional.windowing import window_delivery_class
from fabulexa_forge.exporters.supplements import load_supplements
from fabulexa_forge.incremental.driver import export_incremental_next, export_window
from fabulexa_forge.incremental.windows import derive_window, parse_range
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor

_RECIPE_CONFIG_PATH = (
    Path(__file__).parent.parent.parent
    / "examples"
    / "recipes"
    / "date-dimension"
    / "config.yaml"
)

#: The recipe emit's data spans days 1..3 (1*DAY..3*DAY); a slice at the end
#: of day 4 bounds every instant, matching test_horizon_acceptance.py's own
#: bound — four day-cadence windows, the last emitting nothing new.
_TAPE_END = 4 * DAY - 1


def _sliced_recipe_emit(tmp_path: Path) -> Path:
    """The date-dimension recipe's own emit, sliced at `_TAPE_END`."""
    raw = tmp_path / "raw"
    build_recipe_emit(raw)
    return slice_emit(raw, tmp_path / "emit", _TAPE_END)


def _recipe_config(date_dimension: DateDimensionConfig | None = None) -> "ExportConfig":
    """The recipe's own config, day cadence, optionally with a narrowed range."""
    config = load_export_config(_RECIPE_CONFIG_PATH)
    update: dict[str, object] = {"incremental": IncrementalConfig(period="day")}
    if date_dimension is not None:
        update["date_dimension"] = date_dimension
    return config.model_copy(update=update)


def _anchor_for(emit_dir: Path) -> "EffectiveAnchor":
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    assert anchor is not None
    return anchor


def _out_of_range_date_dimension() -> DateDimensionConfig:
    """A range excluding the 3rd day-window's event date (2024-01-04)."""
    return DateDimensionConfig.model_validate(
        {"from": "1985-01-01", "to": "2024-01-03"}
    )


# ---------------------------------------------------------------------------
# dim_date: snapshot/replace, identical bytes, every emitting window
# ---------------------------------------------------------------------------


def test_dim_date_snapshot_identical_across_next_windows(tmp_path: Path) -> None:
    """`dim_date` is delivered every window (first, a later one, the empty
    last one), with byte-identical content."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    config = _recipe_config()
    out = tmp_path / "drops"

    labels: list[str] = []
    with open_emit(emit_dir) as emit:
        for _ in range(4):
            outcome = export_incremental_next(
                emit, config, out, "csv", anchor, discard_notice_sink, None, ()
            )
            assert outcome.status == "emitted"
            assert outcome.window is not None
            labels.append(outcome.window.label)

    contents = {(out / label / "dim_date.csv").read_bytes() for label in labels}
    assert len(contents) == 1


def test_dim_date_identical_on_explicit_range(tmp_path: Path) -> None:
    """A standalone `--from/--to` range delivers the same `dim_date` bytes."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    config = _recipe_config()
    drip_out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit, config, drip_out, "csv", anchor, discard_notice_sink, None, ()
        )
        assert outcome.status == "emitted"
        assert outcome.window is not None
        drip_bytes = (drip_out / outcome.window.label / "dim_date.csv").read_bytes()

        range_window = parse_range("2024-01-01", "2024-01-10", anchor)
        range_out = tmp_path / "range_out"
        export_window(
            emit,
            config,
            range_out,
            "csv",
            anchor,
            range_window,
            None,
            discard_notice_sink,
            None,
            (),
        )

    assert (range_out / "dim_date.csv").read_bytes() == drip_bytes


def test_dim_date_after_declared_tables_before_supplements(tmp_path: Path) -> None:
    """`dim_date` sits after the declared tables and before the supplements."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    supplement = SupplementDecl(
        name="rate_tier",
        columns={"tier": "VARCHAR"},
        rows=[{"tier": "standard"}],
    )
    config = _recipe_config().model_copy(update={"supplements": [supplement]})
    supplements = load_supplements(config, tmp_path)
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit, config, out, "csv", anchor, discard_notice_sink, None, supplements
        )
    assert outcome.status == "emitted"
    assert outcome.report is not None
    names = [table.name for table in outcome.report.tables]
    assert names == [
        "fact_status_event",
        "dim_patient",
        "dim_patient_status",
        "dim_date",
        "rate_tier",
    ]


# ---------------------------------------------------------------------------
# The range guard: raises before the window's write, ordered after the
# source-is-output gate and the overlay check
# ---------------------------------------------------------------------------


def test_out_of_range_key_raises_before_window_write_cursor_unadvanced(
    tmp_path: Path,
) -> None:
    """A window whose delta carries an out-of-range key raises
    `DateRefOutOfRange` before that window's write: earlier windows' files
    are intact, the cursor has not advanced, and no partial file for the
    window exists."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    config = _recipe_config(_out_of_range_date_dimension())
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        labels: list[str] = []
        for _ in range(3):
            outcome = export_incremental_next(
                emit, config, out, "csv", anchor, discard_notice_sink, None, ()
            )
            assert outcome.status == "emitted"
            assert outcome.window is not None
            labels.append(outcome.window.label)

        before = {
            label: (out / label / "dim_date.csv").read_bytes() for label in labels
        }
        fourth_window = derive_window(3, config.incremental, anchor)

        with pytest.raises(DateRefOutOfRange):
            export_incremental_next(
                emit, config, out, "csv", anchor, discard_notice_sink, None, ()
            )

    for label, data in before.items():
        assert (out / label / "dim_date.csv").read_bytes() == data
    assert not (out / fourth_window.label).exists()
    assert not (out / f".tmp_{fourth_window.label}").exists()


def test_guard_runs_after_source_is_output_gate(tmp_path: Path) -> None:
    """A `dim_date.csv`-sourced supplement is refused as source-is-output
    even when a key is out of range — the source-is-output gate runs first."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    config = _recipe_config(_out_of_range_date_dimension())
    out = tmp_path / "drops"
    window = derive_window(3, config.incremental, anchor)
    collision = out / window.label / "dim_date.csv"
    collision.parent.mkdir(parents=True)
    collision.write_text("code,label\n", encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(collision),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    supplements = load_supplements(
        config.model_copy(update={"supplements": [decl]}), tmp_path
    )

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_window(
                emit,
                config,
                out,
                "csv",
                anchor,
                window,
                None,
                discard_notice_sink,
                None,
                supplements,
            )


# ---------------------------------------------------------------------------
# Fingerprint sensitivity to date_dimension.to
# ---------------------------------------------------------------------------


def test_changing_date_dimension_to_is_fingerprint_mismatch(tmp_path: Path) -> None:
    """Changing `date_dimension.to` between windows is a fingerprint
    mismatch; re-running with the original range is not."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    original = _recipe_config()
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit, original, out, "csv", anchor, discard_notice_sink, None, ()
        )
        assert outcome.status == "emitted"

        narrowed = _recipe_config(_out_of_range_date_dimension())
        with pytest.raises(IncrementalFingerprintMismatch):
            export_incremental_next(
                emit, narrowed, out, "csv", anchor, discard_notice_sink, None, ()
            )

        outcome2 = export_incremental_next(
            emit, original, out, "csv", anchor, discard_notice_sink, None, ()
        )
    assert outcome2.status == "emitted"


# ---------------------------------------------------------------------------
# date_ref and window delivery class: upsert (mutable source) vs append
# ---------------------------------------------------------------------------


def _fact_deactivation() -> TableDecl:
    return TableDecl(
        name="fact_deactivation",
        role="fact",
        source=SourceDecl(grain="records", kind="patient"),
        key=["patient_id"],
        columns=[
            ColumnDecl(name="patient_id", **{"from": "record_id"}),
            ColumnDecl(
                name="deactivated_key", date_ref=DateRefSpec(source="deactivated_at")
            ),
        ],
    )


def _fact_created() -> TableDecl:
    return TableDecl(
        name="fact_created",
        role="fact",
        source=SourceDecl(grain="records", kind="patient"),
        key=["created_at"],
        columns=[ColumnDecl(name="created_at", **{"from": "created_sim_time"})],
    )


def test_date_ref_over_mutable_source_is_upsert(tmp_path: Path) -> None:
    """A fact with `date_ref {source: deactivated_at}` is delivered
    `upsert` — `deactivated_at` is a records structural column the producer
    may change after creation."""
    emit_dir = tmp_path / "emit"
    build_recipe_emit(emit_dir)
    table_decl = _fact_deactivation()
    config = DimensionalConfig(tables=[table_decl])
    with open_emit(emit_dir) as emit:
        assert window_delivery_class(table_decl, config, emit.sidecar) == "upsert"


def test_fact_without_date_ref_keyed_on_created_sim_time_is_append(
    tmp_path: Path,
) -> None:
    """The same fact without the `date_ref` column, keyed on
    `created_sim_time` (set once, never revised), is delivered `append`."""
    emit_dir = tmp_path / "emit"
    build_recipe_emit(emit_dir)
    table_decl = _fact_created()
    config = DimensionalConfig(tables=[table_decl])
    with open_emit(emit_dir) as emit:
        assert window_delivery_class(table_decl, config, emit.sidecar) == "append"


# ---------------------------------------------------------------------------
# Bound-shape date_ref: window_bounds forwarding + delivery class through
# export_window / export_incremental_next
# ---------------------------------------------------------------------------


def _dim_patient_status_append_decl() -> TableDecl:
    """A type-2 dim declaring only the `valid_from` bound key (and no
    `valid_to` of either form) -- every value channel is horizon-invariant,
    so `window_delivery_class` classifies it `append`; the sibling of the
    recipe's own `dim_patient_status`, which carries both bounds and so
    classifies `upsert`."""
    return TableDecl(
        name="dim_patient_status_append",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="patient"),
        key=["patient_id", "valid_from"],
        columns=[
            ColumnDecl(name="patient_id", **{"from": "record_id"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(
                name="valid_from_date_key",
                date_ref=DateRefSpec(scd_window="valid_from"),
            ),
        ],
    )


def _status_window_config(
    *, incremental: IncrementalConfig | None = None
) -> ExportConfig:
    """The recipe's own config, plus the `append`-classified sibling dim
    (`_dim_patient_status_append_decl`) beside its `dim_patient_status`."""
    config = _recipe_config()
    assert config.dimensional is not None
    update: dict[str, object] = {
        "dimensional": DimensionalConfig(
            tables=[*config.dimensional.tables, _dim_patient_status_append_decl()]
        )
    }
    if incremental is not None:
        update["incremental"] = incremental
    return config.model_copy(update=update)


def test_windowed_window_bounds_equals_full_export(tmp_path: Path) -> None:
    """A windowed compile's `TableReport.window_bounds` equals the full
    export's, per table -- over the recipe's own `dim_patient_status`."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    config = _status_window_config()

    with open_emit(emit_dir) as emit:
        full_report = export_dimensional(
            emit,
            config,
            tmp_path / "full.duckdb",
            "duckdb",
            anchor,
            discard_notice_sink,
            None,
            (),
        )
        window = parse_range("2024-01-01", "2024-01-10", anchor)
        windowed = export_window(
            emit,
            config,
            tmp_path / "window.duckdb",
            "duckdb",
            anchor,
            window,
            None,
            discard_notice_sink,
            None,
            (),
        )

    full_by_name = {table.name: table for table in full_report.tables}
    for table in windowed.report.tables:
        assert table.window_bounds == full_by_name[table.name].window_bounds
    assert full_by_name["dim_patient_status"].window_bounds == {
        "valid_from_date_key": "valid_from",
        "valid_to_date_key": "valid_to",
    }
    assert full_by_name["dim_patient_status_append"].window_bounds == {
        "valid_from_date_key": "valid_from"
    }


def test_bound_key_dim_upserts_valid_to_append_dim_never_revises(
    tmp_path: Path,
) -> None:
    """Driven through `export_incremental_next` day cadence: the recipe's
    `dim_patient_status` (both bounds) delivers `upsert` -- an earlier
    version's row is revised in place, closing its `valid_to`, so p001 ends
    with exactly one row per version and no stale open `valid_to`; the
    sibling dim declaring only `valid_from` delivers `append` -- the same
    version count, each row inserted once."""
    emit_dir = _sliced_recipe_emit(tmp_path)
    anchor = _anchor_for(emit_dir)
    config = _status_window_config(incremental=IncrementalConfig(period="day"))
    out = tmp_path / "warehouse.duckdb"

    with open_emit(emit_dir) as emit:
        for _ in range(4):
            outcome = export_incremental_next(
                emit, config, out, "duckdb", anchor, discard_notice_sink, None, ()
            )
            assert outcome.status == "emitted"

    conn = duckdb.connect(str(out), read_only=True)
    upsert_valid_to_keys = conn.execute(
        'SELECT valid_to_date_key FROM "dim_patient_status" WHERE patient_id = ?'
        " ORDER BY valid_from_date_key",
        ["p001"],
    ).fetchall()
    (append_row_count,) = conn.execute(
        'SELECT COUNT(*) FROM "dim_patient_status_append" WHERE patient_id = ?',
        ["p001"],
    ).fetchone()
    conn.close()

    assert [key for (key,) in upsert_valid_to_keys] == [20240103, 20240104, None]
    assert append_row_count == 3
