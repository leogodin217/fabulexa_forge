## overview

The billing scenario for the inherited-complex-query teaching track. The
warehouse carries every billing **fact** the simulation emits — plan terms,
the per-SKU rate ladder, negotiated commercial terms per account, promotions,
credits, seats and usage — and none of the **outcomes**: there is no invoice
table, no commit balance, no applied-credit residue. Those are the reference
query's job, and the spec below is what that query implements.

The exercises themselves — a clean reference query and an inherited one per
tier, the pinned conventions, and a grader that needs only the DuckDB CLI —
live in [fabulexa_complex_queries](https://github.com/leogodin217/fabulexa_complex_queries),
which also publishes this warehouse as a ready-made DuckDB file so the track
can be run without forge.

### Billing spec — the reference query's stage order

One invoice per account per calendar month. Stages run in this order; each
consumes the previous stage's output.

1. **Billable seats** — seats whose `dim_user_status` interval overlaps the
   month in a billable state (`active` or `using`), prorated by seat-days
   over the month's days. `provisioned` and `offboarded` days are not billed.
2. **Seat charge** — billable seats × `dim_account_terms.seat_price` as of
   month end.
3. **Usage per account / SKU / month** — the `volume` leaf of
   `fact_usage_event.context` (a JSON payload: `json_extract(context,
   '$.volume')`) summed by the SKU each session bound; only
   `billing_model = metered` SKUs rate.
4. **Promotions** — volume on a SKU during an active `dim_promotion` window
   (`starts_at` ≤ `occurred_at` < `ends_at`) is removed before anything
   else, as is all volume inside the account's free trial (`dim_account.
   created_at` + `dim_plan_terms.trial_days`).
5. **Allowance** — Core (`SKU-001`) volume is netted by billable seats ×
   included units: `dim_account_terms.allowance_override` when present,
   else `dim_plan_terms.included_units_per_seat` for the account's plan.
6. **Tiered rating** — the remaining cumulative period volume is walked up
   the ladder band by band. The ladder is `custom_tier` for the account +
   SKU when a row is effective for the month (see that table's notes),
   otherwise `dim_rate_tier` restricted to rows effective at month end. A
   metered SKU with no ladder at all (`Workflow`, `SKU-007`) rates at
   `dim_sku.standard_rate` flat.
7. **Negotiated discount** — the rated amount × `dim_account_terms.
   pct_of_list_rate / 100` as of month end. A `custom_tier` row that
   carries `pct_of_list` instead of `rate` *replaces* this stage for that
   band: it is applied to `dim_sku.standard_rate`, and the account's own
   `pct_of_list_rate` is not applied on top.
8. **Flat add-ons** — `dim_sku.flat_fee` for each `flat` SKU the account
   subscribes to (`dim_account.addon_sso` / `addon_support`).
9. **Commit drawdown** — for an enterprise account with `term_commit` > 0,
   usage charges draw the commit down across the term's months (a term is
   the interval between consecutive `dim_account_terms` versions whose
   `term_commit` changed); charges above the commit bill as overage; the
   unused balance expires at term end.
10. **Credits** — open `fact_credit` rows (issued before month end, not yet
    expired: `issued_at` + `dim_plan_terms.credit_validity_days`) apply
    FIFO by `issued_at`; a partially consumed credit carries its remainder
    forward. Credits reduce the invoice, never below zero.

**Conventions.** Month-end attribute values (stage 2, 5, 7) are the
`dim_account_terms` version whose `[valid_from, valid_to)` contains the
last instant of the month. The month an instant belongs to is read through
its `*_date_key` on `dim_date` (the local date in the export's zone), and a
month's length is the calendar's row count for that month. Volume is in the
SKU's own units; the ladder bounds are inclusive on both ends.

### Deliberate ambiguities

The spec above picks one reading. The alternatives are real, and a reviewer
of the inherited query should be able to say which one it implements.

- **Price as of when?** — month end (above) vs the instant of each usage
  event. They differ whenever terms change mid-month.
- **Band bounds and the repriced Analytics band** — `dim_rate_tier` has two
  rows for Analytics band 2, one deactivated on the day the other starts.
  "Effective at month end" and "effective at event time" give different
  rates for the changeover month.
- **Allowance before or after promotion** — the spec removes promo volume
  first; netting the allowance first is the common mistake.
- **Commit terms** — read off `term_commit` version boundaries (988
  changes across the run) or off the review cadence itself
  (`fact_lifecycle_interval` rows in state `reviewed`, 2,977 of them); the
  two disagree whenever a review leaves the commit unchanged.

### Known traps

- The metered quantity is not a column. It lives inside the JSON
  `fact_usage_event.context` as `volume`; summing anything else sums
  nothing.
- Renewal reviews never touch `dim_company.status` (onboarding / active /
  churned only). The review timeline — and each credit's issuing review —
  is on `fact_lifecycle_interval` under `lifecycle_type =
  company_lifecycle`, states `reviewed` and `credit_issued`.
- Three quarters of `dim_account_terms` versions change nothing you can
  see: 10,189 of 13,591 rows carry the same seat price, discount, commit
  and allowance as the row before them (the upstream system re-versioned
  the account on an attribute this warehouse does not carry). Counting
  versions does not count amendments; `LAG` over the four terms columns
  does. The as-of-month-end rule is unaffected.
- There is no churn column on the account or its terms. Churn is the
  `dim_company` version whose `status` is `churned`, one hop away through
  `dim_account.company_id`; its `valid_from` is the cancellation date.
- `dim_user.account_id` points at the **account** (`ACC-`); `dim_account.
  company_id` and `fact_credit.company_id` point at the **company**
  (`CMP-`). Both are "the company" in plain English. A credit reaches its
  account through `dim_account.company_id`, not the other way round.
- `Workflow` (`SKU-007`) launched mid-run and has no `dim_rate_tier` rows.
  An inner join to the ladder silently drops every Workflow session.
- The Analytics band-2 repricing is a **new row** in `dim_rate_tier` with
  its own `effective_from`, not a change to the old one — filter by
  effective window or you will double-count the band.
- `custom_tier.account_id` is written in the same identity surface the
  warehouse elects for accounts (`ACC-#####`). Nothing checks this: a
  ladder row for an id that does not exist joins to zero rows and is never
  reported.
- `dim_date` spans exactly the five run years, 2026-01-01 to 2030-12-31. A
  date the query computes — a credit's expiry, a term end a year out — can
  fall past it and then has no `date_key`; compare such dates as dates or
  instants rather than keying into the calendar.
  `fact_lifecycle_interval.ended_date_key` is NULL on an interval still open
  at the extract's end, like `ended_at`.
- `dim_account.size_band` is the customer's **headcount** band (employees),
  not its seat count. Seats are read off `dim_user_status`; a segmentation
  by `size_band` will not track billable seats and is not meant to.

## table: custom_tier

The side table finance hands over with the words "this is the contract
now" — not produced by the simulation, and carrying no foreign-key
guarantee.

**Override rule.** For an account + SKU + month, if `custom_tier` has rows
whose `[effective_from, effective_to]` window contains the month end, the
tiered-rating stage uses those rows and ignores `dim_rate_tier` for that
SKU. SKUs the account has no custom rows for fall back to `dim_rate_tier`
as normal. `effective_to` is inclusive; empty means still in force.

**Two pricing forms.** A row carries exactly one of `rate` (a per-unit
price, replaces the band's `dim_rate_tier.rate`) or `pct_of_list` (a
percentage applied to `dim_sku.standard_rate` for volume in that band,
replacing the account's `pct_of_list_rate` — not stacked with it).

**Selection.** The four accounts are the top enterprise accounts by total
usage volume over the run (`ACC-00002`, `ACC-00095`, `ACC-00027`,
`ACC-00096`); their ladders were authored by hand, with each
`effective_from` on one of that account's real renewal-review dates.

**Irregularities to expect.**

- `ACC-00002` has two Core ladders in sequence — five bands until
  2028-05-24, four bands from 2028-05-25 — with `ordinal` restarting at 1 in
  the second window. Its Analytics and API ladders have a single open
  window; Storage and Workflow have no custom rows.
- `ACC-00095`'s ladder starts mid-life (2027-07-04); earlier months rate on
  `dim_rate_tier`.
- `ACC-00027`'s Analytics ladder has a **coverage gap**: no band covers
  10,001–12,000 units. The spec does not say what happens to volume in the
  gap; the reference query must choose (and document) a rule.
- `ACC-00096`'s Core ladder is in `pct_of_list` form, and the account also
  carries its own negotiated `pct_of_list_rate` on `dim_account_terms`.
- `ACC-00027` and `ACC-00096` churned; their windows close on the churn
  date. `ACC-00002` and `ACC-00095` are open at the end of the extract.
