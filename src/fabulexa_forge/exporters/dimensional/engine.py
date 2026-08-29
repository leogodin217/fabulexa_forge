"""Dimensional export engine: QuerySpec, build_query_specs, export_dimensional.

Compiles each table declaration into a deterministic QuerySpec by:
1. Enforcing the SingleBranch guard.
2. Validating each table against business rules.
3. Building the grain SQL for each table.

Supports: records (type1 + type2), history_point, history_interval, membership
grains; from, correlation, derived (ordinal/value_map/timestamp/scd_window),
null, fk column modes.

Resolves the election once (`exporters.election.resolve_election`) and
threads it to `validate_table` and `build_grain_sql`. Immediately after
composing each table's SQL, guards every FK column whose resolved surface is
non-`record_id` (§ `_guard_fk_columns`), recomputing the same resolved
surface + destination population set `validate_table` already gated
(`resolve_dim_source_populations` / `resolve_fk_surface` are pure, so the two
computations cannot disagree); after every table is compiled, guards the
dim-side leg — each dim whose declared key projects a surface some inbound
edge resolved (§ `_guard_dim_side_legs`) — before any writer runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig, TableDecl
    from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
    from fabulexa_forge.exporters.dimensional.populations import DimSourcePopulations
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import ExportReport
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.config.models import DimensionalConfig
from fabulexa_forge.derivations import require_single_branch
from fabulexa_forge.exporters.base_relations import apply_base_relations
from fabulexa_forge.exporters.companion import (
    validate_overlay_tables,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.dimensional.fk import check_fk_target_is_dim
from fabulexa_forge.exporters.dimensional.grains import build_grain_sql
from fabulexa_forge.exporters.dimensional.populations import (
    dim_identity_relation_at_end_sql,
    dim_key_projects_surface,
    dim_population_sub_types,
    resolve_dim_source_populations,
    resolve_fk_surface,
)
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.exporters.election import (
    build_population_spine_sql,
    check_elected_key_unique,
    resolve_election,
)
from fabulexa_forge.exporters.query_spec import QuerySpec, write_query_specs

__all__ = ["QuerySpec", "build_query_specs", "export_dimensional"]

#: The two non-record_id surfaces the guard covers, in a fixed order so a
#: mixed dim-side leg's guard calls are deterministic across runs.
_GUARD_SURFACES: tuple[Literal["record_index", "presentation_id"], ...] = (
    "record_index",
    "presentation_id",
)


def _guard_context_label(base_label: str, window: "Window | None") -> str:
    """Suffix a guard's context label with the window display label, if any.

    Args:
        base_label: The table/edge identity (e.g. `"fact_ride.driver_id"`).
        window: The active window, or None for a full/sliced export.

    Returns:
        `base_label`, suffixed `" (<window.label>)"` under an incremental
        invocation.
    """
    return base_label if window is None else f"{base_label} ({window.label})"


def _guard_fk_relation(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    dim_populations: "DimSourcePopulations",
    resolved_surface: "Literal['record_index', 'presentation_id']",
    context_label: str,
) -> None:
    """Guard one composed identity relation over a destination population set.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        dim_populations: The destination dim's source population set.
        resolved_surface: The non-`record_id` surface to guard.
        context_label: The guard's error-message identity.

    Raises:
        ElectedKeyDuplicate: The elected surface is not a bijection on
            record_id over the consumed population set.
    """
    relation_sql = dim_identity_relation_at_end_sql(
        sidecar, fork_path, dim_populations.kind, resolved_surface
    )
    spine_sql = (
        build_population_spine_sql(
            sidecar,
            fork_path,
            dim_populations.kind,
            dim_population_sub_types(dim_populations),
        )
        if dim_populations.proper_subset
        else None
    )
    check_elected_key_unique(
        emit, relation_sql, resolved_surface, spine_sql, context_label
    )


def _guard_fk_columns(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    table_decl: "TableDecl",
    config: DimensionalConfig,
    election: "Election",
    window: "Window | None",
    dim_decls: "dict[str, TableDecl]",
    dim_surfaces: "dict[str, set[Literal['record_index', 'presentation_id']]]",
) -> None:
    """Guard every non-`record_id` fk column's composed relation on one table.

    Recomputes the same `(resolved_surface, dim_populations)` pair
    `validate_table` already gated for this table (`resolve_dim_source_populations`
    / `resolve_fk_surface` are pure, so the two computations agree by
    construction — sprint contracts § 5's recompute-not-thread posture).
    Records, for each destination dim whose declared key also projects the
    resolved surface, an entry into `dim_decls` / `dim_surfaces` for the
    dim-side leg (§ `_guard_dim_side_legs`) to guard once after every table
    compiles.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        table_decl: The output table declaration whose fk columns to guard.
        config: The dimensional config.
        election: The resolved election.
        window: The active window, or None for a full/sliced export.
        dim_decls: Accumulator: dim table name -> its TableDecl.
        dim_surfaces: Accumulator: dim table name -> the surfaces ≥ 1 inbound
            edge resolved that the dim's declared key also projects.

    Raises:
        ElectedKeyDuplicate: A guarded fk relation is not a bijection on
            record_id over its destination population set.
    """
    for col_decl in table_decl.columns:
        if col_decl.fk is None:
            continue
        target_table_decl = check_fk_target_is_dim(col_decl, table_decl, config)
        target_kind = target_table_decl.source.kind
        edge_name = f"{table_decl.name}.{col_decl.name}"
        dim_populations = resolve_dim_source_populations(
            sidecar, target_kind, target_table_decl.source.filter
        )
        resolved_surface = resolve_fk_surface(
            election, dim_populations, col_decl.fk.target_key, edge_name
        )
        if resolved_surface == "record_id":
            continue
        _guard_fk_relation(
            emit,
            sidecar,
            fork_path,
            dim_populations,
            resolved_surface,
            _guard_context_label(edge_name, window),
        )
        if dim_key_projects_surface(target_table_decl, resolved_surface):
            dim_decls[target_table_decl.name] = target_table_decl
            dim_surfaces.setdefault(target_table_decl.name, set()).add(resolved_surface)


def _guard_dim_side_legs(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    dim_decls: "dict[str, TableDecl]",
    dim_surfaces: "dict[str, set[Literal['record_index', 'presentation_id']]]",
    window: "Window | None",
) -> None:
    """Guard the dim-side leg: each dim whose key an inbound edge's surface projects.

    Doc § The elected-key uniqueness guard, dimensional (b): for each dim
    that is the destination of ≥ 1 edge whose resolved non-`record_id`
    surface the dim's declared `key` also projects, guards that surface's
    relation for the dim's own source kind — the join's other side, over the
    dim's own population set.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        dim_decls: dim table name -> its TableDecl, from `_guard_fk_columns`.
        dim_surfaces: dim table name -> the surfaces to guard.
        window: The active window, or None for a full/sliced export.

    Raises:
        ElectedKeyDuplicate: A guarded dim relation is not a bijection on
            record_id over the dim's own source population set.
    """
    for dim_name, surfaces in dim_surfaces.items():
        dim_table_decl = dim_decls[dim_name]
        dim_populations = resolve_dim_source_populations(
            sidecar, dim_table_decl.source.kind, dim_table_decl.source.filter
        )
        label = _guard_context_label(f"{dim_name} (dim-side leg)", window)
        for surface in _GUARD_SURFACES:
            if surface not in surfaces:
                continue
            _guard_fk_relation(
                emit, sidecar, fork_path, dim_populations, surface, label
            )


def _table_author_descriptions(table_decl: "TableDecl") -> "Mapping[str, str]":
    """One table's `author_descriptions`, keyed by the entry's own output name.

    Every column entry mode (`from`, `derived`, `null`, `fk`, `correlation`,
    `lookup`) may carry a `description` — the entry's own `name` is already
    the output name, so no rename translation applies (dimensional has no
    `descriptions`-key gate).

    Args:
        table_decl: The table declaration.

    Returns:
        Output column name -> author-supplied prose, `columns` declaration
        order; empty when no entry carries a `description`.
    """
    return {
        col_decl.name: col_decl.description
        for col_decl in table_decl.columns
        if col_decl.description is not None
    }


def build_query_specs(
    emit: "Emit",
    config: DimensionalConfig,
    anchor: "EffectiveAnchor | None",
    window: "Window | None",
    notice_sink: "NoticeSink",
    base_relations: "Mapping[str, str] | None",
    *,
    election: "Election | None" = None,
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

    New behavior: SliceOnlyColumnRefused runs always-on over every
    config-referenced source-column resolution; LookupColumnSafety keys on
    temporal_class: constant (exempt discriminator excepted, any class);
    DiscriminatorValueObserved emits a 'discriminator-value-unobserved'
    Notice through notice_sink instead of warnings.warn. Resolves the
    election once (`resolve_election(sidecar, config.keys)` by
    `ExportConfig.keys`-holding callers, or the all-default election when
    `election=None`), threads it to `validate_table` and `build_grain_sql`
    for every table, then guards every non-`record_id` fk relation
    (§ `_guard_fk_columns`) and the dim-side leg (§ `_guard_dim_side_legs`)
    before returning.

    Args:
        emit: The open emit (trunk-only; sole branch).
        config: The validated dimensional config.
        anchor: The resolved EffectiveAnchor, or None for raw sim_time.
        window: The window to filter to, or None for the full export.
        notice_sink: Receiver for plan notices.
        base_relations: Physical base-table name -> replacing relation (a complete
            SELECT). When given, every base-table read in the compiled plan
            resolves through the mapping via one name-shadowing CTE per mapped
            name wrapped around each compiled query (never a textual prefix — a
            compiled query may already open with its own WITH); unmapped names
            fall back to the physical table. None compiles byte-identically to
            the pre-parameter surface; the full-export and windowed callers pass
            None explicitly.
        election: The resolved election, or None to resolve the all-default
            election internally (every population elects record_id — the
            caller has no `keys` block to thread, or is an election-free
            internal/test caller). Callers that hold `ExportConfig.keys`
            (`export_dimensional`, the incremental driver, tier-2 shaped
            playback) resolve and pass it.

    Returns:
        One QuerySpec per declared table, in declaration order. Each spec's
        `provenance` is `build_grain_sql`'s fifth element, stamped
        verbatim; `kind_values` stays empty — dimensional has no
        kind-name-as-value output column. `author_descriptions` is stamped
        from the table's column entries (§ `_table_author_descriptions`),
        keyed by each entry's own output name.

    Raises:
        ExportError: The existing rules; plus, when window is not None:
            IncrementalGrainUnsupported, IncrementalElapsedUnsupported,
            IncrementalFkMembershipUnsupported, IncrementalFkMutableHop,
            IncrementalOrdinalOrderBy, IncrementalSliceColumnMutable,
            IncrementalFilterColumnMutable, IncrementalScd2IdentityKey,
            IncrementalScd2ValidFromUnique, IncrementalReservedName.
        ElectedKeyDuplicate: A corrupted elected key fails the uniqueness
            guard on some fk relation or the dim-side leg.
        ElectionInheritanceAmbiguous: An fk column's `target_key` is absent
            and its destination dim's source population set carries more
            than one distinct election.
        ElectionUnionUnsafe: An fk column's admitted target populations'
            resolved key spaces contain a pairwise-unsafe pair.
        ElectionPresentationUndeclared: An fk column resolves
            presentation_id over a source population set with an uncovered
            population.
        ElectionDimKeyDisagrees: An fk column inherits a non-default
            surface the destination dim's declared key does not project.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            absent or out of enum (non-conformant emit).
    """
    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)
    resolved_election = (
        election if election is not None else resolve_election(sidecar, None)
    )

    specs: list[QuerySpec] = []
    dim_decls: "dict[str, TableDecl]" = {}
    dim_surfaces: "dict[str, set[Literal['record_index', 'presentation_id']]]" = {}

    for table_decl in config.tables:
        source_table_name = validate_table(
            table_decl,
            config,
            sidecar,
            window,
            notice_sink,
            anchor=anchor,
            election=resolved_election,
        )
        sql, write_mode, view_name, view_sql, provenance = build_grain_sql(
            table_decl,
            source_table_name,
            sidecar,
            anchor,
            fork_path,
            config,
            window,
            election=resolved_election,
        )
        sql = apply_base_relations(sql, base_relations)
        _guard_fk_columns(
            emit,
            sidecar,
            fork_path,
            table_decl,
            config,
            resolved_election,
            window,
            dim_decls,
            dim_surfaces,
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
                provenance=provenance,
                author_descriptions=_table_author_descriptions(table_decl),
            )
        )

    _guard_dim_side_legs(emit, sidecar, fork_path, dim_decls, dim_surfaces, window)

    return specs


