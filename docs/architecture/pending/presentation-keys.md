---
status: draft
---

# Presentation Keys

Adoption of the contract's `presentation_keys` block: a typed reader accessor,
the union-safety algebra, and claims-driven key declaration on exported tables.

---

## Problem

The vendored contract's sidecar now carries a `presentation_keys` block — per
kind, per minting declaration, the statically-derived key properties of every
`presentation_id` column (`unique_within` scope, `branch_stable` /
`slice_stable`, and a `key_space` identity), plus a kind-level rollup for
partitioned kinds and a normative union-safety algebra. Forge gates on the
version that defines it and C1 validates its shape — but nothing reads it.

Three gaps follow:

1. **Keys are asserted nowhere.** Every mode keys output on record identity —
   `record_id`, or base's `record_index`-derived `<kind>_key`, both forge-side
   surrogates. `presentation_id` — the operational, human-facing identifier a
   downstream consumer would naturally key on (`WARD_007`, an order number) —
   is carried as an unlabeled payload column even when the contract *proves*
   it unique and stable. An author landing a base or source export into a
   warehouse cannot know whether `presentation_id` is safe as a primary key,
   a merge key, or a join key without probing data — exactly the question the
   block exists to answer statically. Concretely: a DuckDB export of a
   `ward` kind today is `CREATE TABLE ward (ward_key BIGINT, id VARCHAR,
   presentation_id VARCHAR, ...)` with no constraint, and the author who asks
   "can I `MERGE ON presentation_id`?" gets no answer from the dataset.
2. **Claim coherence is judged nowhere.** C1 checks the block's JSON shape,
   but the block's normative consistency rules — kind membership agreeing
   with column presence, `sub_types` keys agreeing with the discriminator
   domain, the scalar claims agreeing with `key_space.class`, the rollup
   agreeing with the algebra — are checked by nothing. A consumer keying a
   table on an incoherent claim would build a wrong key silently.
3. **The stability guarantees go unused.** `slice_stable: true` is precisely
   the "incremental re-export cannot renumber" guarantee an incremental
   warehouse's natural-key merge needs; nothing surfaces it.

## Solution

