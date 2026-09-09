# Sprint: supplementary-tables

Design doc: [`docs/architecture/pending/supplementary-tables.md`](../../architecture/pending/supplementary-tables.md)
— the WHY, every semantic table, every error message. This spec carries the
contracts, phases, and test cases; it does not restate the design's prose.
Where a contract below says "§ X", that is a section of the design doc.

## Purpose

An author ships a hand-maintained table with a dimensional warehouse by
declaring it in the export config — a CSV file or inline rows, a typed column
list, prose — and forge carries it verbatim through every dimensional delivery
path: full export, `--next` / `--from/--to` windows, shaped playback, the
README + manifest, and the dataset pack.

```yaml
mode: dimensional
supplements:
  - name: region_code
    file: region_code.csv           # resolved against the config's directory
    columns: {code: VARCHAR, label: VARCHAR}
    description: Sales-region codes used by account management.
```

`fabulexa-forge export emit/ config.yaml out/ --fmt csv` then writes
`out/region_code.csv` beside the declared tables, lists it in the README, and
records the source file and its SHA-256 in the manifest.

## Scope

**Capabilities touched:**
- config-envelope: `SupplementDecl`, `ExportConfig.supplements`, the parse-time validators, the loader step `load_supplements` (not: streaming / source / base envelopes)
- dimensional-exporter: `compile_supplement_specs`, the source-is-output gate over pure naming functions, `export_dimensional` carrying supplements (not: `init` proposals, `references`, per-row timing)
- companion-artifacts: `QuerySpec.supplement` / `TableReport.supplement`, the manifest `tables[].supplement` field, `manifest_format_version` 2 → 3 (not: the README template or ordering contract)
- incremental-export: `snapshot` delivery of supplements under `--next` and ranges, the fingerprint's `supplements` map + presentation exclusions, the windowed source-is-output gate
- playback-seam: supplement tables in a dimensional shape's `tables()` / `window()` / `state()` and the per-ask selection domain; the supplement-only selection opens no horizon
- dataset-distribution: the pack builder loads every packed config through its loader and carries file supplements
- recipes: `examples/recipes/supplement-tables`

**Not included:** per-row availability times and emit-shaped bundle
augmentation (§ Solution, shapes 2 and 3); source-mode seed tables; a
`references` existence check; `StreamConfig`; `init`; the consumer-voice README
rewrite; widening the supplement type vocabulary (INTERVAL / BLOB).

## Breaking Changes

