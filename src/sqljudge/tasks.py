"""Базовые запросы и их варианты.

Запросы адаптированы под DuckDB из моих проектов:
  bank-lakehouse: dbt/models/dds/fact_card_txn.sql, marts/mart_client_monthly.sql,
                  marts/mart_daily_turnover.sql
  retail-dwh:     sql/marts/60_customer_rfm.sql, sql/analytics/04_cohort_retention.sql,
                  sql/analytics/01_store_monthly_yoy.sql

Вариант = замена одной строки (old -> new) в оригинале. Поле expect — что я
ожидаю; настоящую метку ставит выполнение (verify.py), и тест их сверяет.
Классы: dedup, null, timezone, fanout, scd2, join, boundary, grain, agg, window, filter.
"""
from dataclasses import dataclass, replace


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

# Вторая партия вариантов (2026-11-11): набор расширен с 40 до ~100.
# Данные генератора не менялись, поэтому ответы судей на первые 40 задач остаются в силе.
C, E = "changed", "equivalent"
EXTRA = {
    "latest_txn": (
        Variant("rank_no_ties", E, "refactor",
                "row_number заменён на rank: (txn_id, version) уникальны, ничьих нет.",
                "row_number() over", "rank() over"),
        Variant("order_ascending", C, "dedup",
                "Убран desc: берётся самая старая версия, а не последняя.",
                "order by version desc, export_id desc", "order by version, export_id"),
        Variant("max_version_subquery", E, "refactor",
                "Последняя версия через коррелированный подзапрос с max(version).",
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
from fact_card_txn_delta d
where version = (select max(x.version) from fact_card_txn_delta x where x.txn_id = d.txn_id)"""),
        Variant("global_max_version", C, "dedup",
                "Подзапрос без привязки к txn_id: остаются только операции с самой большой версией во всей таблице.",
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
where version = (select max(version) from fact_card_txn_delta)"""),
        Variant("drop_deleted_rows", C, "dedup",
                "После выбора последней версии удалённые операции выброшены: в таблице их больше нет, а флаг is_deleted нужен дальше.",
                "where rn = 1", "where rn = 1 and not is_deleted"),
        Variant("rn_less_than_2", E, "refactor",
                "rn = 1 записано как rn < 2.",
                "where rn = 1", "where rn < 2"),
        Variant("nulls_last", E, "refactor",
                "Добавлено nulls last: колонки сортировки NOT NULL.",
                "order by version desc, export_id desc", "order by version desc nulls last, export_id desc nulls last"),
    ),
    "client_monthly": (
        Variant("status_not_declined", C, "filter",
                "status = 'posted' стал status <> 'declined': в расчёт попали reversed.",
                "where f.status = 'posted'", "where f.status <> 'declined'"),
        Variant("denominator_known_country", C, "null",
                "В знаменателе доли остались только операции с известной страной.",
                "/ nullif(sum(f.amount_kzt), 0), 4)",
                "/ nullif(sum(f.amount_kzt) filter (where f.merchant_country is not null), 0), 4)"),
        Variant("coalesce_to_kz", E, "refactor",
                "coalesce(country, 'KZ') <> 'KZ': NULL-страна как и раньше не считается иностранной.",
                "filter (where f.merchant_country <> 'KZ')", "filter (where coalesce(f.merchant_country, 'KZ') <> 'KZ')"),
        Variant("make_date", E, "refactor",
                "Первый день месяца собран через make_date.",
                "date_trunc('month', f.txn_date)::date", "make_date(year(f.txn_date), month(f.txn_date), 1)"),
        Variant("utc_plus_6", C, "timezone",
                "Месяц из txn_ts со сдвигом +6 часов, а Алматы — UTC+5.",
                "date_trunc('month', f.txn_date)::date", "date_trunc('month', f.txn_ts + interval '6 hours')::date"),
        Variant("utc_plus_5", E, "refactor",
                "Месяц из txn_ts со сдвигом +5 часов: по схеме txn_date — это дата в Алматы (UTC+5).",
                "date_trunc('month', f.txn_date)::date", "date_trunc('month', f.txn_ts + interval '5 hours')::date"),
        Variant("group_by_expressions", E, "refactor",
                "group by 1, 2 записан выражениями.",
                "group by 1, 2", "group by date_trunc('month', f.txn_date)::date, f.client_id"),
        Variant("having_count", E, "refactor",
                "Добавлен having count(*) > 0: пустых групп не бывает.",
                "group by 1, 2", "group by 1, 2\nhaving count(*) > 0"),
        Variant("count_country", C, "null",
                "count(*) стал count(merchant_country): операции с NULL-страной не считаются.",
                "count(*) as txn_count", "count(f.merchant_country) as txn_count"),
        Variant("count_txn_id", E, "refactor",
                "count(*) стал count(txn_id): колонка NOT NULL.",
                "count(*) as txn_count", "count(f.txn_id) as txn_count"),
        Variant("is_not_true", E, "refactor",
                "not is_deleted записан как is_deleted is not true: колонка NOT NULL.",
                "and not f.is_deleted", "and f.is_deleted is not true"),
    ),
    "daily_turnover": (
        Variant("exclusive_valid_from", C, "boundary",
                ">= valid_from стал >: операция ровно на начале версии остаётся без сегмента.",
                "f.txn_ts >= c.valid_from", "f.txn_ts > c.valid_from"),
        Variant("between", C, "boundary",
                "Интервал записан через BETWEEN: он включает обе границы, операция на стыке попадает в две версии.",
                "f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to", "f.txn_ts between c.valid_from and c.valid_to"),
        Variant("segment_without_coalesce", C, "null",
                "Убран coalesce: в ключе появляется NULL.",
                "coalesce(c.segment, 'unknown') as segment", "c.segment as segment"),
        Variant("count_client_not_distinct", C, "agg",
                "count(distinct client_id) стал count(client_id): считаются операции, а не клиенты.",
                "count(distinct f.client_id)", "count(f.client_id)"),
        Variant("group_by_all", E, "refactor",
                "group by 1, 2, 3 заменён на group by all.",
                "group by 1, 2, 3", "group by all"),
        Variant("left_outer", E, "refactor",
                "left join записан как left outer join.",
                "left join dim_client c", "left outer join dim_client c"),
        Variant("interval_in_where", C, "join",
                "Условие интервала перенесено из ON в WHERE: левое соединение стало внутренним и вдобавок размножает строки до фильтра.",
                "   and f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to\nwhere f.status = 'posted' and not f.is_deleted\n",
                "where f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to\n  and f.status = 'posted' and not f.is_deleted\n"),
        Variant("not_greater_equal", E, "refactor",
                "< valid_to записан как not (>= valid_to): обе колонки NOT NULL.",
                "f.txn_ts < c.valid_to", "not (f.txn_ts >= c.valid_to)"),
        Variant("utc_date", C, "timezone",
                "Дата взята из txn_ts (UTC), а не бизнес-дата Алматы.",
                "    f.txn_date,\n", "    f.txn_ts::date as txn_date,\n"),
        Variant("interval_by_dates", C, "boundary",
                "Интервал сравнивается по датам, а не по времени: операция вечером накануне смены сегмента получает новый сегмент.",
                "f.txn_ts >= c.valid_from and f.txn_ts < c.valid_to",
                "f.txn_date >= c.valid_from::date and f.txn_date < c.valid_to::date"),
    ),
    "customer_rfm": (
        Variant("interval_one_year", E, "refactor",
                "365 дней заменены на 1 год: между 2025-07-01 и 2026-07-01 нет 29 февраля.",
                "interval '365 days'", "interval '1 year'"),
        Variant("interval_52_weeks", C, "boundary",
                "365 дней заменены на 52 недели = 364 дня: окно начинается на день позже.",
                "interval '365 days'", "interval '52 weeks'"),
        Variant("status_not_cancelled", C, "filter",
                "PAID заменён на <> 'CANCELLED': в расчёт попали неоплаченные заказы NEW.",
                "f.order_status = 'PAID'", "f.order_status <> 'CANCELLED'"),
        Variant("drop_not_null_filter", E, "refactor",
                "Убран фильтр hk_customer is not null: такие строки всё равно отсекает внутреннее соединение со справочником.",
                "    where f.hk_customer is not null\n      and f.order_status = 'PAID'", "    where f.order_status = 'PAID'"),
        Variant("ntile_descending", C, "window",
                "Квинтиль считается по убыванию выручки: лучшие клиенты получают 1 вместо 5.",
                "ntile(5) over (order by p.revenue, d.customer_id)", "ntile(5) over (order by p.revenue desc, d.customer_id)"),
        Variant("max_of_date", E, "refactor",
                "max(order_ts)::date записан как max(order_ts::date).",
                "max(order_ts)::date as last_order_date", "max(order_ts::date) as last_order_date"),
        Variant("prefiltered_dim", E, "refactor",
                "is_current вынесен в подзапрос справочника.",
                "join dim_customer d on d.hk_customer = p.hk_customer and d.is_current",
                "join (select * from dim_customer where is_current) d on d.hk_customer = p.hk_customer"),
        Variant("not_in_filtered", E, "refactor",
                "NOT IN с явным отсевом NULL в подзапросе: тогда он равен NOT EXISTS.",
                "where not exists (select 1 from blacklist b where b.customer_id = d.customer_id)",
                "where d.customer_id not in (select b.customer_id from blacklist b where b.customer_id is not null)"),
        Variant("group_by_order_ts", E, "refactor",
                "В группировку добавлен order_ts: по схеме он одинаков у всех строк заказа.",
                "group by f.hk_customer, f.order_no\n", "group by f.hk_customer, f.order_no, f.order_ts\n"),
    ),
    "cohort_retention": (
        Variant("any_status", C, "filter",
                "Убран фильтр оплаченных заказов: когорта считается по любым заказам.",
                "where hk_customer is not null and order_status = 'PAID'", "where hk_customer is not null"),
        Variant("first_value", E, "refactor",
                "min(month) заменён на first_value с сортировкой по месяцу.",
                "min(month) over (partition by hk_customer)", "first_value(month) over (partition by hk_customer order by month)"),
        Variant("running_min", C, "window",
                "В окно добавлен order by month desc: рамка по умолчанию становится нарастающей, и «когорта» равна текущему месяцу.",
                "min(month) over (partition by hk_customer)", "min(month) over (partition by hk_customer order by month desc)"),
        Variant("count_star_denominator", C, "agg",
                "В знаменателе m1 count(*): это все строки когорты за все месяцы, а не её размер.",
                "             / count(distinct hk_customer) filter (where month_no = 0), 1) as m1_pct",
                "             / count(*), 1) as m1_pct"),
        Variant("integer_literal", E, "refactor",
                "100.0 заменено на 100: в DuckDB оператор / всегда делит дробно.",
                "round(100.0 * count(distinct hk_customer) filter (where month_no = 1)",
                "round(100 * count(distinct hk_customer) filter (where month_no = 1)"),
        Variant("month_ge_1", C, "boundary",
                "month_no = 1 стал >= 1: считаются все, кто вернулся когда-либо.",
                "filter (where month_no = 1)", "filter (where month_no >= 1)"),
        Variant("year_month_diff", E, "refactor",
                "Разница месяцев посчитана вручную с учётом года.",
                "datediff('month', cohort, month)", "(year(month) - year(cohort)) * 12 + month(month) - month(cohort)"),
        Variant("swapped_datediff", C, "boundary",
                "Аргументы datediff переставлены: номер месяца отрицательный.",
                "datediff('month', cohort, month)", "datediff('month', month, cohort)"),
        Variant("group_by_1", E, "refactor",
                "group by cohort записан как group by 1.",
                "group by cohort", "group by 1"),
    ),
    "store_monthly": (
        Variant("refund_without_coalesce", C, "null",
                "Убран coalesce: net_amount - NULL = NULL, строки без возврата выпадают из суммы.",
                "sum(f.net_amount - coalesce(r.refund, 0)) as revenue", "sum(f.net_amount - r.refund) as revenue"),
        Variant("inner_join_refunds", C, "join",
                "LEFT JOIN с возвратами стал JOIN: остались только строки с возвратом.",
                "    left join refunds r on", "    join refunds r on"),
        Variant("window_descending", C, "window",
                "Окно отсортировано по убыванию месяца: lag берёт следующий месяц вместо предыдущего.",
                "window w as (partition by store_code order by month)", "window w as (partition by store_code order by month desc)"),
        Variant("lag_explicit_offset", E, "refactor",
                "lag(revenue) записан как lag(revenue, 1).",
                "lag(revenue) over w", "lag(revenue, 1) over w"),
        Variant("growth_other_denominator", C, "agg",
                "Рост делится на текущую выручку, а не на прошлую.",
                "round(100 * (revenue / nullif(lag(revenue) over w, 0) - 1), 1) as mom_pct",
                "round(100 * (1 - lag(revenue) over w / nullif(revenue, 0)), 1) as mom_pct"),
        Variant("left_join_store", E, "refactor",
                "JOIN со справочником стал LEFT JOIN: фильтр по s.format в WHERE всё равно убирает строки без пары.",
                "    join dim_store s on s.store_code", "    left join dim_store s on s.store_code"),
        Variant("rank_tiebreak", C, "window",
                "В сортировку ранга добавлен store_code: магазины с равной выручкой больше не делят место.",
                "order by revenue desc) as rank_in_city", "order by revenue desc, store_code) as rank_in_city"),
        Variant("group_by_all_refunds", E, "refactor",
                "В CTE возвратов group by записан как group by all.",
                "    group by order_no, line_no", "    group by all"),
        Variant("join_on_order_only", C, "fanout",
                "Возвраты соединены только по номеру заказа: возврат одной строки вычитается из всех строк заказа.",
                "left join refunds r on r.order_no = f.order_no and r.line_no = f.line_no",
                "left join refunds r on r.order_no = f.order_no"),
        Variant("sum_minus_sum_coalesce", E, "refactor",
                "Сумма разностей записана как разность сумм с coalesce.",
                "sum(f.net_amount - coalesce(r.refund, 0)) as revenue",
                "sum(f.net_amount) - coalesce(sum(r.refund), 0) as revenue"),
        Variant("sum_minus_sum", C, "null",
                "Разность сумм без coalesce: у магазина без возвратов за месяц выручка становится NULL.",
                "sum(f.net_amount - coalesce(r.refund, 0)) as revenue",
                "sum(f.net_amount) - sum(r.refund) as revenue"),
        Variant("where_order_swapped", E, "refactor",
                "Условия в WHERE переставлены местами.",
                "where s.format <> 'ONLINE' and f.order_status = 'PAID'", "where f.order_status = 'PAID' and s.format <> 'ONLINE'"),
    ),
}

BASES = tuple(replace(b, variants=b.variants + EXTRA[b.name])
              for b in (LATEST_TXN, CLIENT_MONTHLY, DAILY_TURNOVER, CUSTOMER_RFM, COHORT_RETENTION, STORE_MONTHLY))


def changed_sql(base: Base, v: Variant) -> str:
    assert base.sql.count(v.old) == 1, f"{base.name}/{v.name}: фрагмент должен встречаться ровно один раз"
    return base.sql.replace(v.old, v.new)
