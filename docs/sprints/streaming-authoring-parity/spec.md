# Sprint: streaming-authoring-parity

## Purpose

Give the streaming exporter the three author surfaces every batch mode has and the
live feed lacks — per-stream output vocabulary (`rename` + kind labeling), row
selection (`where` + membership owner `sub_types`), and change scope
(`only` / `ignore`) — so an author can stream a domain-vocabulary feed
("`session_id`", "`security_event`") of exactly the rows and changes they declare,
from YAML alone.

Design doc: `docs/architecture/pending/streaming-authoring-parity.md` (the WHY and
the full semantics — referenced per phase below, never duplicated here).

## Scope

**Capabilities touched:**

- streaming exporter (◐): bare-name wire keys, per-stream `rename` / `kind_label`,
  config `kind_labels`, `where` on both stream shapes, owner `sub_types` on
  `MembershipStream`, `only` / `ignore` change scope, required `notice_sink`
  threading, membership key-uniformity gate over the addressed owner set
- export-config models: new fields + parse-time validators on `KindStream` /
  `MembershipStream` / `StreamConfig`
- row-predicate grammar: two new consumer surfaces (`streams[].where`); grammar
  itself unchanged
- selection-spine device: promoted from source-private to a mode-neutral module
  (source re-imports; behavior unchanged)
- streaming mixer: mechanical `notice_sink` pass-through on `seed_mixer_run`

**Not included:** windowed/incremental streaming, new content/format/sink, Debezium
key message, temporal elections on streams, base-mode `where`, `init` proposals of
the new fields (it proposes none, by design), new recipes for the new features
(recipe creation is its own post-ship lifecycle step).

## Breaking Changes

1. **Bare-name wire keys (deliberate breaking wire change).** After-image payload
   keys drop the `prop__` / `elem__` prefixes; a membership reference field
   `member__<f>__kind` / `member__<f>__id` becomes `<f>_kind` / `<f>_id`. Identity
   entries, `presentation_id`, and the Debezium membership `event` column keep
   their contract names. Every streaming test asserting wire keys and 13 of the 15
   streaming recipe `expect.yaml` corpora re-pin (Phase 3).
2. **Required `notice_sink`.** `iter_stream_events`, `stream_export`, and
   `seed_mixer_run` gain a required `notice_sink` parameter (notice-channel
   posture: no default). Every caller migrates (Phase 2).
3. **`elect_after_image_columns` and `_rekey_after_image` are deleted** — subsumed
   by the new output-name resolvers (Phase 3).
4. **`SourceWhereEntry` → `WhereEntry`**, relocated with the spine device to the
   new mode-neutral `exporters/selection_spine.py`; `_needs_population_filter` /
   `_where_predicate_elements` / `_check_where_values_observed` go public there.
   Source call sites update; source behavior byte-identical (Phase 4).
5. **Membership key-uniformity granularity loosens**: the gate ranges over the
   addressed owner set (declared `sub_types`, else full domain) instead of the
   owner kind's full domain. Previously-refused mixed-election configs become
   legal per sub-type (Phase 4).
6. **`StreamEvent.kind` carries the resolved envelope value** (per-stream
   `kind_label` → `kind_labels` → verbatim kind). Byte-identical when no labels
   are declared (Phase 3).

## Success Criteria

- [ ] The design doc's two Configuration examples parse and stream end to end.
- [ ] Wire keys are bare / renamed; identity + `event` names untouched; the
      Debezium value schema and rendered rows agree by construction (one resolver).
- [ ] `kind_labels` / `kind_label` resolve per the precedence table with identity
      fall-through; injectivity and masquerade-refusal gates enforced eagerly.
- [ ] `where` is constant-gated, AND-joined, evaluated over base values; a
      zero-match selection yields the declared-but-empty topic; out-of-domain
      values draw the per-element notice through the caller's sink, in
      deterministic eager-pass order.
- [ ] `only` / `ignore` narrow `u`-event membership only; both absent is
      byte-identical to today (test-guarded).
- [ ] Event-set and presentation-invariance invariants hold (design doc § The
      event-set invariant): projection / rename / labels never change event count,
      order, `seq`, `ts`, message keys, or topic assignment.
