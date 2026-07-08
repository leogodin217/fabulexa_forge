# Streaming Routing

The two-layer routing surface that sits between the streaming exporter's
`seq`-stamped event stream and its sinks. It partitions the already-totally-ordered
event stream into named **topics**: a content-specific **routing-key derivation**
(Layer A) feeds a content-agnostic **routing policy** (Layer B) the author configures
in YAML. Routing is a pure post-merge partition — a function of each event's immutable
routing identity only — so it touches neither `seq`, the canonical total order, nor any
payload value. It generalises a destination from a bare `kind` to a configurable topic,
so one kind's sub-types (the object-role kind `actor` → `customer`, `vip_customer`,
`staff`) split into the per-source-table topics a real CDC stream carries, and several
sub-types regroup into one topic. Layer A has one form per streaming content type — a
`state-changes` form keyed on `kind` / `<kind>_type`, and a `membership-events` form keyed
on the `(owner_kind, property)` membership-table identity; Layer B is shared by both. The
streaming exporter itself is documented in [`streaming.md`](streaming.md); this doc owns
the routing contract it composes.

**Source:**
[`exporters/streaming/routing.py`](../../src/fabulexa_export/exporters/streaming/routing.py)
(Layer A — `route_attributes` for state-changes, `membership_route_attributes` for
membership-events — plus the Layer B functions),
[`config/models.py`](../../src/fabulexa_export/config/models.py) (`RoutingConfig`,
`StreamKindSelection.types`), the routing business rules in
[`engine.py`](../../src/fabulexa_export/exporters/streaming/engine.py)
(`_validate_routing_rules`) and
[`driver.py`](../../src/fabulexa_export/exporters/streaming/driver.py).
Tests:
[`tests/exporters/streaming/test_routing.py`](../../tests/exporters/streaming/test_routing.py),
[`tests/exporters/streaming/test_routing_engine.py`](../../tests/exporters/streaming/test_routing_engine.py),
[`tests/config/test_routing_config.py`](../../tests/config/test_routing_config.py).
Recipes: [`examples/recipes/streaming/`](../../examples/recipes/streaming/)
(`routing-topic-template`, `routing-subtype-topics`, `routing-subtype-select`,
`routing-groups`, `multi-kind-routing`, `membership-routing`).

## Boundary

- **Input.** Each event's immutable route identity plus the resolved `RoutingConfig`
  policy. For state-changes the identity is the `kind`, and, when the kind is sub-typed, its
  `prop__<kind>_type` discriminator — read from the **record spine**
  (`records__<kind>.prop__<kind>_type`) via the reader's sidecar, independent of which
  `properties` the author carries in the after-image. For membership-events the identity is
  the `(owner_kind, property)` membership-table pair, a per-selection constant that needs no
  per-row read.
- **Output.** A `topic` (Layer B) and a `route_table` (Layer A) stamped on each
  `StreamEvent`, plus the run's full topic set (`enumerate_topics`) the sinks
  materialize. Routing produces names, never payload: it never reads or rewrites `seq`,
  `op`, `ts`, the message key, or the after-image.
- **Runs after the merge.** Routing is downstream of the cross-source k-way merge and
  `seq` stamping. Sub-type *selection* is the one exception that runs before the merge
  (it drops rows, see § Sub-type selection), so `seq` numbers only emitted events.
- **Layer separation is the contract.** Layer A is the sole interpreter of the route
  identity — `kind` / `<kind>_type` for state-changes, `(owner_kind, property)` for
  membership-events — and owns the `route_table` rule. Layer B treats the route attributes
  as opaque template variables and never references `kind` / `sub_type` / `owner_kind` by
  name, so each content type's Layer A produces its own attribute set and the same Layer B
  routes it.

## Semantics

### The routing pipeline (per event, after the merge)

| Step | Owner | Produces |
|------|-------|----------|
| 1. Derive attributes | Layer A (`route_attributes` / `membership_route_attributes`) | state-changes: `{kind, route_table}`, plus `{sub_type}` when the kind is sub-typed. membership-events: `{owner_kind, property, route_table}` |
| 2. Render base topic | Layer B (`resolve_topic`) | `topic_template` applied to the attribute mapping |
| 3. Apply grouping | Layer B (`resolve_topic`) | if the rendered name is a `groups` member, remap to that entry's target topic; else keep it |
| 4. Stamp the event | engine | `StreamEvent.topic` (step 3) and `StreamEvent.route_table` (step 1 leaf) |

