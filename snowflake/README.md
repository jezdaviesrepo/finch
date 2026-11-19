# FINCH on Snowflake

Loads the FINCH benchmark into a Snowflake database (default `FINCH`).

| Object | Contents |
|---|---|
| `FINCH.BENCHMARK.NL_SQL_PAIRS` | 75,725 questions with `SQL_SQLITE` (original gold SQL) and `SQL_SNOWFLAKE` (converted, fully qualified) |
| `FINCH.<DB_ID>.*` | one schema per FINCH database (33), e.g. `FINCH.FINANCIAL.ACCOUNT` |

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install "snowflake-connector-python[pandas]" sqlglot pyarrow
.venv/bin/python snowflake/load_finch.py --connection default --warehouse MY_WH
```

`--connection` is a name in `~/.snowflake/connections.toml`. Without `--warehouse`, the script uses
your default warehouse, or the first one it can see. The git-lfs data files (~2 GB) are downloaded
into `data/` the first time you run it. Re-running replaces the schemas.

## Conversion notes

- Identifiers are upper-cased, so unquoted Snowflake SQL resolves (`select * from financial.account`).
  Names with spaces still need quotes (`"SALES TEAM"`).
- Column types come from the values SQLite actually stores: whole numbers become `NUMBER`, other
  numbers `FLOAT`, and anything containing text `VARCHAR`. Dates stay `VARCHAR`, as they are in SQLite.
- `SQL_SNOWFLAKE` is produced by `sqlglot` (sqlite → snowflake). All 75,725 queries parse, but
  conversion is best-effort: SQLite-only semantics (date arithmetic like `DATE(x, '-3 month')`,
  case-insensitive `LIKE`, integer division) can produce different results. Treat `SQL_SQLITE` as the gold answer.
- `bird/bird.sqlite` is skipped because no question references it.
