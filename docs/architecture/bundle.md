# The Bundle, From the Consumer's Side

**Purpose:** bundle-and-emit context for export architects. The vendored contract
([`../../contract/base-format.md`](../../contract/base-format.md)) defines the
*shape* of an emit; this doc carries the *meaning* — what each table genre
represents, which guarantees hold in every emit, and how emits relate inside a
bundle. Read this when designing a new exporter or corrupter mode; read the
contract when implementing one.

**Status: informational, not normative.** The enforced input contract remains
the two files per emit (`run.duckdb` + `base.json`) at the vendored
`base_format_version`. Nothing here extends or overrides `contract/`; where a
statement below touches contractual ground, the vendored spec wins.

---

## Where an emit comes from

```
  upstream producer (opaque to you)  │  the bundle (your input)
  ───────────────────────────────────┼──────────────────────────────────
   scenario ─► simulation ─► run  ────┼─►  emit  (run.duckdb + base.json)
```

An emit is a deterministic rendering of one simulation run. Everything left of
the bundle boundary — the scenario, the simulation, the run it persists — is
upstream and opaque: you never read it and cannot influence it. Your input is
the emit. Two consequences you can rely on:

- **Everything is simulated.** Every row derives from a simulated event. There
  is no fabricated mid-process state — an "established" population was
  simulated from an earlier start, not synthesized in place. Patterns in the
  data (queueing dynamics, funnel conversion, contention) emerge from the run;
  they are not sampled from a target distribution an analyst could read back
  out.
- **Everything is re-derivable except the run.** A mis-scoped emit is fixed by
  a cheap upstream re-emit — no re-simulation. Your own outputs are re-derivable
  from the emit. Only the run itself requires re-simulating. Corollary: when an
  emit lacks coverage (a table, an earlier slice), the fix is
  upstream; there is no downstream backfill.

---

## Bundle anatomy

```
my_bundle/
  bundle.json        ← single source of truth: manifest, run summary, emit entries
  scenario.yaml      ← archived author input
  run/               ← the upstream run — sealed, opaque, never your input
  emits/
    01JE7X3KRC.../   ← one ULID-named directory per emit invocation
      config.yaml    ← archived producing config (the export config)
      run.duckdb     ← data        (role: "data")
      base.json      ← sidecar     (role: "sidecar")
    01JE7X8RKD.../
```

Facts that shape consumer design:

- **`bundle.json` is the single source of truth.** Manifest (scenario path +
  sha256, `code_versions`, `bundle_format_version`), run summary (`run_id`,
  `master_seed`, `stop_reason`, `final_sim_time`, `total_firings`, branch list,
  `fork_tree_fingerprint`), and the append-ordered `emits[]` log all live in
  this one file.
- **`emits/` is append-only and atomic.** A directory visible at
  `emits/<ULID>/` is complete — crash residue is quarantined under
  `emits/.tmp/`. Re-emitting produces a new entry, never a mutation. ULID names
  sort chronologically.
- **File discovery is role-keyed.** Locate the data file and sidecar via the
  emit entry's `outputs[]` (`role: "data"` / `"sidecar"`); basenames are not
  fixed by contract. Never assume `run.duckdb` / `base.json` by name.
- **The reproducibility triple is recorded.** `master_seed` + `scenario.sha256`
  + `code_versions` pin row-level re-derivation of the entire bundle.
- **`run/` is never your input.** The run is sealed and opaque — everything a
  downstream tool needs is rendered into emits.
- **Many emits, one run.** Each emit carries its own export config — its branch
  scope and slice time — so a bundle may accumulate several views of one
  simulation. Choosing the right emit is the consumer's first act: iterate
  `emits[]`, check each entry's `scope` and sidecar.

> **Contract status.** `bundle.json`'s schema is owned upstream
> (`bundle_format_version: 1`) and is **not** yet vendored in `contract/`. Every
> `fabulexa-forge` verb today takes an emit directory and parses nothing at bundle
> level. The first feature that traverses a bundle (emit discovery, multi-emit
> combine) must first vendor the bundle spec into `contract/` under the same
> explicit re-sync policy as the base format.

---

## Table genres

