# silent-sql-judge

Can an LLM judge, reviewing a pull request to a SQL pipeline, notice a **silent bug** — a change
that still runs and still passes the usual data tests, but returns different numbers?

A small experiment: 98 SQL changes, each presented to the judge as "a pure refactoring".
The ground truth comes from executing both queries, not from anyone's opinion.

## Setup

- **6 base queries** in DuckDB SQL, adapted from my own warehouse projects (card transactions
  with late-arriving versions and an SCD2 client dimension; retail sales with returns and a
  blacklist): deduplication, monthly aggregates, SCD2 interval join, RFM, cohort retention,
  ranking with window functions.
- **98 variants**, each a one-fragment edit of a base query:
  - 52 change the result. 48 of them are *silent*: the result still passes `not_empty`,
    `not_null` and `unique` on the key. 4 are caught by those tests.
  - 46 are real refactorings (result identical; also checked on a second data seed).
- Bug classes: fan-out joins, SCD2, NULL semantics, boundaries (`<` vs `<=`, month arithmetic),
  time zones, deduplication order, grain, join type, filters, aggregates, window functions.
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
  98 × 3 × 3 × 3 = 2646 calls; one malformed answer was re-run.

## Results

### Per run

Share of runs (294 per cell: 156 on changed variants, 138 on equivalent ones).

| Model | Context | Accuracy | Silent bugs caught | False alarms | Tasks with unstable verdict |
|---|---|---|---|---|---|
| gemma3:4b | diff | 0.531 | 1.000 | 1.000 | 0 / 98 |
| gemma3:4b | schema | 0.531 | 1.000 | 1.000 | 0 / 98 |
| gemma3:4b | profile | 0.531 | 1.000 | 1.000 | 0 / 98 |
| qwen2.5-coder:7b | diff | 0.673 | 0.806 | 0.471 | 23 / 98 |
| qwen2.5-coder:7b | schema | 0.653 | 0.764 | 0.464 | 34 / 98 |
| qwen2.5-coder:7b | profile | 0.650 | 0.854 | 0.558 | 31 / 98 |
| qwen3:8b | diff | 0.837 | 0.882 | 0.217 | 17 / 98 |
| qwen3:8b | schema | 0.881 | 0.847 | 0.072 | 21 / 98 |
| qwen3:8b | profile | 0.864 | 0.840 | 0.094 | 17 / 98 |

### Per task (majority of 3 repeats), with 95% bootstrap interval

| Judge | Accuracy [95% CI] | Changes caught (of 52) | False alarms (of 46) |
|---|---|---|---|
| qwen3:8b @ diff | 0.867 [0.796, 0.929] | 0.904 | 0.174 |
| qwen3:8b @ schema | 0.898 [0.837, 0.959] | 0.846 | 0.043 |
| qwen3:8b @ profile | 0.888 [0.816, 0.949] | 0.827 | 0.043 |
| qwen2.5-coder:7b @ diff | 0.684 [0.592, 0.776] | 0.808 | 0.457 |
| jury qwen2.5 + qwen3 @ schema, rule `all` | 0.816 [0.735, 0.898] | 0.692 | 0.043 |
| jury qwen2.5 + qwen3 @ schema, rule `any` | 0.755 [0.663, 0.837] | 0.904 | 0.413 |
| jury qwen3 over 3 contexts, majority | 0.898 [0.837, 0.949] | 0.846 | 0.043 |

The interval comes from resampling tasks with replacement; paired comparisons use the exact
McNemar test on tasks where only one of two judges is right. Full tables: `task_level` in
`reports/summary.json`.

What I read from this:

1. **The smallest model is not a judge at all.** `gemma3:4b` answered `changed` in all 882
   runs. Its "100% recall" is worthless; recall must always be read next to false alarms.
2. **Context trades recall for fewer false alarms, and the overall gain is not significant.**
   For `qwen3:8b` the schema cut false alarms from 8 to 2 of 46 refactorings, but the judge
   also caught fewer real changes (47 → 44 of 52). Accuracy rose from 0.867 to 0.898, yet the
   paired test does not separate the two (5 tasks right only with the diff, 8 only with the
   schema, p = 0.58), and the 95% intervals overlap. For `qwen2.5-coder:7b` more context mostly
   added noise (false alarms per run 0.47 → 0.56 with the profile).
3. **Constraints can become an excuse.** With the schema or profile in the prompt, `qwen3:8b`
   approved removing `is_current` from SCD2 joins "because exactly one row per key is
   current" — the very fact that makes the filter necessary. In 5 of 6 runs with schema or
   profile it also accepted `NOT IN` over the blacklist as equivalent to `NOT EXISTS`, although
   the schema marks the column as nullable.
4. **Confidence carries no signal.** Mean confidence of `qwen3:8b` is 0.982 when right and 0.974
   when wrong; 117 of its 123 wrong verdicts have confidence ≥ 0.9.
5. **A single run is not a verdict.** Even the best model changed its answer across repeats on
   17–21 of 98 tasks.
6. **Juries did not beat the best single judge.** Requiring both models to say `changed` keeps
   false alarms low but loses a third of the bugs; accepting either one's alarm catches 90% of
   bugs but raises false alarms to 41%. Voting `qwen3:8b` over its three contexts gives the same
   result as the schema context alone. With one weak model, the jury inherits its noise.
7. **Hardest for the best model** (share of correct runs): join type (0.69), window functions
   (0.74), fan-out joins (0.76) and NULL semantics (0.76). The NULL result agrees with Argus
   (see below).

Per-class and per-task numbers are in `reports/summary.json`; every raw answer with the judge's
reason is in `reports/runs.jsonl`.

## Limitations

- **Small**: 98 tasks, 6 base queries, one author. The bootstrap intervals are about ±0.06, so
  differences of a few points between contexts are within noise. This is a pilot, not a benchmark.
- **Small local models only** (4–8B parameters). Nothing here says how frontier models behave.
- **One prompt, one dialect** (DuckDB). No prompt tuning, no few-shot examples.
- "Equivalent" is established by execution on two seeds of generated data plus my reasoning
  about the constraints — not by a formal proof.
- Some equivalent variants (for example `latest_txn/order_by_version_only`,
  `latest_txn/rank_no_ties`) are equivalent only because of a constraint that the `diff` context
  does not show. The prompt tells the judge to answer `changed` when it cannot verify an
  assumption, so on these a "false alarm" at the `diff` level is arguably the right answer.
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
.venv/Scripts/python -m pytest -q                  # 106 tests; every label is confirmed by execution
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
