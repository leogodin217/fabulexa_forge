# Sprint: presentation-keys

## Purpose

Adopt the contract's `presentation_keys` block end-to-end: a strict typed reader
accessor plus the union-safety algebra, an opt-in `declare_keys` on the base and
source modes that materializes contract-proven keys as real DuckDB
`PRIMARY KEY` / `UNIQUE` constraints, and the advisory surfaces (`init`
natural-key comment, incremental first-window declaration, the
`keys-not-declarable-csv` notice). An author landing a base or source export in
a warehouse can then answer "can I `MERGE ON presentation_id`?" from the
dataset itself instead of probing data.

**Design doc:** `docs/architecture/pending/presentation-keys.md` — the WHY and
full semantics live there; this spec carries contracts, phases, and tests. The
Semantics § Key resolution per output table and § Incremental interplay tables
are normative for every resolution decision below.

## Scope

**Capabilities touched:**
- reader: `PresentationKeys` typed view (+ `KeySpace`, `PartitionKey`,
  `WholeColumnClaim`), strict `Sidecar.presentation_keys()` accessor,
  `union_safe` / `combined_claim`, `PresentationKeysInvalidError`
- shared exporter shape: `TableKeys`, `QuerySpec.keys`, DuckDB writer
  explicit-DDL-plus-insert constraint path, `write_query_specs` threading
- base mode: `BaseConfig.declare_keys` + per-flat-table key resolution
- source mode: `SourceConfig.declare_keys` + per-genre key resolution
- incremental: `keys-not-declarable-csv` per driver invocation; windowed
  first-window constraint creation (falls out of the mode compiles + writer)
- dimensional `init`: advisory natural-key comment per claimed kind
- notice channel: one new code, `keys-not-declarable-csv`

