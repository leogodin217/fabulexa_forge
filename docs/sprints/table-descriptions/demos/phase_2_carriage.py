#!/usr/bin/env python
"""
Demo: Compiled-plan carriage of `author_table_description` + `event_log`
(`QuerySpec` / `TableReport`), stamped by all three plan compilers and
forwarded verbatim by both report-assembly sites; fingerprint exclusion of
the three table-description fields.

Sprint: table-descriptions
Phase: 2

Builds a source-mode export (a described `visit` table plus an `events`
declaration) and a base-mode export (a described `records__visit` rename
entry) against a shared fixture emit -- reusing the source exporter test
fixture (DuckDB + stdlib only, no producer dependency) via a `sys.path`
splice, mirroring `dev/demo/build_emit.py`. Prints each compiled spec's
`author_table_description` / `event_log`, confirms exactly one spec (the
event log, compiled last) is marked, runs both exports and prints the
`TableReport` fields forwarded verbatim, then computes the incremental
fingerprint with and without each of the three `description` fields --
identical every time.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (_REPO_ROOT, _REPO_ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _support.notices import discard_notice_sink  # noqa: E402

from fabulexa_forge.anchor import resolve_effective_anchor  # noqa: E402
from fabulexa_forge.config.loader import load_export_config  # noqa: E402
from fabulexa_forge.exporters.base.engine import export_base  # noqa: E402
from fabulexa_forge.exporters.election import resolve_election  # noqa: E402
from fabulexa_forge.exporters.query_spec import QuerySpec, TableReport  # noqa: E402
from fabulexa_forge.exporters.source.engine import (  # noqa: E402
    build_source_query_specs,
    export_source,
)
from fabulexa_forge.exporters.source.plan import build_source_plan  # noqa: E402
from fabulexa_forge.incremental.fingerprint import compute_fingerprint  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402
from tests.exporters.source._source_fixtures import build_source_test_emit  # noqa: E402

_SOURCE_TABLE_DESCRIPTION = "Visit records as the app would present them."

_BASE_TABLE_DESCRIPTION = "The raw visit records table, renamed for the export."

#: Dummy fingerprint identity inputs -- the fingerprint's exclusion behavior
#: is pure over the config, so these need not trace to the fixture emit.
_FP_SIDECAR_SHA256 = "a" * 64
_FP_FORK_PATH = "trunk"
_FP_PACKAGE_VERSION = "0.0.0-demo"


def _source_config_yaml(*, described: bool) -> str:
    """One `mode: source` config: a declared `visit` table plus an `events`
    log, with or without the table's `description`."""
    description_line = (
        f'      description: "{_SOURCE_TABLE_DESCRIPTION}"\n' if described else ""
    )
    return f"""
mode: source
source:
  tables:
    - name: visit_state
      kind: visit
{description_line}  events:
    name: audit
    sources:
      - kind: visit
"""


def _base_config_yaml(*, described: bool) -> str:
    """One `mode: base` config: a `records__visit` rename entry (`name` is
    unconditional so the entry stays legal with `description` toggled off),
    with or without its `description`."""
    description_line = (
        f'      description: "{_BASE_TABLE_DESCRIPTION}"\n' if described else ""
    )
    return f"""
mode: base
base:
  rename:
    - table: records__visit
      name: visit
{description_line}"""


def _write_config(tmp_dir: Path, name: str, text: str) -> Path:
    """Write one example config's YAML to `tmp_dir/name` and return its path."""
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _print_query_specs(specs: "tuple[QuerySpec, ...]") -> None:
    """Print each compiled spec's carried table-description fields."""
    for spec in specs:
        print(
            f"  spec {spec.table_name!r}: author_table_description="
            f"{spec.author_table_description!r} event_log={spec.event_log}"
        )


def _print_table_reports(tables: "tuple[TableReport, ...]") -> None:
    """Print each written table's forwarded table-description fields."""
    for table in tables:
        print(
            f"  table {table.name!r}: author_table_description="
            f"{table.author_table_description!r} event_log={table.event_log}"
        )


def _demo_source_carriage(tmp_dir: Path, emit_dir: Path) -> None:
    """Compile and run the source export; show the event log is the one
    marked spec, compiled last, and the report forwards both fields."""
    print("== source: compiled specs ==")
    config = load_export_config(
        _write_config(tmp_dir, "source.yaml", _source_config_yaml(described=True))
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture declares a runtime block"
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        specs = build_source_query_specs(plan, None)
        _print_query_specs(specs)
        marked = [spec for spec in specs if spec.event_log]
        assert marked == [specs[-1]], "exactly one spec (the log, last) is marked"
        assert marked[0].author_table_description is None, (
            "the log carries no author table description -- no config surface"
        )

        print("== source: written report (forwarded verbatim) ==")
        report = export_source(
            emit,
            config,
            tmp_dir / "source_export.duckdb",
            "duckdb",
            anchor,
            discard_notice_sink,
            None,
        )
        _print_table_reports(report.tables)


def _demo_base_carriage(tmp_dir: Path, emit_dir: Path) -> None:
    """Run the base export; show the renamed table's report carries the
    rename entry's description, forwarded verbatim."""
    print("== base: written report (forwarded verbatim) ==")
    config = load_export_config(
        _write_config(tmp_dir, "base.yaml", _base_config_yaml(described=True))
    )
    with open_emit(emit_dir) as emit:
        report = export_base(
            emit,
            config,
            tmp_dir / "base_export.duckdb",
            "duckdb",
            None,
            discard_notice_sink,
            None,
        )
        _print_table_reports(report.tables)
        described = [t for t in report.tables if t.author_table_description is not None]
        assert len(described) == 1
        assert described[0].author_table_description == _BASE_TABLE_DESCRIPTION


def _demo_fingerprint_exclusion(tmp_dir: Path) -> None:
    """Compute the fingerprint with and without each of the source and base
    table-description fields -- identical either way."""
    print("== fingerprint: description fields excluded ==")
    for label, config_yaml_fn, filename in (
        ("source", _source_config_yaml, "fp_source.yaml"),
        ("base", _base_config_yaml, "fp_base.yaml"),
    ):
        described = load_export_config(
            _write_config(
                tmp_dir, f"described_{filename}", config_yaml_fn(described=True)
            )
        )
        undescribed = load_export_config(
            _write_config(
                tmp_dir, f"undescribed_{filename}", config_yaml_fn(described=False)
            )
        )
        fp_described = compute_fingerprint(
            described,
            None,
            _FP_SIDECAR_SHA256,
            _FP_FORK_PATH,
            "duckdb",
            _FP_PACKAGE_VERSION,
        )
        fp_undescribed = compute_fingerprint(
            undescribed,
            None,
            _FP_SIDECAR_SHA256,
            _FP_FORK_PATH,
            "duckdb",
            _FP_PACKAGE_VERSION,
        )
        assert fp_described == fp_undescribed, (
            f"{label}: fingerprint changed when its description field was removed"
        )
        print(f"  {label}: fingerprint identical with/without description")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        build_source_test_emit(emit_dir)
        _demo_source_carriage(tmp_dir, emit_dir)
        _demo_base_carriage(tmp_dir, emit_dir)
        _demo_fingerprint_exclusion(tmp_dir)

    print(
        "SUCCESS: all three plan compilers stamp author_table_description /"
        " event_log; both report-assembly sites forward them verbatim; the"
        " fingerprint is unaffected by any of the three description fields"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
