"""Tests for LookupClause and the ColumnDecl lookup mode.

Verifies the parse-time constraints introduced in the lookup-column-mode sprint:
- LookupClause parses correctly and rejects path-without-to.
- ColumnDecl accepts lookup as exactly one of six modes.
- The six-mode exactly-one rule raises on zero or two modes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import ColumnDecl, LookupClause

# ---------------------------------------------------------------------------
# LookupClause
# ---------------------------------------------------------------------------


def test_lookup_clause_property_only() -> None:
    """LookupClause with property only parses; to and path default to None."""
    clause = LookupClause.model_validate({"property": "journey_type"})
    assert clause.property == "journey_type"
    assert clause.to is None
    assert clause.path is None


def test_lookup_clause_with_to() -> None:
    """LookupClause with to set (no path) parses."""
    clause = LookupClause.model_validate({"property": "actor_type", "to": "patient"})
    assert clause.property == "actor_type"
    assert clause.to == "patient"
    assert clause.path is None


def test_lookup_clause_with_to_and_path() -> None:
    """LookupClause with to and path parses."""
    clause = LookupClause.model_validate(
        {"property": "actor_type", "to": "clinic", "path": ["patient", "clinic"]}
    )
    assert clause.property == "actor_type"
    assert clause.to == "clinic"
    assert clause.path == ["patient", "clinic"]


def test_lookup_clause_path_without_to_raises() -> None:
    """LookupClause with path set but to None raises ValidationError matching 'path'."""
    with pytest.raises(ValidationError, match="path"):
        LookupClause.model_validate({"property": "actor_type", "path": ["hop"]})


def test_lookup_clause_rejects_unknown_field() -> None:
    """LookupClause rejects unknown fields (extra='forbid')."""
    with pytest.raises(ValidationError):
        LookupClause.model_validate(
            {"property": "actor_type", "unknown_field": "value"}
        )


# ---------------------------------------------------------------------------
# ColumnDecl — lookup mode
# ---------------------------------------------------------------------------


def test_column_decl_lookup_only_is_valid() -> None:
    """ColumnDecl with lookup only is valid (exactly one mode)."""
    col = ColumnDecl.model_validate(
        {"name": "journey_type", "lookup": {"property": "journey_type"}}
    )
    assert col.lookup is not None
    assert col.lookup.property == "journey_type"
    assert col.from_ is None
    assert col.fk is None


def test_column_decl_lookup_and_from_raises() -> None:
    """ColumnDecl with lookup and from raises matching 'exactly one'."""
    with pytest.raises(ValidationError, match="exactly one"):
        ColumnDecl.model_validate(
            {
                "name": "col",
                "lookup": {"property": "actor_type"},
                "from": "some_col",
            }
        )


def test_column_decl_no_mode_raises() -> None:
    """ColumnDecl with no mode raises (regression for the six-mode rule)."""
    with pytest.raises(ValidationError, match="exactly one"):
        ColumnDecl.model_validate({"name": "col"})
