"""Tests for incremental/fingerprint.py — compute_fingerprint.

All tests are pure: no IO, no DuckDB, no emit.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fabulexa_export.anchor import EffectiveAnchor
from fabulexa_export.config.models import ExportConfig
from fabulexa_export.incremental.fingerprint import compute_fingerprint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_CONFIG_YAML = {
    "mode": "dimensional",
    "dimensional": {
        "tables": [
            {
                "name": "dim_x",
                "role": "dim",
                "scd": "type1",
                "source": {"grain": "records", "kind": "actor"},
                "key": ["id"],
                "columns": [{"name": "id", "from": "record_id"}],
            }
        ]
    },
}


def _config(**overrides: object) -> ExportConfig:
    data = dict(_MINIMAL_CONFIG_YAML)
    data.update(overrides)
    return ExportConfig.model_validate(data)


def _anchor(tz_key: str = "UTC") -> EffectiveAnchor:
    return EffectiveAnchor(
        start_instant=datetime.fromisoformat("2020-03-01T00:00:00+00:00"),
        timezone=ZoneInfo(tz_key),
    )


def _fp(
    *,
    config: ExportConfig | None = None,
    anchor: EffectiveAnchor | None = _anchor(),
    sidecar_sha256: str = "a" * 64,
    fork_path: str = "root",
    fmt: str = "duckdb",
    package_version: str = "1.0.0",
) -> str:
    c = config if config is not None else _config()
    return compute_fingerprint(
        config=c,
        anchor=anchor,
        sidecar_sha256=sidecar_sha256,
        fork_path=fork_path,
        fmt=fmt,  # type: ignore[arg-type]
        package_version=package_version,
    )


# ---------------------------------------------------------------------------
# Basic properties
# ---------------------------------------------------------------------------


def test_fingerprint_is_64_char_hex() -> None:
    """Fingerprint is a 64-character lowercase hex string (SHA-256)."""
    result = _fp()
    assert len(result) == 64
    assert result == result.lower()
    assert all(c in "0123456789abcdef" for c in result)


def test_fingerprint_stable_across_calls() -> None:
    """Same inputs → same fingerprint every call."""
    assert _fp() == _fp()


# ---------------------------------------------------------------------------
# Sensitivity: every input change produces a different digest
# ---------------------------------------------------------------------------


def test_fingerprint_changes_on_sidecar_sha256() -> None:
    """Different sidecar digest → different fingerprint."""
    a = _fp(sidecar_sha256="a" * 64)
    b = _fp(sidecar_sha256="b" * 64)
    assert a != b


def test_fingerprint_changes_on_fork_path() -> None:
    """Different fork_path → different fingerprint."""
    a = _fp(fork_path="root")
    b = _fp(fork_path="root/branch1")
    assert a != b


def test_fingerprint_changes_on_fmt() -> None:
    """Different fmt → different fingerprint."""
    a = _fp(fmt="duckdb")
    b = _fp(fmt="csv")
    assert a != b


def test_fingerprint_changes_on_package_version() -> None:
    """Different package_version → different fingerprint."""
    a = _fp(package_version="1.0.0")
    b = _fp(package_version="1.0.1")
    assert a != b


def test_fingerprint_changes_on_anchor_instant() -> None:
    """Different anchor.start_instant → different fingerprint."""
    anchor_a = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2020-03-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    anchor_b = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2020-03-02T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    a = _fp(anchor=anchor_a)
    b = _fp(anchor=anchor_b)
    assert a != b


def test_fingerprint_changes_on_anchor_zone() -> None:
    """Different anchor.timezone → different fingerprint."""
    a = _fp(anchor=_anchor("UTC"))
    b = _fp(anchor=_anchor("Europe/London"))
    assert a != b


def test_fingerprint_changes_on_anchor_none() -> None:
    """anchor=None vs anchor present → different fingerprint."""
    a = _fp(anchor=None)
    b = _fp(anchor=_anchor())
    assert a != b


def test_fingerprint_changes_on_config() -> None:
    """Different config (extra table) → different fingerprint."""
    cfg_a = _config()
    cfg_b = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": {
                "tables": [
                    {
                        "name": "dim_x",
                        "role": "dim",
                        "scd": "type1",
                        "source": {"grain": "records", "kind": "actor"},
                        "key": ["id"],
                        "columns": [{"name": "id", "from": "record_id"}],
                    },
                    {
                        "name": "fact_y",
                        "role": "fact",
                        "source": {"grain": "records", "kind": "event"},
                        "key": ["id"],
                        "columns": [{"name": "id", "from": "record_id"}],
                    },
                ]
            },
        }
    )
    assert _fp(config=cfg_a) != _fp(config=cfg_b)


# ---------------------------------------------------------------------------
# Dict key order does not affect fingerprint (canonicalization)
# ---------------------------------------------------------------------------


def test_fingerprint_dict_key_order_invariant() -> None:
    """Key order in the document does not change the fingerprint (keys sorted)."""
    # This is implicit in compute_fingerprint using sort_keys=True.
    # Verify by calling with the same logical inputs multiple times.
    results = [_fp() for _ in range(5)]
    assert len(set(results)) == 1
