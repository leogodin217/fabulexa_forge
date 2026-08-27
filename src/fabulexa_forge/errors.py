"""Export-pipeline exception hierarchy for fabulexa_forge.

Three types under one base, covering the three phases of the export pipeline:
config parsing, engine query planning, and writer materialization.

These do NOT subclass the reader's ReaderError; the export pipeline and the
reader are separate failure domains. cmd_export catches (ReaderError,
ExporterError) — the full set of handled failures.
"""

from __future__ import annotations


class ExporterError(Exception):
    """Base for export-pipeline errors (config, build, write)."""


class ConfigError(ExporterError):
    """Loader: the config file is missing, is not valid YAML, or fails Pydantic
    validation (unknown / missing field, or a model validator)."""


class ReadmeOverlayInvalid(ConfigError):
    """The `readme_overlay` file is unreadable, is not UTF-8, or violates the
    slot grammar — content before the first H2, a heading matching neither
    slot form, or a duplicate slot key. Names the offending heading or key."""


class ExportError(ExporterError):
    """Engine: the config is well-formed but does not fit the emit — the
    multi-branch guard or any business rule fails."""


class ReadmeOverlayUnknownTable(ExportError):
    """An overlay `table:` slot names a table the compiled plan does not
    produce. Raised post-compile, before any data or artifact is written.
    Names the slot and lists the plan's output tables."""


class ExportRuntimeError(ExporterError):
    """Writers: query materialization or output file write fails."""


class RebaseError(ExporterError):
    """Base class for anchor-resolution failures (a config/usage error class)."""


class RebaseTimezoneUnresolvable(RebaseError):
    """A base_date resolves but no timezone does (from CLI, config, or the sidecar)."""


class RebaseOriginUnresolvable(RebaseError):
    """A timezone override resolves but there is no origin to apply it to."""


class RebaseDateNotNaive(RebaseError):
    """The winning base_date carries tzinfo/offset."""


class RebaseDateUnresolvable(RebaseError):
    """localize(base_date, timezone) is nonexistent (DST gap) or ambiguous (fold)."""


class RebaseUnknownTimezone(RebaseError):
    """A supplied IANA zone string is not a known zone."""


class RebaseInvalidRuntimeAnchor(RebaseError):
    """The sidecar start_datetime is not a parseable ISO-8601 tz-aware datetime."""


class InitRequiresRecordRoles(ExporterError):
    """`init` was run on an emit whose sidecar omits `record_roles`.

    The candidate-config proposer reads warehouse role from the `record_roles`
    registry and has no inference fallback. A sanitised emit carrying >= 1
    records kind always carries the registry, so its absence is an error in the
    input, not a degraded mode. Raised before any proposal is built.

    A direct child of `ExporterError` (sibling of `ConfigError`, `ExportError`,
    `ExportRuntimeError`, `RebaseError`, `IncrementalError`); deliberately NOT an
    `ExportError`, since `init` runs no engine and reads no config. Caught by the
    CLI's `(ReaderError, ExporterError)` handler.
    """


class StreamInitNothingToStream(ExporterError):
    """`init --mode streaming` was run against an emit with nothing to propose.

    Raised either when the emit carries no records kind (and therefore no
    membership table — an interval requires an owner record within the
    slice), or when every sidecar-derived stream name is topic-illegal, so no
    proposal survives live. A candidate config that cannot stream is not
    proposed.

    A direct child of `ExporterError` (the `InitRequiresRecordRoles` posture:
    `init` runs no engine and reads no config, so its failure is not an
    `ExportError`). Caught by the CLI's `(ReaderError, ExporterError)` handler.
    """


class IncrementalError(ExporterError):
    """Base for incremental-driver failures (regime, cursor, fingerprint)."""


class IncrementalConfigMissing(IncrementalError):
    """--next requires an `incremental` block in the config."""


class IncrementalAnchorRequired(IncrementalError):
    """A calendar `period` is declared but no EffectiveAnchor resolves."""


class IncrementalPeriodRegimeMismatch(IncrementalError):
    """`sim_period_ns` is declared but an EffectiveAnchor resolves."""


class IncrementalFingerprintMismatch(IncrementalError):
    """Stored drip fingerprint differs from the computed one."""


