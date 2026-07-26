# Sprint: structural-temporal

Design doc: `docs/architecture/pending/structural-temporal-columns.md` — semantics
and rationale live there; this spec carries contracts, phases, and test cases only.

## Purpose

Give the reader one surface owning the contract's structural-column temporal facts,
and migrate every consumer's private copy onto it — so an author's records-grain
fact can finally carry its birth (`created_sim_time`) and close (`deactivated_at`)
instants as `derived: timestamp` sources, joining SCD-2 dimensions effective-dated.

## Scope

**Capabilities touched:**
- reader: structural-temporal surface (`structural_instant_columns`,
  `records_structural_column_is_mutable`), sidecar-parse category gate
- dimensional exporter: records-grain timestamp allowlist widens one → three
  instants; allowlist + mutability set resolve through the reader
- source exporter: records + junction wallclock sets resolve through the reader
  (behavior unchanged)
- base exporter: wallclock set resolves through the reader (behavior unchanged)
- conformance: out-of-set sidecar `category` reclassifies from C1 `CheckResult`
  to sidecar-parse refusal (no conformance code change)

**Not included:**
- Collapsing dimensional's live incremental window-key copies
  (`grains.py`, `validation.py`) — explicitly out of scope per the design doc.
  Only the *dead, zero-reference* `_GRAIN_WINDOW_KEY` constant in `columns.py`
  is deleted (Principle #8 hygiene, not a window-key change).
- Any new config surface, presentation-column detection, or record-kind
  archetypes (design doc § What Doesn't Change).
- Promoting the pending design doc to live — ships separately post-archival.

## Breaking Changes

- **Sidecar parse refuses an out-of-set table `category`.** Today
  `Sidecar.from_raw` admits any string and defers diagnosis to `validate` (C1);
  after this sprint a `category` outside `{"fixed", "records", "membership"}`
  raises `SidecarStructureError` at parse — the same failure class as a missing
  or non-string category. An emit carrying one no longer opens; `validate`
  surfaces the structural refusal instead of a C1 `CheckResult`. Conformant
  emits are unaffected. One existing test flips intent:
  `tests/reader/test_sidecar.py::test_succeeds_with_bogus_category` becomes a
  refusal assertion.
- **Dimensional records-grain timestamp allowlist widens** from
  `{last_mutation_sim_time}` to all three records instants. Purely additive for
  authors: previously-refused configs now validate; no accepted config changes
  meaning.

## Success Criteria

- [ ] `structural_instant_columns` / `records_structural_column_is_mutable` are
  exported from the reader, pure and emit-independent, loud per the design doc's
  Loudness table.
- [ ] An out-of-set sidecar `category` refuses at parse; the three contract
  categories still parse.
- [ ] A records-grain fact with `derived: timestamp` on `created_sim_time` or
  `deactivated_at` validates and exports; NULL `deactivated_at` renders a NULL
  timestamp.
- [ ] No exporter holds a private structural-temporal column set — dimensional,
  source, and base all resolve through the reader surface.
- [ ] Source and base export output is byte-identical to before the migration
  (behavior-unchanged refactor; existing tests pass unmodified).
- [ ] `make test` green; recipe corpus gates pass including the new
  `fact-from-records` recipe.

## Contracts

New surface lives in `src/fabulexa_forge/reader/records_columns.py`, exported via
`fabulexa_forge/reader/__init__.py` alongside `records_column_role`.

```python
StructuralInstant = Literal[
    "created", "closed", "last_touched", "changed", "joined", "left"
]
```

```python
def structural_instant_columns(category: str) -> Mapping[str, StructuralInstant]:
    """
    The structural columns of a table category that carry a sim-time instant.

    Pure and emit-independent: the mapping is a property of the contract's
    pinned column layout for the category, not of any particular emit. A
    category's mapping is the same for every emit at the supported format
    version. Columns absent from the returned mapping carry no instant.

    Args:
        category: The sidecar table category — "fixed", "records", or
            "membership".

    Returns:
        Column name to the instant it names, for every instant-carrying
        structural column of the category. Empty for a category that pins
        none.

    Raises:
        ValueError: `category` is not a recognised table category. The
            category set is closed and validated when the sidecar is read, so
            an unrecognised value is a caller error, never emit data.
    """
```

```python
def records_structural_column_is_mutable(name: str) -> bool:
    """
    Whether a records-table structural column's value may change after the
    record is created.

    Answers the structural half of temporal mutability only, over a closed
    domain: the contract's pinned records structural columns. A
    `prop__<name>` column's mutability is declared per-emit by its sidecar
    temporal pair and is not answered here; a `ref_index__<name>` column
    tracks its sibling `prop__<name>` and follows the sibling's answer. A
    caller needing both halves classifies through the records-column
    taxonomy first — routing by family plus the taxonomy's ref-index
    prefix rule, since `ref_index__<name>` classifies as `identity` — and
    asks each half in turn.

    Args:
        name: A records structural column name — one the records-column
            taxonomy classifies as `identity` (excluding the ref-index
            prefix), `presentation`, or `lifecycle`.

    Returns:
        True when the column is one whose value the producer may change
        after creation; False when the contract pins it as set once.

    Raises:
        ValueError: `name` is not a records structural column — a
            `prop__<name>`, a `ref_index__<name>`, or a name the contract
            does not pin. The structural set is closed; mutability of the
            open remainder is either the sidecar's question (`prop__`,
            `ref_index__`) or nowhere guaranteed (a producer-added column),
            so a silent False would state a fact the contract does not
            hold.
    """
```

**Sidecar parse narrowing** (not a new surface — `_parse_table` in
`reader/sidecar.py`, after the existing missing/non-string category check):

```python
# category not in {"fixed", "records", "membership"}
#     → SidecarStructureError(
#           f"table '{name}' unrecognised category '{category}'"
#       )
```

The value set restates the vendored schema's `category` enum — contract-pinned,
the same hardcoding class as the pinned column lists. C1's schema check keeps
its `category` enum clause unchanged (it becomes unreachable, not removed).

**Instant vocabulary** (design doc § Semantics — the authoritative table):

| Category | Column | Instant | Nullable |
|---|---|---|---|
| `records` | `created_sim_time` | `created` | no |
| `records` | `deactivated_at` | `closed` | yes |
| `records` | `last_mutation_sim_time` | `last_touched` | no |
| `fixed` | `sim_time` | `changed` | no |
| `membership` | `joined_sim_time` | `joined` | no |
| `membership` | `left_sim_time` | `left` | yes |

**Records structural mutability** (design doc § Semantics — Mutability):
mutable = `active`, `deactivated_at`, `last_mutation_sim_time`; set-once =
`created_sim_time`, `fork_path`, `record_id`, `record_index`,
`presentation_id`. `ref_index__<name>` and `prop__<name>` are outside the
domain — asking raises.

## Phases

### Phase 1: Reader structural-temporal surface + category gate

**Delivers:** The two new pure classifiers, reader-exported, plus the
sidecar-parse category refusal that makes their loudness a caller-error signal.

**Demo:** `phase_1_structural_surface.py` — prints the instant mapping for each
of the three categories and the mutability answer per records structural column;
demonstrates loudness (caught `ValueError` for an unknown category, a
`prop__<name>`, a `ref_index__<name>`, and an unpinned name); writes a minimal
emit whose sidecar carries `category: "bogus"` and shows `open_emit` refusing
with `SidecarStructureError`.

**Contracts:** `StructuralInstant`, `structural_instant_columns`,
`records_structural_column_is_mutable`, the `_parse_table` narrowing.

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/records_columns.py` |
| Modify | `src/fabulexa_forge/reader/sidecar.py` |
| Modify | `src/fabulexa_forge/reader/__init__.py` |
| Modify | `tests/reader/test_records_columns.py` |
| Modify | `tests/reader/test_sidecar.py` |
| Modify | `tests/reader/test_open_emit.py` |
| Create | `docs/sprints/structural-temporal/demos/phase_1_structural_surface.py` |

**Tests:**
- `structural_instant_columns("records")` returns exactly
  `{"created_sim_time": "created", "deactivated_at": "closed", "last_mutation_sim_time": "last_touched"}`.
- `structural_instant_columns("fixed")` returns exactly `{"sim_time": "changed"}`.
- `structural_instant_columns("membership")` returns exactly
  `{"joined_sim_time": "joined", "left_sim_time": "left"}`.
- `structural_instant_columns` raises `ValueError` on an unrecognised category
  (e.g. `"bogus"`), naming the value.
- Vocabulary totality: the union of returned instants across the three
  categories is exactly the six-member `StructuralInstant` vocabulary, each
  appearing once.
- `records_structural_column_is_mutable` returns True for `active`,
  `deactivated_at`, `last_mutation_sim_time`; False for `created_sim_time`,
  `fork_path`, `record_id`, `record_index`, `presentation_id`.
- `records_structural_column_is_mutable` raises `ValueError` on
  `prop__status`, `ref_index__owner`, and a name the contract does not pin
  (e.g. `sim_time` — a fixed-category column, not a records structural one).
- Sidecar: `Sidecar.from_raw` raises `SidecarStructureError` for
  `category: "bogus"`, message naming the table and the value
  (`test_succeeds_with_bogus_category` flips to this).
- Sidecar: all three contract categories still parse; the existing
  missing-category and non-string-category refusals are unchanged.
- `open_emit` on an emit dir whose sidecar carries an out-of-set category
  raises `SidecarStructureError` (the reclassified path — `validate` never
  reaches C1).
- All other existing reader tests pass unmodified.

### Phase 2: Consumers resolve through the reader

**Delivers:** Dimensional's timestamp allowlist (records grain widened to three
instants) and structural mutability set, source's records/junction wallclock
sets, and base's wallclock set all resolve through the reader surface; the
dead `_GRAIN_WINDOW_KEY` constant is deleted; a `fact-from-records` recipe
demonstrates the author-facing payoff.

**Demo:** `phase_2_records_instants.py` — synthesizes a small emit (one records
kind; one deactivated record, one still-active record), exports a records-grain
fact whose config carries `derived: timestamp` columns sourced from
`created_sim_time`, `deactivated_at`, and `last_mutation_sim_time`, and prints
the output rows: wallclock birth/close/last-touched instants, with NULL close
for the still-active record. This exact config errored before this sprint.

**Contracts:** consumers of `structural_instant_columns` /
`records_structural_column_is_mutable`; the `TimestampSourceAvailable` rule's
consulted set moves from a private literal to the reader surface (shape and
message unchanged).

**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/base/renders.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/dimensional/test_export_dimensional.py` |
| Create | `examples/recipes/fact-from-records/config.yaml` |
| Create | `examples/recipes/fact-from-records/expect.yaml` |
| Create | `docs/sprints/structural-temporal/demos/phase_2_records_instants.py` |

Implementation notes (behavior, not code):
- `_TIMESTAMP_SOURCES_BY_GRAIN` becomes derived: each grain's set is
  `structural_instant_columns(<grain's table category>).keys()`, plus
  `lead_sim_time` for `history_interval` only (the virtual grain column stays
  dimensional's own, as does the grain→category mapping).
- `_MUTABLE_SOURCES` becomes derived through
  `records_structural_column_is_mutable` over the records grain's structural
  surface columns, not a literal.
- Source `_RECORDS_WALLCLOCK_COLUMNS` / `_JUNCTION_WALLCLOCK_COLUMNS` and base
  `_WALLCLOCK_COLUMNS` derive from `structural_instant_columns("records")` /
  `("membership")`. Base's set widens to three names but behavior is unchanged:
  the state-at projection never carries `last_mutation_sim_time`. Source's
  `_SNAPSHOT_VERBATIM_COLUMNS` superset relationship (noted in its docstring)
  is preserved — the derived records set is the same three columns.
- `_GRAIN_WINDOW_KEY` in `dimensional/columns.py` is deleted (zero references).
  The live inline window keys in `grains.py` and `validation.py` are NOT
  touched.

**Tests:**
- Records-grain fact with `derived: timestamp` source `created_sim_time`
  passes `check_timestamp_source_available` (was: `ExportError`).
- Same for `deactivated_at`; `last_mutation_sim_time` still accepted.
- A non-instant structural source on the records grain (e.g. `record_index`)
  is still refused with the unchanged message
  `"timestamp source '{source}' is not available on grain '{grain}' ..."`.
- History-point, history-interval, and membership grain allowlists reproduce
  their current sets exactly (existing tests pass unmodified;
  `lead_sim_time` still accepted on history_interval only).
- End-to-end export: a records-grain fact renders `created_sim_time` and
  `deactivated_at` as wallclock timestamps through the effective anchor; a
  still-active record's `deactivated_at` timestamp is NULL, not an error.
- Recipe corpus gates pass with `fact-from-records` (config loads, export
  runs, output matches `expect.yaml`).
- All existing source, base, and dimensional exporter tests pass unmodified
  (behavior-unchanged migrations).

## What Doesn't Change

- The `slice_only` policy, in every mode (truthfulness gate; nothing here
  relaxes it).
- Conformance C1–C14 code, including C1's `category` enum clause and the pinned
  structural column lists — those lists ARE the check and stay literal.
- Dimensional's incremental window-key selection, including the two live inline
  keys at `exporters/dimensional/grains.py` and `exporters/dimensional/validation.py`
  (only the dead `columns.py` constant goes).
- Every mode's output naming policy and rename maps — source's operational
  rename map, base's minimal defaults, dimensional's author-verbatim naming.
- The projection surface for `from:` / `correlation:` (already sidecar-resolved,
  already admits every structural column).
- Source's `_JUNCTION_FIXED_COLUMNS`, `_CHANGELOG_VERBATIM_COLUMNS`,
  `_SNAPSHOT_VERBATIM_COLUMNS` and base's `_VERBATIM_COLUMNS` — different
  concepts (render-class sets, not instant facts); they stay private.
- The records-column taxonomy (`records_column_role`, `ref_index_sibling`) —
  the new surface sits beside it, neither subsumes the other.
- Each derivation's fold-output column tuple.
- The timestamp/anchor rendering path — the renderer already qualifies whatever
  column it is given; widening the allowlist changes no rendering code.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/reader/records_columns.py` | Add `StructuralInstant`, `structural_instant_columns`, `records_structural_column_is_mutable` |
| `src/fabulexa_forge/reader/sidecar.py` | `_parse_table` refuses out-of-set `category` with `SidecarStructureError` |
| `src/fabulexa_forge/reader/__init__.py` | Export the new surface |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | `_TIMESTAMP_SOURCES_BY_GRAIN` + `_MUTABLE_SOURCES` resolve through the reader; records grain widens to three instants |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | Delete dead `_GRAIN_WINDOW_KEY` |
| `src/fabulexa_forge/exporters/source/renders.py` | Wallclock sets resolve through the reader |
| `src/fabulexa_forge/exporters/base/renders.py` | Wallclock set resolves through the reader |
| `tests/reader/test_records_columns.py` | New-surface tests (totality, mutability, loudness) |
| `tests/reader/test_sidecar.py` | Category refusal tests; `test_succeeds_with_bogus_category` flips |
| `tests/reader/test_open_emit.py` | Out-of-set category refuses at open (reclassified path) |
| `tests/exporters/dimensional/test_validation.py` | Widened records-grain allowlist cases |
| `tests/exporters/dimensional/test_export_dimensional.py` | End-to-end instant rendering + NULL close |
| `examples/recipes/fact-from-records/config.yaml` | New recipe: records-grain fact carrying its instants |
| `examples/recipes/fact-from-records/expect.yaml` | Recipe expectation |
| `docs/sprints/structural-temporal/demos/phase_1_structural_surface.py` | Phase 1 demo |
| `docs/sprints/structural-temporal/demos/phase_2_records_instants.py` | Phase 2 demo |
