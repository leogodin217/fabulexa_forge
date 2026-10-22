# Sprint: base-format-v13

## Purpose

Adopt the re-vendored base-format contract end to end: forge opens, validates, and
fixtures the new format — key-space texture (`start`/`gaps`) flows through the
strict registry view into the four-parameter union-safety rule, the new domain-closure
conformance check (C16) is judged by `validate` and declared by the corrupter
impact oracle, and the fixture authority mints schema-valid sidecars again.

An author on the new format runs `fabulexa-forge validate` / `export` / `corrupt`
exactly as before; the only visible deltas are the version gate (older emits are
refused), C16 in validate reports, C16 in `defects.json` impact sets, and an
empty dataset catalog until packs are rebuilt.

The sprint's parent is the contract-sync branch (`contract-sync/v13-4762bc4b`), so
the upstream PR lands contract + adoption together. **The parent's test suite is
red by design** (the fixture authority schema-validates against the already-synced
contract while the code literal lags); the baseline test gate is skipped with the
user's explicit acknowledgment. The pre-commit baseline still gates.

## Scope

**Capabilities touched:**

- Reader + conformance: the version gate, `key_space` texture parse (strict
  registry view), the four-parameter `record_index` × `record_index` union-safety
  rule, the C16 domain-closure check (not: any new reader query surface)
- Key election: synthesized built-in key spaces carry texture (not: any config
  grammar change — the `keys` block is untouched)
- Corrupters: C16 joins the impact vocabulary and the cell-mutation impact
  oracles; corrupt recipe ground truths re-curated (not: any new operation, no
  selector/grammar change)
- Test infrastructure: key-claim entry constructors join the fixture authority
  (`sidecar_builder`), containing the next contract bump's sidecar-shape churn
- Dataset distribution: the authored catalog empties until packs are rebuilt at
  the supported version (not: pack rebuild — tracked as follow-up; not: any
  loader/fetch/builder change)

**Not included:** multi-branch work, Parquet, Stage 5, rebuilding dataset packs
(requires upstream bundles at the new version), architecture-doc updates
(ship separately post-archival, including the README status-row version cite).

## Breaking Changes

- **Version gate.** `SUPPORTED_BASE_FORMAT_VERSION` becomes the vendored
  contract's version; emits at the prior version are refused
  (`UnsupportedBaseFormatVersionError`). This is the bump's purpose.
- **`KeySpace` gains two fields** (`start`, `gaps`). Every construction site
  updates (the type checker names them); the dataclass stays frozen. `union_safe`
  verdicts change for texture-divergent `record_index` pairs: identical
  `prefix`/`width` with differing `start` or `gaps` was safe, is now unsafe —
  the contract's own incompatible-reading fix. All election/declared-keys gates
  inherit the new verdicts through the one algebra authority.
- **`validate` runs C16** and reports 16 results; `run_check` accepts `"C16"`.
  The corrupt engine's input gate (refuse a non-conformant source emit) now
  includes C16 — a conformant producer emit passes it by definition.
- **`ImpactCode` gains `"C16"`**; `defects.json` impact sets may now carry it.
  The `beyond-c1-c12` sentinel keeps its historical spelling (user decision —
  published vocabulary consumed downstream).
- **Fixture authority**: digit-rendered key-claim fixture entries are built
  through the new `key_space`/`partition_key` constructors; hand-built dicts
  missing the texture fields fail `write_emit`'s schema net at construction.
- **Dataset catalog**: the shipped manifest's `datasets` list empties;
  `fabulexa-forge datasets list` renders the existing no-datasets line. The
  version-agreement hygiene test stays strict.

## Success Criteria

- [ ] `make test` green (full suite; parent was red by design)
- [ ] `open_emit` accepts the vendored contract's version and only it
- [ ] `union_safe` implements the four-parameter `record_index` row and the
      texture-never-rescues-comparable-prefixes rule
- [ ] `validate` runs C1–C16 in order; C16 passes on the spanning fixture and
      fails on the out-of-domain negative fixture, naming kind/property/value
