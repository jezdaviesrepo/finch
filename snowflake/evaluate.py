"""Run the FINCH benchmark against Snowflake.

For each sampled question:
  1. ask a Cortex model for Snowflake SQL, given the question and the schema of its database
  2. run that SQL in FINCH.<DB_ID>
  3. run the gold SQL_SQLITE on the local SQLite copy (the answer key)
  4. compare result sets (order-insensitive unless the gold SQL has ORDER BY; numbers to ~1e-5 relative)

--model gold uses the converted SQL_SNOWFLAKE instead of a model, which checks the load and the
SQLite->Snowflake conversion rather than a model.

Results are appended to FINCH.BENCHMARK.EVAL_RESULTS (one RUN_ID per run) and summarised on screen.

Usage:
  python snowflake/evaluate.py --model claude-sonnet-4-5 --sample 100
  python snowflake/evaluate.py --model gold --sample 500
"""
import argparse
import datetime
import decimal
import re
import sqlite3
import time

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

from load_finch import ROOT, TODAY, connect, connection_args, q, real_file, repair_gold

PROMPT = """You are an expert Snowflake SQL analyst.
Write one Snowflake SQL query that answers the question. The current schema is {schema}; refer to
tables by name only. Return only the SQL, with no explanation and no markdown.{today}

Tables ({schema}):
{tables}

Question: {question}"""


def degenerate(rows):
    """Gold answer carries no signal: no rows, or only null/zero/empty values."""
    return all(all(v in (None, 0, "") for v in r) for r in rows)


def norm(v):
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float, decimal.Decimal)):
        return f"{float(v):.5g}"  # ponytail: ~1e-5 relative rounding, not the paper's exact |a-b|/max(1,|b|) <= tau
    return "" if v is None else str(v).strip()


def same_result(gold_rows, pred_rows, ordered):
    g = [tuple(map(norm, r)) for r in gold_rows]
    p = [tuple(map(norm, r)) for r in pred_rows]
    return g == p if ordered else sorted(g) == sorted(p)


def sqlite_rows(path, sql, timeout_s):
    db = sqlite3.connect(path)
    db.text_factory = lambda b: b.decode("utf-8", "replace")
    deadline = time.time() + timeout_s
    db.set_progress_handler(lambda: time.time() > deadline, 100_000)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def strip_fences(sql):
    m = re.search(r"```(?:sql)?\s*(.*?)```", sql, re.S | re.I)
    return (m.group(1) if m else sql).strip().rstrip(";")


def main():
    ap = argparse.ArgumentParser()
    connection_args(ap)
    ap.add_argument("--model", required=True, help="Cortex model name, or 'gold' to test SQL_SNOWFLAKE")
    ap.add_argument("--sample", type=int, default=100, help="questions to run (0 = all)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", help="only this db_id, e.g. financial")
    ap.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    ap.add_argument("--timeout", type=int, default=60, help="seconds per query")
    ap.add_argument("--keep-empty-gold", action="store_true",
                    help="also score questions whose gold answer is empty/zero (mostly broken gold SQL)")
    args = ap.parse_args()
    D = args.database

    sf = connect(args)
    cur = sf.cursor()
    cur.execute(f"alter session set statement_timeout_in_seconds = {args.timeout}")
    pairs = cur.execute(f"select * from {D}.BENCHMARK.NL_SQL_PAIRS").fetch_pandas_all()
    if args.db:
        pairs = pairs[pairs.DB_ID == args.db]
    if args.difficulty:
        pairs = pairs[pairs.DIFFICULTY == args.difficulty]
    if args.sample and args.sample < len(pairs):
        pairs = pairs.sample(args.sample, random_state=args.seed)

    sqlite_path = {p.stem: p for p in (ROOT / "text2sql-db").rglob("*.sqlite")}
    schemas = {}
    run_id = f"{args.model}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    results = []
    for i, r in enumerate(pairs.itertuples(), 1):
        schema = r.DB_ID.upper()
        out = dict(RUN_ID=run_id, MODEL=args.model, QUESTION_ID=r.QUESTION_ID, DB_ID=r.DB_ID,
                   SOURCE=r.SOURCE, DIFFICULTY=r.DIFFICULTY, PRED_SQL=None, PRED_ERROR=None,
                   GOLD_ERROR=None, CORRECT=False)
        gold_sql = r.SQL_SQLITE
        try:
            gold_sql = repair_gold(r.SQL_SQLITE, r.DB_ID)
            gold = sqlite_rows(real_file(str(sqlite_path[r.DB_ID].relative_to(ROOT))), gold_sql, args.timeout)
            if degenerate(gold) and not args.keep_empty_gold:
                out["GOLD_ERROR"] = "empty/zero gold answer"
        except Exception as e:
            out["GOLD_ERROR"] = str(e)[:1000]
        if out["GOLD_ERROR"] and not args.keep_empty_gold:
            results.append(out)
            print(f"[{i}/{len(pairs)}] {r.DB_ID:<28} {r.DIFFICULTY:<6} skip (gold: {out['GOLD_ERROR'][:40]})")
            continue
        try:
            if args.model == "gold":
                pred_sql = r.SQL_SNOWFLAKE
            else:
                if schema not in schemas:
                    cols = cur.execute(
                        f"select table_name, column_name, data_type from {D}.information_schema.columns "
                        "where table_schema = %s order by table_name, ordinal_position", (schema,)).fetchall()
                    tables = {}
                    for t, c, ty in cols:
                        tables.setdefault(t, []).append(f"{q(c)} {ty}")
                    schemas[schema] = "\n".join(f"{q(t)}({', '.join(cs)})" for t, cs in tables.items())
                today = f"\nToday's date is {TODAY[r.DB_ID]}: use that literal date, not CURRENT_DATE." if r.DB_ID in TODAY else ""
                prompt = PROMPT.format(schema=schema, tables=schemas[schema], question=r.QUESTION, today=today)
                pred_sql = strip_fences(cur.execute("select snowflake.cortex.complete(%s, %s)", (args.model, prompt)).fetchone()[0])
            out["PRED_SQL"] = pred_sql
            cur.execute(f"use schema {D}.{q(schema)}")
            pred = cur.execute(pred_sql).fetchall()
            if out["GOLD_ERROR"] is None:
                out["CORRECT"] = same_result(gold, pred, ordered="order by" in r.SQL_SQLITE.lower())
        except Exception as e:
            out["PRED_ERROR"] = str(e)[:1000]
        results.append(out)
        print(f"[{i}/{len(pairs)}] {r.DB_ID:<28} {r.DIFFICULTY:<6} {'OK ' if out['CORRECT'] else 'ERR' if out['PRED_ERROR'] else 'X  '}")

    res = pd.DataFrame(results)
    res = res.astype({c: "string" for c in res.columns if c not in ("QUESTION_ID", "CORRECT")})
    write_pandas(sf, res, "EVAL_RESULTS", database=D, schema="BENCHMARK", auto_create_table=True)
    scored = res[res.GOLD_ERROR.isna()]
    print(f"\nrun {run_id}: {scored.CORRECT.mean():.1%} execution accuracy on {len(scored)} questions "
          f"({len(res) - len(scored)} skipped: gold answer empty/zero or failed, {res.PRED_ERROR.notna().sum()} prediction errors)")
    for col in ("DIFFICULTY", "SOURCE"):
        print(scored.groupby(col).CORRECT.agg(accuracy="mean", n="size").to_string(float_format="{:.1%}".format), "\n")
    print(f"details: select * from {D}.BENCHMARK.EVAL_RESULTS where run_id = '{run_id}'")


if __name__ == "__main__":
    main()