- [ ] Full suite + `make check` green; source-mode outputs byte-identical after
      the spine promotion.

## Contracts

Signatures + docstrings only; no implementation. The design doc § Interface
Contracts is authoritative for the five surfaces it defines — restated here in
brief with their decided homes.

### Config models — `src/fabulexa_forge/config/models.py`

Design doc § Config Models, verbatim: `KindStream` gains `where: dict[str,
PredicateValue] | None = None`, `only: list[str] | None = None`, `ignore:
list[str] | None = None`, `rename: dict[str, str] | None = None`, `kind_label:
str | None = None`; `MembershipStream` gains `sub_types: list[str] | None = None`,
`where`, `rename`, `kind_label`; `StreamConfig` gains `kind_labels: dict[str, str]
| None = None`. All `| None = None` fields are absence detection (the shipped
optional-field posture), never value defaults. Validators extend the shipped
`kind_stream_well_formed` / `membership_stream_well_formed` and add
`kind_labels_well_formed` per design doc § Parse-Time, reusing
`_require_rename_map_valid`, `_require_where_map_valid`,
`_require_dict_entries_nonempty`.

### Output-name resolution — new `src/fabulexa_forge/exporters/streaming/presentation.py`

Pure config+sidecar presentation resolution, shared by the engine's after-image
assembly and the driver's Debezium value-schema builders (the single-producer
discipline extended from column order to column naming). Subsumes and replaces
`engine.elect_after_image_columns` (deleted; its `driver.py` callers switch) and
`engine._rekey_after_image` (deleted; assembly keys dicts directly by the resolved
pairs). Absorption arrives via the caller-resolved `identity_key`
(`identity_key == "presentation_id"` is the absorbed case).

```python
def resolve_stream_output_columns(
    sidecar: Sidecar,
    kind: str,
    properties: Sequence[str],
    rename: Mapping[str, str] | None,
    identity_key: str,
) -> list[tuple[str, str]]:
    """Resolve a kind-shaped stream's after-image (fold column, output key)
    pairs — the single naming authority extending resolve_stream_columns.

    Order is resolve_stream_columns order exactly (identity entry, then
    presentation_id when carried and not absorbed, then projected properties
    in sidecar order); the identity entry's output key is `identity_key`,
    payload columns take their bare name or their rename target.

    Args:
        sidecar: The typed sidecar.
        kind: The stream's records kind, bare.
        properties: The stream's declared projection, bare names.
        rename: The stream's rename map, or None.
        identity_key: The identity entry's output key — the stream's elected
            surface's contract column name (record_id / record_index /
            presentation_id), resolved by the caller from the stream's
            election with absorption applied. Defines the reserved-name set
            together with presentation_id, reserved when the kind carries
            one and identity_key is not presentation_id (the unabsorbed
            case).

    Returns:
        Ordered (fold column name, output key) pairs — the one list the
        after-image keying, the JSONL renderer, and the Debezium value
        schema all consume.

    Raises:
        StreamRenameUnresolvable: A rename key names no selected property.
        StreamOutputNameCollision: Two output keys collide, or an output key
            collides with a reserved identity name.
    """


def resolve_membership_output_columns(
    sidecar: Sidecar,
    membership: MembershipRef,
    fields: Sequence[str],
    rename: Mapping[str, str] | None,
    owner_identity_key: str,
) -> list[tuple[str, str]]:
    """The membership analog of resolve_stream_output_columns, extending
    resolve_membership_columns. Order is resolve_membership_columns order
    exactly: owner identity entry, then selected element fields in
    element-schema declaration order (never the config `fields` list's
    order) — a scalar field one pair, a reference field its `<f>_kind` /
    `<f>_id` pair renamed in place.

    Args:
        sidecar: The typed sidecar.
        membership: The stream's membership-table address.
        fields: The stream's declared field projection, bare names.
        rename: The stream's rename map, or None.
        owner_identity_key: The owner identity entry's output key — the
            owner's elected surface's contract column name, resolved by the
            caller. With the membership `event` name, defines the reserved
            set.

    Returns:
        Ordered (fold column name, output key) pairs.

    Raises:
        StreamRenameUnresolvable: A rename key names no selected field.
        StreamOutputNameCollision: Two output keys collide, or an output key
            collides with the owner identity entry or the reserved
            membership `event` name.
    """


def resolve_stream_kind_vocabulary(
    config: StreamConfig,
    sidecar: Sidecar,
) -> Mapping[str, str]:
    """Validate the run's kind vocabulary — the config-level kind_labels
    map plus every per-stream kind_label — and return the declared value
    mapping.

    Args:
        config: The stream config (kind_labels plus every per-stream
            kind_label).
        sidecar: The typed sidecar (the kind universe the integrity rules
            range over).

    Returns:
        The declared config-level (kind, label) pairs; callers render an
        undeclared kind verbatim (identity fall-through is caller-side —
        the total mapping is the pair of this map and that rule). A
        per-stream kind_label is validated here but never enters the
        mapping: the engine applies it on its own stream's envelope only.

    Raises:
        StreamKindLabelUnknown: A kind_labels key names no sidecar kind.
        StreamKindLabelCollision: A label or a per-stream kind_label equals
            a different kind's rendered name.
    """
```

