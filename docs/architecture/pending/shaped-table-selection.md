---
status: draft
---

# Shaped Table Selection

Per-table asks on the tier-2 shaped playback head — `window(T1, T2, tables=S)`
and `state(T, tables=S)` — with the dimensional key rule promoted to a
load-time fact so every table that opens can be windowed, and the start
horizon opened only when an ask delivers a delta.

---

## Problem

The tier-2 shaped head answers only whole shapes. `window(T1, T2)` and
`state(T)` compile and materialize every declared table for one bound pair, so
a consumer that needs one table at its own bounds pays for all of them. Loom's
channel model makes that the common case: every strip (one declared table
under a head) carries its own lag, so under one NHS source head the `booking`
strip asks `window(last_flush, frontier − 30 min)` while `patient` asks
`window(last_flush, frontier)` — two asks, each materializing all 14 tables.

Measured on the NHS bundle at the current code, a whole-shape
`window(HOUR, 2·HOUR)` costs:

| Shape | Compile (both horizons, guards included) | Materialize all tables | Whole ask |
|---|---|---|---|
| `source.yaml` (14 tables) | 0.16 s | 2.03 s (`audit_log` alone 1.5 s; the rest ~80 ms each) | 1.5–1.7 s |
| `dimensional.yaml` (30 tables) | 0.53 s | 1.12 s (~40 ms each, flat) | 1.4–1.75 s |

Loom's tick is synchronous, so per-ask latency is its budget: a 4-table window
ask must cost well under 250 ms and an 11-table ask about 1 s at 1000× speed.
The whole-shape ask is 5–7× over. The cost is O(declared tables) in three
places, not one: materialization; the compile's per-table **data guards**,
which run at both window horizons (elected-key uniqueness scans per FK edge,
upsert-key uniqueness at the end horizon); and notice re-emission. Making
materialization lazy would leave the 0.53 s dimensional compile floor alone
already over budget.

A second cost hides in the horizons themselves. Every `window()` ask opens the
tape at both horizons and runs the whole compile at each, yet a table
delivered whole (`snapshot`) is answered from the end horizon alone — its
start-horizon compile, guards, and notices are produced and discarded. In the
source shape every `state` and `junction` table is `snapshot`; only the event
log needs the start horizon. A `patient` ask pays two whole-config plan builds
for a table that needs one.

A third gap is the dimensional mode's two static windowed refusals, which are
whole-shape and ask-time. `WindowKeyMutable` refuses an `upsert`-delivered
table whose declared `key` includes a column whose value can change between
horizons; `IncrementalReservedName` refuses a table named for an incremental
bookkeeping table. Both are pure functions of the declaration and the sidecar,
yet they run only under a windowed compile: the same config exports one-shot
without complaint and fails on the first `window()` ask, and one such table
refuses the whole shape. The root of the first rule is a modelling fact, not a
windowing one — a declared `key` is the table's row identity, and a value that
changes over the run is not an identity. Checking it only where reconciliation
happens to need it is what makes it late and whole-shape.

## Solution

Three changes, each independent of the others' mechanics and all serving the
one purpose — an ask pays for what it delivers, and every table that opens can
be asked.

**1. A table selection on both tier-2 asks.** `window()` and `state()` take a
caller-chosen `tables`: any non-empty subset of the names `tables()` reports
(a singleton included), or `None` for the whole shape — byte-identical to
today. Selection is a **projection over the whole-shape compile, never a
config rewrite**: every declaration still resolves against the full config (FK
targets, the election), but per-table compile, data guards, and
materialization run only for selected tables. The answer is the selected
tables in `tables()` order, and one invariant is contract: **a table's answer
is identical whether it is asked alone, with any siblings, or as part of the
whole shape.** The head stays stateless — bounds, grouping, and position are
the caller's; forge defines no sets.

