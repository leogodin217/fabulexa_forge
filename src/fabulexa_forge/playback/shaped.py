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
shipped ones, never reimplemented. `ShapedPlayback.state()` runs the same
mode's full-export compile (window=None) against the truncated tape: the
compile indirection's `base_relations` mapping (one entry per sidecar base
table, built from the derivations-owned truncated-tape builders) plus the
truncated sidecar view (a second `Emit` composed over the head's own open
connection), so every faithful builder enumerates exactly the columns the
truncated relations carry and the mode never sees a horizon — the bridging
theorem's realization. Dimensional's engine takes `base_relations` directly;
the source engine carries no such parameter (§ 2), so this seam applies the
rewrite itself, post-compile, over the source engine's plain specs
(`_rewrite_specs_base_relations`).

Layer-direction invariant: unlike the rest of `fabulexa_forge.playback`, this
module is the seam's one crossing into `exporters.*` / `config` — tier 2 wraps
the exporters' own compile surfaces rather than reimplementing their business
rules. Imports the reader, `derivations.guard`, `derivations.truncated_tape`,
`config.models`, `exporters.notices`, the dimensional validation/engine
modules, the source plan/engine modules, `exporters.query_spec`,
`incremental.windows`, `fabulexa_forge.errors`, `fabulexa_forge.playback.errors`,
and stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.truncated_tape import open_truncated_tape
from fabulexa_forge.exporters.base_relations import apply_base_relations
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.exporters.dimensional.windowing import window_delivery_class
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.horizon import WindowDelivery
from fabulexa_forge.exporters.source.engine import (
    build_source_query_specs,
    build_windowed_source_query_specs,
    require_source_anchor,
    source_window_delivery,
)
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.reader.emit import Emit, pin_session_timezone

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pyarrow as pa

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import DimensionalConfig, ExportConfig
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import QuerySpec
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

_STATE_TIME_INVALID_MSG = (
    "invalid state position: at_sim_time={at_sim_time} must be >= 0"
)


@dataclass(frozen=True)
class ShapedTable:
    """One output table of a shaped answer.

    name: the shape's output table name, exactly as its full export names it
        (author-declared for a dimensional shape; genre-derived and
        `rename`-mapped for a source shape).
    delivery: how a caller lands this relation — its ShapedTableDecl's
        window_delivery for window(); 'snapshot' for every table of state().
    table: the relation, typed at zero rows, under the full export's column
        list; a dimensional window() delta is ordered by every projected
        column in declared order.
    """

    name: str
    delivery: WindowDelivery
    table: "pa.Table"


@dataclass(frozen=True)
class ShapedTableDecl:
    """One declared output table of the shape — knowable at open, no data read.

    name: the shape's output table name, exactly as its full export names it.
    window_delivery: the table's delivery class under window() — a pure
        function of the declaration and the sidecar, never a refusal. For a
        dimensional shape (`windowing.window_delivery_class`): 'snapshot'
        replaces the table with state(T2 - 1); 'upsert' delivers the rows
        of state(T2 - 1) absent from state(T1 - 1), reconciled by the
        table's declared key; 'append' is an upsert whose delta never
        revises an earlier window's row. A source shape
        (`source.engine.source_window_delivery`): state and junction tables
        'snapshot', the event log 'append'. This is the only delivery fact
        a caller needs before its first ask (sink provisioning, DDL, topic
        setup).
    """

    name: str
    window_delivery: WindowDelivery


