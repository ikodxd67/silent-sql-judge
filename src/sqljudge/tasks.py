"""Базовые запросы и их варианты.

Запросы адаптированы под DuckDB из моих проектов:
  bank-lakehouse: dbt/models/dds/fact_card_txn.sql, marts/mart_client_monthly.sql,
                  marts/mart_daily_turnover.sql
  retail-dwh:     sql/marts/60_customer_rfm.sql, sql/analytics/04_cohort_retention.sql,
                  sql/analytics/01_store_monthly_yoy.sql

Вариант = замена одной строки (old -> new) в оригинале. Поле expect — что я
ожидаю; настоящую метку ставит выполнение (verify.py), и тест их сверяет.
Классы: dedup, null, timezone, fanout, scd2, join, boundary, grain, agg, window.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    name: str
    expect: str   # 'changed' | 'equivalent'
    cls: str      # класс ошибки или 'refactor'
    note: str     # что сделано и почему это (не) меняет результат
    old: str
    new: str


@dataclass(frozen=True)
class Base:
    name: str
    purpose: str            # что должен считать запрос — это видит судья
    key: tuple[str, ...]    # зерно результата: по нему идут тесты unique и not_null
    sql: str
    variants: tuple[Variant, ...]


LATEST_TXN = Base(
    name="latest_txn",
    purpose="Keep the latest version of every card transaction from the raw exports.",
    key=("txn_id",),
    sql="""\
with latest as (
    select *, row_number() over (partition by txn_id order by version desc, export_id desc) as rn
    from fact_card_txn_delta
)
select txn_id, txn_ts, txn_date, client_id, mcc, merchant_country, amount_kzt,
       status, is_deleted, version, export_id
from latest
where rn = 1""",
    variants=(
        Variant("order_by_export", "changed", "dedup",
                "Сортировка по export_id вместо version: новая версия иногда приходит в более ранней выгрузке.",
                "order by version desc, export_id desc", "order by export_id desc, version desc"),
        Variant("filter_deleted_first", "changed", "dedup",
                "Удалённые версии отброшены ДО выбора последней: удалённая операция «воскресает» старой версией.",
                "    from fact_card_txn_delta\n", "    from fact_card_txn_delta\n    where not is_deleted\n"),
        Variant("partition_with_status", "changed", "grain",
                "В partition добавлен status: по операции остаётся по строке на каждый статус.",
                "partition by txn_id order", "partition by txn_id, status order"),
        Variant("qualify", "equivalent", "refactor",
                "Тот же отбор через QUALIFY вместо CTE.",
                """with latest as (
    select *, row_number() over (partition by txn_id order by version desc, export_id desc) as rn
    from fact_card_txn_delta
)
select txn_id, txn_ts, txn_date, client_id, mcc, merchant_country, amount_kzt,
       status, is_deleted, version, export_id
from latest
where rn = 1""",
                """select txn_id, txn_ts, txn_date, client_id, mcc, merchant_country, amount_kzt,
       status, is_deleted, version, export_id
from fact_card_txn_delta
qualify row_number() over (partition by txn_id order by version desc, export_id desc) = 1"""),
        Variant("order_by_version_only", "equivalent", "refactor",
                "export_id убран из сортировки: (txn_id, version) уникальны, второй ключ ничего не решал.",
                "order by version desc, export_id desc", "order by version desc"),
    ),
)

CLIENT_MONTHLY = Base(
    name="client_monthly",
    purpose="Monthly spend per client over posted, non-deleted transactions; months are business "
            "months (Asia/Almaty); foreign_share is the share of spend at merchants known to be outside KZ.",
    key=("month", "client_id"),
    sql="""\
select
    date_trunc('month', f.txn_date)::date as month,
    f.client_id,
    count(*) as txn_count,
    sum(f.amount_kzt) as spend_kzt,
    round(sum(f.amount_kzt) filter (where f.merchant_country <> 'KZ')
          / nullif(sum(f.amount_kzt), 0), 4) as foreign_share,
    count(distinct f.mcc) as mcc_count
