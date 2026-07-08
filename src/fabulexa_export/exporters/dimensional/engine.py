"""Dimensional export engine: QuerySpec, build_query_specs, export_dimensional.

Compiles each table declaration into a deterministic QuerySpec by:
1. Enforcing the SingleBranch guard.
2. Validating each table against business rules.
3. Building the grain SQL for each table.

Supports: records (type1 + type2), history_point, history_interval, membership
grains; from, correlation, derived (ordinal/value_map/timestamp/scd_window),
null, fk column modes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_export.anchor import EffectiveAnchor
    from fabulexa_export.config.models import ExportConfig
    from fabulexa_export.incremental.windows import Window
    from fabulexa_export.reader.emit import Emit

from fabulexa_export.config.models import DimensionalConfig
from fabulexa_export.derivations import require_single_branch
from fabulexa_export.exporters.dimensional.grains import build_grain_sql
from fabulexa_export.exporters.dimensional.validation import validate_table
from fabulexa_export.exporters.query_spec import QuerySpec, write_query_specs

__all__ = ["QuerySpec", "build_query_specs", "export_dimensional"]


def build_query_specs(
    emit: "Emit",
    config: DimensionalConfig,
    anchor: "EffectiveAnchor | None",
    window: "Window | None",
) -> list[QuerySpec]:
    """Compile table declarations; optionally windowed.

    window=None is the existing full-export contract, unchanged (no
    parameter default — full-export call sites pass None explicitly). With a
    window: the per-class membership predicate (design doc § Window
    membership: records grain on last_mutation_sim_time, history_point on
    sim_time, half-open on raw ns) is applied as the outermost WHERE over
    the full-export SELECT (after window functions and derived columns, so
    every emitted value equals its full-export value); SCD-2 dims with a
    valid_to column compile to a '<name>__rows' spec without the valid_to
    slots, with the trailing __valid_from_ns ordering key, plus the
    companion view; type-1 dims compile to their full snapshot (no
    predicate); write modes are tagged append (facts, SCD-2 rows) / replace
    (type-1 dims). Windowed business rules run before any SQL is emitted.

    Args:
        emit: The open emit (trunk-only; sole branch).
        config: The validated dimensional config.
        anchor: The resolved EffectiveAnchor, or None for raw sim_time.
        window: The window to filter to, or None for the full export.

    Returns:
        One QuerySpec per declared table, in declaration order.

    Raises:
        ExportError: The existing rules; plus, when window is not None:
            IncrementalGrainUnsupported, IncrementalElapsedUnsupported,
            IncrementalFkMembershipUnsupported, IncrementalFkMutableHop,
            IncrementalOrdinalOrderBy, IncrementalSliceColumnMutable,
            IncrementalFilterColumnMutable, IncrementalScd2IdentityKey,
            IncrementalScd2ValidFromUnique, IncrementalReservedName.
    """
    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)

    specs: list[QuerySpec] = []

    for table_decl in config.tables:
        source_table_name = validate_table(table_decl, config, sidecar, window)
        sql, write_mode, view_name, view_sql = build_grain_sql(
            table_decl, source_table_name, sidecar, anchor, fork_path, config, window
        )
        # For SCD-2 dims with valid_to under a window, view_name = author name
        # and the physical table is <name>__rows.
        if view_name is not None:
            phys_table_name = f"{table_decl.name}__rows"
        else:
            phys_table_name = table_decl.name
        specs.append(
            QuerySpec(
                table_name=phys_table_name,
                sql=sql,
                write_mode=write_mode,
                view_name=view_name,
                view_sql=view_sql,
            )
        )

    return specs


def export_dimensional(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
) -> dict[str, int]:
    """Run the dimensional exporter and write the star schema.

    Builds the QuerySpecs, then dispatches by `fmt` to the matching writer,
    handing it the open `emit` (the writer materializes each spec through
    `Emit.query_arrow`).

    Args:
        emit: The open emit.
        config: The validated export config (mode='dimensional').
        out: The output target — interpreted by `fmt`: a **directory** that
            receives one `<table>.csv` per declared table (`fmt='csv'`), or the
            **`.duckdb` file path** to create (`fmt='duckdb'`). The two writers'
            output shapes (one file per table vs. one file holding every table)
            are the reason `out` is a directory for CSV and a file for DuckDB.
        fmt: Output format. The CLI constrains the raw `--fmt` string to
            `{'csv','duckdb'}` before this is reached (see `cmd_export`).
        anchor: The resolved EffectiveAnchor, or None for raw sim_time integers.

    Returns:
        Mapping of **every** declared table name -> row count written (`0` for a
        table whose grain resolved to no rows; such a table is still emitted —
        empty typed DuckDB table or header-only CSV — never dropped). Both
        writers obey this rule identically.

    Raises:
        ExportError: Branch guard or a business rule fails.
        ExportRuntimeError: A writer fails.
    """
    assert config.dimensional is not None
    specs = build_query_specs(emit, config.dimensional, anchor, None)
    return write_query_specs(emit, specs, out, fmt)
