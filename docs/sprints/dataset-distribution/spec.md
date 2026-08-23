# Sprint: dataset-distribution

## Purpose

Ship the dataset-distribution surface: installed users run
`fabulexa-forge datasets list` (offline, from a manifest baked into the wheel)
and `fabulexa-forge datasets get <name>` (anonymous HTTPS download, sha256
verification, safe extraction, next-step commands printed), and maintainers
build deterministic release archives with a repo-side pack builder.

Design doc: `docs/architecture/pending/dataset-distribution.md` — the
authority on semantics, rationale, and invariants. This spec enumerates
contracts, phases, and tests; it does not restate the doc's prose. Where this
spec and the doc disagree, raise it — do not silently pick one.

**Design deviation (deliberate):** the manifest ships as **in-package** data at
`src/fabulexa_forge/datasets/manifest.yaml` (hatchling auto-includes files
inside `packages = ["src/fabulexa_forge"]`; one `importlib.resources` path
resolves in both the wheel and the in-tree layout). The doc's "force-include,
same mechanism as the vendored contract schema" described the mechanism for a
repo-root file; the invariant — the manifest ships as package data inside the
wheel — holds with no `pyproject.toml` change and no dual-path fallback.

The manifest ships **empty** (`datasets: []`) this sprint. Publishing real
packs (building archives from `docs/examples/`, uploading to a GitHub
Release, pasting stamped entries) is post-sprint maintainer work.

## Scope

**Capabilities touched:**
- CLI: new `datasets` verb — first verb with sub-verbs (`list` / `get`),
  parsed inside the handler
- Dataset distribution (new subsystem `src/fabulexa_forge/datasets/`):
  manifest models + loader, listing renderer, fetch/verify/extract with the
  injectable `Transport` seam
- Repo-side tooling: deterministic pack builder at
  `tools/build_dataset_pack.py` (never shipped in the wheel)
- Hygiene tests: manifest version agreement + command/config coherence

**Not included:** Hugging Face / Kaggle dual-publish (deferred by the design);
publishing actual packs / manifest entries; the superseded
`docs/examples/*/published/` convention.

## Success Criteria

- [ ] `fabulexa-forge datasets list` renders the shipped (empty) manifest
      offline in text and `--format json`, exit 0
- [ ] `fabulexa-forge datasets get <unknown>` errors to stderr naming valid
      names, exit 1, no network I/O attempted
- [ ] `get_dataset` end-to-end against a local-bytes transport: download,
      size + sha256 + member-safety verification, extraction, `{dir}`
      substitution — with failure atomicity (target untouched on every
      pre-extraction failure) and no temporary residue
- [ ] Pack builder produces a byte-identical archive on rebuild (same input
      tree → same sha256) and prints a paste-ready stamp fragment
- [ ] Hygiene tests pin every manifest entry's `base_format_version` to
      `SUPPORTED_BASE_FORMAT_VERSION` and every command's `{dir}/*.yaml`
      reference to the entry's `configs`
- [ ] Existing CLI help/usage tests pass unmodified (they parametrize over
      `VERBS`; `datasets --help` / `-h` prints
      `usage: fabulexa-forge datasets`, bare `datasets` exits 2)
- [ ] `make check` green

## Contracts

Extracted from the design doc §§ Interface Contracts + Validation Rules.
Module placement is this spec's decision; behavior is the doc's.

### `src/fabulexa_forge/datasets/models.py`

```python
class DatasetEntry(StrictBaseModel):
    """One published dataset: identity, pinned bytes, pack contents, next steps."""

    name: str
    description: str
    url: str
    sha256: str
    size_bytes: int
    base_format_version: int
    configs: list[str]
    commands: list[str]

    @model_validator(mode="after")
    def entry_well_formed(self) -> Self:
        """name matches `[a-z0-9]+(-[a-z0-9]+)*` (lowercase alphanumeric runs
        separated by single hyphens); url is https; sha256 is 64 lowercase hex;
        size_bytes > 0; base_format_version >= 1; configs and commands
        non-empty; every configs entry is a bare filename ending '.yaml' with
        no path separator ('/' or '\\'); every command contains '{dir}' at
        least once, and every brace-delimited run in it (each match of
        `\{[^{}]*\}`) is exactly '{dir}' — no other placeholder exists."""


class DatasetManifest(StrictBaseModel):
    """The authored allowlist of published datasets, in authored order."""

    datasets: list[DatasetEntry]

    @model_validator(mode="after")
    def names_unique(self) -> Self:
        """Manifest entry names are unique."""
```

