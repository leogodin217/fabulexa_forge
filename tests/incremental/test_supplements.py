"""Tests for incremental delivery of supplementary tables (Phase 3 of the
supplementary-tables sprint): windowed delivery under `--next` and
`--from`/`--to` for both formats, `csv_removed_dirs`, the fingerprint's
`supplements` map and presentation exclusions (plus an end-to-end
`--next` mismatch), and the windowed source-is-output gate.

Uses tmp_path for all IO. Builds minimal emits inline (records__entity +
history, `role: fact` over `history_point`, so a windowed export shows a
genuine per-window delta: one row in windows 0-2, zero in window 3 — the
"empty window" case).
"""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    IncrementalConfig,
    SourceDecl,
    SupplementDecl,
    TableDecl,
)
from fabulexa_forge.errors import (
    IncrementalFingerprintMismatch,
    IncrementalRangeTargetExists,
    SupplementSourceIsOutput,
)
from fabulexa_forge.exporters.companion import companion_artifact_paths
from fabulexa_forge.exporters.supplements import (
    ResolvedSupplement,
    SupplementSource,
    load_supplements,
)
from fabulexa_forge.incremental import fingerprint as fingerprint_module
from fabulexa_forge.incremental.cursor import csv_cursor_path, read_cursor
from fabulexa_forge.incremental.driver import (
    csv_removed_dirs,
    export_incremental_next,
    export_window,
)
from fabulexa_forge.incremental.fingerprint import compute_fingerprint
from fabulexa_forge.incremental.windows import Window, derive_window
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Emit + config builders
# ---------------------------------------------------------------------------

_RECORDS_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    identity_column("record_index", "BIGINT"),
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

_PERIOD_NS = 100
_SLICE_AT = 350  # 4 emitting windows (last empty), drained on the 5th call

_REGION_CODE_CSV = "code,label\nUS,United States\nCA,\n"


def _build_emit(tmp_path: Path) -> Path:
    """Self-contained emit: three entities; history at sim_times 10/110/210.

    A `role: fact` / `history_point` windowed export over this emit shows
    a genuine per-window delta: window 0 sees the t=10 entry, window 1 the
    t=110 entry, window 2 the t=210 entry, window 3 (still within
    `slice_at`) sees none — the "empty window" case.

    Args:
        tmp_path: The test's tmp_path.

    Returns:
        The emit directory.
    """
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _RECORDS_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({col_ddl})')
    for record_index, (entity_id, name, mutation_time) in enumerate(
        [("e001", "Alice", 10), ("e002", "Bob", 110), ("e003", "Carol", 210)]
    ):
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
            [
                "trunk",
                entity_id,
                mutation_time,
                True,
                mutation_time,
                record_index,
                name,
            ],
        )

    hist_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({hist_ddl})')
    for sim_time, val in [(10, "alpha"), (110, "beta"), (210, "gamma")]:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "entity", "e001", "state", sim_time, val],
        )
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__entity",
                "category": "records",
                "columns": _RECORDS_COLUMNS,
                "rows": 3,
                "record_kind": "entity",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 3,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_AT}],
    )
    return emit_dir


def _region_code_decl(file: str = "region_code.csv") -> SupplementDecl:
    return SupplementDecl(
        name="region_code",
        file=file,
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )


def _rate_tier_decl() -> SupplementDecl:
    return SupplementDecl(
        name="rate_tier",
        columns={"tier": "VARCHAR", "capacity": "BIGINT"},
        rows=[{"tier": "standard", "capacity": 10}],
    )


def _fact_config(supplements: list[SupplementDecl] | None = None) -> ExportConfig:
    """A windowed `role: fact` / `history_point` config, `sim_period_ns` cadence."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_history",
                    role="fact",
                    source=SourceDecl(
                        grain="history_point", kind="entity", property="state"
                    ),
                    key=["id"],
                    columns=[
                        ColumnDecl(name="id", **{"from": "record_id"}),
                        ColumnDecl(name="sim_time", **{"from": "sim_time"}),
                        ColumnDecl(name="value", **{"from": "value"}),
                    ],
                )
            ]
        ),
        incremental=IncrementalConfig(sim_period_ns=_PERIOD_NS),
        supplements=supplements,
    )


def _config_dir_with_region_code(
    tmp_path: Path, csv_text: str = _REGION_CODE_CSV
) -> Path:
    """A fresh directory holding `region_code.csv`, distinct from the emit dir."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "region_code.csv").write_text(csv_text, encoding="utf-8")
    return config_dir


