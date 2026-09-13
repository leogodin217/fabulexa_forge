-- billing_monthly.sql
-- ***** DO NOT MODIFY WITHOUT TALKING TO FINANCE FIRST *****
-- v3  2017-06  jm   seats prorated by day now (was whole months)
-- v5  2018-02  jm   commit drawdown for ent accts
-- v7  2019-03  dk   credits!! recursive part at the bottom, dont touch
-- v9  2021-11  rp   custom tier override for the big accts (ct table from acct mgmt)
-- v11 2023-08  rp   workflow sku has no tiers, falls thru to std rate (see coalesce)
with recursive
t1 as (
select m.aid, m.cid, m.ym, m.ms, m.nms, m.dim, m.pln, m.cat,
cast(coalesce((select round(sum(date_diff('day', case when dus.valid_from > m.ms then dus.valid_from else m.ms end, case when dus.valid_to is null or dus.valid_to > m.nms then m.nms else dus.valid_to end)) / m.dim, 6) from dim_user du, dim_user_status dus where du.account_id = m.aid and dus.user_id = du.user_id and dus.status in ('active','using') and dus.valid_from < m.nms and (dus.valid_to is null or dus.valid_to > m.ms)),0) as decimal(12,6)) as sts,
cast(cast(coalesce((select round(sum(date_diff('day', case when dus.valid_from > m.ms then dus.valid_from else m.ms end, case when dus.valid_to is null or dus.valid_to > m.nms then m.nms else dus.valid_to end)) / m.dim, 6) from dim_user du, dim_user_status dus where du.account_id = m.aid and dus.user_id = du.user_id and dus.status in ('active','using') and dus.valid_from < m.nms and (dus.valid_to is null or dus.valid_to > m.ms)),0) as decimal(12,6)) * (select dat.seat_price from dim_account_terms dat where dat.account_id = m.aid and dat.valid_from < cast(m.nms as timestamp) and (dat.valid_to is null or dat.valid_to >= cast(m.nms as timestamp))) as decimal(28,14)) as sc,
cast(coalesce((select sum(coalesce(
  -- v9 custom ladder for this acct+sku, if any rows in force at month end
  (select sum(cast(case when (case when n.net > coalesce(ct.to_units, n.net) then coalesce(ct.to_units, n.net) else n.net end) - (case when ct.from_units > 0 then ct.from_units - 1 else 0 end) < 0 then 0 else (case when n.net > coalesce(ct.to_units, n.net) then coalesce(ct.to_units, n.net) else n.net end) - (case when ct.from_units > 0 then ct.from_units - 1 else 0 end) end as decimal(18,6))
     * cast(case when ct.pct_of_list is not null then cast(ds.standard_rate as decimal(10,6)) * ct.pct_of_list * 0.01 else cast(ct.rate as decimal(10,6)) * (select dat.pct_of_list_rate from dim_account_terms dat where dat.account_id = m.aid and dat.valid_from < cast(m.nms as timestamp) and (dat.valid_to is null or dat.valid_to >= cast(m.nms as timestamp))) * 0.01 end as decimal(14,8)))
   from custom_tier ct where ct.account_id = m.aid and ct.sku_id = n.sku and ct.effective_from <= m.nms - interval 1 day and (ct.effective_to is null or ct.effective_to >= m.nms - interval 1 day)),
  -- std ladder
  (select sum(cast(case when (case when n.net > coalesce(drt.to_units, n.net) then coalesce(drt.to_units, n.net) else n.net end) - (case when drt.from_units > 0 then drt.from_units - 1 else 0 end) < 0 then 0 else (case when n.net > coalesce(drt.to_units, n.net) then coalesce(drt.to_units, n.net) else n.net end) - (case when drt.from_units > 0 then drt.from_units - 1 else 0 end) end as decimal(18,6))
     * cast(cast(drt.rate as decimal(10,6)) * (select dat.pct_of_list_rate from dim_account_terms dat where dat.account_id = m.aid and dat.valid_from < cast(m.nms as timestamp) and (dat.valid_to is null or dat.valid_to >= cast(m.nms as timestamp))) * 0.01 as decimal(14,8)))
   from dim_rate_tier drt where drt.sku_id = n.sku and drt.effective_from < cast(m.nms as timestamp) and (drt.effective_to is null or drt.effective_to >= cast(m.nms as timestamp))),
  -- v11 no tiers at all (workflow) -> std rate
  n.net * cast(cast(ds.standard_rate as decimal(10,6)) * (select dat.pct_of_list_rate from dim_account_terms dat where dat.account_id = m.aid and dat.valid_from < cast(m.nms as timestamp) and (dat.valid_to is null or dat.valid_to >= cast(m.nms as timestamp))) * 0.01 as decimal(14,8))
  ))
 from (select v.sku, cast(case when v.sku = 'SKU-001' then (case when v.vol - cast(coalesce((select round(sum(date_diff('day', case when dus.valid_from > m.ms then dus.valid_from else m.ms end, case when dus.valid_to is null or dus.valid_to > m.nms then m.nms else dus.valid_to end)) / m.dim, 6) from dim_user du, dim_user_status dus where du.account_id = m.aid and dus.user_id = du.user_id and dus.status in ('active','using') and dus.valid_from < m.nms and (dus.valid_to is null or dus.valid_to > m.ms)),0) as decimal(12,6)) * coalesce((select dat.allowance_override from dim_account_terms dat where dat.account_id = m.aid and dat.valid_from < cast(m.nms as timestamp) and (dat.valid_to is null or dat.valid_to >= cast(m.nms as timestamp))), case m.pln when 'starter' then 40 when 'professional' then 75 else 100 end) < 0 then 0 else v.vol - cast(coalesce((select round(sum(date_diff('day', case when dus.valid_from > m.ms then dus.valid_from else m.ms end, case when dus.valid_to is null or dus.valid_to > m.nms then m.nms else dus.valid_to end)) / m.dim, 6) from dim_user du, dim_user_status dus where du.account_id = m.aid and dus.user_id = du.user_id and dus.status in ('active','using') and dus.valid_from < m.nms and (dus.valid_to is null or dus.valid_to > m.ms)),0) as decimal(12,6)) * coalesce((select dat.allowance_override from dim_account_terms dat where dat.account_id = m.aid and dat.valid_from < cast(m.nms as timestamp) and (dat.valid_to is null or dat.valid_to >= cast(m.nms as timestamp))), case m.pln when 'starter' then 40 when 'professional' then 75 else 100 end) end) else v.vol end as decimal(18,6)) as net
       from (select fue.sku_id as sku, cast(sum(cast(json_extract_string(fue.context, '$.volume') as decimal(14,2))) as decimal(18,6)) as vol
             from dim_user du, dim_sku ds, fact_usage_event fue left join dim_promotion dp on dp.sku_id = fue.sku_id and dp.kind = 'free_usage' and dp.starts_at <= fue.occurred_at and (dp.ends_at is null or fue.occurred_at < dp.ends_at)
             where du.user_id = fue.user_id and du.account_id = m.aid and ds.sku_id = fue.sku_id and ds.billing_model = 'metered'
               and fue.occurred_at >= cast(m.ms as timestamp) and fue.occurred_at < cast(m.nms as timestamp)
               and fue.occurred_at >= m.cat + (case m.pln when 'starter' then 14 else 30 end) * interval 1 day  -- trial
               and dp.promotion_id is null
             group by fue.sku_id) v) n, dim_sku ds
 where ds.sku_id = n.sku),0) as decimal(28,14)) as uc,
cast(case when m.sso = 'yes' then 150 else 0 end + case when m.sup = 'yes' then 500 else 0 end as decimal(28,14)) as ac,
cast(coalesce((select sum(dat.term_commit) from dim_account_terms dat where dat.account_id = m.aid and dat.term_commit > 0 and dat.valid_from >= cast(m.ms as timestamp) and dat.valid_from < cast(m.nms as timestamp) and coalesce((select dat2.term_commit from dim_account_terms dat2 where dat2.account_id = dat.account_id and dat2.valid_to = dat.valid_from), -1) <> dat.term_commit),0) as decimal(28,14)) as cc,
(select max(dat.valid_from) from dim_account_terms dat where dat.account_id = m.aid and dat.term_commit > 0 and dat.valid_from < cast(m.nms as timestamp) and coalesce((select dat2.term_commit from dim_account_terms dat2 where dat2.account_id = dat.account_id and dat2.valid_to = dat.valid_from), -1) <> dat.term_commit) as ts
from (select da.account_id as aid, da.company_id as cid, da.plan as pln, da.addon_sso as sso, da.addon_support as sup, da.created_at as cat,
             substr(cast(mm.ms as varchar),1,7) as ym, cast(mm.ms as date) as ms,
             cast(case when substr(cast(mm.ms as varchar),6,2) = '12' then cast(cast(substr(cast(mm.ms as varchar),1,4) as int) + 1 as varchar) || '-01-01' else substr(cast(mm.ms as varchar),1,5) || lpad(cast(cast(substr(cast(mm.ms as varchar),6,2) as int) + 1 as varchar), 2, '0') || '-01' end as date) as nms,
             case when substr(cast(mm.ms as varchar),6,2) in ('04','06','09','11') then 30 when substr(cast(mm.ms as varchar),6,2) = '02' then case when cast(substr(cast(mm.ms as varchar),1,4) as int) % 4 = 0 then 29 else 28 end else 31 end as dim
      from dim_account da,
           (select cast(cast((select substr(cast(min(da0.created_at) as varchar),1,7) from dim_account da0) || '-01' as date) + (d1.n + 10 * d2.n) * interval 1 month as date) as ms
            from (select 0 as n union all select 1 union all select 2 union all select 3 union all select 4 union all select 5 union all select 6 union all select 7 union all select 8 union all select 9) d1,
                 (select 0 as n union all select 1 union all select 2 union all select 3 union all select 4 union all select 5 union all select 6 union all select 7 union all select 8 union all select 9) d2) mm
      where substr(cast(mm.ms as varchar),1,7) >= substr(cast(da.created_at as varchar),1,7)
        and substr(cast(mm.ms as varchar),1,7) <= (select case when x.chn is null or x.chn > x.lst then x.lst else x.chn end from (select (select substr(cast(min(dat.valid_from) as varchar),1,7) from dim_account_terms dat where dat.account_id = da.account_id and dat.churn_flag = 1) as chn, (select substr(cast(max(fli.started_at) as varchar),1,7) from fact_lifecycle_interval fli) as lst) x)
     ) m
),
-- v5 commit drawdown: needs the other months so has to sit on top of t1
t2 as (
select t1.aid, t1.cid, t1.ym, t1.ms, t1.nms, t1.pln, t1.sc, t1.uc, t1.ac, t1.cc, t1.ts,
cast(case when t1.ts is null then 0 else (case when t1.uc < (case when (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) < 0 then 0 else (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) end) then t1.uc else (case when (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) < 0 then 0 else (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) end) end) end as decimal(28,14)) as ca,
cast(t1.sc + t1.uc + t1.ac + t1.cc - cast(case when t1.ts is null then 0 else (case when t1.uc < (case when (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) < 0 then 0 else (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) end) then t1.uc else (case when (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) < 0 then 0 else (select dat.term_commit from dim_account_terms dat where dat.account_id = t1.aid and dat.valid_from = t1.ts) - coalesce((select sum(t1b.uc) from t1 t1b where t1b.aid = t1.aid and t1b.ts = t1.ts and t1b.ym < t1.ym),0) end) end) end as decimal(28,14)) as decimal(28,14)) as pct
from t1
),
-- v7 credits. FIFO by issue date, carry the rest, expire per plan (90/180/365).
-- dk: credits expire in the same order they get used so we only need to track where we are in the list:
--     (clt, cli) = last credit fully used up or expired, prt = how much of the NEXT one is already used.
--     starts one month before the first bill so the same step handles every month
t3 as (
select i.aid, cast(i.ms - interval 1 month as date) as ms, cast('1900-01-01' as timestamp) as clt, '' as cli, cast(0 as decimal(28,14)) as prt, cast(0 as decimal(28,14)) as cn
from t2 i where i.ym = (select min(i2.ym) from t2 i2 where i2.aid = i.aid)
union all
select y.aid, y.ms,
  case when y.pct >= y.tot then coalesce((select max(fc.issued_at) from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = y.aid and fc.issued_at < cast(y.nms as timestamp) and (fc.issued_at > y.xt or (fc.issued_at = y.xt and fc.credit_id > y.xi))), y.xt)
       else coalesce((select max(fc.issued_at) from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = y.aid and fc.issued_at < cast(y.nms as timestamp) and (fc.issued_at > y.xt or (fc.issued_at = y.xt and fc.credit_id > y.xi))
                        and (select sum(fc2.amount) from fact_credit fc2, dim_account da2 where da2.company_id = fc2.company_id and da2.account_id = y.aid and (fc2.issued_at > y.xt or (fc2.issued_at = y.xt and fc2.credit_id > y.xi)) and (fc2.issued_at < fc.issued_at or (fc2.issued_at = fc.issued_at and fc2.credit_id <= fc.credit_id))) - y.xprt <= y.pct), y.xt) end as clt,
  case when y.pct >= y.tot then coalesce((select max(fc.credit_id) from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = y.aid and fc.issued_at < cast(y.nms as timestamp) and (fc.issued_at > y.xt or (fc.issued_at = y.xt and fc.credit_id > y.xi))
                        and fc.issued_at = (select max(fc3.issued_at) from fact_credit fc3, dim_account da3 where da3.company_id = fc3.company_id and da3.account_id = y.aid and fc3.issued_at < cast(y.nms as timestamp) and (fc3.issued_at > y.xt or (fc3.issued_at = y.xt and fc3.credit_id > y.xi)))), y.xi)
       else coalesce((select max(fc.credit_id) from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = y.aid and fc.issued_at < cast(y.nms as timestamp) and (fc.issued_at > y.xt or (fc.issued_at = y.xt and fc.credit_id > y.xi))
                        and (select sum(fc2.amount) from fact_credit fc2, dim_account da2 where da2.company_id = fc2.company_id and da2.account_id = y.aid and (fc2.issued_at > y.xt or (fc2.issued_at = y.xt and fc2.credit_id > y.xi)) and (fc2.issued_at < fc.issued_at or (fc2.issued_at = fc.issued_at and fc2.credit_id <= fc.credit_id))) - y.xprt <= y.pct
                        and fc.issued_at = (select max(fc3.issued_at) from fact_credit fc3, dim_account da3 where da3.company_id = fc3.company_id and da3.account_id = y.aid and fc3.issued_at < cast(y.nms as timestamp) and (fc3.issued_at > y.xt or (fc3.issued_at = y.xt and fc3.credit_id > y.xi))
                                              and (select sum(fc4.amount) from fact_credit fc4, dim_account da4 where da4.company_id = fc4.company_id and da4.account_id = y.aid and (fc4.issued_at > y.xt or (fc4.issued_at = y.xt and fc4.credit_id > y.xi)) and (fc4.issued_at < fc3.issued_at or (fc4.issued_at = fc3.issued_at and fc4.credit_id <= fc3.credit_id))) - y.xprt <= y.pct)), y.xi) end as cli,
  cast(case when y.pct >= y.tot then 0
       else y.pct - coalesce((select max(z.cum) from (select (select sum(fc2.amount) from fact_credit fc2, dim_account da2 where da2.company_id = fc2.company_id and da2.account_id = y.aid and (fc2.issued_at > y.xt or (fc2.issued_at = y.xt and fc2.credit_id > y.xi)) and (fc2.issued_at < fc.issued_at or (fc2.issued_at = fc.issued_at and fc2.credit_id <= fc.credit_id))) - y.xprt as cum
                                                     from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = y.aid and fc.issued_at < cast(y.nms as timestamp) and (fc.issued_at > y.xt or (fc.issued_at = y.xt and fc.credit_id > y.xi))) z where z.cum <= y.pct), -y.xprt) end as decimal(28,14)) as prt,  -- nothing closed -> partial keeps growing
  cast(case when y.pct < y.tot then y.pct else y.tot end as decimal(28,14)) as cn
from (
  -- x = state after expiring whatever ran out before this month started, plus this month's pool and the total open
  select x.aid, x.ms, x.nms, x.pct, x.xt, x.xi, x.xprt,
         coalesce((select sum(fc.amount) from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = x.aid and fc.issued_at < cast(x.nms as timestamp) and (fc.issued_at > x.xt or (fc.issued_at = x.xt and fc.credit_id > x.xi))),0) - x.xprt as tot
  from (
    select i.aid, i.ms, i.nms, i.pct,
           coalesce((select max(fc.issued_at) from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = i.aid and fc.issued_at < cast(i.nms as timestamp) and (fc.issued_at > s.clt or (fc.issued_at = s.clt and fc.credit_id > s.cli)) and fc.issued_at + (case i.pln when 'starter' then 90 when 'professional' then 180 else 365 end) * interval 1 day < cast(i.ms as timestamp)), s.clt) as xt,
           coalesce((select max(fc.credit_id) from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = i.aid and fc.issued_at < cast(i.nms as timestamp) and (fc.issued_at > s.clt or (fc.issued_at = s.clt and fc.credit_id > s.cli)) and fc.issued_at + (case i.pln when 'starter' then 90 when 'professional' then 180 else 365 end) * interval 1 day < cast(i.ms as timestamp)
                       and fc.issued_at = (select max(fc3.issued_at) from fact_credit fc3, dim_account da3 where da3.company_id = fc3.company_id and da3.account_id = i.aid and fc3.issued_at < cast(i.nms as timestamp) and (fc3.issued_at > s.clt or (fc3.issued_at = s.clt and fc3.credit_id > s.cli)) and fc3.issued_at + (case i.pln when 'starter' then 90 when 'professional' then 180 else 365 end) * interval 1 day < cast(i.ms as timestamp))), s.cli) as xi,
           case when exists (select 1 from fact_credit fc, dim_account da where da.company_id = fc.company_id and da.account_id = i.aid and fc.issued_at < cast(i.nms as timestamp) and (fc.issued_at > s.clt or (fc.issued_at = s.clt and fc.credit_id > s.cli)) and fc.issued_at + (case i.pln when 'starter' then 90 when 'professional' then 180 else 365 end) * interval 1 day < cast(i.ms as timestamp)) then cast(0 as decimal(28,14)) else s.prt end as xprt
    from t3 s, t2 i
    where i.aid = s.aid and i.ms = cast(s.ms + interval 1 month as date)
  ) x
) y
)
select b.cid as company_id, b.ms as billing_month, b.sc as seat_charge, b.uc as usage_charge, b.ac as addon_charge, b.cc as commit_charge, b.ca as commit_applied, b.cr as credit_applied,
       cast(b.sc + b.uc + b.ac + b.cc - b.ca - b.cr as decimal(12,2)) as total_due
from (select i.cid, i.ms, cast(round(i.sc,2) as decimal(12,2)) as sc, cast(round(i.uc,2) as decimal(12,2)) as uc, cast(round(i.ac,2) as decimal(12,2)) as ac, cast(round(i.cc,2) as decimal(12,2)) as cc, cast(round(i.ca,2) as decimal(12,2)) as ca,
             cast(round(coalesce(l.cn,0),2) as decimal(12,2)) as cr
      from t2 i left join t3 l on l.aid = i.aid and l.ms = i.ms) b
order by 1, 2