### Row selection — new `src/fabulexa_forge/exporters/streaming/selection.py`

```python
def resolve_stream_selection(
    emit: Emit,
    stream: KindStream | MembershipStream,
    notice_sink: NoticeSink,
) -> frozenset[str] | None:
    """Compute a stream's satisfying record set (owner set, for a
    membership stream) from its declared selection, or None when the
    stream declares no selection this function owns: a kind stream's
    `sub_types` stay the shipped discriminator-index device (None when it
    declares no `where`); a membership stream's owner `sub_types` and
    `where` resolve together here through the parent-lookup spine (None
    only when it declares neither).

    Compiles the predicate through the shared rendering authority against
    the subject kind's records spine (via the shared selection-spine
    parent lookup for a membership stream); the constant-column gate and
    the plan-time value casts run first. Emits the per-element
    `discriminator-value-unobserved` notice for each `where` element
    outside its column's declared `enum_domains` entry.

    Args:
        emit: The open emit.
        stream: The declared stream.
        notice_sink: The caller-supplied sink the out-of-domain notices
            flow through.

    Returns:
        The record_ids whose events the stream carries — codec-encoded
        strings, the type the engine's shipped str-keyed row-scoping
        device compares — or None when the stream declares no selection
        this function owns (all rows in scope).

    Raises:
        StreamWhereNotConstant: A `where` key names a tracked or
            slice_only property.
        StreamWhereOnDiscriminator: A `where` key names the subject kind's
            discriminator.
        StreamWhereColumnUnresolved: A `where` key resolves to no payload
            property of the subject kind.
        StreamWhereValueUncastable: A value fails its column's
            sidecar-declared cast.
    """
```

Private helper `_resolve_stream_where(sidecar, where, subject_kind, stream_name)
-> tuple[WhereEntry, ...]`: the streaming-local gate walk over the shared
primitives (`cast_predicate_element`, `sidecar.temporal_class`, `WhereEntry`),
mirroring source's `_resolve_where_selection` semantics with the design doc's
`stream '{name}'` messages and the `StreamWhere*` classes. Deliberately not a
parametrized reuse of source's resolver (its `key_form` / label / error
entanglement makes sharing a worse abstraction than ~50 lines).

### Selection spine — new `src/fabulexa_forge/exporters/selection_spine.py` (promotion)

Mode-neutral home (the `exporters/election.py` precedent) for the device both
modes compose; streaming never imports `exporters.source`. Moved verbatim from
`exporters/source/` with these renames, signatures otherwise unchanged:

- `build_selection_spine_sql(sidecar, fork_path, kind, populations, where) -> str | None`
  (from `source/renders.py`; `where` becomes `tuple[WhereEntry, ...]`)
- `SourceWhereEntry` → `WhereEntry` (from `source/plan.py`; fields unchanged)
- `_needs_population_filter` → `needs_population_filter`
- `_where_predicate_elements` → `where_predicate_elements`
- `_check_where_values_observed` → `check_where_values_observed(sidecar, entries,
  subject_kind, notice_sink, message)` — `message: Callable[[str, str, bool],
  str]` renders one notice's text from `(key, element, wholly_unobserved)`;
  wording is the only per-mode delta, so it is the caller's callable (source
  passes its shipped wording, streaming the design doc's two-case stream
  wording).

