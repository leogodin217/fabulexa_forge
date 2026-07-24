#!/usr/bin/env bash
# Runs gates 1-4 (scd2_windows, refs_resolve, trace_domain, determinism)
# across every example + mode dataset under out/exports/, prints a
# per-dataset PASS/FAIL matrix, and exits nonzero if any gate failed.
#
# Usage:
#   tools/qa/run_gates.sh              # all examples
#   tools/qa/run_gates.sh nhs retail   # subset
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
TOTAL_FAILURES=0

record() {  # example, mode, gate, exit_code
  local key="${1}/${2}/${3}"
  RESULT["$key"]=$([[ "$4" -eq 0 ]] && echo "PASS" || echo "FAIL")
  [[ "$4" -eq 0 ]] || TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
}

for ex in "${EXAMPLES[@]}"; do
  bundle="docs/examples/${ex}/bundle"
  [[ -f "${bundle}/base.json" ]] || { echo "== ${ex}: no bundle, skipping =="; continue; }

  for mode in base source dimensional; do
    cfg="docs/examples/${ex}/${mode}.yaml"
    ds="out/exports/${ex}/${mode}.duckdb"
    [[ -f "$cfg" ]] || continue
    ROWS+=("${ex}/${mode}")

    if [[ ! -f "$ds" ]]; then
      echo "MISSING dataset: ${ds} (run tools/run_all_exports.sh ${ex} first)" >&2
      record "$ex" "$mode" "scd2" 1
      record "$ex" "$mode" "refs" 1
      record "$ex" "$mode" "trace" 1
      continue
    fi

    uv run python tools/qa/scd2_windows.py "$ds" >"$GATE_TMP"/qa_scd2_"${ex}_${mode}".json 2>"$GATE_TMP"/qa_scd2_"${ex}_${mode}".err
    record "$ex" "$mode" "scd2" $?

    uv run python tools/qa/refs_resolve.py "$cfg" "$ds" >"$GATE_TMP"/qa_refs_"${ex}_${mode}".json 2>"$GATE_TMP"/qa_refs_"${ex}_${mode}".err
    record "$ex" "$mode" "refs" $?

    uv run python tools/qa/trace_domain.py "$cfg" "$ds" "${bundle}/run.duckdb" \
      >"$GATE_TMP"/qa_trace_"${ex}_${mode}".json 2>"$GATE_TMP"/qa_trace_"${ex}_${mode}".err
    record "$ex" "$mode" "trace" $?
  done

  bash tools/qa/determinism.sh "$ex" >"$GATE_TMP"/qa_determinism_"${ex}".log 2>&1
  det_status=$?
  RESULT["${ex}/*/determinism"]=$([[ "$det_status" -eq 0 ]] && echo "PASS" || echo "FAIL")
  [[ "$det_status" -eq 0 ]] || TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
done

echo ""
echo "=================== GATE MATRIX ==================="
printf "%-45s %-6s %-6s %-6s\n" "dataset" "scd2" "refs" "trace"
for row in "${ROWS[@]}"; do
  printf "%-45s %-6s %-6s %-6s\n" \
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
echo ""
echo "total failing gate invocations: ${TOTAL_FAILURES}"
if [[ -n "${QA_KEEP_TMP:-}" ]]; then
  echo "gate detail JSON/logs kept under ${GATE_TMP}/"
else
  echo "gate detail JSON/logs were under ${GATE_TMP}/ (removed on exit; rerun with QA_KEEP_TMP=1 to inspect)"
fi
exit "$TOTAL_FAILURES"