**Not included** (design doc § What Doesn't Change): streaming/Kafka keying
(stays `record_id`), any new conformance check (no C15), data-probing of
claims, `FOREIGN KEY` constraints, corrupter changes (verbatim carry already
holds), dimensional `declare_keys`, CSV output-shape changes, Parquet.

## Breaking Changes

Internal only; no author-facing config or output changes when `declare_keys`
is absent.

- **`write_duckdb` gains a required `keys: Mapping[str, TableKeys]` parameter**
  (no default — contract rule). Call sites: the `write_query_specs` dispatch
  (updated in the same phase) and `tests/writers/test_duckdb.py` (migrated in
  the same phase, passing `{}`). An empty mapping reproduces today's behavior
  exactly.
- **`QuerySpec` gains `keys: TableKeys | None = None`.** A benignly-defaulted
  *internal runtime* field (permitted — not an author config field), so every
  existing `QuerySpec(...)` construction (dimensional/source/base engines,
  playback, tests) compiles unchanged and writes exactly as today.
- **`BaseConfig.at_least_one_field` / `SourceConfig.at_least_one_field`**
  validator messages extend to name `declare_keys`; a section setting only
  `declare_keys: true` becomes valid (the field counts as set). Existing
  configs are unaffected.

## Success Criteria

- [ ] `Sidecar.presentation_keys()` returns `None` on absence, a verbatim
      typed view on a coherent block, and raises `PresentationKeysInvalidError`
      naming kind/sub-type/clause on each of the six coherence violations.
- [ ] `union_safe` / `combined_claim` reproduce the contract's normative
      tables (contract § The `presentation_keys` block), including prefix
      comparability edge cases (`A-`/`A-1`, `""`/`"1"`, `WARD_`/`THTR_`).
- [ ] `mode: base` + `declare_keys: true` + `duckdb` yields, per kind table:
      `<kind>_key` PRIMARY KEY, `id` UNIQUE, and `presentation_id` UNIQUE
      exactly when claimed; `declare_keys` off → byte-identical output to today.
- [ ] `mode: source` + `declare_keys: true` declares per the genre table
      (reference/transaction/split-unit/snapshot declare; changelog and
      junction never).
- [ ] `declare_keys` + CSV: data unchanged, one `keys-not-declarable-csv`
      notice per invocation, before data.
- [ ] A falsified claim (duplicated `presentation_id`) fails the DuckDB load
      loudly naming the table; under incremental the window rolls back leaving
      the warehouse untouched.
- [ ] `init` stubs gain the advisory comment iff the kind carries a
      whole-table claim.
- [ ] Full suite green; `declare_keys` absent leaves every existing test
      untouched in behavior.

## Contracts

The reader types and functions (`KeySpace`, `PartitionKey`,
`WholeColumnClaim`, `PresentationKeys`, `Sidecar.presentation_keys()`,
`union_safe`, `combined_claim`, `PresentationKeysInvalidError`), the shared
shape (`TableKeys`, the `QuerySpec.keys` field, dispatch threading), and
`write_duckdb` / `write_duckdb_window` semantics are specified **verbatim** in
the design doc § Interface Contracts — implement those signatures and
docstrings as written; do not re-derive them. Placement:

- `KeySpace`, `PartitionKey`, `WholeColumnClaim`, `PresentationKeys`,
  `union_safe`, `combined_claim` → `src/fabulexa_forge/reader/sidecar.py`
  (beside the sibling `RecordRoles` / `SubTypeColumns` views); all exported
  from `fabulexa_forge.reader`.
- `PresentationKeysInvalidError(ReaderError)` → `src/fabulexa_forge/reader/errors.py`.
- `TableKeys` → `src/fabulexa_forge/exporters/query_spec.py` beside `QuerySpec`.

Contracts new to this spec (not in the design doc) follow.

### Shared notice helper (`exporters/query_spec.py`)

```python
NOTICE_KEYS_NOT_DECLARABLE_CSV: str
"""The notice code 'keys-not-declarable-csv'."""


def keys_not_declarable_csv_notice() -> Notice:
    """The one notice a declare_keys-under-CSV invocation emits.

    Shared by the base and source full-export entry paths and the incremental
    driver so all three emit an identical, deterministic message: CSV carries
    no constraint surface, the data is unchanged, and the declaration is
    dropped for this invocation.

    Returns:
        A Notice with code NOTICE_KEYS_NOT_DECLARABLE_CSV and a fully
        rendered, self-contained message.
    """
```

### Base key resolution (`exporters/base/plan.py`)

```python
def resolve_base_table_keys(
    sidecar: "Sidecar",
    spec: BaseTableSpec,
) -> TableKeys:
    """Resolve one base flat table's declared keys from the sidecar alone.

    Pure plan-time resolution (design doc § Key resolution per output table,
    'base' row); the engine calls it only when `declare_keys` is on. The
    primary key is the record-index self key's post-`rename` output name
    (`column_renames['record_index']`); `unique` always contains the
    record-id column's output name (`column_renames['record_id']`), plus the
    `presentation_id` column's output name iff the block claims whole-column
    uniqueness for the kind: a flat kind's `key` entry (every entry carries a
    `unique_within`), or a partitioned kind's rollup with a non-None
    `unique_within`. A kind absent from the block, or an absent block,
    yields identity keys only. `unique_within` scope ('emit' vs 'branch') is
    not surfaced — both are table-wide under the single-branch guard.

    Args:
        sidecar: The open emit's sidecar (claims read via
            `sidecar.presentation_keys()` — strict-on-read applies).
        spec: The resolved table spec (post-rename names in
            `spec.column_renames`).

    Returns:
        The table's declared keys (never None — the base primary key is a
        contract guarantee, claim or no claim).

    Raises:
        PresentationKeysInvalidError: The sidecar block is present and
            incoherent (propagated from the accessor; plan-time, before any
            output).
    """
```

### Source key resolution (`exporters/source/plan.py`)

```python
def resolve_source_table_keys(
    sidecar: "Sidecar",
    spec: SourceTableSpec,
    change_delivery: Literal["changelog", "snapshot"],
) -> TableKeys | None:
    """Resolve one source output table's declared keys, or None for its genre.

    Pure plan-time resolution (design doc § Key resolution per output table,
    'source' rows); the engine calls it only when `declare_keys` is on.
    Genre rule:

    - junction → None (membership rows carry no claimed key).
    - changelog genre under `change_delivery: 'changelog'` → None (multiple
      rows per record; no honest key exists post-render).
    - changelog genre under `change_delivery: 'snapshot'` → whole-table rule
      (one row per record at the horizon; tracked kinds never sub-type
      split).
    - reference / transaction, unsplit (`spec.sub_type is None`) → primary
      key on the record-identity (`id`) column's output name; unique on
      `presentation_id`'s output name iff the whole-table claim holds (flat
      `key` entry, or partitioned rollup with non-None `unique_within`).
    - reference / transaction, split unit (`spec.sub_type` set) → primary
      key on `id`; unique on `presentation_id` iff `key_for(kind, sub_type)`
      exists — the entry's presence is the claim.

    Output names are read from `spec.columns` (source → output pairs), so
    renames are honored. A kind absent from the block declares identity keys
    only.

    Args:
        sidecar: The open emit's sidecar (claims via
            `sidecar.presentation_keys()` — strict-on-read applies).
        spec: The resolved output table spec.
        change_delivery: The mode's change-log delivery, deciding the
            changelog-genre rule.

    Returns:
        The table's declared keys, or None when the genre declares nothing.

    Raises:
        PresentationKeysInvalidError: The block is present and incoherent
            (propagated; plan-time, before any output).
    """
```

### Behavioral changes to existing functions (no signature changes)

- `build_base_query_specs` / `build_source_query_specs`: when the mode
  section's `declare_keys` is true, set `QuerySpec.keys` from the resolution
  functions above (format-agnostic — resolution and the strict accessor run
  whatever the format, so an incoherent block raises at plan time under CSV
  too; and window or not — the windowed declarations equal the full-export
  ones per the design doc § Incremental interplay table, the append/replace
  regimes preserving every declared constraint). Otherwise `keys=None` on
  every spec.
- `export_base` / `export_source`: when `declare_keys` is true and
  `fmt == 'csv'`, emit `keys_not_declarable_csv_notice()` to `notice_sink`
  once, before any data is written.
- `export_window` (incremental driver): same notice, once per invocation,
  before any data, when `declare_keys` (of the mode section in play) meets
  `fmt == 'csv'`. Emitted here — never in the compiles, the dispatch, or the
  writers.
- `write_query_specs`: signature unchanged; its DuckDB arm flattens
  `spec.keys` into `write_duckdb`'s `keys` mapping beside the existing
  name → SQL flattening; its CSV arm ignores keys.
- `write_duckdb_window` (signature unchanged): on its create-if-missing path
  only, a keyed spec's table is created via the same explicit-DDL-plus-insert
  path; append and replace paths untouched. A constraint violation in any
  window raises `ExportRuntimeError` and rolls back atomically.
- `BaseConfig` / `SourceConfig`: gain `declare_keys: bool | None = None`
  (absent = off; Pydantic-optional author field whose absence is a semantic
  default "off", mirroring `slice_at` — not an invented mapping value). The
  `at_least_one_field` validators and messages extend to include it. No
  cross-field rule (design doc § Validation Rules).
- `generate_init_config` / dim stub writers (`exporters/dimensional/init.py`):
  when the emit's block carries a whole-table claim for a proposed kind (flat
  `key`, or partitioned rollup with non-None `unique_within`), the kind's
  stub gains one advisory comment line naming `presentation_id` as the
  contract-declared natural key and its scope. No claim (or no block) → no
  comment. `init` consults the accessor and shares its strict-on-read
  behavior.