Three layers, foundation-up, all schema knowledge flowing from the sidecar
(reader-first, Principle #10):

1. **Reader** — a typed `Sidecar.presentation_keys()` accessor mirroring the
   `record_roles()` / `sub_type_columns()` registry views: a frozen view of
   the block, verbatim carry, `None` when the block is absent ("no claims").
   Beside it, the contract's union-safety algebra as pure functions over the
   typed entries. Because no conformance check covers the block's semantic
   rules, the accessor is **strict on read**: an incoherent present block
   raises rather than yielding claims a consumer would key on.
2. **Key declaration** — a new opt-in `declare_keys` config field on the base
   and source modes. When on, each compiled output table carries declared key
   metadata (a primary key on the record-identity key the contract already
   guarantees unique; a uniqueness declaration on `presentation_id` exactly
   where the block claims it), and the DuckDB writer materializes those
   declarations as real `PRIMARY KEY` / `UNIQUE` constraints. Off (the
   default), output is byte-identical to today.
3. **Advisory surfaces** — dimensional `init` annotates its per-kind stubs
   with the claimed natural key, and the incremental driver's windowed DuckDB
   path declares keys on table creation where the windowed write regime
   preserves them.

```
sidecar presentation_keys ──▶ PresentationKeys view ──▶ mode plan (base / source)
                                    │   ▲ strict-on-read           │
                union-safety algebra┘                              ▼
                                                    QuerySpec.keys ──▶ DuckDB DDL
```

## Affected Subsystems

- **Reader** — gains the `PresentationKeys` typed view (with `PartitionKey`,
  `KeySpace`, and `WholeColumnClaim` value types), the
  `Sidecar.presentation_keys()` accessor, the union-safety algebra
  (`union_safe`, `combined_claim`), and a new `PresentationKeysInvalidError`.
  Unlike the sibling registry views, construction validates the block's
  semantic consistency (see Semantics § Strict-on-read) — there is no
  conformance check to defer to.
- **Config** — `BaseConfig` and `SourceConfig` gain an optional boolean
  `declare_keys`. No other envelope changes.
- **Shared exporter compile shape** — `QuerySpec` gains optional declared-key
  metadata (`TableKeys`); the shared write dispatch threads it to the DuckDB
  writer (signature unchanged — flattening `spec.keys` beside `spec.sql` is
  its only new behavior). The `keys-not-declarable-csv` notice is emitted by
  the mode's full-export entry path and by each incremental driver
  invocation — the layers that hold both `declare_keys` and the resolved
  format, and that already carry the required `notice_sink`; neither the
  compiles, the dispatch, nor the writers gain one.
- **Writers (DuckDB)** — gains a constraint-carrying creation path: a table
  whose spec declares keys is created with explicit DDL (column types from
  the Arrow schema, `PRIMARY KEY` / `UNIQUE` constraints) and loaded by
  insert, instead of bare `CREATE TABLE AS`. A constraint violation during
  load is a loud export failure naming the table.
- **Base mode** — under `declare_keys`, each flat table declares its
  `<kind>_key` primary key and `id` uniqueness (both contract-guaranteed),
  plus `presentation_id` uniqueness when the block claims it for the whole
  kind.
- **Source mode** — under `declare_keys`, reference and transaction tables
  declare an `id` primary key, plus `presentation_id` uniqueness per the
  whole-kind claim (unsplit kinds) or the per-sub-type entry (split units).
  Change-log (`changelog` delivery) and junction tables declare nothing;
  `snapshot` delivery's per-kind state tables declare like reference tables
  (see Semantics).
- **Dimensional `init`** — when the block carries a claim for a proposed
  kind, the stub gains an advisory comment naming `presentation_id` as the
  contract-declared natural key and its scope. No config grammar change.
- **Incremental driver** — under `declare_keys`, the windowed DuckDB path
  declares keys at table creation for exactly the tables whose write regime
  preserves them (see Semantics § Incremental interplay).
- **Notice channel** — one new code, `keys-not-declarable-csv`.

## What Doesn't Change

- **Streaming and Kafka keying.** Message keying stays `record_id`; the
  streaming mode reads none of this. Deliberate: `record_id` keying is a
  correctness choice (always present, always stable), not a gap.
- **`fabulexa-forge validate` and the C-set.** Conformance remains the
  published C1–C14, reimplemented verbatim — forge does not invent a C15.
  The contract defines no check for the block's semantic rules, docs' C-ID
  citations must resolve in `contract/`, and the conformance suite is
  documented as deliberately narrower than the producer's QA. Enforcement
  lives in the accessor instead, at the moment claims are about to be used.
- **Claims are never validated against data.** The contract makes data
  validation optional and forge declines it everywhere: no mode probes
  `presentation_id` values to confirm a claim. The declared-constraint path
  is the one place a false claim surfaces — as the DuckDB constraint
  violation the author opted into.
- **Corrupter composition.** `declare_keys` defaults off, so the
  test-guarded corrupt→base and corrupt→source compositions are untouched:
  a corrupted emit's defects surface unchanged. The corrupter's base-emit
  writer carries the block through verbatim, as it does every sidecar
  registry; a corruption that falsifies a claim (a duplicated row under a
  claimed-unique key) is deliberate semantic non-conformance the contract
  itself anticipates. Verbatim carry also means a structural corruption — a
  `schema_drift` renaming or dropping a `presentation_id` column — can leave
  the carried block incoherent against the drifted catalog, the same
  verbatim-staleness posture drift already imposes on every copied registry;
  the strict accessor then refuses such an emit exactly on the
  claim-consuming paths (`declare_keys`, `init`), while exports that ignore
  claims are untouched. `defects.json` gains no new impact vocabulary — its
  vocabulary is the C-set, and no C-ID covers the block, so this staleness is
  inherently manifest-invisible.
- **Foreign keys.** `declare_keys` declares within-table keys only — no
  `FOREIGN KEY` constraints. Referential declarations are a distinct
  capability with hazards of their own: the incremental replace regime
  rewrites parent snapshots (reference tables) under their children's
  persisted rows each window, and an `exclude`-restricted extract legally
  drops FK targets. Deferred until demand appears (Principle #8).
- **CSV output shape.** CSV carries no constraint surface; under
  `declare_keys` + CSV the data is identical and a notice records the
  undeliverable declaration.
- **Dimensional export grammar.** Authors declare dimensional keys
  themselves; only `init`'s advisory comments change.
- **The `<kind>_key` / `<p>_key` record-index key columns.** Base's key
  columns, their derivation, naming, and horizon binding are untouched; the
  block adds declarations *about* columns, never columns.
- **Reserved names, `slice_only` policy, anchor resolution, playback.**

## Semantics

### The typed view

`Sidecar.presentation_keys()` returns `None` when the sidecar has no
`presentation_keys` key — "no claims", the same absence posture as the sibling
registries — and a `PresentationKeys` view otherwise. The view is a verbatim
carry: nothing is inferred, nothing re-derived from data, entry order is the
sidecar's (contract-guaranteed lexicographic). A partitioned kind's declared
sub-type set is never narrowed to sub-types with surviving rows
(slice-stable, matching `sub_type_columns`).

| Sidecar state | `presentation_keys()` result |
|---|---|
| No `presentation_keys` key | `None` |
| Present, coherent | `PresentationKeys` view |
| Present, incoherent (any clause below) | `PresentationKeysInvalidError` |

### Strict-on-read

Construction (lazy, on first call) verifies the block's normative consistency
rules. The sibling registries parse leniently because C12/C14 own their
diagnosis; no check owns this block's, and a silently-mended block would feed
wrong keys downstream — so the accessor refuses instead. Each clause names
the kind (and sub-type) that violates it:

| Clause | Rule |
|---|---|
| Kind membership (both directions) | A kind appears in the block **iff** its declared `records__<kind>` table carries a `presentation_id` column |
| Entry shape | `sub_types` entry iff the kind carries a synthesized `<kind>_type` discriminator domain in `enum_domains`; `key` entry iff not |
| Sub-type domain | `sub_types` keys ⊆ the kind's discriminator domain |
| Scalar–key-space coupling | `unique_within` and the stability pair equal the values `key_space.class` determines (`counter` → `emit`/`false`/`false`; every other class → `branch`/`true`/`true`) |
| Key-space shape | `prefix`/`width` present iff the class is digit-rendered (`counter`/`record_index`) |
| Rollup consistency | A partitioned kind's rollup equals `combined_claim` over its entries (including an omitted `unique_within` exactly when the algebra derives no claim) |

An error is raised only when the block is *present and incoherent*; absence
never raises. A defective block therefore surfaces exactly on the paths that
consult claims (`declare_keys`, `init`), never on an export that ignores them.

### The union-safety algebra

`union_safe` and `combined_claim` implement the contract's normative tables
verbatim — pairwise safety over `key_space` identities (identical-`prefix`/
`width` `record_index` pairs safe; `uuid`×`uuid` safe; `record_id`×`record_id`
safe; digit-rendered pairs safe iff prefixes incomparable, where P₁, P₂ are
comparable iff one equals the other plus a possibly-empty digit string; every
cross-family pair unsafe) and the combined-set derivation (all-counter →
`emit`/`false`/`false`; all-stable → `branch`/`true`/`true`; mixed →
`branch`/`false`/`false`; any-pair-unsafe → no uniqueness claim, stability
pair `true`/`true` iff every member stable). They are kind-scoped, as the
contract scopes them; no cross-kind call is meaningful and none is provided.
Their one consumer today is the rollup-consistency clause; they are public
because they state contract-normative behavior tests must exercise directly.

### Key resolution per output table

Key declaration is resolved at plan time, before any data is written, from
the sidecar alone. The record-identity keys need no claim — `record_id`
uniqueness per kind and `record_index` density are contract guarantees — so
they are declared whenever `declare_keys` is on; `presentation_id`
declarations require a claim. Under the single-branch guard (C8), a
`unique_within` of `"branch"` and `"emit"` are equally table-wide, so both
scopes yield a declaration; the distinction is not surfaced.

| Mode · table | Primary key | Unique | Claim source |
|---|---|---|---|
| base · per-kind flat table | `<kind>_key` (post-`rename` name) | `id`; `presentation_id` iff claimed | Flat kind: `key` entry. Partitioned kind: the rollup's `unique_within` (absent rollup claim → no declaration) |
| source · reference / transaction (unsplit) | `id` | `presentation_id` iff claimed | Same whole-table rule as base |
| source · split unit (per sub-type) | `id` | `presentation_id` iff that sub-type's entry exists | `key_for(kind, sub_type)` — the entry's presence *is* the claim (declared partitions are total non-NULL) |
| source · change-log (`change_delivery: changelog`) | none | none | Multiple rows per record; the only candidate composite includes a rendered wallclock `TIMESTAMP` whose microsecond precision can collide distinct nanosecond events — no honest key exists post-render |
| source · change-log snapshot (`change_delivery: snapshot`) | `id` | `presentation_id` iff claimed | Same whole-table rule as base — one row per record at the horizon (tape's end in full export, the window horizon under incremental), so the changelog rationale above does not apply. Tracked kinds are never sub-type split, so the per-sub-type rule never arises here |
| source · junction | none | none | The block speaks only to `presentation_id` on records kinds; membership rows carry no claimed key |

A kind absent from the block (legally — its column never minted, or the block
absent entirely) declares identity keys only. `presentation_id` uniqueness is
declared as a `UNIQUE` constraint, never a primary key: the claims range over
non-NULL cells and SQL `UNIQUE` ignores NULLs — the same semantics — whereas
`PRIMARY KEY` would reject the NULLs a partitioned kind's undeclared
sub-types legitimately carry.

| Condition | Result |
|---|---|
| `declare_keys` absent or false | No key metadata compiled; output identical to today |
| `declare_keys: true`, fmt `duckdb` | Constraints in the output DDL |
| `declare_keys: true`, fmt `csv` | Data unchanged; one `keys-not-declarable-csv` notice per export invocation, before any data is written (a `--next` drip re-emits it each invocation — the compile-notice rule) |
| `declare_keys: true`, block absent | Identity keys declared; no `presentation_id` declarations; no notice (absence is "no claim", not a defect) |
| `declare_keys: true`, block present and incoherent | `PresentationKeysInvalidError` at plan time, before any output |
| `declare_keys: true` over an emit whose data falsifies a declared key (e.g. corrupter-duplicated rows) | The DuckDB load fails loudly naming the table — the author opted into enforcement, and a silent constraint drop would misdescribe the dataset |

`keys-not-declarable-csv` is emitted where `declare_keys` meets a resolved
`csv` format: the mode's full-export entry path, and each incremental driver
invocation. Both already carry the required `notice_sink`; the compiles, the
shared dispatch, and the writers gain none. The compiles stay
format-agnostic — which is also why key resolution, and the strict accessor
with it, runs whatever the format: an incoherent block raises at plan time
under CSV too.

### Writer semantics

A `QuerySpec` carrying `TableKeys` is materialized as: create the table with
explicit column DDL (names and types read from the materialized Arrow
schema — the writer stays schema-ignorant of modes; it transcribes what the
relation already is) plus the declared constraints, then load by insert. A
spec without keys keeps today's `CREATE TABLE AS` path byte-for-byte. Row
counts, empty-table emission, and the fresh-output-connection rule are
unchanged. Constraint names are DuckDB defaults; forge names nothing.

### Incremental interplay

Under `declare_keys`, keys are declared at first-window table creation only
where the write regime preserves the constraint across windows: replace-class
tables trivially (each window rewrites the whole table inside one
transaction), append-class tables only where a row lands in exactly one
window and is final. The gating is the windowed compile's — it sets
`QuerySpec.keys` per the table below; the writer consumes, never decides.

| Windowed table class | Write regime | Declared |
|---|---|---|
| base per-kind flat table | replace — full state-at snapshot per window | Same as full export |
| source reference / split unit | replace — full current-state snapshot per window | Same as full export |
| source change-log, `change_delivery: snapshot` | replace — state-at-horizon per window | Per the snapshot row (§ Key resolution) |
| source transaction | append — `last_mutation_sim_time` lands a row in exactly one window, final | Same as full export |
| source change-log (`changelog`), junction | append — multiple rows per record; a closed interval re-emits | none (as full export) |
| dimensional (type-1, SCD-2, facts) | — | n/a — dimensional carries no `declare_keys` |

A false claim under incremental surfaces as a rolled-back window: the
constraint violation aborts the window's transaction under the windowed
writer's existing atomicity rule, leaving the warehouse exactly as before.

The cursor and fingerprint are unaffected; `declare_keys` participates in the
config fingerprint exactly as any other config field does.

### `init` advisory

When the emit's block carries a whole-table claim for a proposed kind (flat
`key`, or a partitioned rollup with a `unique_within`), the kind's stub gains
one comment naming `presentation_id` as the contract-declared natural key.
No claim, no comment. `init` consults the accessor and therefore shares its
strict-on-read behavior.

### Invariants

- **Determinism.** Key resolution is a pure function of (sidecar, config);
  no data participates. Same emit + config + code → identical declarations.
- **Claims are read, never invented.** No declaration exists without either
  a contract guarantee (identity keys) or a block claim (`presentation_id`).
  Absence of a claim degrades to absence of a declaration, never to probing.
- **Declarations never change data.** Under any `declare_keys` value the
  rows, columns, ordering, and typing of every output are identical; only
  DDL differs. (Corollary: the tier-2 playback bridging equivalence is
  untouched — playback compiles relations, not DDL.)
- **Strictness is use-scoped.** An incoherent block fails exactly the
  operations that would consume it, and no others.

## Configuration

```yaml
# Base export with declared keys
mode: base
base:
  declare_keys: true
```

```yaml
# Source export with declared keys
mode: source
source:
  change_delivery: changelog
  declare_keys: true
```

| Field | Type | Required | Description |
|---|---|---|---|
| `base.declare_keys` | bool | No (absent = off) | Declare primary-key / uniqueness constraints on DuckDB output, from contract guarantees and `presentation_keys` claims |
| `source.declare_keys` | bool | No (absent = off) | Same, for the source mode's per-genre rule |

## Interface Contracts

### Runtime Types (reader)

```python
@dataclass(frozen=True)
class KeySpace:
    """A minting declaration's key-space identity, verbatim from the sidecar.

    `space_class` carries the sidecar field `class` (a Python keyword).
    `prefix` and `width` are present iff the class is digit-rendered
    ('counter' / 'record_index') — the contract's presence rule, mirrored as
    None-ness rather than sentinel values.
    """

    space_class: Literal["counter", "record_index", "uuid", "record_id"]
    prefix: str | None
    width: int | None
```

```python
@dataclass(frozen=True)
class PartitionKey:
    """One minting declaration's key claims, scoped to its partition.

    All claims range over the partition's cells, which the contract declares
    total non-NULL (a declared partition has no NULL `presentation_id`).
    """

    unique_within: Literal["emit", "branch"]
    branch_stable: bool
    slice_stable: bool
    key_space: KeySpace
```

```python
@dataclass(frozen=True)
class WholeColumnClaim:
    """A whole-column key claim: a kind rollup, or an algebra-derived union.

    unique_within is None when no uniqueness claim is derivable — "no
    claim", never "not unique".
    """

    unique_within: Literal["emit", "branch"] | None
    branch_stable: bool
    slice_stable: bool
```

```python
@dataclass(frozen=True)
class PresentationKeys:
    """Typed view of the sidecar `presentation_keys` registry.

    Verbatim carry of the per-kind key-claim block: per minting declaration
    (per sub-type for partitioned kinds, a single entry for flat kinds), the
    key scalars and key-space identity, plus the kind rollup for partitioned
    kinds. Constructed only from a coherent block — `Sidecar.
    presentation_keys()` raises rather than yield an incoherent view. Built
    from the sidecar; never re-exported from a producer type.
    """

    def kinds(self) -> tuple[str, ...]:
        """The kinds carrying claims, in sidecar (lexicographic) order.

        Returns:
            Kind names, verbatim order.
        """

    def is_partitioned(self, kind: str) -> bool:
        """Whether a kind's entry is per-sub-type (`sub_types`) or flat (`key`).

        Args:
            kind: A kind present in the block.

        Returns:
            True iff the kind's entry carries `sub_types`.

        Raises:
            KeyError: `kind` is not in the block.
        """

    def key(self, kind: str) -> PartitionKey:
        """A flat kind's single declaration — the whole-column claim.

        Args:
            kind: A kind present in the block.

        Returns:
            The `key` entry's claims.

        Raises:
            KeyError: `kind` is not in the block.
            ValueError: `kind` is partitioned (read `key_for` / rollup
                instead).
        """

    def sub_types(self, kind: str) -> tuple[str, ...]:
        """A partitioned kind's declared (minting) sub-types, sidecar order.

        Every sub-type whose declaration mints, zero-row partitions included;
        never narrowed to sub-types with surviving rows.

        Args:
            kind: A kind present in the block.

        Returns:
            Sub-type names, verbatim order.

        Raises:
            KeyError: `kind` is not in the block.
            ValueError: `kind` is flat and has no enumerable sub-types.
        """

    def key_for(self, kind: str, sub_type: str) -> PartitionKey:
        """A partitioned kind's per-sub-type declaration.

        Presence is itself a claim: every row of this sub-type carries a
        non-NULL `presentation_id`.

        Args:
            kind: A kind present in the block.
            sub_type: A declared sub-type of `kind`.

        Returns:
            That sub-type's claims.

        Raises:
            KeyError: `kind` is not in the block, or `sub_type` is not among
                its declared entries (an undeclared sub-type mints nothing —
                its cells are NULL, and it carries no claims).
            ValueError: `kind` is flat.
        """

    def whole_table_claim(self, kind: str) -> WholeColumnClaim:
        """The whole-column claim for a kind, whatever its entry shape.

        The one method a consumer keying a whole-kind table reads: a flat
        kind's `key` scalars, a partitioned kind's rollup.

        Args:
            kind: A kind present in the block.

        Returns:
            The whole-column claim; `unique_within` None when the rollup
            derives no claim.

        Raises:
            KeyError: `kind` is not in the block.
        """
```

### Functions (reader)

```python
def presentation_keys(self) -> PresentationKeys | None:
    """The sidecar `presentation_keys` registry as a typed view.

    Method on `Sidecar`, sibling of `record_roles()` / `sub_type_columns()`.
    Verbatim carry; nothing inferred. Unlike its siblings the parse is
    strict: no conformance check owns this block's semantic rules, and a
    mended block would feed wrong keys to consumers, so an incoherent
    present block refuses rather than degrades.

    Returns:
        The typed view, or None when the sidecar carries no
        `presentation_keys` key ("no claims").

    Raises:
        PresentationKeysInvalidError: The block is present and violates a
            consistency clause (kind membership vs `presentation_id` column
            presence, entry shape vs discriminator domain, sub-type keys
            outside the domain, scalars inconsistent with `key_space.class`,
            key-space presence-rule violation, or rollup inconsistent with
            the union algebra) — the message names the kind, sub-type, and
            clause.
    """
```

```python
def union_safe(
    a: KeySpace,
    b: KeySpace,
) -> bool:
    """Whether two key spaces of one kind are union-safe.

    The contract's normative pairwise algebra: a value collision must be
    impossible given only the declarations. Kind-scoped — callers must not
    pass entries of different kinds (the spaces make no cross-kind claim,
    and the function cannot detect the misuse).

    Args:
        a: One declaration's key space.
        b: Another declaration's key space, same kind.

    Returns:
        True iff the pair is union-safe per the contract's table (shared
        injective `record_index` space; independent `uuid` draws; verbatim
        `record_id`; digit-rendered pairs with incomparable prefixes).
    """
```

```python
def combined_claim(
    entries: Sequence[PartitionKey],
) -> WholeColumnClaim:
    """The whole-column claim for a union of one kind's partitions.

    The contract's combined-set derivation: pairwise-unsafe sets carry no
    uniqueness claim; otherwise all-counter → 'emit', all-stable →
    'branch', mixed → 'branch'; the stability pair is true/true iff every
    member is stable-class. A singleton set's claim equals its entry's
    scalars.

    Args:
        entries: One kind's declarations (any subset, one or more).

    Returns:
        The union's claim.

    Raises:
        ValueError: `entries` is empty — an empty union has no claim to
            state and a caller reaching it holds a logic error.
    """
```

### Runtime Types (shared exporter shape)

```python
@dataclass(frozen=True)
class TableKeys:
    """Declared key metadata for one compiled output table.

    Column names are post-`rename` output names. Carried by `QuerySpec`;
    materialized as constraints by the DuckDB writer, reported as
    undeliverable by the CSV dispatch.

    A table with nothing to declare carries `QuerySpec.keys = None`, never
    an empty `TableKeys`: every constructed instance has a non-empty
    `primary_key` (the resolution table always yields one), while `unique`
    may be empty (no block claim → identity keys only).
    """

    primary_key: tuple[str, ...]
    unique: tuple[tuple[str, ...], ...]
```

`QuerySpec` gains a `keys: TableKeys | None` field; every existing compile
path sets it to `None` (full compatibility by construction — a `None`-keyed
spec writes exactly as today). The shared dispatch's (`write_query_specs`)
signature is unchanged: its DuckDB arm flattens `spec.keys` into
`write_duckdb`'s `keys` mapping beside the existing name → SQL flattening;
its CSV arm ignores keys (the notice belongs to the export entry path, not
the dispatch).

### Functions (writers)

```python
def write_duckdb(
    emit: "Emit",
    queries: dict[str, str],
    output_path: Path,
    keys: Mapping[str, TableKeys],
) -> dict[str, int]:
    """Materialize each query into a new DuckDB file, declaring keys.

    Unchanged Arrow materialization path. A table named in `keys` is created
    with explicit column DDL (names/types transcribed from its Arrow
    schema) plus the declared PRIMARY KEY / UNIQUE constraints, then loaded
    by insert; a table absent from `keys` keeps the CREATE TABLE AS path.
    An empty mapping reproduces today's behavior exactly.

    Args:
        emit: The open (read-only) emit; queried via Emit.query_arrow.
        queries: Mapping of output table name -> SELECT SQL.
        output_path: Output .duckdb file path to create.
        keys: Declared keys per table name; tables without declarations are
            simply absent. Names must be a subset of `queries`' names.

    Returns:
        Mapping of every table name -> row count (0 for an empty table).

    Raises:
        ExportRuntimeError: DuckDB creation or table load fails — including
            a declared-constraint violation, reported naming the table.
        ValueError: `keys` names a table absent from `queries`.
    """
```

`write_duckdb_window`'s signature is unchanged — it already receives
`list[QuerySpec]` and reads `spec.keys` on its create-if-missing path only:
a keyed spec's table is created with the same explicit-DDL-plus-insert path,
the append and replace paths untouched (constraints created at the first
window persist, and DuckDB enforces them on every later insert). A constraint
violation in any window is the same `ExportRuntimeError`, rolled back
atomically under the windowed writer's existing transaction rule.

### Errors

```python
class PresentationKeysInvalidError(ReaderError):
    """The sidecar's presentation_keys block is present but incoherent.

    Raised by Sidecar.presentation_keys() naming the kind (and sub-type)
    and the violated clause. Absence of the block never raises.
    """
```

## Validation Rules

### Parse-Time (Pydantic)

- `BaseConfig.declare_keys` / `SourceConfig.declare_keys`: optional boolean;
  absent means off. No cross-field rule — `declare_keys` composes with
  `slice_at`, `change_delivery`, `exclude`, `rename`, and `incremental`
  without restriction (key resolution runs after renames, on output names).

### Business Rules

| Rule | Checks | Error / notice |
|---|---|---|
| Strict accessor | The six coherence clauses (Semantics § Strict-on-read) | `PresentationKeysInvalidError` naming kind, sub-type, clause |
| CSV declaration | `declare_keys` on a CSV delivery cannot be materialized | Notice `keys-not-declarable-csv`, once per invocation (a `--next` drip re-emits), before data; emitted by the export entry path / driver invocation, never the compiles, dispatch, or writers |
| Writer keys subset | `keys` mapping names only compiled tables | `ValueError` (a caller bug, not an author error) |