The per-event `sub_type` comes from one `resolve_subtype_index` map
(`record_id → sub_type`) built once per sub-typed kind before the merge. While stamping
each event the engine looks up the event's `record_id` in that kind's index, then calls
`route_attributes`. Non-sub-typed kinds skip the index and pass `sub_type=None`. The
field set and signatures are the contract of
[`routing.py`](../../src/fabulexa_export/exporters/streaming/routing.py).

### Layer A — `route_table` (the logical source table)

`route_table` is the *logical source table* an event's record belongs to: the table a
real CDC stream would carry it on.

| Condition | `route_table` | `sub_type` attribute |
|-----------|---------------|----------------------|
| Kind is sub-typed — `Sidecar.subtype_values(kind)` non-empty | the record's `prop__<kind>_type` value (read from spine) | present (same value) |
| Kind is not sub-typed — `subtype_values(kind)` empty | the bare kind name | absent |
| `enum_domains` absent, or no `<kind>_type` entry for the kind | the bare kind name — a special case of not-sub-typed (`subtype_values` returns `()`) | absent |

Sub-typed-ness is the `<kind>_type` discriminator domain, read through
[`Sidecar.subtype_values`](reader.md#the-discriminator-oracle-answers-sub-typed-ness):
a kind splits per sub-type iff that domain is non-empty. A kind's warehouse role
(`record_roles`) plays no part — `entity` carries a bare `"dimension"` role yet splits
when it declares an `entity_type` domain. `route_attributes` itself is decoupled from
the reader: the caller resolves the verdict from `subtype_values` and passes a plain
`is_subtyped` flag, so the routing leaf carries no `record_roles` or
discriminator-source dependency.

The discriminator is read from the record spine independent of the selected
`properties` — routing works whether or not the author carries the discriminator in the
after-image. Because the `<kind>_type` discriminator is contract-immutable, every event
of a record carries identical attributes, so a record never migrates topics across its
`c`/`u`/`d` events — which is what keeps `record_id`-keyed log compaction coherent
within a topic.

### Layer A for membership-events

For `content: membership-events`, `membership_route_attributes(owner_kind, property_name)`
derives one event's route attributes from its source table:

| Attribute | Value |
|-----------|-------|
| `owner_kind` | the kind that owns the collection-struct property |
| `property` | the collection-struct property name |
| `route_table` | `<owner_kind>__<property>` — the membership relation's logical table |

`route_table` is the leaf logical source table (the membership analog of the state-changes
leaf), so the default policy (`topic_template = "{route_table}"`) yields one topic per
membership table. There is **no `sub_type`** attribute: membership tables are not
`<kind>_type`-discriminated. The attribute set is a per-selection constant — every event of
one selected table carries identical attributes — so a membership stream's events never
migrate topics, keeping `record_id`-keyed (owner-keyed) coherence within a topic, exactly as
the immutable discriminator does for state-changes.

`member_kind` is deliberately **not** a route attribute. The member kind is a per-row column
(`member__<f>__kind`) — one table may reference several member kinds, and may carry more than
one reference field — so there is no single stable per-relation member kind to route on, and
routing on a per-row value could let one interval's `join` and `leave` diverge. The stable,
unambiguous relation identity is `(owner_kind, property)`; member-kind routing is a future
extension. A `topic_template` override or a `groups` merge may route two membership tables
with different `fields` onto one topic; their `after` maps then carry different key sets on
that topic and `{record_id}` partition, which for JSONL is faithful, not a defect — each line
self-describes, so heterogeneous after-image shapes coexist on one topic with no per-topic
shape check. (The state-changes Debezium analog `StreamTopicSchemaUnambiguous` exists only
because the Debezium schema envelope is per-topic; JSONL has no such envelope, and Debezium
over membership-events is refused up front — see [`streaming.md`](streaming.md) § Validation
Rules.)

### Layer B — topic rendering and grouping

Layer B renders each event's attribute mapping into a topic name through
`topic_template`, then applies a `groups` map for many-to-one regrouping.

| Condition | Result |
|-----------|--------|
| `routing` block omitted entirely | default policy: `topic_template = "{route_table}"`, no groups, `table_identity = source_table` |
| `topic_template = "{route_table}"` (default) | one topic per leaf — sub-type for sub-typed kinds, kind otherwise |
| `topic_template = "{kind}"` | one topic per kind; all sub-types of a kind merge, no `groups` needed |
| `topic_template = "cdc.{route_table}"` | leaf topics under a literal prefix |
| `topic_template = "{kind}.{sub_type}"` | qualified topics; valid only if every selected kind is sub-typed (else a validation error) |
| Two distinct route attributes render to the same topic name | they merge into one topic — a deterministic, intentional union (e.g. `{kind}` collapsing sub-types) |
| Rendered name is a `groups` member | the event is remapped to that entry's target topic |

Grouping is **many-to-one**: a `groups` entry maps a target topic to the list of
rendered names whose events it absorbs. A rendered name appears in at most one group.
The target topic name may itself coincide with a rendered name (a further merge).
Rendering is `str.format(**attributes)`; the template grammar (bare placeholders, escaped
literal braces) is fixed by the `groups_well_formed` parse-time validator (§ Validation
Rules), so the only render-time failure is a missing attribute, which the business pass
catches first.

### Sub-type selection (`types`)

`StreamKindSelection.types` scopes a sub-typed kind to a subset of its declared
sub-types.

| Condition | Result |
|-----------|--------|
| `types` omitted on a sub-typed kind | all of the kind's declared sub-types stream |
| `types` lists a subset | only rows whose discriminator is in the subset stream; other sub-types are dropped (a faithful selection, not a fabrication) |
| `types` non-empty on a kind with empty `subtype_values` (no `<kind>_type` domain) | validation error — the kind is not sub-typed |
| `types` names a value outside the kind's declared sub-types (`subtype_values`) | validation error (no invented sub-types — Principle #7) |

The filter is applied **post-fold, via the `resolve_subtype_index` map** (the same
discriminator source Layer A uses), not in the fold SQL: after the per-kind rows are
materialised, a record whose `prop__<kind>_type` is not in the selected set is dropped
before that kind's rows enter the cross-kind merge. Because the drop precedes the merge —
and the merge is what stamps `seq` — `seq` numbers only emitted events; dropping a
sub-type's rows removes its events entirely and does not perturb the `seq` of the events
that remain. Selection is by inclusion only, consistent with `kinds` and `properties`;
there is no sub-type *exclusion* form.

### Declared-but-empty topics

The topic set for a run is the **union** of (a) the topic each *selected* route
attribute renders/regroups to and (b) every `groups` target topic name
(`enumerate_topics`). Each topic in the set gets a stream even when it carries zero
events: the `file` sink writes an empty `<topic>.jsonl`, and
`StreamOutcome.events_per_topic` records `0` for it. The `stdout` sink writes no bytes
for an empty stream but still reports the zero counts. This is the streaming exporter's
selected-set zero-still-emits guarantee at topic granularity (see [`streaming.md`](streaming.md)
§ Routing and empty streams).

Topic enumeration is deterministic: kinds in selection order, sub-types in
`enum_domains` `<kind>_type` **declaration order** (the order `subtype_values`
returns), then group targets in config order. De-duplication **keeps the first
occurrence**: a group target that coincides with a name already rendered by a selected
route keeps that earlier (rendered) position and does not reappear at group-config
order.

### `table_identity` and the Debezium masquerade

`table_identity` governs what the Debezium `source.table` (and the value-schema name
`<source.name>.<table>.Value`) reports. It is a *realism* knob — the question is what a
real Debezium connector would emit, not what is faithful to the bundle. It is read only
by the `debezium` format; `jsonl` ignores it and carries the bundle `kind` directly —
JSONL is the transparent format, Debezium is the masquerade.

| `table_identity` | `source.table` reports |
|------------------|------------------------|
| `source_table` (default) | the event's `route_table` (leaf logical table). Canonical Debezium: the *origin* table is reported even when a routing rule merges tables into one topic. For a non-sub-typed kind this is the kind itself. |
| `topic` | the event's resolved `topic` (the routed destination). |

The bundle `kind` is deliberately **not** an option: `actor` is a modeling artifact, not
a table in the masqueraded database; `source_table` reports `actor` only when `actor` is
genuinely non-sub-typed.

The masquerade covers both content types. For `membership-events` the leaf logical table is
the membership `route_table` `<owner_kind>__<property>` (e.g. `queue__waiters`); `source_table`
reports it and `topic` reports the resolved destination, exactly as for a kind. The
value-schema name is `<source.name>.<table>.Value` keyed by the same identity.

A single per-topic Debezium value schema is well-defined only when every event on the
topic comes from one logical source table — one kind (`state-changes`) or one membership
table (`membership-events`). Under `table_identity = topic` a topic that merges more than
one source table has divergent column shapes, so an unambiguous per-topic schema cannot be
built — the `StreamTopicSchemaUnambiguous` rule rejects that combination when
`schemas_enable: true` (for membership, e.g. a `topic_template = "{owner_kind}"` collapsing
several properties of one owner onto one topic). With `schemas_enable: false` schemaless
Debezium emits no per-topic value schema, so there is nothing to make ambiguous and the
merge is allowed. Such merges are otherwise legal — for JSONL, and for
`table_identity = source_table` (each event declares its own source table / schema, keyed by
`route_table` never the topic); the constraint is specific to a single per-topic Debezium
schema.

## Invariants

1. **Routing is a pure post-merge partition.** It is a function of an event's immutable
   route attributes only; it never alters `seq`, the canonical order, timestamps, the
   message key, or after-images.
2. **An aggregate's events all land in one topic** — a record's `c`/`u`/`d` (guaranteed by
   `<kind>_type` discriminator immutability) and one collection's `join`/`leave` (guaranteed
   by the per-selection-constant `(owner_kind, property)` identity) — which keeps the
   `record_id`-keyed stream coherent within a topic.
