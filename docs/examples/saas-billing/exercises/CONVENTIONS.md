# Billing query conventions

The pinned choices every query in this directory implements. The README of the
dimensional export (`../exports/dimensional-dimensional-readme.md`, § Billing
spec) states the stage order and the deliberate ambiguities; this file records
which side of each ambiguity the **reference** takes, so that `fabulexa-forge
compare` against the reference render is an exact-equality test. A candidate
query that picks a different convention is not wrong about billing — it is
wrong about *this* spec, and the diff will say where.

## Output

Three tiers share these conventions; each tier's output is a prefix of the
column list below (`beginner/`: seat and add-on lines; `intermediate/`: plus
usage; `advanced/`: all seven), with `total_due` always the sum of the lines
the tier carries. See `README.md` § Tiers.

One row per **company** (`company_id`, `CMP-#####`) per calendar month
(`billing_month`, the first day of the month, `DATE`), from the month the
company's account was provisioned through the month it churned (inclusive), or
the last month of the extract if it never churned.

| Column | Meaning |
|---|---|
| `seat_charge` | prorated billable seats × seat price |
| `usage_charge` | rated metered usage after promo, trial, allowance, tiers, negotiated discount |
| `addon_charge` | flat SKU fees the account subscribes to |
| `commit_charge` | committed spend billed up front in the month a term starts |
| `commit_applied` | the part of `usage_charge` covered by the current term's prepaid commit |
| `credit_applied` | credits consumed against this month's invoice |
| `total_due` | `seat_charge + usage_charge + addon_charge + commit_charge − commit_applied − credit_applied` |

All seven are `DECIMAL(12,2)`. The six line columns are each rounded once, at
the very end (half away from zero — DuckDB `ROUND`); `total_due` is the sum of
the six **rounded** lines, so the printed total always equals the printed
lines. Every intermediate is `DECIMAL`, never `DOUBLE`, wide enough that
nothing rounds implicitly: inputs are cast at the boundary (`volume` to
`DECIMAL(14,2)`, rates to `DECIMAL(10,6)`, a unit price to `DECIMAL(14,8)`) and
money is carried as `DECIMAL(28,14)`. The only other rounding is the billable
seat fraction, rounded to 6 decimals. The result therefore does not depend on
the order rows happen to be summed in, or on how a query is restructured.

## Time

- **The month spine is the warehouse calendar.** `dim_date` (one row per day,
  2026-01-01 to 2030-12-31) grouped by `year, month` gives every billing
  month's `month_start`, `next_month_start`, and `days_in_month` (its row
  count). Nothing is generated in the query.
- **An instant belongs to the month of its `*_date_key`** (`dim_account.
  created_date_key`, `fact_usage_event.occurred_date_key`, `fact_credit.
  issued_date_key`, `fact_lifecycle_interval.started_date_key`) looked up on
  `dim_date.date_key`; a `DATE` column (`dim_company.valid_from`) is looked
  up on `dim_date.date`. The key is the local date of the same instant the
  timestamp column renders, so `date_trunc('month', <timestamp>)` agrees
  with it — a candidate may use either.
- **Month end instant** `T` = the last instant of the month. An SCD-2 version
  is "as of month end" when `valid_from < next_month_start AND (valid_to IS
  NULL OR valid_to >= next_month_start)`. The same rule reads `dim_rate_tier`
  windows. For `custom_tier` (DATE, inclusive both ends): `effective_from <=
  month_end_date AND (effective_to IS NULL OR effective_to >= month_end_date)`.
- **The extract ends** in the month of the last `fact_lifecycle_interval`
  transition. It is read from the data, not written into the query.
- **Churn month** = the month of the first `dim_company` version with
  `status = 'churned'`, reached through `dim_account.company_id`. Nothing
  after it is billed, including usage that seats run while offboarding.
- **Account → plan terms** through `dim_account.plan = dim_plan_terms.tier`.

## Stages

1. **Billable seats** — for every `dim_user_status` interval in state `active`
   or `using` overlapping the month (`[valid_from, valid_to)`, `valid_to` NULL =
   open), sum the overlapping days; divide by the days in the month. Fractional
   seats are kept at 6 decimals. Zero seats is a valid month: the account still
   owes add-ons and commits.
2. **Seat charge** — billable seats × `seat_price` as of month end.
3. **Usage** — `json_extract` the `volume` leaf of `fact_usage_event.context`;
   only `dim_sku.billing_model = 'metered'` sessions count; sum per account, SKU,
   month of `occurred_at`.
4. **Promotion / trial** — a session is dropped before aggregation when a
   `dim_promotion` row of `kind = 'free_usage'` for its SKU has `starts_at <=
   occurred_at < ends_at`, or when `occurred_at < dim_account.created_at +
   trial_days`.
5. **Allowance** — applies to the SKU named `Core` only. Allowance units =
   billable seats × (`allowance_override` as of month end, else the plan's
   `included_units_per_seat`). Net volume = `GREATEST(0, volume − allowance)`.
6. **Tiered rating** — the ladder for (account, SKU, month) is `custom_tier`
   when any row of it is effective at month end, else `dim_rate_tier` rows
   effective at month end, else no ladder. Units in a band = `GREATEST(0,
   LEAST(net_volume, to_units) − GREATEST(from_units − 1, 0))`, with a NULL
   `to_units` read as unbounded — bands are continuous with an inclusive top;
   volume no band covers (ACC-00027's gap) is unrated. A SKU with no ladder
   rates all net volume at `dim_sku.standard_rate`.
7. **Negotiated discount** — every rated amount is multiplied by
   `pct_of_list_rate / 100` as of month end, **except** a `custom_tier` band in
   `pct_of_list` form, which is `units × standard_rate × pct_of_list / 100` and
   nothing else.
8. **Add-ons** — `flat_fee` of the SKU named `SSO` when `addon_sso = 'yes'`,
   plus `flat_fee` of `Premium Support` when `addon_support = 'yes'`, every
   billed month, never prorated, never drawn from a commit.
9. **Commit** — a term starts at each `dim_account_terms` version whose
   `term_commit` is positive and differs from the previous version's; it ends
   where the next term starts, or is open. `commit_charge` in a month = the sum
   of commits of terms starting in that month. The month's term is the one
   containing month end; `commit_applied = LEAST(usage_charge, GREATEST(0,
   term_commit − usage already applied in earlier months of the same term))`.
   Unused commit expires with the term; a term that starts and ends inside one
   month is billed and never drawn.
10. **Credits** — a credit belongs to the account of the company it names.
    It is open from its issue month through the month containing `issued_at +
    credit_validity_days` (the plan's), inclusive. Each month, open credits
    apply in order of `issued_at` then `credit_id` against `pre_credit_total =
    seat + usage + addon + commit_charge − commit_applied`; a credit consumes
    `LEAST(remaining, pre_credit_total − consumed by earlier credits this
    month)`, its remainder carries to the next month, and whatever is left when
    it expires is forfeited. A credit issued after the account's churn month is
    never applied.