def _window0_label() -> str:
    """The label window 0 derives under `_fact_config()`'s cadence."""
    incremental = _fact_config().incremental
    assert incremental is not None
    return derive_window(0, incremental, None).label


_RANGE_WINDOW = Window(index=None, start_ns=0, end_ns=200, label="r_ns0_ns200")


# ---------------------------------------------------------------------------
# csv_removed_dirs: naming, plus on-disk agreement with the write path
# ---------------------------------------------------------------------------


def test_csv_removed_dirs_next_regime() -> None:
    """`--next`: `(<out>/<label>, <out>/.tmp_<label>)`."""
    out = Path("/tmp/probe/drops")
    window = Window(index=2, start_ns=200, end_ns=300, label="w00002_ns200")
    assert csv_removed_dirs(out, window) == (
        out / "w00002_ns200",
        out / ".tmp_w00002_ns200",
    )


def test_csv_removed_dirs_range_regime() -> None:
    """Range: `(<out parent>/.tmp_<label>,)`."""
    out = Path("/tmp/probe/range_out")
    assert csv_removed_dirs(out, _RANGE_WINDOW) == (out.parent / ".tmp_r_ns0_ns200",)


def test_csv_removed_dirs_next_matches_actual_drop_on_disk(tmp_path: Path) -> None:
    """The drop `csv_removed_dirs` names is exactly what `--next` produces;
    the staging half is gone once the window commits."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl()])
    supplements = load_supplements(config, config_dir)
    out = tmp_path / "drops"

    with open_emit(emit_dir) as emit:
        outcome = export_incremental_next(
            emit, config, out, "csv", None, discard_notice_sink, None, supplements
        )

    assert outcome.status == "emitted"
    assert outcome.window is not None
    drop_dir, staging_dir = csv_removed_dirs(out, outcome.window)
    assert drop_dir.is_dir()
    assert (drop_dir / "region_code.csv").exists()
    assert not staging_dir.exists()


def test_csv_removed_dirs_range_matches_actual_drop_on_disk(tmp_path: Path) -> None:
    """The staging dir `csv_removed_dirs` names for a range is gone once the
    range commits; the drop is `out` itself."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl()])
    supplements = load_supplements(config, config_dir)
    out = tmp_path / "range_out"

    with open_emit(emit_dir) as emit:
        export_window(
            emit,
            config,
            out,
            "csv",
            None,
            _RANGE_WINDOW,
            None,
            discard_notice_sink,
            None,
            supplements,
        )

    (staging_dir,) = csv_removed_dirs(out, _RANGE_WINDOW)
    assert out.is_dir()
    assert (out / "region_code.csv").exists()
    assert not staging_dir.exists()


# ---------------------------------------------------------------------------
# DuckDB --next: created, replaced (no duplication), empty window rewritten,
# _export_windows bookkeeping unaffected
# ---------------------------------------------------------------------------