`StrictBaseModel` is imported from `fabulexa_forge.config.models`.

### `src/fabulexa_forge/datasets/manifest.py`

```python
def load_manifest() -> DatasetManifest:
    """Load and validate the dataset manifest shipped as package data.

    Resolves `importlib.resources.files("fabulexa_forge") / "datasets" /
    "manifest.yaml"` — one path, valid in both the wheel and in-tree layouts.

    Returns:
        The validated manifest, entries in authored order.

    Raises:
        ValidationError: If the packaged manifest does not satisfy the model.
        yaml.YAMLError: If the packaged document is not parseable YAML,
            propagated from the parser.
        (Both unreachable in a released wheel — the hygiene test loads the
        manifest and gates the build — but loud during development.)
    """


def render_dataset_listing(manifest: DatasetManifest, fmt: str) -> str:
    """Render the manifest as the `datasets list` payload.

    Args:
        manifest: The loaded manifest.
        fmt: "text" for the human table, "json" for the byte-stable
            document (the manifest's field set verbatim, raw values,
            authored entry order, sorted keys, separators (",", ":")).
            An empty manifest renders the no-datasets line under "text"
            and the model document verbatim under "json".

    Returns:
        The complete stdout payload, without trailing newline.
    """
```

Text-format per-entry content (design doc § `datasets list`): name,
human-readable size rendered from `size_bytes`, `base_format_version`, config
coverage (the `configs` filenames verbatim, authored order), description.

### `src/fabulexa_forge/datasets/fetch.py`

```python
Transport = Callable[[str], BinaryIO]
"""Opens a URL for streamed reading. The CLI wires the stdlib HTTPS opener
with a network timeout (value is CLI presentation, not contract); tests wire
local-bytes openers. The seam that keeps the suite offline.

Failure contract: a failed open or read — timeout included — raises OSError
(which subsumes urllib's URLError/HTTPError). get_dataset maps OSError by
phase, not source: any OSError during download-and-verify (transport open,
transport read, temporary-file write — one streaming loop) becomes
DatasetError; nothing broader is caught, and post-verification OSError
propagates unwrapped."""


class DatasetError(Exception):
    """A dataset operation failed: unknown name, occupied target, download
    failure, verification mismatch, or unsafe archive. The message is the
    user-facing diagnostic; the CLI maps it to stderr + exit 1."""


@dataclass(frozen=True)
class GetResult:
    """Outcome of a successful dataset get.

    Attributes:
        target_dir: Directory the pack was extracted into.
        commands: The entry's example commands with {dir} substituted.
    """

    target_dir: Path
    commands: tuple[str, ...]


def get_dataset(
    manifest: DatasetManifest,
    name: str,
    target_dir: Path | None,
    force: bool,
    transport: Transport,
    progress: Callable[[int, int], None] | None,
) -> GetResult:
    """Download, verify, and extract one published dataset pack.

    Pipeline (design doc § `datasets get <name>`): resolve entry → check
    target path → download to a temporary file → verify (byte count, sha256,
    then every archive member's safety) → prepare target directory (--force
    removal only after all verification passes) → extract → delete the
    temporary archive → return next steps. The target path is never touched
    before the downloaded archive fully verifies; the temporary archive never
    survives — deleted on success and on every failure path alike.

    Args:
        manifest: The loaded manifest.
        name: Dataset to fetch; must match an entry's name.
        target_dir: Extraction directory; None means ./<name>. Created
            (parents included) when absent; the path as given here is what
            {dir} substitutes to — never absolutized.
        force: If True, an occupied target path (non-empty directory or
            non-directory) is removed and the directory recreated — only
            after the downloaded archive fully verifies (size, sha256,
            member safety), never before; if False, that condition refuses
            up front, before any download.
        transport: URL opener (HTTPS in the CLI, local bytes in tests).
        progress: Optional callback (bytes_received, size_bytes) for
            presentation-only progress reporting.

    Returns:
        GetResult with the target directory and substituted example commands.

    Raises:
        DatasetError: Unknown name; occupied target without force; download
            failure (any OSError during the download-and-verify phase —
            transport open/read and temporary-file write alike — mapped);
            size or sha256 mismatch; unsafe archive member (path escaping
            the target directory, or a non-regular member). All
            pre-extraction failures leave the target path exactly as found
            and the temporary archive deleted.
        OSError: An I/O failure after verification (target preparation or
            extraction), propagated unwrapped — environmental, not a
            dataset-contract failure; the target may hold a partial tree.
    """
```

