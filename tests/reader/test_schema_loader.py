"""Tests for reader/_schema.py: vendored-schema resolution paths.

Covers _read_schema_text's two-path resolution: the importlib.resources wheel
layout, the __file__-relative editable fallback, and the FileNotFoundError
raised when both fail (the documented packaging-defect signal).
"""

from __future__ import annotations

import json

import pytest

from fabulexa_export.reader import _schema

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _UnreadableRef:
    """A traversable-like ref whose read_text always raises."""

    def __init__(self, exc_type: type[Exception]) -> None:
        self._exc_type = exc_type

    def __truediv__(self, other: str) -> "_UnreadableRef":
        return self

    def read_text(self, encoding: str = "utf-8") -> str:
        raise self._exc_type("resource not readable")


class _NowherePath:
    """A Path-like whose parent/joins resolve to itself and which never exists."""

    def __init__(self, _path: str) -> None:
        pass

    @property
    def parent(self) -> "_NowherePath":
        return self

    def __truediv__(self, other: str) -> "_NowherePath":
        return self

    def exists(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_read_schema_text_resolves_vendored_schema() -> None:
    """_read_schema_text returns the vendored schema as parseable JSON."""
    text = _schema._read_schema_text()
    parsed = json.loads(text)
    assert isinstance(parsed, dict)


@pytest.mark.parametrize(
    "exc_type",
    [
        pytest.param(FileNotFoundError, id="file-not-found"),
        pytest.param(TypeError, id="type-error"),
    ],
)
def test_read_schema_text_falls_back_when_package_data_unreadable(
    monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
) -> None:
    """When importlib.resources fails, the __file__-relative fallback resolves.

    Both documented failure modes of the package-data read (FileNotFoundError
    and TypeError) route to the fallback, which finds the in-tree contract/
    copy in this editable checkout.
    """
    monkeypatch.setattr(
        "importlib.resources.files", lambda package: _UnreadableRef(exc_type)
    )
    text = _schema._read_schema_text()
    parsed = json.loads(text)
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Both paths fail — the packaging-defect signal
# ---------------------------------------------------------------------------


def test_read_schema_text_both_paths_fail_raises_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When package data AND the fallback path fail, FileNotFoundError surfaces.

    This is the explicit packaging-defect signal: never swallowed, never
    reported as an emit conformance failure.
    """
    monkeypatch.setattr(
        "importlib.resources.files",
        lambda package: _UnreadableRef(FileNotFoundError),
    )
    monkeypatch.setattr(_schema, "Path", _NowherePath)
    with pytest.raises(FileNotFoundError, match="packaging defect"):
        _schema._read_schema_text()