def export_dimensional(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    overlay: "ReadmeOverlay | None",
) -> "ExportReport":
    """Run the dimensional exporter and write the star schema.

    Resolves the election from `config.keys` and builds the QuerySpecs
    (threading notice_sink and the resolved election to the compile).
    Immediately after compiling — before any write — validates `overlay`'s
    `table:` slots against the compiled plan's output tables when `overlay`
    is present. Dispatches by `fmt` to the matching writer, handing it the
    open `emit` (the writer materializes each spec through
    `Emit.query_arrow`), then writes the companion README + manifest and
    returns the report.

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
        notice_sink: Receiver for plan notices.
        overlay: The parsed README overlay, or None.

    Returns:
        The invocation's `ExportReport`: one `TableReport` per declared table,
        in declaration order (`0` row count for a table whose grain resolved
        to no rows; such a table is still emitted — empty typed DuckDB table
        or header-only CSV — never dropped). Both writers obey this rule
        identically.

    Raises:
        ExportError: Branch guard or a business rule fails.
        ReadmeOverlayUnknownTable: `overlay` names a table the compiled plan
            does not produce.
        ExportRuntimeError: A writer fails, or the companion artifacts fail
            to write.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
    assert config.dimensional is not None
    election = resolve_election(emit.sidecar, config.keys)
    specs = build_query_specs(
        emit,
        config.dimensional,
        anchor,
        None,
        notice_sink,
        base_relations=None,
        election=election,
    )
    if overlay is not None:
        validate_overlay_tables(overlay, [spec.table_name for spec in specs])
    report = write_query_specs(emit, specs, out, fmt)
    write_companion_artifacts(emit, config, fmt, anchor, report, overlay, out, None)
    return report
