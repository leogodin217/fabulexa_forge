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
       build_query_specs(…, window) → one QuerySpec per table (dimensional):
         the full-export compile over the truncated tape at end − 1 and start − 1
           snapshot (type-1 dim, mutable filter) → state(end − 1) whole   → replace
           upsert   (a value channel can change)  → state(end−1) ∖ state(start−1) → delete-by-key, insert
           append   (every channel invariant)     → the same delta         → insert
                     │
   ┌─────────────────┴───────────────────┐
   ▼ fmt=duckdb                           ▼ fmt=csv
 warehouse.duckdb (grows in place,      out/ one drop dir per window
   one txn/window)                        w00000_2020-03-01/  dim_*.csv fact_*.csv
   dim_* fact_*                          w00001_2020-03-02/  …
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
| [`exporters/query_spec.py`](../../src/fabulexa_forge/exporters/query_spec.py) | `QuerySpec` (`write_mode` / `upsert_key`) and `write_query_specs` — the mode-neutral compiled-table shape and full-export write dispatch every mode's windowed compile produces and this driver consumes |
| [`exporters/dimensional/engine.py`](../../src/fabulexa_forge/exporters/dimensional/engine.py) | `build_query_specs(…, window)` — the dimensional horizon compile: the full-export compile over the truncated tape at each of the window's two horizons, composed per delivery class |
| [`exporters/dimensional/windowing.py`](../../src/fabulexa_forge/exporters/dimensional/windowing.py) | `window_delivery_class` (the static per-table delivery class), `check_window_key_invariant` (`WindowKeyMutable`), `check_window_key_unique` (`WindowKeyDuplicate`), `check_windowed_reserved_names`, `compose_window_delta_sql` |
| [`derivations/truncated_tape.py`](../../src/fabulexa_forge/derivations/truncated_tape.py) | `open_truncated_tape` / `TruncatedTape` — the emit presented as a producer slice at a horizon ([`derivations.md`](derivations.md) § The truncated-tape surface) |
| [`exporters/source/engine.py`](../../src/fabulexa_forge/exporters/source/engine.py) | `build_source_query_specs(…, window)` — the source windowed compile; see [`source.md`](source.md) § Incremental composition for its per-render window membership |
| [`writers/duckdb.py`](../../src/fabulexa_forge/writers/duckdb.py) | `write_duckdb_window` — one-transaction-per-window append / replace / keyed upsert, bookkeeping tables |
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
  + the windowed write path; it adds no new read surface. The dimensional
  windowed compile is the mode's own full-export compile run over the truncated
  tape (§ Horizon windowing) — the mode never sees a window.
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
compile ([`notices.md`](notices.md)). Every driver invocation derives exactly
one window — an explicit `--from`/`--to` range is a single range-window — and
the sink threads through with no forwarding or dedup logic. A dimensional
window compiles twice, once per horizon, each a full-export compile, so a
plan notice reaches the sink once per horizon compiled (twice per window);
source and base compile once. A `--next` drip re-emits its compile's notices
each invocation; the sequence is deterministic and the repetition is the
contract.

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

### Horizon windowing (dimensional)

A window horizon is a slice end. Window k's content for every dimensional
table is the mode's **unchanged full-export compile over the truncated tape**
(`open_truncated_tape`, [`derivations.md`](derivations.md) § The truncated-tape
surface) at the window's two horizons — the end horizon `h = end_ns − 1` and
the start horizon `h' = start_ns − 1`, both inclusive positions on the one
event-time line the playback seam's `state()` uses. A value emitted at `h` is
honest at `h` by construction, so no window-gated column rule exists. Each
table is delivered by a **class** derived statically from its declaration
(`window_delivery_class`):

| Class | Window k delivers | DuckDB write | CSV drop `<name>.csv` | Which tables |
|---|---|---|---|---|
| `snapshot` | `state(h)` whole | replace the table | the whole relation, every window | type-1 dims; any table whose records `filter` reads a mutable column (its row set can shrink) |
| `upsert` | `state(h) EXCEPT ALL state(h')` — the multiset of rows present at `h` and not at `h'`, reconciled by the declared `key`; a changed row and a new row are both in it, undistinguished | delete every target row whose key equals a delta row's key, then insert the delta | the delta | every other table |
| `append` | the same delta, which for this class contains only new keys | insert the delta | the delta | an `upsert` table every one of whose value channels is horizon-invariant |