**2. The static windowed rules become always-on load-time rules.** The
dimensional key rule is restated as what it is — **`KeyColumnsStable`: every
`key` column of every table is a horizon-invariant value channel** — and runs
with the mode's always-on business rules at load, sidecar-only, one-shot
export included, on every table regardless of its delivery class. The
reserved table-name rule joins the always-on set the same way (as the source
mode's already does at every plan build). A shape that opens therefore has no
static reason any table cannot be windowed: `tables()` carries the complete
windowability surface (the delivery class), `ShapedTableDecl` needs no new
field, and `window()` never raises a rule the one-shot export would not.

**3. An ask opens the start horizon only when it delivers a delta.** A
selection is **delta-bearing** when some selected table's delivery class is
`upsert` or `append` (dimensional) or is the event log (source); otherwise it
is **delta-free**. A delta-free `window()` opens the tape at the end horizon
only: no start-horizon compile, guards, or notices. Every `snapshot` answer is
the end-horizon relation whole in either case, so the answer is unchanged;
what changes is that a `patient` ask on the source shape runs one plan build,
not two.

```
tables()  →  (patient  snapshot,  booking  snapshot,  audit_log  append, …)

window(T1, T2, tables={"booking"})          → (booking,)     end horizon only; booking's compile + materialization
window(T1, T2, tables={"gp", "patient"})    → (patient, gp)  tables() order, not ask order; end horizon only
window(T1, T2, tables={"audit_log"})        → (audit_log,)   both horizons; the log's delta
window(T1, T2, tables={"patient", "audit_log"}) → (patient, audit_log)  both horizons; patient answered from the end
state(T, tables={"audit_log"})              → (audit_log,)
window(T1, T2)                              → the whole shape, unchanged
window(T1, T2, tables=set())                → PlaybackError  an ask that names nothing
window(T1, T2, tables="booking")            → PlaybackError  a str, not a collection of names
window(T1, T2, tables={"nope"})             → PlaybackError  not a declared table

open_shaped_playback(emit, config)          → ExportError    a fact keyed on a tracked property: refused at open,
                                                             exactly as `export` refuses it one-shot
```

## Affected Subsystems

- **Playback seam, tier 2 (the shaped head)** — `window()` and `state()` gain
  the `tables` selection with its three gates (a bare `str`, empty, unknown);
  the projection-invariance and horizon-economy invariants join the seam's
  invariant set; no first-`window()`-ask static refusal exists.
  `ShapedTableDecl` is unchanged.
- **Dimensional validation** — `KeyColumnsStable` joins the always-on rule
  set, reading the horizon-invariance classification the windowing module
  already owns; the reserved table-name check moves from the windowed compile
  into the always-on set beside the reserved column-name check. Both run at
  every load of a dimensional config — `export`, the incremental driver, and
  `open_shaped_playback` alike.
- **Dimensional engine** — its one compile entry (full-export and horizon
  forms alike) accepts a table selection. The per-table loop,
  `WindowKeyDuplicate`, and the FK guards run for selected tables only; the
  dim-side leg guard therefore covers exactly the dims the selected tables'
  edges reach. The windowed form opens the start horizon only for a
  delta-bearing selection. The two static windowed checks leave the engine
  (they are validation's now).
- **Source engine** — the windowed compile accepts the same table selection:
  its plan builds stay whole-config (the plan is the source mode's unit of
  validation; its data-dependent guards are plan-time and population-scoped,
  not per output table), it returns the selected specs, and it opens the start
  horizon only when the event log is selected. The full-export compile is
  unchanged; the seam selects among its specs for `state()`.
- **Incremental driver** — passes no selection and keeps sharing one compile
  with the seam. Three behaviors follow from the rules moving, none a change
  in delivered content: a config with an unstable key or a reserved table
  name is refused when the config loads rather than at the first windowed
  compile; a whole shape that is delta-free (no `upsert` / `append` table; a
  source shape with no event log) compiles one horizon per window; and the
  start-horizon plan notices such a shape used to re-emit per window are no
  longer emitted.

## What Doesn't Change

- The answer for any table: `ShapedTable(name, delivery, table)` and every
  value in it. Selection and the horizon economy never alter content, order
  within a table, typing, or the delivery class.
- `ShapedTableDecl`; `tables()` order and membership; `window_delivery`
  semantics and the classifier.
- The two-horizon compile for a delta-bearing ask, the truncated tape, the
  compile indirection, the consistency algebra, and the bridging theorem.
- The incremental driver's window sequence, cursor, fingerprint, writers, and
  its whole-shape compile; the full-export verbs' output for every config that
  loads.
- The source mode's plan build (whole-config at each horizon it opens) and its
  windowed delivery classes.
- The seam's statelessness: no per-table position, cursor, or cache exists at
  the head. Grouping tables that share bounds into one ask is the caller's
  optimization, never a forge requirement.
- Tier 1 and the stream head.
- The declared `key`'s posture as a validated logical key, never a
  materialized constraint: uniqueness is still not checked one-shot, and the
  only data-level windowed refusal is still `WindowKeyDuplicate`.
- Query performance of any single table (the `audit_log` outlier is a separate
  source-mode item).

## Semantics

### The selection

| Condition | Result |
|---|---|
| `tables=None` | The whole shape: every declared table, `tables()` order — byte-identical to today |
| `tables=S`, `S` a non-empty subset of the declared names | The tables in `S`, in `tables()` order (ask order is irrelevant) |
| `S` given as a sequence with a repeated name | Set semantics: the name selects its table once |
| `S` given as a bare `str` | `PlaybackError` — a `str` is a `Collection[str]` of its characters, so `tables="booking"` would otherwise read as five one-character names; the seam refuses the type before reading any name |
| `S` empty | `PlaybackError` — an ask that names nothing is a caller error, not an empty answer |
| `S` contains a name `tables()` does not report | `PlaybackError` naming every unknown name (sorted — the message is deterministic whatever the collection's iteration order) and the declared names; nothing compiles |

Gate order on an ask: bounds → selection (a bare `str`, empty, unknown) →
compile. Every gate is sidecar-only; no data is read before the compile.

**An ask is atomic.** One call returns every selected table or raises: a
data guard failing on any selected table — or on a dim a selected table's
guarded edge reaches — refuses the whole ask, naming the offending table, and
no sibling in that ask is delivered. Isolation is the caller's, by asking
separately: `booking` and `patient` asked in one call share one fate; asked
in two calls, a `booking` key duplicate refuses `booking` alone. The dim-side
leg guard is the case to know: a dim asked without any fact that reaches it
is not guarded (as in the full export, where a dim no edge reaches is never
guarded), so `dim_customer` alone succeeds where `{fact_booking,
dim_customer}` may refuse — the guard's outcome is local to the edge, the
ask's outcome is the conjunction of its gates.

### Horizons per ask

| Ask | Delta-bearing selection | Delta-free selection |
|---|---|---|
| `window(T1, T2, tables=S)` | Both horizons: the tape at `T2 − 1` and at `T1 − 1`; a `snapshot` table's answer is its end-horizon relation, every other table's is `end EXCEPT ALL start` | The end horizon only: the tape at `T2 − 1`; every answer is its end-horizon relation whole. The start horizon is not opened — no compile, no guard, no notice at it |
| `state(T, tables=S)` | One horizon, the tape at `T` — unchanged | Same |

A selection is delta-bearing when some selected table's class is `upsert` or
`append` (dimensional) or the event log is selected (source). The whole shape
(`tables=None`) is delta-bearing exactly when some declared table is. A
`window()` answer never depends on which case applies: a `snapshot` table's
relation is the end-horizon compile in both, and a delta table forces both
horizons. What the start horizon contributes to a delta-free ask today is a
discarded compile, a set of guards whose verdicts the end horizon already
implies (§ Invariants relied on), and a second emission of the same plan
notices.

### What a selected compile does and does not run

| Work | Selected tables | Unselected tables |
|---|---|---|
| Declaration resolution the selected tables depend on (FK target lookup, the election, sub-type domains) | Runs against the full config | Their declarations are visible to the resolver; nothing is compiled from them |
| Always-on per-table business rules (`validate_table` and its notices) | Runs, at each horizon opened | Skipped — the engine's selection contract presumes the caller validated the whole shape (the seam does, at open; the driver passes no selection) |
| Per-table SQL compile | Runs, at each horizon opened | Skipped |
| `WindowKeyDuplicate` (dimensional `upsert`, end horizon) | Runs | Skipped |
| FK-edge elected-key uniqueness guard | Runs for the selected table's edges, at each horizon opened | Skipped |
| Dim-side leg guard | Runs for each dim reached by a selected table's guarded edge, at each horizon opened | A dim reached by no selected edge is not guarded |
| Materialization | Runs | Skipped |
| Plan notices from per-table resolution | Emitted, once per horizon opened | Not emitted |
| Source plan build (its plan-time guards and notices) | Runs whole-config, once per horizon opened | Runs whole-config, once per horizon opened |

An unselected table's **own** gates never affect a selected table: its
`WindowKeyDuplicate` and the guards on its own fk edges are not run, and a
`booking` key duplicate refuses a `booking` ask and no other. The one way an
unselected table's name appears in a selected ask's refusal is the dim-side
leg guard: a selected table's edge reaches a dim whose declared `key`
projects the edge's resolved surface, the dim's population surface fails
uniqueness, and the ask refuses with the engine's own label (`"<dim>
(dim-side leg)"`). That is the selected edge's guard — a fact of the tape,
the edge, and the reached dim's declaration (resolved against the full
config, as every declaration is) — so the dim need not be selected for it to
run, and selecting the dim alone does not run it.

### The stable-key rule

`KeyColumnsStable`: for every declared dimensional table, every `key` column's
value channel is horizon-invariant under the reading the windowing module
already owns ([`incremental.md`](incremental.md) § Horizon-invariant value
channels) — an identity column, an event-time column, a constant property, a
version's `valid_from`, an `scd: type2` property, a reference-hop FK over
`history_tracked: false` hops, a membership-grain row's own binding, a
`lookup`, or a `null`. A column whose value the run can change — a tracked
property on a non-versioned table, `active`, `deactivated_at`, the recorded
trail, a `valid_to`, an `elapsed` duration, an ordinal over a mutable
partition, a membership FK on a records grain — is not a row identity and is
refused in a key, naming the table, the column, and the varying source
(§ Validation Rules).

The reading is conservative, and the rule inherits that posture whole: a
channel whose constancy the sidecar cannot establish is refused, not admitted.
That covers a key column reading a producer-added column outside the
contract's structural set, a `prop__` column the sidecar does not declare, and
every `fk via: reference` key column on an emit whose sidecar carries no
`history_tracked` flags — each refused with a message naming the unknown
constancy as the varying source. Today those verdicts reach only an `upsert`
table under a windowed compile; under this rule they reach the one-shot export
of every table. The always-on path softens nothing.

The rule runs last among the per-table always-on rules — after every rule the
reading presumes (`KeyColumnsDeclared`, `ProjectionColumnExists`,
`OrdinalRefsSiblings`, `FkTargetIsDim`, `ReferencePathResolvable`,
`MembershipEdgeResolvable`, `LookupColumnSafety`), so a bad fk target, path
hint, or ordinal reference refuses under its own rule's identity, never as a
stability refusal or an internal error. This is the position the delivery
classifier already occupies (it never raises on a declaration the always-on
rules admit); the rule and the classifier read the same function from the same
position.

The rule holds on every table whatever its delivery class. A `snapshot`
table's key is never used to reconcile, but the key is the table's declared
row identity, documented and recorded as the warehouse PK; an identity that
changes is wrong on a one-shot export exactly as it is under a window. The
rule is a pure function of declaration and sidecar, runs with the always-on
rules at load before any data is read, and is horizon-independent (§
Invariants relied on) — so it holds at every horizon a windowed compile opens
without being re-derived there.

`WindowKeyMutable` is retired as a windowed rule: with `KeyColumnsStable`
always-on, no config reaches a windowed compile with an unstable key. The
reserved table-name rule (`_export_meta` / `_export_windows`) is likewise
retired from the windowed compile and enforced always-on beside the reserved
column-name check, so a full export and a later drip on the same target agree
by construction — the source mode's posture, now both modes'.

### Invariants introduced

1. **Projection invariance (contract).** For every shape, bounds, and
   selection `S`, and every `t ∈ S`: the `ShapedTable` for `t` under
   `window(T1, T2, tables=S)` equals the `ShapedTable` for `t` under
   `window(T1, T2)`, row-for-row and byte-for-byte; likewise for `state`. A
   consumer that records asks grouped one way may replay them grouped another
   way and see the same data. The horizon economy preserves it: a `snapshot`
   answer is the end-horizon relation whether or not the start horizon was
   opened.
2. **Guard locality.** The outcome of every ask-time gate is a function of
   the gated table's declaration, its reached dims' declarations, the
   bounds, and the tape — never of which siblings share the ask. Whether a table is
   *delivered* is the ask's outcome — the conjunction of the gates the
   selection runs — so grouping never changes a gate's verdict, only which
   verdicts one call collects.
3. **Static completeness at open.** After `open_shaped_playback`, `tables()`
   carries everything about windowability that is knowable without data — the
   delivery class — and every table that opens windows. No `window()` ask
   raises a rule the one-shot export of the same config would not; the only
   ask-time refusals are the data guards.
4. **Key identity.** On every dimensional table of every config that loads,
   the declared `key` is a stable row identity: a row keeps its key at every
   horizon at which it exists. A load-time fact of every mode entry, not a
   windowed one.
5. **Horizon economy.** A `window()` ask opens the start horizon if and only
   if it delivers a delta.

### Invariants relied on

- Every table's compiled SQL is self-contained: it reads base tables (through
  the horizon's truncated relations) and never a sibling's output. This is
  what makes projection exact.
- The only cross-table coupling in the compile is declaration-level (an FK
  column names a declared dim); it is satisfied by the full config being
  present, not by the dim being in the ask.
- `window_delivery_class` and the horizon-invariance reading are pure
  functions of declaration and sidecar — which is what lets the key rule be a
  load-time fact and the delivery class a decl fact.
- Both modes' static rules are horizon-independent for every shape that
  opens: the truncated sidecar view is the physical one minus the
  `slice_only` columns (bar the discriminator carve-out —
  [`derivations.md`](derivations.md) § The truncated-tape surface;
  `last_mutation_sim_time` stays declared, presented as the recorded trail),
  and a shape that reads a `slice_only` column is refused at open by the
  modes' always-on rules. So every column a rule consults at a horizon it
  also consulted at open, and a plan that builds at open builds at every
  horizon.
- **Start-horizon guards are implied by the end horizon.** Dimensional:
  elected-key uniqueness is monotone under row-subsetting (a
  creation-constant surface unique over the rows at `T2 − 1` is unique over
  the subset at `T1 − 1`), and `WindowKeyDuplicate` is evaluated at the end
  horizon only by definition. Source: the plan-time uniqueness guard is
  horizon-independent by construction — it runs against the physical tape
  through the shared connection whichever truncated view the plan builds
  over (conservatively strict: a collision among rows the truncation drops
  still refuses), so the start-horizon plan's verdict is the end-horizon
  plan's verdict. Either way, skipping the start horizon on a delta-free ask
  removes no verdict a delta-bearing ask would have produced — the economy
  is verdict-preserving, not merely answer-preserving.

### Cost shape

The design's purpose is a cost that is a per-ask floor plus a per-selected-
table marginal: a single-table ask pays the floor and that table's compile,
guards, and materialization, never a sibling's — and a delta-free ask pays a
one-horizon floor. That shape is a consequence of the semantics above, not a
separately verified bar — no timing target gates this design; the loom
budgets in § Problem are the motivation, and whether projection meets them is
measured once the logic ships. The source floor — one whole-config plan build
per horizon opened — is a stated, accepted cost: two per delta-bearing
`window` ask, one per delta-free `window` ask, one per `state` ask.

## Configuration

None. The seam is a library surface; selection is an ask argument, and no
YAML declares which tables a consumer may select. The stable-key rule
introduces no field: it constrains the existing `key`.

## Interface Contracts

### Runtime Types

`ShapedTableDecl` and `ShapedTable` are unchanged.

### The shaped head

```python
class ShapedPlayback:
    def tables(self) -> tuple[ShapedTableDecl, ...]:
        """The shape's declared output tables, canonical order, each with
        its static delivery class.

        Returns:
            One ShapedTableDecl per table window() and state() can deliver,
            independent of data — the complete static windowability
            surface: every table that opens can be windowed, and its class
            says how a caller lands it.
        """

    def window(
        self,
        start_sim_time: int,
        end_sim_time: int,
        tables: "Collection[str] | None" = None,
    ) -> tuple[ShapedTable, ...]:
        """The selected tables for the half-open window [start, end).

        Stateless: the caller owns the frontier and the grouping. The
        horizon compile runs for the selected tables only — per-table
        compile, data guards, and materialization — while every declaration
        still resolves against the full config. The start horizon is opened
        only when the selection is delta-bearing (some selected table's
        class is 'upsert' or 'append'; the source event log). The answer for
        a table is identical whichever selection it is asked in (projection
        invariance).

        Args:
            start_sim_time: Inclusive lower bound (ns); >= 0.
            end_sim_time: Exclusive upper bound (ns); >= start_sim_time.
            tables: The declared names to answer, set semantics, or None
                for every declared table. Answered in tables() order.

        Returns:
            One ShapedTable per selected table, tables() order, zero-row
            typed relations included.

        Raises:
            PlaybackError: Negative bounds or start > end; `tables` a bare
                str, or empty; a name tables() does not report. All before
                any compile.
            ExportError: WindowKeyDuplicate for a selected 'upsert' table at
                the end horizon; ElectedKeyDuplicate on a selected table's
                guarded edge or a dim it reaches (dimensional); a source
                business rule for a plan at an opened horizon (source). The
                ask is atomic: any of these refuses every selected table.
            TemporalClassUnavailableError: A consulted column's temporal pair
                is absent or out of enum (non-conformant emit).
        """

    def state(
        self,
        at_sim_time: int,
        tables: "Collection[str] | None" = None,
    ) -> tuple[ShapedTable, ...]:
        """The selected tables as if the emit's slice ended at T (inclusive).

        The mode's full-export compile over the truncated tape, for the
        selected tables only; delivery is 'snapshot' on every table.
        state(T_slice, tables=S) equals the full export restricted to S (the
        bridging theorem under projection).

        Args:
            at_sim_time: The inclusive position T (ns); >= 0.
            tables: The declared names to answer, set semantics, or None
                for every declared table. Answered in tables() order.

        Returns:
            One ShapedTable per selected table, tables() order.

        Raises:
            PlaybackError: at_sim_time < 0; `tables` a bare str, or empty;
                a name tables() does not report.
            ExportError: ElectedKeyDuplicate on a selected table's guarded
                edge or a dim it reaches (dimensional); a source business
                rule for the plan over the truncated view (source). The ask
                is atomic: any of these refuses every selected table.
            TemporalClassUnavailableError: A consulted column's temporal pair
                is absent or out of enum (non-conformant emit).
        """
```

### Dimensional validation

```python
def check_key_columns_stable(
    table_decl: "TableDecl",
    config: DimensionalConfig,
    sidecar: "Sidecar",
) -> None:
    """Enforce KeyColumnsStable: every `key` column is a stable row identity.

    An always-on business rule of the dimensional mode, run by
    `validate_table` on every table regardless of delivery class — the
    one-shot export, the incremental driver, and the shaped head alike —
    and run last among the per-table rules, after every rule the reading
    presumes (declared key columns, resolvable projections, ordinal
    sibling refs, fk targets and paths, membership edges, lookup safety),
    so those refuse under their own identities first. A pure function of
    declaration and sidecar; never reads data. Each key column's value
    channel is classified by the windowing module's horizon-invariance
    reading — conservative: a channel of unknown constancy (a producer
    column outside the structural set, a reference fk on an emit without
    `history_tracked` flags) is refused, not admitted. The first varying
    channel in `key` order refuses.

    Args:
        table_decl: The output table declaration (its key and columns).
        config: The dimensional config (fk resolution).
        sidecar: The open emit's sidecar.

    Raises:
        ExportError: A key column's value can change over the run; the
            message names the table, the column, and the varying source.
    """
```

The reserved table-name check (`_export_meta` / `_export_windows`) runs in the
same always-on set, beside the reserved column-name check, through the shared
reserved-names authority the source mode already reads.

### Dimensional engine

```python
def build_query_specs(
    emit: "Emit",
    config: DimensionalConfig,
    anchor: "EffectiveAnchor | None",
    window: "Window | None",
    notice_sink: "NoticeSink",
    base_relations: "Mapping[str, str] | None",
    *,
    election: "Election | None" = None,
    tables: "Collection[str] | None",
) -> list[QuerySpec]:
    """Compile table declarations; optionally windowed; optionally a selection.

    `tables` narrows the compile to the named declarations: per-table
    compile, WindowKeyDuplicate, the fk-edge guard, and the dim-side leg
    guard (over the dims the selected tables' edges reach) run for selected
    tables only; every declaration remains visible to resolution. None
    compiles every declared table — the full-export and incremental-driver
    contract. No parameter default: every caller states its selection (the
    two whole-shape callers pass None).

    Under a window: the horizon compile over the truncated tape. The end
    horizon (`window.end_ns - 1`) is always opened; the start horizon
    (`window.start_ns - 1`) only when some compiled table's delivery class
    is 'upsert' or 'append'. A 'snapshot' table's spec is its end-horizon
    query with write_mode 'replace'; every other table's is `end EXCEPT ALL
    start` with write_mode 'append' or 'upsert' (and the key as
    `upsert_key`), guarded by WindowKeyDuplicate over the end-horizon
    relation. No static windowed rule runs here: KeyColumnsStable and the
    reserved table-name rule are always-on validation.

    Args:
        emit: The open emit (trunk-only; sole branch).
        config: The validated dimensional config.
        anchor: The resolved EffectiveAnchor, or None for raw sim_time.
        window: The half-open window to compile, or None for the full export.
        notice_sink: Receiver for plan notices — the compiled tables'
            per-table notices, once per horizon opened.
        base_relations: As today; must be None under a window.
        election: As today.
        tables: The declared table names to compile, set semantics, or
            None for every declared table. Every name must be declared —
            the seam validates this before calling; an undeclared name is
            a programming error and fails an `assert` (never ExportError),
            so no caller can mistake it for a business rule. A selection
            presumes the caller has already run the always-on rules over
            the whole shape: unselected declarations are resolved against,
            never validated.

    Returns:
        One QuerySpec per compiled table, declaration order.

    Raises:
        ExportError: As today, scoped to the compiled tables: an always-on
            business rule (KeyColumnsStable and the reserved names among
            them); under a window, WindowKeyDuplicate; ElectedKeyDuplicate
            on a compiled edge or a reached dim; the election gates on a
            compiled fk column.
        ValueError: window is set and base_relations is not None.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            absent or out of enum.
    """
```

### Source engine

```python
def build_windowed_source_query_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor",
    election: Election,
    window: "Window",
    notice_sink: "NoticeSink",
    *,
    tables: "Collection[str] | None",
) -> list[QuerySpec]:
    """The source mode's horizon compile, optionally a selection.

    The plan builds whole-config at every horizon opened — the plan is the
    mode's unit of validation, and its data-dependent guards and notices
    are plan-scoped, not per output table. `tables` decides two things
    only: which units' specs are returned, and whether the start horizon is
    opened — it is, exactly when the event log is selected (or `tables` is
    None and the plan declares one). A `state` / `junction` unit's spec is
    its end-horizon query with write_mode 'replace'; the event log's is
    `end EXCEPT ALL start` with write_mode 'append'.

    Args:
        emit: The open physical emit.
        config: The full export config (mode: source).
        anchor: The resolved wallclock anchor.
        election: The resolved key-election view.
        window: The half-open window to compile.
        notice_sink: Receiver for plan notices, once per horizon opened.
        tables: The output table names to return, set semantics, or None
            for every unit. Every name must be an output table of the
            plan — the seam validates this before calling; an unknown name
            fails an `assert`.

    Returns:
        One QuerySpec per selected unit, declared order, the event log last.

    Raises:
        ExportError: A source business rule fails for a plan at an opened
            horizon.
    """
```

The full-export compile (`build_source_query_specs`) is unchanged; for
`state()` the seam selects among its specs by `table_name` before
materialization.

## Validation Rules

### Parse-Time (Pydantic)

None — no config surface changes.

### Business Rules

All seam gates raise `PlaybackError`; mode rules keep their `ExportError`
identities.

| Rule | Where | Checks | Error Message |
|---|---|---|---|
| `SelectionIsString` | seam, `window` and `state` | `tables` is a `str` | `"tables must be a collection of table names, not a str: {value!r}"` |
| `SelectionEmpty` | seam, `window` and `state` | `tables` is not None and names nothing | `"tables must name at least one declared table"` |
| `SelectionUnknownTable` | seam, `window` and `state` | a name in `tables` is not a `tables()` name | `"tables names no declared output table: {unknown}; declared: {names}"` — `unknown` every unknown name sorted, `names` in `tables()` order |
| `KeyColumnsStable` | dimensional validation, always-on, every table | every `key` column's value channel is horizon-invariant | `"table '{name}' key column '{key_col}': its value can change over the run ({variance}); a key identifies the row for the whole run"` |
| Reserved table name | dimensional validation, always-on | no table named `_export_meta` / `_export_windows` | as today's `IncrementalReservedName` text |
| `WindowKeyMutable` | — | retired; subsumed by `KeyColumnsStable` | — |
| `IncrementalReservedName` | — | retired as a windowed rule; the check is always-on | — |
| `WindowKeyDuplicate` | engine | as today, for compiled `upsert` tables at the end horizon | as today |
| `ElectedKeyDuplicate` | engine | as today, for compiled tables' edges and the dims they reach, at each horizon opened | as today |

## Rationale

- **Selection over laziness.** A lazy `ShapedTable` removes materialization
  only; the compile's per-table data guards run at both horizons regardless
  and already exceed loom's budget on the dimensional shape. Selection scopes
  every per-table term. It also keeps the tier-2 answer a value rather than a
  handle bound to the head's connection.
- **Selection over per-table sub-heads.** A `head.table(name)` object would be
  a stateless wrapper over the same compile that also loses the ability to
  batch tables sharing bounds into one ask — which loom's board does for most
  of its strips (11 PAS tables hourly at one lag).
- **Set-valued, caller-defined.** Any table can be asked alone, so per-table
  lag is unconditional; tables sharing bounds may be grouped to amortize the
  floor. Forge defines no groupings — a fixed grouping would be a scheduling
  concept, which is the consumer's.
- **Projection, not config rewrite.** Narrowing the config would change
  semantics (an FK target must be declared) and re-run whole-config validation
  per ask. Projecting over the whole-shape compile keeps declaration
  resolution exactly as the full export sees it.
- **Ask-scoped guards.** Scoping data guards to the selection is what makes
  strips independent — one table's key duplicate refuses that table's flush
  and no other, when the strips ask separately — and is exact because every
  guard is per table or per reached dim. Asks stay atomic rather than
  delivering partial answers because a partial answer would need a per-table
  error channel on a value-returning call, and grouping is already the
  caller's choice: a caller that wants isolation asks alone. The trade: a
  defect on an unasked table is discovered when that table is asked, not
  before. That is the same posture as today's "first `window()` ask",
  narrowed.
- **A key is an identity, not a delivery detail.** The declared `key` is the
  table's documented grain and recorded warehouse PK. A column whose value
  the run changes does not identify a row; a table keyed on one has a wrong
  key on a one-shot export too — the window path was merely the first place
  the wrongness had a consequence. Checking it once, at load, on every table,
  is cheaper to reason about than a windowed rule with a class-dependent
  scope, and it is the posture the repo already takes for silently-broken
  joins (the dim-key agreement gate refuses statically before any data is
  read). The alternative — deliver an unstable-key table as a whole snapshot
  and notify — keeps a wrong key working and slows the fast path silently;
  refusing at load says what to fix. Every dimensional config shipped with
  the repo (the example estate and every recipe) already satisfies the rule;
  it changes the outcome only for a hand-written key that includes a value
  column or a column of unknown constancy (a producer-added column; a
  reference fk on an emit without `history_tracked` flags — § The stable-key
  rule).
- **Reserved names are config errors.** A table named for a bookkeeping table
  is wrong whether or not this invocation drips; the source mode already
  refuses it at every plan build so a one-shot export and a later `--next` on
  the same target agree. Dimensional adopting the same posture removes the
  last static windowed rule, which is what lets `tables()` be the complete
  windowability surface with no new decl field and no seam-side gate.
- **A delta-free ask opens one horizon.** The start horizon exists to
  subtract from; a `snapshot` table subtracts nothing. Opening it anyway costs
  a whole compile (for source, a whole-config plan build with its
  data-dependent guards — half the accepted floor) to produce a relation the
  compile discards, guards whose verdicts the end horizon implies, and a
  duplicate notice emission. The rule is decided at the selection, not per
  table, so an ask mixing a `snapshot` table with a delta table opens both
  horizons and answers the `snapshot` table from the end — projection
  invariance is untouched either way.
- **Source narrowing at the spec level, selection at the horizon.** The
  source mode's data-dependent guards are plan-time and population-scoped,
  and the plan is whole-config by construction (junctions and the event log
  span tables). Threading a selection into the plan would change what it
  validates; the selection reaches the source engine only to choose which
  specs come back and whether the start horizon is opened — the plan's unit
  of validation is untouched.
- **Empty selection refuses.** An ask that names nothing is far more likely a
  caller bug than intent; an empty answer would hide it.

## Boundaries

- **No per-table position.** The seam holds no cursor, edge, or last-flush per
  table; the caller owns all of it.
- **No horizon memoization.** Materializing the truncated tape once per
  horizon and sharing it across asks is a possible later performance lever
  that would add connection state; it is compatible with selection and not
  part of it.
- **No key uniqueness at load.** `KeyColumnsStable` is about stability, not
  uniqueness; a faithful-but-duplicate key still exports one-shot (Principle
  #6), and `WindowKeyDuplicate` remains the one data-level windowed refusal.
- **No per-table query performance work.** The `audit_log` query's cost is a
  source-mode item on its own.
- **No selection on tier 1 or the stream head.** Tier 1 already selects atoms;
  the stream head's per-topic independence is a client-side partition of one
  merged feed by design.
- **No config surface.** Selection is an ask argument; YAML never declares it.
