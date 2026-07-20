# Sprint: playback-api

## Purpose

Implement the playback seam per `docs/architecture/pending/playback.md` — forge's
caller-driven, deterministic library surface for driving an emit as a tape, in two
tiers (primitive `events`/`snapshot`/`seek`; shaped `window`/`state`). An educator's
tooling (a loom channel, a script, a test rig) can then pull "exactly what changed in
`[T1, T2)`" or "this star schema as of T" as data, with no config file, cursor, or
writer directory involved.

The design doc is the authority for all semantics. This spec carries contracts by
reference, the phase decomposition, and test cases — it does not restate the doc's
rationale. Read the doc section named next to each contract before implementing it.

## Scope

**Capabilities touched:**
- playback (new package): tier-1 primitive (atom selection surface, `open_playback`,
  `events` / `snapshot` / `seek`, entry-point-invariant `seq`, consistency algebra);
  tier-2 shaped (`open_shaped_playback`, `tables` / `window` / `state`)
- derivations: membership-state-at fold (new resident), `build_state_at_end_sql`
  (additive second entry point on state-at), truncated-tape surface (three relation
  builders + truncated sidecar view)
- exporters (dimensional + source): `base_relations` compile indirection
  (name-shadowing CTE wrap); the two declared changes — `last_mutation_sim_time`
  reserved output name; horizon-less `change_delivery: snapshot` redefined to
  end-of-tape reconstruction