3. **Determinism over topic assignment.** Same emit + same `routing` config → identical
   topic set and identical per-topic event sequences (an extension of the streaming
   determinism invariant).
4. **Layer B is content-agnostic.** It operates on the attribute mapping and never
   references `kind` / `sub_type` by name; the content type owns the attribute set and
   the `route_table` rule.
5. **The sub-type split is a function of the `<kind>_type` discriminator domain
   alone.** A kind splits per sub-type iff `Sidecar.subtype_values(kind)` is
   non-empty; routing never reads `record_roles`, so a kind's warehouse role never
   affects its topic assignment.

These rely on guarantees the streaming exporter and the base layer already hold: the
`<kind>_type` discriminator is present (non-null, in-domain) for every record of a
sub-typed kind, so a kind the split gate marks sub-typed always has the spine column
`resolve_subtype_index` reads; `enum_domains` is intent not observation, so the
declared sub-type set — and thus the topic set — is stable across `slice_at`; the
cross-source merge yields a total order with `seq` stamped before routing; and
sub-types of one kind share an after-image column set (`resolve_stream_columns`
depends on kind + selected properties, not on sub-type).

## Validation Rules

**Parse-time** (Pydantic, in
[`config/models.py`](../../src/fabulexa_export/config/models.py)):
`RoutingConfig.groups_well_formed` requires a non-empty `topic_template` with balanced
braces and no format-spec or conversion on any placeholder (checked with
`string.Formatter().parse`), every `groups` target/member a non-empty string, and no
member shared by two groups. `StreamKindSelection.types_are_bare` rejects any `types`
value carrying the `prop__` prefix. `table_identity` is constrained to
`{"source_table", "topic"}` by its `Literal`. Template well-formedness guarantees
`resolve_topic`'s `str.format` can fail at render time only on a missing attribute,
never on a malformed template.