def _delivery_for_write_mode(
    write_mode: Literal["create", "append", "replace", "upsert"],
) -> WindowDelivery:
    """Map a windowed QuerySpec's write_mode to a ShapedTable delivery class.

    A windowed compile never tags a spec 'create'.

    Args:
        write_mode: The compiled spec's write_mode.

    Returns:
        'append' / 'upsert' for the same-named write modes; 'snapshot' for
        'replace'.
    """
    if write_mode == "append" or write_mode == "upsert":
        return write_mode
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
    election: "Election",
) -> "list[QuerySpec]":
    """Dispatch a shape's windowed compile to its mode's engine.

    The exact call the incremental driver's `export_window` makes for the
    windowed compile step: the horizon compile — the truncated tape at each
    of the window's horizons, § Shaped window — through `build_query_specs`
    with the window for a dimensional shape, or
    `build_windowed_source_query_specs` for a source shape, with the
    `election` `open_shaped_playback` already resolved.

    Args:
        emit: The open emit.
        config: The target shape.
        anchor: The resolved effective anchor, or None.
        window: The half-open window to compile.
        notice_sink: Receiver for plan notices.
        election: The resolved election, threaded to both modes' engines.

    Returns:
        One QuerySpec per declared output table, in the mode's deterministic
        compile order.

    Raises:
        ExportError: A windowed business rule fails for the shape (naming the
            offending table).
        SourceAnchorRequired: A source shape's anchor is None.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    if config.mode == "source":
        resolved_anchor = require_source_anchor(anchor)
        return build_windowed_source_query_specs(
            emit, config, resolved_anchor, election, window, notice_sink
        )
    assert config.dimensional is not None
    return build_query_specs(
        emit,
        config.dimensional,
        anchor,
        window,
        notice_sink,
        base_relations=None,
        election=election,
        tables=None,
    )


def _rewrite_specs_base_relations(
    specs: "list[QuerySpec]",
    base_relations: "Mapping[str, str]",
) -> "list[QuerySpec]":
    """Apply the base-relations rewrite to every compiled spec.

    playback/shaped.py — the state() seam's post-compile step, replacing
    the engine-side rewrite the old source engine's `base_relations`
    parameter performed (§ 2: the source engine loses that parameter
    entirely — `apply_base_relations` is a pure post-compile SQL rewrite,
    so hoisting it to its one non-None caller loses nothing). Rewrites
    every spec's `sql` via `apply_base_relations`, rebuilding the frozen
    QuerySpecs; every other field passes through unchanged.

    Args:
        specs: The mode engine's compiled specs, compile order.
        base_relations: Physical base-table name -> replacing relation
            SELECT, one entry per sidecar base table
            (the truncated tape's).

    Returns:
        The rewritten specs, input order.
    """
    return [
        replace(spec, sql=apply_base_relations(spec.sql, base_relations))
        for spec in specs
    ]


def _compile_state_specs(
    truncated_emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    base_relations: "Mapping[str, str]",
    election: "Election",
) -> "list[QuerySpec]":
    """Dispatch a shape's state(T) compile to its mode's full-export engine.

    The mode's full-export compile (window=None — write_mode='create' on
    every spec, the full-export tag both modes already emit) against the
    truncated emit view. Dimensional keeps its own `base_relations`
    parameter (mapping every sidecar base table to its truncated-at-T
    replacement, § The compile indirection — the mode never sees a
    horizon). A source shape's engine carries no `base_relations`
    parameter at all (§ 2): its plan builds against the truncated emit
    view directly (`windowed=False`), the query specs compile
    (`build_source_query_specs(plan)`), and this seam applies the
    same rewrite itself (`_rewrite_specs_base_relations`) — so the elected-
    key uniqueness guard, having moved to plan time, executes against the
    truncated *view*'s physical tape through its shared connection: sound
    (uniqueness of a creation-constant surface is monotone under
    row-subsetting) but conservatively strict — a collision existing only
    among rows the truncation drops would still refuse. A state-only shape
    never runs the windowed rules (WindowKeyMutable / WindowKeyDuplicate):
    window=None is the full-export compile.

    Args:
        truncated_emit: The truncated emit view (§ Shaped state).
        config: The target shape.
        anchor: The resolved effective anchor, or None.
        notice_sink: Receiver for plan notices.
        base_relations: Physical base-table name -> replacing relation SELECT,
            one entry per sidecar base table.
        election: The resolved election, threaded to both modes' engines.

    Returns:
        One QuerySpec per declared output table, in the mode's deterministic
        compile order.

    Raises:
        ExportError: A config business rule fails for the shape.
        SourceAnchorRequired: A source shape's anchor is None.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    if config.mode == "source":
        resolved_anchor = require_source_anchor(anchor)
        plan = build_source_plan(
            truncated_emit,
            config,
            resolved_anchor,
            election,
            notices=notice_sink,
        )
        specs = list(build_source_query_specs(plan))
        return _rewrite_specs_base_relations(specs, base_relations)
    assert config.dimensional is not None
    return build_query_specs(
        truncated_emit,
        config.dimensional,
        anchor,
        None,
        notice_sink,
        base_relations=base_relations,
        election=election,
        tables=None,
    )