from fact_card_txn f
where f.status = 'posted' and not f.is_deleted
group by 1, 2""",
    variants=(
        Variant("month_from_utc_ts", "changed", "timezone",
                "Месяц взят из txn_ts (UTC), а не из txn_date (Алматы): вечерние операции последнего дня уезжают в другой месяц.",
                "date_trunc('month', f.txn_date)::date", "date_trunc('month', f.txn_ts)::date"),
        Variant("null_country_is_foreign", "changed", "null",
                "IS DISTINCT FROM считает NULL-страну иностранной, а <> её пропускал.",
                "where f.merchant_country <> 'KZ'", "where f.merchant_country is distinct from 'KZ'"),
        Variant("count_mcc_not_distinct", "changed", "agg",
                "count(distinct mcc) стал count(mcc): считаются операции, а не разные MCC.",
                "count(distinct f.mcc)", "count(f.mcc)"),
        Variant("drop_deleted_filter", "changed", "agg",
                "Убран фильтр мягко удалённых операций.",
                "where f.status = 'posted' and not f.is_deleted", "where f.status = 'posted'"),
        Variant("case_instead_of_filter", "equivalent", "refactor",
                "FILTER переписан через CASE: NULL-страна по-прежнему не попадает.",
                "sum(f.amount_kzt) filter (where f.merchant_country <> 'KZ')",
                "sum(case when f.merchant_country <> 'KZ' then f.amount_kzt end)"),
        Variant("not_equal_as_not", "equivalent", "refactor",
                "<> переписан как NOT (=): для NULL оба дают NULL, строка не проходит.",
                "where f.merchant_country <> 'KZ'", "where not (f.merchant_country = 'KZ')"),
        Variant("is_deleted_false", "equivalent", "refactor",
                "not is_deleted записан как is_deleted = false: колонка NOT NULL.",
                "and not f.is_deleted", "and f.is_deleted = false"),
    ),
)

DAILY_TURNOVER = Base(
    name="daily_turnover",
    purpose="Daily turnover by MCC and client segment. The segment is the one valid at the moment "
            "of the transaction (SCD2 interval join); transactions without a matching segment go to 'unknown'.",
    key=("txn_date", "mcc", "segment"),
    sql="""\
select
    f.txn_date,
    f.mcc,
    coalesce(c.segment, 'unknown') as segment,
    count(*) as txn_count,
    count(distinct f.client_id) as client_count,
    sum(f.amount_kzt) as turnover_kzt
from fact_card_txn f
left join dim_client c
    on c.client_id = f.client_id
   and f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to
where f.status = 'posted' and not f.is_deleted
group by 1, 2, 3""",
    variants=(
        Variant("join_without_interval", "changed", "fanout",
                "Убрано условие по интервалу: операция умножается на число версий клиента.",
                "    on c.client_id = f.client_id\n   and f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to\n",
                "    on c.client_id = f.client_id\n"),
        Variant("inclusive_valid_to", "changed", "boundary",
                "< valid_to стал <= valid_to: операция ровно на границе попадает в две версии.",
                "f.txn_ts < c.valid_to", "f.txn_ts <= c.valid_to"),
        Variant("inner_join", "changed", "join",
                "LEFT JOIN стал JOIN: пропадают операции клиентов без подходящей версии.",
                "left join dim_client c", "join dim_client c"),
        Variant("current_segment", "changed", "scd2",
                "Берётся текущий сегмент, а не сегмент на момент операции.",
                "   and f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to\n",
                "   and c.valid_to = timestamp '9999-12-31'\n"),
        Variant("filter_moved_to_on", "changed", "join",
                "Фильтр факта перенесён из WHERE в ON левого соединения: непроведённые операции остаются в результате.",
                "   and f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to\nwhere f.status = 'posted' and not f.is_deleted\n",
                "   and f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to\n   and f.status = 'posted' and not f.is_deleted\n"),
        Variant("flipped_comparisons", "equivalent", "refactor",
                "Сравнения записаны в другую сторону.",
                "f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to", "c.valid_from <= f.txn_ts and c.valid_to > f.txn_ts"),
        Variant("case_instead_of_coalesce", "equivalent", "refactor",
                "COALESCE переписан через CASE.",
                "coalesce(c.segment, 'unknown')", "case when c.segment is null then 'unknown' else c.segment end"),
        Variant("prefilter_cte", "equivalent", "refactor",
                "Фильтр факта вынесен в подзапрос до соединения: для левой таблицы LEFT JOIN это то же самое.",
                "from fact_card_txn f\n", "from (select * from fact_card_txn where status = 'posted' and not is_deleted) f\n"),
    ),
)

CUSTOMER_RFM = Base(
    name="customer_rfm",
    purpose="Per customer: last order date, number of paid orders and revenue for the 365 days before "
            "2026-07-01 (inclusive start), with a revenue quintile; blacklisted customers are excluded.",
    key=("customer_id",),
    sql="""\
