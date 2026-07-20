"""Tier-2 shaped playback: a target export shape's tables per window or as of T.

`open_shaped_playback` binds a target shape (an `ExportConfig`) to an open emit,
running the mode's own full config validation sidecar-only at open — the same
validation a full export runs, reused rather than re-derived, so an invalid
shape's `ExportError` passes through unchanged and a valid shape opens having
read no data. `ShapedPlayback.tables()` reports the shape's declared output
tables and their static per-class/per-genre window-delivery class.
`ShapedPlayback.window()` promotes the incremental driver's own windowed
compile verbatim — the same `build_query_specs` / `build_source_query_specs`
call the driver's `export_window` makes, executed against the head's emit
connection instead of written to a file — so window content and the windowed
business rules (ask-scoped, validated on the first `window()` call) are the
shipped ones, never reimplemented. `.state()` (Phase 12) lands later.

Layer-direction invariant: unlike the rest of `fabulexa_forge.playback`, this
module is the seam's one crossing into `exporters.*` / `config` — tier 2 wraps
the exporters' own compile surfaces rather than reimplementing their business
rules. Imports the reader, the derivations single-branch guard, `config.models`,
`exporters.notices`, the dimensional validation/engine modules, the source
plan/engine modules, `exporters.query_spec`, `incremental.windows`,
`fabulexa_forge.errors`, `fabulexa_forge.playback.errors`, and stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.validation import (
    check_incremental_grain_supported,
    validate_table,
)
from fabulexa_forge.exporters.query_spec import query_spec_output_name
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.playback.errors import PlaybackError

if TYPE_CHECKING:
    import pyarrow as pa

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import DimensionalConfig, ExportConfig, TableDecl
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import QuerySpec
    from fabulexa_forge.exporters.source.plan import SourceTableSpec
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

_SOURCE_ANCHOR_REQUIRED_MSG = (
    "a source shape requires a resolved effective anchor: source renders"
    " wallclock timestamps for every structural sim-time column;"
    " supply rebase.base_date/timezone or rely on the sidecar runtime anchor"
)

_WINDOW_BOUNDS_INVALID_MSG = (
    "invalid window bounds: start_sim_time={start} must be >= 0 and"
    " end_sim_time={end} must be >= start_sim_time"
)


@dataclass(frozen=True)
class ShapedTable:
    """One output table of a shaped answer.

    name: the shape's output table name, exactly as its full export names it
        (author-declared for a dimensional shape; genre-derived and
        `rename`-mapped for a source shape).
    delivery: how a caller lands this relation — 'append' (land the rows
        additively; where a class revises a row across windows — junction /
        history_interval extract-on-change — reconciling is the class's
        documented consumer merge) or 'snapshot' (replace the table). Every
        table of state() is 'snapshot'.
    table: the relation, typed at zero rows.
    """

    name: str
    delivery: Literal["append", "snapshot"]
    table: "pa.Table"


@dataclass(frozen=True)
class ShapedTableDecl:
    """One declared output table of the shape — knowable at open, no data read.

    name: the shape's output table name, exactly as its full export names it.
    window_delivery: the table's delivery class under window() — static per
        table class / genre (§ Shaped window) — or None for a table class
        the windowed-grain rule rejects (history_interval / membership
        grain). None is diagnostic, never a skip: the rule is whole-shape,
        so while any declared table carries None, window() refuses the
        whole shape on its first ask, naming the table; the decl tells the
        caller at open which table its config must drop to window this
        shape. state() is unaffected — every table of state() is delivered
        'snapshot' regardless. This is the only delivery fact a caller
        needs before its first ask (sink provisioning, DDL, topic setup).
    """

    name: str
    window_delivery: Literal["append", "snapshot"] | None


def _dimensional_window_delivery(
    table_decl: "TableDecl",
) -> Literal["append", "snapshot"] | None:
    """Static window-delivery class for one dimensional table declaration.

    Mirrors the windowed dispatch in `dimensional.grains.build_grain_sql`:
    history_interval / membership grains are rejected (the windowed-grain
    rule, reused here via `check_incremental_grain_supported` so the two
    surfaces can never drift); a type-1 dim replaces its full snapshot every
    window; every other class (records/history_point fact, SCD-2 dim) appends.

    Args:
        table_decl: The output table declaration.

    Returns:
        'append', 'snapshot', or None (grain rejected by the windowed-grain
        rule).
    """
    try:
        check_incremental_grain_supported(table_decl)
    except ExportError:
        return None
    if table_decl.role == "dim" and table_decl.scd == "type1":
        return "snapshot"
    return "append"


def _source_window_delivery(
    genre: Literal["changelog", "reference", "transaction", "junction"],
    change_delivery: Literal["changelog", "snapshot"],
) -> Literal["append", "snapshot"]:
    """Static window-delivery class for one source output table's genre.

    Mirrors the windowed dispatch in `source.engine._write_mode_for_genre`:
    change-log genre appends under `changelog` delivery, or snapshots (a full
    state-at reconstruction each window) under `snapshot` delivery; reference
    is a full snapshot every window; transaction and junction append.

    Args:
        genre: The resolved output table's genre.
        change_delivery: The source config's delivery mode for change-log
            kinds.

    Returns:
        'append' or 'snapshot'. Source genres have no grain rejected by the
        windowed-grain rule, so this never returns None.
    """
    if genre == "changelog":
        return "snapshot" if change_delivery == "snapshot" else "append"
    if genre == "reference":
        return "snapshot"
    return "append"


def _delivery_for_write_mode(
    write_mode: Literal["create", "append", "replace"],
) -> Literal["append", "snapshot"]:
    """Map a windowed QuerySpec's write_mode to a ShapedTable delivery class.

    A windowed compile (window is not None) never tags a spec 'create' — the
    shipped windowed dispatch (§ Shaped window) always emits 'append' or
    'replace' — so this only ever sees the two windowed write modes.

    Args:
        write_mode: The compiled spec's write_mode.

    Returns:
        'append' for write_mode='append'; 'snapshot' for write_mode='replace'.
    """
    if write_mode == "append":
        return "append"
    if write_mode == "replace":
        return "snapshot"
    raise AssertionError(
        f"unreachable: a windowed compile tagged write_mode={write_mode!r}"
    )


def _compile_window_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    window: Window,
    notice_sink: "NoticeSink",
) -> "list[QuerySpec]":
    """Dispatch a shape's windowed compile to its mode's engine.

    The exact call the incremental driver's `export_window` makes for the
    windowed compile step — `base_relations=None`, so the compiled SQL reads
    the emit's physical base tables unmediated.

    Args:
        emit: The open emit.
        config: The target shape.
        anchor: The resolved effective anchor, or None.
        window: The half-open window to compile.
        notice_sink: Receiver for plan notices.

    Returns:
        One QuerySpec per declared output table, in the mode's deterministic
        compile order.

    Raises:
        ExportError: A windowed business rule fails for the shape (naming the
            offending table).
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    if config.mode == "source":
        return build_source_query_specs(
            emit, config, anchor, window, notice_sink, base_relations=None
        )
    assert config.dimensional is not None
    return build_query_specs(
        emit, config.dimensional, anchor, window, notice_sink, base_relations=None
    )


