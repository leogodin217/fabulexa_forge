#!/usr/bin/env bash
# Run every per-example export config against its bundle.
# ============================================================
# For each docs/examples/<name>/ that carries a bundle (bundle/base.json), runs
# whichever mode configs are present:
#
#   base.yaml         -> fabulexa-forge export ... --fmt <FMT>   (mode: base)
#   source.yaml       -> fabulexa-forge export ... --fmt <FMT>   (mode: source)
#   dimensional.yaml  -> fabulexa-forge export ... --fmt <FMT>   (mode: dimensional)
#   stream.yaml       -> fabulexa-forge stream ... --sink file   (CDC replay, dry run)
#
# Configs are committed to git; the datasets this writes are NOT — everything
# lands under docs/examples/<name>/exports/<mode>/, which is gitignored
# (docs/examples/*/exports/).
#
# Usage:
#   tools/run_all_exports.sh [example ...]      # default: all examples
#   FMT=csv tools/run_all_exports.sh nhs        # override output format
#   tools/run_all_exports.sh --no-stream        # skip the streaming replay
#
# Exit code: number of configs that failed (0 = all clean).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FMT="${FMT:-duckdb}"          # duckdb | csv  — export + stream output format
RUN_STREAM=1
EXAMPLES=()

for arg in "$@"; do
  case "$arg" in
    --no-stream) RUN_STREAM=0 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) EXAMPLES+=("$arg") ;;
  esac
done

# Default: every example dir that has a bundle.
if [[ ${#EXAMPLES[@]} -eq 0 ]]; then
  for d in docs/examples/*/; do
    [[ -f "${d}bundle/base.json" ]] && EXAMPLES+=("$(basename "$d")")
  done
fi

FAILURES=0
RAN=0

run() {  # label, command...
  local label="$1"; shift
  echo "---- ${label}"
  if "$@" 2>&1 | grep -v 'VIRTUAL_ENV' | sed 's/^/     /'; then
    RAN=$((RAN + 1))
  else
    echo "     FAILED (exit ${PIPESTATUS[0]})"
    FAILURES=$((FAILURES + 1))
  fi
}

for ex in "${EXAMPLES[@]}"; do
  bundle="docs/examples/${ex}/bundle"
  if [[ ! -f "${bundle}/base.json" ]]; then
    echo "== ${ex}: no bundle, skipping =="; continue
  fi
  echo "======================== ${ex} ========================"

  out_root="docs/examples/${ex}/exports"

  for mode in base source dimensional; do
    cfg="docs/examples/${ex}/${mode}.yaml"
    [[ -f "$cfg" ]] || { echo "---- ${mode}: (no config)"; continue; }
    mkdir -p "$out_root"
    if [[ "$FMT" == "duckdb" ]]; then
      # DuckDB output is a single file at the out path — must not pre-exist as a dir.
      out="${out_root}/${mode}.duckdb"
      rm -rf "$out"
    else
      # CSV output is a directory of per-table files.
      out="${out_root}/${mode}"
      rm -rf "$out"; mkdir -p "$out"
    fi
    run "$mode" uv run fabulexa-forge export "$bundle" "$cfg" "$out" --fmt "$FMT"
  done

  if [[ "$RUN_STREAM" -eq 1 && -f "docs/examples/${ex}/stream.yaml" ]]; then
    out="${out_root}/stream"
    rm -rf "$out"; mkdir -p "$out"
    run "stream" uv run fabulexa-forge stream "$bundle" "docs/examples/${ex}/stream.yaml" \
        --fmt jsonl --sink file --out "$out" --fast
  fi
done

echo "========================================================"
echo "ran ${RAN} config(s), ${FAILURES} failure(s); output under docs/examples/*/exports/"
exit "$FAILURES"
