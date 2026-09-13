# Billing query exercises

Working directory for the inherited-complex-query teaching track over the
`saas-billing` pack. Everything here runs against the pack's dimensional export
(`../exports/dimensional.duckdb`, produced by `../dimensional.yaml`), and every
query is graded by `fabulexa-forge compare` against the reference render.

| Path | What it is |
|---|---|
| `CONVENTIONS.md` | The pinned billing conventions every query implements — read first |
| `<tier>/reference.sql` | The clean form: one CTE per stage, in spec order |
| `<tier>/inherited.sql` | The same bill as it would look after years of maintenance — identical output |
| `run_query.py` | Runs a query against the export and writes the result as one table |

## Tiers

Three tiers, each a prefix of the billing stages with its own (narrower) output
table. The reference of each tier is literally the advanced reference cut
short, so a column shared between tiers carries the same value for the same
company-month — a learner can check beginner work against the full invoice.

| Tier | Stages | Output columns | Inherited form |
|---|---|---|---|
| `beginner/` | 1, 2, 8 — prorated seats × price, flat add-ons | `seat_charge`, `addon_charge`, `total_due` | ~30 lines: terse aliases, comma joins, a digits-table month spine, hardcoded fees, one level of nesting |
| `intermediate/` | 1–8 — adds usage from the JSON payload, promo/trial exclusion, allowance, tiered rating with the `custom_tier` override, negotiated discount | + `usage_charge` | ~60 lines: nested four deep, the seat subquery copy-pasted, plan constants inlined, the ladder override as a `COALESCE` of correlated aggregates; no recursion, no running balance |
| `advanced/` | 1–10 — adds commit drawdown and the FIFO credit ledger | + `commit_charge`, `commit_applied`, `credit_applied` | ~100 lines, longest line 1,600+ chars: everything above plus a cross-month running balance and a recursive ledger that is correct only because of an unstated data property |

Every column is `DECIMAL(12,2)`; `total_due` is the sum of the tier's rounded
lines. Reference queries run in about 5 s; the advanced inherited query takes
about 70 s (its recursion re-evaluates the invoice every iteration).

## Run and compare

```bash
cd docs/examples/saas-billing
uv run python exercises/run_query.py exercises/intermediate/reference.sql /tmp/ref.duckdb
uv run python exercises/run_query.py exercises/intermediate/inherited.sql /tmp/inh.duckdb
uv run fabulexa-forge compare /tmp/ref.duckdb /tmp/inh.duckdb      # EQUAL, exit 0
```

`compare` is exact relational equality over the one `monthly_bill` table; a
refactor is correct when it is `EQUAL` to the reference render.