- Incremental fingerprint: no code change — `declare_keys` participates via
  the existing config-fingerprint mechanism exactly as any other config
  field. Verified by test, not by new code.

## Phases

### Phase 1: Reader — typed view, strict accessor, union algebra

**Delivers:** `KeySpace`, `PartitionKey`, `WholeColumnClaim`,
`PresentationKeys`, `Sidecar.presentation_keys()` (lazy strict-on-read over
the six coherence clauses), `union_safe`, `combined_claim`,
`PresentationKeysInvalidError`; all exported from `fabulexa_forge.reader`.
**Demo:** Builds a minimal emit (tempdir) whose sidecar carries a flat kind
and a partitioned kind; prints the typed claims, algebra verdicts for
safe/unsafe key-space pairs, and shows an incoherent block raising with
kind/clause named.
**Contracts:** Design doc § Runtime Types (reader), § Functions (reader),
§ Errors — verbatim.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/sidecar.py` |
| Modify | `src/fabulexa_forge/reader/errors.py` |
| Modify | `src/fabulexa_forge/reader/__init__.py` |
| Create | `tests/reader/test_presentation_keys.py` |
| Create | `docs/sprints/presentation-keys/demos/phase_1_reader_view.py` |

**Tests** (`tests/reader/test_presentation_keys.py`, raw-sidecar-dict helpers
in the `test_sidecar.py` style):
- Absent `presentation_keys` key → `presentation_keys()` returns `None`; never raises.
- Coherent flat kind: `kinds()` order is sidecar order; `is_partitioned` False;
  `key()` returns the entry's scalars verbatim; `sub_types`/`key_for` raise `ValueError`.
- Coherent partitioned kind: `is_partitioned` True; `sub_types()` verbatim
  (zero-row sub-types retained); `key_for` returns per-sub-type claims;
  undeclared sub-type → `KeyError`; `key()` raises `ValueError`.
- `whole_table_claim`: flat kind → key scalars; partitioned → rollup;
  rollup with omitted `unique_within` → `unique_within is None`.
- Unknown kind → `KeyError` from every kind-taking method.
- Each of the six coherence clauses violated in isolation raises
  `PresentationKeysInvalidError` naming the kind (and sub-type) and clause:
  (a) kind in block without `presentation_id` column, (b) kind with
  `presentation_id` column absent from block, (c) `key` entry on a
  discriminator-bearing kind / `sub_types` entry on a flat kind,
  (d) `sub_types` key outside the discriminator domain, (e) scalars
  inconsistent with `key_space.class` (counter claiming `branch`; uuid
  claiming `emit`), (f) `prefix`/`width` present on `uuid`, absent on
  `counter`, (g) rollup disagreeing with `combined_claim` (both a wrong
  scalar and a wrongly-present/absent `unique_within`).
- Laziness: an incoherent block parses at `open`/construction time without
  raising; the error surfaces on first `presentation_keys()` call.
- `union_safe`: every row of the contract's pairwise table — identical
  `record_index` pair safe; differing-width `record_index` pair unsafe;
  `uuid`×`uuid` safe; `record_id`×`record_id` safe; digit-rendered pairs
  `WARD_`/`THTR_` safe, `A-`/`A-1` unsafe, `""`/`"1"` unsafe, `""`/`X_`
  safe, equal-prefix counters unsafe; `uuid`×digit unsafe;
  `record_id`×digit unsafe.
- `combined_claim`: singleton equals its entry's scalars; all-counter →
  `emit`/false/false; all-stable → `branch`/true/true; mixed →
  `branch`/false/false; any-pair-unsafe → `unique_within is None` with
  stability true/true iff all stable; empty sequence → `ValueError`.
- Existing `tests/reader/test_sidecar.py` and the fixture suite still pass
  untouched (block absent everywhere today).

### Phase 2: Shared shape + DuckDB writer constraint path

**Delivers:** `TableKeys`; `QuerySpec.keys: TableKeys | None = None`;
`write_duckdb(emit, queries, output_path, keys)` with the
explicit-DDL-plus-insert path for keyed tables; `write_query_specs` DuckDB-arm
flattening; `write_duckdb_window` create-if-missing keyed path; migration of
`write_duckdb` call sites to pass `{}`.
**Demo:** Hand-builds two `QuerySpec`s over a tiny emit — one keyed, one not —
writes a DuckDB file, prints `duckdb_constraints()` showing PRIMARY KEY +
UNIQUE on the keyed table only, then loads a claim-falsifying (duplicate)
relation and shows the loud `ExportRuntimeError` naming the table.
**Contracts:** Design doc § Runtime Types (shared exporter shape), § Functions
(writers) — verbatim.
**Steps:** none (single implementer — the migration is one test file with ~9
call sites)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/writers/duckdb.py` |
| Modify | `tests/writers/test_duckdb.py` |
| Modify | `tests/writers/test_duckdb_window.py` |
| Create | `docs/sprints/presentation-keys/demos/phase_2_writer_constraints.py` |