Unsafe member = path escaping the target directory (absolute or `..`), or a
member that is not a regular file or directory (link, device, fifo). Every
member is checked before any is extracted.

### `src/fabulexa_forge/datasets/__init__.py`

Re-exports the public surface: `DatasetEntry`, `DatasetManifest`,
`load_manifest`, `render_dataset_listing`, `Transport`, `GetResult`,
`DatasetError`, `get_dataset`.

### `src/fabulexa_forge/cli.py`

```python
def _cmd_datasets(args: list[str]) -> int:
    """Handle the `datasets` verb: sub-verbs `list` and `get`.

    Follows the existing Verb registry shape: one entry in VERBS; the
    sub-verb split is parsed inside this handler via argparse subparsers
    (prog="fabulexa-forge datasets", so --help/-h satisfy the parametrized
    help tests). A missing or unknown sub-verb is an argparse usage error —
    usage text to stderr, exit 2. Payload (listing, next-step commands) to
    stdout; progress and diagnostics to stderr.

    `list [--format {text,json}]`: load_manifest → render_dataset_listing →
    stdout. No network I/O, ever. `--format` is a required-choice flag whose
    absence means text — argparse surface, not config (CLI presentation
    mirrors the existing `compare --format` flag).

    `get <name> [--dir DIR] [--force]`: load_manifest → get_dataset with the
    stdlib urllib transport (network timeout applied — value is CLI
    presentation) and a stderr progress callback → print the GetResult
    commands to stdout. DatasetError → its message to stderr, exit 1.
    """
```

Plus one `Verb("datasets", ...)` entry in `VERBS`.

### `tools/build_dataset_pack.py` (repo-side, never shipped)

```python
class PackBuildError(Exception):
    """A pack cannot be built: missing bundle file, missing config, or a
    bundle the reader refuses (including a base_format_version other than
    SUPPORTED_BASE_FORMAT_VERSION — the version refusal renders the reader's
    UnsupportedBaseFormatVersionError.found_version). The message is the
    refusal diagnostic; main maps it to stderr + exit 1."""


@dataclass(frozen=True)
class PackStamp:
    """The builder-stamped manifest fields for one built archive.

    Attributes:
        sha256: Digest of the archive bytes.
        size_bytes: Archive size in bytes.
        base_format_version: Stamped from the pack's own base.json, read
            through open_emit — never parsed ad hoc.
    """

    sha256: str
    size_bytes: int
    base_format_version: int


def build_pack(entry: DatasetEntry, example_dir: Path, out_path: Path) -> PackStamp:
    """Build one deterministic release archive from an example directory.

    Driven by the entry's authored fields: `configs` names the YAMLs packed;
    the entry's stamped fields (sha256, size_bytes, base_format_version) are
    ignored on read and recomputed. Archive layout: bundle/run.duckdb,
    bundle/base.json, bundle/ATLAS.md, and the configs at the archive root —
    all member paths relative, no wrapper directory.

    Deterministic means byte-identical: members added in sorted-path order;
    member mtime 0, uid/gid 0, uname/gname empty; mode 0644 for files, 0755
    for directories; gzip stream with mtime 0 and an empty original-filename
    field.

    Args:
        entry: The authored manifest entry driving the build.
        example_dir: The dataset's example directory
            (docs/examples/<name>/ in production use).
        out_path: The archive file to write (<out>/<name>.tar.gz in
            production use).

    Returns:
        The stamped fields computed from the written archive and the opened
        bundle.

    Raises:
        PackBuildError: Bundle triple (run.duckdb / base.json / ATLAS.md)
            incomplete, naming the missing file; a configs file absent from
            example_dir, naming it; the bundle refuses to open under
            open_emit (version refusal included).
    """


def render_stamp_fragment(stamp: PackStamp) -> str:
    """Render the stamped fields as a paste-ready YAML fragment for the
    maintainer to commit into the manifest entry, without trailing newline.

    Returns:
        Three lines: sha256, size_bytes, base_format_version.
    """


def main(argv: list[str]) -> int:
    """Entry point: build_dataset_pack.py <name> --out DIR.

    Locates docs/examples/<name>/ relative to the repo root and the entry by
    name in the shipped manifest (load_manifest); writes <out>/<name>.tar.gz;
    prints the stamp fragment to stdout. Print, never edit — the manifest is
    never rewritten. Refusals (PackBuildError, unknown name) to stderr,
    exit 1. Never talks to the network.
    """
```

