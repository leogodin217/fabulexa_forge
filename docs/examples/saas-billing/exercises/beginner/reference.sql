-- Monthly seat bill per company — the reference (clean) form, beginner tier.
-- Conventions: ../CONVENTIONS.md (stages 1, 2 and 8). Runs against exports/dimensional.duckdb.
-- The same CTEs as the advanced reference, cut after the seat and add-on stages.

WITH

-- ---------------------------------------------------------------- calendar
extract_end AS (
    SELECT CAST(date_trunc('month', MAX(started_at)) AS DATE) AS last_month
    FROM fact_lifecycle_interval
),
calendar AS (
    SELECT CAST(m AS DATE)                    AS month_start,
           CAST(m + INTERVAL 1 MONTH AS DATE) AS next_month_start
    FROM (
        SELECT UNNEST(generate_series(
                   (SELECT CAST(date_trunc('month', MIN(created_at)) AS TIMESTAMP) FROM dim_account),
                   (SELECT CAST(last_month AS TIMESTAMP) FROM extract_end),
                   INTERVAL 1 MONTH)) AS m
    )
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
           CAST(date_trunc('month', a.created_at) AS DATE) AS first_month,
           LEAST(
               COALESCE(
                   (SELECT CAST(date_trunc('month', MIN(t.valid_from)) AS DATE)
                    FROM dim_account_terms t
                    WHERE t.account_id = a.account_id AND t.churn_flag = 1),
                   e.last_month),
               e.last_month) AS last_month
    FROM dim_account a
    JOIN dim_plan_terms p ON p.tier = a.plan
    CROSS JOIN extract_end e
),
billing_month AS MATERIALIZED (
    SELECT ac.account_id, c.month_start, c.next_month_start,
           date_diff('day', c.month_start, c.next_month_start) AS days_in_month
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
