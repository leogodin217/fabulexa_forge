"""Stage-wide single-branch guard for the derivations layer.

Enforces the trunk-only stage invariant: every derivation filters to the sole
branch's fork_path. This is the one implementation; all modes invoke it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_export.reader.sidecar import Sidecar

from fabulexa_export.errors import ExportError


def require_single_branch(sidecar: "Sidecar") -> str:
    """Enforce the trunk-only stage guard and return the sole branch's fork_path.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        The single branch's canonical fork_path, for derivations to filter on.

    Raises:
        ExportError: The sidecar enumerates zero or more than one branch.
    """
    branches = sidecar.branches()
    n = len(branches)
    if n != 1:
        raise ExportError(
            f"export requires a single-branch emit (trunk-only stage);"
            f" emit has {n} branches (branch-aware export is Stage 5)"
        )
    return branches[0].fork_path