## Phases

### Phase 1: Manifest subsystem — models, loader, listing

**Delivers:** The `datasets` package: validated models, the shipped (empty)
manifest, `load_manifest`, `render_dataset_listing`, and the hygiene tests.
**Demo:** Loads the shipped manifest offline; renders text and JSON for the
empty catalog; constructs a populated manifest in memory and renders both
formats; shows validator refusals (bad slug, http url, path-separator config,
foreign placeholder).
**Contracts:** `DatasetEntry`, `DatasetManifest`, `load_manifest`,
`render_dataset_listing`.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/datasets/__init__.py` |
| Create | `src/fabulexa_forge/datasets/models.py` |
| Create | `src/fabulexa_forge/datasets/manifest.py` |
| Create | `src/fabulexa_forge/datasets/manifest.yaml` |
| Create | `tests/datasets/test_models.py` |
| Create | `tests/datasets/test_manifest.py` |
| Create | `tests/datasets/test_manifest_hygiene.py` |
| Create | `docs/sprints/dataset-distribution/demos/phase_1_manifest_listing.py` |

**Tests:**
- `test_models.py` — validator acceptance: a fully well-formed entry passes.
  Refusals, one test each: uppercase / underscore / leading-hyphen name;
  non-https url; sha256 wrong length / uppercase hex; `size_bytes` 0 and
  negative; `base_format_version` 0; empty `configs`; empty `commands`;
  configs entry with `/` or `\`; configs entry not ending `.yaml`; command
  without `{dir}`; command with a foreign placeholder (`{out}`); duplicate
  entry names across the manifest; unknown field rejected (strict model).
- `test_manifest.py` — `load_manifest()` returns the shipped empty manifest
  (`datasets == []`) with no network and no filesystem outside package data;
  text render of an empty manifest is the no-datasets line; JSON render of an
  empty manifest is exactly `{"datasets":[]}`; text render of a two-entry
  manifest lists both in authored order with name, human size,
  `base_format_version`, configs verbatim, description; JSON render is
  byte-stable (sorted keys, `(",", ":")` separators, no trailing newline,
  authored entry order, `size_bytes` as integer); rendering twice yields
  identical bytes.
- `test_manifest_hygiene.py` — every shipped-manifest entry's
  `base_format_version == SUPPORTED_BASE_FORMAT_VERSION` (message names the
  stale entry per the doc's Business Rules table); every shipped-manifest
  command's `{dir}/`-prefixed `.yaml` path reference names a file in that
  entry's `configs` (covering `=`-attached forms like `--config={dir}/x.yaml`);
  both rules exercised against constructed violating manifests so the checks
  are proven non-vacuous while the shipped manifest is empty.

### Phase 2: get_dataset — fetch, verify, extract

**Delivers:** The full `get` pipeline behind the injectable transport:
resolve → target check → download → verify (size, sha256, member safety) →
prepare → extract → cleanup → substituted commands.
**Demo:** Builds a small pack archive in a temp dir; a local-bytes transport
serves it; `get_dataset` extracts it and prints substituted commands; then a
tampered byte trips sha256 verification with the target left untouched and no
temp residue.
**Contracts:** `Transport`, `DatasetError`, `GetResult`, `get_dataset`.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/datasets/fetch.py` |
| Modify | `src/fabulexa_forge/datasets/__init__.py` |
| Create | `tests/datasets/test_get_dataset.py` |
| Create | `docs/sprints/dataset-distribution/demos/phase_2_get_dataset.py` |

**Tests:** (all through local-bytes transports; no network)
- Success: pack extracted directly into the default `./<name>` target
  (relative members, no wrapper dir); `GetResult.commands` carry `{dir}`
  replaced by the path as given — `--dir` value verbatim, `./<name>` default —
  never absolutized; temp archive gone after success.
