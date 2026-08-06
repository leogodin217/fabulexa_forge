#!/usr/bin/env bash
# Runs gates 1-4 (scd2_windows, refs_resolve, trace_domain, determinism)
# across every example + mode dataset under docs/examples/*/exports/, prints a
# per-dataset status matrix, and exits nonzero if any gate failed.
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
# Usage:
#   tools/qa/run_gates.sh              # all examples
#   tools/qa/run_gates.sh nhs retail   # subset
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

EXAMPLES=("$@")
if [[ ${#EXAMPLES[@]} -eq 0 ]]; then
  for d in docs/examples/*/; do
    [[ -f "${d}bundle/base.json" ]] && EXAMPLES+=("$(basename "$d")")
  done
fi

declare -A RESULT
ROWS=()
UNGATED_NOTES=()
TOTAL_FAILURES=0
TOTAL_UNGATED=0

#: Gate exit code meaning "could not read the dataset" (see the gate scripts).
UNGATED_EXIT=3

record() {  # example, mode, gate, exit_code
  local key="${1}/${2}/${3}"
  if [[ "$4" -eq 0 ]]; then
    RESULT["$key"]="PASS"
  elif [[ "$4" -eq "$UNGATED_EXIT" ]]; then
    RESULT["$key"]="UNGATED"
    TOTAL_UNGATED=$((TOTAL_UNGATED + 1))
  else
    RESULT["$key"]="FAIL"
    TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
  fi
}

ungate_row() {  # example, mode, status, reason
  local gate
  for gate in scd2 refs trace; do
    RESULT["${1}/${2}/${gate}"]="$3"
    TOTAL_UNGATED=$((TOTAL_UNGATED + 1))
  done
  UNGATED_NOTES+=("${1}/${2}: ${3} -- ${4}")
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

for ex in "${EXAMPLES[@]}"; do
  bundle="docs/examples/${ex}/bundle"
  [[ -f "${bundle}/base.json" ]] || { echo "== ${ex}: no bundle, skipping =="; continue; }

  for mode in base source dimensional; do
    cfg="docs/examples/${ex}/${mode}.yaml"
    ds="docs/examples/${ex}/exports/${mode}.duckdb"
    [[ -f "$cfg" ]] || continue
    ROWS+=("${ex}/${mode}")

    if [[ ! -f "$ds" ]]; then
      echo "MISSING dataset: ${ds} (run tools/run_all_exports.sh ${ex} first)" >&2
      ungate_row "$ex" "$mode" "MISSING" "no dataset at ${ds}"
      continue
    fi

    # A dataset older than the inputs it was built from is not a gateable
    # artifact: trace_domain would compare it against a bundle it never saw,
    # and the config-driven maps may name columns it does not carry. The
    # honest verdict is "re-export", not FAIL.
    stale_inputs=()
    [[ "${bundle}/run.duckdb" -nt "$ds" ]] && stale_inputs+=("bundle run.duckdb")
    [[ "${bundle}/base.json" -nt "$ds" ]] && stale_inputs+=("bundle base.json")
    [[ "$cfg" -nt "$ds" ]] && stale_inputs+=("config ${cfg}")
    if [[ ${#stale_inputs[@]} -gt 0 ]]; then
      echo "STALE: ${ds} predates ${stale_inputs[*]}" >&2
      ungate_row "$ex" "$mode" "STALE" \
        "dataset predates its inputs (${stale_inputs[*]}) -- re-run tools/run_all_exports.sh ${ex}"
      continue
    fi

    fp_before="$(fingerprint "$ds")"
    if [[ "$fp_before" == *wal-present ]]; then
      echo "UNGATED: ${ds} has an open write-ahead log (${ds}.wal)" >&2
      ungate_row "$ex" "$mode" "UNGATED" \
        "open write-ahead log beside the dataset -- export in flight or not cleanly closed"
      continue
    fi

    uv run python tools/qa/scd2_windows.py "$ds" >"$GATE_TMP"/qa_scd2_"${ex}_${mode}".json 2>"$GATE_TMP"/qa_scd2_"${ex}_${mode}".err
    ec_scd2=$?

    uv run python tools/qa/refs_resolve.py "$cfg" "$ds" >"$GATE_TMP"/qa_refs_"${ex}_${mode}".json 2>"$GATE_TMP"/qa_refs_"${ex}_${mode}".err
    ec_refs=$?

    uv run python tools/qa/trace_domain.py "$cfg" "$ds" "${bundle}/run.duckdb" \
      >"$GATE_TMP"/qa_trace_"${ex}_${mode}".json 2>"$GATE_TMP"/qa_trace_"${ex}_${mode}".err
    ec_trace=$?

    # Only trust the three verdicts if the dataset held still while they ran.
    if [[ "$(fingerprint "$ds")" != "$fp_before" ]]; then
      echo "UNSTABLE: ${ds} changed while the gates read it" >&2
      ungate_row "$ex" "$mode" "UNSTABLE" \
        "dataset changed while the gates ran (concurrent export?) -- verdicts discarded"
      continue
    fi

    record "$ex" "$mode" "scd2" "$ec_scd2"
    record "$ex" "$mode" "refs" "$ec_refs"
    record "$ex" "$mode" "trace" "$ec_trace"
  done

  bash tools/qa/determinism.sh "$ex" >"$GATE_TMP"/qa_determinism_"${ex}".log 2>&1
  det_status=$?
  RESULT["${ex}/*/determinism"]=$([[ "$det_status" -eq 0 ]] && echo "PASS" || echo "FAIL")
  [[ "$det_status" -eq 0 ]] || TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
done

echo ""
echo "=================== GATE MATRIX ==================="
printf "%-45s %-9s %-9s %-9s\n" "dataset" "scd2" "refs" "trace"
for row in "${ROWS[@]}"; do
  printf "%-45s %-9s %-9s %-9s\n" \
    "$row" \
    "${RESULT["${row}/scd2"]:-?}" \
    "${RESULT["${row}/refs"]:-?}" \
    "${RESULT["${row}/trace"]:-?}"
done
echo ""
echo "=================== DETERMINISM ===================="
for ex in "${EXAMPLES[@]}"; do
  [[ -n "${RESULT["${ex}/*/determinism"]:-}" ]] && \
    printf "%-45s %-6s\n" "$ex" "${RESULT["${ex}/*/determinism"]}"
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
