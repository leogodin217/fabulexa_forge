# Derivations Layer

**Status:** Implemented. Code is the contract — see
[`derivations/`](../../src/fabulexa_forge/derivations/)
(`versioned_intervals.py`, `reference_resolution.py`, `row_state_events.py`,
`membership_events.py`, `state_at.py`, `membership_state_at.py`,
`record_index.py`, `presentation_key.py`, `truncated_tape.py`, `guard.py`),
[`tests/derivations/`](../../tests/derivations/).
Public API: `VERSIONED_INTERVAL_COLUMNS`,
`build_versioned_intervals_sql`, `REFERENCE_RESOLUTION_COLUMNS`,
`build_reference_path_sql`, `build_membership_edge_sql`, `ROW_STATE_EVENT_COLUMNS`,
`build_row_state_events_sql`, `EVENT_CLASS_CREATE` / `EVENT_CLASS_UPDATE` /
`EVENT_CLASS_DELETE`, `MEMBERSHIP_EVENT_COLUMNS`, `EVENT_CLASS_JOIN` /
`EVENT_CLASS_LEAVE`, `resolve_membership_columns`, `build_membership_events_sql`,
`STATE_AT_COLUMNS`, `build_state_at_sql`, `build_state_at_end_sql`,
`MEMBERSHIP_STATE_AT_COLUMNS`, `build_membership_state_at_sql`,
`RECORD_INDEX_COLUMNS`, `build_record_index_at_sql`,
`build_record_index_at_end_sql`, `PRESENTATION_KEY_COLUMNS`,
`build_presentation_key_at_sql`, `build_presentation_key_at_end_sql`,
`build_truncated_history_sql`, `build_truncated_membership_sql`,
`build_truncated_records_sql`, `build_truncated_sidecar`, `require_single_branch`.

The interpretive layer between the base reader and the exporter modes. The reader
is faithful — it makes no reshaping choices. A derivation makes the *shared*
reshaping choices that more than one mode needs (interval reconstruction,
reference resolution, point-in-time replay), so each mode composes a derivation
rather than re-deriving the answer. Every derivation is a pure SQL fold whose
output values each trace to reader-visible base values; the mode materializes the
SQL through the reader's query surfaces, wraps it in a representation step, and
dispatches to a writer. The layer holds eight residents: `history` →
versioned-intervals, the reference-resolution pair (reference-path and
membership-edge), `history` → row-state-events (the per-record `c`/`u`/`d`
change-event stream the streaming exporter replays and the source exporter's
event log folds into audit rows), `membership__<K>__<p>` → membership-events
(the `join`/`leave` event stream the streaming exporter replays for
collection-valued properties), `history` + `records__<kind>` → state-at (the
point-in-time row reconstruction, with a horizoned and an end-of-tape entry
point), `membership__<K>__<p>` → membership-state-at (interval containment
at a horizon), `records__<kind>` → record-index (the id-space-to-index-space
join relation a mode `LEFT JOIN`s to resolve integer surrogate keys, with the same
horizoned / end-of-tape entry-point split), and `records__<kind>` →
presentation-key (record-index's exact sibling — the `record_id` →
`presentation_id` join relation the key-election surface resolves elected
identities through, same entry-point split). Alongside the folds it carries the
**truncated-tape surface** —
three relation presenters and a sidecar view that render the emit sliced at T
for a mode to compile over. The playback seam composes state-at,
membership-state-at, and the truncated-tape surface for its point-in-time and
shaped-`state` answers ([`playback.md`](playback.md)).

```
reader        — faithful, no choices
  ▼
derivations   — interpretive shared folds (this layer)
  │   residents: history → versioned-intervals  (one row per record-version)
  │              reference resolution           (reference-path · membership-edge)
  │              history → row-state-events      (one event per record state change: c/u/d)
  │              membership → membership-events  (join/leave per membership interval)
  │              history + records → state-at    (one row per record, as of a horizon or the tape's end)
  │              membership → membership-state-at (one row per interval containing a horizon)
  │              records → record-index            (record_id → record_index, for a mode to LEFT JOIN)
  │              records → presentation-key        (record_id → presentation_id, its exact sibling)
  │              truncated-tape presenters        (base tables rendered sliced at T, for a mode to compile over)
  │   owns:      the temporal-honesty contract and the single-branch guard
  ▼
exporters     — modes compose a derivation + a representation step + a writer
```

---

## Boundary

- **Inputs.** The reader's typed `Sidecar` plus plain parameters — `str`,
  `frozenset[str]`, ints. A derivation signature never names a mode's config-model
  type (`TableDecl`, `DimensionalConfig`, …). This is the anti-weld rule: a
  derivation usable by one mode is usable by all.
- **Output.** One complete `SELECT` string over base tables, producing a
  documented, ordered, canonical column list. The derivation performs no I/O and
  holds no connection; the calling mode materializes it through `Emit.query` /
  `Emit.query_arrow`.
