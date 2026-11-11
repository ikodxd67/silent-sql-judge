"""LLM-судья: получает изменение в SQL, заявленное как рефакторинг, и решает,
изменит ли оно результат запроса.

python -m sqljudge.judge --models claude-opus-5-5 claude-haiku-5-5 --repeats 3 [--limit 2]

Ответы копятся в reports/runs.jsonl. Повторный запуск продолжает с места
остановки и не платит дважды за уже полученные ответы.
"""
import argparse
import difflib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

from sqljudge import data, tasks, verify

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "reports" / "runs.jsonl"
CONTEXTS = ("diff", "schema", "profile")  # каждый следующий уровень включает предыдущий

SYSTEM = """You review changes to SQL data pipelines (DuckDB dialect).

A pull request is described by its author as a pure refactoring: the query result must stay \
exactly the same (same rows and values; row order does not matter). Decide whether that is true.

Answer "changed" if there can be valid data for which the new query returns a different result \
than the original. Answer "equivalent" if the result is the same for all data allowed by what \
you know about the tables. Do not guess constraints that are not stated: if equivalence depends \
on an assumption you cannot verify from the given context, answer "changed".

Give the verdict, your confidence from 0 to 1, and a one-sentence reason."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["changed", "equivalent"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reason"],
    "additionalProperties": False,
}


def build_prompt(task: dict, context: str, profile: str) -> str:
    diff = "\n".join(difflib.unified_diff(
        task["original_sql"].splitlines(), task["changed_sql"].splitlines(),
        "original.sql", "changed.sql", lineterm=""))
    parts = [f"Purpose of the query: {task['purpose']}",
             f"Original query:\n```sql\n{task['original_sql']}\n```",
             f"Diff of the pull request:\n```diff\n{diff}\n```"]
    if context in ("schema", "profile"):
        parts.append(f"Table definitions and constraints:\n```sql\n{data.SCHEMA.strip()}\n```")
    if context == "profile":
        parts.append(f"Profile of the current data:\n{profile}")
    return "\n\n".join(parts)


def ask(client: anthropic.Anthropic, model: str, prompt: str) -> dict:
    """Один вызов судьи. Запасная модель (fallbacks) намеренно не включена:
    ответ другой модели испортил бы сравнение, отказ записывается как ошибка."""
    try:
        r = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        )
    except anthropic.RateLimitError as e:
        return {"error": f"rate_limit: {e.message}"}
    except anthropic.APIStatusError as e:
        return {"error": f"http_{e.status_code}: {e.message}"}
    except anthropic.APIConnectionError as e:
        return {"error": f"connection: {e}"}
    usage = {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}
    if r.stop_reason != "end_turn":
        return {"error": f"stop_reason={r.stop_reason}", **usage}
    return {**json.loads(next(b.text for b in r.content if b.type == "text")), **usage}


def load_env() -> None:
    """Читает .env проекта: ключ не должен попадать в код и историю команд."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key, value.strip().strip('"'))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--limit", type=int, help="взять только первые N задач (пробный запуск)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    load_env()
    client = anthropic.Anthropic()
    items = verify.build_tasks()[: args.limit]
    profile = data.profile(data.build(tasks.LATEST_TXN.sql))

    done = set()
    if RUNS.exists():
        for line in RUNS.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if "error" not in r:  # ошибочные ответы переспрашиваем
                done.add((r["model"], r["context"], r["task"], r["rep"]))
    jobs = [(m, c, t, rep) for m in args.models for c in CONTEXTS for t in items
            for rep in range(args.repeats) if (m, c, t["id"], rep) not in done]
    print(f"к запуску: {len(jobs)} вызовов, уже есть: {len(done)}")

    lock = threading.Lock()
    RUNS.parent.mkdir(exist_ok=True)

    def run(job):
        model, context, task, rep = job
        out = ask(client, model, build_prompt(task, context, profile))
        row = {"model": model, "context": context, "task": task["id"], "rep": rep,
               "label": task["label"], "silent": task["silent"], "class": task["class"], **out}
        with lock, open(RUNS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return "error" in out

    with ThreadPoolExecutor(args.workers) as pool:
        errors = sum(pool.map(run, jobs))
    print(f"готово, ошибок: {errors}")


if __name__ == "__main__":
    main()
