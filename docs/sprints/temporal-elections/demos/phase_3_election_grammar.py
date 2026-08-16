#!/usr/bin/env python
"""
Demo: Election config models
Sprint: temporal-elections
Phase: 3

The complete temporal-rendering election grammar in `config/models.py` — a
parse surface only; no mode consumes an election yet (Phase 4-6 wire the
attach points). This demo exercises every election form the grammar admits
and every refusal its validators enforce.

Shows:
  1. Loads three YAML configs (dimensional / source / base — mutually
     exclusive sections on `ExportConfig`) each exercising every election
     form: dimensional `as` on timestamp/scd_window/elapsed, a `date_parse`
     derivation, source table `render`/`date_parse` maps, events `render`,
     base `render` declaration list. Prints the parsed elections.
  2. Demonstrates five refusals: both-set elapsed, neither-set elapsed, an
     incomplete `date_parse` format, a `%H` directive, and a column named in
     both `render` and `date_parse`.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

from pydantic import ValidationError

from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import (
    DateParseSpec,
    ElapsedSpec,
    ExportConfig,
    SourceTableDecl,
)

_DIMENSIONAL_YAML = textwrap.dedent(
    """\
    mode: dimensional
    dimensional:
      tables:
        - name: patients
          role: dim
          source:
            grain: records
            kind: patient
          key: [patient_id]
          columns:
            - name: patient_id
              from: record_id
            - name: admission_date
              derived:
                timestamp: {source: sim_time, as: date}
            - name: admitted_at
              derived:
                timestamp: {source: sim_time, as: timestamptz}
            - name: valid_from
              derived:
                scd_window: {bound: valid_from, as: date}
            - name: wait
              derived:
                elapsed:
                  correlate_on: patient_id
                  other_where: {prop__step: arrival}
                  start_source: sim_time
                  end_source: sim_time
                  as: interval
            - name: birth_date
              derived:
                date_parse: {from: prop__dob, format: "%Y-%m-%d"}
    """
)

_SOURCE_YAML = textwrap.dedent(
    """\
    mode: source
    source:
      tables:
        - name: patients
          kind: patient
          render: {created_sim_time: date, last_mutation_sim_time: timestamptz}
          date_parse: {prop__dob: "%Y-%m-%d"}
      events:
        name: audit_log
        render: {event_sim_time: date}
        sources:
          - kind: patient
    """
)

_BASE_YAML = textwrap.dedent(
    """\
    mode: base
    base:
      render:
        - table: records__patient
          columns: {created_sim_time: date}
          date_parse: {prop__signup_date: "%Y-%m-%d"}
    """
)


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _load(config_dir: Path, name: str, yaml_text: str) -> ExportConfig:
    """Write `yaml_text` to `name`.yaml under `config_dir` and load it.

    Args:
        config_dir: Directory to write the YAML file into.
        name: File stem (also used as the printed label).
        yaml_text: The YAML document text.

    Returns:
        The validated ExportConfig.
    """
    path = config_dir / f"{name}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_export_config(path)


def show_elections(config_dir: Path) -> None:
    """Load and print the three mode configs, each exercising every election form."""
    dimensional = _load(config_dir, "dimensional", _DIMENSIONAL_YAML)
    assert dimensional.dimensional is not None
    table = dimensional.dimensional.tables[0]
    print("Dimensional elections (table 'patients'):")
    for col in table.columns:
        if col.derived is None:
            continue
        derived = col.derived
        if derived.timestamp is not None:
            print(f"  {col.name}: derived.timestamp.as = {derived.timestamp.as_!r}")
        elif derived.scd_window is not None:
            print(f"  {col.name}: derived.scd_window = {derived.scd_window!r}")
        elif derived.elapsed is not None:
            print(
                f"  {col.name}: derived.elapsed.unit={derived.elapsed.unit!r}"
                f" as={derived.elapsed.as_!r}"
            )
        elif derived.date_parse is not None:
            dp = derived.date_parse
            print(
                f"  {col.name}: derived.date_parse ="
                f" {{from: {dp.from_!r}, format: {dp.format!r}}}"
            )
    print()

    source = _load(config_dir, "source", _SOURCE_YAML)
    assert source.source is not None
    src_table = source.source.tables[0]
    print("Source elections (table 'patients'):")
    print(f"  render = {src_table.render}")
    print(f"  date_parse = {src_table.date_parse}")
    assert source.source.events is not None
    print(f"  events.render = {source.source.events.render}")
    print()

    base = _load(config_dir, "base", _BASE_YAML)
    assert base.base is not None and base.base.render is not None
    entry = base.base.render[0]
    print("Base elections (table 'records__patient'):")
    print(f"  columns = {entry.columns}")
    print(f"  date_parse = {entry.date_parse}")
    print()


def _print_refusal(number: int, label: str, exc: ValidationError) -> None:
    """Print one numbered refusal line: the label plus the validator's message.

    Args:
        number: The refusal's ordinal in the demo's printed list.
        label: A short description of the malformed input.
        exc: The ValidationError the malformed input raised.
    """
    message = str(exc).splitlines()[1].strip()
    print(f"  {number}. {label} -> {message}")


def show_refusals() -> None:
    """Demonstrate the five load-time refusals the grammar enforces."""
    refusals: list[str] = []

    print("Refusals:")

    try:
        ElapsedSpec(
            correlate_on="patient_id",
            other_where={"prop__step": "arrival"},
            start_source="sim_time",
            end_source="sim_time",
            unit="minutes",
            **{"as": "interval"},
        )
        refusals.append("elapsed: both unit and as set")
    except ValidationError as exc:
        _print_refusal(1, "elapsed both unit+as set", exc)

    try:
        ElapsedSpec(
            correlate_on="patient_id",
            other_where={"prop__step": "arrival"},
            start_source="sim_time",
            end_source="sim_time",
        )
        refusals.append("elapsed: neither unit nor as set")
    except ValidationError as exc:
        _print_refusal(2, "elapsed neither unit nor as", exc)

    try:
        DateParseSpec(**{"from": "prop__dob", "format": "%Y-%m"})
        refusals.append("date_parse: incomplete format (no day)")
    except ValidationError as exc:
        _print_refusal(3, "date_parse incomplete format", exc)

    try:
        DateParseSpec(**{"from": "prop__dob", "format": "%H:%M"})
        refusals.append("date_parse: %H directive")
    except ValidationError as exc:
        _print_refusal(4, "date_parse %H directive", exc)

    try:
        SourceTableDecl(
            name="patients",
            kind="patient",
            render={"created_sim_time": "date"},
            date_parse={"created_sim_time": "%Y-%m-%d"},
        )
        refusals.append("source table: column in both maps")
    except ValidationError as exc:
        _print_refusal(5, "column in both render and date_parse", exc)

    if refusals:
        raise _fail(f"the following configs should have been refused: {refusals}")
    print()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        show_elections(Path(tmp))
    show_refusals()

    print(
        "SUCCESS: every election form (dimensional as/scd_window/elapsed/date_parse,"
        " source render/date_parse maps, events render, base render list) parses,"
        " and every malformed election is refused at load time"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