- **Forbidden imports.** A derivation imports only the reader,
  [`errors.py`](../../src/fabulexa_forge/errors.py), and stdlib — never
  `exporters.*` and never `config`. Importing *from* this layer is open in the
  other direction — any higher layer (a mode, or `config` for parse-time
  validation against a derivation's canonical column list) may import a
  derivation's public symbols, and no cycle results because derivations never
  import config or `exporters.*`.

## Semantics

### The layer contract

Every derivation — the resident and any future one — obeys six rules:

1. **Pure SQL fold.** A function of the sidecar plus plain parameters, returning
   one complete `SELECT` over base tables. No I/O, no connection handling.
2. **Anti-weld.** Signatures take the sidecar and plain values, never a mode's
   config-model types.
3. **Canonical raw output.** Output columns are a documented, ordered, canonical
   list; values are raw (`sim_time` as ns `BIGINT`, property values as the codec's
   `VARCHAR`). Renaming and wallclock rendering are mode-side representation.
4. **Traceability.** Every output value is a base value, a deterministic recoding
   of base values (a cast, a `LEAD`/`LAG`, a row number over a defined total
   order), or a `NULL` with exactly one declared meaning. Nothing is invented.
5. **Determinism.** Same emit + same parameters + same code version → an identical
   relation. A fold — a relation read in its own right — declares its own `ORDER BY`
   and is identical under it. A **join relation** — reference-resolution,
   record-index — is deterministic as a set and declares no `ORDER BY`: a consumer
   `LEFT JOIN`s it rather than reading it ordered, and the reading mode's own order
   governs the result.
6. **Temporal honesty.** Every output row carries a derivation-defined **event-time
   key** (raw ns). No value on the row derives from base state later than that key,
   except sources the derivation declares temporally constant. This is the one
   shared contract that makes any derivation safe to window under a cadence driver
   without per-mode re-analysis; the incremental driver's window-membership rules
   are the dimensional-mode instance of it.

Derivations are single-branch at this stage: each takes the sole branch's
`fork_path` from `require_single_branch` and filters every base read to it.

### The versioned-intervals derivation

`build_versioned_intervals_sql(sidecar, fork_path, kind, tracked_properties,
discriminator_filter)` produces one row per `(record_id, version)` interval for a
kind over a set of history-tracked properties, optionally restricted to the
records the predicate selects. Canonical columns are `VERSIONED_INTERVAL_COLUMNS` —
`record_id`, `version_start`, `version_end` (raw ns; `version_end` is `NULL` on a
record's last version) — followed by one `prop__<p>` column per tracked property, in
the kind's sidecar column-declaration order. The fold reads only `history` (filtered
to the kind and the tracked properties) and the sidecar (to order the `prop__`
columns).

Version boundaries are the union of the tracked properties' change points,
**set-deduplicated on `(record_id, sim_time)`**: two tracked properties that change
at the same `sim_time` for a record contribute one boundary, not two. `version_end`
is `LEAD(sim_time) OVER (PARTITION BY record_id ORDER BY sim_time)`. Each `prop__<p>`
is that property's **most-recent `history.value` at or before `version_start`** — a
correlated as-of lookback (`sim_time <= version_start`, `ORDER BY sim_time DESC LIMIT
1`), codec `VARCHAR`. A `prop__<p>` is `NULL` when no history row precedes the
boundary (the property's first write postdates the version start); this is
indistinguishable from a `history.value` that is itself `NULL` (codec passthrough),
both rendering `NULL`.

One fold serves two consumers:

| Tracked-property count | Version boundaries | Consumer |
|---|---|---|
| Exactly one | that property's change points | the dimensional `history_interval` grain |
| More than one | the deduplicated union of all tracked properties' change points | the dimensional SCD-2 wide reconstruction |

The two cases are one fold because `sim_time` strictly increases within a
`(fork_path, kind, record_id, property)` series, so for a single property "every
history row" and "distinct change point" coincide — no per-property `DISTINCT` is
needed, and the cross-property union deduplicated on `sim_time` reproduces both
primitives. In the single-property case the as-of value on a boundary is trivially
that boundary row's own `value` (the boundary *is* that property's change point); in
the multi-property case a boundary set by property A takes property B's last-known
value at or before it, so each `prop__<p>` resolves independently against its own
series.

The event-time key is `version_start`: the first version's values come from the
earliest tracked change rows, each read at its own `sim_time`, so no value on a
version row derives from base state later than `version_start` (the temporal-honesty
contract). The derivation carries no static (untracked) columns and no provenance
columns — static values, per-source-type casts, and provenance enrichment are
mode-side representation (see [`dimensional.md`](dimensional.md)). `tracked_properties`
is non-empty; the empty set is not a valid call. A record with no history row for any
tracked property contributes no boundary and is absent from the relation — no
creation-time version is synthesized. The restriction is a semi-join on the reader's
records relation, which owns the predicate rendering
([`row-predicates.md`](row-predicates.md)); this fold passes the predicate through
untouched and renders no condition of its own. Behavioral cases are exercised in
[`tests/derivations/test_versioned_intervals.py`](../../tests/derivations/test_versioned_intervals.py).

### The row-state-events derivation

`build_row_state_events_sql(sidecar, fork_path, kind, properties)` produces one event
row per `(record_id, sim_time)` at which a record of `kind` changes state — the
per-record `c`/`u`/`d` change-event stream the streaming exporter replays (see
[`streaming.md`](streaming.md)). Canonical columns are `ROW_STATE_EVENT_COLUMNS` —
`record_id`, `event_sim_time` (raw ns), `event_class`, `op` — followed by
`presentation_id` when the kind carries it and one `prop__<p>` after-image column per
selected property, in the kind's sidecar column-declaration order. Rows are ordered
`(event_sim_time, event_class, record_id)`. It reads `history` (filtered to the kind
and the selected history-tracked subset) and `records__<kind>` (lifecycle spine,
current values, column order). It is distinct from versioned-intervals: it anchors a
genesis event on `created_sim_time`, carries type-1 current values in the after-image,
and emits delete events.

Three event sources, one per `event_class`:

| Source | `event_class` | `op` | Time | Condition |
|---|---|---|---|---|
| `records__<kind>.created_sim_time` | 0 | `c` | `C` | always — exactly one genesis per record, including records with zero history-tracked properties |
| selected type-2 `history` change points | 1 | `u` | each distinct `sim_time > C` | one per distinct history `sim_time` strictly after creation |
| `records__<kind>.deactivated_at` | 2 | `d` | `D` | only when `deactivated_at IS NOT NULL` |

`op` is a deterministic, invertible 1-to-1 recoding of `event_class`, projected in SQL
by the fold as `CASE event_class WHEN 0 THEN 'c' WHEN 1 THEN 'u' WHEN 2 THEN 'd' END` —
both columns ride on the fold output with distinct jobs: `event_class` is the ordering
key (and the streaming engine's cross-kind merge tiebreak; see
[`streaming.md`](streaming.md)), `op` is the renderable code a format reads.

**After-image reconstruction.** The after-image is the full row reconstructed at the
event's `sim_time = t`, every column cast to codec `VARCHAR` so the whole map is
`str`-or-`NULL` and needs no per-type JSON codec downstream:

- **identity** — `record_id`, and `presentation_id` when the kind carries it (the
  base-format optional slot immediately after `record_id`), cast to codec `VARCHAR` (a
  `BIGINT` `presentation_id` is cast, not assumed already-string).
- **selected type-2 property** — the most-recent `history.value` at or before `t`, an
  **inclusive** `sim_time ≤ t` as-of lookback (codec `VARCHAR`; `NULL` when no row is at
  or before), the same `<= version_start` rule as versioned-intervals.
- **selected type-1 property** — the record's **current** `records__<kind>.prop__<p>`
  value cast to codec `VARCHAR`, temporally constant — the declared temporal-honesty
  exception.

On a `d` row every after-image column (`presentation_id` and each `prop__<p>`) projects
`NULL`: a canonical after-only delete, the terminal state already delivered by the
preceding `c`/`u`. The after-image keys are the fold's output column names verbatim —
`record_id`, `presentation_id`, and `prop__<name>` per selected property (the same
`prop__` prefix the base table uses), so a property selected by its bare config name
`name` appears under the key `prop__name`.

**Type-1 vs type-2 by the sidecar.** `properties` is partitioned by each column's
sidecar `history_tracked` flag under the `is True` convention: exactly `True` is type-2
(history-tracked, as-of value); `False` — or `None` on a non-conformant emit — is
type-1 (current value). The split is never inferred from `history` and has no
inference fallback; the version gate admits the emit but does not re-check
conformance, so the `None`-as-current-value rule defines the otherwise-undefined case
rather than failing. Every history-tracked property carries a genesis `history` row at
its record's `created_sim_time` (the unconditional creation seed —
[`bundle.md`](bundle.md) § Column temporal classes), so the genesis fold at `C`
(below) is load-bearing for every flagged property, presentation values included: the
inclusive lookback folds the seed row into the `c` after-image while `t > C` keeps it
from spawning a spurious `u` at the record's own creation instant.

