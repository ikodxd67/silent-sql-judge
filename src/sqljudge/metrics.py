"""Считает метрики из reports/runs.jsonl и пишет reports/summary.json.

python -m sqljudge.metrics

Положительный класс — «изменение меняет результат» (то, что ревью должно поймать).
"""
import json
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


def main() -> None:
    rows = [json.loads(line) for line in (REPORTS / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = summarize(rows)
    (REPORTS / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for r in summary["by_model_context"]:
        print(r)


if __name__ == "__main__":
    main()
