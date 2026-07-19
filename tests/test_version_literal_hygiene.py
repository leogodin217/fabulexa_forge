"""Version-literal hygiene: prose outside `contract/` never restates the
supported `base_format_version` (or any other contract version) as an integer
literal.

The version gate admits exactly one `base_format_version`
(`SUPPORTED_BASE_FORMAT_VERSION`), so a docstring, comment, or doc that names a
version integer is either stale (it names a version this reader no longer
supports) or redundant (it names the one version every emit this reader opens
must already be) — see `docs/architecture/README.md` § Inputs and fixtures,
"Prose is version-free".

Scanned surfaces are the prose carriers only — `.py` (docstrings/comments) and
`.md` (architecture docs) — under `SCANNED_ROOTS`; JSON/YAML data (a published
example's `base.json`, a demo config) is real data, not prose, and is out of
scope.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

VERSION_LITERAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # (a) `base_format_version` within ~8 characters of a digit — catches
    # "base_format_version 6", "base_format_version: 6", "base_format_version=99",
    # "base_format_version == 5", "v5 JSON Schema" phrased as "base_format_version
    # 5 JSON Schema", etc.
    re.compile(r"base_format_version.{0,8}?\d", re.IGNORECASE | re.DOTALL),
    # (b) word-bounded bare v+single-digit ("v6", "v4 emit", "at v5"). Excludes a
    # "v<digit>" glued onto a preceding word/hyphen character (`Alice-v0`,
    # `stateDiagram-v2`, a test's `_at_v5` identifier suffix) — those are sample
    # data tokens or code identifiers, not a bare version reference.
    re.compile(r"(?<![\w-])v\d\b"),
)

# ---------------------------------------------------------------------------
# Scanned surface
# ---------------------------------------------------------------------------

_SCANNED_EXTENSIONS = frozenset({".py", ".md"})

_SCANNED_ROOTS: tuple[Path, ...] = (
    _REPO_ROOT / "src",
    _REPO_ROOT / "tests",
    _REPO_ROOT / "docs",
)

_SCANNED_FILES: tuple[Path, ...] = (
    _REPO_ROOT / "CLAUDE.md",
    _REPO_ROOT / "README.md",
)

_EXCLUDED_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "contract",
    _REPO_ROOT / "docs" / "sprints",
    _REPO_ROOT / "docs" / "architecture" / "pending",
)

# ---------------------------------------------------------------------------
# Allowlist — repo-relative posix path -> reason
# ---------------------------------------------------------------------------

ALLOWED: Mapping[str, str] = {
    "src/fabulexa_forge/__init__.py": (
        "The single code literal — SUPPORTED_BASE_FORMAT_VERSION's assignment."
    ),
    "docs/architecture/README.md": (
        "The canonical status-table row citing the vendored contract's version, "
        "plus the 'Prose is version-free' bullet's own quoted example ('a v6 "
        "shape') of the pattern this hygiene test forbids everywhere else."
    ),
    "src/fabulexa_forge/reader/conformance.py": (
        "Historical rationale in C11's converse-clause docstring: 'legal at a "
        "prior contract version (v4)' names the version whose behavior changed, "
        "not the currently supported one — the version IS the content."
    ),
    "src/fabulexa_forge/reader/errors.py": (
        "Historical rationale: TemporalClassUnavailableError's docstring "
        "explains that the base_format_version 5 bump introduced the explicit "
        "temporal_class attribute, deleting the history_tracked-inference "
        "fiction this error replaces — the version IS the content."
    ),
    "tests/corrupters/operations/test_drop_events.py": (
        "Sample history-value tokens ('v1', 'v2') in test data, coincidentally "
        "version-shaped — not a version reference."
    ),
    "tests/corrupters/operations/test_shift_sim_time.py": (
        "Sample history-value tokens ('v0', 'v1') in test data, coincidentally "
        "version-shaped — not a version reference."
    ),
    "tests/exporters/streaming/test_routing_engine.py": (
        "Sample record id ('v1', the VIP row's id) in test data, coincidentally "
        "version-shaped — not a version reference."
    ),
    "tests/reader/test_sidecar.py": (
        "Version-gate / structural-floor type-validation tests exercise "
        "arbitrary out-of-range or mistyped base_format_version literals (99, "
        '3.0, "3") to prove the gate\'s strict-int check — test input values, '
        "not stale version prose."
    ),
    "tests/test_version_literal_hygiene.py": (
        "Self: this file's patterns and allowlist necessarily name version "
        "literals and the word 'version' near digits."
    ),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_excluded(path: Path) -> bool:
    return any(
        excluded == path or excluded in path.parents for excluded in _EXCLUDED_DIRS
    )


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCANNED_ROOTS:
        for candidate in root.rglob("*"):
            if (
                candidate.is_file()
                and candidate.suffix in _SCANNED_EXTENSIONS
                and not _is_excluded(candidate)
            ):
                files.append(candidate)
    files.extend(_SCANNED_FILES)
    return sorted(set(files))


def _relpath(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_version_literals_outside_allowlist() -> None:
    """Every version-literal-shaped match falls inside an ALLOWED file.

    A hit outside the allowlist means new prose named a version integer: fix it
    (de-version, or cite the contract's `§` section instead) or, if it is
    genuine historical rationale — the version IS the content — allowlist it
    with a reason.
    """
    violations: list[str] = []
    for path in _scanned_files():
        rel = _relpath(path)
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in VERSION_LITERAL_PATTERNS:
                match = pattern.search(line)
                if match is not None:
                    violations.append(
                        f"{rel}:{lineno}: {match.group(0)!r} in {line.strip()!r} "
                        "-- remedy: de-version the prose, cite the contract's § "
                        "section instead, or allowlist with a historical-"
                        "rationale reason in ALLOWED"
                    )
                    break

    assert not violations, "\n".join(violations)


def test_allowed_entries_still_match() -> None:
    """Every ALLOWED path exists and still matches at least one pattern.

    Guards against a dead allowlist entry: once the file it names no longer
    contains a version literal, the entry should be deleted, not carried
    forward as false documentation.
    """
    dead_entries: list[str] = []
    for rel in ALLOWED:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"ALLOWED entry {rel!r} does not exist"
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not any(pattern.search(text) for pattern in VERSION_LITERAL_PATTERNS):
            dead_entries.append(rel)

    assert not dead_entries, (
        "ALLOWED entries with no matching version literal (delete these): "
        + ", ".join(dead_entries)
    )
