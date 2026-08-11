"""CLI entry point for fabulexa_forge.

Provides the `fabulexa-forge` command with the `validate`, `export`, `init`,
`stream`, `mixer`, and `corrupt` verbs.

Usage:
    fabulexa-forge validate <emit_dir>
    fabulexa-forge export <emit_dir> <config_path> <out> --fmt <csv|duckdb>
    fabulexa-forge init <emit_dir> [<out_path>]
    fabulexa-forge stream <emit_dir> <config_path> --fmt jsonl --sink <stdout|file>
        [--out <dir>] [--base-date <iso>] [--timezone <iana>]
    fabulexa-forge mixer <emit_dir> <config_path> --fmt jsonl|debezium
        --bootstrap-servers <addr> [--base-date <iso>] [--timezone <iana>]
        [--speed <n>] [--play|--paused] [--tick <s>]
        [--host <h>] [--port <p>]
    fabulexa-forge corrupt <emit_dir> --config <corrupt.yaml> --out <out_dir>

Exit codes:
    0  — success
    1  — error (usage, reader, config, or export failure)
    3  — drained (--next found no more windows to emit)
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.errors import ExporterError
from fabulexa_forge.reader import open_emit, validate
from fabulexa_forge.reader.errors import ReaderError

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.corrupters.state import CorruptReport
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.conformance import CheckResult
    from fabulexa_forge.reader.emit import Emit


def _print_check_result(result: "CheckResult") -> None:
    """Print a single check result line to stdout.

    Args:
        result: The check result to print.
    """
    status = "PASS" if result.passed else "FAIL"
    line = f"  {result.check}: {status}"
    if result.messages:
        line += f"  — {result.messages[0]}"
    if result.skips:
        line += f"  [skips: {len(result.skips)}]"
    print(line)


def _run_validate(emit_dir_str: str) -> int:
    """Open an emit and run conformance checks C1–C11, printing each result.

    Args:
        emit_dir_str: Path string to the emit directory.

    Returns:
        0 if all checks passed; 1 otherwise.
    """
    emit_dir = Path(emit_dir_str)
    try:
        with open_emit(emit_dir) as emit:
            report = validate(emit)
    except ReaderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for result in report.results:
        _print_check_result(result)

    if report.ok:
        print(f"PASS: all {len(report.results)} checks passed")
        return 0
    else:
        failing = [r.check for r in report.results if not r.passed]
        print(f"FAIL: {len(failing)} check(s) failed: {', '.join(failing)}")
        return 1


def _cmd_validate(args: list[str]) -> int:
    """Dispatch the validate subcommand.

    Args:
        args: Remaining arguments after the 'validate' verb.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="fabulexa-forge validate",
        description="Run C1-C14 conformance checks against an emit.",
    )
    parser.add_argument("emit_dir", type=Path)
    parsed = parser.parse_args(args)
    return _run_validate(str(parsed.emit_dir))


def _print_window_counts(window_label: str, counts: dict[str, int]) -> None:
    """Print per-table row counts prefixed by the window label.

    Args:
        window_label: The window's display label.
        counts: Mapping of table name to row count.
    """
    for table_name, row_count in counts.items():
        print(f"  [{window_label}] {table_name}: {row_count} rows")


def _print_full_counts(counts: dict[str, int]) -> None:
    """Print per-table row counts for a full (non-windowed) export.

    Args:
        counts: Mapping of table name to row count.
    """
    for table_name, row_count in counts.items():
        print(f"  {table_name}: {row_count} rows")