- [ ] A `mutate_cells` out-of-domain corruption yields `defects.json` declaring
      C16, and `validate` on the corrupted emit fails exactly the declared codes
      (recipe equality + engine containment suites green)
- [ ] Every fixture key-claim entry is built through the fixture authority's new
      constructors; no hand-built `key_space` dict literals remain in the test tree
- [ ] `fabulexa-forge datasets list` renders the no-datasets line; the shipped
      manifest version-agreement hygiene test passes strict

## Design decisions (pinned)

1. **Synthesized texture for the built-in `record_index` surface** (user-approved):
   `start=0, gaps=None`. The built-in surface is the raw 0-based dense
   `record_index` rendered as-is — as a rendering function it is `f(i) = i`,
   series base 0, no gaps. Consequence (deliberate): a registry `record_index`
   declaration carrying the contract's undeclared-default `start: 1` is **not**
   union-safe beside the built-in surface — raw index 1 and a start-1 rendering
   of index 0 both read `"1"`; that collision is exactly what the contract bump
   names. The prior behavior (safe under identical `prefix`/`width` alone) was
   the incompatible reading the bump exists to retire.
2. **C16 value comparison**: enum-domained properties are string properties (the
   registry's own contract); the check compares the column's non-NULL values,
   cast to VARCHAR, against the declared option strings by exact equality.
   Offending values are reported as sorted distinct values — deterministic
   messages.
3. **C16 impact-oracle sites**: exactly the two cell-mutation writers —
   `mutate_cells` and `duplicate_rows`' mutation mode. Rationale per other
   operation: `null_cells` writes NULL (C16 ranges over non-NULL values);
   `insert_rows` clones donors and resamples from observed in-domain pools;
   `freeze_series` propagates a same-series (in-domain) anchor value;
   `delete_rows`/`drop_events`/`shift_sim_time`/`distort_intervals` never write
   a records property value; `dangle_reference`/`mispoint_reference` write into
   reference-annotated columns, which the registry does not declare domains over
   (and `mispoint` writes an observed value). If a future emit ever declares a
   domain over a column one of these touches, the agreement suite's containment
   check is the alarm that reopens this list.
4. **Oracle-before-check within the phase**: the C16 check, the impact oracle,
   the vocabulary, and the recipe ground truths land in ONE phase — the curated
   corrupt recipes assert set *equality* between validate's failing set and the
   manifest impact union, so no partial landing is green.

## Contracts

### Reader — `src/fabulexa_forge/reader/sidecar.py`

```python
@dataclass(frozen=True)
class Gaps:
    """A digit-rendered minting declaration's seeded-gap policy, verbatim from
    the sidecar.

    Present on a KeySpace iff the declaration declared a gap policy (contract
    § The presentation_keys block); digit-rendered classes only. `rate` is the
    per-step probability that a gap precedes the next value; `max` the largest
    gap size. The parameters are declarations from one producer configuration,
    not measurements — comparison in the union-safety algebra is exact
    equality, no tolerance.
    """

    rate: float
    max: int
```

```python
@dataclass(frozen=True)
class KeySpace:
    """A minting declaration's key-space identity, verbatim from the sidecar.

    `space_class` carries the sidecar field `class` (a Python keyword).
    `prefix`, `width`, and `start` are present iff the class is digit-rendered
    ('counter' / 'record_index'); `gaps` additionally requires the declaration
    to have declared a gap policy — the contract's presence rules, mirrored as
    None-ness rather than sentinel values. `start` is the base of the minted
    series (the contract writes 1 when the declaration declared none).
    """

    space_class: Literal["counter", "record_index", "uuid", "record_id"]
    prefix: str | None
    width: int | None
    start: int | None
    gaps: Gaps | None
```

`_parse_key_space(raw: object, context: str) -> KeySpace` — behavioral change
(clause f, key-space shape): `start` presence must match digit-rendered-ness
exactly as `prefix`/`width` do, and must be a non-bool int ≥ 0. `gaps`, when
present, requires a digit-rendered class and must be a mapping carrying exactly
`rate` (a non-bool int/float number) and `max` (a non-bool int); absence parses
to `gaps=None`. Any violation raises `PresentationKeysInvalidError` naming the
context and the clause, matching the existing message convention.

`union_safe(a: KeySpace, b: KeySpace) -> bool` — behavioral change: the
`record_index` × `record_index` affirmative row requires identical `prefix`,
`width`, `start`, AND `gaps` (exact equality, `None` vs declared differ). The
incomparable-prefix row and every other row are unchanged — texture never
rescues comparable prefixes, exactly as width never does. Docstring cites the
contract's updated table.

`combined_claim` — unchanged (derives through `union_safe`).

`src/fabulexa_forge/reader/__init__.py` — export `Gaps` beside `KeySpace`
(import + `__all__`).

### Election — `src/fabulexa_forge/exporters/election.py`

```python
_RECORD_ID_KEY_SPACE = KeySpace(
    space_class="record_id", prefix=None, width=None, start=None, gaps=None
)

_RECORD_INDEX_KEY_SPACE = KeySpace(
    space_class="record_index", prefix="", width=0, start=0, gaps=None
)
```

The `_RECORD_INDEX_KEY_SPACE` comment pins design decision 1 (truthful texture
of the raw 0-based dense index rendered verbatim; a start-1 registry
declaration is deliberately not union-safe beside it). No other election change
— the gates inherit the new verdicts through `union_safe`.

### Fixture authority — `tests/_support/sidecar_builder.py`

```python
def key_space(
    space_class: "Literal['counter', 'record_index', 'uuid', 'record_id']",
    *,
    prefix: str | None = None,
    width: int | None = None,
    start: int | None = None,
    gaps: tuple[float, int] | None = None,
) -> dict[str, object]:
    """Build one conformant key_space object for a fixture sidecar.

    The sole constructor for a key-space entry across every fixture builder —
    the one place the test tree knows the contract's presence rules. A
    digit-rendered class ('counter' / 'record_index') requires prefix, width,
    AND start together (the contract's uniform-participation rule, required
    explicitly like prop_column's temporal pair — no silent default start);
    gaps is optional and digit-rendered-only, rendered as
    {"rate": gaps[0], "max": gaps[1]}. A non-digit-rendered class requires all
    four absent. A shape-defective negative fixture mutates the returned dict;
    a defect is never expressible through the constructor.

    Args:
        space_class: The key-space identity class.
        prefix: Rendering prefix; required iff digit-rendered.
        width: Zero-pad width; required iff digit-rendered.
        start: Base of the minted series; required iff digit-rendered.
        gaps: The declared (rate, max) gap policy; optional, digit-rendered only.

    Returns:
        The key_space dict for a partition_key entry.

    Raises:
        ValueError: A digit-rendered class missing any of prefix/width/start,
            a non-digit-rendered class given any of them, or gaps on a
            non-digit-rendered class.
    """
```

```python
def partition_key(key_space: dict[str, object]) -> dict[str, object]:
    """Build one conformant partition_key entry from its key space.

    Derives the three key-claim scalars from key_space["class"] — the contract
    fully determines them ('counter' -> emit/False/False; every other class ->
    branch/True/True) — so a fixture can never state the scalar-key-space
    coupling wrong (clause e is unexpressible through the constructor; negative
    variants mutate the returned dict).

    Args:
        key_space: A key_space dict, from the `key_space` constructor.

    Returns:
        {"unique_within": ..., "branch_stable": ..., "slice_stable": ...,
         "key_space": key_space}.

    Raises:
        ValueError: key_space carries no recognized "class".
    """
```

(Defaults on these fixture-helper keyword arguments are ordinary Python per the
Config Boundary's test-helper carve-out; `None` means "absent", and the
constructors enforce the contract's presence rules — nothing is invented.)

### Conformance — `src/fabulexa_forge/reader/conformance.py`

```python
def _check_c16(emit: "Emit") -> CheckResult:
    """C16: Domain closure.

    For each (kind, property) in the sidecar's enum_domains registry whose
    records__<kind> table exists in the catalog and carries prop__<property>:
    every non-NULL value in that column, cast to VARCHAR, is one of the
    entry's declared option strings. The inverse of the registry's
    intent-not-observation stance: a declared option with no live row is
    conformant; a live value outside the declared list is the violation.
    Failure messages name the kind, property, and the sorted distinct
    offending values.

    Skips (recorded, not failures): enum_domains absent entirely; a registry
    kind with no records table in the emit (the registry is run-wide; an
    emit's table set may be narrower); a registry property whose column the
    table does not carry. Reads the registry through the sidecar's parsed
    enum_domains view, never the raw block.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for check id "C16".
    """
```

Registry: `_CHECKS` gains `"C16"`, `_RECOGNIZED_IDS` gains `"C16"` (order:
after `"C15"`); `validate` / `run_check` docstrings say C1–C16.

### Corrupters

`src/fabulexa_forge/corrupters/manifest.py` — `ImpactCode` gains `"C16"`
(after `"C13"`, before the sentinel); the docstring adds one sentence: the
cell-mutation operations can write a non-NULL value outside an enum-domained
records string property's declared list. The `beyond-c1-c12` sentinel is
unchanged.

`src/fabulexa_forge/corrupters/operations/_impact.py`:

```python
def enum_value_undeclared(
    sidecar: "Sidecar", kind: str, column: str, post_value: object
) -> bool:
    """The C16 predicate for one mutated records cell.

    Mirrors _check_c16's domain-closure clause: True iff `column` is a
    prop__<p> whose (kind, <p>) has an enum_domains entry and `post_value` is
    a non-NULL string outside the declared option list. False when
    enum_domains is absent, the (kind, property) is unregistered, the value is
    None, or the value is not a string (the registry declares string domains;
    a non-string post value on a registered column is unreachable through the
    type-preserving transforms).

    Args:
        sidecar: The source emit's sidecar.
        kind: The mutated cell's records kind.
        column: The mutated cell's column name (prop__-prefixed).
        post_value: The cell's post-mutation stored value.

    Returns:
        True iff C16, run on an emit written from this state, would report
        this value as outside its declared domain.
    """
```

`mutate_cells._cell_impact` — behavioral change (docstring): records
`prop__<p>` unions `C16` iff `enum_value_undeclared`; the C6 and C12 clauses
are unchanged; the union of whichever codes hold, else the sentinel.
`_history_value_impact` is unchanged (the `history` table carries no
enum-domained column).

`duplicate_rows` (mutation mode) — the conflicting-duplicate cell-mutation
impact site gains the same `enum_value_undeclared` clause, same union rule.

`src/fabulexa_forge/corrupters/engine.py` — the input-gate prose and message
("C1-C15") widen to C16; no logic change (the gate already runs `validate`).

`tests/recipes/_harness.py` — `_MANIFEST_VOCABULARY` gains `"C16"`;
`failing_checks_in_manifest_vocabulary`'s docstring updates (C14/C15 stay
excluded: no corrupter can break a sidecar-only or surface-only check).

### Datasets — `src/fabulexa_forge/datasets/manifest.yaml`

The authored `datasets` list empties (`datasets: []`), keeping the authoring
header comment plus a one-line note that the catalog refills as packs are
rebuilt against bundles at the supported version. `DatasetManifest` already
admits an empty list and `render_dataset_listing` already owns the
no-datasets line — no model or renderer change.

## Phases

### Phase 1: v13 compatibility — version gate, key-space texture, fixture authority

**Delivers:** forge opens and fixtures the new format: the gate literal, the
texture-carrying `KeySpace` through parse and four-parameter union safety, the
synthesized election spaces, the fixture authority's key-claim constructors with
every fixture migrated through them, and the stale dataset catalog emptied.
Whole suite green at phase end (parent was red).

**Demo:** `demos/phase_1_v13_reader.py` — builds a temp emit through the fixture
authority with a partitioned `presentation_keys` block carrying texture
(`start`, one entry with `gaps`); opens it via `open_emit` (the gate accepts the
new version), prints the typed registry view, and prints `union_safe` verdicts
for three pairs: identical four-parameter `record_index` pair (safe), identical
`prefix`/`width` with differing `start` (unsafe — the bump's new row), and an
incomparable-prefix digit pair (safe). Exits non-zero on any wrong verdict.

**Contracts:** `Gaps`, `KeySpace`, `_parse_key_space`, `union_safe`, election
constants, `key_space` / `partition_key` constructors, manifest.yaml change.

**Steps:** `source → migrate (fan-out, 21 files) → author (5 files)` — atomic:
the bump, the parse, and the fixture shape must land together (any intermediate
state is suite-red; the schema net enforces it).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/__init__.py` |
| Modify | `src/fabulexa_forge/reader/sidecar.py` |
| Modify | `src/fabulexa_forge/reader/__init__.py` |
| Modify | `src/fabulexa_forge/exporters/election.py` |
| Modify | `src/fabulexa_forge/datasets/manifest.yaml` |
| Modify | `tests/_support/sidecar_builder.py` |
| Modify | `tests/exporters/base/_base_fixtures.py` |
| Modify | `tests/exporters/base/test_election_plan.py` |
| Modify | `tests/exporters/base/test_election_renders.py` |
| Modify | `tests/exporters/base/test_plan.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/dimensional/test_election_fk.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Modify | `tests/exporters/dimensional/test_populations.py` |
| Modify | `tests/exporters/source/_source_fixtures.py` |
| Modify | `tests/exporters/source/test_election_plan.py` |
| Modify | `tests/exporters/source/test_init.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/streaming/_election_fixtures.py` |
| Modify | `tests/exporters/streaming/test_election_stream.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Modify | `tests/exporters/streaming/test_init.py` |
| Modify | `tests/exporters/streaming/test_presentation.py` |
| Modify | `tests/exporters/streaming/test_selection.py` |
| Modify | `tests/exporters/test_keys_init.py` |
| Modify | `tests/incremental/test_driver.py` |
| Modify | `tests/test_cli_init.py` |
| Modify | `tests/reader/test_presentation_keys.py` |
| Modify | `tests/exporters/test_election.py` |
| Modify | `tests/_support/test_sidecar_builder.py` |
| Modify | `tests/datasets/test_manifest.py` |
| Modify | `tests/test_cli_datasets.py` |
| Create | `docs/sprints/base-format-v13/demos/phase_1_v13_reader.py` |

**Tests:**

- `_parse_key_space` accepts a digit-rendered entry with `start` and no `gaps`;
  with `start` and well-formed `gaps`; a non-digit-rendered entry with neither
- Clause-f negatives, each raising `PresentationKeysInvalidError` naming the
  clause: digit-rendered missing `start`; non-digit-rendered carrying `start`;
  `start` boolean; `start` negative; `gaps` on a non-digit-rendered class;
  `gaps` missing `rate` or `max`; `gaps` members mistyped (bool / string)
- `union_safe`: identical four-parameter `record_index` pair → True; identical
  `prefix`/`width`, differing `start` → False; identical `prefix`/`width`/`start`,
  one declaring `gaps` → False; identical declared `gaps` on both → True;
  comparable prefixes with differing texture → still False (texture never
  rescues); incomparable prefixes with any texture → True; all pre-existing
  rows (uuid/record_id/counter mixes) unchanged
- `combined_claim` over a texture-divergent `record_index` pair → no uniqueness
  claim, stability pair preserved
- Synthesized spaces: built-in `record_index` population resolves to
  `(record_index, "", 0, 0, None)`; built-in × registry entry with `start: 1`,
  same `prefix`/`width` → not union-safe (decision 1's consequence, asserted
  directly); built-in × built-in → safe
- `key_space` constructor: conformant digit-rendered and non-digit-rendered
  shapes; every ValueError arm (missing start, texture on uuid, gaps on
  record_id); `partition_key` derives emit/False/False for counter and
  branch/True/True for the stable classes; constructor output passes
  `write_emit`'s schema net inside a presentation_keys block
- `write_emit` default-stamps the supported version (existing test, now green)
- Dataset catalog: `load_manifest()` returns the empty catalog;
  `datasets list` renders the no-datasets line (text) and `{"datasets":[]}`
  (json); version-agreement hygiene test green against the empty manifest
- Existing tests that must still pass: the whole suite — in particular every
  election/declared-keys/init suite over the migrated fixtures, the
  version-gate negatives (sentinel-based, untouched), and the version-literal
  hygiene test (no new literals outside the allowlist)

### Phase 2: C16 domain closure — check, impact oracle, ground truths

**Delivers:** `validate` judges domain closure; the corrupter impact oracle
declares it; the curated corrupt recipe ground truths move deliberately with it.

**Demo:** `demos/phase_2_c16_domain_closure.py` — builds a temp emit with an
enum-domained property; `validate` → C16 PASS; corrupts it with a `mutate_cells`
out-of-domain mutation on that property; `validate` on the corrupted emit → C16
FAIL naming the value; loads `defects.json` and asserts the defect declares
`C16` and the corrupted emit's failing-check set is contained in the manifest's
impact union. Exits non-zero on any miss.

**Contracts:** `_check_c16`, registry additions, `ImpactCode`,
`enum_value_undeclared`, `_cell_impact` / duplicate-rows impact behavioral
change, engine gate prose, `_MANIFEST_VOCABULARY`.

**Steps:** `source → author (9 files)` — atomic: the curated corrupt recipes
assert set equality between validate's failing set and the manifest impact
union, so the check, the oracle, and the ground truths cannot land separately.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/conformance.py` |
| Modify | `src/fabulexa_forge/corrupters/engine.py` |
| Modify | `src/fabulexa_forge/corrupters/manifest.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/_impact.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/mutate_cells.py` |
| Modify | `src/fabulexa_forge/corrupters/operations/duplicate_rows.py` |
| Modify | `tests/recipes/_harness.py` |
| Modify | `tests/reader/_fixtures_build.py` |
| Modify | `tests/reader/test_conformance_data.py` |
| Modify | `tests/reader/test_conformance_structural.py` |
| Modify | `tests/test_cli.py` |
| Modify | `tests/corrupters/operations/test_mutate_cells.py` |
| Modify | `tests/corrupters/operations/test_duplicate_rows.py` |
| Modify | `tests/corrupters/test_agreement.py` |
| Modify | `examples/recipes/corrupt/mutate-out-of-domain/expect.yaml` |
| Modify | `examples/recipes/corrupt/mutate-string-dirt/expect.yaml` |
| Create | `docs/sprints/base-format-v13/demos/phase_2_c16_domain_closure.py` |

**Tests:**

- C16 passes on the spanning fixture (its enum-domained columns carry only
  declared values — live input for the check)
- C16 negative fixture (new deliberately-broken variant in `_fixtures_build.py`:
  one live out-of-domain value in an enum-domained column): fails C16 **and no
  other check** (the registry is sidecar-only for C14; the value is otherwise
  well-formed), message names kind, property, and the offending value
- C16 skips: emit without `enum_domains` → skip recorded, pass; a registry kind
  with no records table → that pair skipped; a registry property whose column
  the table does not carry → that pair skipped
- `validate` returns results in C1..C16 order (update the pinned-order test);
  the CLI `validate` output lists each of C1–C16 (update the pinned-output test)
- `enum_value_undeclared`: out-of-domain string on a registered (kind, property)
  → True; declared value → False; unregistered property → False; absent
  registry → False; None / non-string post value → False
- `mutate_cells` out-of-domain mutation of the discriminator → impact
  `("C12", "C16")` (union, sorted); truncation of an enum-domained non-tracked
  status value → `C16` joins whatever the C6 clause yields; mutation of an
  unregistered column → impact unchanged from today
- `duplicate_rows` mutation mode mutating an enum-domained cell on the copy →
  `C16` joins the declared union; exact/near modes unchanged
- Re-curated ground truths: `mutate-out-of-domain` impact_union and
  contains_defects gain C16 beside C12; `mutate-string-dirt`'s
  `truncate_status_value` defect gains C16 beside C6 — recipe equality suite
  green; agreement containment suite green over the whole matrix
- Existing tests that must still pass: every other corrupt recipe's ground
  truth byte-for-byte (only the two named recipes move), the engine
  determinism and structural-preservation suites, `test_manifest_models`'
  sentinel-mix rejection (now also covering a C16 mix)

## What Doesn't Change

- `contract/` — vendored and already synced on the parent; this sprint never
  touches it
- `UNSUPPORTED_VERSION_SENTINEL` and the version-gate negative posture (the
  sentinel is version-proof by design)
- `combined_claim`'s derivation rules — unchanged semantics, new verdicts flow
  in only through `union_safe`
- The `keys` config grammar, election resolution gates' structure, and every
  mode's elected rendering — texture changes union-safety *verdicts* only
- `_check_c1` … `_check_c15` bodies
- The `beyond-c1-c12` sentinel spelling and `_normalize_impact`'s mix rejection
  (user decision 3)
- `_history_value_impact` — the `history` table carries no enum-domained column
- The datasets loader, fetch/verify/extract machinery, pack builder, and
  `render_dataset_listing` — only the authored YAML list empties
- The kafka integration rig's canned envelopes (`tests/integration/kafka/_envelopes.py`
  is hand-authored and version-free; the rig's emits flow through `write_emit`
  and are healed by phase 1)
- `tests/reader/_emit_helpers.py` and every fixture builder that mints no
  presentation_keys block — the version stamp flows from the one literal

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/__init__.py` | Version-gate literal bump (the single allowlisted code literal) |
| `src/fabulexa_forge/reader/sidecar.py` | `Gaps`; `KeySpace.start`/`gaps`; clause-f parse extension; four-parameter `union_safe` row |
| `src/fabulexa_forge/reader/__init__.py` | Export `Gaps` |
| `src/fabulexa_forge/exporters/election.py` | Synthesized built-in key spaces carry texture (`start=0`, `gaps=None` on record_index; None/None on record_id) |
| `src/fabulexa_forge/datasets/manifest.yaml` | Authored catalog empties until packs rebuild at the supported version |
| `src/fabulexa_forge/reader/conformance.py` | `_check_c16` + registry + C-range docstrings |
| `src/fabulexa_forge/corrupters/engine.py` | Input-gate prose/messages widen to C16 |
| `src/fabulexa_forge/corrupters/manifest.py` | `ImpactCode` gains `"C16"` |
| `src/fabulexa_forge/corrupters/operations/_impact.py` | `enum_value_undeclared` C16-mirror predicate |
| `src/fabulexa_forge/corrupters/operations/mutate_cells.py` | `_cell_impact` unions C16 |
| `src/fabulexa_forge/corrupters/operations/duplicate_rows.py` | Mutation-mode impact unions C16 |
| `tests/_support/sidecar_builder.py` | `key_space` / `partition_key` constructors (the key-claim fixture authority) |
| `tests/recipes/_harness.py` | Manifest vocabulary gains C16 |
| `tests/reader/_fixtures_build.py` | C16 out-of-domain negative fixture variant |
| 21 fixture/test files (phase 1 migrate list) | Key-claim entries rebuilt through the new constructors, texture explicit |
| `tests/reader/test_presentation_keys.py`, `tests/exporters/test_election.py`, `tests/_support/test_sidecar_builder.py` | v13 parse/algebra/constructor/synthesized-space coverage |
| `tests/datasets/test_manifest.py`, `tests/test_cli_datasets.py` | Empty-catalog expectations |
| `tests/reader/test_conformance_data.py`, `tests/reader/test_conformance_structural.py`, `tests/test_cli.py` | C16 coverage; C1..C16 order/output pins |
| `tests/corrupters/operations/test_mutate_cells.py`, `tests/corrupters/operations/test_duplicate_rows.py`, `tests/corrupters/test_agreement.py` | C16 impact coverage |
| `examples/recipes/corrupt/mutate-out-of-domain/expect.yaml`, `examples/recipes/corrupt/mutate-string-dirt/expect.yaml` | Ground truths gain C16 (deliberate re-curation) |
| `docs/sprints/base-format-v13/demos/phase_1_v13_reader.py`, `.../phase_2_c16_domain_closure.py` | Phase demos |