**Tests:**
- Existing `test_duckdb.py` cases migrated: every current call passes
  `keys={}` and behavior is unchanged (CREATE TABLE AS path).
- Keyed table: created with explicit DDL — column names/types transcribed
  from the Arrow schema, declared PRIMARY KEY and UNIQUE present in
  `duckdb_constraints()`; row counts identical to the unkeyed path.
- Empty keyed table: constraints declared, zero rows, table present.
- Multi-column `unique` tuple entries produce one composite UNIQUE constraint each.
- NULLs pass a UNIQUE constraint (SQL semantics — the partitioned-kind
  undeclared-sub-type case); duplicate non-NULL values fail loudly with
  `ExportRuntimeError` naming the table.
- `keys` naming a table absent from `queries` → `ValueError`.
- `write_query_specs` with a keyed spec (fmt duckdb) lands constraints; fmt
  csv ignores keys, no notice at this layer.
- `write_duckdb_window`: first window with a keyed replace-class spec creates
  the table with constraints; a later window's replace preserves them; a
  constraint-violating window raises and rolls back — warehouse state (rows
  and `_export_windows`) identical to before the failed window; unkeyed
  specs byte-identical behavior to today.

### Phase 3: Base mode `declare_keys`

**Delivers:** `BaseConfig.declare_keys`; `resolve_base_table_keys`;
`build_base_query_specs` setting `QuerySpec.keys` when on; `export_base`
emitting `keys_not_declarable_csv_notice()` under CSV; the shared notice
helper + code constant.
**Demo:** Builds a small emit with a claimed flat kind and an
unclaimed/partitioned kind; runs `mode: base` + `declare_keys: true` to
DuckDB, prints per-table constraints (claimed kind: `<kind>_key` PK, `id` +
`presentation_id` UNIQUE; unclaimed: identity keys only); re-runs with CSV
showing identical data plus the notice; re-runs with `declare_keys` absent
showing constraint-free output.
**Contracts:** § Shared notice helper, § Base key resolution, and the
`BaseConfig` / engine behavioral changes above.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/engine.py` |
| Modify | `tests/config/test_base_config.py` |
| Modify | `tests/exporters/base/test_plan.py` |
| Modify | `tests/exporters/base/test_engine.py` |
| Create | `docs/sprints/presentation-keys/demos/phase_3_base_declare_keys.py` |

**Tests:**
- Config: `base: {declare_keys: true}` alone is a valid section;
  `declare_keys: false` behaves as absent; a non-bool rejects; the
  at-least-one-field error message names `declare_keys`.
- `resolve_base_table_keys`: flat claimed kind → PK `<kind>_key`, unique
  `id` + `presentation_id`; renamed columns resolve to post-rename names;
  partitioned kind with rollup claim → `presentation_id` declared; rollup
  without `unique_within` → not declared; kind absent from block → identity
  keys only; block absent → identity keys only; incoherent block →
  `PresentationKeysInvalidError`.
- Engine: `declare_keys` off (absent and false) → every spec `keys is None`;
  on → every spec keyed; windowed compile (window set) keys identically to
  full; CSV fmt still resolves keys at compile (strict accessor fires under
  CSV too).
- `export_base` CSV + `declare_keys` → exactly one notice with code
  `keys-not-declarable-csv`, before data; DuckDB + `declare_keys` → no
  notice; end-to-end DuckDB export carries the constraints.
- Existing base plan/engine tests pass unchanged.

### Phase 4: Source mode `declare_keys`

**Delivers:** `SourceConfig.declare_keys`; `resolve_source_table_keys` with
the per-genre rule; `build_source_query_specs` setting keys when on;
`export_source` CSV notice.
**Demo:** Builds an emit with a tracked (changelog) kind, an untracked
partitioned kind (split units), and a membership property; runs
`mode: source` + `declare_keys: true` under both `change_delivery` values to
DuckDB; prints per-table constraints showing the genre table (changelog +
junction: none; snapshot/reference/transaction: `id` PK; split units:
`presentation_id` UNIQUE iff the sub-type's entry exists).
**Contracts:** § Source key resolution and the `SourceConfig` / engine
behavioral changes above.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `tests/config/test_source_config.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_engine.py` |
| Create | `docs/sprints/presentation-keys/demos/phase_4_source_declare_keys.py` |

**Tests:**
- Config: `source: {declare_keys: true}` alone valid; message names it;
  composes with `change_delivery` freely.
- `resolve_source_table_keys` per genre: junction → None; changelog genre +
  `changelog` delivery → None; changelog genre + `snapshot` delivery →
  whole-table rule; unsplit reference/transaction → `id` PK +
  `presentation_id` iff whole-table claim; split unit → `presentation_id`
  UNIQUE iff `key_for(kind, sub_type)` present, absent entry → identity key
  only; renamed tables/columns resolve to output names; block absent →
  identity keys only; incoherent → raises.
- Engine: off → all `keys is None`; on → per-genre keys; windowed compile
  keys equal full-export keys per genre (reference replace, transaction
  append, snapshot replace, changelog/junction none).
- `export_source` CSV + `declare_keys` → one notice; DuckDB end-to-end
  carries constraints.
- Existing source plan/engine tests pass unchanged.

### Phase 5: Incremental notice + `init` advisory

**Delivers:** `export_window` emitting the CSV notice once per invocation;
`init` per-kind stub advisory comment; integration coverage that windowed
DuckDB warehouses carry constraints from the first window and that a
falsifying window rolls back; fingerprint participation verified.
**Demo:** Builds an emit with a claimed kind; drives `mode: base` +
`declare_keys` + `incremental` `--next` twice into a DuckDB warehouse,
printing constraints after window 1 and row growth after window 2; runs the
incremental CSV path showing the per-invocation notice re-emitted each drip;
prints the `init` stub for the emit showing the advisory natural-key comment.
**Contracts:** the `export_window`, `generate_init_config`, and fingerprint
behavioral changes above.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/init.py` |
| Modify | `tests/incremental/test_driver.py` |
| Modify | `tests/test_cli_init.py` |
| Create | `docs/sprints/presentation-keys/demos/phase_5_incremental_init.py` |

