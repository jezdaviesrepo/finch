"""Load FINCH into a Snowflake database.

Creates:
  <DB>.<SOURCE_DB>.*           one schema per FINCH SQLite database (33), identifiers upper-cased
  <DB>.BENCHMARK.NL_SQL_PAIRS  the 75,725 question/SQL pairs, with the gold SQL also
                               converted to Snowflake dialect (SQL_SNOWFLAKE), fully qualified

Usage:
  python snowflake/load_finch.py --connection default --warehouse COMPUTE_WH [--database FINCH]
"""
import argparse
import json
import pathlib
import sqlite3
import urllib.request

import pandas as pd
import snowflake.connector
import sqlglot
from snowflake.connector.pandas_tools import write_pandas
from sqlglot import exp

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"  # git-lfs files are fetched here; the repo itself only holds pointers
LFS_URL = "https://media.githubusercontent.com/media/finreflectkg/finch/main/"
CHUNK = 500_000


def real_file(rel):
    """Path to the real content of a repo file, downloading it if the checkout only has an LFS pointer."""
    src = ROOT / rel
    if not src.read_bytes()[:40].startswith(b"version https://git-lfs"):
        return src
    dst = DATA / rel
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {rel}")
        urllib.request.urlretrieve(LFS_URL + rel, dst)
    return dst


def q(name):
    return '"' + name.upper().replace('"', '""') + '"'


def column_types(db, table, cols):
    """Snowflake type per column from the values SQLite actually stores (SQLite typing is per value)."""
    t = q(table)
    probes = ", ".join(
        f"max(typeof({q(c)}) in ('text','blob')), max(typeof({q(c)}) = 'real')" for c in cols
    )
    flags = db.execute(f"select {probes} from {t}").fetchone()
    types = []
    for i in range(len(cols)):
        is_text, is_real = flags[2 * i], flags[2 * i + 1]
        types.append("VARCHAR" if is_text else "FLOAT" if is_real else "NUMBER(38,0)")
    return types


def load_sqlite(sf, database, db_id, path):
    schema = db_id.upper()
    sf.cursor().execute(f"create or replace schema {database}.{q(schema)}")
    db = sqlite3.connect(path)
    db.text_factory = lambda b: b.decode("utf-8", "replace")
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    for table in tables:
        cols = [r[1] for r in db.execute(f"pragma table_info({q(table)})")]
        types = column_types(db, table, cols)
        ddl = ", ".join(f"{q(c)} {t}" for c, t in zip(cols, types))
        sf.cursor().execute(f"create table {database}.{q(schema)}.{q(table)} ({ddl})")
        rows = 0
        for df in pd.read_sql_query(f"select * from {q(table)}", db, chunksize=CHUNK):
            df.columns = [c.upper() for c in df.columns]
            for c, t in zip(df.columns, types):
                df[c] = df[c].astype({"VARCHAR": "string", "FLOAT": "float64", "NUMBER(38,0)": "Int64"}[t])
            write_pandas(sf, df, table.upper(), database=database, schema=schema, quote_identifiers=True)
            rows += len(df)
        print(f"  {schema}.{table.upper()}: {rows:,} rows")


def to_snowflake(sql, schema):
    """SQLite gold SQL -> Snowflake SQL, identifiers upper-cased and tables qualified with their schema."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    ctes = {c.alias_or_name.upper() for c in tree.find_all(exp.CTE)}
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.this.upper())
        ident.set("quoted", True)
    for t in tree.find_all(exp.Table):
        if t.name not in ctes:
            t.set("db", exp.to_identifier(schema, quoted=True))
    return tree.sql(dialect="snowflake")


def load_pairs(sf, database):
    pairs = pd.DataFrame(json.loads(real_file("finch_dataset.json").read_text()))
    out, errs = [], []
    for sql, db_id in zip(pairs["SQL"], pairs["db_id"]):
        try:
            out.append(to_snowflake(sql, db_id.upper()))
            errs.append(None)
        except Exception as e:  # keep the row; record why conversion failed
            out.append(None)
            errs.append(str(e)[:1000])
    pairs = pairs.rename(columns={"db_name": "source", "SQL": "sql_sqlite"})
    pairs["sql_snowflake"] = out
    pairs["conversion_error"] = errs
    pairs.columns = [c.upper() for c in pairs.columns]
    pairs = pairs.astype({c: "string" for c in pairs.columns if c != "QUESTION_ID"})
    sf.cursor().execute(f"create or replace schema {database}.BENCHMARK")
    write_pandas(sf, pairs, "NL_SQL_PAIRS", database=database, schema="BENCHMARK", auto_create_table=True)
    print(f"BENCHMARK.NL_SQL_PAIRS: {len(pairs):,} rows, {sum(e is not None for e in errs)} conversion errors")
    return set(pairs["DB_ID"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", default="default", help="name in ~/.snowflake/connections.toml")
    ap.add_argument("--warehouse")
    ap.add_argument("--database", default="FINCH")
    args = ap.parse_args()

    # no Keychain token cache: a stale cached OAuth token fails instead of re-prompting
    sf = snowflake.connector.connect(connection_name=args.connection, client_store_temporary_credential=False)
    cur = sf.cursor()
    warehouse = args.warehouse or cur.execute("select current_warehouse()").fetchone()[0]
    if not warehouse:  # no default set: take the first warehouse the role can see
        warehouse = cur.execute("show warehouses").fetchone()[0]
    cur.execute(f"use warehouse {q(warehouse)}")
    print(f"using warehouse {warehouse}")
    sf.cursor().execute(f"create database if not exists {args.database}")

    db_ids = load_pairs(sf, args.database)
    for path in sorted((ROOT / "text2sql-db").rglob("*.sqlite")):
        if path.stem not in db_ids:  # e.g. stray bird/bird.sqlite, not referenced by any question
            print(f"skipping {path.relative_to(ROOT)} (no questions use it)")
            continue
        print(path.stem)
        load_sqlite(sf, args.database, path.stem, real_file(str(path.relative_to(ROOT))))


if __name__ == "__main__":
    main()
