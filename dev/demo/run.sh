#!/usr/bin/env bash
# FabulMixer demo launcher.
#
# Usage: dev/demo/run.sh <example>
#
# Resolves the triple (bundle, stream.yaml, demo.yaml) from
# docs/examples/<example>/, renders consumer flags from demo.yaml, and
# exec-replaces itself with `uv run fabulexa-forge mixer ...`.
#
# Environment:
#   BOOTSTRAP  Kafka bootstrap servers (default: localhost:9092).
#   DRY_RUN    When non-empty, print the resolved command and exit 0 without
#              executing (no broker needed).
#
# Exit codes:
#   0  Success (or DRY_RUN print).
#   1  Missing bundle / config / demo.yaml (named error to stderr).
#   2  Wrong argument count (usage to stderr).
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $(basename "$0") <example>" >&2
    exit 2
fi

EXAMPLE="$1"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

BUNDLE="${REPO_ROOT}/docs/examples/${EXAMPLE}/bundle"
CONFIG="${REPO_ROOT}/docs/examples/${EXAMPLE}/stream.yaml"
DEMO="${REPO_ROOT}/docs/examples/${EXAMPLE}/demo.yaml"

if [ ! -d "$BUNDLE" ]; then
    echo "error: bundle directory not found: ${BUNDLE}" >&2
    exit 1
fi
if [ ! -f "$CONFIG" ]; then
    echo "error: config file not found: ${CONFIG}" >&2
    exit 1
fi
if [ ! -f "$DEMO" ]; then
    echo "error: demo.yaml not found: ${DEMO}" >&2
    exit 1
fi

BOOTSTRAP="${BOOTSTRAP:-localhost:9092}"

# Parse demo.yaml in canonical block form (line-oriented).
# Collects flags in demo.yaml order: windows[], joins[], consumer_group, consumer_offset.
EXTRA_FLAGS=()
IN_WINDOWS=0
IN_JOINS=0
CURRENT_FACT=""

while IFS= read -r line; do
    # Strip inline comments, then skip blank lines.
    line="${line%%#*}"
    [[ -z "${line//[[:space:]]/}" ]] && continue

    # Top-level key (no leading whitespace).
    if [[ "$line" =~ ^([a-z_]+):[[:space:]]*(.*) ]]; then
        key="${BASH_REMATCH[1]}"
        val="${BASH_REMATCH[2]}"
        val="${val%"${val##*[! ]}"}"   # rtrim trailing whitespace
        IN_WINDOWS=0
        IN_JOINS=0
        CURRENT_FACT=""
        case "$key" in
            windows)         IN_WINDOWS=1 ;;
            joins)           IN_JOINS=1 ;;
            consumer_group)  EXTRA_FLAGS+=("--consumer-group" "$val") ;;
            consumer_offset) EXTRA_FLAGS+=("--consumer-offset" "$val") ;;
        esac
        continue
    fi

    # Window list item.
    if [ "$IN_WINDOWS" -eq 1 ] && [[ "$line" =~ ^[[:space:]]*-[[:space:]]+([0-9]+) ]]; then
        EXTRA_FLAGS+=("--window" "${BASH_REMATCH[1]}")
        continue
    fi

    # Join list items: `  - fact: <name>` then `    dim: <name>`.
    if [ "$IN_JOINS" -eq 1 ]; then
        if [[ "$line" =~ ^[[:space:]]*-[[:space:]]+fact:[[:space:]]+(.*) ]]; then
            CURRENT_FACT="${BASH_REMATCH[1]}"
            CURRENT_FACT="${CURRENT_FACT%"${CURRENT_FACT##*[! ]}"}"
            continue
        fi
        if [[ "$line" =~ ^[[:space:]]+dim:[[:space:]]+(.*) ]]; then
            dim="${BASH_REMATCH[1]}"
            dim="${dim%"${dim##*[! ]}"}"
            if [ -n "$CURRENT_FACT" ]; then
                EXTRA_FLAGS+=("--join" "${CURRENT_FACT}:${dim}")
                CURRENT_FACT=""
            fi
            continue
        fi
    fi
done < "$DEMO"

CMD_PARTS=("uv" "run" "fabulexa-forge" "mixer" "$BUNDLE" "$CONFIG" "--fmt" "jsonl" "--bootstrap-servers" "$BOOTSTRAP" "--consumer")
if [ "${#EXTRA_FLAGS[@]}" -gt 0 ]; then
    CMD_PARTS+=("${EXTRA_FLAGS[@]}")
fi

if [ -n "${DRY_RUN:-}" ]; then
    echo "${CMD_PARTS[*]}"
    exit 0
fi

exec "${CMD_PARTS[@]}"