**Tests:**
- Driver CSV + `declare_keys` (base and source modes): exactly one
  `keys-not-declarable-csv` notice per invocation, before data; a second
  `--next` invocation re-emits it; DuckDB fmt → no notice.
- Windowed DuckDB + `declare_keys`: after window 1 the warehouse tables
  carry the declared constraints; window 2 append/replace succeeds with
  constraints intact.
- A window whose data falsifies a declared key raises `ExportRuntimeError`
  and leaves the warehouse exactly as before (rows, `_export_windows`,
  cursor untouched).
- Fingerprint: flipping `declare_keys` changes the config fingerprint (a
  `--next` against an existing warehouse with the other value refuses, per
  the existing mismatch rule).
- `init`: a claimed flat kind's stub carries the advisory comment; a
  partitioned kind with rollup claim carries it; no-claim rollup and absent
  block → no comment; incoherent block → `init` fails with
  `PresentationKeysInvalidError`.
- Existing driver and init tests pass unchanged.

## What Doesn't Change

- **Streaming, mixer, Kafka** — no file under `exporters/streaming/` is
  touched; message keying stays `record_id`.
- **Conformance** (`reader/conformance.py`) — the C-set stays C1–C14; no new
  check. Strictness lives in the accessor.
- **Corrupters** — no file under `corrupters/` is touched; the base-emit
  writer already carries unknown sidecar fields (the block included) verbatim.
