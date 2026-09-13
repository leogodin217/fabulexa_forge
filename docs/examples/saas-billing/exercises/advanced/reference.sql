-- Monthly bill per company — the reference (clean) form.
-- Conventions: ../CONVENTIONS.md. Runs against exports/dimensional.duckdb.
-- One CTE per billing stage, in the order the spec applies them.

WITH RECURSIVE

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

-- ---------------------------------------------------------------- 3-4 usage
usage AS (                                        -- metered, outside promo and trial
    SELECT u.account_id,
           CAST(date_trunc('month', e.occurred_at) AS DATE) AS month_start,
           e.sku_id,
           CAST(SUM(CAST(json_extract_string(e.context, '$.volume') AS DECIMAL(14,2))) AS DECIMAL(18,6)) AS volume
    FROM fact_usage_event e
    JOIN dim_user u    ON u.user_id = e.user_id
    JOIN dim_sku k     ON k.sku_id = e.sku_id AND k.billing_model = 'metered'
    JOIN account ac    ON ac.account_id = u.account_id
    WHERE e.occurred_at >= ac.created_at + ac.trial_days * INTERVAL 1 DAY
      AND NOT EXISTS (
            SELECT 1 FROM dim_promotion pr
            WHERE pr.sku_id = e.sku_id
              AND pr.kind = 'free_usage'
              AND pr.starts_at <= e.occurred_at
              AND (pr.ends_at IS NULL OR e.occurred_at < pr.ends_at))
    GROUP BY u.account_id, date_trunc('month', e.occurred_at), e.sku_id
),

-- ---------------------------------------------------------------- 5 allowance
net_usage AS MATERIALIZED (
    SELECT us.account_id, us.month_start, us.sku_id,
           CAST(CASE WHEN k.name = 'Core'
                     THEN GREATEST(0, us.volume
                                      - sc.seats * COALESCE(ta.allowance_override, ac.included_units_per_seat))
                     ELSE us.volume
                END AS DECIMAL(18,6)) AS net_volume
    FROM usage us
    JOIN dim_sku k       ON k.sku_id = us.sku_id
    JOIN account ac      ON ac.account_id = us.account_id
    JOIN terms_as_of ta  ON ta.account_id = us.account_id AND ta.month_start = us.month_start  -- also restricts to billed months
    JOIN seat_charge sc  ON sc.account_id = us.account_id AND sc.month_start = us.month_start
),

-- ---------------------------------------------------------------- 6-7 rating
ladder AS MATERIALIZED (                          -- the bands in force for (account, sku, month)
    SELECT nu.account_id, nu.month_start, nu.sku_id,
           ct.from_units, ct.to_units,
           CAST(ct.rate AS DECIMAL(10,6)) AS rate,
           ct.pct_of_list
    FROM net_usage nu
    JOIN billing_month bm ON bm.account_id = nu.account_id AND bm.month_start = nu.month_start
    JOIN custom_tier ct
      ON ct.account_id = nu.account_id
     AND ct.sku_id = nu.sku_id
     AND ct.effective_from <= bm.next_month_start - INTERVAL 1 DAY
     AND (ct.effective_to IS NULL OR ct.effective_to >= bm.next_month_start - INTERVAL 1 DAY)

    UNION ALL

    SELECT nu.account_id, nu.month_start, nu.sku_id,
           rt.from_units, rt.to_units,
           CAST(rt.rate AS DECIMAL(10,6)),
           NULL
    FROM net_usage nu
    JOIN billing_month bm ON bm.account_id = nu.account_id AND bm.month_start = nu.month_start
    JOIN dim_rate_tier rt
      ON rt.sku_id = nu.sku_id
     AND rt.effective_from < CAST(bm.next_month_start AS TIMESTAMP)
     AND (rt.effective_to IS NULL OR rt.effective_to >= CAST(bm.next_month_start AS TIMESTAMP))
    WHERE NOT EXISTS (
            SELECT 1 FROM custom_tier ct
            WHERE ct.account_id = nu.account_id
              AND ct.sku_id = nu.sku_id
              AND ct.effective_from <= bm.next_month_start - INTERVAL 1 DAY
              AND (ct.effective_to IS NULL OR ct.effective_to >= bm.next_month_start - INTERVAL 1 DAY))

    UNION ALL                                     -- no ladder at all: one open band at list rate

    SELECT nu.account_id, nu.month_start, nu.sku_id,
           0, NULL,
           CAST(k.standard_rate AS DECIMAL(10,6)),
           NULL
    FROM net_usage nu
    JOIN billing_month bm ON bm.account_id = nu.account_id AND bm.month_start = nu.month_start
    JOIN dim_sku k ON k.sku_id = nu.sku_id
    WHERE NOT EXISTS (
            SELECT 1 FROM custom_tier ct
            WHERE ct.account_id = nu.account_id
              AND ct.sku_id = nu.sku_id
              AND ct.effective_from <= bm.next_month_start - INTERVAL 1 DAY
              AND (ct.effective_to IS NULL OR ct.effective_to >= bm.next_month_start - INTERVAL 1 DAY))
      AND NOT EXISTS (
            SELECT 1 FROM dim_rate_tier rt
            WHERE rt.sku_id = nu.sku_id
              AND rt.effective_from < CAST(bm.next_month_start AS TIMESTAMP)
              AND (rt.effective_to IS NULL OR rt.effective_to >= CAST(bm.next_month_start AS TIMESTAMP)))
),
rated_usage AS (
    SELECT nu.account_id, nu.month_start, nu.sku_id,
           CAST(SUM(
               CAST(GREATEST(0, LEAST(nu.net_volume, COALESCE(l.to_units, nu.net_volume))
                                - GREATEST(l.from_units - 1, 0)) AS DECIMAL(18,6))
               * CAST(CASE WHEN l.pct_of_list IS NOT NULL
                           THEN CAST(k.standard_rate AS DECIMAL(10,6)) * l.pct_of_list * 0.01
                           ELSE l.rate * ta.pct_of_list_rate * 0.01
                      END AS DECIMAL(14,8))
           ) AS DECIMAL(28,14)) AS amount
    FROM net_usage nu
    JOIN ladder l        ON l.account_id = nu.account_id AND l.month_start = nu.month_start AND l.sku_id = nu.sku_id
    JOIN dim_sku k       ON k.sku_id = nu.sku_id
    JOIN terms_as_of ta  ON ta.account_id = nu.account_id AND ta.month_start = nu.month_start
    GROUP BY nu.account_id, nu.month_start, nu.sku_id
),
usage_charge AS MATERIALIZED (
    SELECT account_id, month_start, CAST(SUM(amount) AS DECIMAL(28,14)) AS usage_charge
    FROM rated_usage
    GROUP BY account_id, month_start
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
),