class IncrementalCursorInvalid(IncrementalError):
    """Cursor state is unreadable or structurally invalid, or absent from a
    non-fresh target — a non-empty DuckDB catalog without _export_meta, or
    CSV non-hidden entries without the cursor file (beyond the CSV window-0
    crash-recovery state)."""


class IncrementalRangeInvalid(IncrementalError):
    """--from/--to fail to parse, localize, or order in the active regime."""


class IncrementalRangeTargetExists(IncrementalError):
    """--from/--to refuses an `out` that already exists; a range never
    appends into or overwrites an existing target."""


class ClockSpeedUnresolvable(ExporterError):
    """A realtime stream run resolves to realtime (a realtime `clock` config, or
    `--speed`/`--idle-cap` given) but no speed is resolvable — e.g. `--idle-cap` over a
    fast/absent config with no `--speed`.

    A direct child of `ExporterError` (sibling of `ConfigError`, `ExportError`,
    `ExportRuntimeError`, `RebaseError`, `IncrementalError`, `InitRequiresRecordRoles`);
    deliberately NOT an `ExportError`, since clock resolution runs no engine and reads
    no config, exactly as `InitRequiresRecordRoles` is placed. Caught by the CLI's
    `(ReaderError, ExporterError)` funnel as exit 1.
    """


class KafkaBootstrapUnresolvable(ExporterError):
    """sink='kafka' but no bootstrap-servers string resolves from --bootstrap-servers,
    the config `kafka` block, or FABEXPORT_KAFKA_BOOTSTRAP. A direct child of
    ExporterError (sibling of ClockSpeedUnresolvable); caught by the CLI funnel."""


class KafkaClientUnavailable(ExporterError):
    """sink='kafka' but confluent-kafka is not importable — the optional `kafka` install
    extra is absent. The message names the fix (install the extra). A direct child of
    ExporterError; caught by the CLI funnel."""


class KafkaDeliveryError(ExportRuntimeError):
    """A Kafka delivery failure: connection, topic creation, produce, or flush failed; a
    delivery callback reported failure; flush left unacked messages; or a pre-existing
    topic has a partition count other than 1. A child of ExportRuntimeError (the writer
    failure domain), since it surfaces at the sink boundary."""


class KafkaConsumeError(ExportRuntimeError):
    """A Kafka consume failure: subscription, metadata, poll, offset read, or close
    failed. A child of ExportRuntimeError; caught by the CLI's (ReaderError,
    ExporterError) funnel as exit 1."""


class MixerExtraUnavailable(ExporterError):
    """`fabulexa-forge mixer` was invoked but FastAPI / the ASGI server is not
    importable — the optional `mixer` install extra is absent. The message names the
    fix (install the extra). A direct child of ExporterError (sibling of
    KafkaClientUnavailable); caught by the CLI's (ReaderError, ExporterError) funnel
    as exit 1."""


class CorruptError(ExporterError):
    """Base for corrupter-engine failures. A direct child of ExporterError (the
    package's CLI-funnel base, sibling of IncrementalError and the Kafka / Mixer
    errors), so `fabulexa-forge corrupt` catches it under the same
    (ReaderError, ExporterError) handler cmd_export uses and exits 1."""


class CorruptValidationError(CorruptError):
    """An emit-dependent check fails: a business rule (table / column existence
    and column eligibility, each resolved against the schema as of the
    operation's position -- the evolved-schema simulation), a source emit that
    fails a C1-C14 check (the corrupter refuses a non-conformant input), a
    schema_drift retype that cannot cast or names an unrecognized DuckDB type,
    or an out_dir that already holds an emit. The message names the operation
    index and the offending table / column / rule -- or, for a non-conformant
    source, the failing check ids."""


class SourceHistoryTrackedRequired(ExportError):
    """A `mode: source` export was run against an emit whose sidecar carries no
    per-column `history_tracked` flags. The event log and the windowed state
    snapshot are flag-authoritative; there is no inference fallback — an emit
    predating the flag is refused."""


class SourceNameCollision(ExportError):
    """Two resolved output tables share a name, or two columns of one resolved
    output table share a name, after defaults and `rename` are applied. Never
    silently suffixed or dropped — the author resolves it via `rename`."""


class SourceKindLabelUnknown(ExportError):
    """A `source.kind_labels` key names no records kind in the sidecar
    (no `records__<kind>` table) — the sidecar-facts-gate-declarations
    posture. Message: `"kind_labels: kind '{kind}' not in this emit"`."""