Source call sites update: `source/renders.py`, `source/events.py`,
`source/plan.py`, `tests/exporters/source/test_events_render.py`.

### Engine + driver + mixer deltas

```python
def iter_stream_events(
    emit: Emit,
    config: StreamConfig,
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
) -> Iterator[StreamEvent]:
    """The shipped engine entry point, gaining the required caller-supplied
    sink (the notice-channel posture: required, no default — a caller
    wanting silence passes a discarding sink).

    The eager validation pass emits the per-element
    `discriminator-value-unobserved` notices through it, before any fold
    materializes; the pass otherwise raises as shipped. Every consumer
    threads it: the stream driver paths (the CLI passes the stderr
    renderer) and the mixer's seed_mixer_run.

    Args / Returns: as shipped, plus notice_sink above.

    Raises:
        ExportError: The shipped eager-pass rules plus this sprint's
            vocabulary, naming, selection, and change-scope gates (the
            Stream* subclasses below).
    """
```

- `stream_export(emit, config, fmt, sink, out, anchor, notice_sink, clock=None,
  bootstrap_servers=None)` — required `notice_sink` inserted after `anchor`,
  threaded to every internal `iter_stream_events` call.
- `seed_mixer_run(emit, config, anchor, sidecar, transport, notice_sink)` —
  required trailing parameter, passed through verbatim.
- `cli.py`: the `stream` verb and the `mixer` verb pass `render_notice_stderr`.
- Engine-private `_kind_property_names` → `_kind_audited_property_names`: same
  `sidecar.columns(records__<kind>)` read minus columns where
  `is_non_exempt_slice_only(...)` — the kind's audited set (tracked + constant;
  the exempt discriminator stays, inert). The engine passes `only` /
  audited−`ignore` / audited as the fold's change scope. Byte-identical event
  sets when both fields are absent (architect-confirmed: `build_row_state_events_sql`
  consumes change scope solely through the `history_tracked` partition, and the
  contract pins `slice_only ⇒ history_tracked == false`).
- `_resolve_membership_stream_surface`: the uniformity gate ranges over the
  addressed owner set (declared `sub_types`, else the full domain).

### New error classes — `src/fabulexa_forge/errors.py`

All subclassing `ExportError`, beside the `SourceWhere*` block; raised per the
design doc § Business Rules message table: `StreamRenameUnresolvable`,
`StreamOutputNameCollision`, `StreamKindLabelUnknown`, `StreamKindLabelCollision`,
`StreamWhereNotConstant`, `StreamWhereOnDiscriminator`,
`StreamWhereColumnUnresolved`, `StreamWhereValueUncastable`,
`StreamChangeScopeUnresolvable`.

## Phases

### Phase 1: Config surface

**Delivers:** The new stream-declaration fields and parse-time validators —
additive, suite stays green (design doc § Configuration, § Parse-Time).
**Demo:** Parses both design-doc Configuration examples into typed models and
shows the parse-time rejections (empty `rename`, colliding rename targets,
`only`+`ignore` together, duplicate `kind_labels` labels, empty `sub_types`).
**Contracts:** Config models block above.
**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `tests/config/test_stream_config.py` |
| Create | `docs/sprints/streaming-authoring-parity/demos/phase_1_config_surface.py` |

**Tests (extend `tests/config/test_stream_config.py`):**

- Each new field absent → `None` (both stream shapes; `kind_labels` on config).
- `rename` parses; empty map / empty key / empty target / two keys one target
  rejected (both shapes).
- `where` parses with scalar and list `PredicateValue`; empty map / empty key
  rejected; malformed predicate value rejected by the `PredicateValue` type
  (both shapes).
- `only` / `ignore` each parse; both present rejected; empty list / duplicate
  entries rejected.
- `kind_label` parses; empty string rejected (both shapes).
- `MembershipStream.sub_types` parses; empty / duplicate rejected.
- `kind_labels` parses; empty map / empty key / empty value / two keys sharing a
  label rejected.
- Existing stream-config tests still pass unchanged.

