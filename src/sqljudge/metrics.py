"""Считает метрики из reports/runs.jsonl и пишет reports/summary.json.

python -m sqljudge.metrics

Положительный класс — «изменение меняет результат» (то, что ревью должно поймать).
"""
import json
import math
import random
from collections import defaultdict
from pathlib import Path

REPORTS = Path(__file__).resolve().parents[2] / "reports"


def latest_valid(rows: list[dict]) -> list[dict]:
    """Без ошибочных ответов; на один (модель, контекст, задача, повтор) — последний ответ."""
    last = {}
    for r in rows:
        if "error" not in r:
            last[(r["model"], r["context"], r["task"], r["rep"])] = r
    return list(last.values())


def score(rows: list[dict]) -> dict:
    """recall — доля пойманных изменений; false_alarm — доля рефакторингов, названных изменением."""
    changed = [r for r in rows if r["label"] == "changed"]
    equiv = [r for r in rows if r["label"] == "equivalent"]
    silent = [r for r in changed if r["silent"]]

    def share(part, pred):
        return round(sum(r["verdict"] == pred for r in part) / len(part), 3) if part else None

    return {"n": len(rows),
            "accuracy": round(sum(r["verdict"] == r["label"] for r in rows) / len(rows), 3) if rows else None,
            "recall_changed": share(changed, "changed"),
            "recall_silent": share(silent, "changed"),
            "false_alarm": share(equiv, "changed")}


def unstable_tasks(rows: list[dict]) -> int:
    """Сколько задач получили разные вердикты в повторах."""
    verdicts = defaultdict(set)
    for r in rows:
        verdicts[r["task"]].add(r["verdict"])
    return sum(len(v) > 1 for v in verdicts.values())


def summarize(rows: list[dict]) -> dict:
    rows = latest_valid(rows)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["context"])].append(r)
    out = {"by_model_context": [], "by_class": [], "hardest": []}
    for (model, context), g in sorted(groups.items()):
        out["by_model_context"].append({"model": model, "context": context, **score(g),
                                        "unstable_tasks": unstable_tasks(g),
                                        "input_tokens": sum(r["input_tokens"] for r in g),
                                        "output_tokens": sum(r["output_tokens"] for r in g)})
    by_class = defaultdict(list)
    for r in rows:
        by_class[(r["model"], r["class"])].append(r)
    for (model, cls), g in sorted(by_class.items()):
        out["by_class"].append({"model": model, "class": cls, "n": len(g),
                                "correct": round(sum(r["verdict"] == r["label"] for r in g) / len(g), 3)})
    by_task = defaultdict(list)
    for r in rows:
        by_task[r["task"]].append(r)
    for task, g in by_task.items():
        correct = sum(r["verdict"] == r["label"] for r in g) / len(g)
        if correct < 1:
            out["hardest"].append({"task": task, "label": g[0]["label"], "correct": round(correct, 3), "n": len(g)})
    out["hardest"].sort(key=lambda t: t["correct"])
    return out


def majority(rows: list[dict]) -> dict:
    """Один вердикт на задачу для одного судьи: большинство по повторам (при равенстве — changed)."""
    by_task = defaultdict(list)
    for r in rows:
        by_task[r["task"]].append(r)
    return {t: {**g[0], "verdict": "changed" if 2 * sum(r["verdict"] == "changed" for r in g) >= len(g) else "equivalent"}
            for t, g in by_task.items()}


def jury(members: list[dict], rule: str) -> list[dict]:
    """Жюри: каждый член (словарь из majority) голосует по задаче.
    rule: 'majority' | 'any' (changed, если хоть один сказал changed) | 'all'."""
    out = []
    for t in set.intersection(*(set(m) for m in members)):
        votes = sum(m[t]["verdict"] == "changed" for m in members)
        need = {"majority": len(members) / 2, "any": 0.5, "all": len(members) - 0.5}[rule]
        out.append({**members[0][t], "verdict": "changed" if votes > need else "equivalent"})
    return out


def mcnemar(a: dict, b: dict) -> dict:
    """Парное сравнение двух судей на одних задачах: точный тест Макнемара по задачам,
    где прав только один из двух. p — вероятность такого перекоса при равных судьях."""
    tasks = set(a) & set(b)
    only_a = sum(a[t]["verdict"] == a[t]["label"] != b[t]["verdict"] for t in tasks)
    only_b = sum(b[t]["verdict"] == b[t]["label"] != a[t]["verdict"] for t in tasks)
    n, k = only_a + only_b, min(only_a, only_b)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0
    return {"only_first_right": only_a, "only_second_right": only_b, "p": round(p, 3)}


def accuracy_ci(rows: list[dict], n_boot: int = 2000, seed: int = 0) -> list[float]:
    """95% интервал точности: бутстрап по задачам (задачи выбираются заново с возвращением)."""
    rng = random.Random(seed)
    hits = [r["verdict"] == r["label"] for r in rows]
    means = sorted(sum(rng.choices(hits, k=len(hits))) / len(hits) for _ in range(n_boot))
    return [round(means[int(0.025 * n_boot)], 3), round(means[int(0.975 * n_boot)], 3)]


def task_level(rows: list[dict]) -> dict:
    """Те же вопросы на уровне задач: судьи по большинству повторов, жюри и парные сравнения."""
    rows = latest_valid(rows)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["context"])].append(r)
    maj = {k: majority(g) for k, g in groups.items()}
    models = sorted({m for m, _ in maj})
    contexts = [c for c in ("diff", "schema", "profile") if any(k[1] == c for k in maj)]
    out = {"judges": [], "juries": [], "paired": []}
    for (model, context), m in sorted(maj.items()):
        v = list(m.values())
        out["judges"].append({"model": model, "context": context, **score(v), "accuracy_ci95": accuracy_ci(v)})

    def add(name, members, rule):
        v = jury(members, rule)
        out["juries"].append({"jury": name, "rule": rule, **score(v), "accuracy_ci95": accuracy_ci(v)})

    for model in models:  # один судья, три уровня контекста
        add(f"{model} x 3 contexts", [maj[(model, c)] for c in contexts], "majority")
    thinking = [m for m in models if not all(r["verdict"] == "changed" for r in rows if r["model"] == m)]
    for c in contexts:    # разные модели на одном контексте
        add(f"all models @ {c}", [maj[(m, c)] for m in models], "majority")
        if len(thinking) > 1:
            for rule in ("any", "all"):
                add(f"{' + '.join(thinking)} @ {c}", [maj[(m, c)] for m in thinking], rule)
    for model in models:
        for a, b in (("diff", "schema"), ("diff", "profile"), ("schema", "profile")):
            if (model, a) in maj and (model, b) in maj:
                out["paired"].append({"model": model, "first": a, "second": b, **mcnemar(maj[(model, a)], maj[(model, b)])})
    return out


def main() -> None:
    rows = [json.loads(line) for line in (REPORTS / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = {**summarize(rows), "task_level": task_level(rows)}
    (REPORTS / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for part in (summary["by_model_context"], *summary["task_level"].values()):
        for r in part:
            print(r)


if __name__ == "__main__":
    main()