class SourceKindLabelCollision(ExportError):
    """After labeling, kind -> rendered name is not injective over the
    emit's whole kind universe: a label equals another kind's label or an
    unlabeled kind's own name, so two kinds would be indistinguishable in
    a `<f>_kind` column. Message:
    `"kind_labels: label '{label}' collides with kind '{kind}'"`."""


class SourceItemTypeCollision(ExportError):
    """Two events sources resolve one item-type over two audited item
    spaces (different kinds, or a membership source sharing any source's
    item-type), or a resolved item-type equals the rendered name of
    another kind (any kind, for a membership source) — ranged over the
    emit's whole kind universe. Messages per the design doc § Business
    Rules row."""


class SourceAnchorRequired(ExportError):
    """A `mode: source` export ran with no resolved `EffectiveAnchor`. Source
    renders every structural sim-time column as wallclock and never falls back to
    raw nanosecond integers; the emit declares no `runtime` block, and no
    `rebase`/CLI override supplied one."""


class SourceUnclassifiedColumn(ExportError):
    """
    A records-category column matched no records-column taxonomy role during
    source export planning.

    Raised at plan/validation time, before any output is written. Names the
    table and column. The exporter-side counterpart of C5's recorded failure: a
    contract column family forge does not know is an error, never a silent
    pass-through.
    """


class SourceTableKindUnknown(ExportError):
    """A declared `kind` has no `records__<kind>` table in the sidecar.

    Raised by `resolve_populations`, the shared population-set resolver
    consumed by both `tables` entries and `events` sources. The message is
    prefixed with the declaring unit's label, verbatim — `table '<name>'`
    for a `tables` entry, `events source #<n>` (1-based, declaration order)
    for an `events` source.
    """


class SourceTableSubTypeUnknown(ExportError):
    """A declared `sub_types` entry names a value outside the kind's
    discriminator domain. Owner-prefixed message, per
    `SourceTableKindUnknown`."""


class SourceSubTypesOnFlatKind(ExportError):
    """A declaration gives `sub_types` for a kind with no discriminator
    domain — a flat kind has no populations to address. Owner-prefixed
    message, per `SourceTableKindUnknown`."""


class SourceTableMembershipUnknown(ExportError):
    """A declared `membership` reference resolves to no
    `membership__<K>__<p>` table in the sidecar. Owner-prefixed message,
    per `SourceTableKindUnknown`."""


class SourceColumnUnresolved(ExportError):
    """A `columns` / `rename` / `only` / `ignore` entry names no column or
    property of its source surface — including a non-elected, unrendered
    identity surface name, and, under a windowed invocation,
    `last_mutation_sim_time` (the windowed state render omits it —
    horizon honesty). The message names the election or the omission
    reason, not just the entry."""


class SourceColumnNotAddressable(ExportError):
    """A declaration entry names a mechanism column (`fork_path`,
    `record_index` outside its role as a table's elected identity surface,
    `ref_index__*`), or a `columns` entry names a state table's *elected*
    identity surface — identity is election-governed there, not
    selection-governed."""


class SourceSliceOnlyRead(ExportError):
    """A declaration entry (`columns` / `rename` / `only` / `ignore`) names
    a non-exempt `temporal_class: slice_only` column — the column carries
    no deliverable value under the export-wide slice_only policy, so the
    reference is unsatisfiable rather than silently omitted."""


class SourceEventSourceOverlap(ExportError):
    """Two `events.sources` entries resolve overlapping population sets
    (membership sources distinct by `(kind, property)`) — each population
    may be audited by exactly one source, so the total event order stays
    tie-free and no event is double-logged."""


class SourceWhereColumnUnresolved(ExportError):
    """A `where` key names no payload property of the declaring unit's
    subject kind (the owner kind for a membership unit) — structural
    columns, membership element fields, and unknown columns all land here.
    Message per doc § Business Rules:
    `"{owner}: where key '{key}' not a payload property of kind '{kind}'"`."""


class SourceWhereNotConstant(ExportError):
    """A resolved `where` column's `temporal_class` is not `constant` —
    `tracked` and `slice_only` each carry their own message variant, per
    doc § Business Rules. `where` keys are this rule's to refuse; the
    existing `SourceSliceOnlyRead` population does not extend to them."""


