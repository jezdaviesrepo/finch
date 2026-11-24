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
import re
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


# BookSQL questions are relative to "today"; its data ends on this date
TODAY = {"accounting": "2023-04-26"}
DOW = ["su", "mo", "tu", "we", "th", "fr", "sa"]  # SQLite 'weekday N': 0 = Sunday


def repair_gold(sql, db_id):
    """Undo damage from FINCH lower-casing its gold SQL, string literals included:
    - 'east bohemia' vs data 'east Bohemia': compare string predicates case-insensitively
    - strftime('%y') was '%Y' (4-digit year)
    and pin CURRENT_DATE / 'now' for BookSQL, whose data stops at TODAY."""
    sql = re.sub(r"(strftime\(\s*')([^']*)'", lambda m: m[1] + m[2].replace("%y", "%Y") + "'", sql, flags=re.I)
    if db_id in TODAY:
        sql = re.sub(r"\bcurrent_date\b|'now'", f"'{TODAY[db_id]}'", sql, flags=re.I)
    tree = sqlglot.parse_one(sql, read="sqlite")
    for node in list(tree.find_all(exp.EQ, exp.NEQ, exp.In)):
        # only direct literals: a literal inside a subquery or function says nothing about the outer column
        if isinstance(node, exp.In):
            col, others = node.this, node.expressions
        elif isinstance(node.left, exp.Literal):
            col, others = node.right, [node.left]
        else:
            col, others = node.left, [node.right]
        if any(o.is_string for o in others) and not isinstance(col, exp.Literal):
            col.replace(exp.Lower(this=col.copy()))
    return tree.sql(dialect="sqlite")


def sqlite_date(args):
    """SQLite DATE(x, 'start of year', '-1 month', 'weekday 0', ...) args -> Snowflake SQL text, or None."""
    base = args[0]
    e = "CURRENT_DATE" if base.is_string and base.name.lower() == "now" else f"CAST({base.sql('snowflake')} AS DATE)"
    for m in args[1:]:
        s = m.name.lower().strip() if m.is_string else ""
        if s.startswith("start of "):
            e = f"DATE_TRUNC('{s[9:]}', {e})"
        elif mm := re.fullmatch(r"([+-]?\d+) (day|month|year)s?", s):
            e = f"DATEADD({mm[2]}, {int(mm[1])}, {e})"
        elif mm := re.fullmatch(r"weekday ([0-6])", s):
            e = f"NEXT_DAY(DATEADD(day, -1, {e}), '{DOW[int(mm[1])]}')"
        else:
            return None  # unknown modifier: leave as-is
    return e


def to_snowflake(sql, schema):
    """SQLite gold SQL -> Snowflake SQL, identifiers upper-cased and tables qualified with their schema."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    for node in list(tree.find_all(exp.Anonymous)):
        name, args = node.name.upper(), node.expressions
        snow = None
        if name == "DATE" and args:
            snow = sqlite_date(args)
        elif name == "STRFTIME" and len(args) > 2 and args[0].is_string:  # STRFTIME(fmt, date, modifiers...)
            d = sqlite_date(args[1:])
            fmt = args[0].name
            for a, b in (("%Y", "YYYY"), ("%m", "MM"), ("%d", "DD"), ("%H", "HH24"), ("%M", "MI"), ("%S", "SS")):
                fmt = fmt.replace(a, b)
            snow = d and f"TO_CHAR({d}, '{fmt}')"
        if snow:
            node.replace(sqlglot.parse_one(snow, read="snowflake"))
    for node in list(tree.find_all(exp.Like)):  # SQLite LIKE ignores case, Snowflake's does not
        node.replace(exp.ILike(this=node.this.copy(), expression=node.expression.copy()))
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
            out.append(to_snowflake(repair_gold(sql, db_id), db_id.upper()))
            errs.append(None)
        except Exception as e:  # keep the row; record why conversion failed
            out.append(None)
            errs.append(str(e)[:1000])
    pairs = pairs.rename(columns={"db_name": "source", "SQL": "sql_sqlite"})
    pairs["sql_snowflake"] = out
    pairs["conversion_error"] = errs
    pairs.columns = [c.upper() for c in pairs.columns]
    pairs = pairs.astype({c: "string" for c in pairs.columns if c != "QUESTION_ID"})
    # not "create or replace": evaluate.py keeps EVAL_RESULTS in this schema
    sf.cursor().execute(f"create schema if not exists {database}.BENCHMARK")
    write_pandas(sf, pairs, "NL_SQL_PAIRS", database=database, schema="BENCHMARK", auto_create_table=True, overwrite=True)
    print(f"BENCHMARK.NL_SQL_PAIRS: {len(pairs):,} rows, {sum(e is not None for e in errs)} conversion errors")
    return set(pairs["DB_ID"])


def connection_args(ap):
    ap.add_argument("--connection", default="default", help="name in ~/.snowflake/connections.toml")
    ap.add_argument("--warehouse")
    ap.add_argument("--database", default="FINCH")


def connect(args):
    # no Keychain token cache: a stale cached OAuth token fails instead of re-prompting
    sf = snowflake.connector.connect(connection_name=args.connection, client_store_temporary_credential=False)
    cur = sf.cursor()
    warehouse = args.warehouse or cur.execute("select current_warehouse()").fetchone()[0]
    if not warehouse:  # no default set: take the first warehouse the role can see
        warehouse = cur.execute("show warehouses").fetchone()[0]
    cur.execute(f"use warehouse {q(warehouse)}")
    print(f"using warehouse {warehouse}")
    return sf


def main():
    ap = argparse.ArgumentParser()
    connection_args(ap)
    ap.add_argument("--pairs-only", action="store_true", help="reload BENCHMARK.NL_SQL_PAIRS only")
    args = ap.parse_args()

    sf = connect(args)
    sf.cursor().execute(f"create database if not exists {args.database}")

    db_ids = load_pairs(sf, args.database)
    if args.pairs_only:
        return
    for path in sorted((ROOT / "text2sql-db").rglob("*.sqlite")):
        if path.stem not in db_ids:  # e.g. stray bird/bird.sqlite, not referenced by any question
            print(f"skipping {path.relative_to(ROOT)} (no questions use it)")
            continue
        print(path.stem)
        load_sqlite(sf, args.database, path.stem, real_file(str(path.relative_to(ROOT))))


if __name__ == "__main__":
    main()