**Business rules** run in the engine's eager `_validate_routing_rules` pass (alongside
the existing single-branch, `StreamKindResolvable`, and `StreamPropertyResolvable`
checks), because they need the emit / sidecar. Each raises `ExportError`, caught by the
CLI's `(ReaderError, ExporterError)` funnel (exit 1). The contract of each rule:

| Rule | Checks |
|------|--------|
| `StreamTypesRequireSubtyping` | `kinds[].types` is non-empty only for a kind with a non-empty `Sidecar.subtype_values` (a `<kind>_type` discriminator domain) |
| `StreamTypesDeclared` | every `types` value is in the kind's `subtype_values` declared set (the `<kind>_type` domain) |
| `StreamTemplatePlaceholders` | every `topic_template` placeholder is present in every selected route attribute mapping (e.g. `{sub_type}` is absent for a non-sub-typed kind) |
| `StreamGroupMembersResolve` | every `groups` member equals a base topic some selected route attribute renders (no dangling members) |
| `StreamTopicSchemaUnambiguous` | under `table_identity='topic'` + `--fmt debezium` + `schemas_enable`, no topic receives events from more than one logical source table — one kind (`state-changes`) or one membership table (`membership-events`) (raised from the driver) |

The exact messages are the contract of the raising sites; the tests in
[`test_routing_engine.py`](../../tests/exporters/streaming/test_routing_engine.py) pin
them.

The content-agnostic Layer-B rules (`StreamTemplatePlaceholders`,
`StreamGroupMembersResolve`) apply to the membership route attributes without special-casing
— a `topic_template` referencing `{sub_type}` fails `StreamTemplatePlaceholders` for
membership-events because its attributes carry no `sub_type`. The membership-events content
rules (`MembershipResolvable`, `MembershipFieldResolvable`) are owned by
[`streaming.md`](streaming.md) § Validation Rules. `StreamTopicSchemaUnambiguous` is
Debezium-specific and applies to membership-events the same way: under `table_identity='topic'`
+ `schemas_enable` a topic merging two membership tables (e.g. `topic_template = "{owner_kind}"`
collapsing several properties of one owner) is rejected.

