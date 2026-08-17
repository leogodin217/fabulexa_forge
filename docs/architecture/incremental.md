# Incremental Export

**Status:** Implemented. Code is the contract — see
[`incremental/`](../../src/fabulexa_forge/incremental/),
[`exporters/dimensional/engine.py`](../../src/fabulexa_forge/exporters/dimensional/engine.py),
[`writers/duckdb.py`](../../src/fabulexa_forge/writers/duckdb.py), and
[`tests/incremental/`](../../tests/incremental/). Public API:
[`incremental/driver.py`](../../src/fabulexa_forge/incremental/driver.py).

A cross-mode driver that exports a run **a window at a time** — one calendar period or
one sim-time interval per invocation — instead of the whole run in one shot. It wraps
a mode's pure range export: `--from`/`--to` is a stateless one-shot range; `--next`
reads a cursor, derives the next window, runs the same range export, and advances the
cursor. Every window is a pure function of `(emit, config, code version, range)`; the
cursor is bookkeeping, never semantics. It serves the package's incremental-ETL,
recurring-report, and landing-zone teaching targets — data that arrives period by
period. The compile step is mode-dispatched: `mode: dimensional` compiles through
`exporters/dimensional/engine.py`, `mode: source` through
`exporters/source/engine.py` (see [`source.md`](source.md) § Incremental
composition), and `mode: base` through `exporters/base/engine.py` — every base
table snapshot-delivered at the window's `end_ns` with `write_mode='replace'`,
reusing this driver's window math, cursor, and fingerprint with no new window
derivation ([`base.md`](base.md) § Three horizons). All three compile to the
shared, mode-neutral `QuerySpec` (`exporters/query_spec.py`), so this driver's
window math, cursor, fingerprint, drained detection, labels, and staging apply
identically across the modes.

```
emit (run.duckdb + base.json @ the supported `base_format_version`)
   │  (reader: Emit + Sidecar; trunk-only — sole branch)
   │  anchor: the one EffectiveAnchor cmd_export resolves (see anchor.md)
   ▼
 fabulexa-forge export … --next         fabulexa-forge export … --from V --to V
   │  read cursor → derive next       │  parse range (no cursor)
   ▼  window → range export           ▼  window → range export
       build_query_specs(…, window) → one QuerySpec per table
         fact (records | history_point) → append rows with key ∈ window
         dim  scd: type2                → append version rows (no valid_to) + view
         dim  scd: type1                → full snapshot (replace / re-emit per drop)
                     │
   ┌─────────────────┴───────────────────┐
   ▼ fmt=duckdb                           ▼ fmt=csv
 warehouse.duckdb (grows in place,      out/ one drop dir per window
   one txn/window)                        w00000_2020-03-01/  dim_*.csv fact_*.csv
   dim_* fact_* + SCD-2 views            w00001_2020-03-02/  …
   _export_meta _export_windows          .fabulexa-forge-cursor.json
   (cursor atomic with data)            (cursor sidecar; re-run overwrites a drop)
```

---

## Surface