### Phase 2: Notice-sink threading

**Delivers:** The required `notice_sink` parameter on `iter_stream_events`,
`stream_export`, and `seed_mixer_run`, threaded by every source caller (CLI passes
the stderr renderer). No streaming notice is emitted yet — the channel lands here
so Phases 3–5 write against the final signatures (design doc § Affected
Subsystems, notice bullet).
**Demo:** Builds a minimal emit, drains `iter_stream_events` with a recording sink
(zero notices today, events unchanged), runs `stream_export` end to end with
`render_notice_stderr`, and seeds the mixer through `seed_mixer_run` with a
discarding sink.
**Contracts:** Engine + driver + mixer deltas above (`notice_sink` rows).
**Steps:** `source → migrate (codemod, 8 files)` — atomic: the required parameter
reddens every caller until all are migrated.

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/driver.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/mixer/scheduler.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Modify | `tests/exporters/streaming/test_election_stream.py` |
| Modify | `tests/exporters/streaming/test_value_election_stream.py` |
| Modify | `tests/exporters/streaming/test_driver.py` |
| Modify | `tests/exporters/streaming/test_routing_engine.py` |
| Modify | `tests/exporters/streaming/test_init.py` |
| Modify | `tests/exporters/streaming/test_mixer.py` |
| Modify | `tests/recipes/test_stream_recipes.py` |
| Create | `docs/sprints/streaming-authoring-parity/demos/phase_2_notice_sink.py` |

**Tests:**

- Migration only: every `iter_stream_events` / `stream_export` / `seed_mixer_run`
  call site gains `notice_sink=discard_notice_sink` (import
  `from _support.notices import discard_notice_sink`) — one uniform transform,
  intent preserved, no assertion changes.
- Existing full suite green at phase end.

### Phase 3: Wire naming + kind vocabulary

**Delivers:** Bare-name after-image keys, per-stream `rename` / `kind_label`,
config `kind_labels` with identity fall-through, the two output-name resolvers as
the single naming authority (Debezium schema ↔ rows agreement by construction),
and the naming/vocabulary eager gates (design doc § Output-name resolution, § Kind
vocabulary).
**Demo:** Streams a two-kind emit as JSONL showing bare keys, a `rename` to
`session_id`, a per-stream `kind_label`, and a `kind_labels`-mapped member-kind
value; renders the Debezium value schema and shows its field list equals the
rendered after-image keys; shows a rename-collision refusal and a masquerade
(`kind_label` = another kind's name) refusal.
**Contracts:** `presentation.py` block above; `StreamEvent.kind` docstring notes
the resolved envelope value.
**Steps:** `source → migrate (fan-out, 10 files) → author (recipe corpus) →
author (new tests)` — atomic: the wire change reddens every wire assertion until
migrated.

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/streaming/presentation.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/driver.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/types.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `tests/exporters/streaming/test_engine.py` |
| Modify | `tests/exporters/streaming/test_debezium.py` |
| Modify | `tests/exporters/streaming/test_jsonl.py` |
| Modify | `tests/exporters/streaming/test_election_stream.py` |
| Modify | `tests/exporters/streaming/test_value_election_stream.py` |
| Modify | `tests/exporters/streaming/test_routing_engine.py` |
| Modify | `tests/exporters/streaming/test_routing.py` |
| Modify | `tests/exporters/streaming/test_mixer.py` |
| Modify | `tests/exporters/streaming/test_driver.py` |
| Modify | `tests/exporters/streaming/_election_fixtures.py` |
| Modify | `examples/recipes/streaming/state-changes/expect.yaml` |
| Modify | `examples/recipes/streaming/subtype-select/expect.yaml` |
| Modify | `examples/recipes/streaming/custom-stream-name/expect.yaml` |
| Modify | `examples/recipes/streaming/combined-stream/expect.yaml` |
| Modify | `examples/recipes/streaming/multi-kind-routing/expect.yaml` |
| Modify | `examples/recipes/streaming/multi-sub-type-streams/expect.yaml` |
| Modify | `examples/recipes/streaming/membership-events/expect.yaml` |
| Modify | `examples/recipes/streaming/multi-membership-streams/expect.yaml` |
| Modify | `examples/recipes/streaming/clock-realtime/expect.yaml` |
| Modify | `examples/recipes/streaming/rebase-ts/expect.yaml` |
| Modify | `examples/recipes/streaming/debezium-state-changes/expect.yaml` |
| Modify | `examples/recipes/streaming/debezium-membership-events/expect.yaml` |
| Modify | `examples/recipes/streaming/debezium-table-identity/expect.yaml` |
| Create | `tests/exporters/streaming/test_presentation.py` |
| Create | `docs/sprints/streaming-authoring-parity/demos/phase_3_wire_naming.py` |

(`identity-tombstone` and `membership-identity-only` expectations carry no
payload keys and do not re-pin. `test_init.py`'s `prop__` occurrences are fixture
column definitions, untouched.)

**Tests (new, `tests/exporters/streaming/test_presentation.py`):**

- Kind resolver: order = identity, `presentation_id` (when carried, unabsorbed),
  properties in sidecar order; bare defaults; rename targets applied; absorbed
  case (`identity_key == "presentation_id"`) drops the standalone entry.
- Membership resolver: owner identity first, element-schema declaration order,
  scalar one pair, reference `<f>_kind` / `<f>_id` renamed in place as a pair.
- Refusals: rename key naming no selected property/field; two rename targets
  colliding; target vs unrenamed bare default; renamed pair member vs anything;
  output key equal to the identity entry's contract name / unabsorbed
  `presentation_id` / membership `event`.
- Vocabulary: precedence (`kind_label` > `kind_labels` > verbatim); member-kind
  value mapping with identity fall-through (unmapped value verbatim, NULL stays
  NULL); byte-identical passthrough with no labels declared; `kind_labels` key
  naming no sidecar kind refused; label equal to a different kind's rendered name
  refused (config-level and per-stream variants); two streams sharing one
  `kind_label` legal.
- Presentation invariance: for a fixed declaration, adding `rename` +
  `kind_labels` + `kind_label` changes only payload key strings and kind /
  member-kind value strings — event count, order, `seq`, `ts`, `key_value`, and
  topic are byte-identical.
- Debezium: value-schema field list equals the resolver's output keys (after the
  leading membership `event`); `route_table` / `source.table` / schema names
  untouched by `kind_labels`.