def _open_dimensional(
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
) -> tuple[ShapedTableDecl, ...]:
    """Run the dimensional mode's full config validation and derive `tables()`.

    Validates every table declaration exactly as `build_query_specs`' full-export
    loop does (`validate_table` with `window=None`) — the same always-on
    business rules, in the same declaration order, with no windowed-only gate
    — so any config `ExportError` (including the reserved-name and
    slice_only-column-read refusals) passes through unchanged.

    Args:
        config: The dimensional-mode section.
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for plan notices.

    Returns:
        One ShapedTableDecl per declared table, in declaration order.

    Raises:
        ExportError: A business rule fails.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    decls: list[ShapedTableDecl] = []
    for table_decl in config.tables:
        validate_table(table_decl, config, sidecar, None, notice_sink)
        decls.append(
            ShapedTableDecl(
                name=table_decl.name,
                window_delivery=_dimensional_window_delivery(table_decl),
            )
        )
    return tuple(decls)


def _open_source(
    config: "ExportConfig",
    sidecar: "Sidecar",
    notice_sink: "NoticeSink",
) -> tuple[ShapedTableDecl, ...]:
    """Run the source mode's full config validation and derive `tables()`.

    `build_source_plan` is the source mode's complete config-validation
    surface (every source business rule, including the reserved-name and
    rename-onto-a-dropped-column refusals, resolves inside it) and is called
    exactly once, so its notices reach notice_sink exactly once.

    Args:
        config: The export config (mode='source').
        sidecar: The open emit's sidecar.
        notice_sink: Receiver for plan notices.

    Returns:
        One ShapedTableDecl per output table, in the mode's deterministic
        full-export enumeration order.

    Raises:
        ExportError: A source business rule fails.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    table_specs: tuple["SourceTableSpec", ...] = build_source_plan(
        sidecar, config.source, notice_sink
    )
    change_delivery = (
        config.source.change_delivery if config.source is not None else "changelog"
    )
    return tuple(
        ShapedTableDecl(
            name=spec.name,
            window_delivery=_source_window_delivery(spec.genre, change_delivery),
        )
        for spec in table_specs
    )