**Not included:** `base` mode (build slot #2), re-seaming any shipped verb (claim C),
named atom groups, multi-branch, any new YAML / CLI surface (the doc mandates none),
streaming code changes (the canonical-order promotion is contract prose, not code).

## Breaking Changes

- **`build_query_specs` / `build_source_query_specs` gain a required
  `base_relations: Mapping[str, str] | None` parameter** (no default — Principle #7;
  the notice-sink precedent). Internal surface; every caller migrates in Phase 8.
  With `None`, compilation is byte-identical to today.
- **A config naming an output column `last_mutation_sim_time`** (dimensional
  author-named column, source `rename` target — both accepted today) is refused at
  load time with an error naming the presentation-name posture. No shipped value
  channel changes; only the raw name on an output column is withdrawn.
- **A full `export` of a `change_delivery: snapshot` source shape** (no window) —
  refused today via `SourceSnapshotRequiresWindows` — becomes legal, reconstructing
  at the tape's end. The error class is deleted (its only raise site is removed).
  Windowed snapshot delivery is untouched.

## Success Criteria

- [ ] The consistency algebra holds under test: `snapshot(T2−1)` equals
  `snapshot(T1−1)` ⊕ `events(T1, T2)` on intact tapes, including coincident-instant
  boundaries.
- [ ] `seq` is entry-point-invariant: bounded and unbounded heads agree.
- [ ] The bridging theorem holds under test: `state(T_slice)` is value-identical to
  the shape's full export, dimensional and source.
- [ ] `base_relations=None` compiles byte-identical SQL; the full existing suite is
  green with no behavioral change outside the two declared changes.
- [ ] Both declared changes are gated by tests (load-time refusal; end-of-tape
  reconstruction).
- [ ] Corrupted tapes play totally: verbatim stamps, orphan membership rows,
  TRY_CAST NULLs — per the doc's § Permissive playback table.
- [ ] Layer direction holds: tier-1 modules import only reader / derivations /
  anchor / errors — never `exporters.*`, never `config`.

## Contracts

Full signatures + docstrings for every playback-package and derivations contract are
**verbatim in the design doc** (`docs/architecture/pending/playback.md`,
§ Interface Contracts) — implement them exactly as written there; do not re-derive.
Index:

| Contract | Doc section | Phase |
|---|---|---|
| `RecordAtom`, `MembershipAtom`, `RecordAtomSelection`, `MembershipAtomSelection`, `PlaybackSelection` | § Selection and identity types | 5 |
| `PlaybackError` | § Errors | 5 |
| Business rules `SelectionNonEmpty` … `AskBoundsValid` | § Validation Rules | 5 (selection rules), 6–7 (ask bounds) |
| `PlaybackEvent` | § The event type | 6 |
| `open_playback` | § Opening a head | 6 |
| `Playback.events` | § The head | 6 |
| `Playback.snapshot`, `Playback.seek` | § The head | 7 |
| `PlaybackSnapshot`, `PlaybackPosition` | § Snapshot and position | 7 |
| `build_membership_state_at_sql` (+ `MEMBERSHIP_STATE_AT_COLUMNS`) | § The new derivations | 1 |
| `build_state_at_end_sql` | § The new derivations | 2 |
| `build_truncated_history_sql`, `build_truncated_membership_sql`, `build_truncated_sidecar` | § The truncated-tape surface | 3 |
| `build_truncated_records_sql` | § The truncated-tape surface | 4 |
| `ShapedTable`, `ShapedTableDecl`, `open_shaped_playback`, `ShapedPlayback.tables` | § Shaped playback (tier 2) | 10 |
| `ShapedPlayback.window` | § Shaped playback (tier 2) | 11 |
| `ShapedPlayback.state` | § Shaped playback (tier 2), § The compile indirection | 12 |

The contracts below are the deltas this spec owns (existing-surface changes and one
internal seam the doc leaves to implementation).

### Changed: the two pure compile surfaces (Phase 8)

Each gains one parameter, threaded per the doc's § The compile indirection. Existing
parameters, behavior, and return contracts are unchanged; with
`base_relations=None` the compiled SQL is byte-identical to today.

```python
def build_query_specs(
    emit: Emit,
    config: DimensionalConfig,
    anchor: EffectiveAnchor | None,
    window: Window | None,
    notice_sink: NoticeSink,
    base_relations: Mapping[str, str] | None,
) -> list[QuerySpec]:
    """(Existing contract unchanged, plus:)

    Args:
        base_relations: Physical base-table name -> replacing relation (a complete
            SELECT). When given, every base-table read in the compiled plan
            resolves through the mapping via one name-shadowing CTE per mapped
            name wrapped around each compiled query (never a textual prefix — a
            compiled query may already open with its own WITH); unmapped names
            fall back to the physical table. None compiles byte-identically to
            the pre-parameter surface; the full-export and windowed callers pass
            None explicitly.
    """
```

```python
def build_source_query_specs(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    window: Window | None,
    notice_sink: NoticeSink,
    base_relations: Mapping[str, str] | None,
) -> list[QuerySpec]:
    """(Existing contract unchanged, plus base_relations — as above.)"""
```

### New: the mode-neutral shadow wrap (Phase 8)

Home: `src/fabulexa_forge/exporters/base_relations.py` (mode-neutral, the
`query_spec.py` precedent).

```python
def shadow_base_relations(
    sql: str,
    base_relations: Mapping[str, str],
) -> str:
    """Wrap a compiled query so mapped base-table names resolve to replacements.

    Emits WITH "<name>" AS (<replacing SELECT>), ... SELECT * FROM (<sql>) — a
    wrap, not a textual prefix, because sql may already open with its own WITH.
    Binding rules are contract (design doc § The compile indirection): a
    replacing relation's self-read binds physical (standard non-recursive WITH
    scoping — pinned by test); its cross-reads are binding-insensitive by
    construction (the builders inline truncation predicates); the compiled
    query's unqualified quoted reads shadow totally.

    Args:
        sql: The compiled query (a complete SELECT, possibly opening with WITH).
        base_relations: Physical base-table name -> replacing relation SELECT.
            Must be non-empty; the None case never reaches this function.

    Returns:
        The wrapped SELECT.
    """
```

### Changed: the reserved-name surface (Phase 9)

`exporters/reserved_names.py`: `is_reserved_column_name` returns True for
`last_mutation_sim_time` as well as `__valid_from_ns`; module + predicate docstrings
gain the presentation-name posture (read freely, deliver under its own name never).
The two existing enforcement sites (`dimensional/validation.py`, `source/plan.py`)
pick it up; each error message names the fix (deliver the value under a
presentation name — the source `updated_at` default, a dimensional `from:` source).

### Changed: horizon-less snapshot delivery (Phase 9)

`build_source_query_specs`: the `change_delivery: snapshot` + `window is None`
refusal (`source/engine.py:179`) is replaced by the end-of-tape render — a snapshot
render composing `build_state_at_end_sql` (no horizon parameter, no horizon
predicate; "the tape's end" is structural — the doc's § Shaped state, "One mode
semantic, redefined"). The render must span spine lifecycle instants: a record
deactivated after its last history event renders inactive. No compile path reads a
slice bound from the sidecar. `SourceSnapshotRequiresWindows` is deleted from
`errors.py`.

### Internal: the resolved selection (Phase 5)

The seam between validation and the asks — internal runtime type, not public API.

```python
@dataclass(frozen=True)
class ResolvedSelection:
    """A PlaybackSelection resolved against one sidecar at open.

    Carries, per record selection: the effective ordered property tuple (full-set
    None resolved to tracked + constant + the exempt discriminator, sidecar
    declaration order), the kind's full fold-invocation property set, sub-type
    predicate values, instance ids, presentation-id presence, and discriminator
    declaredness; per membership selection: the effective ordered field tuple,
    the table's full element-schema field set, owner predicate values, instance
    ids, and owner-discriminator declaredness. Built by resolve_selection; every
    later "selected" means these resolved sets.
    """
```

```python
def resolve_selection(
    sidecar: Sidecar,
    selection: PlaybackSelection,
) -> ResolvedSelection:
    """Validate a selection against the sidecar and resolve its effective sets.

    Applies every selection business rule (design doc § Validation Rules:
    SelectionNonEmpty, RecordKindResolvable, SubTypesDeclared,
    PropertiesResolvable, PropertiesNotSliceOnly, MembershipResolvable,
    OwnerSubTypesDeclared, MembershipFieldsResolvable, AtomsUnique,
    InstanceSetNonEmpty) — sidecar-only, no data reads.

    Args:
        sidecar: The open emit's sidecar.
        selection: The caller's atom selection.

    Returns:
        The resolved selection.

    Raises:
        PlaybackError: Any rule fails; messages per the doc's rule table.
    """
```

## Phases

Demo scripts live in `docs/sprints/playback-api/demos/`. Every phase gates on
`make test` (full suite). Test fixtures: synthesize emits programmatically in the
directory's existing style (`tests/derivations/_fixtures.py`,
`tests/exporters/source/_source_fixtures.py`); playback tests get their own
`tests/playback/_fixtures.py` (Phase 5).

### Phase 1: membership-state-at derivation
**Delivers:** `build_membership_state_at_sql` + `MEMBERSHIP_STATE_AT_COLUMNS`, the
point-in-time membership containment fold (new derivations resident, six-rule layer
contract).
**Demo:** Open a fixture emit, print containment rows for one membership table at
two horizons — an interval visible at T1 and gone at T2.
**Contracts:** `build_membership_state_at_sql` (doc § The new derivations).
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/derivations/membership_state_at.py` |
| Create | `tests/derivations/test_membership_state_at.py` |
| Create | `docs/sprints/playback-api/demos/phase_1_membership_state_at.py` |

**Tests:**
- Interval with `joined ≤ T < left` yields one row; open interval (`left` NULL) is
  contained; interval fully after T is absent.
- Zero-width interval (`joined == left`) is contained at no horizon.
- Inverted interval (`left < joined`) is contained at no horizon — total, never an
  error; overlapping duplicate intervals yield one row each.
- `left_sim_time` is never projected; columns are `MEMBERSHIP_STATE_AT_COLUMNS`
  plus each selected field's shape (`elem__<f>` scalar; `member__<f>__kind` /
  `member__<f>__id` pair) in `resolve_membership_columns` order, each codec VARCHAR.
- Declared ORDER BY `(joined_sim_time, record_id, <field tail>)`, tail compared
  `CAST(... AS VARCHAR) NULLS FIRST`.
- Empty `fields` tuple → owner identity + `joined_sim_time` only.
- Missing membership table → `TableNotFoundError`; unresolvable field →
  `ExportError`.

### Phase 2: end-of-tape state entry point
**Delivers:** `build_state_at_end_sql` — the state-at resident's additive second
entry point (no horizon parameter, no horizon predicate).
**Demo:** Print end-of-tape state for one kind; show equality with
`build_state_at_sql` at a beyond-everything horizon, and the divergence a
history-only horizon would cause (a record deactivated after its last history
event).
**Contracts:** `build_state_at_end_sql` (doc § The new derivations).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/derivations/state_at.py` |
| Create | `tests/derivations/test_state_at_end.py` |
| Create | `docs/sprints/playback-api/demos/phase_2_state_at_end.py` |

**Tests:**
- Equivalence contract: equals `build_state_at_sql` at any horizon strictly beyond
  every history *and lifecycle* instant.
- A record deactivated after its last history event is inactive end-of-tape (the
  case a history-only horizon gets wrong).
- The emitted SQL carries no horizon predicate: composing it over a
  truncation-filtered `history` relation bounds the answer at the filter.
- Tracked property at latest recorded history value; constant property at current
  records value; columns + ORDER BY exactly as the horizoned builder.
- Existing horizoned builder untouched: `tests/derivations/test_state_at.py` green
  unchanged.
- `TableNotFoundError` / `ExportError` per contract.

### Phase 3: truncated tape — history, membership, sidecar view
**Delivers:** `build_truncated_history_sql`, `build_truncated_membership_sql`,
`build_truncated_sidecar` — the truncated-tape surface minus the records builder.
**Demo:** Show a membership interval open at T (`left_sim_time` masked NULL) vs the
physical row; print the truncated sidecar's dropped-column set for a kind with a
`slice_only` column.
**Contracts:** the three builders (doc § The truncated-tape surface).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/derivations/truncated_tape.py` |
| Create | `tests/derivations/test_truncated_tape.py` |
| Create | `docs/sprints/playback-api/demos/phase_3_truncated_tape.py` |

**Tests:**
- History: exactly the rows with `sim_time ≤ T`, column shape verbatim, fork_path
  filtered.
- Membership: intervals with `joined ≤ T` only; `left_sim_time` masked NULL when
  `> T`, kept when `≤ T`; every other column verbatim.
- Sidecar view: each `records__<kind>` entry drops exactly the non-exempt
  `slice_only` columns; a sub-typed kind's `slice_only` discriminator
  `prop__<kind>_type` is kept; `last_mutation_sim_time` stays declared; every
  other table entry and sidecar field (slice bound included) is unchanged.
- View is pure and T-independent; the returned `Sidecar` composes with the public
  `Emit(sidecar=..., emit_dir=..., conn=...)` constructor over an open connection.
- Missing membership table → `TableNotFoundError`.

### Phase 4: truncated records builder
**Delivers:** `build_truncated_records_sql` — the records table reconstructed as of
T: recorded trail, codec round-trip, `ref_index__` re-derivation, `slice_only`
drops with the discriminator carve-out.
**Demo:** One kind at T: a tracked property at its as-of value beside the physical
current value; the recorded-trail `last_mutation_sim_time`; a `ref_index__`
re-derived NULL for a reference to a record created after T.
**Contracts:** `build_truncated_records_sql` (doc § The truncated-tape surface).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/derivations/truncated_tape.py` |
| Create | `tests/derivations/test_truncated_records.py` |
| Create | `docs/sprints/playback-api/demos/phase_4_truncated_records.py` |

**Tests:**
- Row filter `created_sim_time ≤ T`; identity columns + `record_index` verbatim;
  `active` / `deactivated_at` horizon-rendered.
- `constant` property verbatim; `tracked` property reconstructed as of T and
  TRY_CAST to its declared type; a corrupted non-parsing history value
  reconstructs NULL (never errors).
- Recorded trail: `greatest(created, latest tracked history ≤ T, deactivated_at
  when ≤ T)`; membership activity is not a component; trail never exceeds the
  physical value.
- `ref_index__<name>` re-derived via the *truncated* target spine: NULL beside a
  NULL reference; NULL beside a verbatim dangling / mispointed / created-after-T
  reference; correct index for an intact reference. Cross-reads carry inline
  truncation predicates (binding-insensitive).
- Non-exempt `slice_only` columns absent; a sub-typed kind's `slice_only`
  discriminator carried verbatim; `tracked` / `constant` presentation properties
  follow their class's rule.
- Column-list agreement with `build_truncated_sidecar` for every fixture kind (the
  stated invariant of the surface).

### Phase 5: tier-1 selection surface
**Delivers:** the `playback` package: atom + selection types, `PlaybackError`,
`resolve_selection` with all ten selection business rules and effective-set
resolution.
**Demo:** Resolve a selection against a fixture emit — print effective ordered
property sets for `None` / empty / named forms; show three rule failures with
their messages (slice_only property, undeclared sub-type, empty id set).
**Contracts:** selection types, `PlaybackError` (doc §§ Selection and identity
types, Errors); `ResolvedSelection` / `resolve_selection` (this spec).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/playback/__init__.py` |
| Create | `src/fabulexa_forge/playback/errors.py` |
| Create | `src/fabulexa_forge/playback/types.py` |
| Create | `src/fabulexa_forge/playback/selection.py` |
| Create | `tests/playback/__init__.py` |
| Create | `tests/playback/_fixtures.py` |
| Create | `tests/playback/test_selection.py` |
| Create | `docs/sprints/playback-api/demos/phase_5_selection.py` |

**Tests:**
- One positive + one negative case per rule, asserting the doc's message shapes:
  `SelectionNonEmpty`, `RecordKindResolvable`, `SubTypesDeclared` (all three
  message variants — unknown value, not sub-typed, undeclared discriminator
  column), `PropertiesResolvable`, `PropertiesNotSliceOnly` (exempt discriminator
  selectable), `MembershipResolvable`, `OwnerSubTypesDeclared`,
  `MembershipFieldsResolvable`, `AtomsUnique`, `InstanceSetNonEmpty`.
- `properties=None` resolves to tracked + constant + exempt discriminator in
  sidecar declaration order, never a non-exempt `slice_only` column; empty tuple
  → identity only; named tuple order does not affect the resolved order.
- `fields=None` resolves to the full element-schema field set in declaration
  order.
- Unknown `record_ids` values pass resolution (a predicate, not a reference).
- Layer direction: the package imports no `exporters.*` / `config` name.

### Phase 6: tier-1 events
**Delivers:** `PlaybackEvent`, `open_playback`, `Playback.events` — canonical total
order across both families, entry-point-invariant `seq`, full-set fold invocation
with projection + population restriction, `ts` rendering.
**Demo:** Open a head over two atoms (one record kind, one membership table);
iterate a window; show the cross-family interleave and that `events(T+1, None)`
resumes with the same `seq`.
**Contracts:** `PlaybackEvent`, `open_playback`, `Playback.events` (doc §§ The
event type, Opening a head, The head).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/playback/events.py` |
| Create | `src/fabulexa_forge/playback/head.py` |
| Modify | `src/fabulexa_forge/playback/__init__.py` |
| Create | `tests/playback/test_events.py` |
| Create | `docs/sprints/playback-api/demos/phase_6_events.py` |

**Tests:**
- Canonical order: `(event_sim_time, event_class, family, source_identity,
  record_id[, field tail])`; an owner's `c` precedes its coincident `join`; a
  `leave` precedes its owner's coincident `d`.
- `seq` entry-point invariance: `events(None, None)` vs `events(T+1, None)` carry
  identical `seq` after T; byte-identical duplicate intervals tie deterministically.
- Projection-only selection: a `u` touching only unselected properties still
  plays; `seq` invariant under `properties` / `fields`; population axes
  (`sub_types`, `record_ids`) change the in-scope stream.
- Population restriction is pure row selection: surviving events equal their
  unrestricted values.
- `ts`: with an anchor, offset-bearing ISO-8601 byte-identical to streaming's
  rendering for the same instant + anchor (microsecond truncation); without, the
  raw int.
- Laziness + independent pullability: no reads until pulled; two outstanding
  iterators on one emit advance independently.
- Bounds: `events(T, T)` empty; `start > end` and negative bounds →
  `PlaybackError`; `open_playback` performs no table reads (sidecar-only) and
  passes through the single-branch guard's `ExportError`.
- Corrupted tapes: resampled discriminator plays as its cell's value; string-dirt
  and NULL stamps verbatim under whole-kind selection; orphan membership rows play
  with `owner_sub_type` NULL; a deleted record's ids select nothing.

### Phase 7: tier-1 snapshot, seek, consistency algebra
**Delivers:** `PlaybackSnapshot`, `PlaybackPosition`, `Playback.snapshot`,
`Playback.seek` — lazy pyarrow tables with stamps and `_ts` siblings; the
consistency algebra proven under test.
**Demo:** `seek(T)`: print the snapshot, replay the tail, re-snapshot later and
show ⊕-agreement.
**Contracts:** `Playback.snapshot`, `Playback.seek`, `PlaybackSnapshot`,
`PlaybackPosition` (doc §§ The head, Snapshot and position).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/playback/snapshot.py` |
| Modify | `src/fabulexa_forge/playback/head.py` |
| Modify | `src/fabulexa_forge/playback/__init__.py` |
| Create | `tests/playback/test_snapshot.py` |
| Create | `tests/playback/test_consistency.py` |
| Create | `docs/sprints/playback-api/demos/phase_7_snapshot_seek.py` |

**Tests:**
- Record table: fold-canonical columns verbatim, then `sub_type` stamp, then `_ts`
  siblings in raw-column order; typed at zero rows; `presentation_id` present
  exactly when the kind carries one.
- Membership table: `left_sim_time` never present; `owner_sub_type` stamp; anchor
  → `joined_sim_time_ts`; typed at zero rows.
- A record created after T is absent; a zero-width interval contains no T;
  `snapshot(0)` includes records created at 0; `at_sim_time` past the slice bound
  → final state, no error.
- Stamp semantics: `sub_type` NULL for a non-sub-typed kind, a NULL cell, an
  undeclared discriminator column; verbatim for an out-of-domain value.
- Consistency algebra: for several `(T1, T2)` pairs including coincident-instant
  boundaries, `snapshot(T2−1)` equals `snapshot(T1−1)` ⊕ `events(T1, T2)` applied
  in `seq` order (`c` insert, `u` replace, `d` deactivate at the event key, `join`
  add, `leave` remove one matching row).
- `seek(T)`: snapshot equals `snapshot(T)`; events equal `events(T+1, None)` with
  identical `seq`; both halves lazy and independently pullable.
- Accessor for an unselected kind / membership table → `PlaybackError`; repeated
  access returns the identical materialized table.

### Phase 8: compile indirection (`base_relations`)
**Delivers:** the required `base_relations` parameter on both pure compile
surfaces, realized by the mode-neutral name-shadowing wrap; every existing caller
passes `None`; binding rules pinned by test.
**Demo:** Compile one dimensional and one source shape with a truncation-shaped
replacing relation for `history`; show shadowed output vs physical, and
byte-identical SQL under `None`.
**Contracts:** changed compile signatures + `shadow_base_relations` (this spec);
binding rules (doc § The compile indirection).
**Steps:** `source → migrate (fan-out, 10 files)` — atomic: the parameter is
required with no default, so the signature change and the caller migration land in
one gated phase.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/base_relations.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Create | `tests/exporters/test_base_relations.py` |
| Modify | `tests/exporters/test_notices.py` |
| Modify | `tests/exporters/dimensional/test_fk.py` |
| Modify | `tests/exporters/dimensional/test_grains.py` |
| Modify | `tests/exporters/dimensional/test_lookup.py` |
| Modify | `tests/exporters/dimensional/test_scd.py` |
| Modify | `tests/exporters/dimensional/test_scd2_source_filter.py` |
| Modify | `tests/exporters/dimensional/test_windowed.py` |
| Modify | `tests/exporters/source/test_engine.py` |
| Modify | `tests/recipes/test_source_recipes.py` |
| Modify | `tests/writers/test_duckdb_window.py` |
| Create | `docs/sprints/playback-api/demos/phase_8_base_relations.py` |

**Tests:**
- `base_relations=None` → byte-identical compiled SQL to the pre-change surface
  (both modes; assert against captured pre-change SQL for one representative
  shape each, or by equality between wrapper-called and direct-called specs).
- A mapping wraps each compiled query in one CTE per mapped name; a compiled
  query that already opens with `WITH` wraps correctly (never a textual prefix).
- Engine-pinning test: a replacing SELECT reading the base table it presents
  binds to the *physical* table inside its own CTE (non-recursive WITH scoping).
- Shadowing is total: a dimensional fk hop and a source lookup read resolve
  through the mapping (no physical leak for a mapped name); unmapped names fall
  back physical.
- Existing suites green after migration (the phase gate) — every direct caller
  passes `base_relations=None`.

### Phase 9: the two declared mode changes
**Delivers:** `last_mutation_sim_time` joins the reserved output-name check (both
modes, load-time); horizon-less `change_delivery: snapshot` reconstructs at the
tape's end via `build_state_at_end_sql`; `SourceSnapshotRequiresWindows` deleted.
**Demo:** A previously-refused full export of a snapshot-delivery source shape
producing end-of-run state tables; a config naming an output column
`last_mutation_sim_time` refused with the posture-naming message.
**Contracts:** reserved-name surface + horizon-less snapshot delivery (this spec);
semantics doc § Shaped state ("One mode semantic, redefined"), § Affected
Subsystems (presentation-name posture).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/reserved_names.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/engine.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_engine.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Create | `docs/sprints/playback-api/demos/phase_9_declared_changes.py` |

**Tests:**
- A dimensional author-named column `last_mutation_sim_time` → load-time error
  naming the fix; a source `rename` target likewise; `__valid_from_ns` behavior
  unchanged.
- Value channels untouched: existing `updated_at`-default, `from:` /
  `derived: timestamp`, and ordinal tests stay green unchanged.
- Full export of a horizon-less snapshot shape: end-of-run state tables; a record
  deactivated after its last history event renders inactive (the lifecycle-instant
  case); tracked values at latest history, constant current.
- The two former refusal tests (`tests/exporters/source/test_engine.py:226,242`)
  rewritten to assert the reconstruction; `SourceSnapshotRequiresWindows` no
  longer importable.
- Windowed snapshot delivery byte-identical to before (existing windowed tests
  green).

### Phase 10: tier-2 open and tables
**Delivers:** `ShapedTable`, `ShapedTableDecl`, `open_shaped_playback`,
`ShapedPlayback.tables` — open-time mode validation, the anchor rule, notice-sink
binding, static delivery declarations.
**Demo:** Open a dimensional and a source shape; print each `ShapedTableDecl`
(name + `window_delivery`), including a `None` for a membership-grain table.
**Contracts:** doc § Shaped playback (tier 2) — the three types + `tables`.
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/playback/shaped.py` |
| Modify | `src/fabulexa_forge/playback/__init__.py` |
| Create | `tests/playback/test_shaped_open.py` |
| Create | `docs/sprints/playback-api/demos/phase_10_shaped_open.py` |

**Tests:**
- Open runs the mode's full validation sidecar-only: an invalid config's
  `ExportError` passes through; a valid shape opens with no data read.
- Source shape with `anchor=None` → `PlaybackError` at open; dimensional opens
  with `None`.
- A shape whose plan names a `slice_only` column or a reserved output name is
  refused at open by the mode's own rules (the inherited precondition).
- `tables()`: names exactly as the full export names them; dimensional in config
  declaration order; source in the mode's deterministic enumeration order.
- `window_delivery` per class / genre (records-grain fact + history_point +
  SCD-2 + changelog + transaction + junction → `append`; type-1 + reference +
  snapshot-delivery changelog → `snapshot`); history_interval / membership grain
  → `None`.
- The config's `rebase` and `incremental` blocks are not read.

### Phase 11: tier-2 window
**Delivers:** `ShapedPlayback.window` — the promoted per-table-class / per-genre
window-membership contract, stateless, ask-scoped windowed rules on first call.
**Demo:** Drive three consecutive windows over a dimensional shape; show
append-class rows accumulating to the same content as one wide window, and the
type-1 dim delivered full each window.
**Contracts:** `ShapedPlayback.window` (doc §§ Shaped playback, Shaped window).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Create | `tests/playback/test_shaped_window.py` |
| Create | `docs/sprints/playback-api/demos/phase_11_shaped_window.py` |

**Tests:**
- Per-class membership per the doc's two tables: records-grain fact on
  `last_mutation_sim_time`; history_point on `sim_time`; SCD-2 versions born in
  window as the physical projection (`__valid_from_ns`, no `valid_to`); type-1 dim
  full every window; changelog on `event_sim_time`; transaction on lmst;
  reference full; junction extract-on-change with `left_at` horizon-masked;
  snapshot-delivery changelog reconstructed at horizon `end`.
- Promotion equality: for one dimensional and one source shape, `window(T1, T2)`
  content equals the incremental driver's windowed compile for the same window.
- Windowed business rules run on the first `window()` only: an offending shape
  fails naming the table (whole-shape, never a per-table skip); the same shape's
  `state()`-only use never runs them (asserted in Phase 12).
- Every value is its full-export value (select-not-recompute: window rows equal
  the full export's rows for the same keys).
- Declared-but-empty: an empty window returns every declared table, zero-row
  typed; union of adjacent windows has no duplicates or gaps for append classes.
- Bounds: negative / `start > end` → `PlaybackError`.

### Phase 12: tier-2 state and the bridging theorem
**Delivers:** `ShapedPlayback.state` — the mode's full-export compile over the
truncated tape via the truncated emit view + all-tables `base_relations` mapping;
the bridging theorem under test.
**Demo:** `state(T)` for a dimensional shape at an interior T — an as-of-T star
schema; then `state(T_slice)` diffed empty against the full export.
**Contracts:** `ShapedPlayback.state` (doc §§ Shaped playback, Shaped state, The
compile indirection).
**Steps:** none.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Create | `tests/playback/test_shaped_state.py` |
| Create | `docs/sprints/playback-api/demos/phase_12_shaped_state.py` |

**Tests:**
- Bridging theorem: `state(T_slice)` value-identical to the shape's full export —
  one dimensional and one source shape, every table.
- Interior-T oracle: `state(T)` equals the shape's full export over a
  *materialized* truncated emit (truncated relations written out physically,
  recorded trail included).
- Per-class consequences (doc table): SCD-2 change points ≤ T with latest version
  open; type-1 / reference constant-current + tracked-as-of-T; records-grain
  values as of T (not end-of-run); changelog rows ≤ T; history_interval
  `lead_sim_time` NULL past T; junction `left_at` NULL when the leave is after T;
  snapshot-delivery genre reconstructed at horizon `T + 1`.
- The mapping carries one entry per sidecar base table: an fk hop to a kind
  outside the shape's declared sources resolves truncated (no physical leak — a
  reference to a record created after T lands NULL / absent per the truncated
  world).
- Delivery is `snapshot` on every table; tables in `tables()` order;
  declared-but-empty at T=0.
- The truncated emit view shares the caller's connection; the seam never closes
  it (the emit remains usable after `state`).
- Notices: each `state` / `window` compile's plan notices reach the bound sink
  (re-emitted per ask).
- A `state`-only shape never runs the windowed business rules.

## What Doesn't Change

Mirrors the design doc's § What Doesn't Change — binding for every phase:

- **The reader's contract** — no reader change; tier-2's truncated emit view is a
  composition by a new consumer (`Emit(sidecar=..., emit_dir=..., conn=...)`).
- **The five existing derivations** — signatures, canonical columns, ORDER BY of
  versioned-intervals, row-state-events, membership-events, state-at (horizoned),
  reference-resolution untouched.
- **Streaming exporter code** — zero edits anywhere under `exporters/streaming/`;
  the canonical-order promotion is contract prose. Tier 1 reimplements the merge
  and `ts` rule inside `playback/` (layer direction forbids importing streaming).
- **Every shipped verb byte-for-byte except the two declared changes** (Phase 9).
- **The incremental driver's mechanics** — window math, cursor, fingerprint,
  staging, writers; Phase 8 only threads `base_relations=None` through
  `export_window`.
- **Config envelopes** — no new YAML, no new fields; `StreamConfig` /
  `CorruptConfig` untouched.
- **The corrupters** — untouched; corrupted tapes are playback *input*, exercised
  in tests.
- **`contract/`** — untouched (not ours to change).

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/derivations/membership_state_at.py` | New: membership containment fold at a horizon (P1) |
| `src/fabulexa_forge/derivations/state_at.py` | Add `build_state_at_end_sql` (P2) |
| `src/fabulexa_forge/derivations/truncated_tape.py` | New: truncated history / membership / records builders + sidecar view (P3, P4) |
| `src/fabulexa_forge/playback/__init__.py` | New package; public exports grow per phase (P5–P10) |
| `src/fabulexa_forge/playback/errors.py` | New: `PlaybackError` (P5) |
| `src/fabulexa_forge/playback/types.py` | New: atoms + selection types (P5) |
| `src/fabulexa_forge/playback/selection.py` | New: `resolve_selection` + rules (P5) |
| `src/fabulexa_forge/playback/events.py` | New: `PlaybackEvent`, canonical merge, `seq`, `ts` (P6) |
| `src/fabulexa_forge/playback/head.py` | New: `open_playback`, `Playback` — `events` (P6), `snapshot` / `seek` (P7) |
| `src/fabulexa_forge/playback/snapshot.py` | New: `PlaybackSnapshot`, `PlaybackPosition` (P7) |
| `src/fabulexa_forge/playback/shaped.py` | New: `open_shaped_playback`, `ShapedPlayback` — `tables` (P10), `window` (P11), `state` (P12) |
| `src/fabulexa_forge/exporters/base_relations.py` | New: `shadow_base_relations` wrap (P8) |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | `base_relations` param threaded (P8) |
| `src/fabulexa_forge/exporters/source/engine.py` | `base_relations` param (P8); horizon-less snapshot render dispatch (P9) |
| `src/fabulexa_forge/exporters/source/renders.py` | End-of-tape snapshot render composing `build_state_at_end_sql` (P9) |
| `src/fabulexa_forge/exporters/reserved_names.py` | `last_mutation_sim_time` reserved output column (P9) |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Reserved-name message names the posture (P9) |
| `src/fabulexa_forge/exporters/source/plan.py` | Reserved-name message names the posture (P9) |
| `src/fabulexa_forge/errors.py` | Delete `SourceSnapshotRequiresWindows` (P9) |
| `src/fabulexa_forge/incremental/driver.py` | Pass `base_relations=None` (P8) |
| `tests/derivations/test_membership_state_at.py` | New (P1) |
| `tests/derivations/test_state_at_end.py` | New (P2) |
| `tests/derivations/test_truncated_tape.py` | New (P3) |
| `tests/derivations/test_truncated_records.py` | New (P4) |
| `tests/playback/__init__.py`, `tests/playback/_fixtures.py` | New (P5) |
| `tests/playback/test_selection.py` | New (P5) |
| `tests/playback/test_events.py` | New (P6) |
| `tests/playback/test_snapshot.py`, `tests/playback/test_consistency.py` | New (P7) |
| `tests/exporters/test_base_relations.py` | New (P8) |
| `tests/exporters/test_notices.py` | Migrate: pass `base_relations=None` (P8) |
| `tests/exporters/dimensional/test_fk.py`, `test_grains.py`, `test_lookup.py`, `test_scd.py`, `test_scd2_source_filter.py`, `test_windowed.py` | Migrate: pass `base_relations=None` (P8) |
| `tests/exporters/source/test_engine.py` | Migrate (P8); refusal tests rewritten (P9) |
| `tests/recipes/test_source_recipes.py`, `tests/writers/test_duckdb_window.py` | Migrate: pass `base_relations=None` (P8) |
| `tests/exporters/dimensional/test_validation.py` | Reserved-name cases (P9) |
| `tests/exporters/source/test_plan.py` | Rename-target refusal cases (P9) |
| `tests/exporters/source/test_renders.py` | End-of-tape render cases (P9) |
| `tests/playback/test_shaped_open.py` | New (P10) |
| `tests/playback/test_shaped_window.py` | New (P11) |
| `tests/playback/test_shaped_state.py` | New (P12) |
| `docs/sprints/playback-api/demos/phase_*.py` | One demo per phase (P1–P12) |