- Migrated wire assertions: after-image keys bare in engine / jsonl / debezium /
  election / mixer / driver suites; identity and election contract names
  unchanged.

### Phase 4: Row selection

**Delivers:** `where` on both stream shapes and owner `sub_types` on
`MembershipStream`, over the promoted mode-neutral selection spine; the
out-of-domain value notices; the addressed-owner-set uniformity granularity
(design doc § Row selection; § Affected Subsystems, selection-spine and
key-election bullets).
**Demo:** Streams a sub-typed emit with `where: {region: emea}` showing the
narrowed feed (`c`/`d` included, out-of-set rows absent, `seq` dense over
survivors), a zero-match `where` yielding the declared-but-empty topic at exit 0,
an out-of-domain value printing the two-case notice via the stderr renderer, and
a membership stream scoped by owner `sub_types` + `where` together.
**Contracts:** `selection.py` and `selection_spine.py` blocks above; uniformity
granularity row of the engine deltas.
**Steps:** `source → author (1 file)` — the source reshape (two modes + engine)
and the enumerative selection suite each need the same deep predicate/spine
surface in a fresh window.

**Files:**

| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/selection_spine.py` |
| Create | `src/fabulexa_forge/exporters/streaming/selection.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `tests/exporters/source/test_events_render.py` |
| Create | `tests/exporters/streaming/test_selection.py` |
| Create | `docs/sprints/streaming-authoring-parity/demos/phase_4_row_selection.py` |

**Tests (new, `tests/exporters/streaming/test_selection.py`):**

- Gate matrix (design doc § Row selection table): constant-class key accepted;
  tracked refused; `slice_only` refused; discriminator refused pointing at
  `sub_types`; structural column / element field / unknown name unresolvable;
  uncastable value refused before any fold — each with the `stream '{name}'`
  message lead.
