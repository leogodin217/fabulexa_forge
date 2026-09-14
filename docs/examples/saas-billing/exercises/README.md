# Billing query exercises

Working directory for the inherited-complex-query teaching track over the
`saas-billing` pack. Everything here runs against the pack's dimensional export
(`../exports/dimensional.duckdb`, produced by `../dimensional.yaml`), and every
query is graded by `fabulexa-forge compare` against the reference render.

| Path | What it is |
|---|---|
| `CONVENTIONS.md` | The pinned billing conventions every query implements — read first |
| `<tier>/reference.sql` | The clean form: one CTE per stage, in spec order; its month spine is the warehouse calendar (`dim_date`), reached from the facts through their `*_date_key` columns |
| `<tier>/inherited.sql` | The same bill as it would look after years of maintenance — identical output |
| `run_query.py` | Runs a query against the export and writes the result as one table |

## Billing rules

Each customer gets one invoice per calendar month, built in this order. The
technical statement of each rule — which column, which as-of instant, which
side of every ambiguity — is in `CONVENTIONS.md`; this is the business view.

1. **Seats.** Every user holding a live seat is billed for the days they held
   it that month, so a seat added on the 20th costs a third of a month.
   Seat-days × the account's negotiated per-seat price, which changes at
   renewal reviews — the price in force at month end is used.
2. **Usage.** Metered products (Core, Analytics, API, Storage, Workflow) are
   billed on volume consumed. Some volume is free before rating: anything used
   during the account's trial period, and anything on a product during a
   promotional window (the spring campaigns).
3. **Allowance.** Each plan includes a monthly quota of Core units per seat;
   enterprise accounts can negotiate a larger one. Only Core volume above
   seats × quota is charged.
4. **Tiered rating.** The remaining volume is priced band by band — the first
   N units at one rate, the next at a lower one, and so on. Each product has a
   published ladder; the four biggest enterprise accounts have privately
   negotiated ladders (`custom_tier`) that replace it. A product with no ladder
   (Workflow) is charged at flat list price.
5. **Negotiated discount.** Enterprise accounts pay a negotiated percentage of
   list on their usage charge. A custom band priced as a percentage of list
   already embeds this and is not discounted twice.
6. **Add-ons.** Flat monthly fees for SSO and Premium Support, if subscribed.
7. **Commitments.** Enterprise accounts commit to a usage spend for each review
   term and pay it up front when the term starts. Each month's usage charge
   draws down that prepaid balance; once it is exhausted, further usage is
   billed as overage. Whatever is unused when the term ends is forfeited.
8. **Credits.** Goodwill, SLA-breach, and billing-error credits issued at
   reviews are applied to invoices oldest-first, any remainder carrying to the
   next month, until they are used up or expire (90 / 180 / 365 days by plan).
   An invoice never goes below zero.

**Total due** = seats + usage + add-ons + commitment billed − commitment drawn
down − credits applied. An account is billed from the month it was provisioned
through the month it churned.

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
