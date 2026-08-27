# Sprint: author-selectable-identity

## Purpose

Deliver the `author-selectable-identity` pending design: an author controls which
identity surfaces (`record_id` / `record_index` / `presentation_id`) the streaming
after-image and the playback tier-1 maps publish, and under what names — and the
election itself becomes visible as a commented menu in every mode's `init`.

**Author use case:** a streaming author writes `identity: [record_index,
presentation_id]` plus `rename: {record_index: id, presentation_id: nhs_number}` and
gets a wire whose key and payload carry `id` / `nhs_number` — joinable against the
paired source export by name, with every published surface gated by the election's
own algebra. A generated config shows the election as a swap-able menu.

**Design doc:** `docs/architecture/pending/author-selectable-identity.md` — the
authority for semantics and rationale. This spec carries contracts, phases, and
test cases; where they seem to disagree, the design doc wins and the discrepancy is
raised, not silently resolved.

## Scope

**Capabilities touched:**

- Streaming exporter: identity projection (`identity` on both stream shapes),
  output-name resolution (rename admitting published-surface keys; reservation
  moves to resolved keys; absorption removed), publication gates widened over every
  published surface, per-surface identity relations at render, `StreamEvent`
  surrogate-field removal + `key_column` becoming the resolved output key
- Playback tier 1: `RecordAtomSelection.identity` projection over the event
  `after` map and the `record_state` table — projection only, no gates
- Cross-mode `init`: uniform `record_index` proposal + commented per-population
  alternatives via the one shared renderer; degradation mechanism retired
- Config loader: duplicate-mapping-key refusal shared by export / streaming /
  corrupt paths

**Not included:** base-mode surrogate suppression/gating (design § Boundaries);
playback tier 2 and membership-atom projection; corrupter/mixer surfaces beyond the
removed `StreamEvent` field; recipe authoring for the new `identity` surface
(post-sprint lifecycle step); regeneration of the shipped example configs
(nhs / retail / ride-sharing).

## Breaking Changes

All internal (greenfield package); no back-compat shims anywhere.

- **`StreamEvent` loses its standalone `presentation_id` field**; `key_column`
  becomes the elected surface's *resolved output key* (was: contract column name).
  Every constructor and reader adapts in the same phase (atomic — Phase 2 steps).
- **Streaming wire default changes:** the after-image no longer auto-carries
  `presentation_id`; a topic publishes its elected surface alone unless `identity`
  declares more. The `presentation_id`-absorption branch is removed.
