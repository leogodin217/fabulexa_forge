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


class ExportError(ExporterError):
    """Engine: the config is well-formed but does not fit the emit — the
    multi-branch guard or any business rule fails."""


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
    fails a C1-C12 check (the corrupter refuses a non-conformant input), a
    schema_drift retype that cannot cast or names an unrecognized DuckDB type,
    or an out_dir that already holds an emit. The message names the operation
    index and the offending table / column / rule -- or, for a non-conformant
    source, the failing check ids."""


class SourceRecordRolesRequired(ExportError):
    """A `mode: source` export was run against an emit whose sidecar carries no
    `record_roles` registry. Classification of untracked kinds requires it; there
    is no inference fallback — an emit predating the registry is refused."""


class SourceHistoryTrackedRequired(ExportError):
    """A `mode: source` export was run against an emit whose sidecar carries no
    per-column `history_tracked` flags. The change-log/reference/transaction
    trichotomy is flag-authoritative; there is no history-table inference
    fallback — an emit predating the flag is refused."""


class SourceRoleUnknown(ExportError):
    """An untracked exported kind — or a declared sub-type of an untracked
    object-registry kind — has no resolvable entry in `record_roles`. A tracked
    kind needs no role (tracked-ness dominates); this error names the kind (and
    sub-type, when applicable)."""


class SourceSubtypesUndeclared(ExportError):
    """An untracked kind's `record_roles` entry is object-valued (role varies by
    sub-type) but the sidecar declares no `<kind>_type` enum domain to enumerate
    its split units from."""


class SourceExcludeUnresolved(ExportError):
    """A `source.exclude.kinds` or `source.exclude.tables` entry matches nothing
    in the open emit's sidecar."""


class SourceRenameUnresolved(ExportError):
    """A `source.rename` entry's `table` (and `sub_type`, when the kind splits)
    does not resolve to an exported unit, or one of its `columns` keys does not
    name a source column of that unit."""


class SourceRenameSliceOnly(ExportError):
    """A `source.rename` entry's `columns` key names a policy-omitted
    `temporal_class: slice_only` column — the column carries no value to
    deliver under this rename, so the rename is unsatisfiable rather than
    silently ignored."""


class SourceNameCollision(ExportError):
    """Two resolved output tables share a name, or two columns of one resolved
    output table share a name, after presentation defaults and `source.rename`
    are applied. Never silently suffixed or dropped — the author resolves it via
    `source.rename`."""


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
