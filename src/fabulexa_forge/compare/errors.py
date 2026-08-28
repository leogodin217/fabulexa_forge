"""The compare-surface exception surface.

One exception type; `compare_datasets` raises it for every malformed-input
condition. A fresh top-level exception — compare is its own failure domain,
coupled to neither the export pipeline (`ExporterError`, `errors.py`) nor the
reader (`ReaderError`), matching the package's one-hierarchy-per-domain
convention.
"""

from __future__ import annotations


class CompareInputError(Exception):
    """Malformed compare input: an unreadable expected/actual path, a CSV file
    without a header row, an unknown or empty `tables` selection, an
    expected-side column type outside the canonical families (within the
    comparison universe), or an invalid `max_row_diffs`.

    The CLI's `compare` command catches it, renders the message to stderr,
    and exits 2.
    """