def _open_dimensional(
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    election: "Election",
) -> tuple[ShapedTableDecl, ...]:
    """Run the dimensional mode's full config validation and derive `tables()`.

    Validates every table declaration exactly as `build_query_specs`' full-export
    loop does (`validate_table`) — the same always-on business rules, in the
    same declaration order, with no windowed-only gate — so any config
    `ExportError` (including the reserved-name and slice_only-column-read
    refusals, and the election gates) passes through unchanged. Each decl's
    delivery class is `window_delivery_class`, static and never a refusal.

    Args:
        config: The dimensional-mode section.
        sidecar: The open emit's sidecar.
        anchor: The resolved effective anchor, or None — threaded to
            TemporalRenderRequiresAnchor.
        notice_sink: Receiver for plan notices.
        election: The resolved election (`resolve_election(sidecar,
            config.keys)`, resolved once by `open_shaped_playback`).

    Returns:
        One ShapedTableDecl per declared table, in declaration order.

    Raises:
        ExportError: A business rule fails.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    decls: list[ShapedTableDecl] = []
    for table_decl in config.tables:
        validate_table(
            table_decl,
            config,
            sidecar,
            notice_sink,
            anchor=anchor,
            election=election,
        )
        decls.append(
            ShapedTableDecl(
                name=table_decl.name,
                window_delivery=window_delivery_class(table_decl, config, sidecar),
            )
        )
    return tuple(decls)


def _open_source(
    config: "ExportConfig",
    emit: "Emit",
    anchor: "EffectiveAnchor",
    notice_sink: "NoticeSink",
    election: "Election",
) -> tuple[ShapedTableDecl, ...]:
    """Run the source mode's full config validation and derive `tables()`.

    Calls `build_source_plan(emit, config, anchor, election, windowed=False,
    notice_sink)` exactly once — the mode's complete validation surface,
    plan-time uniqueness guards included, notices emitted exactly once —
    and maps units to `ShapedTableDecl(name, source_window_delivery(unit))`,
    `tables` declaration order, event log last.

    Open validates the FULL-export shape. A config whose `columns` /
    `rename` names `last_mutation_sim_time` therefore opens and serves
    `state()`; its first `window()` ask raises `SourceColumnUnresolved` from
    the windowed plan build — the source mode's one windowed refusal,
    surfaced as a plan-time error rather than a decl field (the refusal is
    per-column, not per-table-class; a dimensional decl's class is never a
    refusal).

    Args:
        config: The export config (mode='source').
        emit: The open emit.
        anchor: The resolved effective anchor.
        notice_sink: Receiver for plan notices.
        election: The resolved key-election view.

    Returns:
        One ShapedTableDecl per output table.

    Raises:
        ExportError: A source business rule fails (the full plan-time
            surface, § build_source_plan).
        TemporalClassUnavailableError: Propagated.
    """
    plan = build_source_plan(emit, config, anchor, election, notice_sink)
    decls = [
        ShapedTableDecl(name=unit.name, window_delivery=source_window_delivery(unit))
        for unit in plan.tables
    ]
    if plan.events is not None:
        decls.append(
            ShapedTableDecl(
                name=plan.events.name,
                window_delivery=source_window_delivery(plan.events),
            )
        )
    return tuple(decls)


class ShapedPlayback:
    """A shaped tape head: the target shape's tables per window or as of T."""

    def __init__(
        self,
        emit: "Emit",
        config: "ExportConfig",
        anchor: "EffectiveAnchor | None",
        notice_sink: "NoticeSink",
        table_decls: tuple[ShapedTableDecl, ...],
        election: "Election",
    ) -> None:
        self._emit = emit
        self._config = config
        self._anchor = anchor
        self._notice_sink = notice_sink
        self._table_decls = table_decls
        self._election = election

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

        Stateless: the caller owns the frontier. For a dimensional shape:
        the horizon compile (`build_query_specs` with the window) over the
        head's connection — per table, state(end - 1) whole for a
        'snapshot' declaration, else its delta against state(start - 1),
        reconciled by the declared key. For a source shape: the same
        two-horizon compile (`build_windowed_source_query_specs`). This is
        the same compile the incremental
        driver's `export_window` runs. One ShapedTable per declared table,
        zero-row typed relations included, in tables() order.

        Args:
            start_sim_time: Inclusive lower bound (ns); >= 0.
            end_sim_time: Exclusive upper bound (ns); >= start_sim_time.

        Returns:
            One ShapedTable per declared output table.

        Raises:
            PlaybackError: Negative bounds or start > end.
            ExportError: WindowKeyMutable for an 'upsert' table whose key
                is not a stable row identity (static; first ask), or
                WindowKeyDuplicate for one at the end horizon (dimensional);
                a source business rule for either horizon's plan (source).
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
            self._emit,
            self._config,
            self._anchor,
            window,
            self._notice_sink,
            self._election,
        )
        return tuple(
            ShapedTable(
                name=spec.table_name,
                delivery=_delivery_for_write_mode(spec.write_mode),
                table=self._emit.query_arrow(spec.sql, ()),
            )
            for spec in specs
        )

    def state(self, at_sim_time: int) -> tuple[ShapedTable, ...]:
        """The shape's tables as if the emit's slice ended at T (inclusive).

        The mode's full-export compile over the truncated tape (§ Shaped
        state); delivery is 'snapshot' on every table. state(T_slice) is
        value-identical to the shape's full export (the bridging theorem).

        Args:
            at_sim_time: The inclusive position T (ns); >= 0.

        Returns:
            One ShapedTable per declared output table, in tables() order.

        Raises:
            PlaybackError: at_sim_time < 0. No slice_only gate exists
                here: a plan projecting or value-reading a slice_only
                column cannot open (the modes' always-on refusal at
                open_shaped_playback — the slice_only precondition), so
                every openable plan binds against the truncated tape.
                last_mutation_sim_time reads bind against the recorded
                trail the view presents, honest at T.
        """
        if at_sim_time < 0:
            raise PlaybackError(_STATE_TIME_INVALID_MSG.format(at_sim_time=at_sim_time))

        sidecar = self._emit.sidecar
        fork_path = require_single_branch(sidecar)
        tape = open_truncated_tape(sidecar, fork_path, at_sim_time)
        specs = _compile_state_specs(
            self._emit.with_sidecar(tape.sidecar),
            self._config,
            self._anchor,
            self._notice_sink,
            tape.base_relations,
            self._election,
        )
        return tuple(
            ShapedTable(
                name=spec.table_name,
                delivery="snapshot",
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

    When an anchor resolves, pins the emit's materialization session to the
    anchor zone (`pin_session_timezone`) before any relation materializes —
    the same session-zone pin the export driver applies — so every
    zone-bearing value this head's window()/state() compiles serializes in
    the anchor zone regardless of the host machine's zone.

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
        Resolves `config.keys` once (`resolve_election`) for either mode and
        threads it to the open validation and every window() / state()
        compile — the None-for-source special case is gone: a source shape's
        identity/edge gates need the same election view a dimensional
        shape's do.

    Raises:
        PlaybackError: A seam-level open gate fails (source shape with
            anchor=None).
        ExportError: The mode's own config validation fails or the
            single-branch guard trips (passed through unchanged).
    """
    if config.mode == "source" and anchor is None:
        raise PlaybackError(_SOURCE_ANCHOR_REQUIRED_MSG)

    if anchor is not None:
        pin_session_timezone(emit, anchor)

    require_single_branch(emit.sidecar)

    election = resolve_election(emit.sidecar, config.keys)
    if config.mode == "source":
        assert anchor is not None, "the source anchor-required check above narrows this"
        table_decls = _open_source(config, emit, anchor, notice_sink, election)
    else:
        assert config.dimensional is not None
        table_decls = _open_dimensional(
            config.dimensional, emit.sidecar, anchor, notice_sink, election
        )

    return ShapedPlayback(emit, config, anchor, notice_sink, table_decls, election)