def test_duckdb_next_supplement_replaced_no_duplication_empty_window_included(
    tmp_path: Path,
) -> None:
    """Every emitting window (including the empty one) rewrites the
    supplement to the same two rows; `_export_windows` gains one row per
    call regardless of the supplement."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl(), _rate_tier_decl()])
    supplements = load_supplements(config, config_dir)
    out = tmp_path / "wh.duckdb"

    fact_counts: list[int] = []
    with open_emit(emit_dir) as emit:
        for _ in range(4):
            outcome = export_incremental_next(
                emit,
                config,
                out,
                "duckdb",
                None,
                discard_notice_sink,
                None,
                supplements,
            )
            assert outcome.status == "emitted"
            assert outcome.row_counts is not None
            fact_counts.append(outcome.row_counts["fact_history"])

            conn = duckdb.connect(str(out), read_only=True)
            region_rows = conn.execute(
                'SELECT code, label FROM "region_code" ORDER BY code'
            ).fetchall()
            (window_count,) = conn.execute(
                "SELECT COUNT(*) FROM _export_windows"
            ).fetchone()
            conn.close()

            assert region_rows == [("CA", None), ("US", "United States")]
            assert window_count == len(fact_counts)

    assert fact_counts == [1, 1, 1, 0]


# ---------------------------------------------------------------------------
# CSV --next: whole file every window, byte-identical
# ---------------------------------------------------------------------------


def test_csv_next_supplement_delivered_whole_byte_identical_every_window(
    tmp_path: Path,
) -> None:
    """Every window drop carries `region_code.csv` whole, byte-identical
    across drops."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl(), _rate_tier_decl()])
    supplements = load_supplements(config, config_dir)
    out = tmp_path / "drops"

    region_bytes: list[bytes] = []
    with open_emit(emit_dir) as emit:
        for _ in range(4):
            outcome = export_incremental_next(
                emit, config, out, "csv", None, discard_notice_sink, None, supplements
            )
            assert outcome.status == "emitted"
            assert outcome.window is not None
            drop = out / outcome.window.label
            assert (drop / "rate_tier.csv").exists()
            region_bytes.append((drop / "region_code.csv").read_bytes())

    assert len(region_bytes) == 4
    assert len(set(region_bytes)) == 1


# ---------------------------------------------------------------------------
# Range (both formats): supplement delivered whole; TableReport.supplement
# populated; row_counts[<supplement>] is the data-row count
# ---------------------------------------------------------------------------


def test_range_csv_supplement_delivered_whole_and_report_stamped(
    tmp_path: Path,
) -> None:
    """A range CSV export carries the supplement whole; its `TableReport`
    is stamped and `row_counts` holds the real row count."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl(), _rate_tier_decl()])
    supplements = load_supplements(config, config_dir)
    out = tmp_path / "range_csv"

    with open_emit(emit_dir) as emit:
        windowed = export_window(
            emit,
            config,
            out,
            "csv",
            None,
            _RANGE_WINDOW,
            None,
            discard_notice_sink,
            None,
            supplements,
        )

    assert (out / "region_code.csv").exists()
    assert (out / "rate_tier.csv").exists()
    region_report = next(t for t in windowed.report.tables if t.name == "region_code")
    expected_sha = hashlib.sha256(_REGION_CODE_CSV.encode("utf-8")).hexdigest()
    assert region_report.supplement == SupplementSource(
        file="region_code.csv", sha256=expected_sha
    )
    assert windowed.row_counts["region_code"] == 2


def test_range_duckdb_supplement_delivered_whole(tmp_path: Path) -> None:
    """A range DuckDB export carries the supplement whole."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl(), _rate_tier_decl()])
    supplements = load_supplements(config, config_dir)
    out = tmp_path / "range.duckdb"

    with open_emit(emit_dir) as emit:
        windowed = export_window(
            emit,
            config,
            out,
            "duckdb",
            None,
            _RANGE_WINDOW,
            None,
            discard_notice_sink,
            None,
            supplements,
        )

    conn = duckdb.connect(str(out), read_only=True)
    rows = conn.execute(
        'SELECT code, label FROM "region_code" ORDER BY code'
    ).fetchall()
    conn.close()
    assert rows == [("CA", None), ("US", "United States")]
    assert windowed.row_counts["region_code"] == 2


# ---------------------------------------------------------------------------
# Fingerprint: the supplements map, presentation exclusions, digest
# sensitivity
# ---------------------------------------------------------------------------

_MINIMAL_DIM_TABLE: dict[str, object] = {
    "name": "dim_x",
    "role": "dim",
    "scd": "type1",
    "source": {"grain": "records", "kind": "actor"},
    "key": ["id"],
    "columns": [{"name": "id", "from": "record_id"}],
}


def _minimal_config(*supplement_decls: SupplementDecl) -> ExportConfig:
    """A single-table dimensional config, optionally carrying supplements."""
    return ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": {"tables": [_MINIMAL_DIM_TABLE]},
            "supplements": list(supplement_decls) or None,
        }
    )