One emit carries three table categories. The contract owns their shapes
(§ Required tables, § Fixed-category column lists); this section says what
each *is*.

| Table | Category | Data genre |
|---|---|---|
| `history` | fixed | Change log (CDC) — SCD-2 history of history-tracked properties; the one mandatory table |
| `membership__<kind>__<prop>` | membership | Interval / session data — membership intervals of a collection-valued property |
| `records__<kind>` | records | Reference state (dimension-like) — record values at the emit's slice time |

**`history`** — long-form: one row per property change; SCD-2 intervals are
implicit (derive `valid_to` with `LEAD` over `(kind, record_id, property)`).
Sufficient to reconstruct any history-tracked property at any `sim_time`;
conformance C6 ties `records__*` and `history` together. Genre-wise this is a
change log: the natural source for CDC-shaped exports and the raw material for
"build the SCD-2 yourself" teaching shapes.

**`membership__<kind>__<prop>`** — one row per membership interval of a
collection-valued property: queue waiters, holder sets, or any time-varying
collection of memberships. Queue depth, FIFO/priority order, and wait times are
ordinary SQL over intervals (contract § Consumer derivations). Members are
references checkable against the corresponding `records__*` table.

**`records__<kind>`** — end-of-slice state per branch in scope: typed `prop__`
columns, references as id-only columns the sidecar declares. Not events —
this is enrichment/dimension data, and the snapshot half of a
snapshot-plus-changelog pattern with `history`.

Beyond table shapes, the sidecar carries the per-emit interpretation surface:
branch enumeration and the `runtime` wallclock anchor, `pinned_ids` (authored
pinned individuals), `enum_domains` (closed `category`/`status` vocabularies —
ready-made lookup/dimension domains), per-column `history_tracked` (which
properties have change rows in `history`), and the `record_roles` registry
(warehouse role per kind). All normative semantics: contract § The sidecar.

---

## The dense record index

Beyond `prop__` payload, every `records__<kind>` table carries two identity
column families (contract § Dense record index — the normative statement):

- `record_index` — `BIGINT NOT NULL`, immediately after the lifecycle prefix:
  the 0-based `(fork_path, kind)` creation-order ordinal.
- `ref_index__<name>` — `BIGINT`, immediately following each reference-typed
  property's `prop__<name>` column: the referenced record's `record_index`,
  NULL iff the reference is NULL.

Both are **identity columns**, like `record_id` and `fork_path`: they carry no
`history_tracked` / `temporal_class` attributes, and `ref_index__<name>`
carries no `references` annotation of its own — the sibling `prop__<name>`'s
sidecar entry is authoritative for the target kind. `history` and membership
tables carry neither family; there is no `ref_index` analog on membership
reference pairs.

The contract pins four guarantees a consumer may lean on:

- **Density.** Per `(fork_path, kind)`, `record_index` values exactly cover
  `0 .. rows − 1` — every integer assigned to exactly one row, so a consumer
  MAY allocate an array of length `rows` and index it directly.
- **Row-order corollary.** `record_index` equals the record's 0-based position
  in the records table's guaranteed row order (creation order within kind) —
  consumer-verifiable directly against the data.
- **Slice stability.** A record keeps the same `record_index` in every emit of
  its branch: an earlier slice drops a creation-order suffix, leaving the
  remaining indices a dense prefix of the same enumeration; deactivation never
  renumbers.
- **Trust class.** Pair agreement (`ref_index__<name>` and its sibling
  `prop__<name>` NULL together and resolving to the same target row) and
  resolution of `ref_index__<name>` values against the target's `record_index`
  are producer-guaranteed **by construction and not verified by C1–C13** — the
  same trust class as `prop__` referential integrity.