- **Naming-authority signatures change:** `resolve_stream_output_columns` /
  `resolve_membership_output_columns` take a resolved `IdentityProjection` (not a
  pre-resolved identity key string) and return `OutputEntry` lists (not
  `(fold column, output key)` pairs). The reserved-name set becomes {each published
  surface's *resolved* output key, `event`}.
- **`rename` widens:** a *published* surface's contract column name is a legal
  rename key on both stream shapes. `StreamRenameUnresolvable` gains a
  published-set suffix when the bad key is an unpublished surface name.
- **`init` keys proposals change output:** uniform `record_index` active election
  with commented alternatives; the degrade-on-gate-failure mechanism and its
  comments are removed (unreachable by construction). Dimensional's
  `presentation_id` advisory comment is emitted again wherever a surrogate is
  declared.
- **Config loaders refuse duplicate YAML mapping keys** (was: silent last-wins).
  Valid existing configs are unaffected; a config that relied on last-wins now
  errors by design.
- **`RecordAtomSelection` gains `identity: tuple[str, ...] | None = None`** —
  internal runtime type, benign default per the seam's established None-means-full
  convention; existing constructions remain valid (no migration).

## Success Criteria

- [ ] `rename: {<elected contract name>: <key>}` re-keys all four elected-identity
      sites at once (message key map, after-image entry, Debezium `d` before-image,
      Debezium value schema); values byte-identical to before the rename
- [ ] A stream's after-image carries exactly the published set — elected alone by
      default; `identity: [...]` widens it in sidecar column order
- [ ] Every published non-elected surface passes resolution, union-safety, and the
      render-time uniqueness guard, with errors naming the stream and surface
- [ ] Playback tier-1 `after` maps and `record_state` honor
      `RecordAtomSelection.identity`; `None` keeps today's full-set output
      byte-identical; no gate runs at the seam
- [ ] All three `init` modes emit the same `keys` menu (uniform `record_index` +
      commented alternatives) through one renderer; generated configs parse and
      run clean
- [ ] Activating a menu alternative without removing the active line fails at load
      with a duplicate-key `ConfigError` naming file, key, and line
- [ ] Full suite green: `make test`

## Contracts

Module homes are this spec's assignment; signatures and semantics are the design
doc's (§ Interface Contracts, § Validation Rules — read them before implementing).

### Phase 1 — `src/fabulexa_forge/config/loader.py`

```python
def load_yaml_mapping(raw: str, label: str, path: Path) -> object:
    """Parse config YAML, refusing duplicate mapping keys.

    The shared parse step for the export, streaming, and corrupt loaders.
    Duplicate keys are refused rather than resolved last-wins.

    Args:
        raw: The file's text.
        label: The config kind, for the message ('export config',
            'stream config', 'corrupt config').
        path: The file's path, named in the message.

    Returns:
        The parsed YAML document.

    Raises:
        ConfigError: The text is not valid YAML, or a mapping carries the same
            key twice at any depth. The duplicate-key message is
            "duplicate key '{key}' in {label} {path} at line {line}".
    """
```

`load_export_config` / `load_stream_config` / `load_corrupt_config` route their
parse through it; their existing error surfaces (missing file, Pydantic
validation) are unchanged.

### Phase 2 — `src/fabulexa_forge/exporters/streaming/presentation.py`

```python
@dataclass(frozen=True)
class IdentityProjection:
    """One stream's resolved, gated identity projection."""

    elected: KeySurface
    """The stream's gated uniform elected surface — for a membership stream,
    the owner's. Always a member of `published`."""

    published: tuple[KeySurface, ...]
    """Every surface this stream publishes, in the kind's sidecar column order
    (record_id, presentation_id, record_index). Never empty."""


@dataclass(frozen=True)
class OutputEntry:
    """One after-image entry: where its value comes from, and its wire name."""

    source_kind: Literal["identity", "payload"]
    """'identity': `source` names a KeySurface rendered through its election
    relation (or, for record_id, the fold's own column). 'payload': `source`
    names a fold output column read verbatim."""

    source: str
    """The surface name or the fold column name, per `source_kind`."""

    output_key: str
    """The wire name — the bare default or the resolved rename target."""


def resolve_identity_output_key(
    rename: Mapping[str, str] | None,
    surface: KeySurface,
) -> str:
    """The wire name of one published identity surface.

    The single producer of every identity output key, consulted by the
    after-image resolvers and the message-key assembly site.

    Args:
        rename: The stream's declared rename map, or None.
        surface: A published surface.

    Returns:
        The rename target keyed on `surface` when the map carries one, else
        `surface` itself (the contract column name).
    """


def resolve_stream_output_columns(
    sidecar: Sidecar,
    kind: str,
    properties: Sequence[str],
    rename: Mapping[str, str] | None,
    identity: IdentityProjection,
) -> list[OutputEntry]:
    """Resolve a kind-shaped stream's after-image entries.

    The single naming authority. Order: published identity surfaces in sidecar
    column order, then selected properties in the column-order producer's
    order. No absorption branch — under a presentation_id election the surface
    is published once, as identity.

    Raises:
        StreamRenameUnresolvable: A rename key names neither a selected
            property nor a published surface; message appends the published
            set only when the key is an unpublished surface name.
        StreamOutputNameCollision: Two output keys collide, or one collides
            with a published identity key.
    """


def resolve_membership_output_columns(
    sidecar: Sidecar,
    membership: MembershipRef,
    fields: Sequence[str],
    rename: Mapping[str, str] | None,
    owner_identity: IdentityProjection,
) -> list[OutputEntry]:
    """The membership analog: published owner identity surfaces in the owner
    kind's sidecar column order, then selected element fields in
    element-schema declaration order.

    Raises:
        StreamRenameUnresolvable: A rename key names neither a selected field
            nor a published owner surface.
        StreamOutputNameCollision: Two output keys collide, or one collides
            with a published owner identity key or with `event`.
    """
```

`StreamEvent` (`types.py`): the `presentation_id` field is deleted; `key_column`
is documented as the elected surface's resolved output key. In Phase 2 the engine
constructs `IdentityProjection(elected=s, published=(s,))` directly from the gated
election (`resolve_identity_projection` arrives in Phase 3 with the config surface
that needs it).

### Phase 3 — `src/fabulexa_forge/exporters/streaming/engine.py` + `config/models.py` + `errors.py`

```python
def resolve_identity_projection(
    sidecar: Sidecar,
    stream_name: str,
    kind: str,
    declared: Sequence[KeySurface] | None,
    election: Election,
    populations: frozenset[str | None],
) -> IdentityProjection:
    """Resolve and gate one stream's identity projection.

    Runs the election's own resolution and union-safety gates over every
    published surface, not only the elected one. The elected surface is
    resolved first (it is the message key), then the declared set is validated
    to contain it.

    Precondition: the identity-uniformity gate has already run
    (ElectionMixedIdentity is not raised here).

    Args:
        sidecar: The typed sidecar.
        stream_name: The declaring stream's name, leading every message.
        kind: The stream's records kind — the owner kind for a membership
            stream.
        declared: The stream's `identity` list, or None for the elected
            surface alone.
        election: The resolved cross-mode election view.
        populations: The populations the stream's keys draw from — spanned
            populations for a kind stream, the addressed owner population set
            for a membership stream. None addresses a flat kind.

    Returns:
        The gated projection, `published` in sidecar column order.

    Raises:
        StreamIdentityMissingElected: `declared` omits the elected surface.
        StreamIdentityUnavailable: presentation_id is published on a kind that
            mints no surrogate.
        ElectionPresentationUndeclared: presentation_id is published for a
            population the registry does not declare.
        ElectionUnionUnsafe: A published surface's spanned key spaces are not
            pairwise union-safe.
    """
```

Config models (`KindStream`, `MembershipStream`): optional `identity:
list[KeySurface]` field, no default value semantics beyond absence (absent =
elected surface alone — the absent path is genuinely taken, not substituted).
Parse-time shape rules: non-empty, duplicate-free when present; membership of the
elected surface and sourceability are business rules. New `ExportError`
subclasses in `errors.py`: `StreamIdentityMissingElected`,
`StreamIdentityUnavailable`, `StreamPropertyNotAddressable` — messages per the
design doc's Business Rules table. `ElectedKeyDuplicate` violations on a published
non-elected surface name the surface, not the election. The render seam composes
one identity relation per published non-`record_id` surface at the end-of-tape
entry point; `record_id` composes none.

### Phase 4 — `src/fabulexa_forge/playback/types.py` + `selection.py`

`RecordAtomSelection` gains:

```python
identity: tuple[str, ...] | None = None
"""The identity surfaces the event `after` map and the `record_state` table
carry; must contain 'record_id'. None means the full available set — the
seam's established convention. Admissible: 'record_id', 'presentation_id'
('record_index' is outside the tier-1 domain). Projection only — never the
typed PlaybackEvent fields, never the fold invocation, never the event row
set or seq."""
```

`ResolvedRecordSelection` gains:

```python
identity: tuple[str, ...]
"""The published identity surfaces, in sidecar column order — the resolution
of the selection's `identity` (None resolving to the full available set:
'record_id', plus 'presentation_id' when the kind mints one)."""
```

`resolve_selection` gains the five playback rules (design doc § Playback rules),
each raising `PlaybackError` at open: identity shape (non-empty, duplicate-free),
identity domain (`record_id` / `presentation_id` only), identity spine
(`record_id` required), identity availability (surrogate minted), properties
disjoint from identity surfaces. The seam runs **no** publication gate and
imports no election machinery — surface names stay string literals (the
layer-direction invariant, AST-test-guarded).

### Phase 5 — `src/fabulexa_forge/exporters/keys_init.py`

```python
@dataclass(frozen=True)
class KeyElectionProposal:
    """An `init` keys proposal: the active election plus its alternatives."""

    active: Mapping[str, KeySurface | Mapping[str, KeySurface]]
    """Per kind, the active election — a scalar for a flat kind, or for a
    partitioned kind no population of which carries a presentation_id
    alternative; a per-sub-type map otherwise. Uniformly record_index."""

    alternatives: Mapping[str, Sequence[KeySurface]]
    """Population address -> the surfaces offered as commented alternatives,
    in surface order. Address: the kind, or '<kind>.<sub_type>'."""


def propose_key_election(sidecar: Sidecar) -> KeyElectionProposal:
    """The cross-mode keys proposal: uniform record_index plus per-population
    alternatives.

    Alternatives by resolvability alone: record_id always; presentation_id
    only where the presentation-key registry declares the population.
    Consults the strict registry accessor and shares its refusal behavior.

    Raises:
        ExportError: The emit carries an incoherent presentation-key block.
    """


def render_keys_block(proposal: KeyElectionProposal) -> list[str]:
    """Render the keys block: active lines and commented alternatives.

    The single renderer, spliced verbatim by the dimensional, source, and
    streaming init engines. Each population's alternatives precede its active
    line as comments, headed by one line stating an alternative replaces the
    active line rather than joining it.

    Returns:
        YAML lines, `keys:` first, ready to splice into a candidate config.
    """
```

The degradation surface retires: `self_gate_edge_safety` and streaming's
`_self_gate_streaming_keys` (and their explanatory comments) are deleted, not
left unreachable. Dimensional's dim-key alignment rule is unchanged and now
follows the uniform `record_index` active election; its `presentation_id`
advisory comment is emitted wherever a surrogate is declared. `init` proposes no
`identity` block (joins streaming's never-proposed list and its trailing comment).

## Phases

### Phase 1: Duplicate-key config loading

**Delivers:** `load_yaml_mapping` — one shared parse step for the three config
loaders, refusing duplicate YAML mapping keys at any depth with a named error.

**Demo:** loads a clean stream config, then one with a duplicate `keys` entry —
the second fails with `ConfigError` naming file, key, and line.

**Contracts:** `load_yaml_mapping`.

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/loader.py` |
| Modify | `tests/config/test_loader.py` |
| Create | `docs/sprints/author-selectable-identity/demos/phase_1_duplicate_key_refusal.py` |

**Tests:**

- Duplicate top-level key in an export config → `ConfigError` naming file, key, line
- Duplicate nested key (e.g. two `rename:` entries under one stream) → refused, at
  any depth
- Duplicate key inside a *list item's* mapping → refused (depth includes sequences)
- Each of the three loaders (export / stream / corrupt) refuses duplicates and
  labels the message with its own config kind
- A valid config with repeated *values* (not keys) still loads
- Existing loader tests still pass unchanged (missing file, invalid YAML, Pydantic
  validation errors) — including the stream/corrupt loader test sections in
  `tests/config/test_stream_config.py` / `test_corrupt_config.py`

### Phase 2: Streaming elected-identity naming

**Delivers:** the resolved identity output key — `rename` addresses the elected
surface's contract column name, one resolved key at all four identity sites; the
auto-published surrogate and the absorption branch are removed; `StreamEvent`
loses its standalone `presentation_id` field and `key_column` carries the
resolved key. The nhs / ride-sharing-marketplace default join traps die here.

**Demo:** streams a small emit under a `record_index` election with
`rename: {record_index: id}` — prints JSONL lines whose key map and after-image
both read `id`, with no `presentation_id` entry; then shows the same values under
no rename (contract-name default).

**Contracts:** `IdentityProjection`, `OutputEntry`, `resolve_identity_output_key`,
widened `resolve_stream_output_columns` / `resolve_membership_output_columns`.

**Steps:** `source → migrate (fan-out, 10 files) → author (4 files)` — atomic:
the field removal and signature changes leave the suite red until every
constructor and consumer migrates, so the whole pipeline lands in one phase and
the gate runs once at the end.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/types.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/presentation.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/driver.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/debezium.py` |
| Modify | `examples/recipes/streaming/stream-rename/config.yaml` |
| Modify | `tests/exporters/streaming/test_pacer.py` |
| Modify | `tests/exporters/streaming/test_mixer.py` |
| Modify | `tests/exporters/streaming/mixer/test_mixer_sink.py` |
| Modify | `tests/exporters/streaming/mixer/test_mixer_meters.py` |
| Modify | `tests/exporters/streaming/test_kafka_sink.py` |
| Modify | `tests/exporters/streaming/test_jsonl.py` |
| Modify | `tests/exporters/streaming/test_debezium.py` |
| Modify | `tests/exporters/streaming/test_selection.py` |
| Modify | `tests/exporters/streaming/test_value_election_stream.py` |
| Modify | `tests/integration/kafka/test_kafka_rig.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Modify | `tests/exporters/streaming/test_presentation.py` |
| Modify | `tests/exporters/streaming/test_election_stream.py` |
| Modify | `tests/exporters/streaming/_election_fixtures.py` |
| Create | `docs/sprints/author-selectable-identity/demos/phase_2_identity_rename.py` |

The `stream-rename` recipe change is comment prose only (it currently teaches
"the identity entry is not addressable by `rename`", which this phase makes
false); its config semantics and test-guarded output are unchanged.

**Tests (author step — new and intent-changing):**

- `rename: {record_index: id}` under a `record_index` election: message key map,
  after-image entry, Debezium `d` before-image, and Debezium value schema all
  carry `id`; values identical to the unrenamed run (presentation invariance)
- No rename: elected surface ships under its contract column name (today's
  behavior preserved for the default)
- Under a `presentation_id` election, the surface appears exactly once, as the
  identity entry (absorption branch gone, no duplicate)
- Under a `record_id` / `record_index` election, `presentation_id` no longer
  appears in the after-image even when the kind mints one
- Reserved-name set follows the resolved key: `rename: {record_index: status}`
  colliding with a selected `status` property → `StreamOutputNameCollision`;
  `rename: {status: record_index}` legal when `record_index` is not published
- `rename` keyed on an unpublished surface name → `StreamRenameUnresolvable`
  with the published-set suffix; a plain property typo gets no suffix
- Membership stream: owner identity entry re-keys the same way (key map,
  after-image, `d` before-image, value schema)
- Kafka message-key bytes change under an identity rename (the one-entry map
  carries the entry name); topic assignment, `seq`, `ts`, values unchanged
- Mixer/pacer event preservation invariants hold over the reduced `StreamEvent`
  field set (no `presentation_id` field anywhere)

**Tests (migrate step):** the 10 fan-out files adapt constructors and expected
wire output to the removed field / resolved `key_column`, intent preserved.

### Phase 3: Streaming identity projection

**Delivers:** the per-stream `identity` declaration on both stream shapes —
multi-surface publication in sidecar column order, every published surface run
through the election's resolution gates, union-safety algebra, and the
render-time uniqueness guard; identity surfaces barred from `properties`.

**Demo:** streams with `identity: [record_index, presentation_id]` and renames —
prints wire lines carrying both surfaces under their wire names; then triggers
`ElectionPresentationUndeclared` by publishing `presentation_id` on an
undeclared population, and `StreamPropertyNotAddressable` by listing an identity
surface in `properties`.

**Contracts:** `resolve_identity_projection`; `KindStream.identity` /
`MembershipStream.identity` + validator widening; new error classes
`StreamIdentityMissingElected`, `StreamIdentityUnavailable`,
`StreamPropertyNotAddressable`.

**Steps:** `source → author (6 files)` — source reshape and the new test suite
both read the same deep election/engine surface, so each gets a fresh context
(the per-phase self-check); migration is nil because Phase 2 already landed the
projection-shaped API.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/presentation.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `tests/config/test_stream_config.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Modify | `tests/exporters/streaming/test_election_stream.py` |
| Modify | `tests/exporters/streaming/test_presentation.py` |
| Modify | `tests/exporters/streaming/test_debezium.py` |
| Modify | `tests/exporters/streaming/test_jsonl.py` |
| Create | `docs/sprints/author-selectable-identity/demos/phase_3_identity_projection.py` |

**Tests:**

- Parse time: `identity: []` refused; duplicates refused; unknown surface name
  unrepresentable (Literal); absent `identity` parses
- `identity` omitting the elected surface → `StreamIdentityMissingElected` with
  the design doc's message
- `identity: [.., presentation_id]` on a kind minting no surrogate →
  `StreamIdentityUnavailable`
- `presentation_id` published on a registry-undeclared population →
  `ElectionPresentationUndeclared` naming stream and population (the deliberate
  tightening — publishing requires the claim)
- A published surface whose spanned key spaces overlap →
  `ElectionUnionUnsafe` naming stream, surface, and pair
- Render-time duplicate on a published non-elected surface →
  `ElectedKeyDuplicate` reading as that surface's failure
- An identity surface in `properties` → `StreamPropertyNotAddressable`; a
  producer payload property named `record_index` (`prop__record_index`) is
  unaddressable, full stop
- Publication order is sidecar column order regardless of declaration order
  (`identity: [record_index, presentation_id]` and the reverse render the same)
- Published non-elected surfaces appear in after-image and Debezium value schema,
  never in the message key
- Membership stream: owner projection admits the three surfaces against the
  owner's election, rendered ahead of element fields (after Debezium `event`)
- `identity` surfaces stay outside `only` / `ignore` / `where` / `render`
  (existing refusal identities: `StreamChangeScopeUnresolvable`,
  `RenderKeyResolves` — one representative case each)
- A mixed-election stream with a malformed `identity` reports
  `ElectionMixedIdentity` (ordering: election gates first)

### Phase 4: Playback tier-1 identity projection

**Delivers:** `RecordAtomSelection.identity` — the caller controls which identity
columns the tier-1 event `after` map and the `record_state` snapshot carry.
Projection only: no gate, no election import, applied above the composed
relations.

**Demo:** plays a small emit twice — default (`None`: `record_id` +
`presentation_id`) and `identity=("record_id",)` (surrogate suppressed in the
`after` map and absent from `record_state` columns) — printing both; typed
`PlaybackEvent.presentation_id` stays populated in both runs.

**Contracts:** `RecordAtomSelection.identity`, `ResolvedRecordSelection.identity`,
`resolve_selection` rules.

**Steps:** none (single implementer).

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/playback/types.py` |
| Modify | `src/fabulexa_forge/playback/selection.py` |
| Modify | `src/fabulexa_forge/playback/events.py` |
| Modify | `src/fabulexa_forge/playback/snapshot.py` |
| Modify | `tests/playback/test_selection.py` |
| Modify | `tests/playback/test_events.py` |
| Modify | `tests/playback/test_snapshot.py` |
| Create | `docs/sprints/author-selectable-identity/demos/phase_4_playback_identity.py` |

**Tests:**

- One positive + one negative per new `resolve_selection` rule (shape, domain,
  spine, availability, properties-disjoint), matching the seam's per-rule test
  convention and the design doc's messages
- `identity=None` resolves to the full available set — `record_id` alone on a
  surrogate-less kind, `record_id` + `presentation_id` on a minting kind; event
  and snapshot output byte-identical to pre-sprint behavior
- `identity=("record_id",)` on a minting kind: `after` map carries no
  `presentation_id` entry; `record_state` has no `presentation_id` column;
  `PlaybackEvent.presentation_id` still populated
- `record_index` in `identity` → `PlaybackError` (outside the tier-1 domain)
- Projection changes neither the event row set nor `seq` (seq-invariance under
  differing `identity`)
- On a corrupted tape, a published colliding surrogate is delivered verbatim
  (permissive playback — no uniqueness check at the seam)
- The layer-direction AST test still passes (no election import; surface names
  are string literals)

### Phase 5: The `init` election menu

**Delivers:** every mode's `init` proposes the election as a visible menu —
uniform `record_index` active, commented per-population alternatives by
resolvability — through one shared renderer; the degradation mechanism retires.

**Demo:** runs `init` proposal + renderer against a fixture emit with a
registry-declared population and a partitioned kind — prints the menu block
(scalar and map shapes, alternatives, the swap-not-uncomment header); then
writes a config with an alternative activated *alongside* the active line and
shows the Phase-1 duplicate-key refusal.

**Contracts:** `KeyElectionProposal`, `propose_key_election`, `render_keys_block`.

**Steps:** `source → author (4 files)` — the existing init tests' degradation
assertions are intent-changing rewrites (the mechanism is retired), judged
per-file against this spec, plus a new unit-test file for the shared module.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/keys_init.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/init.py` |
| Modify | `src/fabulexa_forge/exporters/source/init.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/init.py` |
| Create | `tests/exporters/test_keys_init.py` |
| Modify | `tests/test_cli_init.py` |
| Modify | `tests/exporters/source/test_init.py` |
| Modify | `tests/exporters/streaming/test_init.py` |
| Create | `docs/sprints/author-selectable-identity/demos/phase_5_init_menu.py` |

**Tests:**

- Active proposal is `record_index` for every population of every kind
- `presentation_id` alternative emitted only for registry-declared populations;
  `record_id` alternative always
- Partitioned kind, no sub-type declared → scalar active line; ≥1 declared →
  per-sub-type map (shape follows the alternatives, not the active values)
- The comment block leads with the swap-not-uncomment line; alternatives precede
  the active line
- All three mode `init`s splice byte-identical menu blocks for the same emit
- Degradation is gone: no `NOTE: ElectionUnionUnsafe` comment path; the union-
  unsafe fixture that used to degrade now emits the uniform proposal clean
- Dimensional: proposed dim key columns source from `record_index`; the
  `presentation_id` advisory comment appears wherever a surrogate is declared
- `init` proposes no `identity` block (streaming's never-proposed trailing
  comment covers it)
- Every generated streaming config still parses and streams clean end-to-end
  (existing liveness posture)
- Incoherent presentation-key block still refuses (`ExportError`)

## What Doesn't Change

Per the design doc § What Doesn't Change — binding for review:

- **The derivations layer**: row-state-events fold, its column-order producer,
  fold-row column list, and the state-at resident are untouched; every
  projection applies above the composed relation
- **Key election's semantics**: grammar, three surfaces, resolution/combination
  gates, union-safety algebra, identity join relations, uniqueness guard —
  widened population, unchanged rules; absent `keys` still elects `record_id`
- **Elected values**: every identity site renders the value it renders today;
  this sprint changes names and which surfaces publish, never a value
- **Event sets, ordering, `seq`, `ts`, merge order, topic assignment, routing**;
  the canonical order and merge key still read the fold's `record_id`
- **Reference and member-field rendering** (target's elected surface; `__kind`
  disambiguator)
- **Source, base, and dimensional export rendering** — only `init` proposals
  change; base keeps its auto-projected standalone surrogate (design §
  Boundaries)
- **Playback tier 2**, membership atoms, and the typed `PlaybackEvent` fields
- **Change scope / row selection / value-rendering election domains**
- **The membership `event` reservation**; payload key bareness (`prop__` /
  `elem__` / `member__` never reach the wire)
- **The mixer's semantics** — pass-through; only its invariant's field list
  shrinks with `StreamEvent`

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/loader.py` | Shared `load_yaml_mapping` with duplicate-key refusal; three loaders route through it |
| `src/fabulexa_forge/config/models.py` | `identity` on `KindStream` / `MembershipStream` + validator shape rules |
| `src/fabulexa_forge/errors.py` | `StreamIdentityMissingElected`, `StreamIdentityUnavailable`, `StreamPropertyNotAddressable` |
| `src/fabulexa_forge/exporters/streaming/types.py` | `StreamEvent`: drop `presentation_id`; `key_column` = resolved output key |
| `src/fabulexa_forge/exporters/streaming/presentation.py` | `IdentityProjection`, `OutputEntry`, `resolve_identity_output_key`; resolvers widen; absorption removed; reserved set = resolved keys |
| `src/fabulexa_forge/exporters/streaming/engine.py` | Projection resolution + gates; per-surface identity relations; eager-pass rules; event production |
| `src/fabulexa_forge/exporters/streaming/driver.py` | Debezium value-schema recompute follows the widened resolver |
| `src/fabulexa_forge/exporters/streaming/debezium.py` | Value schema / `d` before-image consume `OutputEntry` + resolved keys |
| `src/fabulexa_forge/playback/types.py` | `RecordAtomSelection.identity` (default `None`) |
| `src/fabulexa_forge/playback/selection.py` | `ResolvedRecordSelection.identity`; five new `resolve_selection` rules |
| `src/fabulexa_forge/playback/events.py` | `after` map honors the resolved projection |
| `src/fabulexa_forge/playback/snapshot.py` | `record_state` identity columns projected above the relation |
| `src/fabulexa_forge/exporters/keys_init.py` | `KeyElectionProposal` / `propose_key_election` / `render_keys_block`; degradation deleted |
| `src/fabulexa_forge/exporters/dimensional/init.py` | Splice new menu; alignment follows active election; advisory comment re-emitted |
| `src/fabulexa_forge/exporters/source/init.py` | Splice new menu; degradation gate call removed |
| `src/fabulexa_forge/exporters/streaming/init.py` | Splice new menu; `_self_gate_streaming_keys` deleted |
| `examples/recipes/streaming/stream-rename/config.yaml` | Comment prose corrected (identity entry now renameable) |
| `tests/…` | Per-phase tables above |
