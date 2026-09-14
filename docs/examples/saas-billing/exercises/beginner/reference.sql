-- Monthly seat bill per company — the reference (clean) form, beginner tier.
-- Conventions: ../CONVENTIONS.md (stages 1, 2 and 8). Runs against exports/dimensional.duckdb.
-- The same CTEs as the advanced reference, cut after the seat and add-on stages.

WITH

-- ---------------------------------------------------------------- calendar
calendar AS (                                     -- one row per month of dim_date
    SELECT d.year, d.month,
           MIN(d.date)                                AS month_start,
           CAST(MAX(d.date) + INTERVAL 1 DAY AS DATE) AS next_month_start,
           COUNT(*)                                   AS days_in_month
    FROM dim_date d
    GROUP BY d.year, d.month
),
calendar_day AS (                                 -- each day's month, keyed as the facts key it
    SELECT d.date_key, d.date, c.month_start
    FROM dim_date d
    JOIN calendar c ON c.year = d.year AND c.month = d.month
),
extract_end AS (
    SELECT MAX(cd.month_start) AS last_month
    FROM fact_lifecycle_interval f
    JOIN calendar_day cd ON cd.date_key = f.started_date_key
),
-- ---------------------------------------------------------------- accounts
account AS (
    SELECT a.account_id,
           a.company_id,
           a.created_at,
           a.addon_sso,
           a.addon_support,
           p.trial_days,
           p.included_units_per_seat,
           p.credit_validity_days,
           cd.month_start AS first_month,
           LEAST(
               COALESCE(
                   (SELECT MIN(ch.month_start)
                    FROM dim_company c
                    JOIN calendar_day ch ON ch.date = c.valid_from
                    WHERE c.company_id = a.company_id AND c.status = 'churned'),
                   e.last_month),
               e.last_month) AS last_month
    FROM dim_account a
    JOIN dim_plan_terms p ON p.tier = a.plan
    JOIN calendar_day cd  ON cd.date_key = a.created_date_key
    CROSS JOIN extract_end e
),
billing_month AS MATERIALIZED (
    SELECT ac.account_id, c.month_start, c.next_month_start, c.days_in_month
    FROM account ac
    JOIN calendar c ON c.month_start BETWEEN ac.first_month AND ac.last_month
),
terms_as_of AS MATERIALIZED (                                  -- account terms at month end
    SELECT bm.account_id, bm.month_start,
           t.seat_price, t.pct_of_list_rate, t.term_commit, t.allowance_override
    FROM billing_month bm
    JOIN dim_account_terms t
      ON t.account_id = bm.account_id
     AND t.valid_from < CAST(bm.next_month_start AS TIMESTAMP)
     AND (t.valid_to IS NULL OR t.valid_to >= CAST(bm.next_month_start AS TIMESTAMP))
),
-- ---------------------------------------------------------------- 1-2 seats
billable_seats AS (
    SELECT bm.account_id, bm.month_start,
           CAST(ROUND(SUM(date_diff('day',
                    GREATEST(s.valid_from, bm.month_start),
                    LEAST(COALESCE(s.valid_to, bm.next_month_start), bm.next_month_start)))
                / bm.days_in_month, 6) AS DECIMAL(12,6)) AS seats
    FROM billing_month bm
    JOIN dim_user u        ON u.account_id = bm.account_id
    JOIN dim_user_status s ON s.user_id = u.user_id
                          AND s.status IN ('active', 'using')
                          AND s.valid_from < bm.next_month_start
                          AND (s.valid_to IS NULL OR s.valid_to > bm.month_start)
    GROUP BY bm.account_id, bm.month_start, bm.days_in_month
),
seat_charge AS MATERIALIZED (
    SELECT bm.account_id, bm.month_start,
           COALESCE(bs.seats, 0) AS seats,
           CAST(COALESCE(bs.seats, 0) * ta.seat_price AS DECIMAL(28,14)) AS seat_charge
    FROM billing_month bm
    JOIN terms_as_of ta         USING (account_id, month_start)
    LEFT JOIN billable_seats bs USING (account_id, month_start)
),
-- ---------------------------------------------------------------- 8 add-ons
addon_charge AS (
    SELECT bm.account_id, bm.month_start,
           CAST(CASE WHEN ac.addon_sso = 'yes'     THEN sso.flat_fee ELSE 0 END
              + CASE WHEN ac.addon_support = 'yes' THEN sup.flat_fee ELSE 0 END AS DECIMAL(28,14)) AS addon_charge
    FROM billing_month bm
    JOIN account ac ON ac.account_id = bm.account_id
    CROSS JOIN (SELECT flat_fee FROM dim_sku WHERE name = 'SSO')             sso
    CROSS JOIN (SELECT flat_fee FROM dim_sku WHERE name = 'Premium Support') sup
)

-- ---------------------------------------------------------------- the bill
SELECT company_id, billing_month, seat_charge, addon_charge,
       CAST(seat_charge + addon_charge AS DECIMAL(12,2)) AS total_due
FROM (
    SELECT ac.company_id,
           sc.month_start                                   AS billing_month,
           CAST(ROUND(sc.seat_charge, 2)  AS DECIMAL(12,2)) AS seat_charge,
           CAST(ROUND(ad.addon_charge, 2) AS DECIMAL(12,2)) AS addon_charge
    FROM seat_charge sc
    JOIN account ac      ON ac.account_id = sc.account_id
    JOIN addon_charge ad ON ad.account_id = sc.account_id AND ad.month_start = sc.month_start
)
ORDER BY company_id, billing_month
