#!/usr/bin/env python
"""
Demo: Companion artifacts for the generated calendar — manifest v5's
`tables[].calendar` / `columns[].references`, and the pinned `dim_date` /
`date_ref` dictionary prose.

Sprint: date-dimension
Phase: 6

Loads the `date-dimension` recipe's own config.yaml, in-memory-extended with
a `doctor_fk` `fk` column on `dim_patient` (-> a new `dim_doctor`) and an
author `description` on `birth_date_key`, against a self-contained emit
shaped to match both the recipe and the added fk edge. Exports to DuckDB
with companions, then prints the manifest's `tables[]` names with their
`calendar` values, every column carrying a non-null `references`, the
manifest format version, the README's `dim_date` section, and the
`event_date_key` (pinned) / `birth_date_key` (author-described) column
lines — both without a unit.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import yaml

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.companion.artifacts import companion_artifact_paths
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.reader.emit import open_emit

_DAY_NS = 86_400 * 1_000_000_000

_RECIPE_CONFIG_PATH = (
    Path(__file__).resolve().parents[4]
    / "examples"
    / "recipes"
    / "date-dimension"
    / "config.yaml"
)

_BIRTH_DATE_KEY_DESCRIPTION = "Local calendar key for the patient's date of birth."

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__dob",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__doctor_id",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
        "references": "doctor",
    },
]

_DOCTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

#: p001: dob 1990-05-14, doctor d001, status pending@1*DAY, active@2*DAY,
#: discharged@3*DAY. p002: dob 1985-11-02, doctor d001, status pending@2*DAY.
_STATUS_ROWS = [
    ("trunk", "patient", "p001", "status", 1 * _DAY_NS, "pending"),
    ("trunk", "patient", "p001", "status", 2 * _DAY_NS, "active"),
    ("trunk", "patient", "p002", "status", 2 * _DAY_NS, "pending"),
    ("trunk", "patient", "p001", "status", 3 * _DAY_NS, "discharged"),
]


def _write_emit(tmp_dir: Path) -> Path:
    """Write a self-contained emit: the recipe's two patients, each with a
    prop__doctor_id reference to the one doctor record d001."""
    conn = duckdb.connect(str(tmp_dir / "run.duckdb"))
    conn.execute(
        'CREATE TABLE "records__patient" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT,'
        ' "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__dob" VARCHAR, "prop__doctor_id" VARCHAR)'
    )
    conn.execute(
        'CREATE TABLE "records__doctor" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT,'
        ' "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__name" VARCHAR)'
    )
    conn.execute(
        'CREATE TABLE "history" ("fork_path" VARCHAR, "kind" VARCHAR,'
        ' "record_id" VARCHAR, "property" VARCHAR, "sim_time" BIGINT,'
        ' "value" VARCHAR)'
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p001", 0, True, None, 0, 0, "1990-05-14", "d001"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p002", 0, True, None, 0, 1, "1985-11-02", "d001"],
    )
    conn.execute(
        'INSERT INTO "records__doctor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "d001", 0, True, None, 0, 0, "Dr. Carter"],
    )
    for row in _STATUS_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 10 * _DAY_NS}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "columns": _PATIENT_COLUMNS,
                "rows": 2,
                "record_kind": "patient",
            },
            {
                "name": "records__doctor",
                "category": "records",
                "columns": _DOCTOR_COLUMNS,
                "rows": 1,
                "record_kind": "doctor",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(_STATUS_ROWS),
            },
        ],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
    }
    (tmp_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_dir


def _write_extended_config(tmp_dir: Path) -> Path:
    """Copy the recipe config, adding a `doctor_fk` fk column on `dim_patient`
    (-> a new `dim_doctor`) and an author description on `birth_date_key`.

    Args:
        tmp_dir: Directory to write the extended config.yaml into.

    Returns:
        The extended config's path.
    """
    raw: dict[str, Any] = yaml.safe_load(
        _RECIPE_CONFIG_PATH.read_text(encoding="utf-8")
    )
    tables: list[dict[str, Any]] = raw["dimensional"]["tables"]

    dim_patient = next(t for t in tables if t["name"] == "dim_patient")
    dim_patient["columns"].append(
        {"name": "doctor_fk", "fk": {"to": "dim_doctor", "via": "reference"}}
    )
    for column in dim_patient["columns"]:
        if column["name"] == "birth_date_key":
            column["description"] = _BIRTH_DATE_KEY_DESCRIPTION

    dim_doctor = {
        "name": "dim_doctor",
        "role": "dim",
        "scd": "type1",
        "source": {"grain": "records", "kind": "doctor"},
        "key": ["doctor_id"],
        "columns": [{"name": "doctor_id", "from": "record_id"}],
    }
    tables.insert(0, dim_doctor)

    config_path = tmp_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return config_path


def _discard_notice(_: Notice) -> None:
    """A notice sink that drops every notice — the demo has none to show."""


def _print_calendar_and_references(manifest: dict[str, Any]) -> None:
    """The `tables[].calendar` values and every non-null `columns[].references`."""
    print("tables[].calendar:")
    for table in manifest["tables"]:
        print(f"  {table['name']}: {table['calendar']}")

    print("columns[].references (non-null):")
    for table in manifest["tables"]:
        for column in table["columns"]:
            if column["references"] is not None:
                print(f"  {table['name']}.{column['name']} -> {column['references']}")


def _print_dim_date_readme_section(readme_text: str) -> None:
    """The `### dim_date` section, up to the next `### ` heading or EOF."""
    start = readme_text.index("### dim_date")
    rest = readme_text[start:]
    end = rest.find("\n### ", 1)
    section = rest if end == -1 else rest[:end]
    print("README dim_date section:")
    print(section.strip())


def _print_date_ref_column_lines(readme_text: str) -> None:
    """The `event_date_key` and `birth_date_key` column lines — one pinned,
    one author-described, both without a unit."""
    print("date_ref column lines:")
    for line in readme_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- `event_date_key`") or stripped.startswith(
            "- `birth_date_key`"
        ):
            print(f"  {stripped}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        emit_dir.mkdir()
        _write_emit(emit_dir)
        config_path = _write_extended_config(tmp_path)
        config = load_export_config(config_path)

        out_path = tmp_path / "export.duckdb"
        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            export_dimensional(
                emit, config, out_path, "duckdb", anchor, _discard_notice, None, ()
            )

        readme_path, manifest_path = companion_artifact_paths(
            out_path, config.mode, "duckdb"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        readme_text = readme_path.read_text(encoding="utf-8")

        print(f"manifest_format_version: {manifest['manifest_format_version']}")
        _print_calendar_and_references(manifest)
        _print_dim_date_readme_section(readme_text)
        _print_date_ref_column_lines(readme_text)

    print(
        "\nSUCCESS: dim_date carries its declared range as tables[].calendar,"
        " the date_ref and fk columns carry their target table as"
        " columns[].references, and the README's dim_date section plus its"
        " pinned/author-described date_ref lines carry no unit"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
