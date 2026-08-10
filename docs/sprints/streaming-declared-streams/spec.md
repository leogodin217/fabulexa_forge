# Sprint: streaming-declared-streams

Implements [`docs/architecture/pending/streaming-declared-streams.md`](../../architecture/pending/streaming-declared-streams.md)
(the design doc — semantics, rationale, validation-rule tables, and the config
model contracts live there; this spec adds phases, file scope, and test cases
and does not restate them).

## Purpose

Replace the streaming exporter's selection + routing grammar with author-named
declared streams, make every stream's event set payload-independent, elect the
message key through the shipped `keys` surface, and ship `init --mode streaming`
— so a streaming author declares realistic per-population feeds by name, exactly
as the source mode's author declares tables.

An author writes `streams: [{name, kind, sub_types, properties}, …]` (the name
*is* the topic), optionally `keys: {<kind>: presentation_id}`, and runs
`fabulexa-forge stream`; a blank-page author runs `fabulexa-forge init --mode
streaming` first and edits the proposal.

## Scope

**Capabilities touched:**
- Derivations — row-state-events: the change-scope × projection contract split
  (not: membership-events or any other resident)
- Streaming exporter: the `streams` grammar replacing `kinds` / `memberships` /
  `RoutingConfig`; per-stream folds and payload-independent event sets;
  stream-name merge component; per-stream Debezium value schemas;
  `table_identity` re-homed into `DebeziumConfig` (not: content/format/sink
  axes, pacing, mixer scheduler, Kafka sink mechanics)
