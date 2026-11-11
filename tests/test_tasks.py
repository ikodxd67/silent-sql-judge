import pytest

from sqljudge import data, tasks, verify

TASKS = verify.build_tasks()


@pytest.mark.parametrize("t", TASKS, ids=lambda t: t["id"])
def test_label_is_confirmed_by_execution(t):
    assert t["label"] == t["expect"]


def test_equivalent_variants_hold_on_another_seed():
    """Эквивалентность не должна быть случайностью одного набора данных."""
    con = data.build(tasks.LATEST_TXN.sql, seed=123)
    for base in tasks.BASES:
        original = verify.rows(con, base.sql)
        for v in base.variants:
            if v.expect == "equivalent":
                assert verify.rows(con, tasks.changed_sql(base, v)) == original, f"{base.name}/{v.name}"


def test_naive_tests_catch_duplicates_and_nulls():
    con = data.build(tasks.LATEST_TXN.sql)
    assert verify.naive_tests(con, "select 1 as k union all select 1", ("k",)) == ["unique"]
    assert verify.naive_tests(con, "select null::int as k", ("k",)) == ["not_null"]
    assert verify.naive_tests(con, "select 1 as k where false", ("k",)) == ["not_empty"]