**The genesis fold at `C`.** The `t > C` condition governs only which distinct history
`sim_time`s spawn a `u`; it never governs the after-image read, which is the inclusive
`sim_time ≤ t` lookback at every event. At the `c` event `t = C`, so the inclusive
lookback folds a creation-seed history row at exactly `C` into the `c` after-image
while `t > C` excludes the same row from spawning a separate `u`. This is the
versioned-intervals `sim_time <= version_start` rule applied at `t = C`, and it is why a
record with zero type-2 history still gets a `c` carrying its creation values.

The event-time key is `event_sim_time`. `properties` may be empty (identity + lifecycle
only — a `c` for every record and a `d` for the deactivated, no `prop__` columns); one
event is emitted per distinct `(record_id, sim_time)`, so multiple property changes at
one instant coalesce into a single after-image with no per-property events. Behavioral
cases are exercised in
[`tests/derivations/test_row_state_events.py`](../../tests/derivations/test_row_state_events.py).

**One column-order producer.** The after-image column order — `record_id`, then
`presentation_id` when the kind carries a surrogate, then each selected `prop__<p>` in
sidecar column-declaration order — is produced by the single function
`resolve_stream_columns(sidecar, kind, properties)`. It takes a sidecar, a bare kind,
and a property set and imports no config, so every consumer of that order calls it: the
fold's SELECT ordering, the streaming engine's after-image keying, and the Debezium
value-schema builder (see [`streaming.md`](streaming.md)). The fold — itself a
derivation-layer module that imports no config — calls it directly; the engine and the
schema builder destructure their `StreamKindSelection` at the call site. One producer,
three callers, pinned by a test: a single producer is what keeps the after-image key
order and the fold's SELECT order identical by construction, so a kind whose sidecar
interleaves history-tracked and current properties cannot mis-pair after-image keys with
their values. The engine's fold-row column list is `ROW_STATE_EVENT_COLUMNS` plus this
list past `record_id`.

### The membership-events derivation

`build_membership_events_sql(sidecar, fork_path, owner_kind, property_name, fields)`
produces one `join`/`leave` event row per membership interval over a single
`membership__<owner_kind>__<property_name>` table — the event stream the streaming
exporter replays for `content: membership-events` (see [`streaming.md`](streaming.md)).
Canonical columns are `MEMBERSHIP_EVENT_COLUMNS` — `record_id` (the collection owner),
`event_sim_time` (raw ns), `event_class`, `op` — followed by one payload column per
selected element-schema field, in element-schema declaration order. Rows are ordered
`(event_sim_time, event_class, record_id, <selected-field columns>)`. It reads only the
one membership table (filtered to `fork_path`) and the sidecar — never `history` or the
records spine, the distinguishing source from row-state-events. The membership table is
discovered through the sidecar (`category == "membership"`, owner kind, property), never
hard-coded.

**Interval → events (the unpivot).** Each membership interval row contributes one or two
events:

| Interval row | Events emitted |
|---|---|
| `joined_sim_time = J`, `left_sim_time = L` (non-null) | a `join` at `J`, then a `leave` at `L` |
| `joined_sim_time = J`, `left_sim_time IS NULL` (present at the slice boundary) | a `join` at `J` only |

`joined_sim_time` is non-null on every interval row, so every interval yields exactly one
`join`. `left_sim_time` is null exactly when the element is still present at the slice
boundary (`base-format.md` § Membership-category tables), so a null `left_sim_time`
faithfully emits no `leave`: absence of departure within the emitted slice is not
departure, and no synthetic `leave` is fabricated. The leave projection is a `UNION ALL`
arm gated on `left_sim_time IS NOT NULL`, so `event_sim_time` is non-null on every emitted
row.

`op` is a deterministic 1-to-1 recoding of `event_class`, projected in SQL by the fold:
`event_class 0 → 'join'` (time `joined_sim_time`), `1 → 'leave'` (time `left_sim_time`).
As with row-state-events, `event_class` is the ordering key (and the streaming engine's
cross-source merge tiebreak; see [`streaming.md`](streaming.md) § Cross-source merge and
global `seq`), `op` is the renderable code a format reads. `event_sim_time` carries the one
event-time value the unpivot selects — `joined_sim_time` on a join row, `left_sim_time` on
a leave row — so the engine's shared `ts` rendering, `seq` stamping, and k-way merge key on
that single column with no content-specific branch, exactly as row-state-events folds
`created_sim_time` / a history `sim_time` / `deactivated_at` into one `event_sim_time`.

