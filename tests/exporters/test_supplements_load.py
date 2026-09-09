"""Tests for load_supplements: the loader step resolving a declared
supplement's data (file or inline) into text rows."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.errors import (
    SupplementFileInvalid,
    SupplementFileMissing,
    SupplementHeaderMismatch,
)
from fabulexa_forge.exporters.supplements import load_supplements

if TYPE_CHECKING:
    from pathlib import Path


def _dimensional_config(
    supplements: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """A minimal mode='dimensional' export config dict, optionally carrying
    a `supplements` list."""
    config: dict[str, object] = {
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
    if supplements is not None:
        config["supplements"] = supplements
    return config


def _region_supplement(**overrides: object) -> dict[str, object]:
    """One minimal file-backed supplement declaration."""
    decl: dict[str, object] = {
        "name": "region_code",
        "columns": {"code": "VARCHAR", "label": "VARCHAR"},
        "file": "region_code.csv",
    }
    decl.update(overrides)
    return decl


def test_no_supplements_returns_empty_tuple(tmp_path: "Path") -> None:
    """No `supplements` declared -> ()."""
    config = ExportConfig.model_validate(_dimensional_config())
    assert load_supplements(config, tmp_path) == ()


def test_file_supplement_resolves(tmp_path: "Path") -> None:
    """path is config_dir / file resolved; sha256 matches; rows in file
    order; empty field -> None (quoted "" too)."""
    csv_bytes = 'code,label\nUS,United States\nCA,""\n'.encode()
    (tmp_path / "region_code.csv").write_bytes(csv_bytes)
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    (resolved,) = load_supplements(config, tmp_path)

    assert resolved.path == (tmp_path / "region_code.csv").resolve()
    assert resolved.sha256 == hashlib.sha256(csv_bytes).hexdigest()
    assert resolved.rows == (("US", "United States"), ("CA", None))


def test_file_supplement_empty_field_is_none(tmp_path: "Path") -> None:
    """An unquoted empty field is None."""
    (tmp_path / "region_code.csv").write_text("code,label\nUS,\n", encoding="utf-8")
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    (resolved,) = load_supplements(config, tmp_path)

    assert resolved.rows == (("US", None),)


def test_file_supplement_quoted_newline_preserved(tmp_path: "Path") -> None:
    """A quoted newline inside a cell is preserved."""
    (tmp_path / "region_code.csv").write_text(
        'code,label\nUS,"multi\nline"\n', encoding="utf-8"
    )
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    (resolved,) = load_supplements(config, tmp_path)

    assert resolved.rows == (("US", "multi\nline"),)


def test_bom_prefixed_header_matches_declaration(tmp_path: "Path") -> None:
    """A UTF-8-BOM-prefixed header matches the declaration."""
    (tmp_path / "region_code.csv").write_bytes(
        b"\xef\xbb\xbfcode,label\nUS,United States\n"
    )
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    (resolved,) = load_supplements(config, tmp_path)

    assert resolved.rows == (("US", "United States"),)


def test_header_only_csv_yields_no_rows(tmp_path: "Path") -> None:
    """A header-only CSV -> rows == ()."""
    (tmp_path / "region_code.csv").write_text("code,label\n", encoding="utf-8")
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    (resolved,) = load_supplements(config, tmp_path)

    assert resolved.rows == ()


def test_zero_byte_file_is_header_mismatch_at_position_one(tmp_path: "Path") -> None:
    """A zero-byte file mismatches at position 1."""
    (tmp_path / "region_code.csv").write_bytes(b"")
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    with pytest.raises(SupplementHeaderMismatch, match="position 1"):
        load_supplements(config, tmp_path)


@pytest.mark.parametrize(
    "header",
    ["label,code", "codee,label", "code,label,extra", "code"],
)
def test_header_mismatch_variants_raise(tmp_path: "Path", header: str) -> None:
    """A header column re-ordered / renamed / extra / missing raises,
    naming the first differing position and both spellings."""
    (tmp_path / "region_code.csv").write_text(f"{header}\nUS,x\n", encoding="utf-8")
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    with pytest.raises(SupplementHeaderMismatch, match="position"):
        load_supplements(config, tmp_path)


def test_ragged_data_row_raises(tmp_path: "Path") -> None:
    """A ragged row raises, naming the 1-based data row and both counts."""
    (tmp_path / "region_code.csv").write_text(
        "code,label\nUS,United States\nCA\n", encoding="utf-8"
    )
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    with pytest.raises(
        SupplementFileInvalid, match="data row 2 has 1 fields, header has 2"
    ):
        load_supplements(config, tmp_path)


def test_non_utf8_bytes_raises(tmp_path: "Path") -> None:
    """Non-UTF-8 bytes raise 'not UTF-8'."""
    (tmp_path / "region_code.csv").write_bytes(b"code,label\n\xff\xfe,x\n")
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    with pytest.raises(SupplementFileInvalid, match="not UTF-8"):
        load_supplements(config, tmp_path)


def test_unterminated_quote_raises(tmp_path: "Path") -> None:
    """An unterminated quote raises 'not valid CSV'."""
    (tmp_path / "region_code.csv").write_text(
        'code,label\nUS,"unterminated\n', encoding="utf-8"
    )
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    with pytest.raises(SupplementFileInvalid, match="not valid CSV"):
        load_supplements(config, tmp_path)


def test_missing_file_raises(tmp_path: "Path") -> None:
    """A missing file raises SupplementFileMissing."""
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    with pytest.raises(SupplementFileMissing):
        load_supplements(config, tmp_path)


def test_directory_at_path_raises(tmp_path: "Path") -> None:
    """A directory at the declared path raises SupplementFileMissing."""
    (tmp_path / "region_code.csv").mkdir()
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))

    with pytest.raises(SupplementFileMissing):
        load_supplements(config, tmp_path)


def test_inline_supplement_cells(tmp_path: "Path") -> None:
    """Inline cells render: null -> None, '' -> '', 7 -> '7', 0.1 -> '0.1',
    1e3 -> '1000.0'; rows in list order; path and sha256 are None."""
    decl = _region_supplement(
        file=None,
        columns={"n": "BIGINT", "f": "DOUBLE", "s": "VARCHAR", "z": "VARCHAR"},
        rows=[
            {"n": 7, "f": 0.1, "s": "", "z": None},
            {"n": 7, "f": 1e3, "s": "x", "z": None},
        ],
    )
    config = ExportConfig.model_validate(_dimensional_config([decl]))

    (resolved,) = load_supplements(config, tmp_path)

    assert resolved.path is None
    assert resolved.sha256 is None
    assert resolved.rows == (
        ("7", "0.1", "", None),
        ("7", "1000.0", "x", None),
    )


def test_two_supplements_resolve_in_declaration_order(tmp_path: "Path") -> None:
    """Two supplements resolve in declaration order."""
    (tmp_path / "region_code.csv").write_text("code,label\nUS,x\n", encoding="utf-8")
    inline_decl = _region_supplement(
        name="rate_tier", file=None, rows=[{"code": "A", "label": "Alpha"}]
    )
    config = ExportConfig.model_validate(
        _dimensional_config([_region_supplement(), inline_decl])
    )

    resolved = load_supplements(config, tmp_path)

    assert [r.decl.name for r in resolved] == ["region_code", "rate_tier"]
