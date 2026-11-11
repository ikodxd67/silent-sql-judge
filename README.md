# silent-sql-judge

Can an LLM judge, reviewing a pull request to a SQL pipeline, notice a **silent bug** — a change
that still runs and still passes the usual data tests, but returns different numbers?

A small experiment: 40 SQL changes, each presented to the judge as "a pure refactoring".
The ground truth comes from executing both queries, not from anyone's opinion.

## Setup

- **6 base queries** in DuckDB SQL, adapted from my own warehouse projects (card transactions
  with late-arriving versions and an SCD2 client dimension; retail sales with returns and a
  blacklist): deduplication, monthly aggregates, SCD2 interval join, RFM, cohort retention,
  ranking with window functions.
- **40 variants**, each a one-fragment edit of a base query:
  - 23 change the result. 20 of them are *silent*: the result still passes `not_empty`,
    `not_null` and `unique` on the key. 3 are caught by those tests.
  - 17 are real refactorings (result identical; also checked on a second data seed).
- Bug classes: fan-out joins, SCD2, NULL semantics, boundaries (`<` vs `<=`, month arithmetic),
  time zones, deduplication order, grain, join type, aggregates, window functions.
- **Data is synthetic** (seeded generator) and built to contain the edge cases: NULLs, a
  transaction exactly on an interval boundary, a new version arriving in an older batch, a
  December→January cohort, ties in ranking.
- **Judge**: one prompt, verdict `changed` / `equivalent` with confidence and a one-sentence
  reason, as structured JSON. Three context levels, each including the previous one:
  1. `diff` — purpose of the query, original SQL, the diff;
  2. `schema` — plus table definitions with constraints written as comments;
  3. `profile` — plus a profile of the current data (row counts, NULL counts, duplicates).
- **Models**: three local models through Ollama on an 8 GB laptop GPU — `gemma3:4b`,
  `qwen2.5-coder:7b`, `qwen3:8b` (a thinking model). 3 repeats each:
  40 × 3 × 3 × 3 = 1080 calls, 0 failed.

## Results

Share of runs (120 per cell: 69 on changed variants, 51 on equivalent ones).

| Model | Context | Accuracy | Silent bugs caught | False alarms | Tasks with unstable verdict |
|---|---|---|---|---|---|
| gemma3:4b | diff | 0.575 | 1.000 | 1.000 | 0 / 40 |
| gemma3:4b | schema | 0.575 | 1.000 | 1.000 | 0 / 40 |
| gemma3:4b | profile | 0.575 | 1.000 | 1.000 | 0 / 40 |
| qwen2.5-coder:7b | diff | 0.717 | 0.750 | 0.314 | 7 / 40 |
| qwen2.5-coder:7b | schema | 0.642 | 0.717 | 0.431 | 13 / 40 |
| qwen2.5-coder:7b | profile | 0.650 | 0.800 | 0.490 | 16 / 40 |
| qwen3:8b | diff | 0.867 | 0.917 | 0.196 | 8 / 40 |
| qwen3:8b | schema | 0.867 | 0.850 | 0.078 | 11 / 40 |
| qwen3:8b | profile | 0.858 | 0.900 | 0.137 | 6 / 40 |

What I read from this:

1. **The smallest model is not a judge at all.** `gemma3:4b` answered `changed` in all 360
   runs. Its "100% recall" is worthless; recall must always be read next to false alarms.
2. **More context did not make the judges more accurate.** For `qwen3:8b` the schema cut false
   alarms (0.196 → 0.078) but also cut recall on silent bugs (0.917 → 0.850). By majority vote
   over 3 repeats it caught 22 of 23 changes with the diff alone and 19 of 23 with the schema.
   For `qwen2.5-coder:7b` more context mostly added noise (false alarms 0.31 → 0.49).
3. **Constraints can become an excuse.** With the schema in the prompt, `qwen3:8b` in some runs
   approved removing `is_current` from an SCD2 join "because exactly one row per key is
   current" — the very fact that makes the filter necessary. In 5 of 6 runs with schema or
   profile it also accepted `NOT IN` over the blacklist as equivalent to `NOT EXISTS`, calling
   the column non-NULL "by business context", although the schema marks it as nullable.
4. **Confidence carries no signal.** Mean confidence of `qwen3:8b` is 0.980 when right and 0.983
   when wrong; 47 of its 49 wrong verdicts have confidence ≥ 0.9.
5. **A single run is not a verdict.** Even the best configuration gave different answers across
   repeats on 6–11 of 40 tasks.
6. **Hardest for the best model:** fan-out joins (0.75 correct), boundaries (0.78) and NULL
   semantics (0.70). The NULL result agrees with Argus (see below).

Per-class and per-task numbers are in `reports/summary.json`; every raw answer with the judge's
reason is in `reports/runs.jsonl`.

## Limitations

- **Small**: 40 tasks, 6 base queries, one author. Differences between context levels are a
  few tasks and are not statistically tested. This is a pilot, not a benchmark.
- **Small local models only** (4–8B parameters). Nothing here says how frontier models behave.
- **One prompt, one dialect** (DuckDB). No prompt tuning, no few-shot examples.
- "Equivalent" is established by execution on two seeds of generated data plus my reasoning
  about the constraints — not by a formal proof.
- Two equivalent variants (`latest_txn/order_by_version_only`, `customer_rfm/count_order_no`)
  are equivalent only because of a constraint that the `diff` context does not show. The prompt
  tells the judge to answer `changed` when it cannot verify an assumption, so on these two a
  "false alarm" at the `diff` level is arguably the right answer.
- Bugs are injected by hand, one per variant. Real pull requests are larger and mix changes.

## Related work

- Judging equivalence of SQL pairs with LLMs was studied for text-to-SQL evaluation:
  LLM-SQL-Solver (arXiv:2312.10321), arXiv:2506.09359, FLEX (NAACL 2025).
  Argus (arXiv:2510.06663) reports that LLM judges fail on NULL-related cases.
- Benchmarks for data-engineering agents exist: Spider 2.0-DBT, DataClawEval, data-eng-bench.
- What is different here is modest: the judge reviews a *change claimed to be a refactoring*
  in a pipeline query, the bugs follow a data-engineering taxonomy and are filtered by "passes
  the usual tests", and the variable under study is the context given to the judge.

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest -q                  # 46 tests; every label is confirmed by execution
PYTHONPATH=src .venv/Scripts/python -m sqljudge.verify    # writes reports/tasks.jsonl
PYTHONPATH=src .venv/Scripts/python -m sqljudge.judge --models ollama:qwen3:8b --repeats 3 --workers 1
PYTHONPATH=src .venv/Scripts/python -m sqljudge.metrics   # writes reports/summary.json
```

Local models need [Ollama](https://ollama.com) running (`ollama pull qwen3:8b`). A model name
without the `ollama:` prefix is sent to the Claude API instead (needs `ANTHROPIC_API_KEY` in
`.env`); I have not run that path. The judge appends to `reports/runs.jsonl` and resumes after
an interruption.

## Layout

- `src/sqljudge/data.py` — schema and seeded data generator
- `src/sqljudge/tasks.py` — base queries and variants
- `src/sqljudge/verify.py` — execution, labels, the "usual tests"
- `src/sqljudge/judge.py` — prompt and model calls
- `src/sqljudge/metrics.py` — metrics
