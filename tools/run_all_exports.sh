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
# Configs run in parallel, JOBS at a time (default 4 — the big bundles are
# memory-bound, not CPU-bound). Each job's output is captured to its own log and
# printed in declaration order once every job has finished.
#
# Usage:
#   tools/run_all_exports.sh [example ...]      # default: all examples
#   FMT=csv tools/run_all_exports.sh nhs        # override output format
#   JOBS=8 tools/run_all_exports.sh             # raise the parallelism cap
#   tools/run_all_exports.sh --no-stream        # skip the streaming replay
#
# Exit code: number of configs that failed (0 = all clean).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FMT="${FMT:-duckdb}"          # duckdb | csv  — export + stream output format
JOBS="${JOBS:-4}"             # max configs exporting at once
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

LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT
REPORT=()   # ordered report lines: "H <example>" | "S <text>" | "J <n> <label>" (job n's log)
NJOBS=0

run() {  # example, label, command...   — launches in the background, at most JOBS at once
  local ex="$1" label="$2"; shift 2
  while (( $(jobs -rp | wc -l) >= JOBS )); do wait -n; done
  local n=$((NJOBS++))
  REPORT+=("J ${n} ${label}")
  echo ">> ${ex}/${label}"
  ( "$@" > "${LOG_DIR}/${n}.log" 2>&1; echo $? > "${LOG_DIR}/${n}.status" ) &
}

for ex in "${EXAMPLES[@]}"; do
  bundle="docs/examples/${ex}/bundle"
  if [[ ! -f "${bundle}/base.json" ]]; then
    REPORT+=("S == ${ex}: no bundle, skipping =="); continue
  fi
  REPORT+=("H ${ex}")

  out_root="docs/examples/${ex}/exports"

  for mode in base source dimensional; do
    cfg="docs/examples/${ex}/${mode}.yaml"
    [[ -f "$cfg" ]] || { REPORT+=("S ---- ${mode}: (no config)"); continue; }
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
    run "$ex" "$mode" uv run fabulexa-forge export "$bundle" "$cfg" "$out" --fmt "$FMT"
  done

  if [[ "$RUN_STREAM" -eq 1 && -f "docs/examples/${ex}/stream.yaml" ]]; then
    out="${out_root}/stream"
    rm -rf "$out"; mkdir -p "$out"
    run "$ex" "stream" uv run fabulexa-forge stream "$bundle" "docs/examples/${ex}/stream.yaml" \
        --fmt jsonl --sink file --out "$out" --fast
  fi
done

wait

FAILURES=0
RAN=0
for entry in "${REPORT[@]}"; do
  case "$entry" in
    "H "*) echo "======================== ${entry#H } ========================" ;;
    "S "*) echo "${entry#S }" ;;
    "J "*)
      n="${entry#J }"; n="${n%% *}"; label="${entry#J * }"
      echo "---- ${label}"
      grep -v 'VIRTUAL_ENV' "${LOG_DIR}/${n}.log" | sed 's/^/     /'
      status="$(cat "${LOG_DIR}/${n}.status")"
      if [[ "$status" == 0 ]]; then
        RAN=$((RAN + 1))
      else
        echo "     FAILED (exit ${status})"
        FAILURES=$((FAILURES + 1))
      fi
      ;;
  esac
done

echo "========================================================"
echo "ran ${RAN} config(s), ${FAILURES} failure(s); output under docs/examples/*/exports/"
exit "$FAILURES"
