"""Схема и сгенерированные данные (seed фиксирован). Данные не настоящие.

Краевые случаи заложены намеренно: без них «тихие» ошибки не проявятся.
"""
import random
from datetime import datetime, timedelta
from decimal import Decimal

import duckdb

# Эту схему видит судья. Ограничения здесь — часть условия задачи.
SCHEMA = """
-- bank: card transactions (timestamps are UTC; business dates are Asia/Almaty, UTC+5)
CREATE TABLE dim_client (              -- SCD2 history of a client
    client_id   INTEGER NOT NULL,
    segment     VARCHAR,                -- nullable
    valid_from  TIMESTAMP NOT NULL,     -- inclusive
    valid_to    TIMESTAMP NOT NULL      -- exclusive; TIMESTAMP '9999-12-31' for the current row
);                                      -- a client may have several rows; intervals do not overlap
CREATE TABLE fact_card_txn_delta (      -- raw exports; a transaction may arrive in several versions
    txn_id            INTEGER NOT NULL,
    version           INTEGER NOT NULL, -- higher = newer; (txn_id, version) is unique
    export_id         INTEGER NOT NULL, -- load batch; a newer version may arrive in an earlier batch
    txn_ts            TIMESTAMP NOT NULL,
    txn_date          DATE NOT NULL,    -- business date in Asia/Almaty
    client_id         INTEGER NOT NULL,
    mcc               INTEGER NOT NULL,
    merchant_country  VARCHAR,          -- nullable
    amount_kzt        DECIMAL(14,2) NOT NULL,
    status            VARCHAR NOT NULL, -- 'posted' | 'declined' | 'reversed'
    is_deleted        BOOLEAN NOT NULL
);
CREATE TABLE fact_card_txn (            -- latest version of each transaction; txn_id is unique
    txn_id INTEGER NOT NULL, txn_ts TIMESTAMP NOT NULL, txn_date DATE NOT NULL,
    client_id INTEGER NOT NULL, mcc INTEGER NOT NULL, merchant_country VARCHAR,
    amount_kzt DECIMAL(14,2) NOT NULL, status VARCHAR NOT NULL, is_deleted BOOLEAN NOT NULL,
    version INTEGER NOT NULL, export_id INTEGER NOT NULL
);
-- retail: sales
CREATE TABLE dim_customer (             -- SCD2; exactly one row per hk_customer has is_current = true
    hk_customer VARCHAR NOT NULL,
    customer_id INTEGER NOT NULL,
    is_current  BOOLEAN NOT NULL
);
CREATE TABLE dim_store (                -- SCD2; exactly one row per store_code has is_current = true
    store_code VARCHAR NOT NULL,
    city       VARCHAR NOT NULL,
    format     VARCHAR,                 -- nullable; 'HYPER' | 'SUPER' | 'ONLINE'
    is_current BOOLEAN NOT NULL
);
CREATE TABLE fact_sales (               -- one row per order line; (order_no, line_no) is unique
    order_no     INTEGER NOT NULL,
    line_no      INTEGER NOT NULL,
    hk_customer  VARCHAR,               -- nullable (anonymous purchase)
    store_code   VARCHAR NOT NULL,
    order_ts     TIMESTAMP NOT NULL,    -- same for all lines of an order
    order_status VARCHAR NOT NULL,      -- 'PAID' | 'NEW' | 'CANCELLED'; same for all lines of an order
    net_amount   DECIMAL(12,2) NOT NULL
);
CREATE TABLE returns (                  -- refunds; one order line may have several refunds
    order_no INTEGER NOT NULL, line_no INTEGER NOT NULL, refund_amount DECIMAL(12,2) NOT NULL
);
CREATE TABLE blacklist (customer_id INTEGER);  -- nullable
"""

FOREVER = datetime(9999, 12, 31)
ALMATY = timedelta(hours=5)


def _money(rng, lo, hi):
    return Decimal(rng.randint(lo * 100, hi * 100)) / 100


