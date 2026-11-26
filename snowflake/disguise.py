"""Disguised copies of a FINCH database, to test models on names they can't have memorised.

financial -> financial_disguised: DISTRICT's columns A2..A16 are renamed X01..X15 in a shuffled order, and the table's
columns are reordered to match, so neither the name nor the position gives a column away. No descriptions (comments).
Everything else is unchanged. The gold SQL is rewritten with the same renaming, and every rewritten query is checked to
give exactly the same answer as the original. No question mentions an A column, so the questions are unchanged.

  python snowflake/disguise.py          # build data/disguised/financial_disguised.sqlite and check the gold SQL
  python snowflake/disguise.py --load   # also load it into Snowflake (schema FINANCIAL_DISGUISED), its questions and keys
  python snowflake/disguise.py --keys   # only declare its keys and TABLE_JOINS rows
"""
import argparse
import json
import random
import shutil
import sqlite3

import pandas as pd
import sqlglot
from snowflake.connector.pandas_tools import write_pandas
from sqlglot import exp

from evaluate import same_result, sqlite_rows
from history import log_change
from load_finch import (DATA, apply_keys, connect, connection_args, lite, load_sqlite, real_file, repair_gold, sqlite_file,
                        table_joins, table_keys, to_snowflake)

SOURCE, NAME = "financial", "financial_disguised"
ID_OFFSET = 1_000_000  # disguised question id = original id + ID_OFFSET
MAP = dict(zip([f"A{i}" for i in range(2, 17)], [f"X{n:02d}" for n in random.Random(7).sample(range(1, 16), 15)]))


def build():
    path = DATA / "disguised" / f"{NAME}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(sqlite_file(SOURCE), path)
    db = sqlite3.connect(path)
    types = {r[1].upper(): r[2] for r in db.execute("pragma table_info(district)")}
    new = sorted(MAP.items(), key=lambda kv: kv[1])  # X01, X02, ... so column order says nothing either
    db.executescript(f"""
        create table district_new (district_id INTEGER primary key, {', '.join(f'{x} {types[a]}' for a, x in new)});
        insert into district_new select district_id, {', '.join(a for a, _ in new)} from district;
        drop table district;
        alter table district_new rename to district;""")
    db.commit()
    db.close()
    return path


def rewrite(sql):
    tree = sqlglot.parse_one(sql, read="sqlite")
    for col in tree.find_all(exp.Column):
        if col.name.upper() in MAP:
            col.set("this", exp.to_identifier(MAP[col.name.upper()]))
    return tree.sql(dialect="sqlite")


def questions():
    """FINCH's financial questions, re-keyed for the disguised copy, with rewritten gold SQL; each one checked."""
    src = [x for x in json.loads(real_file("finch_dataset.json").read_text()) if x["db_id"] == SOURCE]
    out, bad = [], []
    for x in src:
        sql = rewrite(x["SQL"])
        a = sqlite_rows(sqlite_file(SOURCE), repair_gold(x["SQL"], SOURCE), 120)
        b = sqlite_rows(sqlite_file(NAME), repair_gold(sql, NAME), 120)
        if not same_result(a, b, "order by" in x["SQL"].lower()):
            bad.append(x["question_id"])
        out.append(dict(x, question_id=x["question_id"] + ID_OFFSET, db_id=NAME, SQL=sql))
    print(f"{len(out)} questions; rewritten gold SQL gives the same answer for {len(out) - len(bad)}" + (f"; DIFFERS for {bad}" if bad else ""))
    if bad:
        raise SystemExit("not loading: fix the rewrite first")
    return out


def load(sf, database, qs):
    load_sqlite(sf, database, NAME, sqlite_file(NAME))
    pairs = pd.DataFrame(qs).rename(columns={"db_name": "source", "SQL": "sql_sqlite"})
    pairs["sql_snowflake"] = [to_snowflake(repair_gold(s, NAME), NAME.upper()) for s in pairs["sql_sqlite"]]
    pairs["conversion_error"] = None
    pairs.columns = [c.upper() for c in pairs.columns]
    pairs = pairs.astype({c: "string" for c in pairs.columns if c != "QUESTION_ID"})
    cur = sf.cursor()
    cur.execute(f"delete from {database}.BENCHMARK.NL_SQL_PAIRS where db_id = %s", (NAME,))
    # ponytail: load_finch.py's full reload rewrites NL_SQL_PAIRS without these rows; rerun disguise.py --load after one
    write_pandas(sf, pairs, "NL_SQL_PAIRS", database=database, schema="BENCHMARK")
    log_change(NAME, "load", f"Created {NAME.upper()}: a copy of FINANCIAL with no descriptions, DISTRICT columns A2..A16 renamed "
               f"X01..X15 in a shuffled order and reordered; {len(qs)} questions added with gold SQL rewritten to match "
               f"(question ids + {ID_OFFSET:,})", detail=MAP)


def add_keys(sf, database):
    """The same primary and foreign keys as the source, and the same rows in BENCHMARK.TABLE_JOINS (no key uses a renamed
    column, so they carry over as they are)."""
    _, rows = table_joins([x for x in json.loads(real_file("finch_dataset.json").read_text()) if x["db_id"] == SOURCE])
    links = [r for r in rows.values() if r["verdict"] == "key"]
    done = apply_keys(sf, database, NAME, table_keys(lite(sqlite_file(NAME)), NAME), links)
    bad = {k: v for k, v in done.items() if v != "declared"}
    print(f"{NAME}: {len(done) - len(bad)} keys declared" + "".join(f"\n    {k}: {v[:160]}" for k, v in bad.items()))
    cur = sf.cursor()
    cur.execute(f"delete from {database}.BENCHMARK.TABLE_JOINS where db_id = %s", (NAME,))
    # ponytail: load_finch.py's key step rewrites TABLE_JOINS without these rows; rerun disguise.py --keys after one
    cur.execute(f"insert into {database}.BENCHMARK.TABLE_JOINS select %s, * exclude (db_id) from {database}.BENCHMARK.TABLE_JOINS "
                "where db_id = %s", (NAME, SOURCE))
    log_change(NAME, "keys", f"Same keys as {SOURCE.upper()}: {sum(k.startswith('pk') for k in done)} primary keys and "
               f"{len(links)} foreign keys declared; {SOURCE}'s links copied into BENCHMARK.TABLE_JOINS")


def main():
    ap = argparse.ArgumentParser()
    connection_args(ap)
    ap.add_argument("--load", action="store_true", help="load into Snowflake, add the questions and the keys")
    ap.add_argument("--keys", action="store_true", help="only (re)declare the keys and TABLE_JOINS rows in Snowflake")
    args = ap.parse_args()
    if args.keys:
        return add_keys(connect(args), args.database)
    print("column map:", MAP)
    build()
    qs = questions()
    if args.load:
        sf = connect(args)
        load(sf, args.database, qs)
        add_keys(sf, args.database)


if __name__ == "__main__":
    main()