def _fp(config: ExportConfig, supplements: "Sequence[ResolvedSupplement]" = ()) -> str:
    return compute_fingerprint(
        config=config,
        anchor=None,
        sidecar_sha256="a" * 64,
        fork_path="root",
        fmt="duckdb",
        package_version="1.0.0",
        supplements=supplements,
    )


def _canonical_document(**kwargs: object) -> str:
    """Call `compute_fingerprint`, capturing the canonical JSON text it hashes.

    Patches only the `json` name inside `incremental.fingerprint` (a
    `SimpleNamespace` exposing `dumps`), so no other module's `json` is
    touched.
    """
    captured: dict[str, str] = {}
    real_dumps = json.dumps

    def _capture_dumps(obj: object, **json_kwargs: object) -> str:
        text = real_dumps(obj, **json_kwargs)
        captured["text"] = text
        return text

    original_json = fingerprint_module.json
    fingerprint_module.json = types.SimpleNamespace(dumps=_capture_dumps)  # type: ignore[assignment]
    try:
        compute_fingerprint(**kwargs)  # type: ignore[arg-type]
    finally:
        fingerprint_module.json = original_json
    return captured["text"]


def _resolved_region_code(
    config_dir: Path, csv_text: str = _REGION_CODE_CSV
) -> ResolvedSupplement:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "region_code.csv").write_text(csv_text, encoding="utf-8")
    config = _minimal_config(_region_code_decl())
    (resolved,) = load_supplements(config, config_dir)
    return resolved


def test_fingerprint_document_contains_empty_supplements_map() -> None:
    """`supplements=()` -> the canonical document carries `"supplements":{}`,
    the sole addition to the pre-sprint document's key set."""
    text = _canonical_document(
        config=_minimal_config(),
        anchor=None,
        sidecar_sha256="a" * 64,
        fork_path="root",
        fmt="duckdb",
        package_version="1.0.0",
        supplements=(),
    )
    assert '"supplements":{}' in text
    document = json.loads(text)
    assert set(document) == {
        "anchor",
        "config",
        "fmt",
        "fork_path",
        "package_version",
        "sidecar_sha256",
        "supplements",
    }


def test_fingerprint_supplements_map_carries_columns_and_sha256(tmp_path: Path) -> None:
    """One declared supplement's map entry: ordered (name, canonical type)
    column pairs, plus its file's sha256."""
    resolved = _resolved_region_code(tmp_path / "cfg")
    config = _minimal_config(_region_code_decl())
    text = _canonical_document(
        config=config,
        anchor=None,
        sidecar_sha256="a" * 64,
        fork_path="root",
        fmt="duckdb",
        package_version="1.0.0",
        supplements=(resolved,),
    )
    document = json.loads(text)
    assert document["supplements"]["region_code"] == {
        "columns": [["code", "VARCHAR"], ["label", "VARCHAR"]],
        "sha256": resolved.sha256,
    }


def test_fingerprint_unaffected_by_supplement_description(tmp_path: Path) -> None:
    """A supplement's `description` -> unaffected fingerprint."""
    resolved = _resolved_region_code(tmp_path / "cfg")
    config_a = _minimal_config(_region_code_decl())
    config_b = _minimal_config(
        SupplementDecl(
            name="region_code",
            file="region_code.csv",
            columns={"code": "VARCHAR", "label": "VARCHAR"},
            description="Sales-region codes.",
        )
    )
    assert _fp(config_a, (resolved,)) == _fp(config_b, (resolved,))


def test_fingerprint_unaffected_by_supplement_descriptions_map(tmp_path: Path) -> None:
    """A supplement's `descriptions` map -> unaffected fingerprint."""
    resolved = _resolved_region_code(tmp_path / "cfg")
    config_a = _minimal_config(_region_code_decl())
    config_b = _minimal_config(
        SupplementDecl(
            name="region_code",
            file="region_code.csv",
            columns={"code": "VARCHAR", "label": "VARCHAR"},
            descriptions={"code": "ISO region code."},
        )
    )
    assert _fp(config_a, (resolved,)) == _fp(config_b, (resolved,))


