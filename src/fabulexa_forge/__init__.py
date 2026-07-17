"""Fabulexa composite export — exporters and corrupters over base-layer emits.

Reads a base-layer emit (run.duckdb + base.json @ the supported
base_format_version) and writes differently-shaped datasets (exporters) or
realistically-broken base layers (corrupters). Zero dependencies outside the
vendored contract — the base-layer contract is the only coupling. See CLAUDE.md.
"""

__version__ = "0.0.1"

# The format version this package's vendored contract covers. The reader refuses any
# base.json whose base_format_version is not this value (no auto-upgrade).
SUPPORTED_BASE_FORMAT_VERSION = 6
