from sqljudge import judge, metrics

TASK = {"purpose": "p", "original_sql": "select a\nfrom t", "changed_sql": "select b\nfrom t"}


def test_prompt_grows_with_context_level():
    diff = judge.build_prompt(TASK, "diff", "PROFILE-TEXT")
    schema = judge.build_prompt(TASK, "schema", "PROFILE-TEXT")
    profile = judge.build_prompt(TASK, "profile", "PROFILE-TEXT")
    assert "-select a" in diff and "+select b" in diff
    assert "CREATE TABLE" not in diff and "CREATE TABLE" in schema
    assert "PROFILE-TEXT" not in schema and "PROFILE-TEXT" in profile


def row(label, verdict, silent=False, task="t", rep=0, **kw):
    return {"model": "m", "context": "diff", "task": task, "rep": rep, "label": label,
            "silent": silent, "class": "c", "verdict": verdict, "input_tokens": 1, "output_tokens": 1, **kw}


def test_score_counts_recall_and_false_alarms():
    rows = [row("changed", "changed", silent=True), row("changed", "equivalent", silent=True),
            row("changed", "changed"), row("equivalent", "changed"), row("equivalent", "equivalent")]
    s = metrics.score(rows)
    assert s == {"n": 5, "accuracy": 0.6, "recall_changed": 0.667, "recall_silent": 0.5, "false_alarm": 0.5}


def test_errors_are_dropped_and_retry_replaces_them():
    rows = [{"model": "m", "context": "diff", "task": "t", "rep": 0, "error": "rate_limit"},
            row("changed", "changed")]
    assert len(metrics.latest_valid(rows)) == 1


def test_unstable_tasks():
    rows = [row("changed", "changed", task="a", rep=0), row("changed", "equivalent", task="a", rep=1),
            row("changed", "changed", task="b", rep=0), row("changed", "changed", task="b", rep=1)]
    assert metrics.unstable_tasks(rows) == 1


def test_majority_and_jury_rules():
    yes = metrics.majority([row("changed", "changed", rep=0), row("changed", "changed", rep=1), row("changed", "equivalent", rep=2)])
    no = metrics.majority([row("changed", "equivalent", rep=i) for i in range(3)])
    assert yes["t"]["verdict"] == "changed" and no["t"]["verdict"] == "equivalent"
    assert metrics.jury([yes, no, no], "majority")[0]["verdict"] == "equivalent"
    assert metrics.jury([yes, no, no], "any")[0]["verdict"] == "changed"
    assert metrics.jury([yes, yes, no], "all")[0]["verdict"] == "equivalent"


def test_mcnemar_counts_discordant_tasks():
    a = {str(i): row("changed", "changed", task=str(i)) for i in range(6)}
    b = {str(i): row("changed", "equivalent" if i < 5 else "changed", task=str(i)) for i in range(6)}
    assert metrics.mcnemar(a, b) == {"only_first_right": 5, "only_second_right": 0, "p": 0.062}
    assert metrics.mcnemar(a, a)["p"] == 1.0