A records reference is thus **one edge with two encodings**: id-space
`prop__<name>` (joins the target's `record_id`) and index-space
`ref_index__<name>` (joins the target's `record_index`). The two families
differ in temporal character — `record_index` is stable across slices of a
branch, while `ref_index__<name>` is a **point-in-time key**: it renders the
referenced record's `record_index` *at the emitted slice*, so it legitimately
differs across slices of the same branch when the reference itself was
rewritten between them. A reconstruction surface therefore re-derives index
values rather than carrying an emitted slice's to another horizon (see
[`derivations.md`](derivations.md) § Boundaries).

The contract also binds `last_mutation_sim_time` as a high-water mark over the
record's property writes; no forge check or mode reads that guarantee.

---

## Guarantees that survive into an emit

These invariants hold **by construction** in every conformant emit.
Exporters must preserve them (faithful reshaping); corrupters break the
semantic ones deliberately. This is the list of what "them" means:

| Inherited guarantee | Meaning in the emit |
|---|---|
| Row-level determinism | Same (`master_seed` + scenario + `code_versions`) → identical rows in identical order. Binary bytes of `run.duckdb` are *not* stable — compare rows, never files. |
| Monotonic time | `sim_time` never decreases along the change log's total order. |
| Referential integrity | Reference columns resolve within the emit's scope; the producer guards scope closure at emit time. |
| Unconditional creation seed | Every `history_tracked: true` property of every record carries a `history` row at that record's `created_sim_time` (NULL-valued when the property was absent at creation) — see § Column temporal classes below. |
| History↔records property naming | Every `history` row's `property` names a `prop__<property>` column on `records__<kind>`, presentation sub-picks included; `history` introduces no property without a corresponding records column. |
| Actor identity across forks | The same `(kind, record_id)` in two branches is the same logical individual — identical intrinsic properties (arrival, demographics, identity draws); only consequences downstream of the divergent config differ. |
| Trunk row-identity | Rows recorded before a fork point are identical across sibling branches. |
| Complete history | Every state row is reachable from simulated events in `history`; nothing was fabricated mid-process. |

**Conformance is narrower than the guarantee set.** C1–C13 verifies structure
and a semantic subset; a defect such as a dangling records-property reference
passes C1–C13 by design (see
[`conformance.md`](conformance.md) § Boundaries). The guarantees above hold by
construction in the emit, not because `fabulexa-forge validate` proves them. Design consequence: an exporter may *lean on* these properties
(e.g. skip dangling-reference handling) but must not claim to have *verified*
them.

---

## Column temporal classes and the genesis guarantee

Every value-carrying `prop__<name>` column on a records-category table declares a
pair of temporal attributes in the sidecar — `history_tracked` (the SCD-class flag)
and `temporal_class` (the point-in-time contract). The pairing is structural: a
column carries one iff it carries the other, and `presentation_id` carries neither.
C13 judges the pairing and the classes' implications; the reader carries the
declared values verbatim and narrows them in one accessor
([`reader.md`](reader.md) § Per-column temporal semantics).

| `temporal_class` | Value at horizon T (T ≥ `created_sim_time`) | Modelled as |
|---|---|---|
| `constant` | the current value — exact at every T | read `records__<kind>.prop__<name>` |
| `tracked` | exact | an ordered lookup over `history` |
| `slice_only` | **unknowable** | nothing in the emit can answer it |

`tracked` implies `history_tracked: true`; `slice_only` implies
`history_tracked: false`; `constant` admits either — a constant column that is also
history-tracked holds exactly its genesis row. The class is never derived from the
bit: a `history_tracked: false` column may be genuinely constant *or* mutable with
an untracked past, and only the declared class separates a value that is exact at
every horizon from one whose past is unknowable.

**The genesis guarantee (the unconditional creation seed).** Every
`history_tracked: true` property of every record carries a `history` row at that
record's `created_sim_time` — NULL-valued when the property was absent at creation.
Consequences:

- An empty as-of lookup over `history` for a flagged property means exactly
  `T < created_sim_time` — never "created NULL, never changed", never "the value
  lives only in `records__`".
- A NULL-valued `history` row means the value was genuinely NULL at that time (NULL
  is a legal history-entry value — `contract/base-format.md` § `history`); it
  round-trips through C6 as NULL-against-NULL.
- Zero `history` rows for a flagged column of a kind with extant records is a
  conformance violation (C11's converse clause — see
  [`conformance.md`](conformance.md)).

**Presentation columns are history-tracked.** Every presentation column carries
`history_tracked: true` — class `tracked` when its bound source is tracked,
otherwise `constant`; a presentation column is never `slice_only`
(`contract/base-format.md` § Column temporal semantics → *Which columns carry the
pair*). Together with the unconditional seed, `history` carries a genesis row for
every flagged property of every record, presentation sub-picks included — so a
change-log genre, an SCD-2 dimension, and a CDC stream all see presentation values
version like any other tracked value, and a kind whose only genuinely-changing
column is a presentation value renders as a change log
([`source.md`](source.md) § Classification).

**What the emit does not say.** A genesis row's value may be an intrinsic birth
value or a truncated as-of initial condition whose real history predates the run.
The contract carries no marker distinguishing them, and this package stays silent
on the distinction rather than guessing.

---

## Time

`sim_time` is everywhere an integer nanosecond offset from the run's origin —
not an epoch. The sidecar's `runtime` block carries the producer's wallclock
anchor (origin + zone) when the scenario declared one; rendering offsets to
wallclock is a consumer-side act through the one `EffectiveAnchor`
([`anchor.md`](anchor.md)), with config/CLI overrides taking precedence over
the sidecar. Temporal realism — calendars, business hours, intra-day arrival
shaping — was applied upstream during simulation and is already encoded in the
offsets. A consumer rebases time; it never re-shapes it.

---

## Forks and branches

The upstream run is a tree — a trunk plus branches forked at snapshot points with
divergent configuration — but a **sanitised emit carries exactly one branch**
(`branches` has one entry; the contract mandates it and C8 asserts it). Every row
in every table is tagged with that branch's `fork_path` in canonical `@`-joined
form (`"trunk@A"`). Consequences:

- **One `fork_path` per emit.** Every table's `fork_path` equals the sole
  `branches[].fork_path`. A branch's `parent` field may name a fork path not
  present in the emit (a descendant branch selected at export names an absent
  parent); C8 constrains `fork_path` values only, not `parent` values.
- **Paired comparison needs a future contract.** Branch-aware export — joining two
  branches on `(kind, record_id)` under identity preservation — is parked: a
  sanitised emit carries one branch, so a multi-branch view is out of reach until
  the contract restores one.
- **No downstream backfill.** A consumer that needs a different branch or slice
  gets it from an upstream re-emit, never a downstream synthesis.

---

## Mechanism and presentation columns

A sanitised emit carries **mechanism** properties — values rules read or write, or
identity-bearing intrinsics (authored `int` / `decimal` / `category` / `status`,
plus emitted `reference` / `timestamp` / `duration` kinds). References are id-only
columns (`member__<f>__id`, reference `prop__<x>` ids), so a dimension key is a
mechanism `record_id`, never a name.

**Presentation** values — names, addresses, formatted ids — are not mechanism data.
When the producer minted them, they sit **inline on the records table**, not in a
separate emit:

- An optional `presentation_id` surrogate occupies the slot right after
  `record_id` (only when a non-`inherit` `presentation_id` strategy minted one; its
  scalar type is producer-determined, the sidecar authoritative). It is a valid FK
  `target_key` for a dimension. It carries neither temporal attribute.
- Presentation properties (and each sub-pick `prop__<name>_<key>`) are
  `history_tracked: true` — class `tracked` when bound to a tracked source,
  otherwise `constant`, never `slice_only` (§ Column temporal classes above). Each
  carries at least its genesis `history` row; a `tracked` one versions like any
  other tracked value.

There is no separate "projected emit", no sidecar `projection` block, and no
emit-level projection flag: presentation, when present, is just more
records-table columns the reader surfaces like any other. An exporter that wants
none reads only the mechanism columns; the contract never forces a presentation
column into an output.

---

## Related

| Document | Why |
|---|---|
| [`../../contract/base-format.md`](../../contract/base-format.md) + `.schema.json` | The normative emit shape this doc deliberately does not restate |
| [`reader.md`](reader.md) | The one read path; typed sidecar accessors incl. `record_roles` |
| [`conformance.md`](conformance.md) | C1–C13 and its boundaries (what validate does *not* prove) |
| [`anchor.md`](anchor.md) | Effective-anchor resolution; time rebasing |
| [`../CLAUDE.md`](../CLAUDE.md) | Boundary, principles, vocabulary |