def _dispatch_export(
    emit: "Emit",
    config: "ExportConfig",
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    next_window: bool,
    range_from: str | None,
    range_to: str | None,
    notice_sink: "NoticeSink",
) -> int:
    """Run the full, next-window, or explicit-range export for any mode.

    The `--next` / `--from`/`--to` leaves call the incremental driver, which
    dispatches on `config.mode` internally (dimensional vs. source vs. base
    engine compile). The full-export leaf dispatches here on `config.mode`.

    Args:
        emit: The open emit.
        config: The validated export config.
        out: Output directory (csv) or .duckdb file path (duckdb).
        fmt: 'csv' or 'duckdb'.
        anchor: The resolved effective anchor, or None.
        next_window: When True, emit the next incremental window (--next).
        range_from: Inclusive start for an explicit range (--from), or None.
        range_to: Exclusive end for an explicit range (--to), or None.
        notice_sink: Receiver for plan notices.

    Returns:
        0 on a written window/range/full export (per-table counts printed,
        prefixed by the window label when windowed); 3 when --next finds the
        run drained.
    """
    if next_window:
        from fabulexa_forge.incremental.driver import export_incremental_next

        outcome = export_incremental_next(emit, config, out, fmt, anchor, notice_sink)
        if outcome.status == "drained":
            print("drained: no more windows to emit")
            return 3
        assert outcome.window is not None
        _print_window_counts(outcome.window.label, outcome.row_counts)
        return 0

    if range_from is not None and range_to is not None:
        from fabulexa_forge.incremental.driver import export_window
        from fabulexa_forge.incremental.windows import parse_range

        window = parse_range(range_from, range_to, anchor)
        range_counts = export_window(
            emit, config, out, fmt, anchor, window, None, notice_sink
        )
        _print_window_counts(window.label, range_counts)
        return 0

    if config.mode == "source":
        from fabulexa_forge.exporters.source.engine import export_source

        full_counts = export_source(emit, config, out, fmt, anchor, notice_sink)
    elif config.mode == "base":
        from fabulexa_forge.exporters.base.engine import export_base

        full_counts = export_base(emit, config, out, fmt, anchor, notice_sink)
    else:
        from fabulexa_forge.exporters.dimensional.engine import export_dimensional

        full_counts = export_dimensional(emit, config, out, fmt, anchor, notice_sink)
    _print_full_counts(full_counts)
    return 0