## Rationale

- **Two layers, not one.** Splitting the content-specific key derivation (Layer A) from
  the content-agnostic policy (Layer B) confines each content type's knowledge of its route
  identity to one place: state-changes' `kind` / `<kind>_type` to `route_attributes`,
  membership-events' `(owner_kind, property)` to `membership_route_attributes`. Both supply
  an attribute mapping that Layer B's templating and grouping consume identically — which is
  why Layer B never names an attribute.
- **`route_table` reads from the spine, not the after-image.** Routing must work
  whether or not the author carries the discriminator as a selected property, so the
  leaf is derived from `records__<kind>.prop__<kind>_type` directly. The discriminator's
  contract immutability is what makes per-record routing identity stable and total — and
  keeps a record's whole lifecycle on one topic.
- **`source_table` is the default `table_identity`, and `kind` is not an option.**
  Canonical Debezium reports the *origin* table even when a routing rule merges tables
  into one topic, so `source_table` is the faithful default. The bundle `kind` (`actor`)
  is a Fabulexa modeling artifact with no counterpart in the masqueraded database, so it
  is never reported as a table name; `source_table` surfaces `actor` only when `actor`
  is genuinely non-sub-typed.
- **Selection is by inclusion.** `types` selects sub-types to stream, mirroring `kinds`
  and `properties`; an exclusion form would be a second, redundant way to express the
  same scope.
- **The split keys on the `<kind>_type` discriminator domain, not the role registry.**
  `record_roles` encodes warehouse role (dimension/fact), which is genuinely per-sub-type
  only for `actor`; keying the split on its object-valued-ness would split `actor` alone
  and miss every other discriminator-bearing kind (`entity`, `resource`). The contract
  names `enum_domains[kind]["<kind>_type"]` the authoritative declared key set for
  per-sub-type routing, so the discriminator domain is the oracle and the two axes —
  "warehouse role varies by sub-type" (`record_roles`, actor-only) and "kind is
  sub-typed" (the discriminator) — are kept distinct. Forcing every sub-typed kind's role
  object-valued would fabricate a role distinction that does not exist; declaring
  sub-typed-ness in a new config field would invent author burden and a second source of
  truth that can silently disagree with the discriminator (Principle #7).
- **The declared sub-type set is intent, not observation.** The topic set fans out over
  the declared `<kind>_type` domain, never `SELECT DISTINCT prop__<kind>_type`, so a
  slice that materialises zero rows for a declared sub-type still gets its topic — the
  declared-but-empty-topic guarantee, stable across `slice_at`.

## Boundaries

What routing deliberately does not own:

- **Reordering, re-timing, or reshaping events.** Routing assigns a name. `seq`, the
  canonical total order, the message key (`record_id`), the after-image, and `ts` are
  all fixed upstream and pass through verbatim.
- **Per-sub-type schema projection.** Sub-types of one kind share the kind's after-image
  column set; routing splits the stream by destination, not by row shape. Distinct
  microservice row shapes per sub-type are not modelled.
- **Member-kind routing.** Membership-events route on the `(owner_kind, property)` relation
  identity only. The member kind is a per-row column (`member__<f>__kind`), not a stable
  per-relation attribute, so it is not a route attribute; routing a relation onto member-kind
  topics is not modelled.
- **The Debezium key message and compaction tombstone.** Routing stamps the `topic`
  each event carries, and the kafka sink delivers to that topic (see
  [`streaming.md`](streaming.md) § The Kafka sink). The native Debezium key message and
  the post-delete null-value tombstone are key-channel artifacts the streaming surface
  does not emit; routing owns only the topic name (see [`streaming.md`](streaming.md) §
  Boundaries).

## Related

| Document | Why |
|---|---|
| [`streaming.md`](streaming.md) | The streaming exporter that composes this routing surface — the `content × format × sink` model, the cross-source merge and `seq`, the Debezium format whose `source.table` `table_identity` governs |
| [`reader.md`](reader.md) | The `Emit` / `Sidecar` surface routing reads the record spine and the `Sidecar.subtype_values` discriminator oracle (sub-typed-ness) through |
| [`config-docstrings.md`](config-docstrings.md) | The three-channel docstring convention `RoutingConfig` / `StreamKindSelection` follow |
| [`config/models.py`](../../src/fabulexa_export/config/models.py) | The `RoutingConfig` / `StreamKindSelection` grammar these semantics bind |
| [`../CAPABILITIES.md`](../CAPABILITIES.md) | Feature inventory and status |
| [`README.md`](README.md) | Design index, package layout, staged roadmap |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary |
</content>
</invoke>