**The payload (after-image) and temporal honesty.** Both `join` and `leave` carry a full
payload — membership-events are an append-only event log, not an upsert/compaction log, so
a `leave` is not a key-only tombstone; it carries what left. The payload is the owner
`record_id` (also the message key), then one column per selected element-schema field in
declaration order — a scalar field `f` → `elem__<f>`; a reference field `f` → the pair
`member__<f>__kind` / `member__<f>__id`. Every payload value is wrapped
`CAST(<col> AS VARCHAR)` — the same inline cast row-state-events applies to its after-image
columns — so each is `VARCHAR`-or-`NULL`; `event_sim_time` and `event_class` are projected
as raw integers, never cast, so the merge key's int components hold. An empty `fields` list
carries owner identity only.

Element-field values are constant over an interval — an interval is by definition a
contiguous span of unchanged membership; a change closes one interval and opens another. So
neither event derives a payload value from base state later than its own event-time key,
satisfying the temporal-honesty contract. The counterpart boundary time is never carried in
the payload: a `join` at `J` carries no `L` (`L > J` is future state), and the `leave`
carries no boundary time either; the event time is conveyed by `ts` / `event_sim_time`
alone.

**The fold's `ORDER BY`.** Each fold emits its rows sorted by
`(event_sim_time, event_class, record_id, <selected-field columns>)`, the selected-field
tail in `resolve_membership_columns` order (element-schema declaration order, not author
`fields` order). Each field column is compared as `CAST(<col> AS VARCHAR)` — the same
expression the payload renders — and ordered `NULLS FIRST` explicitly (DuckDB's `ASC`
default is `NULLS LAST`), realizing the contract's NULL-first membership row-order rule; a
reference field orders by its `member__<f>__kind` then `member__<f>__id` column.
`event_class 0 < 1` orders a coincident `join` strictly before a `leave` at one instant.
This `ORDER BY` *extends* the engine's
`(event_sim_time, event_class, source_identity, owner_record_id)` merge-key prefix with the
field-value tail, so the cross-source k-way merge — keyed only on the prefix — receives each
fold's rows already in canonical order and never compares a field value across folds (see
[`streaming.md`](streaming.md) § Cross-source merge and global `seq`). Behavioral cases are
exercised in
[`tests/derivations/test_membership_events.py`](../../tests/derivations/test_membership_events.py).

**One column-order producer.** The payload column order — `record_id`, then each selected
element-schema field's column(s) in declaration order — is produced by the single function
`resolve_membership_columns(sidecar, owner_kind, property_name, fields)`, which imports no
config. Both the fold's SELECT and the streaming engine's after-image keying call it, so the
declared order and the rendered rows are one list by construction — the membership analog of
`resolve_stream_columns`. A conformant emit makes each field exactly one shape — scalar
(`elem__<f>`) or reference (`member__<f>__*`), never both (`base-format.md` § Membership
tables) — so the bare field name resolves to one column shape; the resolver probes the
reference pair first, then the scalar column, and uses the first that resolves rather than
defending against a non-conformant table carrying both.

### The state-at derivation

`build_state_at_sql(sidecar, fork_path, kind, properties, horizon_ns)`
reconstructs one row per record of the kind, alive or deactivated, as of an
exclusive horizon — the point-in-time counterpart to versioned-intervals' per-version
rows. A row reflects every event with `sim_time < horizon_ns` and nothing
at-or-after; the exclusive horizon aligns the fold with half-open window
arithmetic (the snapshot at window `k`'s end is exactly the state produced by
windows `0..k`). `properties` may be empty (identity + lifecycle only); an
unresolvable property name raises `ExportError`, a missing `records__<kind>`
table raises `TableNotFoundError`.

Canonical columns are `STATE_AT_COLUMNS` — `record_id`, `presentation_id` when the
kind carries it, `created_sim_time`, `active`, `deactivated_at` (both
horizon-rendered, raw ns) — followed by one `prop__<p>` per selected property in
sidecar column-declaration order: a history-tracked property as its most-recent
`history.value` strictly before the horizon (codec `VARCHAR`; `NULL` when none),
an untracked property as its current records value (the declared
temporally-constant exception every other derivation and mode representation
shares). The `history_tracked` partition applies the `is True` convention. The
relation's event-time key is the constant horizon: every value derives from base
state strictly earlier than it, so the fold is temporally honest by the same test
as every other resident. Declared order: `(created_sim_time, record_id)`. Reads
only `history` and `records__<kind>`, filtered to `fork_path`. Values are raw;
wallclock rendering and per-source-type casts are mode-side representation — the
source exporter's windowed state snapshot (see [`source.md`](source.md) §
Incremental composition) is this fold's first consumer; point-in-time export
(§ Staged roadmap, Stage 5) is a later one. Behavioral cases are exercised in
[`tests/derivations/test_state_at.py`](../../tests/derivations/test_state_at.py).

**The end-of-tape entry point.** `build_state_at_end_sql(sidecar, fork_path,
kind, properties)` is the resident's second entry point: the same canonical
`STATE_AT_COLUMNS` relation and declared ORDER BY as the horizoned builder,
reconstructed with no horizon — no `created_sim_time` row filter (every record
of the kind), `active` / `deactivated_at` from the spine verbatim, each tracked
property at its latest recorded `history` value, constant properties at their
current records value. "The tape's end" is **structural**: the SQL carries no
horizon parameter and no horizon predicate, so composing this relation over
truncated base relations bounds it at the truncation position with no horizon
ever computed — the property the playback seam's shaped `state` and the base
exporter's tape's-end horizon both rest on ([`playback.md`](playback.md),
[`base.md`](base.md)). The equivalence is the testable
contract: this relation equals `build_state_at_sql` at any `horizon_ns` strictly
beyond every `history` and lifecycle instant of the composed relations — a
horizon cleared against `history` alone is wrong, rendering a later-deactivated
record active, because a deactivation is a spine fact, not a `history` row. The
horizoned builder's signature is untouched; `build_state_at_end_sql` raises the
same cause-based errors (`TableNotFoundError`, `ExportError`).

### The membership-state-at derivation