| Condition | Result |
|---|---|
| `start_ns = 0` (window 0, or a range from the tape's start) | The start horizon is the empty tape; the delta is `state(h)` whole |
| `end_ns − 1 ≥ slice_at` (the tail window, or a range past the tape) | Truncation at or beyond the slice bound is the identity presentation, so `state(h)` is the full export (under the recorded-trail condition, below) |
| Both horizons past `slice_at` (a trailing empty window) | Every delta is empty; every snapshot equals the full export |
| Explicit `--from` / `--to` range | The same two horizons; a pure rendering with no cursor |

All positions are raw sim-time ns; the calendar regime converts civil
boundaries to physical ns before any horizon is taken.

**Horizon-invariant value channels.** A value channel is horizon-invariant
when its value on a row cannot differ between two horizons at both of which
the row exists. The reading per column form (`windowing._channel_variance`,
read by the classifier and the key gate alike so the two cannot drift):

| Column form | Horizon-invariant iff |
|---|---|
| `from:` / `correlation:` / `value_map.from` / `decimal.from` / `date_parse.from` / `json_precision.from` / `derived: timestamp` `source` | The source column is constant: an identity column, `created_sim_time`, `sim_time`, `joined_sim_time`, `value` / `property` on a history grain, an element field, a `history_tracked: false` property or its `ref_index__` sibling — or, on an `scd: type2` dim, any property (a version row carries its version's value). Not: `active`, `deactivated_at`, `last_mutation_sim_time` (the recorded trail advances), a tracked property on a non-versioned table, `lead_sim_time`, `left_sim_time` |
| `derived: scd_window: valid_from` | Always (a version's start is its identity) |
| `derived: scd_window: valid_to` | Never (the successor closes it) |
| `derived: elapsed` | Never (the counterpart row may land later) |
| `derived: ordinal` | `order_by` resolves to the grain's raw time key under a window-monotone rendering (`created_sim_time` on a records grain, `sim_time` on a history grain, `joined_sim_time` on a membership grain; not a `time` election — the election-aware ordinal amendment, [`dimensional.md`](dimensional.md) § Derived columns) **and** `partition_by` is horizon-invariant. Later rows then never renumber earlier ones |
| `fk via: reference` | Every hop column on the resolved path is `history_tracked: false` (the terminal `record_id` is identity) |
| `fk via: membership` | On a membership grain without `as_of` — the FK is the row's own `member__<f>__id`. Never on a records grain (the binding may join later) and never for the point-in-time `as_of` form (its instant may be an interval end) |
| `lookup` | Always (`LookupColumnSafety` admits only `temporal_class: constant` reads) |
| `null` | Always |

The classifier is conservative by construction: `upsert` is correct for every
table, and `append` is admitted only where the invariance argument is total. A
`role: dim` with no `scd` is classified by its channels like any other table.

**The key.** Two facts about an `upsert` table's `key` are all the mechanism
needs, and both are checked:

| Condition | Result |
|---|---|
| `upsert` table; a `key` column is not a horizon-invariant channel | Refused statically at the windowed compile — `WindowKeyMutable`, naming the table, the column, and the varying source. Without it the drip silently diverges: a row keyed `(A, NULL)` at `h'` re-keys to `(A, 588)` at `h`; delete-by-key removes nothing and no later delta carries `(A, NULL)`, so the open row persists in the warehouse while the full export closes it |
| `upsert` table; `key` unique in `state(h)` | Reconciles: after every window is applied the target equals `state(h)` as a multiset |
| `upsert` table; `key` **not** unique in `state(h)` | Refused at that window's compile before any write — `WindowKeyDuplicate`, naming the table and the number of duplicated key values (delete-by-key would remove a sibling row the delta does not restore). A rendered key (a µs-truncated `derived: timestamp`) collides when two ticks share a microsecond; key on the raw `from: sim_time` instead |
| `append` / `snapshot` table | Neither check runs — an insert-only write reconciles regardless of the key; a replace ignores it |

Uniqueness is evaluated over `state(h)` only: uniqueness at every end horizon
implies uniqueness at every start horizon the drip has used. Key identity —
in the guard's count, the writer's delete (`IS NOT DISTINCT FROM`), and the
`EXCEPT ALL` difference — is DuckDB's distinct semantics: `NULL` equals
`NULL`.

**The two-horizon compile.** Each horizon compile is the shipped full-export
compile run against the truncated emit view (`Emit.with_sidecar` over the
tape's sidecar) with the tape's `base_relations` mapping — one entry per
sidecar base table, so an fk hop, lookup, or elapsed correlation outside the
shape's declared sources truncates too; the mode never sees a horizon. Each
compiled query is wrapped by the name-shadowing realization
([`playback.md`](playback.md) § The compile indirection) and the two are
composed as `end EXCEPT ALL start` over the two wrapped subqueries, projected
under the full export's column list in declared order and **ordered by every
projected column** — a total order over distinct rows that needs no internal
column (byte-identical duplicates, which the contract allows and the multiset
difference preserves, are the only ties and are indistinguishable). A
`snapshot` table's spec is the end-horizon query alone. The delta relation is
schema-identical to the one-shot table.

**The recorded-trail condition.** The truncated tape presents
`last_mutation_sim_time` as the recorded trail, honest at every horizon and
advancing across windows (an `upsert` channel). The full export reads the
physical value. The two agree — and the drained warehouse equals the full
export on an lmst-sourced column — exactly when the producer holds the trail on
every record ([`playback.md`](playback.md) § The recorded trail); the reference
producer does.

| Edge | Result |
|---|---|
| Empty window | Header-only delta drops; snapshot tables re-emitted whole; a zero-row DuckDB transaction logging the window row |
| Author table named `_export_meta` / `_export_windows` | Refused under a windowed invocation (`IncrementalReservedName`) |
| Semantically defective data (a corrupted emit) | Total: the horizon compile is the full-export compile, which already tolerates it; the only data-level refusal is `WindowKeyDuplicate` |
| An emit whose sidecar declares no `history` table | The trail is `greatest(created_sim_time, deactivated_at when ≤ T)`; a tracked property implies a `history` table by contract |

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

The delivery classes and the two-horizon compile are also the playback seam's
tier-2 `window()` contract for a dimensional shape ([`playback.md`](playback.md)
§ Shaped window): `ShapedTableDecl.window_delivery` is the same
`window_delivery_class`, and `window(T1, T2)` runs the same
`build_query_specs` call this driver runs. The driver keeps its own mechanics
(the window-boundary sequence, cursor, fingerprint, drained detection, labels,
staging, writers) above the seam. The source mode's windowed compile keeps its
per-render window membership ([`source.md`](source.md) § Incremental
composition) and the base mode its per-window snapshot ([`base.md`](base.md)
§ Three horizons); re-seaming both over the truncated tape is a separable
later change.

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
compact separators, no NaN/Infinity) of: the parsed `ExportConfig` (model dump, the
documentation-presentation surfaces excluded — `readme_overlay` and the three
per-column description-override surfaces
([`documentation-channel.md`](documentation-channel.md) § The author description
override) — the fingerprint guards data-seam consistency, and those fields provably
never affect data, so improving documentation mid-drip must not halt a drip; the
manifest's embedded config keeps the fields —
[`companion-artifacts.md`](companion-artifacts.md) § The manifest), the
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
  the cursor file, `.tmp_*` staging — never count, and neither do companion artifact
  filenames, `is_companion_artifact_name`: otherwise the window-0 artifacts would make
  every later `--next` read as a lost cursor, and a directory holding only stale
  artifacts could not classify as fresh; the DuckDB boundary is catalog-based and
  unaffected by sibling files). Non-hidden entries with no cursor
  file are lost, with one exception: exactly one non-hidden entry, a directory named
  the **derived window-0 label** (drop renamed, first-ever cursor write lost),
  restarts at window 0 and overwrites that drop. Because the allowed drop must match
  the label derived from the *current* config, even that crash state refuses a
  mid-crash config change.

There is no reset verb: all state lives in the output target, so deleting the warehouse
file or output directory is the reset. A leftover `.tmp_*` staging directory is
discarded at the next staging.

Each emitting invocation — `--next` window, range, or empty window — rewrites the
companion README + manifest whole-state after the window's data and cursor are
committed; a drained invocation writes nothing, artifacts included. Placement,
writing rules, and the accepted last-window staleness wart are owned by
[`companion-artifacts.md`](companion-artifacts.md).

### Window labels and output layout

| Regime | Label |
|---|---|
| Calendar | `w{index:05d}_{civil start date}` — e.g. `w00000_2020-03-01` (the partial first window is labeled by its civil date) |
| Sim-time | `w{index:05d}_ns{start_ns}` |
| Explicit range, calendar | `r_{from}_{to}` — each bound rendered as its civil input: the bare date when midnight, else `YYYY-MM-DDTHHMMSS` (colon-free, filesystem-safe) |
| Explicit range, sim-time | `r_ns{start_ns}_ns{end_ns}` |

Zero-padded indices keep drops sortable; the suffix keeps them human-readable. DuckDB
records the same label in `_export_windows`. Author table names must not collide
with the bookkeeping tables (`IncrementalReservedName`).

### Empty windows

An empty window is **emitted, never skipped**: a CSV drop with header-only change-feed
files (schema survives a no-data day) plus the full snapshot dims; a DuckDB transaction
appending zero rows, replacing snapshot dims, and logging the window row. "Ran, empty"
is distinguishable from "never ran", the drain loop is uniform, and empty-input
handling is itself a downstream exercise worth exercising.

### Explicit ranges (`--from` / `--to`)

A range is a **standalone, stateless** one-shot export of a half-open window — the same
per-class semantics as a drip window (an `upsert` / `append` table's delta between the
range's two horizons, a `snapshot` table whole at its end), but with no cursor read or
written and no bookkeeping tables. An `incremental` block is
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
2. **Horizon honesty.** Every value delivered in window k equals its value in
   `state(end_k − 1)` — the shape's full export over the emit sliced at the
   window's cutoff. No carve-out: a snapshot's row membership is the population
   born by the horizon; an open interval is open; a dim reads as of the cutoff.
   (Dimensional. Source and base keep their own per-render statements.)
3. **Reconciliation (DuckDB).** After each window is applied, every author-named
   dimensional table equals `state(end_k − 1)` as a multiset; after the tape drains,
   every table equals the full export under the table's deterministic `ORDER BY`
   (physical insertion order may differ; the recorded-trail condition on
   lmst-sourced columns).
4. **Concatenation (CSV).** Per class: `append` — the ordered concatenation of a
   table's drops equals the full export as a multiset; `upsert` — the concatenation
   reduced by latest-drop-wins per key equals the full export as a multiset;
   `snapshot` — the last drop equals the full export row-for-row.
5. **Class soundness.** An `append`-classified table's delta never contains a key
   present in the start-horizon state. A violation is a classifier defect, never an
   author error; the acceptance suite (`tests/incremental/test_horizon_acceptance.py`)
   detects it as a reconciliation failure.
6. **Totality.** Every dimensional config that compiles as a full export compiles as a
   window, provided each `upsert` table's `key` is a stable row identity
   (`WindowKeyMutable`, the one static windowed rule); the only data-level refusal is
   `WindowKeyDuplicate`.
7. **Determinism.** Same emit + config + code version → byte-identical CSV drops,
   identical labels and cursor contents, identical warehouse query results (DuckDB file
   bytes excluded, per the repo-wide stance).

**Relied on (upstream guarantees).** The sole branch's `slice_at` bounds all data
`sim_time`s; the run-level `runtime` anchor is never altered by resume/fork; the
truncated tape's honesty and the bridging theorem
([`playback.md`](playback.md) § Shaped state); and **row-set monotonicity under
truncation** — for every table not filtered on a mutable column, the keys present
at an earlier horizon are present at every later one: `history` is append-only, the
records spine (`created_sim_time ≤ T`), the membership intervals (`joined_sim_time
≤ T`), and the SCD-2 version set (change instants ≤ T) are prefix-monotone in T, every
other row-selecting predicate (`source.where`, `source.value`, `fk.where`,
`elapsed.other_where`) reads columns verbatim under truncation, and a membership-FK
fan-out only adds rows. Only a records `filter` can read a mutable column, which is
exactly the `snapshot` condition — this is what makes delete-by-key-then-insert
reproduce `state(h)`. The dependency is on the vendored contract
([`base-format.md`](../../contract/base-format.md)), which this driver reads but does
not redefine.

## Validation Rules

Field shapes are defined by the Pydantic grammar in
[`config/models.py`](../../src/fabulexa_forge/config/models.py); error message text is
owned by [`exporters/dimensional/validation.py`](../../src/fabulexa_forge/exporters/dimensional/validation.py)
and [`tests/incremental/`](../../tests/incremental/). The rules below state *what* is
rejected and *when*.

**Parse-time (Pydantic).** `IncrementalConfig` sets **exactly one** of `period` /
`sim_period_ns`; `sim_period_ns`, when set, is ≥ 1 (`exactly_one_cadence`).

**Windowed business rules (dimensional).** These run **only when
`build_query_specs` receives a window** — a full export is untouched — and live in
[`windowing.py`](../../src/fabulexa_forge/exporters/dimensional/windowing.py).
Every always-on rule of the full export applies to each horizon compile at the
same point it applies today; no other windowed refusal exists.

| Rule | Rejects | Error message |
|---|---|---|
| `WindowKeyMutable` | Static, `upsert` tables only: a `key` column whose value channel is not horizon-invariant (§ Horizon windowing) | `"table '{table}' key column '{column}': its value can change between windows ({source}); an upsert-delivered table reconciles by key, so key on columns that identify the row for the whole run"` |
| `WindowKeyDuplicate` | Against the data, `upsert` tables only: the declared `key` is not unique in the end-horizon relation | `"table '{table}': key {key} is not unique at the window horizon ({n} duplicated key values); an upsert-delivered table reconciles by key, so declare a key that identifies one row"` |
| `IncrementalReservedName` | An author table named `_export_meta` / `_export_windows` | `"table '{table}': name '{table}' is reserved under incremental export"` |

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
- **A window horizon is a slice end.** The alternative — selecting rows out of the
  un-truncated full export by a per-grain window key — has to refuse every column form
  through which a value could depend on data past the window, an open-ended rule set
  (ten at its peak, each rejecting a config that exports fine one-shot), plus a type-1
  snapshot whose population was the end of the run and an SCD-2 regime that needed a
  physical `__rows` table, a bookkeeping column, and a view to stay append-only.
  Compiling the unchanged full export over the truncated tape makes every value honest
  at its horizon by construction, so drip ≡ one-shot rests on the bridging theorem plus
  reconciliation by key, and the only rule left is that an `upsert` table's key be a
  stable row identity. A horizon compile costs a fraction of a second on a
  half-million-row bundle; the design was established empirically over every
  dimensional recipe before it was built, and the acceptance suite that drove it
  (drip-then-`compare` against the one-shot export; window k against a test-side
  producer slice at the cutoff) is the regression gate.
- **`upsert` by default, `append` only where total.** Delete-by-key-then-insert is
  correct for every table with a stable unique key; insert-only is an optimization
  admitted only where every channel is provably invariant, because a wrong `append`
  leaves a stale row the reconciliation test catches but a consumer would not.
- **The ordinal amendment is regime-uniform.** An `ordinal.order_by` naming a
  rendered-time column orders by its raw-ns source in **both** full and windowed export
  (see [`dimensional.md`](dimensional.md) § Derived columns); it is what lets an
  ordinal over the grain's raw time key count as horizon-invariant.
- **Period boundaries resolve narrowly; author input fails fast.** A `base_date` or a
  range bound is author input interpreted narrowly — a DST gap/fold is rejected. Period
  boundaries are derived calendar structure that must always exist, so they resolve to
  the earliest valid instant at or after the civil time. "The period starts at the
  earliest instant of its first civil moment" is a canonical reading, not an invented
  value.
- **A range never appends.** All drip state lives in the output target; a range is
  stateless by definition, so it writes its own fresh target rather than splicing into
  drip state. Re-driving one past window is a range to a fresh path.

## Boundaries

- **A fanning membership FK on a records grain has no stable row identity.** A
  records-grain fact whose `fk via: membership` `where` selects more than one binding
  fans one record into several rows; keyed on the record it fails `WindowKeyDuplicate`,
  keyed on the FK it fails `WindowKeyMutable` (the binding is not invariant on a
  records grain). Declare it as a membership-grain fact — the binding *is* the row
  there, keyed `(record_id, joined_sim_time, …)`.
- **Source and base keep their shipped windowed compiles.** Both are already total,
  so re-seaming them over the truncated tape is deferred; the seam's `window()` for a
  source shape keeps its shipped dispatch.
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
| [`playback.md`](playback.md) | The seam whose tier-2 `window()` runs this driver's horizon compile and whose `state()` defines the horizons it reconciles to |
| [`anchor.md`](anchor.md) | The single `EffectiveAnchor` calendar windows resolve through |
| [`temporal-elections.md`](temporal-elections.md) | The election vocabulary the ordinal invariance reading is election-aware over |
| [`declared-keys.md`](declared-keys.md) | The `declare_keys` capability and its per-write-regime window gating |
| [`companion-artifacts.md`](companion-artifacts.md) | The README + manifest pair each emitting invocation rewrites whole-state; the census and fingerprint exclusions it motivates |
| [`reader.md`](reader.md) | The `Emit` / `Sidecar` surface the driver reads through |
| [`derivations.md`](derivations.md) | The truncated tape every horizon compile runs over |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The vendored contract carrying the relied-on `slice_at` and row-order guarantees |
