#!/usr/bin/env bash
# Runs gates 1-5 (scd2_windows, refs_resolve, trace_domain, zero_rows,
# determinism) across every example + mode dataset under
# docs/examples/*/exports/ -- or, with --dataset, one dataset at any path --
# prints a per-dataset status matrix, and exits nonzero if any gate failed.
#
# A gate cell is one of:
#   PASS      the invariant holds
#   FAIL      the invariant is violated -- a real data defect
#   UNGATED   the dataset could not be read (gate exit 3): locked by a
#             concurrent writer, mid-write, or corrupt
#   UNSTABLE  the dataset changed underneath the gates, so whatever they
#             reported (PASS included) is not trustworthy
#   STALE     the dataset predates the bundle or config it was built from --
#             gating it would judge an artifact nobody ships
#   MISSING   no dataset to gate
#
# The last three are NOT data defects and are counted separately. This
# distinction is the point: a FAIL on correct data trains people to ignore
# the gate.
#
# The zero-row gate (tools/qa/zero_rows.py) fails on any empty table unless
# the table is on the example's expected-empty allowlist,
# docs/examples/<example>/expected-empty.yaml (mode -> {table: reason}; see the
# gate's docstring). An example without that file allowlists nothing.
#
# Usage:
#   tools/qa/run_gates.sh              # all examples
#   tools/qa/run_gates.sh nhs retail   # subset
#
#   # One dataset at an arbitrary path (how the fleet QA round gates a staged
#   # pack): the config and bundle it was exported from are given explicitly.
#   # --expected-empty names the allowlist (default: none); --label names the
#   # matrix row (default: <parent-dir>/<dataset-stem>). Same cells, same exit
#   # codes; the determinism gate re-exports the given config from the given
#   # bundle.
#   tools/qa/run_gates.sh --dataset PATH.duckdb --config CONFIG.yaml --bundle DIR \
#       [--expected-empty FILE] [--label NAME]
#
# Exit codes:
#   0    every dataset gated cleanly and passed
#   N>0  N failing gate invocations (data defects)
#   3    no data defects, but one or more datasets could not be gated --
#        always read the final two count lines rather than the code alone
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

GATE_TMP="$(mktemp -d)"
if [[ -z "${QA_KEEP_TMP:-}" ]]; then
  trap 'rm -rf "$GATE_TMP"' EXIT
fi

EXAMPLES=()
ONE_DATASET=""
ONE_CONFIG=""
ONE_BUNDLE=""
ONE_ALLOW=""
ONE_LABEL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset)        ONE_DATASET="$2"; shift 2 ;;
    --config)         ONE_CONFIG="$2";  shift 2 ;;
    --bundle)         ONE_BUNDLE="$2";  shift 2 ;;
    --expected-empty) ONE_ALLOW="$2";   shift 2 ;;
    --label)          ONE_LABEL="$2";   shift 2 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) EXAMPLES+=("$1"); shift ;;
  esac
done