Internal, no shims (CLAUDE.md Principle #9). The base-format contract is untouched.

| What changes | Effect on existing callers |
|---|---|
| `export_dimensional(...)` gains required `supplements: Sequence[ResolvedSupplement]` after `overlay` | Every caller passes it; `()` when the config declares none. The overlay precedent — a loader-produced input outside the config, never defaulted |
| `export_window(...)` / `export_incremental_next(...)` gain required `supplements` after `overlay` | Same |
| `open_shaped_playback(...)` gains required `supplements` after `notice_sink` | Same |
| `compute_fingerprint(...)` gains required `supplements` after `package_version` | Same; the canonical document gains a `supplements` key even when empty (`{}`), so **every existing drip's fingerprint changes** — a `--next` target created before this sprint refuses with `IncrementalFingerprintMismatch`. Acceptable: greenfield, and the package version already trips the fingerprint on every release |
| `TableReport` gains required `supplement: SupplementSource \| None` | Every report-assembly site and every test constructor states it (`None` for a mode table) |
| `QuerySpec` gains `supplement: SupplementSource \| None = None` | Internal runtime type with defaulted siblings (`keys`, `provenance`, …); no caller changes |
| `_MANIFEST_FORMAT_VERSION` 2 → 3; every `tables[]` entry carries `supplement` | Manifest readers see a new always-present key |
| `companion/artifacts.py` `_artifact_paths` becomes public `companion_artifact_paths` | Its in-module callers are renamed; no external caller exists |
| Recipe corpus guard admits `*.csv` beside `config.yaml` / `expect.yaml` | Existing recipe folders are unaffected |

## Success Criteria

- [ ] A `mode: dimensional` config with `supplements` (one `file`, one `rows`) exports under both formats; the supplement tables land after the declared tables in declaration order, typed as declared, rows in file / list order, `NULL` where the cell was empty / `null`.
- [ ] Every refusal in § Validation Rules fires with its stated message before any output is written, and the CLI exits 1.
- [ ] The manifest carries `manifest_format_version: 3` and `tables[].supplement` (`{"file", "sha256"}` for a file supplement, both `null` for inline, `null` for a mode table); the README lists the supplement as any table; an overlay `table:` slot may name it.
- [ ] `--next` delivers every supplement whole (`replace` / whole file) in every emitting window including an empty one; a changed CSV byte, inline row, column, or type mid-drip raises `IncrementalFingerprintMismatch`; a changed `description` / `descriptions`, or a renamed file with identical bytes, does not.
- [ ] A shaped dimensional head lists supplements in `tables()` as `snapshot`, returns them identically at every `state(T)` and in every `window()`, accepts them in a `tables=` selection, and emits no plan notice for a supplement-only selection.
- [ ] `tools/build_dataset_pack.py` loads every packed config through its loader, packs each file supplement at its config-relative path, and refuses a config that does not load, a missing file, or a path escaping the dataset directory.
- [ ] `examples/recipes/supplement-tables` passes the recipe gates.
- [ ] `make test` and pre-commit green at every phase end.

## Contracts

Signatures and docstrings only. Error messages are pinned in the design doc
§ Validation Rules → Business Rules and are not restated here.

### Config models (`src/fabulexa_forge/config/models.py`)

```python
def canonical_supplement_type(type_text: str) -> str:
    """Canonicalize one supplement column's declared type text.

    The one authority on the supplement type vocabulary (§ Supplement
    declaration and data → Declared type text): `BIGINT` / `INTEGER` /
    `SMALLINT` / `TINYINT`, `DOUBLE` / `FLOAT`, `BOOLEAN`, `VARCHAR`,
    `TIMESTAMP`, `DATE`, `TIME`, `TIMESTAMPTZ`, and `DECIMAL(p, s)` under
    `_check_decimal_bounds`. Matched case-insensitively with surrounding
    whitespace ignored; returned in the canonical upper-case spelling
    (`DECIMAL(p, s)` re-rendered as `DECIMAL(p,s)` with the parsed
    integers). Read by the `supplement_types_known` validator, the compile
    (the CAST target), and the fingerprint (the `columns` pair list).

    Args:
        type_text: The author's type text, verbatim.

    Returns:
        The canonical spelling.

    Raises:
        ValueError: The text is not in the vocabulary, or a DECIMAL's
            precision / scale are out of bounds (the bounds helper's
            message).
    """


class SupplementDecl(StrictBaseModel):
    """One author-supplied table carried verbatim into a dimensional export."""

    name: str
    """Output table name — a SQL identifier, unique across the export's
    tables (`supplements_names_unique` on ExportConfig)."""
    columns: dict[str, str]
    """Ordered column name -> type text from the supplement type vocabulary
    (`supplement_types_known`). Non-empty; names are SQL identifiers. Cells
    are cast by the session's VARCHAR cast, whose leniency is the contract:
    a fractional text rounds into an integer column (`1.5` -> 2), a DECIMAL
    rounds to its scale, boolean spellings are DuckDB's."""
    file: str | None = None
    """CSV path, resolved against the config file's directory by the loader;
    the model never touches the filesystem. Exactly one of `file` / `rows`."""
    rows: list[dict[str, str | int | float | None]] | None = None
    """Inline rows in output order; each row's keys equal `columns`' names.
    Every value is a YAML scalar rendered to text at load and cast to the
    column's declared type. Quote any value YAML would not read as a plain
    string: temporal and boolean scalars are refused (an unquoted date,
    time, `true`, or `yes` is resolved by YAML before this model sees it);
    numeric look-alikes (`010`, `1_000`, `12:30:00`) are not detectable and
    ship as the number YAML made of them."""
    description: str | None = None
    """Table prose; rendered and embedded like a declared table's.
    Presentation only — excluded from the incremental fingerprint."""
    descriptions: dict[str, str] | None = None
    """Per-column prose; keys are a subset of `columns`. Presentation only —
    excluded from the incremental fingerprint."""

    @field_validator("rows", mode="before")
    @classmethod
    def supplement_rows_scalars(cls, value: object) -> object:
        """Refuse a `date` / `datetime` / `time` cell ("quote temporal
        values") and a `bool` cell ("quote boolean values"), each naming the
        1-based row and the column, before Pydantic's union check so the
        author sees the rule, not a union-failure text. `bool` is checked
        before `int` (it is an `int` subclass). Any other shape passes
        through to the union.

        Raises:
            ValueError: A temporal or boolean scalar cell.
        """

    @model_validator(mode="after")
    def supplement_name_is_sql_identifier(self) -> Self:
        """`name` matches `_SQL_IDENTIFIER_RE`.

        Raises:
            ValueError: It does not.
        """

    @model_validator(mode="after")
    def supplement_columns_well_formed(self) -> Self:
        """`columns` is non-empty; every name is a SQL identifier; every type
        text is non-blank.

        Raises:
            ValueError: Any of the three fails.
        """

    @model_validator(mode="after")
    def supplement_types_known(self) -> Self:
        """Every `columns` type text canonicalizes under
        `canonical_supplement_type`.

        Raises:
            ValueError: The design's message naming the supplement, the
                column, the offending text, and the vocabulary.
        """

    @model_validator(mode="after")
    def supplement_source_exactly_one(self) -> Self:
        """Exactly one of `file` / `rows` is present; a present `file` is
        non-blank.

        Raises:
            ValueError: Both, neither, or a blank `file`.
        """

    @model_validator(mode="after")
    def supplement_rows_shape(self) -> Self:
        """Every inline row's key set equals `columns`' key set.

        Raises:
            ValueError: A row with a missing or extra key, naming the
                1-based row.
        """

    @model_validator(mode="after")
    def supplement_documentation_well_formed(self) -> Self:
        """A present `description` is non-blank; `descriptions` keys are
        declared columns and values are non-blank.

        Raises:
            ValueError: Any fails.
        """


class ExportConfig(StrictBaseModel):
    """Top-level export configuration block."""

    supplements: list[SupplementDecl] | None = None
    """Author-supplied tables carried verbatim into the warehouse; absent
    means none. Legal only with mode='dimensional'
    (`supplements_require_dimensional`); non-empty when present, names
    unique among themselves and against the declared dimensional tables
    (`supplements_names_unique`)."""
    # every existing field unchanged

    @model_validator(mode="after")
    def supplements_require_dimensional(self) -> Self:
        """A present `supplements` list requires mode='dimensional'.

        Raises:
            ValueError: `supplements` present under mode='source' / 'base'.
        """

    @model_validator(mode="after")
    def supplements_names_unique(self) -> Self:
        """A present `supplements` list is non-empty; no two supplements
        share a `name`; no supplement `name` equals a declared dimensional
        table's `name` (message: "supplement '{name}' collides with declared
        table '{name}'").

        Raises:
            ValueError: Empty list, duplicate name, or collision.
        """
```

### Errors (`src/fabulexa_forge/errors.py`)

```python
class SupplementFileMissing(ConfigError):
    """A supplement's resolved `file` does not exist or is not a regular file."""


class SupplementFileInvalid(ConfigError):
    """A supplement's file is not UTF-8, is not valid CSV under the strict
    dialect, or has a data row whose field count differs from the header's."""


class SupplementHeaderMismatch(ConfigError):
    """A supplement's CSV header differs from the declared column names in
    count, order, or spelling; the message names the first differing position."""


class SupplementValueInvalid(ExportError):
    """A supplement cell does not cast to its declared type; names the table,
    the column, the 1-based data row, and the cell text. Raised at plan
    compile, before any write."""


class SupplementSourceIsOutput(ExportError):
    """A file supplement's resolved source path is a file this invocation
    writes or lies under a directory it removes. Raised before any write."""
```

### Supplement module (`src/fabulexa_forge/exporters/supplements.py`, new)

```python
@dataclass(frozen=True)
class ResolvedSupplement:
    """One supplement with its data resolved by the config's loader.

    `path` is the resolved absolute path for a file supplement (None for
    inline); `sha256` the hex digest of the file bytes (None for inline);
    `rows` the text rows — one tuple per data row in file / list order,
    one `str | None` cell per declared column in declared order (an empty
    CSV field or a YAML `null` is None; every inline scalar is rendered to
    text at load per § Supplement declaration and data). `decl` is the
    declaration verbatim. Built once per invocation; the dimensional entry
    points, the incremental driver, the shaped playback head, and the pack
    builder all consume this — the file is never read again.
    """

    decl: SupplementDecl
    path: Path | None
    sha256: str | None
    rows: tuple[tuple[str | None, ...], ...]


@dataclass(frozen=True)
class SupplementSource:
    """Manifest-facing provenance of one supplement table: the declared
    (config-relative) file string and its SHA-256, both None for inline."""

    file: str | None
    sha256: str | None


def load_supplements(
    config: ExportConfig,
    config_dir: Path,
) -> tuple[ResolvedSupplement, ...]:
    """Resolve every declared supplement's data beside the loaded config.

    The filesystem step the model does not take: resolves each `file`
    against `config_dir`, reads and hashes the bytes, decodes them as
    `utf-8-sig`, parses them with Python `csv` (default dialect,
    `strict=True`, `newline=''` semantics), checks the header against the
    declaration, and yields the text rows (an empty field is None). Inline
    supplements are rendered to text rows with no file facts: `None` ->
    None, `str` -> itself (`''` stays the empty string), `int` -> decimal
    text, `float` -> `repr`.

    Args:
        config: The validated export config.
        config_dir: The directory the config file was loaded from.

    Returns:
        One ResolvedSupplement per declaration, in declaration order; empty
        when the config declares none.

    Raises:
        SupplementFileMissing: A `file` does not exist or is not a regular
            file.
        SupplementFileInvalid: The bytes are not UTF-8, do not parse as
            CSV, or a data row's field count differs from the header's
            (names the 1-based data row).
        SupplementHeaderMismatch: The header row differs from the declared
            column names in count, order, or spelling; names the first
            differing position (a headerless file mismatches at position 1).
    """


def compile_supplement_specs(
    emit: Emit,
    supplements: Sequence[ResolvedSupplement],
    anchor: EffectiveAnchor | None,
    write_mode: Literal["create", "replace"],
) -> list[QuerySpec]:
    """Compile every supplement into a QuerySpec over the session.

    Each spec's SQL is the VALUES-and-cast relation over the resolved text
    rows (§ The relation: a leading row-position column, every cell a
    VARCHAR literal or NULL, one CAST per declared column to its canonical
    type, ordered by position with the position projected away; the typed
    `SELECT CAST(NULL AS <type>) AS <col>, … WHERE false` form for zero
    rows) and carries no keys, empty `provenance` / `kind_values`, the
    author's `descriptions` as `author_descriptions`, the author's
    `description` as `author_table_description`, `event_log=False`, and a
    `SupplementSource`. Runs, in order: the reserved-name gate over every
    supplement name (`is_reserved_table_name`, the supplement message); the
    anchor rule (a TIMESTAMPTZ column with `anchor` None); the cell probe
    (§ Evaluation before any write — per column in declared order, the
    first row whose non-NULL cell `TRY_CAST`s to NULL). Every gate is a
    pure function of the declaration, the resolved rows, and the anchor —
    no emit table is read — which is what lets the shaped head call this
    at open.

    Args:
        emit: The open emit whose session materializes the relation.
        supplements: The resolved supplements, in declaration order.
        anchor: The resolved effective anchor, or None — consulted by the
            TIMESTAMPTZ rule only.
        write_mode: 'create' for a full export, 'replace' for a windowed
            compile — the caller's delivery regime, never inferred.

    Returns:
        One QuerySpec per supplement, in declaration order; empty for an
        empty input.

    Raises:
        ExportError: A supplement name is a bookkeeping name.
        TemporalRenderRequiresAnchor: A TIMESTAMPTZ column with anchor None;
            names the supplement and the column.
        SupplementValueInvalid: A cell does not cast to its declared type;
            names the table, column, 1-based data row, and cell text.
    """


def check_supplement_sources_not_outputs(
    supplements: Sequence[ResolvedSupplement],
    output_paths: Collection[Path],
    removed_dirs: Collection[Path],
) -> None:
    """Refuse a supplement whose resolved source file this invocation would
    overwrite or delete.

    Both tests run over the resolved source path (`Path.resolve()` on both
    sides): equality with any output path; containment under any removed
    directory at any depth.

    Args:
        supplements: The resolved supplements (inline ones have no path
            and are skipped).
        output_paths: Every file the invocation writes, resolved — the
            union of the naming functions for the invocation's fmt and
            regime (plus `out` itself under fmt='duckdb'), assembled by the
            caller from those functions, never enumerated by hand.
        removed_dirs: Every directory the invocation removes wholesale,
            resolved — `csv_removed_dirs` under a CSV `--next` window or
            range; empty for a full export and every DuckDB invocation.

    Raises:
        SupplementSourceIsOutput: A file supplement's `path` equals an
            output path, or lies under a removed directory; names the
            supplement and the path.
    """
```

### Query spec + report (`src/fabulexa_forge/exporters/query_spec.py`)

```python
@dataclass(frozen=True)
class QuerySpec:
    """(Existing fields unchanged.) Gains:"""

    supplement: SupplementSource | None = None
    """Set iff this spec is a supplement table; forwarded to TableReport by
    both report-assembly sites (`write_query_specs`, the driver's
    `_build_windowed_report`)."""


@dataclass(frozen=True)
class TableReport:
    """(Existing fields unchanged.) Gains, stated explicitly at every
    report-assembly site like its siblings — no default:"""

    supplement: SupplementSource | None
```

`write_query_specs` keeps its signature; both writer arms stamp
`supplement=spec.supplement`.

### Naming functions (pure; each site's existing convention made callable)

```python
def csv_output_paths(
    out: Path,
    table_names: Sequence[str],
    window_label: str | None,
) -> tuple[Path, ...]:
    """(`writers/csv.py`.) Every data file a CSV write of `table_names` lands.

    Args:
        out: The output directory.
        table_names: The output tables, in plan order.
        window_label: The `--next` window's label, or None for a full export
            or an explicit range (whose drop is `out` itself).

    Returns:
        `<out>/<table>.csv` per table, or `<out>/<window_label>/<table>.csv`
        under `--next`, in `table_names` order.
    """


def companion_artifact_paths(
    target: Path,
    mode: str,
    fmt: Literal["csv", "duckdb"],
) -> tuple[Path, Path]:
    """(`companion/artifacts.py` — the existing private `_artifact_paths`,
    made public and re-exported from `companion/__init__`.) The README +
    manifest pair's paths under the placement rule: inside the output
    directory for CSV (the root, never a window drop), siblings of the
    database file for DuckDB, under the existing prefix convention
    (`<mode>` for CSV, `<db-stem>-<mode>` for DuckDB).

    Args:
        target: The output directory (csv) or `.duckdb` file path (duckdb).
        mode: The export config's mode literal.
        fmt: Output format.

    Returns:
        `(readme_path, manifest_path)`.
    """


def csv_cursor_path(out: Path) -> Path:
    """(`incremental/cursor.py`.) The `--next` CSV cursor file's path,
    `<out>/_CURSOR_FILE` — the cursor writer's own naming, read by the
    source-is-output gate under `--next` CSV and by the cursor reader /
    writer themselves.

    Args:
        out: The drop parent directory.

    Returns:
        The cursor file path.
    """


def csv_removed_dirs(out: Path, window: Window) -> tuple[Path, ...]:
    """(`incremental/driver.py`.) The directories a CSV windowed invocation
    removes wholesale — the driver's existing names made callable, read by
    the CSV write path and the source-is-output gate so the two can never
    disagree.

    Under `--next` (window.index is not None): the window drop, deleted and
    re-created from staging on every emitting window, and the staging
    directory, whose leftover is discarded at the next staging. Under a
    range (window.index is None): the sibling staging directory alone —
    the range's drop is `out`, refused when pre-existing, never removed.

    Args:
        out: The drop parent directory (`--next`) or the range's drop
            (`--from/--to`).
        window: The window being exported; its `index` distinguishes the
            two regimes, its `label` names the directories.

    Returns:
        `(<out>/<label>, <out>/.tmp_<label>)` under `--next`;
        `(<out parent>/.tmp_<label>,)` under a range.
    """
```

### Manifest (`src/fabulexa_forge/exporters/companion/manifest.py`)

`_MANIFEST_FORMAT_VERSION` becomes `3`. `build_manifest_document` and
`render_manifest_bytes` keep their signatures; `_table_json` gains a
`supplement` key on every entry: `None` when `table.supplement` is None,
otherwise `{"file": <declared string or None>, "sha256": <hex or None>}`.
The embedded `config` dump carries `supplements` through the existing
`model_dump` (with `file` as declared and `rows` verbatim).

### Dimensional entry point (`src/fabulexa_forge/exporters/dimensional/engine.py`)

```python
def export_dimensional(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    overlay: ReadmeOverlay | None,
    supplements: Sequence[ResolvedSupplement],
) -> ExportReport:
    """(Existing contract.) Gains `supplements`, the loader-resolved set —
    empty when the config declares none. After the plan compile and before
    any write, in order: compiles the supplement specs
    (`compile_supplement_specs(..., write_mode='create')`); runs
    `check_supplement_sources_not_outputs` over the union of
    `csv_output_paths(out, <plan + supplement names>, None)` and
    `companion_artifact_paths(out, config.mode, fmt)` under CSV, or
    `(out,)` plus the companion pair under DuckDB, with an empty
    removed-directory set; then validates the overlay's `table:` slots
    against the union of plan and supplement names. Supplement specs are
    appended after the declared tables in declaration order and flow
    through the same write dispatch and companion write.

    Raises:
        (Existing.) Plus every compile_supplement_specs error and
        SupplementSourceIsOutput, all before the first write.
    """
```

### CLI (`src/fabulexa_forge/cli.py`)

```python
def _resolve_supplements(
    config: ExportConfig, config_path: Path
) -> tuple[ResolvedSupplement, ...]:
    """Load `config.supplements`, resolved against the config file's
    directory — the sibling of `_resolve_readme_overlay`, run in the same
    place (after the config loads, before the emit opens).

    Args:
        config: The validated export config.
        config_path: The export-config YAML path, as given on the command line.

    Returns:
        `load_supplements(config, config_path.parent)` — empty when the
        config declares none.

    Raises:
        SupplementFileMissing / SupplementFileInvalid /
            SupplementHeaderMismatch: per load_supplements.
    """
```

`_dispatch_export` gains `supplements: Sequence[ResolvedSupplement]` after
`overlay` and threads it to `export_incremental_next`, `export_window`, and
`export_dimensional`; the source and base leaves receive the config's
guarantee (a source / base config cannot declare supplements) and take no
parameter. `cmd_export` calls `_resolve_supplements` beside
`_resolve_readme_overlay` inside the existing `(ReaderError, ExporterError)`
funnel, so every supplement refusal renders as `ERROR: …` with exit 1 and no
data written.

### Incremental driver (`src/fabulexa_forge/incremental/driver.py`)

```python
def export_window(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    window: Window,
    fingerprint: str | None,
    notice_sink: NoticeSink,
    overlay: ReadmeOverlay | None,
    supplements: Sequence[ResolvedSupplement],
) -> WindowedExport:
    """(Existing contract.) Gains `supplements`. Under the dimensional arm,
    after the mode compile and before any write: compiles the supplement
    specs with write_mode='replace' and appends them after the mode's
    specs; runs the source-is-output gate with the invocation's outputs —
    under CSV `csv_output_paths(out, <all names>, window.label if --next
    else None)`, `companion_artifact_paths(out, config.mode, 'csv')`, plus
    `csv_cursor_path(out)` under `--next`, with `csv_removed_dirs(out,
    window)` as the removed set; under DuckDB `(out,)` plus the companion
    pair, removed set empty — then validates the overlay against the union
    of names. The CSV write path derives its staging and drop directories
    from `csv_removed_dirs` (the one naming authority). A supplement is
    delivered as `snapshot` — DuckDB `replace`, CSV the whole file in the
    window drop — in every emitting window, the empty window included.
    The source and base arms never see a supplement (the config cannot
    declare one).

    Raises:
        (Existing.) Plus every compile_supplement_specs error and
        SupplementSourceIsOutput, all before the first write.
    """


def export_incremental_next(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    overlay: ReadmeOverlay | None,
    supplements: Sequence[ResolvedSupplement],
) -> IncrementalOutcome:
    """(Existing contract.) Gains `supplements`, threaded to the fingerprint
    computation (`_build_fingerprint` → `compute_fingerprint`) and to
    `export_window`.
    """
```

### Fingerprint (`src/fabulexa_forge/incremental/fingerprint.py`)

```python
def compute_fingerprint(
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    sidecar_sha256: str,
    fork_path: str,
    fmt: Literal["csv", "duckdb"],
    package_version: str,
    supplements: Sequence[ResolvedSupplement],
) -> str:
    """(Existing contract.) Gains `supplements`, serialized under the key
    `supplements` in the canonical document as a map from supplement name
    to `{"columns": [[name, canonical_type], ...], "sha256": hex-or-null}`
    — the column list as an ordered list of pairs, since the canonical
    form's sorted keys would erase a reordering of the declared map;
    `canonical_type` from `canonical_supplement_type`. `_FINGERPRINT_EXCLUDE`
    additionally excludes every `supplements[].description` /
    `descriptions` / `file` (the sha256 in the map is the file's identity;
    its path is not) and still carries `columns` and `rows` as declared.

    Args:
        (Existing.) Plus supplements: the resolved set, declaration order;
            empty when none are declared.
    """
```

### Shaped playback (`src/fabulexa_forge/playback/shaped.py`)

```python
def open_shaped_playback(
    emit: Emit,
    config: ExportConfig,
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    supplements: Sequence[ResolvedSupplement],
) -> ShapedPlayback:
    """(Existing contract.) Gains `supplements`: the resolved supplement set
    bound beside the shape — empty for a source shape (the config cannot
    declare any). Open runs every supplement gate through
    `compile_supplement_specs(emit, supplements, anchor, 'create')` after
    the mode's own open validation; each gate is a pure function of the
    declaration, the resolved rows, and the anchor, so open still reads no
    emit data and no supplement ask ever refuses. The compiled specs are
    bound on the head.

    Raises:
        (Existing.) Plus the reserved-name ExportError,
        TemporalRenderRequiresAnchor, and SupplementValueInvalid, all at
        open.
    """


class ShapedPlayback:
    """A shaped tape head: the target shape's tables per window or as of T."""

    def __init__(
        self,
        emit: Emit,
        config: ExportConfig,
        anchor: EffectiveAnchor | None,
        notice_sink: NoticeSink,
        table_decls: tuple[ShapedTableDecl, ...],
        election: Election,
        supplement_specs: tuple[QuerySpec, ...],
    ) -> None:
        """(Existing.) Gains `supplement_specs`: the open-compiled supplement
        specs in declaration order, empty for a source shape."""

    def tables(self) -> tuple[ShapedTableDecl, ...]:
        """(Existing.) A dimensional shape's tuple is the declared tables in
        declaration order followed by one `ShapedTableDecl(name,
        'snapshot')` per supplement in declaration order — the union the
        per-ask selection gates range over."""

    def window(
        self,
        start_sim_time: int,
        end_sim_time: int,
        tables: Collection[str] | None = None,
    ) -> tuple[ShapedTable, ...]:
        """(Existing.) The selection resolves over the union. The dimensional
        part of the selection compiles exactly as today; when it is empty
        (a supplement-only selection) no horizon is opened, no mode compile
        runs, and no plan notice is emitted. Every selected supplement is
        delivered after the dimensional tables, in declaration order, as
        `snapshot` with its whole relation — identical for every window."""

    def state(
        self,
        at_sim_time: int,
        tables: Collection[str] | None = None,
    ) -> tuple[ShapedTable, ...]:
        """(Existing.) As `window()`: supplements join the answer after the
        dimensional tables, `snapshot`, identical at every T; a
        supplement-only selection opens no truncated tape and emits no
        notice."""
```

### Pack builder (`tools/build_dataset_pack.py`)

```python
def _packed_config_members(
    entry: DatasetEntry, example_dir: Path
) -> list[tuple[str, Path]]:
    """Locate every configs entry, load it through the loader its top-level
    shape names, and collect its supplement files as members.

    Replaces `_resolve_config_paths`. A document with a top-level `mode`
    key loads through `load_export_config`; one with a top-level `streams`
    key through `load_stream_config`; a document with neither is refused.
    For an export config, `load_supplements(config, example_dir)` resolves
    every file supplement; each is a member at its config-relative path.

    Args:
        entry: The authored manifest entry naming the configs.
        example_dir: The dataset's example directory.

    Returns:
        (archive path, source path) pairs: each config in the entry's
        authored order, then each of its supplement files in declaration
        order. Determinism is unaffected — `_write_deterministic_archive`
        sorts members by path.

    Raises:
        PackBuildError: A configs entry is absent, naming it; a config is
            neither an export nor a stream config; a config its loader
            refuses (the loader's diagnostic, prefixed `"dataset '{name}':
            config '{cfg}': "`); a supplement file that is missing or whose
            resolved path is outside `example_dir`.
    """
```

`build_pack` keeps its signature and its docstring gains the loader-gate and
supplement-member rules.

## Phases

### Phase 1: Declaration and loader

**Delivers:** `SupplementDecl`, `ExportConfig.supplements`, all nine parse-time
validators, `canonical_supplement_type`, the three loader `ConfigError`s,
`ResolvedSupplement`, `SupplementSource`, and `load_supplements`. Purely
additive — no existing signature changes.

**Demo:** Writes a config with one file supplement (a UTF-8-BOM CSV with an
empty cell and a quoted newline) and one inline supplement into a temp dir,
loads it, prints each `ResolvedSupplement`'s `path` / `sha256` / text rows;
then drives one refusal of each kind (unknown type, both sources, unquoted
`yes`, duplicate name, collision with a declared table, header mismatch,
ragged row, missing file, `supplements` under `mode: source`) and prints the
message.

**Contracts:** `canonical_supplement_type`, `SupplementDecl` + validators,
`ExportConfig.supplements` + validators, the three `ConfigError`s,
`ResolvedSupplement`, `SupplementSource`, `load_supplements`.

**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Create | `src/fabulexa_forge/exporters/supplements.py` |
| Create | `tests/config/test_supplements.py` |
| Create | `tests/exporters/test_supplements_load.py` |
| Modify | `tests/test_errors.py` |
| Create | `docs/sprints/supplementary-tables/demos/phase_1_declare_and_load.py` |

**Tests:**
- `tests/config/test_supplements.py`:
  - `canonical_supplement_type`: each vocabulary member round-trips upper-cased; `bigint` / ` Bigint ` → `BIGINT`; `decimal(10, 2)` → `DECIMAL(10,2)`; `DECIMAL(39,0)`, `DECIMAL(5,6)` raise the bounds message; `INTERVAL`, `BLOB`, `VARCHAR(10)`, `STRUCT(a INT)` raise.
  - A file supplement parses; an inline supplement parses; `rows: []` parses.
  - `supplements` under `mode: source` and `mode: base` is refused.
  - `supplements: []` is refused; two supplements named alike refused; a supplement named like a declared dimensional table refused with the collision message naming both.
  - `name: "1abc"` refused; `columns: {}` refused; a column name with a hyphen refused; a blank type refused.
  - Unknown type refused with the vocabulary message naming supplement, column, and text.
  - `file` + `rows` refused; neither refused; `file: "  "` refused.
  - An inline row with a missing key refused naming the row; one with an extra key refused.
  - Unquoted `2024-01-01` refused with "quote temporal values (row 1, column 'd')"; unquoted `yes` / `true` / `off` refused with "quote boolean values"; `bool` refused even in an INTEGER column.
  - Blank `description` refused; `descriptions` with an undeclared key refused; blank `descriptions` value refused.
  - Docstring convention test (`tests/config/test_docstring_convention.py`) still passes over the new model.
- `tests/exporters/test_supplements_load.py`:
  - No `supplements` → `()`.
  - File supplement: `path` is `config_dir / file` resolved; `sha256` equals `hashlib.sha256(bytes).hexdigest()`; rows in file order; empty field → `None` (quoted `""` too); quoted newline preserved inside a cell.
  - BOM-prefixed header matches the declaration.
  - Header-only CSV → `rows == ()`; zero-byte file → `SupplementHeaderMismatch` at position 1.
  - Header with a column re-ordered / renamed / extra / missing → `SupplementHeaderMismatch` naming the first differing position and both spellings.
  - Ragged data row → `SupplementFileInvalid` naming the 1-based data row and both counts.
  - Non-UTF-8 bytes → `SupplementFileInvalid` "not UTF-8".
  - Unterminated quote → `SupplementFileInvalid` "not valid CSV".
  - Missing file / a directory at the path → `SupplementFileMissing`.
  - Inline: `null` → `None`; `''` → `''`; `7` → `'7'`; `0.1` → `'0.1'`; `1e3` (YAML float) → `'1000.0'`; rows in list order; `path` and `sha256` are `None`.
  - Two supplements resolve in declaration order.
- `tests/test_errors.py`: the three new classes subclass `ConfigError`, are catchable as `ExporterError`, and do not subclass `ReaderError`.
- Existing: `tests/config/`, `tests/test_errors.py` all pass.

### Phase 2: Compile, full export, and companions

**Delivers:** `compile_supplement_specs`, `check_supplement_sources_not_outputs`,
`csv_output_paths`, `companion_artifact_paths`, `QuerySpec.supplement` /
`TableReport.supplement` stamped at both report-assembly sites, the manifest
`supplement` field + format version 3, `export_dimensional` carrying
supplements, and the CLI wiring for the full export. A supplement exports
end-to-end under both formats.

**Demo:** Builds a self-contained minimal emit, a config with one file and one
inline supplement (VARCHAR, BIGINT, DOUBLE, DATE, DECIMAL(5,2), BOOLEAN, a
`NULL`), exports under CSV and DuckDB, prints each supplement table's DESCRIBE
and rows, the README's table section, and the manifest's `tables[]` entries
(`supplement` populated vs `null`, `manifest_format_version: 3`); then shows
`SupplementValueInvalid` on a bad cell, `TemporalRenderRequiresAnchor` on a
TIMESTAMPTZ column with no anchor, and `SupplementSourceIsOutput` when
`file: region_code.csv` sits in the CSV output directory — each with the
output directory still empty afterward.

**Contracts:** `SupplementValueInvalid`, `SupplementSourceIsOutput`,
`compile_supplement_specs`, `check_supplement_sources_not_outputs`,
`csv_output_paths`, `companion_artifact_paths`, `QuerySpec.supplement`,
`TableReport.supplement`, manifest v3, `export_dimensional`,
`_resolve_supplements`, `_dispatch_export`.

**Steps:** `source → migrate (fan-out, 12 files) → author (3 files) → author (3 files)` — atomic: `TableReport.supplement` and the `export_dimensional` parameter are required, so every existing constructor / caller is red until migrated. The two author steps split unit tests from end-to-end tests so neither re-reads the whole surface.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/supplements.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/exporters/companion/manifest.py` |
| Modify | `src/fabulexa_forge/exporters/companion/artifacts.py` |
| Modify | `src/fabulexa_forge/exporters/companion/__init__.py` |
| Modify | `src/fabulexa_forge/writers/csv.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `tests/exporters/test_companion_integration.py` |
| Modify | `tests/exporters/test_notices.py` |
| Modify | `tests/exporters/dimensional/test_export_dimensional.py` |
| Modify | `tests/exporters/dimensional/test_rebasing.py` |
| Modify | `tests/incremental/test_driver.py` |
| Modify | `tests/incremental/test_horizon_acceptance.py` |
| Modify | `tests/recipes/test_determinism.py` |
| Modify | `tests/recipes/test_recipes.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_artifacts.py` |
| Create | `tests/exporters/test_supplements_compile.py` |
| Create | `tests/exporters/companion/test_paths.py` |
| Modify | `tests/writers/test_csv.py` |
| Create | `tests/exporters/dimensional/test_export_supplements.py` |
| Modify | `tests/test_cli_export.py` |
| Modify | `tests/test_errors.py` |
| Create | `docs/sprints/supplementary-tables/demos/phase_2_full_export.py` |

**Tests:**
- Migration (mechanical, intent preserved): every `export_dimensional(...)` call gains `supplements=()`; every `TableReport(...)` constructor gains `supplement=None`; the manifest-version pin in `test_manifest.py` reads `3`.
- `tests/exporters/test_supplements_compile.py`:
  - Empty input → `[]`.
  - One spec per supplement in declaration order; `write_mode` echoes the argument; `keys`, `upsert_key` None; `provenance`, `kind_values` empty; `author_descriptions` / `author_table_description` from the decl (empty map / None when absent); `event_log` False; `supplement` carries the declared `file` string (not the resolved path) and the sha256, both None for inline.
  - Materializing the SQL yields the declared column names in declared order with the canonical types (DESCRIBE), rows in given order, `None` cells as NULL.
  - Zero rows → typed empty relation with the declared columns.
  - Cast leniency is DuckDB's: `'1.5'` → BIGINT 2; `'12.345'` → DECIMAL(5,2) 12.35; `'yes'` → BOOLEAN true; `' 7 '` → BIGINT 7.
  - Bad cell → `SupplementValueInvalid` naming table, column, 1-based row, and the cell text; the first bad cell by column-then-row order is the one named.
  - TIMESTAMPTZ with anchor None → `TemporalRenderRequiresAnchor` naming supplement and column; with an anchor it compiles and renders in the anchor zone.
  - `name: _export_meta` → `ExportError` with the supplement reserved-name message.
  - Gate order: reserved name fires before a bad cell; anchor fires before a bad cell.
  - `check_supplement_sources_not_outputs`: inline-only passes with any paths; a path equal to an output refused; a path under a removed dir at depth 2 refused; a path merely sharing a parent with an output passes; a relative `output_paths` entry is compared resolved.
- `tests/exporters/companion/test_paths.py`: `companion_artifact_paths` under csv → `<out>/<mode>-readme.md` / `-manifest.json`; under duckdb → `<parent>/<stem>-<mode>-…`; `write_companion_artifacts` lands exactly those paths.
- `tests/writers/test_csv.py`: `csv_output_paths` with `window_label=None` → `<out>/<t>.csv` in order; with a label → `<out>/<label>/<t>.csv`; `write_csv`'s actual file equals the function's answer for that table.
- `tests/exporters/dimensional/test_export_supplements.py`:
  - CSV and DuckDB full export: supplement tables written after the declared tables in declaration order; `ExportReport.tables` order matches; `row_count` is the data-row count (0 for `rows: []`); `TableReport.supplement` populated for supplements, None for mode tables; `keys` None.
  - Written values round-trip: NULL cell empty in CSV / NULL in DuckDB; DATE / DECIMAL under the writers' pinned forms.
  - Idempotent: two exports byte-identical (CSV).
  - Manifest: `manifest_format_version == 3`; `tables[].supplement` is `{"file": "region_code.csv", "sha256": ...}` / `{"file": null, "sha256": null}` / `null` respectively; `description` / `columns[].description` from the author's prose, `unit` / `enum_options` null; `primary_key` / `unique` null; `config.supplements` embedded with `file` as declared.
  - README contains a `## table: <supplement>` section (or the template's per-table heading) rendering the author's table and column prose; an overlay whose `table:` slot names a supplement validates and renders; one naming an unknown table still raises `ReadmeOverlayUnknownTable`.
  - `SupplementSourceIsOutput`: file supplement whose resolved path equals `<out>/<name>.csv` under CSV; equals the DuckDB `out` path; equals the manifest path — each refused with nothing written (output dir empty / file absent).
  - A `SupplementValueInvalid` refusal leaves the output empty (no declared table written first).
  - No supplements (`supplements=()`) → output identical to before.
- `tests/test_cli_export.py`: full export via `cmd_export` with a file + inline supplement under both fmts exits 0 and prints the supplement row counts; a missing file exits 1 with `ERROR: supplement '…': file not found` and no output; a source-is-output config exits 1 and leaves the target empty; `supplements` under `mode: source` exits 1 at load.
- `tests/test_errors.py`: the two new classes subclass `ExportError`.
- Existing: the whole suite passes after migration.

### Phase 3: Incremental delivery and the fingerprint

**Delivers:** Supplements under `--next` and `--from/--to` for both formats
(`replace` / whole-file `snapshot` in every emitting window), `csv_cursor_path`,
`csv_removed_dirs` as the CSV write path's naming authority, the windowed
source-is-output gate, the fingerprint's `supplements` map and presentation
exclusions, and the CLI wiring for the two incremental leaves.

**Demo:** Self-contained emit; a `--next` CSV drip of three windows (one empty)
with a file supplement — prints each window drop's listing showing the
supplement re-delivered whole; then a DuckDB drip showing the table replaced
per window; then edits the supplement's `description` (next window succeeds),
renames the file with identical bytes (succeeds), and changes one CSV byte
(`IncrementalFingerprintMismatch`); finally a `--next` config whose source file
sits under the window drop → `SupplementSourceIsOutput` before any write.

**Contracts:** `csv_cursor_path`, `csv_removed_dirs`, `export_window`,
`export_incremental_next`, `compute_fingerprint`, `_dispatch_export`.

**Steps:** `source → migrate (fan-out, 6 files) → author (3 files)` — atomic: the three gained parameters are required.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/incremental/cursor.py` |
| Modify | `src/fabulexa_forge/incremental/fingerprint.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `tests/exporters/test_notices.py` |
| Modify | `tests/exporters/dimensional/test_election_fk.py` |
| Modify | `tests/incremental/test_companion_artifacts.py` |
| Modify | `tests/incremental/test_driver.py` |
| Modify | `tests/incremental/test_fingerprint.py` |
| Modify | `tests/incremental/test_horizon_acceptance.py` |
| Create | `tests/incremental/test_supplements.py` |
| Modify | `tests/incremental/test_cursor.py` |
| Modify | `tests/test_cli_export.py` |
| Create | `docs/sprints/supplementary-tables/demos/phase_3_incremental_drip.py` |

**Tests:**
- Migration (mechanical): every `export_window(...)` / `export_incremental_next(...)` / `compute_fingerprint(...)` call gains `supplements=()`; any pinned fingerprint literal in `test_fingerprint.py` is recomputed rather than hand-edited.
- `tests/incremental/test_supplements.py`:
  - `csv_removed_dirs`: `--next` → `(<out>/<label>, <out>/.tmp_<label>)`; range → `(<out parent>/.tmp_<label>,)`; the driver's actual staging / drop directories equal these (assert via the on-disk result of one window).
  - DuckDB `--next`: window 0 creates the supplement table with its rows; window 1 (with a mode-table delta) replaces it — same rows, no duplication; an empty window still rewrites it; `_export_windows` bookkeeping unaffected.
  - CSV `--next`: every window drop contains `<supplement>.csv` whole, byte-identical across drops.
  - Range (both fmts): the supplement is delivered whole.
  - The windowed `TableReport.supplement` is populated; `row_counts[<supplement>]` is the data-row count.
  - Fingerprint: `compute_fingerprint` with `supplements=()` differs from the same call before this sprint only by the `supplements` key (assert the canonical document contains `"supplements":{}`); changing `description` / `descriptions` / `file` (same bytes, renamed) leaves the digest unchanged; changing a CSV byte, an inline cell, a column name, a column order, or a type spelling changes it.
  - End-to-end mismatch: a second `export_incremental_next` after editing the CSV → `IncrementalFingerprintMismatch`; after editing only `description` → emits.
  - Source-is-output under `--next` CSV: a file at `<out>/<label>/<name>.csv`, at `<out>/.tmp_<label>/x.csv`, at `<out>/<mode>-manifest.json`, at `csv_cursor_path(out)` — each refused before any write (cursor absent, no drop). Under a range CSV: a file at `<out parent>/.tmp_<label>/x.csv` refused; a file at `<out>/<name>.csv` refused. DuckDB: a file at `out` refused.
- `tests/incremental/test_cursor.py`: `csv_cursor_path(out) == out / ".fabulexa-forge-cursor.json"`; `write_csv_cursor` writes exactly that path.
- `tests/test_cli_export.py`: `--next` and `--from/--to` via `cmd_export` with a supplement exit 0 and print the supplement's line; a mid-drip CSV edit exits 1 with the fingerprint message.
- Existing: the whole suite passes after migration; `test_horizon_acceptance.py` unchanged in intent.

### Phase 4: Shaped playback

**Delivers:** `open_shaped_playback` binding supplements; supplement tables in
`tables()`, `window()`, `state()`, and the per-ask selection domain; the
supplement-only selection that opens no horizon and emits no notice.

**Demo:** Self-contained emit with one type-1 dim and one upsert fact; a config
with two supplements. Opens the head, prints `tables()` (supplements last,
`snapshot`); prints `state(T)` at two instants showing the supplement rows
identical; prints a `window()` mixing a fact and a supplement; a
supplement-only `window(tables={...})` with a notice-counting sink showing zero
notices; a selection naming a supplement plus an unknown name → `PlaybackError`
listing the union; a config whose supplement has a bad cell refused at open.

**Contracts:** `open_shaped_playback`, `ShapedPlayback.__init__` / `tables` /
`window` / `state`.

**Steps:** `source → migrate (fan-out, 5 files) → author (1 file)` — atomic: the parameter is required.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Modify | `tests/exporters/dimensional/test_election_fk.py` |
| Modify | `tests/playback/test_shaped_open.py` |
| Modify | `tests/playback/test_shaped_selection.py` |
| Modify | `tests/playback/test_shaped_state.py` |
| Modify | `tests/playback/test_shaped_window.py` |
| Create | `tests/playback/test_shaped_supplements.py` |
| Create | `docs/sprints/supplementary-tables/demos/phase_4_shaped_playback.py` |

**Tests:**
- Migration (mechanical): every `open_shaped_playback(...)` call gains `supplements=()`.
- `tests/playback/test_shaped_supplements.py`:
  - `tables()` lists declared tables then supplements in declaration order, each supplement `window_delivery == 'snapshot'`; a source shape with `supplements=()` is unchanged.
  - `state(T)` includes every supplement with `delivery == 'snapshot'` and the full relation; `state(T1)` and `state(T2)` supplement tables are `equals`; T = 0 too.
  - `window(T1, T2)` includes every supplement whole; an empty window still delivers it.
  - Selection: `window(tables={sup})` returns just the supplement; `state(tables={sup, dim})` returns both in shape order; projection invariance — each singleton answer `equals` the corresponding table under `tables=None`.
  - Horizon economy: a supplement-only `window()` / `state()` emits zero notices on a config whose dimensional table carries an unobserved-value notice; a mixed selection emits the dimensional part's notices exactly as today.
  - Gates: an unknown name → `PlaybackError` whose declared-names list includes the supplements; a bare `str`; empty.
  - Open refusals: a bad cell → `SupplementValueInvalid` at open; `_export_meta` name → `ExportError` at open; TIMESTAMPTZ without anchor → `TemporalRenderRequiresAnchor` at open; a supplement colliding with a declared table never reaches open (parse refusal).
  - Open reads no emit data: patch `Emit.query_arrow` / the truncated-tape opener to count calls; open with a supplement makes no base-table read (the probe queries only the VALUES relation).
- Existing: `tests/playback/` passes after migration; `test_playback_package_imports_no_exporters_or_config` unchanged (only `shaped.py` gains an `exporters` import).

### Phase 5: Pack builder and the recipe

**Delivers:** The pack builder's loader gate and supplement-file carriage; the
`supplement-tables` recipe; the recipe harness resolving supplements and the
corpus guard admitting the recipe's CSV.

**Demo:** Builds a temp example directory (fixture emit + a dimensional config
with a file supplement under `data/`) and runs `build_pack`; prints the archive
member list showing `data/region_code.csv`; then shows the three refusals
(missing supplement file, `file: ../escape.csv`, a config that fails to load)
with their messages; finally runs the recipe's config through
`export_dimensional` against the recipe emit and prints the supplement table.

**Contracts:** `_packed_config_members`, `build_pack` (behavior).

**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `tools/build_dataset_pack.py` |
| Create | `examples/recipes/supplement-tables/config.yaml` |
| Create | `examples/recipes/supplement-tables/expect.yaml` |
| Create | `examples/recipes/supplement-tables/region_code.csv` |
| Modify | `tests/recipes/test_recipes.py` |
| Modify | `tests/datasets/test_pack_builder.py` |
| Create | `docs/sprints/supplementary-tables/demos/phase_5_pack_and_recipe.py` |

**Tests:**
- `tests/datasets/test_pack_builder.py`:
  - An export config with a file supplement at `data/region_code.csv` → the archive contains `data/region_code.csv` with the source bytes; member order still sorted; build byte-identical across two runs.
  - An inline-only supplement adds no member.
  - A stream config (`streams` top-level) still packs and loads through `load_stream_config`.
  - A missing supplement file → `PackBuildError` naming dataset, config, path, "missing".
  - `file: ../outside.csv` (existing outside) → `PackBuildError` "outside the dataset directory"; an absolute path likewise.
  - A config with neither `mode` nor `streams` → the "neither an export nor a stream config" message.
  - A config its loader refuses (unknown field) → `PackBuildError` carrying the loader's diagnostic with the `dataset '…': config '…': ` prefix.
  - Existing success / determinism / stamp / missing-bundle tests unchanged.
- `tests/recipes/test_recipes.py`:
  - Gate 2 passes `load_supplements(config, recipe.config_path.parent)` to `export_dimensional`.
  - The corpus guard admits `config.yaml`, `expect.yaml`, and any `*.csv`; a folder with any other file still fails the guard.
  - `supplement-tables` passes gates 1–3: `region_code` (file) and the inline supplement appear with the expected columns, row counts, and `contains_rows`.
- Existing: every other recipe passes unchanged.

## What Doesn't Change

- **Writers** (`writers/csv.py` `write_csv`, `writers/duckdb.py`): they serialize whatever relation they are handed; no writer learns what a supplement is. `csv_output_paths` is the existing placement made callable, not a new rule.
- **`compare`**, **corrupters**, **`init`**, **`StreamConfig`** and the streaming exporter: untouched.
- **Source and base modes**: `export_source` / `export_base` and their compiles take no `supplements` parameter — the config cannot declare one under those modes (parse refusal).
- **The README template, the ordering contract, and the overlay grammar** (`overview` / `table: <name>`): a supplement is one more output table; `validate_overlay_tables` is called with a longer name list, not changed.
- **`write_companion_artifacts` and `build_manifest_document` signatures**: supplement provenance rides on the report.
- **The documentation channel's inheritance rule** (`companion/dictionary.py`): a supplement column has no source property, inherits nothing, and renders the author's `descriptions` alone through the existing author-first path.
- **Per-column value elections, key election, the fk pathfind, `lookup`**: none reach a supplement; it is not a kind.
- **`build_query_specs`** (the dimensional plan compile): unchanged — supplement specs are compiled beside it and appended by the entry points.
- **The reader's boundary**: reading an author CSV is not opening `run.duckdb` / parsing `base.json`.
- **Tier-1 playback** (`playback/head.py`, `events`, `snapshot`, `seek`) and `stream.py`: untouched; only `shaped.py` gains an import from `exporters.supplements`.
- **The pack builder's determinism rules and `main`**: unchanged; supplement members join the sorted-path order.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | `canonical_supplement_type`; `SupplementDecl` + validators; `ExportConfig.supplements` + two validators |
| `src/fabulexa_forge/errors.py` | `SupplementFileMissing` / `SupplementFileInvalid` / `SupplementHeaderMismatch` (ConfigError); `SupplementValueInvalid` / `SupplementSourceIsOutput` (ExportError) |
| `src/fabulexa_forge/exporters/supplements.py` | New: `ResolvedSupplement`, `SupplementSource`, `load_supplements`, `compile_supplement_specs`, `check_supplement_sources_not_outputs` |
| `src/fabulexa_forge/exporters/query_spec.py` | `QuerySpec.supplement`; `TableReport.supplement` (required); both writer arms stamp it |
| `src/fabulexa_forge/exporters/companion/manifest.py` | `_MANIFEST_FORMAT_VERSION = 3`; `_table_json` emits `supplement` |
| `src/fabulexa_forge/exporters/companion/artifacts.py` | `_artifact_paths` → public `companion_artifact_paths` |
| `src/fabulexa_forge/exporters/companion/__init__.py` | Re-export `companion_artifact_paths` |
| `src/fabulexa_forge/writers/csv.py` | `csv_output_paths`; `write_csv` derives its path from it |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | `export_dimensional` gains `supplements`; compile + gate + overlay over the union; specs appended |
| `src/fabulexa_forge/cli.py` | `_resolve_supplements`; `_dispatch_export` threads `supplements` to the three dimensional-capable leaves |
| `src/fabulexa_forge/incremental/driver.py` | `_build_windowed_report` stamps `supplement`; `csv_removed_dirs`; `export_window` / `export_incremental_next` gain `supplements` (replace-compile, gate, overlay union); CSV path uses `csv_removed_dirs` |
| `src/fabulexa_forge/incremental/cursor.py` | `csv_cursor_path`; reader / writer use it |
| `src/fabulexa_forge/incremental/fingerprint.py` | `compute_fingerprint` gains `supplements`; `_FINGERPRINT_EXCLUDE` gains the three presentation fields |
| `src/fabulexa_forge/playback/shaped.py` | `open_shaped_playback` gains `supplements`; head binds compiled specs; `tables` / `window` / `state` over the union; supplement-only selection opens no horizon |
| `tools/build_dataset_pack.py` | `_packed_config_members` replaces `_resolve_config_paths`: loader gate + supplement members |
| `examples/recipes/supplement-tables/{config.yaml,expect.yaml,region_code.csv}` | New recipe |
| `tests/config/test_supplements.py` | New: model + vocabulary tests |
| `tests/exporters/test_supplements_load.py` | New: loader tests |
| `tests/exporters/test_supplements_compile.py` | New: compile + gate tests |
| `tests/exporters/companion/test_paths.py` | New: `companion_artifact_paths` |
| `tests/exporters/dimensional/test_export_supplements.py` | New: full-export end-to-end |
| `tests/incremental/test_supplements.py` | New: windowed delivery, fingerprint, gate |
| `tests/playback/test_shaped_supplements.py` | New: shaped head |
| `tests/test_errors.py` | Hierarchy assertions for the five classes |
| `tests/writers/test_csv.py` | `csv_output_paths` |
| `tests/incremental/test_cursor.py` | `csv_cursor_path` |
| `tests/test_cli_export.py` | CLI end-to-end (full, `--next`, range, refusals) |
| `tests/recipes/test_recipes.py` | Harness resolves supplements; corpus guard admits `*.csv` |
| `tests/datasets/test_pack_builder.py` | Loader gate + supplement-member tests |
| `tests/exporters/test_companion_integration.py`, `tests/exporters/test_notices.py`, `tests/exporters/dimensional/test_export_dimensional.py`, `tests/exporters/dimensional/test_rebasing.py`, `tests/exporters/dimensional/test_election_fk.py`, `tests/incremental/test_driver.py`, `tests/incremental/test_horizon_acceptance.py`, `tests/incremental/test_companion_artifacts.py`, `tests/incremental/test_fingerprint.py`, `tests/recipes/test_determinism.py`, `tests/exporters/companion/_fixtures.py`, `tests/exporters/companion/test_manifest.py`, `tests/exporters/companion/test_readme.py`, `tests/exporters/companion/test_artifacts.py`, `tests/playback/test_shaped_open.py`, `tests/playback/test_shaped_selection.py`, `tests/playback/test_shaped_state.py`, `tests/playback/test_shaped_window.py` | Mechanical migration to the required parameters |
| `docs/sprints/supplementary-tables/demos/phase_{1..5}_*.py` | Per-phase demos |