def test_fingerprint_unaffected_by_supplement_file_renamed_same_bytes(
    tmp_path: Path,
) -> None:
    """Renaming a file supplement's source, same bytes -> unaffected
    fingerprint (`file` is excluded; the map's identity is `sha256`)."""
    original = _resolved_region_code(tmp_path / "cfg_a")
    renamed_dir = tmp_path / "cfg_b"
    renamed_dir.mkdir()
    (renamed_dir / "renamed.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    renamed_config = _minimal_config(_region_code_decl(file="renamed.csv"))
    (renamed,) = load_supplements(renamed_config, renamed_dir)
    assert renamed.sha256 == original.sha256
    assert renamed.decl.file != original.decl.file

    original_config = _minimal_config(_region_code_decl())
    assert _fp(original_config, (original,)) == _fp(renamed_config, (renamed,))


def test_fingerprint_changes_on_supplement_csv_byte(tmp_path: Path) -> None:
    """A single changed byte in a file supplement's CSV -> different digest."""
    original = _resolved_region_code(tmp_path / "cfg_a")
    edited = _resolved_region_code(
        tmp_path / "cfg_b", "code,label\nUS,United States!\nCA,\n"
    )
    config = _minimal_config(_region_code_decl())
    assert _fp(config, (original,)) != _fp(config, (edited,))


def test_fingerprint_changes_on_inline_cell(tmp_path: Path) -> None:
    """A changed inline-row cell -> different digest (via the embedded
    config dump, whose `rows` are not excluded)."""
    config_a = _minimal_config(_rate_tier_decl())
    config_b = _minimal_config(
        SupplementDecl(
            name="rate_tier",
            columns={"tier": "VARCHAR", "capacity": "BIGINT"},
            rows=[{"tier": "standard", "capacity": 99}],
        )
    )
    supplements_a = load_supplements(config_a, tmp_path)
    supplements_b = load_supplements(config_b, tmp_path)
    assert _fp(config_a, supplements_a) != _fp(config_b, supplements_b)


def test_fingerprint_changes_on_supplement_column_name(tmp_path: Path) -> None:
    """A renamed column -> different digest."""
    config_a = _minimal_config(
        SupplementDecl(
            name="rate_tier", columns={"tier": "VARCHAR"}, rows=[{"tier": "standard"}]
        )
    )
    config_b = _minimal_config(
        SupplementDecl(
            name="rate_tier", columns={"level": "VARCHAR"}, rows=[{"level": "standard"}]
        )
    )
    supplements_a = load_supplements(config_a, tmp_path)
    supplements_b = load_supplements(config_b, tmp_path)
    assert _fp(config_a, supplements_a) != _fp(config_b, supplements_b)


def test_fingerprint_changes_on_supplement_column_order(tmp_path: Path) -> None:
    """A reordered `columns` map -> different digest (the map's ordered
    pair list, not the canonical document's sorted keys, carries order)."""
    config_a = _minimal_config(
        SupplementDecl(
            name="rate_tier",
            columns={"tier": "VARCHAR", "capacity": "BIGINT"},
            rows=[{"tier": "standard", "capacity": 10}],
        )
    )
    config_b = _minimal_config(
        SupplementDecl(
            name="rate_tier",
            columns={"capacity": "BIGINT", "tier": "VARCHAR"},
            rows=[{"tier": "standard", "capacity": 10}],
        )
    )
    supplements_a = load_supplements(config_a, tmp_path)
    supplements_b = load_supplements(config_b, tmp_path)
    assert _fp(config_a, supplements_a) != _fp(config_b, supplements_b)


def test_fingerprint_changes_on_supplement_type_spelling(tmp_path: Path) -> None:
    """A different declared type (BIGINT vs INTEGER) -> different digest."""
    config_a = _minimal_config(
        SupplementDecl(
            name="rate_tier", columns={"capacity": "BIGINT"}, rows=[{"capacity": 10}]
        )
    )
    config_b = _minimal_config(
        SupplementDecl(
            name="rate_tier", columns={"capacity": "INTEGER"}, rows=[{"capacity": 10}]
        )
    )
    supplements_a = load_supplements(config_a, tmp_path)
    supplements_b = load_supplements(config_b, tmp_path)
    assert _fp(config_a, supplements_a) != _fp(config_b, supplements_b)


# ---------------------------------------------------------------------------
# End-to-end mismatch: a --next re-run after editing the supplement's file
# vs. after editing only its description
# ---------------------------------------------------------------------------


def test_end_to_end_mismatch_after_editing_supplement_csv(tmp_path: Path) -> None:
    """A second `--next` after the supplement's CSV bytes change ->
    IncrementalFingerprintMismatch."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl()])
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        supplements = load_supplements(config, config_dir)
        first = export_incremental_next(
            emit, config, out, "duckdb", None, discard_notice_sink, None, supplements
        )
        assert first.status == "emitted"

        (config_dir / "region_code.csv").write_text(
            "code,label\nUS,United States!\nCA,\n", encoding="utf-8"
        )
        edited_supplements = load_supplements(config, config_dir)

        with pytest.raises(IncrementalFingerprintMismatch):
            export_incremental_next(
                emit,
                config,
                out,
                "duckdb",
                None,
                discard_notice_sink,
                None,
                edited_supplements,
            )


def test_end_to_end_description_only_edit_still_emits(tmp_path: Path) -> None:
    """A second `--next` after editing only the supplement's `description`
    -> emits (description is fingerprint-excluded)."""
    emit_dir = _build_emit(tmp_path)
    config_dir = _config_dir_with_region_code(tmp_path)
    config = _fact_config([_region_code_decl()])
    out = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        supplements = load_supplements(config, config_dir)
        first = export_incremental_next(
            emit, config, out, "duckdb", None, discard_notice_sink, None, supplements
        )
        assert first.status == "emitted"

        described_config = _fact_config(
            [
                SupplementDecl(
                    name="region_code",
                    file="region_code.csv",
                    columns={"code": "VARCHAR", "label": "VARCHAR"},
                    description="Sales-region codes.",
                )
            ]
        )
        described_supplements = load_supplements(described_config, config_dir)

        second = export_incremental_next(
            emit,
            described_config,
            out,
            "duckdb",
            None,
            discard_notice_sink,
            None,
            described_supplements,
        )
        assert second.status == "emitted"
        assert second.window is not None
        assert second.window.index == 1


# ---------------------------------------------------------------------------
# Windowed source-is-output gate
# ---------------------------------------------------------------------------


def test_source_is_output_next_csv_file_in_window_drop(tmp_path: Path) -> None:
    """`--next` CSV: a file at `<out>/<label>/<name>.csv` is refused before
    any write; the cursor stays absent (fresh)."""
    emit_dir = _build_emit(tmp_path)
    out = tmp_path / "drops"
    label = _window0_label()
    source_dir = out / label
    source_dir.mkdir(parents=True)
    (source_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(source_dir / "region_code.csv"),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    config = _fact_config([decl])
    supplements = load_supplements(config, tmp_path)

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_incremental_next(
                emit, config, out, "csv", None, discard_notice_sink, None, supplements
            )

    assert read_cursor(out, "csv", label) is None
    assert not (source_dir / "fact_history.csv").exists()


def test_source_is_output_next_csv_file_in_staging_dir(tmp_path: Path) -> None:
    """`--next` CSV: a file at `<out>/.tmp_<label>/x.csv` is refused; the
    cursor stays absent."""
    emit_dir = _build_emit(tmp_path)
    out = tmp_path / "drops"
    label = _window0_label()
    staging_dir = out / f".tmp_{label}"
    staging_dir.mkdir(parents=True)
    (staging_dir / "x.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(staging_dir / "x.csv"),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    config = _fact_config([decl])
    supplements = load_supplements(config, tmp_path)

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_incremental_next(
                emit, config, out, "csv", None, discard_notice_sink, None, supplements
            )

    assert read_cursor(out, "csv", label) is None
    assert not (out / label).exists()


def test_source_is_output_next_csv_file_at_manifest_path(tmp_path: Path) -> None:
    """`--next` CSV: a file at `<out>/<mode>-manifest.json` is refused; the
    manifest bytes stay untouched."""
    emit_dir = _build_emit(tmp_path)
    out = tmp_path / "drops"
    out.mkdir(parents=True)
    _readme_path, manifest_path = companion_artifact_paths(out, "dimensional", "csv")
    manifest_path.write_text(_REGION_CODE_CSV, encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(manifest_path),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    config = _fact_config([decl])
    supplements = load_supplements(config, tmp_path)
    label = _window0_label()

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_incremental_next(
                emit, config, out, "csv", None, discard_notice_sink, None, supplements
            )

    assert read_cursor(out, "csv", label) is None
    assert manifest_path.read_text(encoding="utf-8") == _REGION_CODE_CSV
    assert not (out / label).exists()


def test_source_is_output_next_csv_file_at_cursor_path(tmp_path: Path) -> None:
    """`--next` CSV: a file at `csv_cursor_path(out)` is refused; the file's
    bytes stay untouched and no drop is created."""
    emit_dir = _build_emit(tmp_path)
    out = tmp_path / "drops"
    out.mkdir(parents=True)
    cursor_path = csv_cursor_path(out)
    cursor_path.write_text(_REGION_CODE_CSV, encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(cursor_path),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    config = _fact_config([decl])
    supplements = load_supplements(config, tmp_path)
    label = _window0_label()

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_incremental_next(
                emit, config, out, "csv", None, discard_notice_sink, None, supplements
            )

    assert cursor_path.read_text(encoding="utf-8") == _REGION_CODE_CSV
    assert not (out / label).exists()


def test_source_is_output_range_csv_file_in_staging_dir(tmp_path: Path) -> None:
    """A range CSV: a file at `<out parent>/.tmp_<label>/x.csv` is refused
    before any write; `out` is never created."""
    emit_dir = _build_emit(tmp_path)
    out = tmp_path / "range_out"
    staging_dir = out.parent / f".tmp_{_RANGE_WINDOW.label}"
    staging_dir.mkdir(parents=True)
    (staging_dir / "x.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(staging_dir / "x.csv"),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    config = _fact_config([decl])
    supplements = load_supplements(config, tmp_path)

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_window(
                emit,
                config,
                out,
                "csv",
                None,
                _RANGE_WINDOW,
                None,
                discard_notice_sink,
                None,
                supplements,
            )

    assert not out.exists()


def test_source_is_output_range_csv_file_at_own_output_path_refused(
    tmp_path: Path,
) -> None:
    """A range CSV whose supplement source sits at its own `<out>/<name>.csv`
    output path: `out` must already hold it, so the pre-existing-target
    gate at the top of `export_window` fires first (range never appends
    into or overwrites an existing target) — a total refusal before any
    write, same as the source-is-output cases."""
    emit_dir = _build_emit(tmp_path)
    out = tmp_path / "range_out"
    out.mkdir(parents=True)
    (out / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(out / "region_code.csv"),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    config = _fact_config([decl])
    supplements = load_supplements(config, tmp_path)

    with open_emit(emit_dir) as emit:
        with pytest.raises(IncrementalRangeTargetExists):
            export_window(
                emit,
                config,
                out,
                "csv",
                None,
                _RANGE_WINDOW,
                None,
                discard_notice_sink,
                None,
                supplements,
            )

    assert not (out / "fact_history.csv").exists()
    assert (out / "region_code.csv").read_text(encoding="utf-8") == _REGION_CODE_CSV


def test_source_is_output_duckdb_file_at_out_path(tmp_path: Path) -> None:
    """DuckDB: a file supplement whose source sits at the `out` path itself
    is refused before any write; `out`'s bytes stay untouched."""
    emit_dir = _build_emit(tmp_path)
    out = tmp_path / "wh.duckdb"
    out.write_text(_REGION_CODE_CSV, encoding="utf-8")

    decl = SupplementDecl(
        name="region_code",
        file=str(out),
        columns={"code": "VARCHAR", "label": "VARCHAR"},
    )
    config = _fact_config([decl])
    supplements = load_supplements(config, tmp_path)
    assert config.incremental is not None
    window = derive_window(0, config.incremental, None)

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_window(
                emit,
                config,
                out,
                "duckdb",
                None,
                window,
                "f" * 64,
                discard_notice_sink,
                None,
                supplements,
            )

    assert out.read_text(encoding="utf-8") == _REGION_CODE_CSV