def _bank(con, rng):
    clients, bounds_by_client = [], {}
    for cid in range(1, 190):                      # клиентов 190–200 в справочнике нет
        start = datetime(2025, 1, 1) if rng.random() < 0.85 else datetime(2026, 2, 15)
        cuts = sorted(datetime(2026, 2, 1) + timedelta(days=rng.randint(20, 110))
                      for _ in range(rng.choice([0, 0, 1, 2])))
        cuts = [c for c in dict.fromkeys(cuts) if c > start]
        edges = [start, *cuts, FOREVER]
        bounds_by_client[cid] = cuts
        for a, b in zip(edges, edges[1:]):
            seg = None if rng.random() < 0.05 else rng.choice(["mass", "affluent", "premium"])
            clients.append((cid, seg, a, b))
    con.executemany("INSERT INTO dim_client VALUES (?,?,?,?)", clients)

    with_cuts = [c for c, cuts in bounds_by_client.items() if cuts]
    mccs = [5411, 5812, 5541, 4121, 5912, 5311, 7011, 4511, 5732, 5999, 4814, 8062]
    rows = []
    for txn_id in range(1, 3001):
        cid = rng.randint(1, 200)
        ts = datetime(2026, 1, 1) + timedelta(seconds=rng.randint(0, 181 * 86400 - 1))
        if txn_id % 40 == 0:                       # вечер последнего дня месяца по UTC = уже следующий месяц в Алматы
            ts = datetime(2026, rng.randint(1, 5), 28, 20) + timedelta(days=3)
            ts = ts.replace(day=1) - timedelta(hours=rng.randint(1, 4))
        if txn_id % 200 == 0:                      # ровно на границе интервала SCD2
            cid = rng.choice(with_cuts)
            ts = rng.choice(bounds_by_client[cid])
        country = None if rng.random() < 0.05 else rng.choices(["KZ", "TR", "AE", "US", "RU"], [70, 8, 8, 7, 7])[0]
        status = rng.choices(["posted", "declined", "reversed"], [85, 10, 5])[0]
        if txn_id % 200 == 0:
            status = "posted"
        base = dict(txn_id=txn_id, ts=ts, cid=cid, mcc=rng.choice(mccs), country=country,
                    amount=_money(rng, 200, 90000), status=status, deleted=False)
        export = rng.randint(1, 5)
        versions = [(1, export, base)]
        r = rng.random()
        if txn_id % 200 == 0:
            pass
        elif r < 0.12:                             # вторая версия: поменялась сумма или статус
            v2 = dict(base, amount=_money(rng, 200, 90000)) if rng.random() < 0.6 else dict(base, status="reversed")
            versions.append((2, export + 1, v2))
        elif r < 0.15:                             # вторая версия — удаление
            versions.append((2, export + 1, dict(base, deleted=True)))
        elif r < 0.16:
            versions[0][2]["deleted"] = True
        elif r < 0.175:                            # новая версия пришла РАНЬШЕ старой
            versions = [(1, export + 1, base), (2, export, dict(base, amount=_money(rng, 200, 90000)))]
        for ver, exp, v in versions:
            rows.append((txn_id, ver, exp, v["ts"], (v["ts"] + ALMATY).date(), v["cid"], v["mcc"],
                         v["country"], v["amount"], v["status"], v["deleted"]))
    con.executemany("INSERT INTO fact_card_txn_delta VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def _retail(con, rng):
    cust = []
    for i in range(1, 301):
        cust.append((f"c{i:03d}", 1000 + i, True))
        if i % 10 == 0:
            cust.append((f"c{i:03d}", 1000 + i, False))  # старая версия
    con.executemany("INSERT INTO dim_customer VALUES (?,?,?)", cust)

    stores = []
    cities = ["Almaty", "Astana", "Shymkent", "Karaganda"]
    for i in range(1, 13):
        fmt = "ONLINE" if i == 12 else None if i == 11 else rng.choice(["HYPER", "SUPER"])
        stores.append((f"S{i:02d}", cities[i % 4], fmt, True))
        if i in (2, 5, 7):
            stores.append((f"S{i:02d}", cities[i % 4], fmt, False))  # старая версия, тот же город
    stores += [("T1", "Aktobe", "SUPER", True), ("T2", "Aktobe", "SUPER", True), ("T3", "Aktobe", "SUPER", True)]
    con.executemany("INSERT INTO dim_store VALUES (?,?,?,?)", stores)

    sales, order_no = [], 0

    def add(hk, store, ts, status, amounts):
        nonlocal order_no
        order_no += 1
        sales.extend((order_no, n, hk, store, ts, status, a) for n, a in enumerate(amounts, 1))

    span = (datetime(2026, 7, 1) - datetime(2025, 1, 1)).days * 86400
    for _ in range(4000):
        hk = None if rng.random() < 0.08 else f"c{rng.randint(1, 300):03d}"
        ts = datetime(2025, 1, 1) + timedelta(seconds=rng.randint(0, span - 1))
        status = rng.choices(["PAID", "NEW", "CANCELLED"], [80, 8, 12])[0]
        add(hk, f"S{rng.randint(1, 12):02d}", ts, status,
            [_money(rng, 300, 40000) for _ in range(rng.choice([1, 1, 2, 3, 4]))])
    add("c007", "S01", datetime(2025, 7, 1), "PAID", [Decimal("5000.00")])  # ровно на границе окна в 365 дней
    month = datetime(2025, 1, 15)
    while month < datetime(2026, 7, 1):            # T1 и T2 всегда делят первое место
        add(None, "T1", month, "PAID", [Decimal("1000.00")])
        add(None, "T2", month, "PAID", [Decimal("1000.00")])
        add(None, "T3", month, "PAID", [Decimal("500.00")])
        month = (month + timedelta(days=31)).replace(day=15)
    con.executemany("INSERT INTO fact_sales VALUES (?,?,?,?,?,?,?)", sales)

    refunds = []
    for o, n, *_, amount in sales:
        r = rng.random()
        if r < 0.08:
            refunds.append((o, n, (amount / 4).quantize(Decimal("0.01"))))
        if r < 0.02:                               # второй возврат по той же строке
            refunds.append((o, n, (amount / 5).quantize(Decimal("0.01"))))
    con.executemany("INSERT INTO returns VALUES (?,?,?)", refunds)
    con.executemany("INSERT INTO blacklist VALUES (?)", [(1005,), (1042,), (1100,), (1201,), (1299,), (None,)])


def build(latest_txn_sql: str, seed: int = 7) -> duckdb.DuckDBPyConnection:
    """База в памяти. fact_card_txn наполняется оригинальным запросом дедупликации."""
    con = duckdb.connect()
    con.execute(SCHEMA)
    rng = random.Random(seed)
    _bank(con, rng)
    _retail(con, rng)
    con.execute(f"INSERT INTO fact_card_txn {latest_txn_sql}")
    return con


PROFILE_SQL = {
    "dim_client: rows / clients / rows with NULL segment":
        "select count(*), count(distinct client_id), count(*) filter (where segment is null) from dim_client",
    "fact_card_txn_delta: rows / distinct txn_id / txn_id where the highest version is not in the highest export_id":
        """select count(*), count(distinct txn_id),
                  (select count(*) from (select txn_id from fact_card_txn_delta group by 1
                                         having arg_max(version, export_id) <> max(version)))
           from fact_card_txn_delta""",
    "fact_card_txn: rows / is_deleted rows / NULL merchant_country / rows where txn_date month <> UTC month of txn_ts":
        """select count(*), count(*) filter (where is_deleted), count(*) filter (where merchant_country is null),
                  count(*) filter (where date_trunc('month', txn_date) <> date_trunc('month', txn_ts))
           from fact_card_txn""",
    "fact_card_txn: client_id values missing in dim_client / rows with txn_ts equal to some dim_client.valid_to":
        """select (select count(distinct client_id) from fact_card_txn where client_id not in (select client_id from dim_client)),
                  (select count(*) from fact_card_txn f join dim_client c on c.client_id = f.client_id and f.txn_ts = c.valid_to)""",
    "dim_customer: rows / distinct hk_customer / dim_store: rows / distinct store_code / stores with NULL format":
        """select (select count(*) from dim_customer), (select count(distinct hk_customer) from dim_customer),
                  (select count(*) from dim_store), (select count(distinct store_code) from dim_store),
                  (select count(*) from dim_store where format is null and is_current)""",
    "fact_sales: rows / orders / rows with NULL hk_customer / min order_ts / max order_ts":
        "select count(*), count(distinct order_no), count(*) filter (where hk_customer is null), min(order_ts), max(order_ts) from fact_sales",
    "returns: rows / distinct (order_no, line_no) / blacklist: rows / NULL customer_id rows":
        """select (select count(*) from returns), (select count(*) from (select distinct order_no, line_no from returns)),
                  (select count(*) from blacklist), (select count(*) from blacklist where customer_id is null)""",
}


def profile(con) -> str:
    """Короткий профиль данных для судьи: размеры, NULL, дубликаты ключей."""
    return "\n".join(f"- {name}: {' / '.join(str(v) for v in con.execute(sql).fetchone())}"
                     for name, sql in PROFILE_SQL.items())
