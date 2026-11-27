# FINCH on Snowflake

Loads the FINCH benchmark into a Snowflake database (default `FINCH`).

| Object | Contents |
|---|---|
| `FINCH.BENCHMARK.NL_SQL_PAIRS` | 75,725 questions with `SQL_SQLITE` (original gold SQL) and `SQL_SNOWFLAKE` (converted, fully qualified) |
| `FINCH.BENCHMARK.EVAL_RESULTS` | one row per question per `evaluate.py` run (`RUN_ID`) |
| `FINCH.BENCHMARK.TABLE_JOINS` | every link between tables (SQLite foreign keys and gold SQL joins), checked against the data, with question counts |
| `FINCH.<DB_ID>.*` | one schema per FINCH database (33), e.g. `FINCH.FINANCIAL.ACCOUNT` |

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install "snowflake-connector-python[pandas]" sqlglot pyarrow
.venv/bin/python snowflake/load_finch.py --connection default --warehouse MY_WH
```

`--connection` is a name in `~/.snowflake/connections.toml`. Without `--warehouse`, the script uses
your default warehouse, or the first one it can see. The git-lfs data files (~2 GB) are downloaded
into `data/` the first time you run it. Re-running replaces the data schemas and `NL_SQL_PAIRS`, and
keeps `EVAL_RESULTS`. `--pairs-only` reloads just `NL_SQL_PAIRS`;
`--keys-only` declares the keys and rewrites `TABLE_JOINS`.

Every script suspends the warehouse when it finishes, even after an error, so it stops costing credits
straight away instead of after its auto-suspend delay.

## Evaluate

```bash
.venv/bin/python snowflake/evaluate.py --model gold --sample 500               # checks the conversion, no model
.venv/bin/python snowflake/evaluate.py --model claude-sonnet-4-5 --sample 100  # a Cortex model
.venv/bin/python snowflake/evaluate.py --model claude-sonnet-4-5 --shots 10 --rerun RUN_ID  # 10 examples, same questions as RUN_ID
```

Each question's SQL runs in Snowflake and its rows are compared with the gold SQL run on the SQLite
copy (the answer key). Row order counts only if the gold SQL has `ORDER BY`; numbers are compared to 5
significant figures. Questions whose gold answer is empty, null or zero are skipped unless you pass
`--keep-empty-gold`. Also skipped: gold SQL that compares `STRFTIME()` text with a number, which SQLite
evaluates as always true or always false (1,643 `accounting` questions, 21 elsewhere). `--shots` adds similar question/SQL pairs from the same
database, never ones sharing a question or query template with the questions under test. Narrow a run with `--db`, `--difficulty`, `--sample` (0 = all) and `--seed`.

## Run history

`results/history.sqlite` (committed) is our own record, written by `history.py`:

| Table | Contents |
|---|---|
| `runs` | one row per `evaluate.py` run: number, label, model, accuracy, and `LAST_CHANGE`, the newest database change it ran against |
| `results` | one row per question per run: question, gold SQL and answer, the model's prompt, SQL and answer, verdict |
| `changes` | one row per change made to the Snowflake database (loads, keys, type changes); `load_finch.py` adds them |

Label each run, e.g. `--label "Run 2: column descriptions added"`. `report.py --runs` writes
`results/report.html`: every run, its questions side by side with the gold answers, and the database
changes made before it.

```bash
.venv/bin/python snowflake/evaluate.py --model claude-sonnet-4-5 --db financial --sample 0 --label "..."
.venv/bin/python snowflake/report.py --runs
```

## Context

`snowflake/context/<db_id>.json` holds logical descriptions of tables and columns (meaning, units, value
ranges, examples; no joins or SQL advice). `context.py` stores them in Snowflake as COMMENTs and logs the
change; `evaluate.py` then shows them in the prompt (tables without descriptions keep the plain one-line form).

```bash
.venv/bin/python snowflake/context.py financial
.venv/bin/python snowflake/history.py note 6 "what this run showed"   # notes shown on the run's page
```

## Scoring

Each question gets two verdicts, both shown on the runs page (switch Strict / Lenient / Both):

- **strict**: exactly the gold answer's columns and rows (FINCH's execution accuracy);
- **lenient**: the gold answer's rows, with every gold column present; extra columns allowed.

Two kinds of questionable gold answers are flagged and scored fairly:

- **Gold arbitrary**: the gold SQL ends `ORDER BY ... LIMIT n` and the cut-off falls inside a tie between different
  answers (detected automatically). Any of the tied answers, or all of them, counts as right.
- **Gold disputed**: a person reviewed the gold answer and found it wrong. `gold_review.json` lists the issue and SQL
  for the answers that also count as right; FINCH's own answer still counts.
- **Gold corrected**: a `gold_review.json` entry with `"replaces_gold": true`, for gold answers that are plainly wrong
  (e.g. the wrong date); only the corrected answer counts.

After editing `gold_review.json`, re-score the stored runs: `python snowflake/rescore.py && python snowflake/report.py --runs`.

`--keys` adds each table's primary and foreign keys to the prompt. `--no-schema` leaves the tables out entirely (instructions and question only), which tests what a model remembers of a
public dataset: Opus 5.5 got 67% of `financial` right that way.

## Disguised copy

`disguise.py` builds `financial_disguised`: `financial` with DISTRICT's columns A2..A16 renamed X01..X15 in a shuffled
order, no descriptions, the same keys, and the gold SQL rewritten (checked to give identical answers). It tests models
on names they can't have memorised.

```bash
.venv/bin/python snowflake/disguise.py --load        # build, check, load into Snowflake with its questions and keys
.venv/bin/python snowflake/context.py financial_disguised
.venv/bin/python snowflake/evaluate.py --model claude-opus-5-5 --db financial_disguised --sample 0 --keys --label "..."
```

## Report

```bash
.venv/bin/python snowflake/report.py                  # run every gold query on SQLite, write data/report_sqlite.html
.venv/bin/python snowflake/report.py --render sqlite  # rebuild the page from the last run
```

## Conversion notes

- Identifiers are upper-cased, so unquoted Snowflake SQL resolves (`select * from financial.account`).
  Names with spaces still need quotes (`"SALES TEAM"`).
- Column types come from the values SQLite actually stores: whole numbers become `NUMBER`, other
  numbers `FLOAT`, and anything containing text `VARCHAR`. Dates stay `VARCHAR`, as they are in SQLite.
- FINCH lower-cased its gold SQL, string literals included. Before converting or running it, the gold SQL
  is repaired: comparisons with a string literal ignore case, and `strftime('%y')` becomes `'%Y'`. For BookSQL
  (`accounting`), whose data stops then, "today" is fixed at 2023-04-26. `SQL_SQLITE` keeps the original text.
- `SQL_SNOWFLAKE` is produced by `sqlglot` (sqlite → snowflake) from the repaired SQL, with `DATE`/`STRFTIME`
  modifiers such as `'-3 month'` or `'start of year'` translated and `LIKE` turned into `ILIKE`. Conversion is
  best-effort: other SQLite-only semantics (integer division, unusual date modifiers) can give different
  results. The repaired SQLite query is the answer key.
- FINCH's `accounting.sqlite` has the 7 BookSQL tables plus 178 empty ones that no gold query uses; those are not loaded.
- `SELECT DISTINCT a ... ORDER BY b` becomes `GROUP BY a ORDER BY MAX(b)` (or `MIN`), as Snowflake rejects the original.
- Primary keys come from SQLite. Every link between tables (SQLite's foreign keys and the joins gold SQL uses) is checked
  against the data: it is a key when the parent side is unique and 95% of child rows find a parent. Only keys become
  foreign keys in Snowflake (`NORELY`, not enforced), with a `UNIQUE` key on the parent where it isn't the primary key.
  Text id columns are turned into numbers where a key needs both sides to match. `TABLE_JOINS` lists every link with its
  verdict (`key`, `partial`, `not unique`, `no values`, `same table`) and why.
- `accounting` primary keys are replaced by ones the data supports (`KEYS` in `load_finch.py`): BookSQL's `(id, businessID,
  name)` is unique only through `id`, and transactions had none. Transactions: `transaction_id + account`. Products and
  payment methods: `id`, plus unique `businessID + name`. Accounts, customers, vendors, employees: `id` only, since business +
  name repeats (82 duplicate accounts; customers, vendors and employees with the same name in one business).
- `bird/bird.sqlite` is skipped because no question references it.
