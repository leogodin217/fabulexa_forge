"""Tests for fabulexa_forge.playback.selection.resolve_selection.

One positive + one negative case per business rule (design doc § Validation
Rules), plus the effective-set resolution behaviours the Phase 5 spec calls
out: properties=None / fields=None full-set resolution, the empty-tuple
identity-only form, named-tuple order independence, unknown record_ids
passing resolution, and the package's layer-direction invariant.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.selection import resolve_selection
from fabulexa_forge.playback.types import (
    MembershipAtomSelection,
    PlaybackSelection,
    RecordAtomSelection,
)

from ._fixtures import build_fixture_sidecar

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar


@pytest.fixture
def sidecar(tmp_path: Path) -> "Sidecar":
    return build_fixture_sidecar(tmp_path)


def _record(
    kind: str = "patient",
    sub_types: tuple[str, ...] = (),
    properties: tuple[str, ...] | None = (),
    record_ids: frozenset[str] | None = None,
) -> RecordAtomSelection:
    return RecordAtomSelection(
        kind=kind,
        sub_types=sub_types,
        properties=properties,
        record_ids=record_ids,
    )


def _membership(
    owner_kind: str = "patient",
    owner_sub_types: tuple[str, ...] = (),
    property_name: str = "team",
    fields: tuple[str, ...] | None = (),
    owner_record_ids: frozenset[str] | None = None,
) -> MembershipAtomSelection:
    return MembershipAtomSelection(
        owner_kind=owner_kind,
        owner_sub_types=owner_sub_types,
        property_name=property_name,
        fields=fields,
        owner_record_ids=owner_record_ids,
    )


# ---------------------------------------------------------------------------
# SelectionNonEmpty
# ---------------------------------------------------------------------------


def test_selection_non_empty_positive(sidecar: "Sidecar") -> None:
    resolve_selection(sidecar, PlaybackSelection(records=(_record(),), memberships=()))


def test_selection_non_empty_negative(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="playback selection is empty"):
        resolve_selection(sidecar, PlaybackSelection(records=(), memberships=()))


# ---------------------------------------------------------------------------
# RecordKindResolvable
# ---------------------------------------------------------------------------


def test_record_kind_resolvable_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar, PlaybackSelection(records=(_record("widget"),), memberships=())
    )


def test_record_kind_resolvable_negative(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="unknown kind 'ghost'"):
        resolve_selection(
            sidecar, PlaybackSelection(records=(_record("ghost"),), memberships=())
        )


# ---------------------------------------------------------------------------
# SubTypesDeclared (record side) — all three message variants
# ---------------------------------------------------------------------------


def test_sub_types_declared_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient", sub_types=("doctor",)),), memberships=()
        ),
    )


def test_sub_types_declared_unknown_value(sidecar: "Sidecar") -> None:
    with pytest.raises(
        PlaybackError, match="kind 'patient' declares no sub-type 'orderly'"
    ):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record("patient", sub_types=("orderly",)),), memberships=()
            ),
        )


def test_sub_types_declared_not_sub_typed(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="kind 'widget' is not sub-typed"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record("widget", sub_types=("x",)),), memberships=()
            ),
        )


def test_sub_types_declared_undeclared_discriminator_column(sidecar: "Sidecar") -> None:
    with pytest.raises(
        PlaybackError, match="kind 'drifted_patient' lacks its discriminator"
    ):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record("drifted_patient", sub_types=("a",)),), memberships=()
            ),
        )


# ---------------------------------------------------------------------------
# PropertiesResolvable
# ---------------------------------------------------------------------------


def test_properties_resolvable_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient", properties=("status",)),), memberships=()
        ),
    )


def test_properties_resolvable_negative(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="kind 'patient' has no property 'ghost'"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record("patient", properties=("ghost",)),), memberships=()
            ),
        )


# ---------------------------------------------------------------------------
# PropertiesNotSliceOnly — the exempt discriminator remains selectable
# ---------------------------------------------------------------------------


def test_properties_not_slice_only_exempt_discriminator_selectable(
    sidecar: "Sidecar",
) -> None:
    resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient", properties=("patient_type",)),), memberships=()
        ),
    )


def test_properties_not_slice_only_negative(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="'notes' on kind 'patient' is slice_only"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record("patient", properties=("notes",)),), memberships=()
            ),
        )


# ---------------------------------------------------------------------------
# MembershipResolvable
# ---------------------------------------------------------------------------


def test_membership_resolvable_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar, PlaybackSelection(records=(), memberships=(_membership(),))
    )


def test_membership_resolvable_negative(sidecar: "Sidecar") -> None:
    with pytest.raises(
        PlaybackError, match="no membership table for 'patient'.'ghost'"
    ):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(), memberships=(_membership(property_name="ghost"),)
            ),
        )


# ---------------------------------------------------------------------------
# OwnerSubTypesDeclared — all three message variants
# ---------------------------------------------------------------------------


def test_owner_sub_types_declared_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(), memberships=(_membership(owner_sub_types=("doctor",)),)
        ),
    )


def test_owner_sub_types_declared_unknown_value(sidecar: "Sidecar") -> None:
    with pytest.raises(
        PlaybackError, match="kind 'patient' declares no sub-type 'orderly'"
    ):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(), memberships=(_membership(owner_sub_types=("orderly",)),)
            ),
        )


def test_owner_sub_types_declared_not_sub_typed(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="kind 'widget' is not sub-typed"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(),
                memberships=(
                    _membership(
                        owner_kind="widget",
                        property_name="tags",
                        owner_sub_types=("x",),
                    ),
                ),
            ),
        )


def test_owner_sub_types_declared_undeclared_discriminator_column(
    sidecar: "Sidecar",
) -> None:
    with pytest.raises(
        PlaybackError, match="kind 'drifted_patient' lacks its discriminator"
    ):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(),
                memberships=(
                    _membership(
                        owner_kind="drifted_patient",
                        property_name="team",
                        owner_sub_types=("a",),
                    ),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# MembershipFieldsResolvable
# ---------------------------------------------------------------------------


def test_membership_fields_resolvable_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar,
        PlaybackSelection(records=(), memberships=(_membership(fields=("role",)),)),
    )


def test_membership_fields_resolvable_unknown_field(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="'patient'.'team' has no field 'ghost'"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(), memberships=(_membership(fields=("ghost",)),)
            ),
        )


def test_membership_fields_resolvable_duplicate_field(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="duplicate field 'role'"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(), memberships=(_membership(fields=("role", "role")),)
            ),
        )


# ---------------------------------------------------------------------------
# AtomsUnique
# ---------------------------------------------------------------------------


def test_atoms_unique_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient"), _record("widget")), memberships=()
        ),
    )


def test_atoms_unique_duplicate_record_kind(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="duplicate selection for 'patient'"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record("patient"), _record("patient")), memberships=()
            ),
        )


def test_atoms_unique_duplicate_membership(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="duplicate selection for"):
        resolve_selection(
            sidecar,
            PlaybackSelection(records=(), memberships=(_membership(), _membership())),
        )


# ---------------------------------------------------------------------------
# InstanceSetNonEmpty
# ---------------------------------------------------------------------------


def test_instance_set_non_empty_positive(sidecar: "Sidecar") -> None:
    resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient", record_ids=frozenset({"r1"})),), memberships=()
        ),
    )


def test_instance_set_non_empty_negative_record_ids(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="empty record_ids"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record("patient", record_ids=frozenset()),), memberships=()
            ),
        )


def test_instance_set_non_empty_negative_owner_record_ids(sidecar: "Sidecar") -> None:
    with pytest.raises(PlaybackError, match="empty owner_record_ids"):
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(),
                memberships=(_membership(owner_record_ids=frozenset()),),
            ),
        )


# ---------------------------------------------------------------------------
# Effective-set resolution
# ---------------------------------------------------------------------------


def test_properties_none_resolves_full_selectable_set_in_declaration_order(
    sidecar: "Sidecar",
) -> None:
    resolved = resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient", properties=None),), memberships=()
        ),
    )
    assert resolved.records[0].properties == ("patient_type", "name", "status")
    assert "notes" not in resolved.records[0].properties


def test_properties_empty_tuple_is_identity_only(sidecar: "Sidecar") -> None:
    resolved = resolve_selection(
        sidecar,
        PlaybackSelection(records=(_record("patient", properties=()),), memberships=()),
    )
    assert resolved.records[0].properties == ()


def test_named_property_order_does_not_affect_resolved_order(
    sidecar: "Sidecar",
) -> None:
    resolved = resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient", properties=("status", "name")),), memberships=()
        ),
    )
    assert resolved.records[0].properties == ("name", "status")


def test_fields_none_resolves_full_element_schema_field_set(sidecar: "Sidecar") -> None:
    resolved = resolve_selection(
        sidecar, PlaybackSelection(records=(), memberships=(_membership(fields=None),))
    )
    assert resolved.memberships[0].fields == ("role", "lead")
    assert resolved.memberships[0].full_fields == ("role", "lead")


def test_unknown_record_ids_pass_resolution(sidecar: "Sidecar") -> None:
    resolved = resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record("patient", record_ids=frozenset({"nonexistent"})),),
            memberships=(),
        ),
    )
    assert resolved.records[0].record_ids == frozenset({"nonexistent"})


# ---------------------------------------------------------------------------
# Layer direction
# ---------------------------------------------------------------------------


def _imported_module_names(file_path: Path) -> set[str]:
    """Every module name a file imports, via ast — text-level, never runtime."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_playback_package_imports_no_exporters_or_config() -> None:
    """Tier 1 (every module but `shaped.py`) imports no `exporters.*` / `config`
    name. `shaped.py` (tier 2) is the seam's one deliberate crossing — it wraps
    the exporters' own compile surfaces rather than reimplementing their
    business rules (design doc § Shaped playback (tier 2))."""
    package_dir = (
        Path(__file__).resolve().parents[2] / "src" / "fabulexa_forge" / "playback"
    )
    for py_file in package_dir.glob("*.py"):
        if py_file.name == "shaped.py":
            continue
        for module_name in _imported_module_names(py_file):
            assert "exporters" not in module_name, f"{py_file}: imports {module_name}"
            assert not module_name.endswith(".config") and module_name != "config", (
                f"{py_file}: imports {module_name}"
            )