with orders as (
    select f.hk_customer, f.order_no, max(f.order_ts) as order_ts, sum(f.net_amount) as amount
    from fact_sales f
    where f.hk_customer is not null
      and f.order_status = 'PAID'
      and f.order_ts >= timestamp '2026-07-01' - interval '365 days'
    group by f.hk_customer, f.order_no
),
per_customer as (
    select hk_customer, max(order_ts)::date as last_order_date, count(*) as orders, sum(amount) as revenue
    from orders
    group by hk_customer
)
select d.customer_id, p.last_order_date, p.orders, p.revenue,
       ntile(5) over (order by p.revenue, d.customer_id) as m_score
from per_customer p
join dim_customer d on d.hk_customer = p.hk_customer and d.is_current
where not exists (select 1 from blacklist b where b.customer_id = d.customer_id)""",
    variants=(
        Variant("lines_as_orders", "changed", "grain",
                "В группировку добавлен line_no: считаются строки заказа, а не заказы.",
                "group by f.hk_customer, f.order_no\n", "group by f.hk_customer, f.order_no, f.line_no\n"),
        Variant("exclusive_window_start", "changed", "boundary",
                ">= стал >: заказ ровно на начале окна выпадает.",
                "f.order_ts >= timestamp", "f.order_ts > timestamp"),
        Variant("join_all_versions", "changed", "fanout",
                "Убран is_current: клиент с историей в справочнике попадает дважды.",
                "on d.hk_customer = p.hk_customer and d.is_current", "on d.hk_customer = p.hk_customer"),
        Variant("not_in_with_null", "changed", "null",
                "NOT EXISTS стал NOT IN: в blacklist есть NULL, поэтому условие никогда не истинно.",
                "where not exists (select 1 from blacklist b where b.customer_id = d.customer_id)",
                "where d.customer_id not in (select b.customer_id from blacklist b)"),
        Variant("anti_join", "equivalent", "refactor",
                "NOT EXISTS переписан как LEFT JOIN ... IS NULL: NULL в blacklist ни с чем не соединяется.",
                "where not exists (select 1 from blacklist b where b.customer_id = d.customer_id)",
                "left join blacklist b on b.customer_id = d.customer_id\nwhere b.customer_id is null"),
        Variant("date_literal", "equivalent", "refactor",
                "Выражение с интервалом заменено готовой датой: 2026-07-01 минус 365 дней = 2025-07-01.",
                "timestamp '2026-07-01' - interval '365 days'", "timestamp '2025-07-01'"),
        Variant("count_order_no", "equivalent", "refactor",
                "count(*) стал count(order_no): order_no в CTE не бывает NULL.",
                "count(*) as orders", "count(order_no) as orders"),
    ),
)

COHORT_RETENTION = Base(
    name="cohort_retention",
    purpose="Cohort retention: cohort = month of a customer's first paid purchase; share of the "
            "cohort that purchased again 1, 2 and 3 calendar months later.",
    key=("cohort",),
    sql="""\
with purchases as (
    select distinct hk_customer, date_trunc('month', order_ts)::date as month
    from fact_sales
    where hk_customer is not null and order_status = 'PAID'
),
cohorted as (
    select hk_customer, month, min(month) over (partition by hk_customer) as cohort
    from purchases
),
offsets as (
    select cohort, hk_customer, datediff('month', cohort, month) as month_no
    from cohorted
)
select cohort,
       count(distinct hk_customer) filter (where month_no = 0) as customers,
       round(100.0 * count(distinct hk_customer) filter (where month_no = 1)
             / count(distinct hk_customer) filter (where month_no = 0), 1) as m1_pct,
       round(100.0 * count(distinct hk_customer) filter (where month_no = 2)
             / count(distinct hk_customer) filter (where month_no = 0), 1) as m2_pct,
       round(100.0 * count(distinct hk_customer) filter (where month_no = 3)
             / count(distinct hk_customer) filter (where month_no = 0), 1) as m3_pct
from offsets
group by cohort""",
    variants=(
        Variant("month_number_diff", "changed", "boundary",
                "Разница месяцев без учёта года: декабрь -> январь даёт -11 вместо 1.",
                "datediff('month', cohort, month)", "month(month) - month(cohort)"),
        Variant("days_div_30", "changed", "boundary",
                "Месяцы считаются как дни // 30: 1 января -> 1 марта это 59 дней, то есть «1 месяц».",
                "datediff('month', cohort, month)", "datediff('day', cohort, month) // 30"),
        Variant("no_distinct_in_purchases", "equivalent", "refactor",
                "Убран DISTINCT в первом CTE: дальше везде count(distinct), а min() к дубликатам безразличен.",
                "select distinct hk_customer, date_trunc", "select hk_customer, date_trunc"),
        Variant("count_star_numerator", "equivalent", "refactor",
                "В числителе m1 count(distinct) стал count(*): в purchases пара (клиент, месяц) уникальна.",
                "round(100.0 * count(distinct hk_customer) filter (where month_no = 1)",
                "round(100.0 * count(*) filter (where month_no = 1)"),
        Variant("unfiltered_cohort_size", "equivalent", "refactor",
                "Размер когорты без фильтра month_no = 0: у каждого клиента когорты есть месяц 0.",
                "count(distinct hk_customer) filter (where month_no = 0) as customers",
                "count(distinct hk_customer) as customers"),
    ),
)

STORE_MONTHLY = Base(
    name="store_monthly",
    purpose="Monthly net revenue (sales minus refunds) of offline stores from paid orders, growth "
            "to the previous month, and the store's rank within its city for that month.",
    key=("store_code", "month"),
    sql="""\