if [[ -n "$ONE_DATASET" || -n "$ONE_CONFIG" || -n "$ONE_BUNDLE" ]]; then
  if [[ -z "$ONE_DATASET" || -z "$ONE_CONFIG" || -z "$ONE_BUNDLE" ]]; then
    echo "--dataset, --config and --bundle must be given together" >&2; exit 2
  fi
  if [[ ${#EXAMPLES[@]} -gt 0 ]]; then
    echo "example names and --dataset are mutually exclusive" >&2; exit 2
  fi
  [[ -f "$ONE_CONFIG" ]] || { echo "no config at ${ONE_CONFIG}" >&2; exit 2; }
  [[ -f "${ONE_BUNDLE}/base.json" ]] || { echo "no bundle at ${ONE_BUNDLE}/base.json" >&2; exit 2; }
  if [[ -z "$ONE_LABEL" ]]; then
    stem="$(basename "$ONE_DATASET")"; stem="${stem%.*}"
    ONE_LABEL="$(basename "$(cd "$(dirname "$ONE_DATASET")" 2>/dev/null && pwd || dirname "$ONE_DATASET")")/${stem}"
  fi
elif [[ ${#EXAMPLES[@]} -eq 0 ]]; then
  for d in docs/examples/*/; do
    [[ -f "${d}bundle/base.json" ]] && EXAMPLES+=("$(basename "$d")")
  done
fi

declare -A RESULT
ROWS=()
DET_ROWS=()
UNGATED_NOTES=()
TOTAL_FAILURES=0
TOTAL_UNGATED=0

#: Gate exit code meaning "could not read the dataset" (see the gate scripts).
UNGATED_EXIT=3

record() {  # row, gate, exit_code
  local key="${1}/${2}"
  if [[ "$3" -eq 0 ]]; then
    RESULT["$key"]="PASS"
  elif [[ "$3" -eq "$UNGATED_EXIT" ]]; then
    RESULT["$key"]="UNGATED"
    TOTAL_UNGATED=$((TOTAL_UNGATED + 1))
  else
    RESULT["$key"]="FAIL"
    TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
  fi
}

ungate_row() {  # row, status, reason
  local gate
  for gate in scd2 refs trace zero; do
    RESULT["${1}/${gate}"]="$2"
    TOTAL_UNGATED=$((TOTAL_UNGATED + 1))
  done
  UNGATED_NOTES+=("${1}: ${2} -- ${3}")
}

# Size + nanosecond mtime, plus whether an open write-ahead log sits beside the
# database. A .wal means the file was not cleanly closed -- an export is in
# flight, or one died. Neither is a gateable state.
fingerprint() {  # dataset_path
  [[ -f "$1" ]] || { echo "absent"; return; }
  local wal="no-wal"
  [[ -e "${1}.wal" ]] && wal="wal-present"
  echo "$(stat -c '%s %y' "$1") ${wal}"
}

# Gates 1-4 on one dataset. The row label keys the matrix; the allowlist may
# be empty (no expected-empty tables).
gate_dataset() {  # row, config, dataset, bundle, allowlist, re-export hint
  local row="$1" cfg="$2" ds="$3" bundle="$4" allow="$5" hint="$6"
  local tag="${row//\//_}"
  ROWS+=("$row")

  if [[ ! -f "$ds" ]]; then
    echo "MISSING dataset: ${ds} (${hint})" >&2
    ungate_row "$row" "MISSING" "no dataset at ${ds}"
    return
  fi

  # A dataset older than the inputs it was built from is not a gateable
  # artifact: trace_domain would compare it against a bundle it never saw,
  # and the config-driven maps may name columns it does not carry. The
  # honest verdict is "re-export", not FAIL.
  local stale_inputs=()
  [[ "${bundle}/run.duckdb" -nt "$ds" ]] && stale_inputs+=("bundle run.duckdb")
  [[ "${bundle}/base.json" -nt "$ds" ]] && stale_inputs+=("bundle base.json")
  [[ "$cfg" -nt "$ds" ]] && stale_inputs+=("config ${cfg}")
  if [[ ${#stale_inputs[@]} -gt 0 ]]; then
    echo "STALE: ${ds} predates ${stale_inputs[*]}" >&2
    ungate_row "$row" "STALE" \
      "dataset predates its inputs (${stale_inputs[*]}) -- ${hint}"
    return
  fi

  local fp_before
  fp_before="$(fingerprint "$ds")"
  if [[ "$fp_before" == *wal-present ]]; then
    echo "UNGATED: ${ds} has an open write-ahead log (${ds}.wal)" >&2
    ungate_row "$row" "UNGATED" \
      "open write-ahead log beside the dataset -- export in flight or not cleanly closed"
    return
  fi

  uv run python tools/qa/scd2_windows.py "$ds" >"$GATE_TMP"/qa_scd2_"${tag}".json 2>"$GATE_TMP"/qa_scd2_"${tag}".err
  local ec_scd2=$?

  uv run python tools/qa/refs_resolve.py "$cfg" "$ds" >"$GATE_TMP"/qa_refs_"${tag}".json 2>"$GATE_TMP"/qa_refs_"${tag}".err
  local ec_refs=$?

  uv run python tools/qa/trace_domain.py "$cfg" "$ds" "${bundle}/run.duckdb" \
    >"$GATE_TMP"/qa_trace_"${tag}".json 2>"$GATE_TMP"/qa_trace_"${tag}".err
  local ec_trace=$?

  local zero_args=("$cfg" "$ds")
  [[ -n "$allow" ]] && zero_args+=(--expected-empty "$allow")
  uv run python tools/qa/zero_rows.py "${zero_args[@]}" \
    >"$GATE_TMP"/qa_zero_"${tag}".json 2>"$GATE_TMP"/qa_zero_"${tag}".err
  local ec_zero=$?

  # Only trust the four verdicts if the dataset held still while they ran.
  if [[ "$(fingerprint "$ds")" != "$fp_before" ]]; then
    echo "UNSTABLE: ${ds} changed while the gates read it" >&2
    ungate_row "$row" "UNSTABLE" \
      "dataset changed while the gates ran (concurrent export?) -- verdicts discarded"
    return
  fi

  record "$row" "scd2" "$ec_scd2"
  record "$row" "refs" "$ec_refs"
  record "$row" "trace" "$ec_trace"
  record "$row" "zero" "$ec_zero"
}

record_determinism() {  # label, exit_code
  DET_ROWS+=("$1")
  RESULT["${1}/*/determinism"]=$([[ "$2" -eq 0 ]] && echo "PASS" || echo "FAIL")
  [[ "$2" -eq 0 ]] || TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
}

if [[ -n "$ONE_DATASET" ]]; then
  gate_dataset "$ONE_LABEL" "$ONE_CONFIG" "$ONE_DATASET" "$ONE_BUNDLE" "$ONE_ALLOW" \
    "re-export ${ONE_CONFIG} from ${ONE_BUNDLE}"
  bash tools/qa/determinism.sh --bundle "$ONE_BUNDLE" --config "$ONE_CONFIG" --label "$ONE_LABEL" \
    >"$GATE_TMP"/qa_determinism_"${ONE_LABEL//\//_}".log 2>&1
  record_determinism "$ONE_LABEL" $?
fi

for ex in "${EXAMPLES[@]}"; do
  bundle="docs/examples/${ex}/bundle"
  [[ -f "${bundle}/base.json" ]] || { echo "== ${ex}: no bundle, skipping =="; continue; }
  allow="docs/examples/${ex}/expected-empty.yaml"
  [[ -f "$allow" ]] || allow=""

  for mode in base source dimensional; do
    cfg="docs/examples/${ex}/${mode}.yaml"
    ds="docs/examples/${ex}/exports/${mode}.duckdb"
    [[ -f "$cfg" ]] || continue
    gate_dataset "${ex}/${mode}" "$cfg" "$ds" "$bundle" "$allow" \
      "run tools/run_all_exports.sh ${ex} first"
  done

  bash tools/qa/determinism.sh "$ex" >"$GATE_TMP"/qa_determinism_"${ex}".log 2>&1
  record_determinism "$ex" $?
done

echo ""
echo "======================== GATE MATRIX ========================"
printf "%-45s %-9s %-9s %-9s %-9s\n" "dataset" "scd2" "refs" "trace" "zero"
for row in "${ROWS[@]}"; do
  printf "%-45s %-9s %-9s %-9s %-9s\n" \
    "$row" \
    "${RESULT["${row}/scd2"]:-?}" \
    "${RESULT["${row}/refs"]:-?}" \
    "${RESULT["${row}/trace"]:-?}" \
    "${RESULT["${row}/zero"]:-?}"
done
echo ""
echo "=================== DETERMINISM ===================="
for label in "${DET_ROWS[@]}"; do
  printf "%-45s %-6s\n" "$label" "${RESULT["${label}/*/determinism"]}"
done
if [[ ${#UNGATED_NOTES[@]} -gt 0 ]]; then
  echo ""
  echo "===================== UNGATED ======================"
  echo "Not data defects -- these datasets could not be gated at all:"
  for note in "${UNGATED_NOTES[@]}"; do
    echo "  ${note}"
  done
  echo "Each line names its own remedy; nothing here says anything about data quality."
fi

echo ""
echo "total failing gate invocations:  ${TOTAL_FAILURES}   (data defects)"
echo "total ungated gate invocations:  ${TOTAL_UNGATED}   (dataset unreadable, changing, or stale)"
if [[ -n "${QA_KEEP_TMP:-}" ]]; then
  echo "gate detail JSON/logs kept under ${GATE_TMP}/"
else
  echo "gate detail JSON/logs were under ${GATE_TMP}/ (removed on exit; rerun with QA_KEEP_TMP=1 to inspect)"
fi

if [[ "$TOTAL_FAILURES" -gt 0 ]]; then
  exit "$TOTAL_FAILURES"
fi
[[ "$TOTAL_UNGATED" -gt 0 ]] && exit 3
exit 0