class SourceWhereOnDiscriminator(ExportError):
    """A `where` key names the subject kind's declared discriminator;
    sub-type selection is `sub_types`' axis. Message per doc § Business
    Rules."""


class SourceWhereValueUncastable(ExportError):
    """A `where` element does not cast to its resolved column's
    sidecar-declared DuckDB type — constant-evaluated at plan time, before
    any write; the disjointness gate's typed-value comparison reuses these
    cast results. Message per doc § Business Rules:
    `"{owner}: where value '{element}' for '{key}' does not cast to {type}"`."""


class BaseExcludeUnresolved(ExportError):
    """A `base.exclude.kinds`/`base.exclude.tables` entry matches nothing base emits."""


class BaseRenameUnresolved(ExportError):
    """A `base.rename` entry's `table` is not a surviving `records__<kind>`, or a
    `columns` key does not name a state-at column of that kind."""


class BaseRenameSliceOnly(ExportError):
    """A `base.rename` entry's `columns` key names a column omitted by the
    `slice_only` policy — the rename is unsatisfiable rather than silently ignored."""


class BaseNameCollision(ExportError):
    """Two resolved base output tables share a name, or two columns of one output
    table do, after presentation defaults and `base.rename`. Never suffixed."""


class ElectionKindUnknown(ExportError):
    """A `keys` entry names a kind with no records table in the emit."""


class ElectionSubTypeUnknown(ExportError):
    """A `keys` map addresses a sub-type outside the kind's discriminator
    domain, or addresses a flat kind with a map."""


class ElectionPresentationUndeclared(ExportError):
    """A population elects presentation_id without a presentation_keys
    entry covering it — or a dimensional edge resolves presentation_id
    (inherited or explicit) over a source population set with an
    uncovered population."""


class ElectionMixedIdentity(ExportError):
    """An output table combines populations electing differing surfaces —
    one table, one identity surface; refused at plan time, naming the
    table and the (population, surface) pairs."""


class ElectionUnionUnsafe(ExportError):
    """Elected key spaces admit a value collision — among a uniform
    presentation_id election's populations on one identity column, or
    across a reference edge's admitted target mix."""


class ElectionInheritanceAmbiguous(ExportError):
    """A dimensional FK without an explicit `target_key` targets a dim
    whose source population set carries more than one distinct election —
    nothing coherent to inherit; names the edge and the differing
    elections."""


class ElectionDimKeyDisagrees(ExportError):
    """A dimensional FK's resolved surface (inherited from the destination
    dim's source population's election, with no explicit target_key
    override) is not among the destination dim's declared key columns'
    sources; names the dim, its key sources, and the elected surface."""


class ElectedKeyDuplicate(ExportError):
    """The render-time uniqueness guard: over a composed identity
    relation, restricted to the consuming population set, row count,
    COUNT(DISTINCT record_id), and COUNT(DISTINCT elected value) are not
    all equal, or an elected value is NULL; names the table or edge and
    the surface."""


class TemporalRenderRequiresAnchor(ExportError):
    """An explicitly-elected instant rendering (dimensional `as`, the
    `scd_window` object form, or a source/base `render` entry) has no
    resolved effective anchor. An elected rendering never falls back to raw
    integers — without a declared calendar the offset is uninterpretable."""


class DateParseSourceColumn(ExportError):
    """A `date_parse` source does not carry a declared VARCHAR type behind
    it (the sidecar type for `prop__` columns, or the `history` table's
    `value` column type on the history_interval grain) — a structural,
    virtual, or grain-constant source, or a non-VARCHAR declared column."""


class RenderKeyResolves(ExportError):
    """A source declared-table or base-entry `render` key does not name a
    column in its value form's key domain: the bare shorthand form requires
    an instant-carrying structural column of the table's category (per the
    reader's `structural_instant_columns`); a typed election
    (`date_parse` / `instant` / `decimal` / `json_precision`) requires a
    payload column of the table's kind (`elem__<f>` element columns on a
    junction; the member pair columns are outside the domain) — a typed
    election naming a structural column is refused, so no rendering ever has
    two spellings. The event log's one legal key is `event_sim_time`
    (mode-definitional); any other key is refused the same way. A stream's
    `render` key must likewise resolve to a declared property or
    element-schema field of that stream's own projection (`decimal` /
    `json_precision` only; a membership stream's reference-field pair is
    outside the domain)."""


