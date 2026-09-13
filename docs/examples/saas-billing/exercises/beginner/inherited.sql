-- seat_bill.sql   jm 2016   monthly seat charge + addons per company (for the CS dashboard)
-- usage / commits / credits are NOT in here, see billing_monthly.sql for the real invoice
select m.cid as company_id, m.ms as billing_month,
       cast(round(m.sts * m.sp, 2) as decimal(12,2)) as seat_charge,
       cast(m.ac as decimal(12,2)) as addon_charge,
       cast(cast(round(m.sts * m.sp, 2) as decimal(12,2)) + cast(m.ac as decimal(12,2)) as decimal(12,2)) as total_due
from (
  select da.company_id as cid, mm.ms,
         cast(coalesce((select round(sum(date_diff('day', case when dus.valid_from > mm.ms then dus.valid_from else mm.ms end, case when dus.valid_to is null or dus.valid_to > mm.nms then mm.nms else dus.valid_to end)) / mm.dim, 6)
                        from dim_user du, dim_user_status dus
                        where du.account_id = da.account_id and dus.user_id = du.user_id and dus.status in ('active','using') and dus.valid_from < mm.nms and (dus.valid_to is null or dus.valid_to > mm.ms)), 0) as decimal(12,6)) as sts,
         (select dat.seat_price from dim_account_terms dat where dat.account_id = da.account_id and dat.valid_from < cast(mm.nms as timestamp) and (dat.valid_to is null or dat.valid_to >= cast(mm.nms as timestamp))) as sp,
         case when da.addon_sso = 'yes' then 150 else 0 end + case when da.addon_support = 'yes' then 500 else 0 end as ac
  from dim_account da,
       (select cast(m0.ms + (d1.n + 10 * d2.n) * interval 1 month as date) as ms,
               cast(m0.ms + (d1.n + 10 * d2.n + 1) * interval 1 month as date) as nms,
               date_diff('day', cast(m0.ms + (d1.n + 10 * d2.n) * interval 1 month as date), cast(m0.ms + (d1.n + 10 * d2.n + 1) * interval 1 month as date)) as dim
        from (select cast(substr(cast(min(da0.created_at) as varchar), 1, 7) || '-01' as date) as ms from dim_account da0) m0,
             (select 0 as n union all select 1 union all select 2 union all select 3 union all select 4 union all select 5 union all select 6 union all select 7 union all select 8 union all select 9) d1,
             (select 0 as n union all select 1 union all select 2 union all select 3 union all select 4 union all select 5 union all select 6 union all select 7 union all select 8 union all select 9) d2) mm
  where mm.ms >= cast(substr(cast(da.created_at as varchar), 1, 7) || '-01' as date)
    and substr(cast(mm.ms as varchar), 1, 7) <= (select substr(cast(max(fli.started_at) as varchar), 1, 7) from fact_lifecycle_interval fli)
    and substr(cast(mm.ms as varchar), 1, 7) <= coalesce((select substr(cast(min(dc.valid_from) as varchar), 1, 7) from dim_company dc where dc.company_id = da.company_id and dc.status = 'churned'), '9999-12')
) m
order by 1, 2