`build_membership_state_at_sql(sidecar, fork_path, owner_kind, property_name,
fields, horizon_ns)` is the point-in-time counterpart to membership-events —
interval containment at an exclusive horizon, under the same six-rule layer
contract. One row per `membership__<owner_kind>__<property_name>` interval
(discovered through the sidecar, filtered to `fork_path`) satisfying
`joined_sim_time < horizon_ns AND (left_sim_time IS NULL OR left_sim_time >=
horizon_ns)`. Canonical columns are `MEMBERSHIP_STATE_AT_COLUMNS` — `record_id`
(the owner), `joined_sim_time` (raw ns) — followed by each selected
element-schema field's column shape in `resolve_membership_columns` order
(`elem__<f>` for a scalar field, the `member__<f>__kind` / `member__<f>__id`
pair for a reference field), each cast to codec `VARCHAR`. **`left_sim_time` is
never projected**: for a contained interval it is `NULL` or strictly-future
state relative to the horizon, and carrying it would break temporal honesty.
Declared order is `(joined_sim_time, record_id, <field tail>)`, the tail
compared `CAST(... AS VARCHAR) NULLS FIRST` (the membership-events tail rule).
The event-time key is the constant horizon; every projected field value is
interval-constant by the same upstream guarantee membership-events relies on, so
the fold is temporally honest. Totality holds as for every resident: an inverted
interval (`left < joined`) satisfies the predicate for no horizon and answers
deterministically, overlapping duplicates yield one row each — faithfully wrong,
never an error. A missing membership table raises `TableNotFoundError`; an
unresolvable field raises `ExportError`. Its first consumer is the playback
seam's tier-1 `snapshot` ([`playback.md`](playback.md)). Behavioral cases are
exercised in
[`tests/derivations/test_membership_state_at.py`](../../tests/derivations/test_membership_state_at.py).

### The reference-resolution derivations

Two functions resolve an anchor record to a related value, each returning
`REFERENCE_RESOLUTION_COLUMNS` — `(record_id, resolved)` — that a mode `LEFT JOIN`s
on `record_id`:

| Function | Resolves | `resolved` is |
|---|---|---|
| `build_reference_path_sql(sidecar, fork_path, anchor_kind, hop_columns, terminal_projection)` | an ordered chain of `references`-annotated `prop__` hop columns from an anchor kind to a terminal record | the terminal `record_id` (FK use) or a terminal `prop__<property>` (lookup use) |
| `build_membership_edge_sql(sidecar, fork_path, owner_kind, property_name, member_field, member_kind, where_predicate)` | a membership table for an owner kind, narrowed by a `where` over `elem__` columns and by member kind | the bound `member__<field>__id` |

The two differ in cardinality. **reference-path is fan-out-free** — every hop is
keyed on `record_id`, unique within a records table per branch, so there is at most
one `resolved` per anchor `record_id`; the empty `hop_columns` is the zero-hop self
case (terminal = anchor record). **membership-edge is not** — it yields one row per
qualifying binding, so the author's `where` predicate is what narrows an owner to a
single member; an ambiguous `where` leaves several rows for one `record_id` and fans
the anchor out on join. A list-valued `where` sits between a scalar and an absent
predicate on that spectrum: it admits every binding matching any element, so it can
fan out where a scalar does not. An anchor whose path does not start resolvable, or a
member-kind mismatch, projects `NULL` — never fabricated.

The edge's `where` entries compile through the one predicate-rendering authority
([`row-predicates.md`](row-predicates.md)) against each element column's sidecar
type, which is how the layer narrows by an author predicate without importing the
exporters: the authority takes plain values and a type string, never a config
object.

**The faithful-vs-interpretive boundary.** Both the faithful membership relation
(reader tier) and the membership edge name the membership table; the deciding fact is
the **member-kind narrow**. The faithful relation returns the membership rows
verbatim, filtered only by `fork_path` and the author's `where`, with no notion of
which member kind an FK targets. The edge additionally narrows `member__<field>__kind
= member_kind` and projects the resolved id. `member_kind` is a reference-graph fact
(which kind the FK lands on), not an author predicate; encoding it is the interpretive
act that keeps it out of the format. So the membership **grain** (all bindings)
composes the faithful reader relation; the membership **FK** (a resolution) composes
the edge. The test: a relation narrowed only by author predicates is faithful; one
narrowed by a reference-graph fact and projecting a resolved value is interpretive —
even over the same table.

**Error taxonomy is by cause.** A missing base table — the only failure the faithful
reader builders and the introspection helper can hit — raises `TableNotFoundError`;
an invalid interpretive resolution (a `terminal_projection` that is neither
`record_id` nor a `prop__` column, a `member_field` naming no reference pair, an
absent membership table on the edge) is a mode pre-validation gap and raises
`ExportError`. The path-resolution logic is shared with validation's resolvability
rules (`ReferencePathResolvable`, `MembershipEdgeResolvable`, `LookupColumnSafety` in
[`dimensional.md`](dimensional.md)), so the "is this resolvable?" check and the
executed resolution give one answer.

### The record-index derivation

`build_record_index_at_sql(sidecar, fork_path, kind, horizon_ns)` resolves a kind's
id-space identity to its index-space identity at an exclusive horizon: one row per
distinct `(record_id, record_index)` pair among the kind's records created strictly
before `horizon_ns`, filtered to `fork_path`. Canonical columns are
`RECORD_INDEX_COLUMNS` — `(record_id, record_index)`. Like the reference-resolution
pair this is a **join relation**, not a fold: it declares no `ORDER BY`, because a
mode `LEFT JOIN`s it onto a spine it already orders. A missing `records__<kind>`
raises `TableNotFoundError`, by the layer's cause-based taxonomy.

Three projection rules carry the contract:

- **`record_index` is projected verbatim, never recomputed.** The format pins it as
  set once at creation and never renumbered, so it is a temporally-constant value
  read at a creation instant the horizon predicate has already bounded below — the
  fold is temporally honest against the constant horizon by the same test as
  state-at. Recomputing it as a row number over the surviving set would renumber
  after a horizon filter and destroy the cross-emit stability the column exists to
  provide.