- Unknown name → `DatasetError` naming the dataset; transport never called.
- Occupied target (non-empty dir) without force → `DatasetError` naming the
  path; transport never called. Non-directory target file → same. Empty
  directory target → proceeds.
- Occupied target with `force=True` → old tree removed and recreated, but
  only after verification: with a sha-mismatched archive and `force=True`,
  the occupied target survives untouched.
- Transport open raises `OSError` → `DatasetError`; target as found; no temp
  residue. Transport read raising mid-stream → same.
- Byte count ≠ `size_bytes` → `DatasetError` naming expected and actual
  counts; nothing extracted; no temp residue.
- sha256 mismatch → `DatasetError` naming expected and actual digests;
  nothing extracted; no temp residue.
- Unsafe members, one test each: absolute path; `..` traversal; symlink;
  fifo — `DatasetError` naming the member, nothing written (all members
  checked before any extraction), target as found.
- Post-verification `OSError` (extraction failure into a read-only target
  dir, or equivalent) propagates unwrapped — not `DatasetError` — and the
  temp archive is still deleted.
- Progress callback receives `(bytes_received, size_bytes)` monotonically up
  to the full size; `progress=None` works.
- Determinism: same manifest + same bytes → identical extracted tree on a
  second get into a fresh dir.

### Phase 3: CLI verb `datasets`

**Delivers:** The `datasets` verb wired into `VERBS`: sub-verb parsing,
transport + progress wiring, exit-code and stream contract.
**Demo:** Drives `main()` in-process: `datasets list` (text and
`--format json`) against the shipped manifest; `datasets get nope` → exit 1
naming valid names; bare `datasets` and unknown sub-verb → exit 2 with usage
on stderr; `datasets --help` → usage on stdout exit 0; then a full
end-to-end `get` by monkeypatching the module's manifest + transport seams to
a locally built pack.
**Contracts:** `_cmd_datasets`, one `VERBS` entry.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/cli.py` |
| Create | `tests/test_cli_datasets.py` |
| Create | `docs/sprints/dataset-distribution/demos/phase_3_cli_datasets.py` |

**Tests:**
- `datasets list` prints the empty-catalog line to stdout, nothing to
  stderr, exit 0; `--format json` prints `{"datasets":[]}`.
- Bare `datasets` → argparse usage error to stderr, exit 2. Unknown
  sub-verb (`datasets frobnicate`) → same.
- `datasets --help` / `-h` → stdout starts `usage: fabulexa-forge datasets`,
  exit 0 (also covered by the parametrized tests in `test_cli_help.py`,
  which must pass unmodified).
- `datasets get nope` → stderr names `nope` and lists valid names, exit 1,
  stdout empty.
- `datasets get <name>` success path (manifest/transport seams
  monkeypatched): substituted commands on stdout, progress on stderr,
  exit 0.
- `datasets get` mapping: `DatasetError` from `get_dataset` → its message on
  stderr, exit 1.
- `datasets list` performs no network I/O: the transport factory is never
  invoked on the list path.
- Existing suites `tests/test_cli_help.py`, `tests/test_cli.py` still pass.

### Phase 4: Pack builder

**Delivers:** `tools/build_dataset_pack.py` — deterministic archive builder
with completeness/version refusals and the paste-ready stamp fragment.
**Demo:** Synthesizes a minimal valid emit inline (the prior-sprint demo
pattern), lays out an example-shaped tree in a temp dir, builds the pack
twice and shows byte-identical sha256; extracts and re-opens the packed
bundle through `open_emit`; shows the missing-config and missing-bundle-file
refusals.
**Contracts:** `PackBuildError`, `PackStamp`, `build_pack`,
`render_stamp_fragment`, `main`.
**Steps:** none (single implementer)

**Files:**
| Action | File |
|--------|------|
| Create | `tools/build_dataset_pack.py` |
| Create | `tests/datasets/test_pack_builder.py` |
| Create | `docs/sprints/dataset-distribution/demos/phase_4_pack_builder.py` |

**Tests:** (the test file loads the tool module via
`importlib.util.spec_from_file_location` — `tools/` is not a package; fixture
emits come from the existing `tests/_support` / reader emit helpers)
- Build success: archive contains exactly `bundle/run.duckdb`,
  `bundle/base.json`, `bundle/ATLAS.md`, and the entry's configs at the
  root — relative paths, no wrapper directory, no extra files (an
  `exports/` dir and a `demo.yaml` in the example dir are not packed when
  absent from `configs`).
- Determinism: building the same tree twice → byte-identical archives
  (same sha256). Normalization pinned: member mtime 0, uid/gid 0,
  uname/gname empty, file mode 0644, dir mode 0755, sorted member order,
  gzip mtime 0.
- Stamp: `PackStamp.sha256`/`size_bytes` match the written file;
  `base_format_version` equals the fixture emit's sidecar version (read via
  `open_emit`); the entry's authored stamped-field values are ignored on
  read (a garbage-sha entry still builds).
- `render_stamp_fragment` output is paste-ready YAML naming the three fields.
- Refusals, one test each: missing `run.duckdb` / `base.json` / `ATLAS.md`
  (naming the file); a `configs` file absent from the example dir (naming
  it); a bundle `open_emit` refuses — unsupported version — with the
  refusal rendering `found_version`.
- `main`: unknown dataset name → stderr, exit 1; success → stamp fragment on
  stdout, archive at `<out>/<name>.tar.gz`, exit 0.
- The tool performs no network I/O and never rewrites the manifest file.

## What Doesn't Change

Per the design doc § What Doesn't Change — binding here:

- `open_emit` and the reader's version gate: no second runtime version
  check anywhere in this sprint's code.
- Exporters, corrupters, streaming, compare, playback: untouched.
- Test fixtures stay synthesized; CI stays offline — no test touches the
  network; every fetch-path test goes through the transport seam.
- No new runtime dependencies: `urllib`, `hashlib`, `tarfile`, `importlib.
  resources` are stdlib; YAML and Pydantic are existing deps.
- The notice channel: `datasets` emits no `Notice` records — progress and
  diagnostics are CLI presentation on stderr.
- `pyproject.toml`: unchanged (in-package manifest needs no force-include).
- The existing verbs, `render_usage`, `dispatch`, `main` dispatch logic in
  `cli.py`: only the `VERBS` tuple gains an entry and `_cmd_datasets` is
  added.
- `tests/test_version_literal_hygiene.py` and its allowlist: new code
  references `SUPPORTED_BASE_FORMAT_VERSION`, never a version literal; the
  empty manifest carries no version value.
- `docs/examples/` content: read by the builder in production use, never
  modified; tests never depend on those gitignored bundles.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/datasets/__init__.py` | Create — public surface re-exports (P1, extended P2) |
