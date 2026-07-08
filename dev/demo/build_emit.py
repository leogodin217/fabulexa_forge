"""Materialize a real base-layer emit for the FabulMixer live-perform demo.

Writes ``run.duckdb`` + ``base.json`` into a destination directory by reusing the
recipe-test fixture builder (DuckDB + stdlib only; the Fabulexa producer is never
invoked). The emit is tiny but spans every table genre the streaming modes touch —
``records__*``, ``history``, and ``membership__*`` — across multiple kinds, so the
mixer board shows more than one channel strip.

Run from the repo root:

    uv run python dev/demo/build_emit.py [dest]   # default dest: dev/demo/emit

The emit is dev scratch, never committed (``.gitignore`` excludes ``dev/demo/emit/``).
Rebuild it any time; the output is fully deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The fixture builder lives under tests/ (an implicit namespace package). Put the
# repo root on sys.path so `tests.recipes` resolves regardless of the cwd Make uses.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.recipes._recipe_fixture import build_recipe_emit  # noqa: E402

_DEFAULT_DEST = _REPO_ROOT / "dev" / "demo" / "emit"


def main(argv: list[str]) -> int:
    """Build the demo emit into argv[0] (or the default dev/demo/emit).

    Idempotent: clears any prior run.duckdb / base.json first, so re-running
    rebuilds cleanly instead of failing on an already-populated database.
    """
    dest = Path(argv[0]) if argv else _DEFAULT_DEST
    for artifact in ("run.duckdb", "base.json"):
        (dest / artifact).unlink(missing_ok=True)
    build_recipe_emit(dest)
    print(f"demo emit written to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
