"""Tests for Emit: open_emit, sidecar passthrough, and record_roles accessor."""

from __future__ import annotations

from pathlib import Path

from fabulexa_export import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_export.reader import RecordRoles, open_emit

from ._emit_helpers import write_emit


def _minimal_sidecar() -> dict[str, object]:
    """Minimal mechanism-emit sidecar with no record_roles block."""
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "history",
                "category": "fixed",
                "columns": [{"name": "fork_path", "type": "VARCHAR"}],
                "rows": 0,
            }
        ],
    }


def _sidecar_with_record_roles() -> dict[str, object]:
    """Sidecar carrying a record_roles block (actor + bare-string kinds)."""
    sidecar = _minimal_sidecar()
    sidecar["record_roles"] = {
        "actor": {"trip": "fact", "visit": "fact", "staff": "dimension"},
        "entity": "dimension",
        "asset": "fact",
    }
    return sidecar


# ---------------------------------------------------------------------------
# Emit: record_roles absent
# ---------------------------------------------------------------------------


def test_emit_record_roles_none_when_absent(tmp_path: Path) -> None:
    """emit.sidecar.record_roles() is None when the sidecar has no record_roles block."""
    emit_dir = write_emit(tmp_path, sidecar=_minimal_sidecar())
    with open_emit(emit_dir) as emit:
        assert emit.sidecar.record_roles() is None


# ---------------------------------------------------------------------------
# Emit: record_roles present
# ---------------------------------------------------------------------------


def test_emit_record_roles_returns_record_roles_instance(tmp_path: Path) -> None:
    """emit.sidecar.record_roles() returns a RecordRoles when the block is present."""
    emit_dir = write_emit(tmp_path, sidecar=_sidecar_with_record_roles())
    with open_emit(emit_dir) as emit:
        rr = emit.sidecar.record_roles()
        assert isinstance(rr, RecordRoles)


def test_emit_record_roles_kinds(tmp_path: Path) -> None:
    """emit.sidecar.record_roles().kinds() matches the sidecar block keys."""
    emit_dir = write_emit(tmp_path, sidecar=_sidecar_with_record_roles())
    with open_emit(emit_dir) as emit:
        rr = emit.sidecar.record_roles()
        assert rr is not None
        assert set(rr.kinds()) == {"actor", "entity", "asset"}


def test_emit_record_roles_is_subtyped_actor(tmp_path: Path) -> None:
    """emit.sidecar.record_roles().is_subtyped('actor') is True."""
    emit_dir = write_emit(tmp_path, sidecar=_sidecar_with_record_roles())
    with open_emit(emit_dir) as emit:
        rr = emit.sidecar.record_roles()
        assert rr is not None
        assert rr.is_subtyped("actor") is True


def test_emit_record_roles_role_of_bare_string_kind(tmp_path: Path) -> None:
    """emit.sidecar.record_roles().role_of('entity', None) == 'dimension'."""
    emit_dir = write_emit(tmp_path, sidecar=_sidecar_with_record_roles())
    with open_emit(emit_dir) as emit:
        rr = emit.sidecar.record_roles()
        assert rr is not None
        assert rr.role_of("entity", None) == "dimension"


def test_emit_record_roles_role_of_object_kind(tmp_path: Path) -> None:
    """emit.sidecar.record_roles().role_of('actor', 'trip') == 'fact'."""
    emit_dir = write_emit(tmp_path, sidecar=_sidecar_with_record_roles())
    with open_emit(emit_dir) as emit:
        rr = emit.sidecar.record_roles()
        assert rr is not None
        assert rr.role_of("actor", "trip") == "fact"