with refunds as (
    select order_no, line_no, sum(refund_amount) as refund
    from returns
    group by order_no, line_no
),
monthly as (
    select s.store_code, s.city,
           date_trunc('month', f.order_ts)::date as month,
           sum(f.net_amount - coalesce(r.refund, 0)) as revenue
    from fact_sales f
    join dim_store s on s.store_code = f.store_code and s.is_current
    left join refunds r on r.order_no = f.order_no and r.line_no = f.line_no
    where s.format <> 'ONLINE' and f.order_status = 'PAID'
    group by 1, 2, 3
)
select store_code, city, month, revenue,
       round(100 * (revenue / nullif(lag(revenue) over w, 0) - 1), 1) as mom_pct,
       rank() over (partition by city, month order by revenue desc) as rank_in_city
from monthly
window w as (partition by store_code order by month)""",
    variants=(
        Variant("join_raw_returns", "changed", "fanout",
                "Соединение с сырыми возвратами: строка с двумя возвратами считается дважды.",
                "sum(f.net_amount - coalesce(r.refund, 0)) as revenue\n    from fact_sales f\n    join dim_store s on s.store_code = f.store_code and s.is_current\n    left join refunds r on",
                "sum(f.net_amount - coalesce(r.refund_amount, 0)) as revenue\n    from fact_sales f\n    join dim_store s on s.store_code = f.store_code and s.is_current\n    left join returns r on"),
        Variant("join_all_store_versions", "changed", "fanout",
                "Убран is_current: у магазинов с историей выручка удваивается.",
                "on s.store_code = f.store_code and s.is_current", "on s.store_code = f.store_code"),
        Variant("null_format_included", "changed", "null",
                "coalesce(format, '') <> 'ONLINE' впускает магазины с NULL-форматом, которые раньше отсекались.",
                "where s.format <> 'ONLINE'", "where coalesce(s.format, '') <> 'ONLINE'"),
        Variant("dense_rank", "changed", "window",
                "rank стал dense_rank: после делёжки места номера идут без пропуска.",
                "rank() over (partition by city, month", "dense_rank() over (partition by city, month"),
        Variant("rank_without_month", "changed", "window",
                "Из partition убран month: ранг считается по всем месяцам города сразу.",
                "partition by city, month order by revenue desc", "partition by city order by revenue desc"),
        Variant("not_in_list", "equivalent", "refactor",
                "<> 'ONLINE' записан как NOT IN ('ONLINE'): NULL по-прежнему не проходит.",
                "where s.format <> 'ONLINE'", "where s.format not in ('ONLINE')"),
        Variant("inline_window", "equivalent", "refactor",
                "Именованное окно подставлено на место.",
                "lag(revenue) over w, 0) - 1), 1) as mom_pct",
                "lag(revenue) over (partition by store_code order by month), 0) - 1), 1) as mom_pct"),
        Variant("is_current_in_where", "equivalent", "refactor",
                "is_current перенесён из ON в WHERE: для внутреннего соединения это одно и то же.",
                "on s.store_code = f.store_code and s.is_current\n    left join refunds r on r.order_no = f.order_no and r.line_no = f.line_no\n    where s.format <> 'ONLINE'",
                "on s.store_code = f.store_code\n    left join refunds r on r.order_no = f.order_no and r.line_no = f.line_no\n    where s.is_current and s.format <> 'ONLINE'"),
    ),
)

BASES = (LATEST_TXN, CLIENT_MONTHLY, DAILY_TURNOVER, CUSTOMER_RFM, COHORT_RETENTION, STORE_MONTHLY)


def changed_sql(base: Base, v: Variant) -> str:
    assert base.sql.count(v.old) == 1, f"{base.name}/{v.name}: фрагмент должен встречаться ровно один раз"
    return base.sql.replace(v.old, v.new)