def cmd_export(
    emit_dir: Path,
    config_path: Path,
    out: Path,
    fmt: str,
    cli_base_date: datetime | None = None,
    cli_timezone: str | None = None,
    next_window: bool = False,
    range_from: str | None = None,
    range_to: str | None = None,
) -> int:
    """`fabulexa-forge export` — full, next-window, or explicit-range export.

    Dispatches on `config.mode` throughout: `dimensional` routes to the
    dimensional engine; `source` routes to the source engine; `base` routes
    to the base engine. All three support the full export and the `--next` /
    `--from`/`--to` incremental leaves identically — the incremental driver
    dispatches on `config.mode` internally (§ `_dispatch_export`).

    Args:
        emit_dir: Directory holding run.duckdb + base.json.
        config_path: Export-config YAML path.
        out: Output directory (csv) or .duckdb file path (duckdb).
        fmt: 'csv' or 'duckdb'. Any other value is rejected here with a usage
            message on stderr and a non-zero exit, before the emit opens.
        cli_base_date: `--base-date` parsed to a datetime, or None when unset.
        cli_timezone: `--timezone` IANA string, or None when unset.
        next_window: When True, emit the next incremental window (--next).
        range_from: Inclusive start for an explicit range (--from), or None.
        range_to: Exclusive end for an explicit range (--to), or None.

    Returns:
        0 on a written window/range/full export (per-table counts printed,
        prefixed by the window label when windowed); 3 when --next finds the
        run drained; 1 on any handled error.
    """
    from fabulexa_forge.exporters.notices import render_notice_stderr

    # Usage-error checks before the emit opens
    if next_window and (range_from is not None or range_to is not None):
        print(
            "Usage error: --next cannot be combined with --from/--to",
            file=sys.stderr,
        )
        return 1

    if (range_from is None) != (range_to is None):
        print(
            "Usage error: --from and --to must be provided together",
            file=sys.stderr,
        )
        return 1

    if fmt not in {"csv", "duckdb"}:
        print(
            f"Usage error: --fmt must be 'csv' or 'duckdb', got {fmt!r}",
            file=sys.stderr,
        )
        return 1

    from fabulexa_forge.config.loader import load_export_config

    try:
        config = load_export_config(config_path)

        with open_emit(emit_dir) as emit:
            sidecar_runtime = emit.sidecar.runtime()
            anchor = resolve_effective_anchor(
                sidecar_runtime, config.rebase, cli_base_date, cli_timezone
            )
            fmt_lit = cast(Literal["csv", "duckdb"], fmt)

            exit_code = _dispatch_export(
                emit,
                config,
                out,
                fmt_lit,
                anchor,
                next_window,
                range_from,
                range_to,
                render_notice_stderr,
            )

    except (ReaderError, ExporterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return exit_code


def cmd_init(
    emit_dir: Path, out_path: Path | None, mode: Literal["dimensional", "source"]
) -> int:
    """`fabulexa-forge init` — emit a commented candidate config.

    Dispatches on `mode`: 'dimensional' calls
    `exporters.dimensional.init.generate_init_config` (unchanged); 'source'
    calls `exporters.source.init.generate_source_init_config` (design doc §
    Interface Contracts — one state table per kind, one junction per
    membership table, the events stub, the keys proposal). Both are pure
    functions of (emit, code version); output goes to `out_path` or stdout
    exactly as today.

    Args:
        emit_dir: Directory holding run.duckdb + base.json.
        out_path: Where to write the candidate YAML; stdout when None.
        mode: Which mode's proposal engine to run.

    Returns:
        Process exit code (1 on ReaderError / ExporterError, else 0).
    """
    from fabulexa_forge.exporters.notices import render_notice_stderr

    try:
        with open_emit(emit_dir) as emit:
            if mode == "dimensional":
                from fabulexa_forge.exporters.dimensional.init import (
                    generate_init_config,
                )

                candidate = generate_init_config(emit, render_notice_stderr)
            else:
                from fabulexa_forge.exporters.source.init import (
                    generate_source_init_config,
                )

                candidate = generate_source_init_config(emit, render_notice_stderr)
    except (ReaderError, ExporterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if out_path is None:
        print(candidate, end="")
    else:
        out_path.write_text(candidate, encoding="utf-8")
        print(f"Wrote candidate config to {out_path}")
    return 0


def _cmd_export(args: list[str]) -> int:
    """Dispatch the export subcommand.

    Args:
        args: Remaining arguments after the 'export' verb.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="fabulexa-forge export",
        description="Run an export config against an emit.",
    )
    parser.add_argument("emit_dir", type=Path)
    parser.add_argument("config_path", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--fmt", required=True)
    parser.add_argument("--base-date", type=datetime.fromisoformat, default=None)
    parser.add_argument("--timezone", type=str, default=None)
    parser.add_argument(
        "--next", dest="next_window", action="store_true", default=False
    )
    parser.add_argument("--from", dest="range_from", type=str, default=None)
    parser.add_argument("--to", dest="range_to", type=str, default=None)

    parsed = parser.parse_args(args)

    return cmd_export(
        parsed.emit_dir,
        parsed.config_path,
        parsed.out,
        parsed.fmt,
        cli_base_date=parsed.base_date,
        cli_timezone=parsed.timezone,
        next_window=parsed.next_window,
        range_from=parsed.range_from,
        range_to=parsed.range_to,
    )


def cmd_stream(
    emit_dir: Path,
    config_path: Path,
    fmt: str,
    sink: str,
    out: Path | None,
    cli_base_date: datetime | None,
    cli_timezone: str | None,
    cli_speed: float | None = None,
    cli_idle_cap_seconds: float | None = None,
    cli_fast: bool = False,
    cli_bootstrap_servers: str | None = None,
) -> int:
    """`fabulexa-forge stream` — replay the base layer as a CDC event stream.

    As today, plus the kafka sink. After resolving the clock and anchor, when
    sink='kafka' it reads os.environ['FABEXPORT_KAFKA_BOOTSTRAP'] (or None), resolves
    the effective bootstrap-servers via resolve_bootstrap_servers(config.kafka,
    cli_bootstrap_servers, env) inside the (ReaderError, ExporterError) funnel, and
    passes it to stream_export; for stdout/file it passes bootstrap_servers=None.
    Flag-level usage checks (exit 1, before the funnel): --sink must be one of
    stdout|file|kafka; --out is forbidden for stdout and kafka and required for file.
    KafkaRequiresAnchor, KafkaBootstrapUnresolvable, KafkaClientUnavailable, and
    KafkaDeliveryError land in the existing funnel as exit 1.

    Args:
        emit_dir: Directory holding run.duckdb + base.json.
        config_path: Streaming-config YAML path.
        fmt: 'jsonl' or 'debezium'.
        sink: 'stdout', 'file', or 'kafka'.
        out: Output directory; required for file, forbidden for stdout and kafka.
        cli_base_date: --base-date parsed to a datetime, or None.
        cli_timezone: --timezone IANA string, or None.
        cli_speed: --speed value, or None.
        cli_idle_cap_seconds: --idle-cap value, or None.
        cli_fast: True when --fast was given.
        cli_bootstrap_servers: --bootstrap-servers value, or None.

    Returns:
        0 on a delivered stream; 1 on any handled usage/reader/config/export/delivery
        error.
    """
    import os
    from typing import Literal, cast

    from fabulexa_forge.config.loader import load_stream_config
    from fabulexa_forge.exporters.streaming.driver import stream_export
    from fabulexa_forge.exporters.streaming.pacer import resolve_clock

    # Flag-level usage checks — before the funnel
    if fmt not in ("jsonl", "debezium"):
        print(
            f"Usage error: --fmt must be 'jsonl|debezium', got {fmt!r}",
            file=sys.stderr,
        )
        return 1

    if sink not in ("stdout", "file", "kafka"):
        print(
            f"Usage error: --sink must be 'stdout|file|kafka', got {sink!r}",
            file=sys.stderr,
        )
        return 1

    if sink == "file" and out is None:
        print(
            "Usage error: --sink file requires --out <dir>",
            file=sys.stderr,
        )
        return 1

    if sink == "stdout" and out is not None:
        print(
            "Usage error: --sink stdout does not accept --out",
            file=sys.stderr,
        )
        return 1

    if sink == "kafka" and out is not None:
        print(
            "Usage error: --sink kafka does not accept --out",
            file=sys.stderr,
        )
        return 1

    # Clock flag-level usage checks
    if cli_fast and (cli_speed is not None or cli_idle_cap_seconds is not None):
        print(
            "Usage error: --fast cannot be combined with --speed or --idle-cap",
            file=sys.stderr,
        )
        return 1

    if cli_speed is not None and cli_speed <= 0:
        print(
            "Usage error: --speed must be > 0",
            file=sys.stderr,
        )
        return 1

    if cli_idle_cap_seconds is not None and cli_idle_cap_seconds <= 0:
        print(
            "Usage error: --idle-cap must be > 0",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_stream_config(config_path)

        clock = resolve_clock(config.clock, cli_speed, cli_idle_cap_seconds, cli_fast)

        bootstrap_servers: str | None = None
        if sink == "kafka":
            from fabulexa_forge.exporters.streaming.kafka_sink import (
                resolve_bootstrap_servers,
            )

            env_bootstrap = os.environ.get("FABEXPORT_KAFKA_BOOTSTRAP")
            bootstrap_servers = resolve_bootstrap_servers(
                config.kafka, cli_bootstrap_servers, env_bootstrap
            )

        with open_emit(emit_dir) as emit:
            sidecar_runtime = emit.sidecar.runtime()
            anchor = resolve_effective_anchor(
                sidecar_runtime, config.rebase, cli_base_date, cli_timezone
            )

            fmt_lit = cast(Literal["jsonl", "debezium"], fmt)
            sink_lit = cast(Literal["stdout", "file", "kafka"], sink)

            outcome = stream_export(
                emit,
                config,
                fmt_lit,
                sink_lit,
                out,
                anchor,
                clock=clock,
                bootstrap_servers=bootstrap_servers,
            )

    except (ReaderError, ExporterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for topic, count in outcome.events_per_topic.items():
        print(f"  {topic}: {count} events")
    return 0


def _cmd_stream(args: list[str]) -> int:
    """Dispatch the stream subcommand.

    Args:
        args: Remaining arguments after the 'stream' verb.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="fabulexa-forge stream",
        description="Replay the base layer as a CDC event stream.",
    )
    parser.add_argument("emit_dir", type=Path)
    parser.add_argument("config_path", type=Path)
    parser.add_argument("--fmt", required=True)
    parser.add_argument("--sink", required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--base-date", type=datetime.fromisoformat, default=None)
    parser.add_argument("--timezone", type=str, default=None)
    parser.add_argument("--speed", type=float, default=None)
    parser.add_argument("--idle-cap", dest="idle_cap", type=float, default=None)
    parser.add_argument("--fast", action="store_true", default=False)
    parser.add_argument(
        "--bootstrap-servers", dest="bootstrap_servers", type=str, default=None
    )

    parsed = parser.parse_args(args)

    return cmd_stream(
        parsed.emit_dir,
        parsed.config_path,
        parsed.fmt,
        parsed.sink,
        parsed.out,
        cli_base_date=parsed.base_date,
        cli_timezone=parsed.timezone,
        cli_speed=parsed.speed,
        cli_idle_cap_seconds=parsed.idle_cap,
        cli_fast=parsed.fast,
        cli_bootstrap_servers=parsed.bootstrap_servers,
    )


def _parse_join_flag(value: str) -> tuple[str, str] | None:
    """Parse a ``--join fact:dim`` flag value into a ``(fact_topic, dim_topic)`` pair.

    Args:
        value: The raw ``--join`` flag value, expected to be ``"fact:dim"``.

    Returns:
        A ``(fact_topic, dim_topic)`` pair, or ``None`` if the value does not
        contain exactly one ``:`` separator with non-empty parts on both sides.
    """
    parts = value.split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return (parts[0], parts[1])


def cmd_mixer(
    emit_dir: Path,
    config_path: Path,
    fmt: str,
    cli_base_date: datetime | None,
    cli_timezone: str | None,
    cli_speed: float,
    cli_playing: bool,
    cli_tick_seconds: float,
    cli_bootstrap_servers: str | None,
    host: str,
    port: int,
    cli_consumer: bool = False,
    cli_windows: tuple[int, ...] = (),
    cli_joins: tuple[tuple[str, str], ...] = (),
    cli_consumer_group: str | None = None,
    cli_consumer_offset: str = "earliest",
) -> int:
    """`fabulexa-forge mixer` — replay the base layer as a live, operator-mixable
    Kafka feed.

    Runs the synchronous setup phase (load config; resolve bootstrap; open_emit;
    resolve anchor and enforce KafkaRequiresAnchor; build the render closure;
    resolve record_roles from the emit sidecar and build the topic set;
    seed_mixer_run; close the emit) inside the (ReaderError, ExporterError)
    funnel, then asyncio.run(serve_mixer(...)).
    Flag-level usage checks (exit 1, before the funnel): --fmt must be jsonl|debezium;
    --speed in [0.1, 1000]; --tick > 0; --port in [1, 65535]; --window / --join
    require --consumer; --window must be > 0; --consumer-offset must be
    earliest|latest. The launch Transport is built from cli_speed / cli_playing.
    Kafka is the sole sink (no --sink choice).

    When cli_consumer is True: after seed_mixer_run, computes the non-empty topic
    set from the producer buffers, parses cli_windows / cli_joins into specs,
    calls seed_consumer_run() to build MixerRunState.consumer, builds a
    ConsumerLaunch (using cli_consumer_group or a fresh unique id, plus
    cli_consumer_offset), and passes it to serve_mixer.

    Args:
        emit_dir: Path to the emit directory (run.duckdb + base.json).
        config_path: Path to the stream config YAML.
        fmt: Output format, must be 'jsonl' or 'debezium'.
        cli_base_date: Optional base date override for anchor resolution.
        cli_timezone: Optional timezone override for anchor resolution.
        cli_speed: Event-time advance per real-time unit; must be in [0.1, 1000].
        cli_playing: Whether the mixer launches in playing state.
        cli_tick_seconds: Loop tick quantum in real seconds; must be > 0.
        cli_bootstrap_servers: Kafka bootstrap servers override.
        host: Host address for uvicorn to bind.
        port: Port for uvicorn to bind; must be in [1, 65535].
        cli_consumer: Enable the consumer instrument (default False).
        cli_windows: Tumbling window sizes in event-time ms (default empty).
        cli_joins: Fact/dimension topic pairings as (fact, dim) tuples (default empty).
        cli_consumer_group: Kafka consumer group id; a fresh unique id if None.
        cli_consumer_offset: Initial offset reset policy; must be
            'earliest' or 'latest'.

    Returns:
        0 on a clean operator-ended session; 1 on any handled usage / reader / config /
        anchor / delivery / serving error.
    """
    import asyncio
    import os
    from typing import Literal, cast

    from fabulexa_forge.config.loader import load_stream_config
    from fabulexa_forge.errors import ExportError
    from fabulexa_forge.exporters.streaming.driver import build_kafka_render_value
    from fabulexa_forge.exporters.streaming.engine import build_topic_set
    from fabulexa_forge.exporters.streaming.kafka_sink import resolve_bootstrap_servers
    from fabulexa_forge.exporters.streaming.mixer.scheduler import (
        Transport,
        seed_mixer_run,
    )
    from fabulexa_forge.exporters.streaming.mixer.serve import serve_mixer

    # Flag-level usage checks — before the funnel
    if fmt not in ("jsonl", "debezium"):
        print(
            f"Usage error: --fmt must be 'jsonl|debezium', got {fmt!r}",
            file=sys.stderr,
        )
        return 1

    if not (0.1 <= cli_speed <= 1000):
        print(
            f"Usage error: --speed must be in [0.1, 1000], got {cli_speed!r}",
            file=sys.stderr,
        )
        return 1

    if cli_tick_seconds <= 0:
        print(
            f"Usage error: --tick must be > 0, got {cli_tick_seconds!r}",
            file=sys.stderr,
        )
        return 1

    if not (1 <= port <= 65535):
        print(
            f"Usage error: --port must be in [1, 65535], got {port!r}",
            file=sys.stderr,
        )
        return 1

    if not cli_consumer and cli_windows:
        print(
            "Usage error: --window requires --consumer",
            file=sys.stderr,
        )
        return 1

    if not cli_consumer and cli_joins:
        print(
            "Usage error: --join requires --consumer",
            file=sys.stderr,
        )
        return 1

    for w in cli_windows:
        if w <= 0:
            print(
                f"Usage error: --window must be > 0, got {w!r}",
                file=sys.stderr,
            )
            return 1

    if cli_consumer_offset not in ("earliest", "latest"):
        print(
            f"Usage error: --consumer-offset must be 'earliest' or 'latest',"
            f" got {cli_consumer_offset!r}",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_stream_config(config_path)

        env_bootstrap = os.environ.get("FABEXPORT_KAFKA_BOOTSTRAP")
        bootstrap_servers = resolve_bootstrap_servers(
            config.kafka, cli_bootstrap_servers, env_bootstrap
        )

        with open_emit(emit_dir) as emit:
            sidecar_runtime = emit.sidecar.runtime()
            anchor = resolve_effective_anchor(
                sidecar_runtime, config.rebase, cli_base_date, cli_timezone
            )

            if anchor is None:
                raise ExportError(
                    "sink 'kafka' requires a resolved effective anchor"
                    " (set rebase.base_date / rebase.timezone, or rely on the sidecar"
                    " runtime anchor); the Kafka record timestamp must be"
                    " epoch-milliseconds"
                )

            topic_set = build_topic_set(config)

            fmt_lit = cast(Literal["jsonl", "debezium"], fmt)
            render_value = build_kafka_render_value(
                emit, config, fmt_lit, anchor, topic_set
            )

            launch_transport = Transport(playing=cli_playing, speed=cli_speed)

            buffers, control, frontier = seed_mixer_run(
                emit, config, anchor, emit.sidecar, launch_transport
            )

        from fabulexa_forge.exporters.streaming.mixer.run_state import MixerRunState

        state = MixerRunState(
            control=control,
            frontier=frontier,
            buffers=buffers,
            anchor=anchor,
            monotonic=__import__("time").monotonic,
            play_origin_monotonic=None,
        )

        consumer_launch = None
        if cli_consumer:
            import uuid

            from fabulexa_forge.exporters.streaming.mixer.consumer import (
                ConsumerLaunch,
                JoinSpec,
                WindowSpec,
                seed_consumer_run,
            )

            nonempty_topics = tuple(t for t in topic_set if buffers[t])
            windows = tuple(WindowSpec(size_ms=ms) for ms in cli_windows)
            joins = tuple(
                JoinSpec(fact_topic=f, dimension_topic=d) for f, d in cli_joins
            )
            content = config.content
            consumer_run_state = seed_consumer_run(
                topic_set=topic_set,
                content=content,
                nonempty_topics=nonempty_topics,
                windows=windows,
                joins=joins,
            )
            state.consumer = consumer_run_state

            group_id = (
                cli_consumer_group
                if cli_consumer_group is not None
                else str(uuid.uuid4())
            )
            offset_reset = cast(Literal["earliest", "latest"], cli_consumer_offset)
            consumer_launch = ConsumerLaunch(
                group_id=group_id, offset_reset=offset_reset
            )

    except (ReaderError, ExporterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        asyncio.run(
            serve_mixer(
                state=state,
                render_value=render_value,
                bootstrap_servers=bootstrap_servers,
                topic_set=topic_set,
                tick_seconds=cli_tick_seconds,
                host=host,
                port=port,
                consumer_launch=consumer_launch,
            )
        )
    except (ReaderError, ExporterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


def _cmd_mixer(args: list[str]) -> int:
    """Dispatch the mixer subcommand.

    Args:
        args: Remaining arguments after the 'mixer' verb.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="fabulexa-forge mixer",
        description="Replay the base layer as a live, operator-mixable Kafka feed.",
    )
    parser.add_argument("emit_dir", type=Path)
    parser.add_argument("config_path", type=Path)
    parser.add_argument("--fmt", required=True)
    parser.add_argument("--base-date", type=datetime.fromisoformat, default=None)
    parser.add_argument("--timezone", type=str, default=None)
    parser.add_argument("--speed", type=float, default=1.0)
    play_group = parser.add_mutually_exclusive_group()
    play_group.add_argument(
        "--play", dest="playing", action="store_true", default=False
    )
    play_group.add_argument("--paused", dest="playing", action="store_false")
    parser.add_argument("--tick", dest="tick_seconds", type=float, default=0.05)
    parser.add_argument(
        "--bootstrap-servers", dest="bootstrap_servers", type=str, default=None
    )
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--consumer", action="store_true", default=False)
    parser.add_argument(
        "--window", dest="windows", type=int, action="append", default=[]
    )
    parser.add_argument("--join", dest="joins", type=str, action="append", default=[])
    parser.add_argument(
        "--consumer-group", dest="consumer_group", type=str, default=None
    )
    parser.add_argument(
        "--consumer-offset", dest="consumer_offset", type=str, default="earliest"
    )

    parsed = parser.parse_args(args)

    # Validate --join format: each must be "fact:dim"
    parsed_joins: list[tuple[str, str]] = []
    for raw in parsed.joins:
        pair = _parse_join_flag(raw)
        if pair is None:
            print(
                f"Usage error: --join must be 'fact:dim', got {raw!r}",
                file=sys.stderr,
            )
            return 1
        parsed_joins.append(pair)

    return cmd_mixer(
        parsed.emit_dir,
        parsed.config_path,
        parsed.fmt,
        cli_base_date=parsed.base_date,
        cli_timezone=parsed.timezone,
        cli_speed=parsed.speed,
        cli_playing=parsed.playing,
        cli_tick_seconds=parsed.tick_seconds,
        cli_bootstrap_servers=parsed.bootstrap_servers,
        host=parsed.host,
        port=parsed.port,
        cli_consumer=parsed.consumer,
        cli_windows=tuple(parsed.windows),
        cli_joins=tuple(parsed_joins),
        cli_consumer_group=parsed.consumer_group,
        cli_consumer_offset=parsed.consumer_offset,
    )


def _cmd_init(args: list[str]) -> int:
    """Dispatch the init subcommand.

    Args:
        args: Remaining arguments after the 'init' verb.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="fabulexa-forge init",
        description="Propose a candidate config from the sidecar.",
    )
    parser.add_argument("emit_dir", type=Path)
    parser.add_argument("out_path", type=Path, nargs="?", default=None)
    parser.add_argument(
        "--mode", choices=("dimensional", "source"), default="dimensional"
    )
    parsed = parser.parse_args(args)
    mode = cast(Literal["dimensional", "source"], parsed.mode)
    return cmd_init(parsed.emit_dir, parsed.out_path, mode)


def _print_corrupt_report(report: "CorruptReport") -> None:
    """Print one summary line per applied operation.

    Args:
        report: The per-operation corrupt report, in application order.
    """
    for outcome in report.outcomes:
        print(
            f"  {outcome.kind:<16} tables={','.join(outcome.tables)}"
            f" units_selected={outcome.units_selected}"
            f" units_affected={outcome.units_affected}"
        )


def cmd_corrupt(emit_dir: Path, config_path: Path, out_dir: Path) -> int:
    """`fabulexa-forge corrupt` — apply a corrupter config to an emit.

    Opens the source emit, loads the corrupter config, and runs `corrupt_emit`,
    which writes a structurally-conformant, semantically-broken `run.duckdb` +
    `base.json` plus the deterministic `defects.json` ground-truth manifest into
    `out_dir` — every run writes the manifest; there is no flag to suppress it.

    Args:
        emit_dir: Directory holding run.duckdb + base.json (the source emit).
        config_path: Corrupter-config YAML path.
        out_dir: Destination directory for run.duckdb + base.json + defects.json.

    Returns:
        0 on success (the per-operation report printed to stdout); 1 on any
        handled error (ConfigError, ReaderError, or CorruptError — all under
        the (ReaderError, ExporterError) funnel cmd_export uses).
    """
    from fabulexa_forge.config.loader import load_corrupt_config
    from fabulexa_forge.corrupters.engine import corrupt_emit

    try:
        config = load_corrupt_config(config_path)
        with open_emit(emit_dir) as emit:
            report = corrupt_emit(emit, config, out_dir)
    except (ReaderError, ExporterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _print_corrupt_report(report)
    print(f"Wrote corrupted emit + defects.json to {out_dir}")
    return 0


def _cmd_corrupt(args: list[str]) -> int:
    """Dispatch the corrupt subcommand.

    Args:
        args: Remaining arguments after the 'corrupt' verb.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="fabulexa-forge corrupt",
        description="Apply a corrupter config to an emit.",
    )
    parser.add_argument("emit_dir", type=Path)
    parser.add_argument("--config", dest="config_path", type=Path, required=True)
    parser.add_argument("--out", dest="out_dir", type=Path, required=True)

    parsed = parser.parse_args(args)

    return cmd_corrupt(parsed.emit_dir, parsed.config_path, parsed.out_dir)


@dataclass(frozen=True)
class Verb:
    """A dispatchable fabulexa-forge verb.

    Attributes:
        name: The verb as typed on the command line.
        summary: One-line description, rendered in the top-level verb table.
        handler: Parses the verb's remaining argv and returns its exit code.
            Lets argparse's SystemExit escape; dispatch translates it.
    """

    name: str
    summary: str
    handler: Callable[[list[str]], int]


VERBS: Final[tuple[Verb, ...]] = (
    Verb("validate", "Run C1-C14 conformance checks against an emit.", _cmd_validate),
    Verb("export", "Run an export config against an emit.", _cmd_export),
    Verb(
        "init",
        "Propose a candidate dimensional config from the sidecar.",
        _cmd_init,
    ),
    Verb("stream", "Replay the base layer as a CDC event stream.", _cmd_stream),
    Verb(
        "mixer",
        "Replay the base layer as a live, operator-mixable Kafka feed.",
        _cmd_mixer,
    ),
    Verb("corrupt", "Apply a corrupter config to an emit.", _cmd_corrupt),
)
"""The verb registry. The sole source of the verb list -- never a literal."""


def render_usage() -> str:
    """Render the top-level usage block: usage line plus the verb table.

    Returns:
        Multi-line text without a trailing newline, derived from VERBS.
    """
    lines = ["Usage: fabulexa-forge <verb> [args...]", "", "Verbs:"]
    width = max(len(v.name) for v in VERBS)
    for v in VERBS:
        lines.append(f"  {v.name:<{width}}  {v.summary}")
    return "\n".join(lines)


def dispatch(verb: Verb, rest: list[str]) -> int:
    """Run a verb handler, translating argparse's SystemExit into an exit code.

    argparse raises SystemExit(0) after writing --help to stdout, and
    SystemExit(2) after writing a usage error to stderr. Both are control flow,
    not failure; this is the one place that distinction is made.

    Args:
        verb: The registry entry to run.
        rest: Argv after the verb token.

    Returns:
        0 if the handler succeeded or argparse printed help; 2 on a usage error;
        the handler's own non-zero code otherwise.
    """
    try:
        return verb.handler(rest)
    except SystemExit as e:
        if e.code is None or e.code == 0:
            return 0
        return 2


def main(argv: list[str] | None = None) -> int:
    """Entry point for the fabulexa-forge CLI.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 on success, non-zero on failure).
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print(render_usage(), file=sys.stderr)
        return 1

    if argv[0] in {"--help", "-h", "help"}:
        print(render_usage())
        return 0

    verb_name = argv[0]
    rest = argv[1:]

    by_name = {v.name: v for v in VERBS}
    v = by_name.get(verb_name)
    if v is None:
        print(f"Unknown verb: {verb_name!r}", file=sys.stderr)
        print(render_usage(), file=sys.stderr)
        return 1

    return dispatch(v, rest)