- Key election: streaming as the fourth consuming mode — `StreamConfig.keys`,
  the one-stream-one-key-surface gate, three render sites (not: any change to
  the shipped gates, algebra, derivations, or other modes' rendering)
- Streaming init: new `init --mode streaming` proposal engine + CLI arm (not:
  dimensional/source init)
- Existing streaming recipes + demo presets: migrated to the declared-stream
  grammar

**Not included:**
- New author-facing recipes for the new capabilities (election keys,
  notification feeds, combined streams, init) — post-sprint recipe pass per
  `docs/PROCESS.md` § Authoring Documentation
- Architecture-doc folding and `docs/recipes/README.md` index updates —
  post-sprint doc commit
- The two pre-existing stale lines in `streaming.md` / `streaming-routing.md`
  (retired `StreamTypesRequireRegistry` mention; stale membership-Debezium
  refusal claim) — separate one-line doc fix

## Breaking Changes

Internal-greenfield breaks; no compatibility shims anywhere (Principle #9).

- **`StreamConfig` grammar**: `kinds`, `memberships`, and `routing` are
  deleted, replaced by the required `streams` list (`KindStream` /
  `MembershipStream` discriminated union). `RoutingConfig`,
  `StreamKindSelection`, `MembershipSelection` are deleted. Every existing
  stream YAML (tests, recipes, presets) must be rewritten — no old-shape config
  parses.
- **`DebeziumConfig` gains `table_identity`** (moved from `RoutingConfig`,
  meaning unchanged, default `source_table` — the shipped default carried over).
- **`build_row_state_events_sql` signature**: gains a required `change_scope`
  parameter (no default — every caller states both scopes). Source and playback
  callers pass equal sets; their output is byte-identical.
- **`build_topic_set` signature**: becomes a pure function of the config (the
  topic set is the declared name list; no sidecar fan-out).
- **Retired public functions**: `resolve_topic`, `enumerate_topics` (Layer B).
  `route_attributes` / `membership_route_attributes` / `resolve_subtype_index`
  survive (Layer A).
- **Behavioral**: a stream's `u` event set is payload-independent (grows for
  subset selections; `properties: []` now carries the full event set); topic
  names are author-declared (default proposals differ from the old
  `{route_table}` rendering only where the author names them differently);
  overlapping streams legally duplicate events across topics with distinct
  `seq`.
- **Phase 3**: `StreamEvent` gains `key_column` / `key_value`; with no `keys`
  block every rendered byte is identical to phase 2 output.

## Success Criteria

- [ ] A polymorphic kind streams as per-sub-type feeds with per-stream column
      lists (no structurally-inapplicable NULLs unless the author combines
      sub-types) — design doc Problem 1
- [ ] Renaming a topic = editing `name`; combining sub-types = one stream
      listing them; `routing:` no longer parses — Problem 2
- [ ] Two streams over one population have identical event sets whatever their
      `properties`; `properties: []` is a notification feed — Problem 3
- [ ] `keys: {<kind>: presentation_id}` keys every message (incl. `d`
      tombstones) and re-renders after-image identity + references in elected
      surfaces; absent `keys` → byte-identical output — Problem 4
- [ ] `fabulexa-forge init --mode streaming` emits a commented candidate config
      that parses and streams clean against the emit — Problem 5
- [ ] `make check` green; source event log and playback outputs byte-identical
      to baseline

## Contracts

Config models (`KindStream`, `MembershipStream`, `StreamDeclaration`,
`StreamConfig`, `DebeziumConfig`) and `generate_stream_init_config` are
specified in full in the design doc § Interface Contracts — implement as
written there. Contracts new to this spec:

```python
def build_row_state_events_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    properties: frozenset[str],
    change_scope: frozenset[str],
) -> str:
    """Build the canonical row-state-events SELECT for one kind.

    The two-scope contract (design doc § Per-stream folds and after-images):
    event membership and after-image projection are independently scoped.
    One event row per (record_id, sim_time) at which the record's state
    changes: a 'c' at created_sim_time for every record, a 'u' at each later
    distinct history sim_time of `change_scope`'s history-tracked subset, and
    a 'd' at deactivated_at when deactivated. The after-image columns are
    `properties` resolved by resolve_stream_columns — the projection scope
    never widens or narrows the event set, and the change scope never adds a
    column to the SELECT. Both scopes partition by the sidecar
    history_tracked flag exactly as shipped; a current-value name in
    change_scope contributes no change points (it has no history rows).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose row state is reconstructed.
        properties: The after-image property names (bare), of either class;
            may be empty (identity + lifecycle only).
        change_scope: The property names (bare) whose history-tracked subset
            drives 'u' event membership; may equal `properties` (the shipped
            single-scope behavior, byte-identical) and may be a superset or
            disjoint. Callers state both scopes explicitly — no default.

    Returns:
        A complete, deterministic SELECT producing ROW_STATE_EVENT_COLUMNS
        (plus presentation_id when present, plus one prop__<p> per selected
        property), ordered by (event_sim_time, event_class, record_id).

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar (defensive;
            engine validation catches first).
        ExportError: A name in `properties` or `change_scope` has no
            prop__<name> column on the kind (likewise defensive).
    """
```

```python
def iter_stream_events(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
) -> Iterator[StreamEvent]:
    """Yield the stream's events in canonical total order with seq stamped.

    Signature unchanged; behavior per the design doc: the eager pass runs the
    declared-stream business rules and (phase 3) the election gates; the
    inner generator materializes one fold per declared stream (kind-shaped:
    change scope = the kind's full property set, projection = the stream's
    declared properties; membership-shaped: unchanged), drops rows outside
    the stream's sub_types scope post-fold via the discriminator index,
    k-way-merges under (event_sim_time, event_class, stream_name, record_id),
    stamps seq, renders ts, stamps topic = the declaring stream's name and
    route_table = the per-event leaf, and (phase 3) renders key_column /
    key_value and the elected after-image through the identity indexes.

    Raises:
        ExportError: An eager business rule failed (design doc § Business
            Rules) — message leads with the stream name.
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
```

```python
def build_topic_set(
    config: "StreamConfig",
) -> tuple[str, ...]:
    """The run's topic set: the declared stream names, in declaration order.

    Pure function of the config — declared intent, not observed rows, drives
    topic existence (the declared-but-empty guarantee is keyed on this set).
    Names are unique by the stream_names_unique validator, so the tuple is
    duplicate-free by construction.

    Args:
        config: The validated streaming configuration.

    Returns:
        Each stream's `name`, config order.
    """
```

```python
@dataclass(frozen=True)
class StreamEvent:
    """Phase 3 — two fields added; all shipped fields unchanged.

    key_column: str
        The message-key entry's column name: the elected surface's contract
        column name for the event's population ('record_id' when no election
        applies — the default rendering). For membership-events, the owner's
        elected surface.
    key_value: str
        The codec-rendered elected key value (record_id verbatim;
        record_index digit-form; presentation_id codec rendering). Equals
        record_id under the default. Renderers build the key map as
        {key_column: key_value}; ordering and merge still read record_id.
    """
```

```python
def build_elected_identity_index(
    emit: "Emit",
    fork_path: str,
    kind: str,
    surface: KeySurface,
) -> dict[str, str]:
    """record_id → codec-rendered elected value for one kind, end-of-tape.

    Composes the record-index or presentation-key derivation at the
    end-of-tape entry point (a record's creation precedes its every event —
    the event-log horizon argument) and runs the elected-key uniqueness guard
    over the drawn rows: rows == DISTINCT record_id == DISTINCT elected
    value, elected value non-NULL. The one data-touching election check
    (key-election.md § The elected-key uniqueness guard); population-set
    restriction to a stream's sub_types subset composes the records-spine
    discriminator as a semi-join at the call site.

    Args:
        emit: The open emit.
        fork_path: The sole branch.
        kind: The records kind.
        surface: 'record_index' or 'presentation_id' (a 'record_id' election
            composes no relation and never calls this).

    Returns:
        The identity map, every value a non-null str.

    Raises:
        ExportError: ElectedKeyDuplicate — the guard failed; names the
            stream or edge and the surface.
    """
```

```python
def elect_after_image_columns(
    columns: list[str],
    surface: KeySurface,
) -> list[str]:
    """The elected after-image column list — the single re-key/absorb rule.

    Transforms a resolve_stream_columns order for one stream under its
    population's elected surface: the leading 'record_id' entry is re-keyed
    to the surface's contract column name; under 'presentation_id' the
    standalone 'presentation_id' entry is absorbed (it IS the identity —
    emitting both would duplicate a column); under 'record_id' /
    'record_index' the surrogate ships verbatim when present. Both the
    Debezium value-schema builder and the after-image rendering consume this
    one output, so the declared schema and the rendered rows stay the same
    list by construction.

    Args:
        columns: The resolve_stream_columns order for the stream.
        surface: The stream's elected surface.

    Returns:
        The rendered after-image column order.
    """
```

`generate_stream_init_config(emit, notice_sink) -> str` and
`StreamInitNothingToStream` — design doc § Interface Contracts, verbatim.
Election resolution/gates reuse `resolve_election` and the shipped gate
helpers in `exporters/election.py`; the `keys` proposal reuses
`exporters/keys_init.py`.

## Phases

### Phase 1: Fold change-scope split

**Delivers:** The two-scope row-state-events contract; all three shipped
consumers pass both scopes explicitly (equal sets — byte-identical behavior).

**Demo:** `phase_1_fold_split.py` — builds a minimal emit
(`tests/_support/sidecar_builder.write_emit`), invokes the fold with
`change_scope` ⊃ `properties` and with `properties=frozenset()`, prints the
event rows: `u` events fire at non-projected columns' change points with
identity-only after-images.

**Contracts:** `build_row_state_events_sql` (above).

**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/derivations/row_state_events.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `src/fabulexa_forge/playback/events.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `tests/derivations/test_row_state_events.py` |
| Create | `docs/sprints/streaming-declared-streams/demos/phase_1_fold_split.py` |

**Tests:**
- `change_scope == properties`: SQL and rows byte-identical to the shipped
  single-scope invocation (regression pin)
- `change_scope ⊃ properties`: a `u` fires at a change point of a tracked
  column not in `properties`; its after-image carries only the projected
  columns, reconstructed at that event time
- `properties=frozenset()`, non-empty `change_scope`: full `c`/`u`/`d` event
  set, after-image is identity-only (`record_id` + `presentation_id?`)
- A current-value (non-tracked) name in `change_scope` contributes no `u`
  events
- `change_scope` disjoint from `properties`: event set follows change_scope,
  payload follows properties
- A bad name in `change_scope` raises `ExportError` naming the column
- Existing source event-log and playback consistency suites pass unchanged
  (their callers pass equal sets)

### Phase 2: Declared-stream grammar, engine, and delivery rework

**Delivers:** The `streams` grammar end-to-end — models, engine (per-stream
folds, payload-independent event sets, stream-name merge), Layer-B
retirement, driver + Debezium per-stream schemas, CLI, and every existing
config surface (tests, recipes, presets) migrated. Atomic: the suite is red
between steps and green at the phase gate.

**Demo:** `phase_2_declared_streams.py` — builds a sub-typed-kind emit,
streams a config with per-sub-type streams, a combined stream, a renamed flat
kind, and a `properties: []` notification feed to a temp dir; prints the
topic file set (= declared names), per-topic counts including a
declared-but-empty `0`, and the same base change appearing once per covering
stream with distinct `seq`.

**Contracts:** config models (design doc), `iter_stream_events`,
`build_topic_set` (above).

**Steps:** `source (models+engine+routing) → source (driver+debezium+cli) →
migrate (fan-out, 5 files) → author (config tests) → author (engine/routing
tests) → author (recipes + presets)` — mirrors `state.yaml`.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/config/__init__.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/routing.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/types.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/__init__.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/driver.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/debezium.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `tests/exporters/streaming/test_driver.py` |
| Modify | `tests/exporters/streaming/test_mixer.py` |
| Modify | `tests/test_cli_stream.py` |
| Modify | `tests/test_cli_mixer.py` |
| Modify | `tests/integration/kafka/test_kafka_cli.py` |
| Modify | `tests/config/test_stream_config.py` |
| Delete | `tests/config/test_routing_config.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Modify | `tests/exporters/streaming/test_routing.py` |
| Modify | `tests/exporters/streaming/test_routing_engine.py` |
| Modify | `tests/recipes/test_stream_recipes.py` |
| Modify | `tests/recipes/test_demo_presets.py` |
| Modify | `examples/recipes/streaming/*/config.yaml` (15 recipes; routing-* dirs replaced by declared-stream equivalents) |
| Modify | `examples/recipes/streaming/membership-events/expect.yaml` |
| Modify | `examples/recipes/streaming/debezium-membership-events/expect.yaml` |
| Modify | `docs/examples/nhs/stream.yaml` |
| Modify | `docs/examples/retail/stream.yaml` |
| Modify | `docs/examples/ride-sharing/stream.yaml` |
| Modify | `docs/examples/ride-sharing-marketplace/stream.yaml` |
| Create | `docs/sprints/streaming-declared-streams/demos/phase_2_declared_streams.py` |

Preset migration constraint: each preset's declared stream names must equal
its old rendered topic set — `demo.yaml` joins and the mixer UI key on them
(`tests/recipes/test_demo_presets.py` guards this).

**Tests:**
- Parse: an entry with both `kind` and `membership`, or neither, fails naming
  the two shapes; `sub_types: []` and duplicate values fail; `properties`
  omitted on a kind stream fails (explicit `[]` parses); name rule
  (`^[A-Za-z0-9._-]+$`, not `.`/`..`) enforced on every stream name; two
  streams sharing a name fail; same kind in two streams parses;
  content/shape mismatch fails; `routing:` no longer parses;
  `debezium.table_identity` parses with default `source_table`
- Business rules (each message leads with the stream name): unknown kind;
  `sub_types` on a flat kind; undeclared sub_type value; unresolvable
  property; non-exempt slice_only property refused; unknown membership
  table; unresolvable field
- Event set: two streams over one population with different `properties`
  yield identical (op, record_id, event_sim_time) sequences; `properties:
  []` yields the full event set with identity-only after-images; a
  sub-typed stream's `u` set spans the kind's tracked columns
- Combined stream: one column list, NULL in a selected column the row's
  sub-type does not declare
- Merge: same-instant same-class events across streams interleave by stream
  name; `seq` is global 1-based; overlapping streams emit one event per
  covering stream with distinct `seq` and identical key
- Topics: topic set = declared names in declaration order; zero-event stream
  → empty `<name>.jsonl` / `events_per_topic == 0`; `StreamEvent.topic` is
  the declaring name; `route_table` is the leaf (sub-type value / bare kind /
  `<owner>__<property>`)
- Debezium: value schema built per stream for both `table_identity` values;
  `source.table` reports `route_table` inside a combined stream under
  `source_table` and the stream name under `topic`; retired
  `StreamTopicSchemaUnambiguous` has no successor test
- Determinism: same emit + config + code → byte-identical stream (existing
  pins re-anchored)
- CLI: stream + mixer verbs run the new grammar end-to-end; kafka
  integration config rewritten

### Phase 3: Message-key election

**Delivers:** `StreamConfig.keys` — the cross-mode election grammar wired
into streaming: static gates, identity indexes, the uniqueness guard, and
the three render sites (message key incl. tombstones, after-image identity
with absorption, references/member fields in target surfaces). Additive:
absent `keys` → byte-identical output.

**Demo:** `phase_3_key_election.py` — streams one emit twice (no `keys`;
`keys: {<kind>: presentation_id}` + a `record_index` kind), prints a `u` and
a `d` message from each run showing the key map, the re-keyed after-image
identity, absorption, and a reference rendered in its target's surface.

**Contracts:** `StreamEvent` additions, `build_elected_identity_index`,
`elect_after_image_columns` (above); gates reused from
`exporters/election.py`.

**Steps:** `source → author` (the election render sites and the enumerative
test suite read the same deep surface; fresh context each).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/types.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/jsonl.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/debezium.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/driver.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/kafka_sink.py` |
| Create | `tests/exporters/streaming/test_election_stream.py` |
| Modify | `tests/config/test_stream_config.py` |
| Modify | `tests/exporters/streaming/test_jsonl.py` |
| Modify | `tests/exporters/streaming/test_debezium.py` |
| Create | `docs/sprints/streaming-declared-streams/demos/phase_3_key_election.py` |

**Tests:**
- No `keys` → byte-identical stream output (golden pin against phase-2
  rendering), `key_column == 'record_id'`, `key_value == record_id`
- `presentation_id` election: key map `{presentation_id: <value>}` on every
  op; `d` tombstone and Debezium key-only before-image carry the same one
  entry; after-image identity entry re-keyed; standalone `presentation_id`
  absorbed (no duplicate column); Debezium value schema follows
  `elect_after_image_columns`
- `record_index` election: digit-form str values; surrogate ships verbatim
  beside it when the kind carries one
- Reference `prop__` entries render the target's elected surface; membership
  `member__<f>` fields render the member kind's surface; the membership
  owner entry re-keys in both formats (element-field format parity holds)
- Gates: mixed election across a stream's spanned populations →
  `ElectionMixedIdentity` naming the stream; union-unsafe uniform
  `presentation_id` → `ElectionUnionUnsafe`; edge over union-unsafe admitted
  targets (full declared domain) → `ElectionUnionUnsafe` naming stream +
  column; unknown kind/sub-type/undeclared registry → shipped resolution
  errors
- Guard: a duplicated/mutated `presentation_id` emit fails
  `ElectedKeyDuplicate`
- Ordering: election never re-sorts — `seq` and inter-stream interleave
  identical with and without `keys`
- Key map never schema-wrapped under `schemas_enable` (shipped rule pinned
  against the elected entry)

### Phase 4: `init --mode streaming`

**Delivers:** The sidecar-driven proposal engine (design doc § `init --mode
streaming` inference contract) and its CLI arm.

**Demo:** `phase_4_init_streaming.py` — runs `generate_stream_init_config`
against a fixture emit (sub-typed kind + flat kind + membership table +
partial `presentation_keys`), prints the proposal, then parses it with
`load_stream_config` and streams it clean — the self-gate proved live.

**Contracts:** `generate_stream_init_config`, `StreamInitNothingToStream`
(design doc, verbatim).

**Steps:** `source → author` (the proposal rules and their enumerative tests
read the same sidecar surface; fresh context each).

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/streaming/init.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/__init__.py` |
| Create | `tests/exporters/streaming/test_init.py` |
| Modify | `tests/test_cli_init.py` |
| Create | `docs/sprints/streaming-declared-streams/demos/phase_4_init_streaming.py` |

**Tests:** (one per design-doc proposal-table row, plus)
- Flat kind → one live stream, payload-role `prop__` columns bare, minus
  non-exempt slice_only
- Sub-typed kind → one live stream per sub-type in domain order, properties
  from the `sub_type_columns` partition, discriminator not proposed
- Missing partition → union fallback with comment
- Lifecycle-only population → live stream under advisory comment
- Name collision (two proposals, one name; and the membership `<K>_<p>`
  underscore ambiguity) → later entry commented, config parses
- Topic-illegal sub-type value → commented with rule + value; never
  sanitized
- `keys` proposal: `presentation_id` for registry-declared, `record_index`
  otherwise; gate failure degrades the kind to uniform `record_index` with
  comment
- Membership alternative fully commented; uncommenting it wholesale parses
  and streams clean
- No records kind → `StreamInitNothingToStream`; all-names-illegal →
  `StreamInitNothingToStream`
- Emit predating temporal classes → `TemporalClassUnavailableError`
  propagates
- Non-exempt slice_only column → never proposed + one
  `slice-only-column-omitted` notice each
- Emitted text round-trips: parses into a valid `StreamConfig` and
  `iter_stream_events` runs clean against the emit
- CLI: `init --mode streaming` writes the proposal; no-records emit exits
  non-zero with the error on stderr

## What Doesn't Change

- `derivations/membership_events.py`, `state_at`, `record_index`,
  `presentation_key`, `versioned_intervals`, `reference_resolution`,
  `truncated_tape` — untouched residents
- Source event-log and playback **behavior** — their fold call sites pass
  equal scopes; every existing suite passes unchanged
- `exporters/election.py` and `exporters/keys_init.py` internals — streaming
  consumes them as shipped; no gate or algebra changes
- Pacing (`pacer.py`, `ClockConfig`, CLI knobs), the mixer scheduler
  (`mixer/`), and the control-API contract — timing overlays; they consume
  the topic set and `StreamEvent`s as before
- The pinned encoder (`encoding.py`), JSONL object layout, Debezium envelope
  shape, `ts` rendering, anchor resolution, `seq` stamping mechanics
- Kafka sink mechanics (pre-creation, single partition, flush-before-return);
  only the key bytes follow `key_value` in phase 3
- The `stream` verb's flag surface; dimensional / source / base modes and
  their init engines
- The internal canonical ordering key stays `record_id`-based — election
  renders identity, never re-sorts

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/derivations/row_state_events.py` | Two-scope fold contract (`change_scope` × `properties`) |
| `src/fabulexa_forge/exporters/source/events.py` | Pass audited set as both scopes |
| `src/fabulexa_forge/playback/events.py` | Pass full set as both scopes |
| `src/fabulexa_forge/config/models.py` | `KindStream`/`MembershipStream`/`streams`; delete `RoutingConfig`/selections; `DebeziumConfig.table_identity`; P3: `StreamConfig.keys` |
| `src/fabulexa_forge/config/__init__.py` | Export the new models; drop the deleted ones |
| `src/fabulexa_forge/exporters/streaming/engine.py` | Per-stream folds, payload-independent events, stream-name merge, declared-name topics, new business rules; P3: election gates + render wiring |
| `src/fabulexa_forge/exporters/streaming/routing.py` | Layer B retired; Layer A (`route_table`) kept |
| `src/fabulexa_forge/exporters/streaming/types.py` | Docstring updates; P3: `key_column`/`key_value` |
| `src/fabulexa_forge/exporters/streaming/driver.py` | Declared-name topic set, per-stream Debezium schemas, retired ambiguity rule |
| `src/fabulexa_forge/exporters/streaming/debezium.py` | Per-stream value schemas; P3: elected key map + before-image |
| `src/fabulexa_forge/exporters/streaming/jsonl.py` | P3: elected key map + after-image identity |
| `src/fabulexa_forge/exporters/streaming/kafka_sink.py` | P3: key bytes from `key_value` |
| `src/fabulexa_forge/exporters/streaming/__init__.py` | Surface updates (drop Layer B; P4: init export) |
| `src/fabulexa_forge/exporters/streaming/init.py` | New: `generate_stream_init_config` |
| `src/fabulexa_forge/errors.py` | New: `StreamInitNothingToStream` |
| `src/fabulexa_forge/cli.py` | Mixer verb off `RoutingConfig`; `init --mode streaming` arm |
| `tests/derivations/test_row_state_events.py` | Two-scope migration + split-semantics tests |
| `tests/config/test_stream_config.py` | Declared-stream grammar tests (+P3 keys) |
| `tests/config/test_routing_config.py` | Deleted (name rule moves to stream-name tests) |
| `tests/exporters/streaming/test_engine.py` | Event-set + merge + business-rule rewrite |
| `tests/exporters/streaming/test_routing.py` | Layer A only |
| `tests/exporters/streaming/test_routing_engine.py` | Declared-stream rules rewrite |
| `tests/exporters/streaming/test_driver.py` | Grammar migration |
| `tests/exporters/streaming/test_mixer.py` | Grammar migration |
| `tests/exporters/streaming/test_jsonl.py` | P3: key-map cases |
| `tests/exporters/streaming/test_debezium.py` | P3: key-map + schema cases |
| `tests/exporters/streaming/test_election_stream.py` | New: election gates + render sites + guard |
| `tests/exporters/streaming/test_init.py` | New: proposal-rule suite |
| `tests/test_cli_stream.py` / `tests/test_cli_mixer.py` | Grammar migration |
| `tests/test_cli_init.py` | `--mode streaming` cases |
| `tests/integration/kafka/test_kafka_cli.py` | Grammar migration |
| `tests/recipes/test_stream_recipes.py` / `test_demo_presets.py` | Recipe/preset alignment |
| `examples/recipes/streaming/**` | 15 recipes rewritten (routing-* replaced) |
| `docs/examples/*/stream.yaml` | 4 presets rewritten (topic names preserved) |
| `docs/sprints/streaming-declared-streams/demos/phase_*_*.py` | 4 demo scripts |