- Kind stream `where`: non-satisfying record's `c`/`u`/`d` all excluded; dropped
  rows consume no `seq`; `where` AND-composes with `sub_types`; predicated
  property need not be projected; reference-valued constant property compared
  over base ids; NULL never satisfies; overlapping streams select independently.
- Membership stream: owner `sub_types` + `where` resolve together through the
  spine (either alone, both AND-composed); non-satisfying owner's `join`/`leave`
  excluded; owner property shadowing an element field resolves to the owner.
- Zero-match selection: topic present and empty (file sink), `events_per_topic
  == 0`.
- Out-of-domain values: per-element notice (never an error), two-case wording,
  deterministic order (streams → keys → elements), emitted before any fold
  through the caller's sink.
- Uniformity granularity: a mixed-election owner kind refused whole-domain but
  legal split per sub-type across two membership streams; gates range over the
  addressed set while `where` never narrows it.
- Source regression: existing source suites green unchanged (promotion is
  relocation only; `test_events_render.py` import fix in the source step).

### Phase 5: Change scope + init trailing comment

**Delivers:** `only` / `ignore` narrowing of the row-state-events change scope,
the audited-set default, the change-scope gates, and the `init --mode streaming`
never-proposed trailing comment naming the new author-intent fields (design doc
§ Change scope, § `init`).
**Demo:** Streams one emit three ways — no scope fields (byte-identical to a
pre-narrowing capture), `only` on one property (other properties' changes fire no
`u`; their as-of values still ride surviving after-images), `ignore` covering
every tracked property (lifecycle-only `c`/`d` feed).
**Contracts:** `_kind_audited_property_names` row of the engine deltas;
`StreamChangeScopeUnresolvable`.
**Steps:** none (single implementer)

**Files:**

| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/init.py` |
| Modify | `tests/exporters/streaming/test_init.py` |
| Create | `tests/exporters/streaming/test_change_scope.py` |
| Create | `docs/sprints/streaming-authoring-parity/demos/phase_5_change_scope.py` |

**Tests (new, `tests/exporters/streaming/test_change_scope.py`):**

- Both fields absent → event stream byte-identical to the full-property-set
  invocation (the design's byte-identical claim, guarded).
- `only`: scoped property's change fires `u`; out-of-scope-only instant produces
  no event and consumes no `seq`; in-scope + out-of-scope coinciding at one
  instant → one `u`.
- Projected-but-not-scoped property: no `u` from its changes, value rides
  surviving after-images; scoped-but-not-projected: `u` fires, after-image
  omits it.
- Constant-class name in scope: legal, inert.
- `ignore` covering every tracked property: lifecycle-only `c`/`d` feed.
- `c` / `d` never affected.
- `StreamChangeScopeUnresolvable`: an entry with no `prop__` column, naming the
  field; a non-exempt `slice_only` entry refused (`StreamPropertySliceOnly`
  extended message shape).
- `test_init.py`: the trailing comment names the never-proposed authoring fields
  (`rename` / `kind_label` / `kind_labels` / `where` / `only` / `ignore` /
  membership `sub_types`) alongside the delivery blocks; proposal output
  otherwise unchanged and parse-clean.

## What Doesn't Change

Design doc § What Doesn't Change is binding; the load-bearing subset for
implementers:

- The content × format × sink model, pacing, mixer scheduling/control semantics,
  Kafka sink — untouched (the one mixer change is the `seed_mixer_run`
  pass-through).
- `render_jsonl_object`, `render_debezium_message`, `encode_pinned`,
  `build_debezium_value_schema` signatures and byte transparency — naming and
  labels are resolved before any renderer runs.
- The derivations layer: no new resident; `build_row_state_events_sql`'s
  two-scope contract and `resolve_stream_columns` / `resolve_membership_columns`
  (fold column names) are consumed differently, not changed.
- The canonical merge key, `seq`, `ts`, anchor rules, message-key election
  machinery (`resolve_election`, gates, `build_elected_identity_index`,
  reference-surface translation) — only the membership uniformity *granularity*
  moves.
- `route_table` / `table_identity` masquerade — `kind_labels` never reaches it.
- The `render:` map — still numeric-only, still keyed by bare source identity,
  unaffected by `rename`.
- Slice-only policy posture (refuse-only on streaming) — new surfaces refused,
  not tolerated.
- Overlapping streams stay legal, no disjointness gate.
- `properties` / `fields`: required-no-default, bare, projection-only.
- Source-mode behavior: the spine promotion is relocation; source output stays
  byte-identical.
- `init` proposals: none of the new fields proposed — only the trailing comment
  grows.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | New stream-declaration fields + parse-time validators (P1) |
| `src/fabulexa_forge/errors.py` | Nine `Stream*` error classes (P3–P5) |
| `src/fabulexa_forge/exporters/streaming/engine.py` | `notice_sink` (P2); resolver-keyed assembly, envelope/member-kind labeling, naming+vocabulary gates, delete `elect_after_image_columns` / `_rekey_after_image` (P3); selection drop + membership scoping + uniformity granularity (P4); audited-set change scope (P5) |
| `src/fabulexa_forge/exporters/streaming/driver.py` | `stream_export` gains `notice_sink` (P2); value-schema builders read the resolvers (P3) |
| `src/fabulexa_forge/exporters/streaming/types.py` | `StreamEvent.kind` docstring: resolved envelope value (P3) |
| `src/fabulexa_forge/exporters/streaming/presentation.py` | New — the two output-name resolvers + kind vocabulary (P3) |
| `src/fabulexa_forge/exporters/streaming/selection.py` | New — `resolve_stream_selection` + `_resolve_stream_where` (P4) |
| `src/fabulexa_forge/exporters/selection_spine.py` | New — promoted spine device (`WhereEntry`, `build_selection_spine_sql`, …) (P4) |
| `src/fabulexa_forge/exporters/source/renders.py` | Import from `selection_spine`; moved code deleted (P4) |
| `src/fabulexa_forge/exporters/source/events.py` | Import update (P4) |
| `src/fabulexa_forge/exporters/source/plan.py` | Import updates; `SourceWhereEntry` / helpers relocated (P4) |
| `src/fabulexa_forge/exporters/streaming/mixer/scheduler.py` | `seed_mixer_run` gains `notice_sink` (P2) |
| `src/fabulexa_forge/exporters/streaming/init.py` | Trailing never-proposed comment extended (P5) |
| `src/fabulexa_forge/cli.py` | Stream + mixer verbs pass `render_notice_stderr` (P2) |
| `tests/config/test_stream_config.py` | New parse-time cases (P1) |
| `tests/exporters/streaming/test_engine.py` | Sink migration (P2); bare-key migration (P3) |
| `tests/exporters/streaming/test_driver.py` | Sink migration (P2); bare-key migration (P3) |
| `tests/exporters/streaming/test_election_stream.py` | Sink migration (P2); bare-key migration (P3) |
| `tests/exporters/streaming/test_value_election_stream.py` | Sink migration (P2); bare-key migration (P3) |
| `tests/exporters/streaming/test_routing_engine.py` | Sink migration (P2); bare-key migration (P3) |
| `tests/exporters/streaming/test_routing.py` | Bare-key migration (P3) |
| `tests/exporters/streaming/test_jsonl.py` | Bare-key migration (P3) |
| `tests/exporters/streaming/test_debezium.py` | Bare-key migration (P3) |
| `tests/exporters/streaming/test_mixer.py` | Sink migration (P2); bare-key migration (P3) |
| `tests/exporters/streaming/test_init.py` | Sink migration (P2); trailing-comment assertion (P5) |
| `tests/exporters/streaming/_election_fixtures.py` | Bare-key migration (P3) |
| `tests/recipes/test_stream_recipes.py` | Sink migration (P2) |
| `examples/recipes/streaming/*/expect.yaml` (13 files) | Re-pinned to bare-name wire keys (P3) |
| `tests/exporters/source/test_events_render.py` | Import update for the promoted spine (P4) |
| `tests/exporters/streaming/test_presentation.py` | New — naming + vocabulary suite (P3) |
| `tests/exporters/streaming/test_selection.py` | New — selection suite (P4) |
| `tests/exporters/streaming/test_change_scope.py` | New — change-scope suite (P5) |
| `docs/sprints/streaming-authoring-parity/demos/phase_[1-5]_*.py` | One demo per phase |
