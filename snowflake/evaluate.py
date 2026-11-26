"""Run the FINCH benchmark against Snowflake.

For each sampled question:
  1. ask a Cortex model for Snowflake SQL, given the question and the schema of its database
  2. run that SQL in FINCH.<DB_ID>
  3. run the gold SQL_SQLITE on the local SQLite copy (the answer key)
  4. compare result sets (order-insensitive unless the gold SQL has ORDER BY; numbers to ~1e-5 relative)

--model gold uses the converted SQL_SNOWFLAKE instead of a model, which checks the load and the
SQLite->Snowflake conversion rather than a model.

Results are appended to FINCH.BENCHMARK.EVAL_RESULTS (one RUN_ID per run) and summarised on screen. Each run is also
saved with both answers to results/history.sqlite (see history.py); `python snowflake/report.py --runs` turns that into
results/report.html.

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

from history import save_run
from load_finch import ROOT, TODAY, connect, connection_args, q, real_file, repair_gold, compares_text_with_number

PROMPT = """You are an expert Snowflake SQL analyst.
Write one Snowflake SQL query that answers the question. The current schema is {schema}; refer to
tables by name only. Match text case-insensitively, e.g. LOWER(col) = 'value'. Return only the SQL, with
no explanation and no markdown.{today}

Tables ({schema}):
{tables}
{examples}
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
    deadline = time.monotonic() + timeout_s  # monotonic: stops while the Mac sleeps
    db.set_progress_handler(lambda: time.monotonic() > deadline, 100_000)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def template(sql):
    """Query with literals masked and tokens sorted, so the same query about another customer, or with its
    conditions in another order, counts as the same template."""
    masked = re.sub(r"'[^']*'|\b\d+(\.\d+)?\b", "?", sql.lower())
    return " ".join(sorted(re.findall(r"\w+|[^\w\s]", masked)))


def words(text):
    return set(re.findall(r"\w+", text.lower()))


def examples(pool, question, n):
    """n most similar pool questions (one per template) as prompt text."""
    # ponytail: word overlap, not embeddings as in the BookSQL paper; use Cortex EMBED_TEXT if retrieval is the limit
    w = words(question)
    best = {}
    for text, sql, tmpl, pw in pool:
        score = len(w & pw) / len(w | pw)
        if score > best.get(tmpl, (-1,))[0]:
            best[tmpl] = (score, text, sql)
    top = sorted(best.values(), reverse=True)[:n]
    return "\nSimilar questions with correct SQL:\n" + "\n".join(f"Q: {t}\nSQL: {s}" for _, t, s in top) + "\n"


def strip_fences(sql):
    m = re.search(r"```(?:sql)?\s*(.*?)```", sql, re.S | re.I)
    return (m.group(1) if m else sql).strip().rstrip(";")