- **Dimensional export grammar and engine** — authors declare dimensional
  keys themselves; only `init`'s stub comments change. `dimensional/engine.py`
  is untouched.
- **Playback** (`playback/`) — compiles relations, not DDL; untouched.
- **CSV writer** (`writers/csv.py`) — no constraint surface; untouched.
- **Base `<kind>_key` / `<p>_key` derivation** (`derivations/record_index.py`,
  `exporters/base/renders.py`) — the block adds declarations *about* columns,
  never columns.
- **`Sidecar` parse leniency for the sibling registries** — `record_roles` /
  `sub_type_columns` keep their lenient parse; only the new accessor is
  strict.
- **CLI** (`cli.py`) — no new flags; `declare_keys` is config-only and
  notices already render through the existing sink.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/reader/sidecar.py` | `KeySpace`/`PartitionKey`/`WholeColumnClaim`/`PresentationKeys` views, raw-block retention, lazy strict `presentation_keys()`, `union_safe`, `combined_claim` |
| `src/fabulexa_forge/reader/errors.py` | `PresentationKeysInvalidError(ReaderError)` |
| `src/fabulexa_forge/reader/__init__.py` | Export the new names |
| `src/fabulexa_forge/exporters/query_spec.py` | `TableKeys`, `QuerySpec.keys` field, DuckDB-arm keys flattening, notice code + helper |
| `src/fabulexa_forge/writers/duckdb.py` | `write_duckdb` required `keys` param + explicit-DDL-plus-insert path; `write_duckdb_window` keyed create-if-missing |
| `src/fabulexa_forge/config/models.py` | `declare_keys` on `BaseConfig` (Phase 3) and `SourceConfig` (Phase 4) + validator message updates |
| `src/fabulexa_forge/exporters/base/plan.py` | `resolve_base_table_keys` |
| `src/fabulexa_forge/exporters/base/engine.py` | Key threading under `declare_keys`; CSV notice in `export_base` |
| `src/fabulexa_forge/exporters/source/plan.py` | `resolve_source_table_keys` (per-genre rule) |
| `src/fabulexa_forge/exporters/source/engine.py` | Key threading under `declare_keys`; CSV notice in `export_source` |
| `src/fabulexa_forge/incremental/driver.py` | CSV notice per invocation in `export_window` |
| `src/fabulexa_forge/exporters/dimensional/init.py` | Advisory natural-key comment on claimed kinds' stubs |
| `tests/reader/test_presentation_keys.py` | New — accessor, coherence clauses, algebra |
| `tests/writers/test_duckdb.py` | Migrate call sites to `keys={}`; new constraint-path cases |
| `tests/writers/test_duckdb_window.py` | New keyed windowed cases |
| `tests/config/test_base_config.py` | `declare_keys` grammar cases |
| `tests/config/test_source_config.py` | `declare_keys` grammar cases |
| `tests/exporters/base/test_plan.py` | `resolve_base_table_keys` cases |
| `tests/exporters/base/test_engine.py` | Key threading + notice + end-to-end constraint cases |
| `tests/exporters/source/test_plan.py` | `resolve_source_table_keys` per-genre cases |
| `tests/exporters/source/test_engine.py` | Key threading + notice + end-to-end constraint cases |
| `tests/incremental/test_driver.py` | Notice, windowed constraints, rollback, fingerprint cases |
| `tests/test_cli_init.py` | Advisory-comment cases |
| `docs/sprints/presentation-keys/demos/phase_1_reader_view.py` | Phase 1 demo |
| `docs/sprints/presentation-keys/demos/phase_2_writer_constraints.py` | Phase 2 demo |
| `docs/sprints/presentation-keys/demos/phase_3_base_declare_keys.py` | Phase 3 demo |
| `docs/sprints/presentation-keys/demos/phase_4_source_declare_keys.py` | Phase 4 demo |
| `docs/sprints/presentation-keys/demos/phase_5_incremental_init.py` | Phase 5 demo |