| Module | Owns |
|---|---|
| [`incremental/windows.py`](../../src/fabulexa_forge/incremental/windows.py) | `Window`, `derive_window`, `parse_range` — pure window math (calendar boundaries through the anchor, or sim-time arithmetic) and range parsing |
| [`incremental/driver.py`](../../src/fabulexa_forge/incremental/driver.py) | `export_incremental_next`, `export_window`, `IncrementalOutcome` — cursor read/advance, drained detection, drop staging, range orchestration |
| [`incremental/cursor.py`](../../src/fabulexa_forge/incremental/cursor.py) | `Cursor`, `read_cursor`, `write_csv_cursor` — the cursor of record per `fmt` and the fresh/lost classification |
| [`incremental/fingerprint.py`](../../src/fabulexa_forge/incremental/fingerprint.py) | `compute_fingerprint` — the SHA-256 drip-identity digest |
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `IncrementalConfig` — the cross-mode `incremental` cadence block (sibling of `mode` and `rebase`) and its parse-time validator |
| [`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py) | `QuerySpec` (`write_mode` / `view_name` / `view_sql`) and `write_query_specs` — the mode-neutral compiled-table shape and full-export write dispatch every mode's windowed compile produces and this driver consumes |
| [`exporters/dimensional/engine.py`](../../src/fabulexa_forge/exporters/dimensional/engine.py) | `build_query_specs(…, window)` — the dimensional windowed compile |
| [`exporters/dimensional/validation.py`](../../src/fabulexa_forge/exporters/dimensional/validation.py) | The ten window-gated business rules, run only when a `window` is present |
| [`exporters/source/engine.py`](../../src/fabulexa_forge/exporters/source/engine.py) | `build_source_query_specs(…, window)` — the source windowed compile; see [`source.md`](source.md) § Incremental composition for its per-render window membership |
| [`writers/duckdb.py`](../../src/fabulexa_forge/writers/duckdb.py) | `write_duckdb_window` — one-transaction-per-window append/replace, view installs, bookkeeping tables |
| [`errors.py`](../../src/fabulexa_forge/errors.py) | `IncrementalError` and its subclasses (config, regime, fingerprint, cursor, range) |

## Boundary

- **Input.** An open `Emit` (trunk-only — sole branch), a validated `ExportConfig`
  with `mode: dimensional` or `mode: source`, the resolved `EffectiveAnchor` (or
  `None` — dimensional tolerates it, source does not, see [`source.md`](source.md)
  § Wallclock timestamps), the `fmt`, and the invocation mode (`--next` or an
  explicit range). The `incremental` cadence block is required for `--next`; an
  explicit range does not need it.
- **Output.** Per `fmt`: a single `.duckdb` warehouse file that **grows in place** (one
  transaction per window, data plus the `_export_meta` / `_export_windows` cursor
  committed together), or a directory holding **one drop sub-directory per window**
  plus an `out/.fabulexa-forge-cursor.json` sidecar. An explicit range writes a standalone
  artifact with no bookkeeping tables.
- **Wraps the pure range export.** The driver computes a window and calls the
  active mode's windowed compile (`build_query_specs` or `build_source_query_specs`)
  + the windowed write path; it adds no new read surface and recomputes no emit
  value.
- **Anchor, consumed.** Calendar windows resolve through the single `EffectiveAnchor`
  the invocation already resolves (see [`anchor.md`](anchor.md)); the driver adds no
  second origin/zone precedence chain.
- **Reader-first.** Every table and column fact flows from the `Sidecar`; the driver
  opens `run.duckdb` only through `Emit`, like every other subsystem.
- **Forbidden imports.** No dependency on the bundle's producer; the vendored
  `contract/` is the only coupling.

## Semantics

### Notice threading

`export_window` and `export_incremental_next` take the same required
`notice_sink` as the full-export entry points and thread it to the mode's
compile ([`notices.md`](notices.md)). Every driver invocation compiles exactly
once — an explicit `--from`/`--to` range is a single range-window, and a
`--next` drip derives one window — so the sink threads through with no
forwarding or dedup logic; a `--next` drip re-emits its compile's notices each
invocation. The window-gated rules themselves never consult `temporal_class`:
`slice_only` reads are refused always-on before any gate runs
([`slice-only.md`](slice-only.md)), so every `history_tracked: false` column
that survives to a window gate is either `constant` or the exempt
discriminator — whose admission is the carve-out working as intended.

### Two regimes, one window sequence

A run drips in exactly one of two regimes, selected by the cadence block against
anchor presence. Window `k` is half-open in sim-time ns.

| Condition | Regime | Window `k` |
|---|---|---|
| `incremental.period` set and an `EffectiveAnchor` resolves | Calendar | `[B_k − start_instant, B_{k+1} − start_instant)` as physical ns, where `B_0 = anchor.start_instant` and `B_1, B_2, …` are successive calendar-period boundaries in `anchor.timezone` |
| `incremental.sim_period_ns` set and no anchor resolves | Sim-time | `[k·P, (k+1)·P)` ns |
| `period` set, no anchor resolves | Error `IncrementalAnchorRequired` |
| `sim_period_ns` set, an anchor resolves | Error `IncrementalPeriodRegimeMismatch` |

Calendar boundaries are civil times in `anchor.timezone`: day → midnight; week →
midnight Monday (ISO-8601); month → midnight on the 1st. `B_1` is the first boundary
**strictly after** `start_instant`, so window 0 is partial when the anchor starts
mid-period (an 08:00 anchor gives an `[08:00, midnight)` day-0 window) and full when
the anchor sits on a boundary. Window widths are physical durations between boundary
instants — a DST-crossing day is 23 or 25 hours of physical ns, faithfully.

A civil boundary that is nonexistent (DST gap) or ambiguous (fold) resolves to the
**earliest valid instant at or after the civil time** (`fold=0`; a gap shifts the
boundary to the gap's end). Period boundaries are derived calendar structure that must
always resolve, so they resolve narrowly rather than fail — distinct from author input
(`base_date`, range bounds), which fails fast on a gap/fold (§ Rationale).

All window-membership tests run on **raw sim-time ns**, never on rendered timestamps,
so DST cannot perturb membership.

### Window membership per table class

The window predicate is applied as the **outermost filter over the full-export
relation** — after window functions, derived columns, and FK resolution. Every value
on an emitted row is therefore its full-export value (ordinals count the full-run
partition; value maps, FKs, and timestamps carry their full-export values); the window
selects rows, never recomputes them. The window key is grain-definitional (the grain's event time), not
configuration — no author-facing window-key knob exists.

| Table class | Window key (ns) | Behavior per window |
|---|---|---|
| Fact, `records` grain | `last_mutation_sim_time` | Append rows with key ∈ window. The row lands when its content stops changing — exactly creation time for write-once kinds — so an appended row is final, never revised |
| Fact, `history_point` grain | `sim_time` | Append rows with key ∈ window |
| Dim, `scd: type2` | the version's `valid_from` change point | Append version rows born in the window, **without** any `scd_window: valid_to` column; the view supplies `valid_to` |
| Dim, `scd: type1` | — (snapshot class) | Full current-state table every window: `replace` in DuckDB, re-emitted in every CSV drop. Columns are gated temporally constant, so every **value** is horizon-exact at every window; the **row set** is the end-of-run population (carve-out below) |
| Fact or dim, `history_interval` / `membership` grain | — | Rejected: `IncrementalGrainUnsupported` |

Membership is half-open (`start_ns <= key < end_ns`): a key exactly on a boundary
belongs to the later window.

Selecting-not-recomputing is temporally honest only if no full-export value derives
from data past the row's window. The windowed business rules (§ Validation Rules)
restrict the config until that holds — `fk` paths traverse only immutable hops,
`ordinal.order_by` resolves to the raw window key, slice-read columns are temporally
constant, dim `filter` predicates read only constant discriminators. Invariant 4 then
holds for every emitted **value**, with one carve-out.

**Election-aware window-key membership.** A column whose declared source is
the window's raw-ns column counts as a window key only if its rendering is
also window-monotone. `date` / `timestamptz` elections (and the unelected
default) remain monotone in the window's raw-ns source and satisfy the rule
exactly as `timestamp` does today; a `time`-elected column is excluded from
the window-key set — time-of-day is not monotone in the window — so an
append-mode `ordinal.order_by` naming a `time`-elected column is refused
([`temporal-elections.md`](temporal-elections.md) § Per-mode attach points,
[`dimensional.md`](dimensional.md) § Derived columns for the amendment this
rule composes with).

**The type-1 snapshot row-membership carve-out.** A type-1 snapshot's row set is the
end-of-run population, so a record first created in window 50 appears in window 0's
snapshot. Filtering rows to "born by `end_k`" is unsound from the slice alone:
`last_mutation_sim_time` stops being the creation time the moment any property mutates,
and creation time is otherwise opt-in provenance an emit may not carry. The full
snapshot is therefore the deliberate choice — every column value is still horizon-exact
(the gate admits only temporally constant sources), so the snapshot is wider than a
real nightly extract, never wrong, and FK-safe at every horizon.

Under `declare_keys` (base and source), the windowed compile resolves declared
keys exactly as the full export does and sets them on each window's `QuerySpec`;
the windowed DuckDB writer applies them at first-window table creation only,
where the write regime preserves the constraint across windows — replace-class
tables trivially, append-class tables only where a row lands in exactly one
window and is final. A false claim surfaces as a rolled-back window under
the writer's transaction rule, and `keys-not-declarable-csv` re-emits per driver
invocation like any compile notice. The per-regime table and rationale are
[`declared-keys.md`](declared-keys.md) § Incremental interplay; `declare_keys`
participates in the config fingerprint exactly as any other config field does.

This per-table-class window-membership contract — the window keys, the per-class
behavior, the type-1 row-membership carve-out, and the windowed-grain rejection —
is also the playback seam's tier-2 `window` contract, promoted verbatim from
driver-internal to seam-owned ([`playback.md`](playback.md) § Shaped window). The
driver keeps its own mechanics (the window-boundary sequence, cursor, fingerprint,
drained detection, labels, staging, writers); those remain above the seam, and the
driver becomes tier 2's first re-seam customer when it is next materially touched.

### The SCD-2 view

`valid_to` is redundant: version N's `valid_to` **is** version N+1's `valid_from`.
Incremental output never materializes it; the information arrives implicitly inside the
successor row and is recovered at read time by a view.

| Condition | Result |
|---|---|
| SCD-2 dim declares ≥ 1 `scd_window: valid_to` column | Physical table `<name>__rows` holds all declared columns **except** the `valid_to` slots, in declared order, plus a trailing bookkeeping column `__valid_from_ns` (the version's raw sim-time change point, ns); view `<name>` projects the declared column list — not `__valid_from_ns` — with each `valid_to` slot computed as `LEAD(<valid_from column>) OVER (PARTITION BY <identity columns> ORDER BY __valid_from_ns)` |
| SCD-2 dim declares no `valid_to` column | Plain table `<name>`, `append` mode, no view |
| Identity columns | The table's `key` entries minus its `scd_window` columns, in key order. Non-empty (`IncrementalScd2IdentityKey`) |
| `valid_from` multiplicity | A table declaring a `valid_to` column declares **exactly one** `scd_window: valid_from` column (`IncrementalScd2ValidFromUnique`) — the view's `LEAD` source is unambiguous |
| Mid-drip currentness | The latest loaded version of each entity has `LEAD = NULL` → `valid_to IS NULL` finds the current row at every horizon |
| A later window appends the successor version | The view closes the prior version automatically — append-only physical, always-consistent logical |
| CSV drops | `<name>.csv` carries the `__rows` projection (declared columns minus `valid_to` slots, plus the trailing `__valid_from_ns`). Closing versions downstream is the consumer's merge job, deterministic because two versions inside one rendered microsecond still order by the raw key |

The `LEAD` is **ordered by `__valid_from_ns`, the raw ns change point — never by the
rendered `valid_from`**. Rendered timestamps truncate to microseconds
([`dimensional.md`](dimensional.md) § Timestamp source and the runtime anchor), so two
versions of one record inside the same microsecond render to equal `valid_from` values
and a rendered-order `LEAD` would be nondeterministic. Version boundaries are distinct
change `sim_time`s per record, so `(identity, __valid_from_ns)` is unique and the view
is total. The *projected* `valid_to` value is still the successor's rendered
`valid_from`, equal to full export's `valid_to` by construction; no anchor logic is
needed in the view.

### Drained detection and the cursor

Run end is the sole branch's `slice_at`, which bounds every data `sim_time`.

| Condition | Result |
|---|---|
| Next window's `start_ns <= slice_at` | Window is emittable (it may still be empty) |
| Next window's `start_ns > slice_at` | **Drained** — nothing written, cursor untouched, exit code 3 with a `drained` message |
| Window contains `slice_at` | Emitted normally; the tail window is sparse, never clipped |

A slice pinned past the data simply yields trailing empty-but-emittable windows before
draining (§ Empty windows). The cursor is
`{cursor_format_version, fingerprint, next_window_index}`; `cursor_format_version`
starts at 1, and the package version lives inside the fingerprint, never as a cursor
field.

| `fmt` | Cursor of record | Atomicity |
|---|---|---|
| `duckdb` | `_export_meta` (`cursor_format_version`, `fingerprint`) + `_export_windows` (one row per emitted window: `window_index`, `label`, `start_ns`, `end_ns`); next index = `max(window_index) + 1` | Committed in the **same transaction** as the window's data — cursor/data drift is impossible |
| `csv` | `out/.fabulexa-forge-cursor.json` — keys are exactly the `Cursor` field names | Window staged in `out/.tmp_<label>`, atomically renamed to `out/<label>`, then the cursor is written. A crash between rename and cursor write re-derives the same window and overwrites the identical drop — idempotent |

The **fingerprint** is a SHA-256 over a canonical JSON document (UTF-8, sorted keys,
compact separators, no NaN/Infinity) of: the parsed `ExportConfig` (model dump), the
resolved anchor (`start_instant` ISO + IANA key, or null), the SHA-256 of `base.json`'s
bytes, the sole branch's `fork_path`, the `fmt`, and the package version. `--next`
recomputes it and refuses on mismatch (`IncrementalFingerprintMismatch`): changed
config, changed rebase flags, a different emit, or a code upgrade mid-drip all halt
rather than splice an inconsistent seam.

A cursor that is unreadable, structurally invalid, or **lost** is
`IncrementalCursorInvalid`. The fresh/lost boundary is exact:

- **DuckDB** — fresh when the file is absent or its catalog is empty (zero tables and
  views — the only legitimate empty state, a rolled-back window 0). Any non-empty
  catalog missing `_export_meta` is lost.
- **CSV** — fresh when `out` is absent or holds no non-hidden entries (dot-entries —
  the cursor file, `.tmp_*` staging — never count). Non-hidden entries with no cursor
  file are lost, with one exception: exactly one non-hidden entry, a directory named
  the **derived window-0 label** (drop renamed, first-ever cursor write lost),
  restarts at window 0 and overwrites that drop. Because the allowed drop must match
  the label derived from the *current* config, even that crash state refuses a
  mid-crash config change.

There is no reset verb: all state lives in the output target, so deleting the warehouse
file or output directory is the reset. A leftover `.tmp_*` staging directory is
discarded at the next staging.

### Window labels and output layout

| Regime | Label |
|---|---|
| Calendar | `w{index:05d}_{civil start date}` — e.g. `w00000_2020-03-01` (the partial first window is labeled by its civil date) |
| Sim-time | `w{index:05d}_ns{start_ns}` |
| Explicit range, calendar | `r_{from}_{to}` — each bound rendered as its civil input: the bare date when midnight, else `YYYY-MM-DDTHHMMSS` (colon-free, filesystem-safe) |
| Explicit range, sim-time | `r_ns{start_ns}_ns{end_ns}` |

Zero-padded indices keep drops sortable; the suffix keeps them human-readable. DuckDB
records the same label in `_export_windows`. Author table names must not end in
`__rows` or collide with the bookkeeping tables, and no author column may be named
`__valid_from_ns` (`IncrementalReservedName`).

### Empty windows

An empty window is **emitted, never skipped**: a CSV drop with header-only change-feed
files (schema survives a no-data day) plus the full snapshot dims; a DuckDB transaction
appending zero rows, replacing snapshot dims, and logging the window row. "Ran, empty"
is distinguishable from "never ran", the drain loop is uniform, and empty-input
handling is itself a downstream exercise worth exercising.

### Explicit ranges (`--from` / `--to`)

A range is a **standalone, stateless** one-shot export of a half-open window — the same
per-class semantics and `__rows` + view shape as a drip window, snapshot dims included,
but with no cursor read or written and no bookkeeping tables. An `incremental` block is
not required (cadence is only for `--next`).

| Condition | Result |
|---|---|
| Anchor resolves | `--from`/`--to` are naive civil datetimes (a bare date is midnight) localized in `anchor.timezone`, each converted to a physical-ns offset from `anchor.start_instant`; a DST-gap or fold value is an error (author input → fail-fast, matching `base_date`) |
| No anchor resolves | `--from`/`--to` are integer sim-time ns |
| Form does not match the regime, or `from >= to` | `IncrementalRangeInvalid` |
| `out` already exists | `IncrementalRangeTargetExists` — a range never appends into or overwrites an existing target; deleting it is the re-run |
| `--next` together with `--from`/`--to` | Usage error |

A bound before the anchor localizes to a negative offset and is **legal**: sim time
starts at 0, so a fully pre-anchor range selects nothing and yields an empty artifact
(§ Empty windows); a straddling range is meaningful. Fail-fast is reserved for
ill-formed input (gap, fold, `from >= to`), never for well-defined-but-empty ranges. A
range never appends into an incremental warehouse; re-driving one past window of a drip
is `--from`/`--to` to a fresh target. A range artifact carries no `_export_meta` /
`_export_windows`, so pointing `--next` at a range-produced target fails as
`IncrementalCursorInvalid` rather than silently extending it. A CSV range is staged at
a sibling `<out parent>/.tmp_<label>` and atomically renamed to `out`.

## Invariants

1. **Window purity.** Each window's content is a pure function of `(emit, config, code
   version, range)`. The cursor only chooses *which* range runs next.
2. **Drip ≡ one-shot (DuckDB).** After draining, every author-named relation in the
   incremental warehouse returns rows identical to the full-export warehouse's same
   relation under the table's deterministic `ORDER BY` (physical insertion order may
   differ; a view is indistinguishable from a table at the SELECT surface — the drained
   view's open `valid_to` is exactly the full export's `NULL`).
3. **Concatenation (CSV).** Ordered concatenation of a table's drop files equals the
   full export of that relation **as a multiset of rows** — exactly for point-fact
   tables; as the `__rows` projection (no `valid_to` slots, trailing `__valid_from_ns`)
   for SCD-2 dims. Concatenation is window-major while the full export uses its own
   `ORDER BY`, so equality holds after re-sorting both sides by that `ORDER BY`, not
   row-for-row as written. Each drop's snapshot-dim copy equals the full-export table
   row-for-row.
4. **No forward references within the drip frame, modulo the type-1 row-membership
   carve-out.** Every **value** emitted in window k derives from `sim_time < end_k` or
   from temporally constant state: `valid_to` is unmaterialized; `last_mutation` keys
   land records-grain rows only once final; ordinals order by the raw-ns window key;
   `fk` paths traverse only immutable hops; slice-read and dim-`filter` columns are
   temporally constant. The carve-out is row membership of type-1 snapshots — their
   rows for later-born entities carry only constant, hence horizon-exact, values
   (§ Window membership).
5. **Determinism.** Same emit + config + code version → byte-identical CSV drops,
   identical labels and cursor contents, identical warehouse query results (DuckDB file
   bytes excluded, per the repo-wide stance).

**Relied on (upstream guarantees).** The sole branch's `slice_at` bounds all data
`sim_time`s; SCD-2 version boundaries are distinct per record; a record's first version
`valid_from` is ≤ any fact referencing it (upstream causal consistency — what makes
append-by-`valid_from` FK-safe at every horizon); the run-level `runtime` anchor is
never altered by resume/fork. Records-grain windowing additionally relies on
`last_mutation_sim_time` bounding **every** content change to its record, deactivation
included (an `active` / `deactivated_at` flip bumps it) — this is
what makes a records-grain row final at landing and lets those facts project
deactivation columns ungated. The producer upholds this; the dependency is on the
vendored contract ([`base-format.md`](../../contract/base-format.md)), which this driver
reads but does not redefine.

## Validation Rules

Field shapes are defined by the Pydantic grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py); error message text is
owned by [`exporters/dimensional/validation.py`](../../src/fabulexa_forge/exporters/dimensional/validation.py)
and [`tests/incremental/`](../../tests/incremental/). The rules below state *what* is
rejected and *when*.

**Parse-time (Pydantic).** `IncrementalConfig` sets **exactly one** of `period` /
`sim_period_ns`; `sim_period_ns`, when set, is ≥ 1 (`exactly_one_cadence`).

**Window-gated business rules.** These run in the existing business-rule pass **only
when `build_query_specs` receives a window** — a full export is untouched. Each rejects
a config that would let a window's value derive from data past the window. Several
gates require the emit to carry `history_tracked` and refuse outright when it does not,
because constancy is otherwise unverifiable (the same stance as `LookupColumnSafety`).

| Rule | Rejects |
|---|---|
| `IncrementalGrainUnsupported` | Any `history_interval` or `membership` grain (an interval is two point events; model journeys as `history_point` facts) |
| `IncrementalElapsedUnsupported` | Any `derived: elapsed` column (its counterpart row may postdate the window) |
| `IncrementalFkMembershipUnsupported` | Any `fk` with `via: membership` (a binding is interval data — the bound member may join after the window) |
| `IncrementalFkMutableHop` | An `fk via: reference` path with a hop column not `history_tracked: false` (a mutable hop would stamp a re-pointed key into a past window); the terminal `record_id` is identity, always constant |
| `IncrementalOrdinalOrderBy` | On an append-mode table, an `ordinal.order_by` that does not resolve to the table's raw-ns window key **under a window-monotone rendering** (a rendered-µs ordering would let same-microsecond ties straddle a boundary; a `time` election is never window-monotone regardless of source — the election-aware window-key rule, § Window membership per table class). Snapshot-class tables are exempt — their inputs are gated constant |
| `IncrementalSliceColumnMutable` | A slice-read column — any column of a `scd: type1` dim, every *static* column of a `scd: type2` dim — reading a mutable source: a structural column the reader's structural-temporal surface marks mutable ([`reader.md`](reader.md) § The structural-temporal surface — `active`, `deactivated_at`, `last_mutation_sim_time`), or a `history_tracked: true` property. Records-grain facts are exempt: keyed on `last_mutation_sim_time`, their content is final at landing |
| `IncrementalFilterColumnMutable` | A dim `filter` discriminator that is not `history_tracked: false` (a mutable discriminator makes window-k membership derive from a future reclassification, outside the carve-out) |
| `IncrementalScd2IdentityKey` | A `scd: type2` `key` with no non-`scd_window` column (the view's partition identity) |
| `IncrementalScd2ValidFromUnique` | A `scd: type2` table declaring a `valid_to` column without exactly one `scd_window: valid_from` column (the view's `LEAD` source) |
| `IncrementalReservedName` | An author table name ending in `__rows` or equal to `_export_meta` / `_export_windows`, or an author column named `__valid_from_ns` |

**Invocation rules (driver).** Regime match (`period` ⇒ anchor resolved;
`sim_period_ns` ⇒ no anchor); fingerprint stored == computed before any window is
derived; cursor parses with a known `cursor_format_version`; cursor not lost (per the
fresh/lost boundary above); range bounds both present, parseable in the active regime,
`from < to`; range target does not already exist; `--next` xor `--from`/`--to` (a
usage error on stderr, exit 1, before the emit opens).

## Rationale

- **The cursor is bookkeeping, not semantics.** A window is a pure function of its
  range, so the cursor only selects which range runs next. This is what lets DuckDB
  commit the cursor inside the data transaction and lets CSV re-derive an identical
  window after a crash — cursor/data drift cannot produce a wrong window, only a
  repeated one.
- **`valid_to` is never materialized.** It is exactly the successor version's
  `valid_from`; materializing it into an appended row would require either a future
  value (a forward reference) or a correction row when the successor lands. The view
  recovers it from the successor row, so the physical feed is literal append-only and
  the logical table is always consistent at every horizon.
- **The view's `LEAD` orders by the raw ns key, not the rendered `valid_from`.**
  Rendered timestamps truncate to microseconds, so two versions inside one microsecond
  would tie at the rendered value and make a rendered-order `LEAD` nondeterministic.
  `__valid_from_ns` carries the untruncated key so `(identity, __valid_from_ns)` is
  total. This is the same doctrine the dimensional exporter already states for row
  ordering — pinned by `sim_time`, never by the rendered timestamp.
- **The ordinal amendment is regime-uniform.** An `ordinal.order_by` naming a
  rendered-time column orders by its raw-ns source in **both** full and windowed export
  (see [`dimensional.md`](dimensional.md) § Derived columns). An incremental-only
  rewrite would break Invariant 2 on exactly the same-microsecond tie pair; making the
  rule uniform changes full-export output only where raw order *is* the event order.
- **Period boundaries resolve narrowly; author input fails fast.** A `base_date` or a
  range bound is author input interpreted narrowly — a DST gap/fold is rejected. Period
  boundaries are derived calendar structure that must always exist, so they resolve to
  the earliest valid instant at or after the civil time. "The period starts at the
  earliest instant of its first civil moment" is a canonical reading, not an invented
  value.
- **The type-1 snapshot is the end-of-run population.** Filtering its rows to
  "born by `end_k`" is unsound from the slice alone — `last_mutation_sim_time` is not
  creation time once a property mutates, and creation time is opt-in provenance an emit
  may lack. Every snapshot value is constant and horizon-exact, so the full population
  is wider than a real nightly extract but never wrong, and FK-safe at every horizon
  (the one carve-out in Invariant 4).
- **A range never appends.** All drip state lives in the output target; a range is
  stateless by definition, so it writes its own fresh target rather than splicing into
  drip state. Re-driving one past window is a range to a fresh path.

## Boundaries

- **Interval grains are not windowable.** `history_interval` and `membership` grains
  are an interval = two point events; their second event may postdate the window. They
  are rejected, not deferred — authors model journeys as `history_point` facts and
  derive intervals downstream (which is the pedagogical point).
- **`derived: elapsed` and membership/mutable FK edges are not windowable** for the
  same forward-reference reason (the gates above).
- **Trunk-only.** The `SingleBranch` guard stands; the fingerprint includes the sole
  branch's `fork_path`, so a branch-aware cursor (Stage 5) extends the key rather than
  reworking it.
- **CSV + DuckDB only.** Parquet is a later writer.
- **No reset verb.** The output target *is* the state; deleting it is the reset.
- **`init` proposes no cadence.** It proposes the dimensional config; the `incremental`
  block is authored by hand (Principle #7 — cadence is never defaulted).

## Related

| Document | Why |
|---|---|
| [`dimensional.md`](dimensional.md) | One mode the driver wraps — grain semantics, SCD-2 `LEAD`, derived columns (incl. the ordinal amendment), the timestamp anchor |
| [`source.md`](source.md) | The other mode the driver wraps — per-render window membership: the windowed state snapshot, the appended event log, junction extract-on-change |
| [`playback.md`](playback.md) | The seam that promotes this driver's per-table-class window-membership rules to its tier-2 `window` contract |
| [`anchor.md`](anchor.md) | The single `EffectiveAnchor` calendar windows resolve through |
| [`temporal-elections.md`](temporal-elections.md) | The election vocabulary the append-mode window-key rule is election-aware over |
| [`declared-keys.md`](declared-keys.md) | The `declare_keys` capability and its per-write-regime window gating |
| [`reader.md`](reader.md) | The `Emit` / `Sidecar` surface the driver reads through |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The vendored contract carrying the relied-on `last_mutation_sim_time` / `slice_at` guarantees |