class ShapedPlayback:
    """A shaped tape head: the target shape's tables per window or as of T."""

    def __init__(
        self,
        emit: "Emit",
        config: "ExportConfig",
        anchor: "EffectiveAnchor | None",
        notice_sink: "NoticeSink",
        table_decls: tuple[ShapedTableDecl, ...],
    ) -> None:
        self._emit = emit
        self._config = config
        self._anchor = anchor
        self._notice_sink = notice_sink
        self._table_decls = table_decls

    def tables(self) -> tuple[ShapedTableDecl, ...]:
        """The shape's declared output tables, in the shape's canonical
        order: config declaration order for a dimensional shape; the source
        mode's deterministic full-export enumeration order for a source
        shape.

        Returns:
            One ShapedTableDecl per table window() and state() will deliver,
            independent of data (the declared-but-empty rule) — name and
            static window delivery class, so a caller can provision sinks
            before any ask.
        """
        return self._table_decls

    def window(
        self,
        start_sim_time: int,
        end_sim_time: int,
    ) -> tuple[ShapedTable, ...]:
        """The shape's tables for the half-open window [start, end).

        Stateless: the caller owns the frontier. Content per table class /
        genre is the promoted window-membership contract (§ Shaped window) —
        this runs the same `build_query_specs` / `build_source_query_specs`
        windowed compile the incremental driver's `export_window` runs, over
        the head's emit connection; every value is its full-export value —
        the window selects rows, never recomputes them. One ShapedTable per
        declared table, zero-row typed relations included, in tables() order.

        Args:
            start_sim_time: Inclusive lower bound (ns); >= 0.
            end_sim_time: Exclusive upper bound (ns); >= start_sim_time.

        Returns:
            One ShapedTable per declared output table.

        Raises:
            PlaybackError: Negative bounds or start > end.
            ExportError: A windowed business rule fails for the shape
                (first window call; passed through unchanged).
        """
        if start_sim_time < 0 or end_sim_time < start_sim_time:
            raise PlaybackError(
                _WINDOW_BOUNDS_INVALID_MSG.format(
                    start=start_sim_time, end=end_sim_time
                )
            )

        window = Window(
            index=None, start_ns=start_sim_time, end_ns=end_sim_time, label=""
        )
        specs = _compile_window_specs(
            self._emit, self._config, self._anchor, window, self._notice_sink
        )
        return tuple(
            ShapedTable(
                name=query_spec_output_name(spec),
                delivery=_delivery_for_write_mode(spec.write_mode),
                table=self._emit.query_arrow(spec.sql, ()),
            )
            for spec in specs
        )


def open_shaped_playback(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> ShapedPlayback:
    """Bind a shaped head to an open emit and a declared target shape.

    Runs the mode's full config validation at open (sidecar-only, no data
    reads) — a shape whose plan projects or value-reads a slice_only column
    is refused here by the mode's own always-on rules (the export-wide
    policy, inherited as a precondition), and an output column named
    last_mutation_sim_time by the mode's reserved output-name check (the
    presentation-name posture). The windowed business rules are ask-scoped —
    validated on the first window() call — so a shape legal for state()
    but not window() still opens. The shape is the config's mode + mode
    section + shared exporter features; the config's rebase block is not
    read (the caller resolves the anchor) and its incremental block is not
    read (cadence-boundary sequences are the caller's job — the seam speaks
    raw-ns bounds only).

    The head binds notice_sink for its lifetime and threads it to every
    mode compile it runs — the open validation and each window() / state()
    compile — so each ask's compile delivers its plan notices to the sink
    as emitted (an ask re-emits its compile's notices, the incremental
    drip rule). Tier 1 runs no mode compile and emits no notices.

    Args:
        emit: An open emit (version-gated by open_emit).
        config: The target shape — a validated ExportConfig (mode:
            dimensional or source; base extends the Literal when it lands).
        anchor: The resolved effective anchor, or None. The source mode's
            mandatory-anchor rule applies at open.
        notice_sink: Receiver for plan notices from every compile the head
            runs (required — the notice-channel contract; a caller that
            wants silence passes a discarding sink).

    Returns:
        A ShapedPlayback head bound to (emit, config, anchor, notice_sink).

    Raises:
        PlaybackError: A seam-level open gate fails (source shape with
            anchor=None).
        ExportError: The mode's own config validation fails or the
            single-branch guard trips (passed through unchanged).
    """
    if config.mode == "source" and anchor is None:
        raise PlaybackError(_SOURCE_ANCHOR_REQUIRED_MSG)

    require_single_branch(emit.sidecar)

    if config.mode == "source":
        table_decls = _open_source(config, emit.sidecar, notice_sink)
    else:
        assert config.dimensional is not None
        table_decls = _open_dimensional(config.dimensional, emit.sidecar, notice_sink)

    return ShapedPlayback(emit, config, anchor, notice_sink, table_decls)