| `src/fabulexa_forge/datasets/models.py` | Create — `DatasetEntry` / `DatasetManifest` + validators |
| `src/fabulexa_forge/datasets/manifest.py` | Create — `load_manifest`, `render_dataset_listing` |
| `src/fabulexa_forge/datasets/manifest.yaml` | Create — shipped manifest, empty (`datasets: []`) |
| `src/fabulexa_forge/datasets/fetch.py` | Create — `Transport`, `DatasetError`, `GetResult`, `get_dataset` |
| `src/fabulexa_forge/cli.py` | Modify — `_cmd_datasets` handler + `VERBS` entry |
| `tools/build_dataset_pack.py` | Create — deterministic pack builder (repo-side) |
| `tests/datasets/test_models.py` | Create — validator acceptance/refusal cases |
| `tests/datasets/test_manifest.py` | Create — loader + listing renderer |
| `tests/datasets/test_manifest_hygiene.py` | Create — version agreement + command/config coherence |
| `tests/datasets/test_get_dataset.py` | Create — full get pipeline, atomicity, safety |
| `tests/test_cli_datasets.py` | Create — verb surface, exit codes, stream split |
| `tests/datasets/test_pack_builder.py` | Create — layout, determinism, stamping, refusals |
| `docs/sprints/dataset-distribution/demos/phase_1_manifest_listing.py` | Create — demo |
| `docs/sprints/dataset-distribution/demos/phase_2_get_dataset.py` | Create — demo |
| `docs/sprints/dataset-distribution/demos/phase_3_cli_datasets.py` | Create — demo |
| `docs/sprints/dataset-distribution/demos/phase_4_pack_builder.py` | Create — demo |
