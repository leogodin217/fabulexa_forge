"""Cross-directory test support packages.

`tests/` is the import root (no `tests/__init__.py`), so modules here resolve
as top-level packages: `from _support.sidecar_builder import ...`.
"""

from __future__ import annotations
