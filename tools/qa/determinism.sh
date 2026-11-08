#!/usr/bin/env bash
# Determinism gate — DATA-ONLY apart from invoking the CLI itself.
# ============================================================
# Runs every mode config for the given example TWICE into two fresh temp
# dirs, then asserts every table in every output .duckdb is row-for-row
# identical (via `EXCEPT` both directions, under a canonical ORDER BY built
# from every column). This is a harness, not a checker: it shells out to
# `fabulexa-forge export` (the only place in tools/qa/ allowed to import
# fabulexa_forge), but the comparison step itself uses only duckdb + stdlib.
#
# Usage:
#   tools/qa/determinism.sh <example>     # e.g. nhs, retail, ride-sharing
#   tools/qa/determinism.sh --bundle DIR --config CONFIG.yaml [--label NAME]
#                                         # one config against one bundle at any
#                                         # path (run_gates.sh --dataset uses this)
#
# Exit code: number of (mode, table) mismatches found (0 = fully deterministic).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

EX=""
BUNDLE=""
ONE_CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    --config) ONE_CONFIG="$2"; shift 2 ;;
    --label)  EX="$2"; shift 2 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) EX="$1"; shift ;;
  esac
done

if [[ -n "$ONE_CONFIG" || -n "$BUNDLE" ]]; then
  if [[ -z "$ONE_CONFIG" || -z "$BUNDLE" ]]; then
    echo "--bundle and --config must be given together" >&2; exit 2
  fi
  [[ -n "$EX" ]] || EX="$(basename "$ONE_CONFIG" .yaml)"
  # (mode label, config path) pairs; the label names the row in the output.
  CONFIGS=("$(basename "$ONE_CONFIG" .yaml)=${ONE_CONFIG}")
else
  [[ -n "$EX" ]] || { echo "usage: determinism.sh <example> | --bundle DIR --config FILE" >&2; exit 2; }
  BUNDLE="docs/examples/${EX}/bundle"
  CONFIGS=()
  for mode in base source dimensional; do
    cfg="docs/examples/${EX}/${mode}.yaml"
    [[ -f "$cfg" ]] && CONFIGS+=("${mode}=${cfg}")
  done
fi
if [[ ! -f "${BUNDLE}/base.json" ]]; then
  echo "no bundle for ${EX} at ${BUNDLE}/base.json" >&2
  exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAILURES=0

for pair in "${CONFIGS[@]}"; do
  mode="${pair%%=*}"
  cfg="${pair#*=}"

  out_a="${WORK}/a-${mode}.duckdb"
  out_b="${WORK}/b-${mode}.duckdb"

  if ! uv run fabulexa-forge export "$BUNDLE" "$cfg" "$out_a" --fmt duckdb >"${WORK}/run_a.log" 2>&1; then
    echo "FAIL ${EX}/${mode}: run A failed"; cat "${WORK}/run_a.log" >&2
    FAILURES=$((FAILURES + 1)); continue
  fi
  if ! uv run fabulexa-forge export "$BUNDLE" "$cfg" "$out_b" --fmt duckdb >"${WORK}/run_b.log" 2>&1; then
    echo "FAIL ${EX}/${mode}: run B failed"; cat "${WORK}/run_b.log" >&2
    FAILURES=$((FAILURES + 1)); continue
  fi

  uv run python - "$EX" "$mode" "$out_a" "$out_b" <<'PYEOF'
import sys
import duckdb

ex, mode, path_a, path_b = sys.argv[1:5]

con = duckdb.connect(":memory:")
con.execute(f"attach '{path_a}' as a (read_only)")
con.execute(f"attach '{path_b}' as b (read_only)")

tables_a = {
    r[0] for r in con.execute(
        "select table_name from duckdb_tables() where database_name = 'a'"
    ).fetchall()
}
tables_b = {
    r[0] for r in con.execute(
        "select table_name from duckdb_tables() where database_name = 'b'"
    ).fetchall()
}

mismatches = 0

if tables_a != tables_b:
    print(f"MISMATCH {ex}/{mode}: table sets differ: {tables_a ^ tables_b}")
    mismatches += 1

for table in sorted(tables_a & tables_b):
    cols = [
        r[0] for r in con.execute(f'describe a."{table}"').fetchall()
    ]
    col_list = ", ".join(f'"{c}"' for c in cols)
    diff_ab = con.execute(
        f'select count(*) from (select {col_list} from a."{table}" '
        f'except select {col_list} from b."{table}")'
    ).fetchone()[0]
    diff_ba = con.execute(
        f'select count(*) from (select {col_list} from b."{table}" '
        f'except select {col_list} from a."{table}")'
    ).fetchone()[0]
    if diff_ab or diff_ba:
        print(
            f"MISMATCH {ex}/{mode}/{table}: {diff_ab} rows only in run A, "
            f"{diff_ba} rows only in run B"
        )
        mismatches += 1
    else:
        print(f"OK {ex}/{mode}/{table}")

sys.exit(1 if mismatches else 0)
PYEOF
  status=$?
  if [[ $status -ne 0 ]]; then
    FAILURES=$((FAILURES + status))
  fi
done

echo "---- determinism ${EX}: ${FAILURES} mismatch(es)"
exit "$FAILURES"
