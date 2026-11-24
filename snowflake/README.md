# FINCH on Snowflake

Loads the FINCH benchmark into a Snowflake database (default `FINCH`).

| Object | Contents |
|---|---|
| `FINCH.BENCHMARK.NL_SQL_PAIRS` | 75,725 questions with `SQL_SQLITE` (original gold SQL) and `SQL_SNOWFLAKE` (converted, fully qualified) |
| `FINCH.BENCHMARK.EVAL_RESULTS` | one row per question per `evaluate.py` run (`RUN_ID`) |
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
keeps `EVAL_RESULTS`. `--pairs-only` reloads just `NL_SQL_PAIRS`.

## Evaluate

```bash
.venv/bin/python snowflake/evaluate.py --model gold --sample 500               # checks the conversion, no model
.venv/bin/python snowflake/evaluate.py --model claude-sonnet-4-5 --sample 100  # a Cortex model
```

Each question's SQL runs in Snowflake and its rows are compared with the gold SQL run on the SQLite
copy (the answer key). Row order counts only if the gold SQL has `ORDER BY`; numbers are compared to 5
significant figures. Questions whose gold answer is empty, null or zero are skipped unless you pass
`--keep-empty-gold`. Narrow a run with `--db`, `--difficulty`, `--sample` (0 = all) and `--seed`.

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
- `bird/bird.sqlite` is skipped because no question references it.