class ElectionKindConflict(ExportError):
    """Across the declared tables of one `(kind, property)` membership
    (junction tables share the membership grain) that emit a source
    property the event log renders, every table must declare the identical
    render election — a silent emitting table counts as differing, its
    column asserting the default raw rendering. Scoped to properties inside
    some `events` source's audited set; tables differing on a property no
    log renders are legal. Message names the property, the membership, and
    the two disagreeing tables — either the "conflicting elections" shape
    (both elect, differently) or the "declares none" shape (one elects, one
    is silent)."""


class DecimalSourceIsDouble(ExportError):
    """A `decimal` election's source column does not carry a declared DOUBLE
    type — the contract's one floating-point type; integers and VARCHARs
    have no precision to elect."""


class InstantSourceIsBigint(ExportError):
    """An `instant` election's source column does not carry a declared
    BIGINT type — the assertion is checkable only against an integer
    sim-offset column."""


class JsonPrecisionSourceIsVarchar(ExportError):
    """A `json_precision` election's source column does not carry a declared
    VARCHAR type — electing the column asserts it is a JSON object payload."""


class StreamRenameUnresolvable(ExportError):
    """A stream's `rename` key names neither a selected property
    (kind-shaped) or field (membership-shaped) nor a published identity
    surface's contract column name — rename keys are source identities and
    must name a member of the stream's own projection or its published
    identity set. Message, engine-wrapped with the declaring stream's name:
    `"stream '{name}': rename key '{key}' names no selected property"`
    (field-variant for membership); when the key is itself a KeySurface name
    this stream simply does not publish, the message appends the published
    set."""


class StreamOutputNameCollision(ExportError):
    """Two of a stream's resolved after-image output keys collide — two
    rename targets, a target vs an unrenamed bare default, a renamed
    reference pair member vs anything — or an output key equals a reserved
    name on that stream: a published identity surface's resolved output key,
    or the membership `event` column. Message, engine-wrapped with the
    declaring stream's name:
    `"stream '{name}': output name '{key}' collides with '{other}'"`."""


class StreamKindLabelUnknown(ExportError):
    """A `kind_labels` key names no sidecar kind (no `records__<kind>`
    table) — the sidecar-facts-gate-declarations posture. Message:
    `"kind_labels: '{kind}' is not a kind in this emit"`."""


class StreamKindLabelCollision(ExportError):
    """A `kind_labels` label, or a per-stream `kind_label`, equals a
    *different* kind's rendered name (its label, or its verbatim name when
    unlabeled) — the masquerade refusal. Two streams sharing one
    `kind_label` is legal; this is not a cross-stream uniqueness rule.
    Message: `"kind_labels: label '{label}' collides with kind '{kind}'"`
    (config-level) or `"stream '{name}': kind_label '{label}' collides with
    kind '{kind}'"` (per-stream)."""


class StreamWhereNotConstant(ExportError):
    """A `where` key names a resolved payload property of the subject kind
    (the declared kind for a kind stream, the owner kind for a membership
    stream) whose `temporal_class` is not `constant` — a stream replays
    every instant of the tape, so only a value identical at every horizon
    can select rows without making the event set time-dependent. Message:
    `"stream '{name}': where key '{key}' is not a constant-class property of
    kind '{kind}'"`."""


class StreamWhereOnDiscriminator(ExportError):
    """A `where` key names the subject kind's declared discriminator;
    sub-type selection is `sub_types`' axis (owner `sub_types` on a
    membership stream). Message: `"stream '{name}': where key '{key}' is the
    discriminator; use sub_types"`."""


class StreamWhereColumnUnresolved(ExportError):
    """A `where` key names no payload property of the subject kind —
    structural columns, membership element fields, and unknown columns all
    land here. Message: `"stream '{name}': where key '{key}' is not a
    payload property of kind '{kind}'"`."""


class StreamWhereValueUncastable(ExportError):
    """A `where` element does not cast to its resolved column's
    sidecar-declared DuckDB type — constant-evaluated at plan time, before
    any fold materializes. Message: `"stream '{name}': where value '{value}'
    does not cast to {type} for '{key}'"`."""


class StreamChangeScopeUnresolvable(ExportError):
    """An `only` / `ignore` entry names no `prop__` column of the stream's
    kind. Message: `"stream '{name}': {field} entry '{property}' has no
    prop__{property} column on kind '{kind}'"` (`field` is `only` or
    `ignore`)."""