- **`DISTINCT`, which keeps a consumer's join one-to-one.** On a conformant emit the
  pair is already unique per record and the `DISTINCT` is a no-op. It is load-bearing
  over a corrupted emit: an exactly-duplicated row carries the *identical* pair, and
  collapsing it here is what keeps a consumer's key join from fanning its spine out
  (the one shape that would fan — two rows of one kind sharing a `record_id` with
  differing `record_index` — is not producible, since identity columns sit outside
  every corrupter cell operation's eligible population).
- **`active` is never a predicate.** Rows are filtered on creation time only. A
  record deactivated before the horizon remains a legal reference target; filtering
  it out would manufacture a dangling edge the base layer does not contain.

Filtering on creation time alone also makes the surviving set a creation-order
**prefix**: `record_index` is the ordinal in creation order and is monotone in
`created_sim_time`, and records sharing a `created_sim_time` are retained or dropped
together, so no tie perforates the prefix. Over a conformant emit the relation's
indexes at any horizon are therefore exactly `0 .. n-1` for its row count. This is
inherited, never enforced — the relation asserts no density check, so a corrupted
emit's perforated or repeated indexes surface verbatim.

**The end-of-tape entry point.** `build_record_index_at_end_sql(sidecar, fork_path,
kind)` is the resident's second entry point: the same `DISTINCT
RECORD_INDEX_COLUMNS` relation filtered only to `fork_path`. "The tape's end" is
**structural** in the state-at sense — the SQL carries no horizon parameter and no
horizon predicate, so composing it over a truncated base relation bounds it at the
truncation with no horizon ever computed. The equivalence is the testable contract:
this relation equals `build_record_index_at_sql` at any `horizon_ns` strictly beyond
every creation instant of the composed relation.

The signature names no mode's concept (the anti-weld rule), so the one relation
answers any mode's surrogate-key question. Its consumers are the base exporter,
which joins it once per output table for the record's own key and once per
reference edge for that edge's key ([`base.md`](base.md) § Record-index key
columns), and any mode rendering a `record_index` key election
([`key-election.md`](key-election.md)). Behavioral cases are exercised in
[`tests/derivations/test_record_index.py`](../../tests/derivations/test_record_index.py).

### The presentation-key derivation

`build_presentation_key_at_sql(sidecar, fork_path, kind, horizon_ns)` is the
record-index derivation's **exact sibling** over the projection-minted identity:
one row per distinct `(record_id, presentation_id)` pair among the kind's records
created strictly before the exclusive `horizon_ns`, filtered to `fork_path`.
Canonical columns are `PRESENTATION_KEY_COLUMNS` — `(record_id,
presentation_id)`. A join relation, not a fold: it declares no `ORDER BY`,
because a mode `LEFT JOIN`s it onto a spine it already orders. A missing
`records__<kind>` raises `TableNotFoundError`; a kind whose records table
declares no `presentation_id` column raises `ExportError` — a caller gating
error, since the key-election gates make the call unreachable from a gated plan
([`key-election.md`](key-election.md) § Static gates).

The record-index derivation's projection rules hold here too, each by the same
argument:

- **`presentation_id` is projected verbatim, never re-derived.** The format pins
  it as genesis-minted, never re-minted, and never carried in `history`, so it is
  a temporally-constant value read at a creation instant the horizon predicate
  has already bounded below — temporally honest against the constant horizon by
  the same test as record-index.
- **`DISTINCT` keeps a consumer's join one-to-one** over exactly-duplicated
  corrupted rows. Unlike `record_index`, the value itself is reachable by cell
  operations (`mutate_cells`), so a duplicated-then-mutated row *can* fan the
  spine out — that shape is the render-time uniqueness guard's to refuse, not
  this relation's ([`key-election.md`](key-election.md) § The elected-key
  uniqueness guard).
- **`active` is never a predicate.** A deactivated record remains a legal
  reference target.

`NULL` `presentation_id` rows project verbatim — an undeclared population's
honest surface value; whether a consumer may draw from such a population is the
election gates' question, not the relation's.

**The end-of-tape entry point.** `build_presentation_key_at_end_sql(sidecar,
fork_path, kind)` mirrors the record-index split exactly: the same `DISTINCT`
relation with no horizon parameter and no horizon predicate — structural in the
state-at sense, so composed over a truncated base relation it is bounded by the
truncation with no horizon computed. It equals the horizoned entry point at any
horizon strictly beyond every creation instant of the composed relation.

The consumer is the key-election surface: any mode rendering a
`presentation_id` election for an identity column or a reference edge joins
this relation at the table's value horizon. Behavioral cases are exercised in
[`tests/derivations/test_presentation_key.py`](../../tests/derivations/test_presentation_key.py).

### The truncated-tape surface

Three relation builders and one sidecar view present the emit as the producer
would have emitted it sliced at `at_sim_time` (inclusive) — the input to the
playback seam's shaped `state` ([`playback.md`](playback.md) § Shaped state).
Unlike the folds, these are relation **presenters**: each returns a complete
SELECT that *replaces* a base table inside a mode's full-export compile, so each
carries the replaced table's column shape (with declared deviations) rather than
a canonical ORDER BY — a replacing relation's order is imposed by the compile
that reads it. They are pure, anti-weld, deterministic, and total over
structurally-conformant input like the folds. No existing resident changes.

- `build_truncated_history_sql(fork_path, at_sim_time)` — `history` rows with
  `sim_time <= at_sim_time`, filtered to `fork_path`; column shape verbatim.
  History is a fixed table, so there is no resolvability to check.
- `build_truncated_membership_sql(sidecar, fork_path, owner_kind, property_name,
  at_sim_time)` — intervals with `joined_sim_time <= at_sim_time`, filtered to
  `fork_path`, with `left_sim_time` masked `NULL` when `> at_sim_time` (an
  interval still open at T, exactly as a slice-at-T emit renders it); every
  other column verbatim. A missing table raises `TableNotFoundError`.
- `build_truncated_records_sql(sidecar, fork_path, kind, at_sim_time)` — one row
  per record with `created_sim_time <= at_sim_time`. Identity columns and
  `record_index` verbatim (`record_index` is slice-stable by contract);
  `active` / `deactivated_at` horizon-rendered; `constant` properties verbatim;
  `tracked` properties reconstructed as of T and `TRY_CAST` back to their
  sidecar-declared type (`NULL` where corrupted history text does not parse — a
  cast never errors); presentation-property columns by the same per-class rule;
  each `ref_index__<name>` re-derived from the reconstructed `prop__<name>` via
  the target kind's *truncated* spine (`NULL` beside an unresolvable reference).
  `slice_only` columns are absent — except a sub-typed kind's `slice_only`
  discriminator `prop__<kind>_type`, carried verbatim as the classification
  column (invariant 5's carve-out, [`slice-only.md`](slice-only.md)) — and
  `last_mutation_sim_time` is presented as the recorded trail
  `greatest(created_sim_time, the latest tracked history instant <= T,
  deactivated_at when <= T)`, never the physical value ([`playback.md`](playback.md)
  § The recorded trail). A missing table raises `TableNotFoundError`.

**One consistent truncated world.** Wherever a builder's recipe reads a base
table other than the one it presents — the records builder's tracked
reconstruction reads `history`; its `ref_index__` re-derivation reads the target
kind's spine — the read carries truncated-world semantics via an inline
truncation predicate, so its result equals a read of that table's truncated
presentation, never the physical table. A builder's read of the table it
*presents* names the physical table (the source being truncated). This makes the
cross-reads binding-insensitive under the seam's name-shadowing composition
(§ The compile indirection in [`playback.md`](playback.md)).

`build_truncated_sidecar(sidecar)` is a pure `Sidecar` derivation identical to
the physical sidecar except that each `records__<kind>` entry's column list
drops exactly the columns its truncated relation lacks (every `slice_only`
column bar the discriminator carve-out; `last_mutation_sim_time` remains declared
— the relation presents it as the trail). It is T-independent — the dropped set
is a function of the declared schema — and column-list agreement with the
relation builders is a stated invariant of the surface. Every other sidecar
field remains physical, the branch's slice bound included, which is why no compile
path under `state` may read a slice bound from the sidecar: the truncated world's
end is defined by its data, never by metadata.

### Relied-on upstream guarantees

The derivation depends on base-layer invariants it does not re-check: `sim_time`
strictly increases within a `(fork_path, kind, record_id, property)` series;
`deactivated_at` is `NULL` iff `active`; `last_mutation_sim_time` bounds every
content change including deactivation; the sole branch's `slice_at` bounds all data
`sim_time`s. Row-state-events additionally relies on `created_sim_time` being present
and non-null on every record; no selected-property history row preceding
`created_sim_time`; deactivation being terminal (no selected-property history row with
`sim_time > deactivated_at`); and the creation-seed guarantee — a type-2 property's
creation value is recoverable as its as-of value at `created_sim_time`, so the `c`
event's after-image carries it and no separate `u` is spawned at `C`. Membership-events
additionally relies on `joined_sim_time` being non-null on every interval row;
`left_sim_time` being null exactly when the element is present at the slice boundary;
element-field values being constant across an interval; a reference field's
`(member__<f>__kind, member__<f>__id)` pair being all-null or all-non-null (C7); and each
element-schema field carrying exactly one column shape — scalar or reference, never both.
Record-index relies on the format's `record_index` guarantees: set once at creation and
never renumbered, dense over each `(fork_path, kind)`, stable for a record across
every emit of its branch, and monotone in `created_sim_time` — creation order
agrees with creation time, which is what makes a creation-time filter carve a
creation-order prefix rather than a perforated set. Agreement between a `prop__<name>`
and its `ref_index__<name>` sibling is producer-guaranteed and outside the conformance
procedure ([`conformance.md`](conformance.md)) — the same trust class as id-space
referential integrity; the layer consumes the guarantee and never re-verifies it.

## Invariants

1. **Determinism.** Same emit + parameters + code version → an identical relation
   under the declared `ORDER BY`.
2. **Faithful reshaping.** Every output value traces to a `history`, `records__*`, or
   `membership__*` value or is a deterministic recoding (a `LEAD`-derived `version_end`, a
   row number over a defined order, the `event_class` / `op` event class of row-state-events
   or membership-events, a horizon-rendered `CASE` recoding of `active` / `deactivated_at`
   in state-at); `NULL` appears only with its declared meanings. The
   membership-events unpivot drops no interval and invents none, except the faithful
   no-`leave`-for-an-open-interval rule.
3. **Temporal honesty.** Every value on an event row derives from base state at or
   before the event's `sim_time` key, or from a declared-constant source.
4. **Layer direction.** Modes import derivations; derivations import only the
   reader, `errors`, and stdlib. No derivation imports `exporters.*` or `config`;
   importing *from* derivations is open to any higher layer.
5. **Inherited.** Version-gated input, no producer dependency, and sidecar-driven
   schema discovery (the derivation enumerates `records__*` tables and columns from
   the sidecar, never `SELECT *`, never a hard-coded list).

## Validation Rules

| Rule | Checks | Error |
|---|---|---|
| `SingleBranch` | The sidecar enumerates exactly one branch. `require_single_branch` is the one implementation; every mode invokes it, so all raise the same message | `"export requires a single-branch emit (trunk-only stage); emit has {n} branches (branch-aware export is Stage 5)"` |

`require_single_branch` returns the sole branch's `fork_path` for derivations to
filter on; it raises `ExportError` on zero or more than one branch.

## Rationale

- **A layer, not per-mode helpers.** Interpretive folding knowledge that more than
  one mode needs lives here once, not welded inside a single mode behind a
  config-model signature. Without a shared layer a new mode either re-implements a
  fold — yielding two subtly different answers to "what interval was this row valid
  for" — or imports a sibling mode's internals, inverting the dependency direction.
  The layer gives each shared fold one home with one anti-weld signature and fixes
  the import direction: modes depend on derivations, never the reverse, never on each
  other. Versioned-intervals is the single interval primitive — the dimensional
  `history_interval` grain and the SCD-2 wide reconstruction both compose it, so the
  "what interval was this row valid for" question has exactly one implementation.
- **Representation is mode-side.** A derivation returns one canonical raw relation;
  column renames and anchor-rendered timestamps belong to the mode. This keeps a
  derivation reusable across modes whose presentation differs.
- **Temporal honesty stated once.** Enforcing "no value reflects base state later
  than its own event time" per surface led to ad-hoc, per-mode window analysis. The
  layer states it as one contract every derivation declares an event-time key
  against, so a cadence driver can window any derivation without re-deriving it.

## Boundaries

- **Seven residents plus the truncated-tape surface.** The residents are `history` →
  versioned-intervals, the reference-resolution pair (reference-path and
  membership-edge), `history` → row-state-events, `membership__<K>__<p>` →
  membership-events, `history` + `records__<kind>` → state-at,
  `membership__<K>__<p>` → membership-state-at, and `records__<kind>` →
  record-index; the truncated-tape surface adds
  three base-table presenters and a sidecar view. The dimensional exporter's
  interval reconstruction and its reference / membership FK and `lookup`
  resolution compose the first two residents; the streaming exporter composes
  row-state-events (for `state-changes`) and membership-events (for
  `membership-events`); the source exporter composes row-state-events and
  membership-events (its event log) and state-at (its windowed state
  snapshot); the base exporter
  composes state-at for its values and record-index for its identity columns; the
  playback seam
  composes state-at, membership-state-at, and the truncated-tape surface — rather
  than any mode authoring its own base-table SQL. Grain assembly and the
  representation step (renames, casts, anchor rendering, the total `ORDER BY`) are
  mode-side — the format's own concern, not a derivation's.
- **Named, unbuilt slots.** Further derivations are anticipated but carry no code
  until a mode consumes them (Principle #8): membership *queue-state* reconstruction
  (wait time, FIFO/priority — distinct from membership-events, which streams the raw
  `join`/`leave` interval boundaries, not a queue-state projection), and
  `record-genesis` — a shared first-appearance fold that would supply creation events
  to record-grain modes. `record-genesis` reads the
  structural `created_sim_time` column — non-NULL on every records row, set once at
  creation — so a record's genesis time is always exact, with no availability
  gating; row-state-events reads that column directly for its `c` event rather than
  composing this shared primitive. Current-state reconstruction and point-in-time
  replay-to-T (feature-store rows) both compose the state-at resident above — the
  source exporter's windowed state snapshot and the playback seam's point-in-time answers
  are its consumers, and the `base` exporter is the consumer for which the resident
  *is* the whole output, materialized at three horizons: the tape's end (via the
  horizon-free end-of-tape entry point), `slice_at: T` at horizon `T + 1`, and each
  window's end under an incremental invocation ([`base.md`](base.md)).
- **Single-branch.** Each derivation filters to the sole `fork_path`. Branch-aware
  derivation (paired counterfactuals, per-branch slices) is parked — the sanitised
  subset carries exactly one branch.
- **Collection-valued (membership) property changes emit no `history` rows** — they
  are invisible to any history-sourced derivation (versioned-intervals, row-state-events).
  The membership-events resident reaches them by reading the `membership__<K>__<p>`
  interval tables directly instead, never `history`.
- **The folds do not lean on the genesis guarantee.** The unconditional creation seed
  makes an as-of lookup over `history` complete for a flagged property — an empty
  result means exactly `T < created_sim_time` — but state-at and row-state-events
  retain their `records__<kind>` join for type-1 current values and identity, and no
  fold substitutes a pure-`history` as-of read for it. A fold redesign that exploits
  the guarantee (dropping the `records__` fallback, an exact variable-horizon form)
  is a contract change to this layer, designed on its own.
- **Reconstruction never carries `ref_index__` values.** `history.value` is
  id-space only, and `ref_index__<name>` is a point-in-time key valid only at
  its own slice ([`bundle.md`](bundle.md) § The dense record index) — so any
  reconstruction surface that surfaces the index **re-derives** `ref_index__<name>`
  from the reconstructed `prop__<name>` via the target's `record_index`; it never
  carries the emitted slice's `ref_index__` value to another horizon. Two surfaces
  apply the rule, and **they bound the target spine with opposite inclusivity** —
  reading one as a template for the other is an off-by-one boundary defect:
  - the **truncated-records presenter** re-derives each `ref_index__<name>` inline
    against a target spine bounded **inclusively** at `at_sim_time`, and carries
    `record_index` verbatim (slice-stable by contract);
  - the **record-index resident** hands a mode the relation to join, bounded
    **exclusively** at a horizon.

  Collapsing the presenter's inline re-derivation onto the resident is a legitimate
  simplification of two implementations of one idea; it is not required by either
  surface's contract, and each is independently tested. The folds themselves remain
  narrower than both: no fold reads or emits identity columns beyond `record_id`,
  and state-at's reconstructed column set is `STATE_AT_COLUMNS` plus selected
  `prop__` columns only.
- **A `slice_only` column has no faithful point-in-time read.** State-at and the
  row-state-events after-image render a type-1 column's *current* `records__` value
  at every horizon — the declared temporal-honesty exception. For a `constant` column
  that value is exact at every T; for a `slice_only` column it is a slice value
  stamped at horizons the emit cannot speak to. The class makes the difference
  visible ([`bundle.md`](bundle.md) § Column temporal classes); the folds do not
  consult it, and the policy that refuses or omits such a column is mode-side —
  the export-wide `slice_only` posture ([`slice-only.md`](slice-only.md)), whose
  column-projection-only invariance rests on exactly this class-agnosticism:
  narrowing a fold's input property set removes after-image columns only, never
  event rows.

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | The `Sidecar` and the query surfaces every derivation reads and materializes through. |
| [`anchor.md`](anchor.md) | The wallclock rendering a mode applies on top of a derivation's raw `sim_time`. |
| [`dimensional.md`](dimensional.md) | The mode that composes the versioned-intervals and reference-resolution residents; the consumer that shares the single-branch guard. |
| [`streaming.md`](streaming.md) | The delivery driver that composes the row-state-events resident (`state-changes`) and the membership-events resident (`membership-events`) into ordered event streams. |
| [`source.md`](source.md) | The mode that composes row-state-events and membership-events (its event log) and state-at (its windowed state snapshot) into landed operational tables. |
| [`base.md`](base.md) | The mode that composes state-at for its values and the record-index resident for its integer key columns. |
| [`key-election.md`](key-election.md) | The cross-mode surface that composes the record-index and presentation-key relations to render elected identities and edges. |
| [`row-predicates.md`](row-predicates.md) | The predicate grammar and rendering authority the membership edge narrows through and the versioned-intervals fold passes along. |
| [`playback.md`](playback.md) | The seam that composes state-at, membership-state-at, and the truncated-tape surface for its point-in-time and shaped-`state` answers. |
| [`../../contract/base-format.md`](../../contract/base-format.md) | The vendored input contract — `history`, `records__*`, branch enumeration. |
| [`README.md`](README.md) | Design index, package layout, staged roadmap. |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary. |
