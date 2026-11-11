"""Ставит метки выполнением и пишет reports/tasks.jsonl.

python -m sqljudge.verify
"""
import json
from collections import Counter
from pathlib import Path

from sqljudge import data, tasks

REPORTS = Path(__file__).resolve().parents[2] / "reports"


def rows(con, sql: str) -> list:
    """Результат как отсортированный список строк: порядок строк не важен."""
    out = [tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in con.execute(sql).fetchall()]
    return sorted(out, key=repr)


def naive_tests(con, sql: str, key: tuple[str, ...]) -> list[str]:
    """Обычные тесты dbt на результат: есть строки, ключ не NULL, ключ уникален.
    Возвращает список упавших тестов."""
    k = ", ".join(key)
    n, nulls, dups = con.execute(f"""
        with r as ({sql})
        select (select count(*) from r),
               (select count(*) from r where {' or '.join(f'{c} is null' for c in key)}),
               (select count(*) from (select {k} from r group by {k} having count(*) > 1))
    """).fetchone()
    return [name for name, bad in (("not_empty", n == 0), ("not_null", nulls > 0), ("unique", dups > 0)) if bad]


def build_tasks() -> list[dict]:
    con = data.build(tasks.LATEST_TXN.sql)
    out = []
    for base in tasks.BASES:
        original = rows(con, base.sql)
        assert naive_tests(con, base.sql, base.key) == [], f"{base.name}: оригинал не проходит свои тесты"
        for v in base.variants:
            sql = tasks.changed_sql(base, v)
            failed = naive_tests(con, sql, base.key)
            label = "equivalent" if rows(con, sql) == original else "changed"
            out.append({
                "id": f"{base.name}/{v.name}", "base": base.name, "purpose": base.purpose,
                "class": v.cls, "expect": v.expect, "label": label,
                "failed_tests": failed, "silent": label == "changed" and not failed,
                "note": v.note, "original_sql": base.sql, "changed_sql": sql,
            })
    return out


def main() -> None:
    items = build_tasks()
    REPORTS.mkdir(exist_ok=True)
    with open(REPORTS / "tasks.jsonl", "w", encoding="utf-8") as f:
        for t in items:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print("tasks:", len(items), dict(Counter(t["label"] for t in items)),
          "silent:", sum(t["silent"] for t in items))
    for t in items:
        if t["label"] == "changed" and not t["silent"]:
            print("  caught by naive tests:", t["id"], t["failed_tests"])
        if t["label"] != t["expect"]:
            print("  MISMATCH:", t["id"], "expect", t["expect"], "got", t["label"])


if __name__ == "__main__":
    main()
