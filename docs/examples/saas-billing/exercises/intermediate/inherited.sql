-- billing_monthly.sql   (pre-2018 version, before commits + credits went in — still used by the AR report)
-- v3  2017-06  jm   seats prorated by day now (was whole months)
-- v9  2021-11  rp   custom tier override for the big accts (ct table from acct mgmt)  <- backported
-- v11 2023-08  rp   workflow sku has no tiers, falls thru to std rate (see coalesce)  <- backported
select b.cid as company_id, b.ms as billing_month, b.sc as seat_charge, b.uc as usage_charge, b.ac as addon_charge,
       cast(b.sc + b.uc + b.ac as decimal(12,2)) as total_due
from (select t1.cid, t1.ms, cast(round(t1.sc,2) as decimal(12,2)) as sc, cast(round(t1.uc,2) as decimal(12,2)) as uc, cast(round(t1.ac,2) as decimal(12,2)) as ac
      from (
select m.cid, m.ms,
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
cast(case when m.sso = 'yes' then 150 else 0 end + case when m.sup = 'yes' then 500 else 0 end as decimal(28,14)) as ac
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
      ) t1) b
order by 1, 2
