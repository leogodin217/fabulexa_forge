"""Tests enforcing the structural docstring convention for config models.

Verifies that every BaseModel subclass in fabulexa_forge.config.models:
- has no Field(description=...) or accidental use_attribute_docstrings
- has a one-line class docstring
- keeps attribute docstrings under the character limit

Does NOT assert presence of attribute docstrings or prose quality — those are
review-time judgments per docs/architecture/config-docstrings.md.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

ATTR_DOCSTRING_MAX_CHARS = 400

_MODULE_NAME = "fabulexa_forge.config.models"
_module = importlib.import_module(_MODULE_NAME)
_module_source = inspect.getsource(_module)
_module_tree = ast.parse(_module_source)


def _get_model_classes() -> list[type[BaseModel]]:
    """Return every BaseModel subclass defined in the config models module."""
    classes = []
    for _name, obj in inspect.getmembers(_module, inspect.isclass):
        if issubclass(obj, BaseModel) and obj.__module__ == _MODULE_NAME:
            classes.append(obj)
    return classes


def _extract_attribute_docstrings(class_name: str) -> dict[str, str]:
    """Parse module AST and extract attribute docstrings for a given class.

    An attribute docstring is a string-literal ast.Expr immediately following
    an ast.AnnAssign in a ClassDef body.
    """
    result: dict[str, str] = {}
    for node in ast.walk(_module_tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        body = node.body
        for i, stmt in enumerate(body):
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if i + 1 >= len(body):
                continue
            next_stmt = body[i + 1]
            if not isinstance(next_stmt, ast.Expr):
                continue
            val = next_stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str):
                continue
            field_name = stmt.target.id if isinstance(stmt.target, ast.Name) else None
            if field_name is not None:
                result[field_name] = val.value
    return result


_MODEL_CLASSES = _get_model_classes()
_MODULE_PATH = Path(inspect.getfile(_module))


# ---------------------------------------------------------------------------
# Test 1: No leaked descriptions (Field(description=...) absent)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
def test_no_field_descriptions(cls: type[BaseModel]) -> None:
    """Every field in the model has description=None (no Field(description=...) leak)."""
    for field_name, field_info in cls.model_fields.items():
        assert field_info.description is None, (
            f"{cls.__name__}.{field_name}: "
            f"Field(description=...) is not allowed — "
            f"use an attribute docstring instead (config-docstrings.md)"
        )


# ---------------------------------------------------------------------------
# Test 2: use_attribute_docstrings flag is off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
def test_use_attribute_docstrings_flag_off(cls: type[BaseModel]) -> None:
    """model_config must not set use_attribute_docstrings=True."""
    assert cls.model_config.get("use_attribute_docstrings") is not True, (
        f"{cls.__name__}: use_attribute_docstrings must remain False "
        f"(see config-docstrings.md rationale)"
    )


# ---------------------------------------------------------------------------
# Test 3: Class docstrings are one line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
def test_class_docstring_is_one_line(cls: type[BaseModel]) -> None:
    """The class docstring, stripped, must contain no newline."""
    doc = cls.__doc__
    assert doc is not None, f"{cls.__name__}: missing class docstring"
    stripped = textwrap.dedent(doc).strip()
    assert "\n" not in stripped, (
        f"{cls.__name__}: class docstring must be one line; "
        f"relocate per-field prose to attribute docstrings "
        f"and cross-field rules to validator docstrings"
    )


# ---------------------------------------------------------------------------
# Test 4: Attribute docstrings stay under the character limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
def test_attribute_docstring_length(cls: type[BaseModel]) -> None:
    """Each attribute docstring must be <= ATTR_DOCSTRING_MAX_CHARS characters."""
    attr_docs = _extract_attribute_docstrings(cls.__name__)
    for field_name, doc_text in attr_docs.items():
        stripped = doc_text.strip()
        length = len(stripped)
        assert length <= ATTR_DOCSTRING_MAX_CHARS, (
            f"{cls.__name__}.{field_name}: attribute docstring is {length} chars "
            f"(limit {ATTR_DOCSTRING_MAX_CHARS}); shorten or move prose to a recipe"
        )
