#!/usr/bin/env python3
"""Completion check for Principle #8 — no future scaffolding.

Flags every function, async function, or class whose body is nothing but
``pass`` (a leading docstring is allowed and ignored). A bare ``pass`` inside
an ``except`` handler is legitimate suppression, not scaffolding, and is not
flagged — which is why this is an AST walk rather than a grep for ``pass``.

Usage:
    python3 tools/check_stubs.py src

Exits 0 when clean, 1 when any stub body is found.
"""

from __future__ import annotations

import ast
import pathlib
import sys


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def find_stub_bodies(root: pathlib.Path) -> list[str]:
    """Return one ``path:line: name`` entry per stub-bodied definition."""
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            body = [stmt for stmt in node.body if not _is_docstring(stmt)]
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                hits.append(f"{path}:{node.lineno}: stub body -- {node.name}")
    return hits


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_stubs.py <dir>", file=sys.stderr)
        return 2
    hits = find_stub_bodies(pathlib.Path(sys.argv[1]))
    if hits:
        print("\n".join(hits))
        return 1
    print("no stub bodies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