-- ---------------------------------------------------------------- 9 commit
term AS (                                         -- one row per commit term
    SELECT account_id, term_start, term_commit,
           LEAD(term_start) OVER (PARTITION BY account_id ORDER BY term_start) AS term_end
    FROM (
        SELECT account_id, valid_from AS term_start, term_commit,
               LAG(term_commit) OVER (PARTITION BY account_id ORDER BY valid_from) AS previous_commit
        FROM dim_account_terms
    )
    WHERE term_commit > 0
      AND (previous_commit IS NULL OR previous_commit <> term_commit)
),
commit_charge AS (
    SELECT bm.account_id, bm.month_start,
           CAST(COALESCE(SUM(t.term_commit), 0) AS DECIMAL(28,14)) AS commit_charge
    FROM billing_month bm
    LEFT JOIN term t
      ON t.account_id = bm.account_id
     AND t.term_start >= CAST(bm.month_start AS TIMESTAMP)
     AND t.term_start <  CAST(bm.next_month_start AS TIMESTAMP)
    GROUP BY bm.account_id, bm.month_start
),
commit_applied AS (
    SELECT account_id, month_start,
           CAST(LEAST(usage_charge,
                      GREATEST(0, term_commit - COALESCE(applied_before, 0))) AS DECIMAL(28,14)) AS commit_applied
    FROM (
        SELECT bm.account_id, bm.month_start, t.term_commit,
               COALESCE(uc.usage_charge, 0) AS usage_charge,
               SUM(COALESCE(uc.usage_charge, 0)) OVER (
                   PARTITION BY bm.account_id, t.term_start
                   ORDER BY bm.month_start
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS applied_before
        FROM billing_month bm
        JOIN term t
          ON t.account_id = bm.account_id
         AND t.term_start < CAST(bm.next_month_start AS TIMESTAMP)
         AND (t.term_end IS NULL OR t.term_end >= CAST(bm.next_month_start AS TIMESTAMP))
        LEFT JOIN usage_charge uc ON uc.account_id = bm.account_id AND uc.month_start = bm.month_start
    )
),

-- ---------------------------------------------------------------- pre-credit invoice
invoice AS MATERIALIZED (
    SELECT bm.account_id, bm.month_start, bm.next_month_start,
           sc.seat_charge,
           COALESCE(uc.usage_charge, 0)   AS usage_charge,
           ad.addon_charge,
           cc.commit_charge,
           COALESCE(ca.commit_applied, 0) AS commit_applied,
           CAST(sc.seat_charge + COALESCE(uc.usage_charge, 0) + ad.addon_charge
              + cc.commit_charge - COALESCE(ca.commit_applied, 0) AS DECIMAL(28,14)) AS pre_credit_total
    FROM billing_month bm
    JOIN seat_charge sc         USING (account_id, month_start)
    LEFT JOIN usage_charge uc   USING (account_id, month_start)
    JOIN addon_charge ad        USING (account_id, month_start)
    JOIN commit_charge cc       USING (account_id, month_start)
    LEFT JOIN commit_applied ca USING (account_id, month_start)
),

-- ---------------------------------------------------------------- 10 credits
credit AS (
    SELECT f.credit_id, ac.account_id, f.issued_at,
           CAST(f.amount AS DECIMAL(28,14)) AS amount,
           f.issued_at + ac.credit_validity_days * INTERVAL 1 DAY AS expires_at
    FROM fact_credit f
    JOIN dim_account a ON a.company_id = f.company_id
    JOIN account ac    ON ac.account_id = a.account_id
),
-- Month-by-month ledger. One cursor row per account per month (credit_id NULL)
-- keeps the recursion advancing through months with no open credit; credit
-- rows carry what remains. FIFO within a month is the running sum of earlier
-- credits' remainders. Credits issued in a month enter in that month's step.
ledger AS (
    -- anchor: a cursor one month before each account's first bill, so the
    -- recursive step below handles every billed month the same way
    SELECT ac.account_id,
           CAST(ac.first_month - INTERVAL 1 MONTH AS DATE) AS month_start,
           CAST(NULL AS VARCHAR)        AS credit_id,
           CAST(NULL AS TIMESTAMP)      AS issued_at,
           CAST(NULL AS DECIMAL(28,14)) AS remaining_before,
           CAST(NULL AS DECIMAL(28,14)) AS consumed
    FROM account ac

    UNION ALL

    SELECT x.account_id, x.month_start, x.credit_id, x.issued_at,
           CAST(x.remaining_before AS DECIMAL(28,14)),
           CAST(LEAST(x.remaining_before, GREATEST(0, x.pre_credit_total
                 - COALESCE(SUM(x.remaining_before) OVER (
                       PARTITION BY x.account_id
                       ORDER BY x.issued_at NULLS FIRST, x.credit_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0))) AS DECIMAL(28,14))
    FROM (
        -- the cursor, and credits carried forward with something left and not yet expired
        SELECT i.account_id, i.month_start, l.credit_id, l.issued_at,
               l.remaining_before - l.consumed AS remaining_before,
               i.pre_credit_total
        FROM ledger l
        JOIN invoice i
          ON i.account_id = l.account_id
         AND i.month_start = CAST(l.month_start + INTERVAL 1 MONTH AS DATE)
        LEFT JOIN credit c ON c.credit_id = l.credit_id
        WHERE l.credit_id IS NULL
           OR (l.remaining_before - l.consumed > 0
               AND c.expires_at >= CAST(i.month_start AS TIMESTAMP))

        UNION ALL

        -- credits issued this month, entering the ledger
        SELECT i.account_id, i.month_start, c.credit_id, c.issued_at,
               c.amount,
               i.pre_credit_total
        FROM ledger l
        JOIN invoice i
          ON i.account_id = l.account_id
         AND i.month_start = CAST(l.month_start + INTERVAL 1 MONTH AS DATE)
        JOIN credit c
          ON c.account_id = i.account_id
         AND c.issued_at >= CAST(i.month_start AS TIMESTAMP)
         AND c.issued_at <  CAST(i.next_month_start AS TIMESTAMP)
        WHERE l.credit_id IS NULL
    ) x
),
credit_applied AS (
    SELECT account_id, month_start, CAST(SUM(consumed) AS DECIMAL(28,14)) AS credit_applied
    FROM ledger
    WHERE credit_id IS NOT NULL
    GROUP BY account_id, month_start
)

-- ---------------------------------------------------------------- the bill
SELECT company_id, billing_month,
       seat_charge, usage_charge, addon_charge, commit_charge, commit_applied, credit_applied,
       CAST(seat_charge + usage_charge + addon_charge + commit_charge
            - commit_applied - credit_applied AS DECIMAL(12,2)) AS total_due
FROM (
    SELECT ac.company_id,
           i.month_start                                                AS billing_month,
           CAST(ROUND(i.seat_charge, 2)                  AS DECIMAL(12,2)) AS seat_charge,
           CAST(ROUND(i.usage_charge, 2)                 AS DECIMAL(12,2)) AS usage_charge,
           CAST(ROUND(i.addon_charge, 2)                 AS DECIMAL(12,2)) AS addon_charge,
           CAST(ROUND(i.commit_charge, 2)                AS DECIMAL(12,2)) AS commit_charge,
           CAST(ROUND(i.commit_applied, 2)               AS DECIMAL(12,2)) AS commit_applied,
           CAST(ROUND(COALESCE(cr.credit_applied, 0), 2) AS DECIMAL(12,2)) AS credit_applied
    FROM invoice i
    JOIN account ac             ON ac.account_id = i.account_id
    LEFT JOIN credit_applied cr ON cr.account_id = i.account_id AND cr.month_start = i.month_start
)
ORDER BY company_id, billing_month