def main():
    ap = argparse.ArgumentParser()
    connection_args(ap)
    ap.add_argument("--model", required=True, help="Cortex model name, or 'gold' to test SQL_SNOWFLAKE")
    ap.add_argument("--sample", type=int, default=100, help="questions to run (0 = all)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--shots", type=int, default=0, help="similar question/SQL examples to put in the prompt")
    ap.add_argument("--rerun", metavar="RUN_ID", help="use the same questions as an earlier run")
    ap.add_argument("--db", help="only this db_id, e.g. financial")
    ap.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    ap.add_argument("--timeout", type=int, default=60, help="seconds per query")
    ap.add_argument("--label", default="", help="what this run is, shown on the report page")
    ap.add_argument("--keep-empty-gold", action="store_true",
                    help="also score questions whose gold answer is empty/zero (mostly broken gold SQL)")
    args = ap.parse_args()
    D = args.database

    sf = connect(args)
    cur = sf.cursor()
    cur.execute(f"alter session set statement_timeout_in_seconds = {args.timeout}")
    pairs = cur.execute(f"select * from {D}.BENCHMARK.NL_SQL_PAIRS").fetch_pandas_all()
    pairs = everything = pairs.sort_values("QUESTION_ID")  # fixed order, so --seed is reproducible
    if args.rerun:
        ids = [r[0] for r in cur.execute(f"select question_id from {D}.BENCHMARK.EVAL_RESULTS where run_id = %s", (args.rerun,))]
        pairs = pairs[pairs.QUESTION_ID.isin(ids)]
    if args.db:
        pairs = pairs[pairs.DB_ID == args.db]
    if args.difficulty:
        pairs = pairs[pairs.DIFFICULTY == args.difficulty]
    if not args.rerun and args.sample and args.sample < len(pairs):
        pairs = pairs.sample(args.sample, random_state=args.seed)

    sqlite_path = {p.stem: p for p in (ROOT / "text2sql-db").rglob("*.sqlite")}
    schemas, pools = {}, {}
    # examples never share a question or a query template with anything being tested
    tested_q, tested_t = set(pairs.QUESTION.str.lower()), set(pairs.SQL_SQLITE.map(template))
    run_id = f"{args.model}{f'_{args.shots}shot' if args.shots else ''}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    results, details = [], []
    for i, r in enumerate(pairs.itertuples(), 1):
        schema = r.DB_ID.upper()
        out = dict(RUN_ID=run_id, MODEL=args.model, QUESTION_ID=r.QUESTION_ID, DB_ID=r.DB_ID,
                   SOURCE=r.SOURCE, DIFFICULTY=r.DIFFICULTY, PRED_SQL=None, PRED_ERROR=None,
                   GOLD_ERROR=None, CORRECT=False)
        gold_sql, gold, pred = r.SQL_SQLITE, None, None
        try:
            gold_sql = repair_gold(r.SQL_SQLITE, r.DB_ID)
            gold = sqlite_rows(real_file(str(sqlite_path[r.DB_ID].relative_to(ROOT))), gold_sql, args.timeout)
            if compares_text_with_number(r.SQL_SQLITE, r.DB_ID):
                out["GOLD_ERROR"] = "gold SQL compares STRFTIME text with a number"
            elif degenerate(gold) and not args.keep_empty_gold:
                out["GOLD_ERROR"] = "empty/zero gold answer"
        except Exception as e:
            out["GOLD_ERROR"] = str(e)[:1000]
        detail = dict(question=r.QUESTION, gold_sql=gold_sql)
        details.append(detail)
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
                if args.shots and schema not in pools:
                    pool = everything[(everything.DB_ID == r.DB_ID) & everything.SQL_SNOWFLAKE.notna()].drop_duplicates("QUESTION")
                    pools[schema] = [(t, s, tm, words(t)) for t, s, tm in zip(pool.QUESTION, pool.SQL_SNOWFLAKE, pool.SQL_SQLITE.map(template))
                                     if t.lower() not in tested_q and tm not in tested_t]
                shots = examples(pools[schema], r.QUESTION, args.shots) if args.shots else ""
                prompt = PROMPT.format(schema=schema, tables=schemas[schema], question=r.QUESTION, today=today, examples=shots)
                detail["prompt"] = prompt
                pred_sql = strip_fences(cur.execute("select snowflake.cortex.complete(%s, %s)", (args.model, prompt)).fetchone()[0])
            out["PRED_SQL"] = pred_sql
            cur.execute(f"use schema {D}.{q(schema)}")
            pred = cur.execute(pred_sql).fetchall()
            if out["GOLD_ERROR"] is None:
                out["CORRECT"] = same_result(gold, pred, ordered="order by" in r.SQL_SQLITE.lower())
        except Exception as e:
            out["PRED_ERROR"] = str(e)[:1000]
        detail.update(gold=None if gold is None else gold[:10], gold_n=None if gold is None else len(gold),
                      pred=None if pred is None else pred[:10], pred_n=None if pred is None else len(pred))
        results.append(out)
        print(f"[{i}/{len(pairs)}] {r.DB_ID:<28} {r.DIFFICULTY:<6} {'OK ' if out['CORRECT'] else 'ERR' if out['PRED_ERROR'] else 'X  '}", flush=True)

    no = save_run(dict(run_id=run_id, label=args.label, model=args.model, shots=args.shots, db_filter=args.db), [dict(
        question_id=int(o["QUESTION_ID"]), db_id=o["DB_ID"], difficulty=o["DIFFICULTY"], question=d["question"],
        gold_sql=d["gold_sql"], gold_answer=d.get("gold"), gold_rows=d.get("gold_n"), gold_error=o["GOLD_ERROR"],
        prompt=d.get("prompt"), pred_sql=o["PRED_SQL"], pred_answer=d.get("pred"), pred_rows=d.get("pred_n"),
        pred_error=o["PRED_ERROR"], correct=bool(o["CORRECT"])) for o, d in zip(results, details)])
    res = pd.DataFrame(results)
    res = res.astype({c: "string" for c in res.columns if c not in ("QUESTION_ID", "CORRECT")})
    write_pandas(sf, res, "EVAL_RESULTS", database=D, schema="BENCHMARK", auto_create_table=True)
    scored = res[res.GOLD_ERROR.isna()]
    print(f"\nRun {no}, {run_id}: {scored.CORRECT.mean():.1%} execution accuracy on {len(scored)} questions "
          f"({len(res) - len(scored)} skipped: gold answer empty/zero or failed, {res.PRED_ERROR.notna().sum()} prediction errors)")
    for col in ("DIFFICULTY", "SOURCE"):
        print(scored.groupby(col).CORRECT.agg(accuracy="mean", n="size").to_string(float_format="{:.1%}".format), "\n")
    print(f"details: select * from {D}.BENCHMARK.EVAL_RESULTS where run_id = '{run_id}'")


if __name__ == "__main__":
    main()
