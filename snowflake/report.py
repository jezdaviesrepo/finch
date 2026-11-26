"""Summarise how every FINCH gold query behaves, as a self-contained HTML page.

Two steps, so another engine can reuse the second one:
  1. run:    one result per distinct (database, gold query)  -> data/report_<engine>.json
  2. render: that JSON                                       -> data/report_<engine>.html

For Snowflake, write the same JSON from a Snowflake run (status "differs" = answer is not SQLite's) and render it.

Usage:
  python snowflake/report.py                   # run on SQLite, then render
  python snowflake/report.py --render sqlite   # render an existing data/report_sqlite.json
  python snowflake/report.py --runs            # render model runs (results/history.sqlite) into results/report.html
"""
import argparse
import datetime
import json
import pathlib
import re
import sqlite3
import time
from collections import Counter
from multiprocessing import Pool

import sqlglot

from evaluate import degenerate, sqlite_rows, template
from history import PATH as HISTORY, connect as connect_history
from load_finch import DATA, ROOT, compares_text_with_number, real_file, repair_gold, table_joins

PLACEHOLDER = re.compile(r"'[^']*\[[a-z_ ]+\][^']*'")  # e.g. payment_method = '[payment_method]'
PATHS = {p.stem: p for p in (ROOT / "text2sql-db").rglob("*.sqlite")}


def classify(sql, db_id, rows):
    if compares_text_with_number(sql, db_id):
        return "date_bug"
    if PLACEHOLDER.search(sql.lower()):
        return "placeholder"
    if not rows:
        return "no_rows"
    if not degenerate(rows):
        return "usable"
    return "zero" if all(v == 0 and v is not None for r in rows for v in r) else "null"


def run_one(job):
    db_id, sql, timeout = job
    out, t0 = dict(db=db_id, sql=sql, status="error", rows=0, sample=""), time.monotonic()
    try:
        path = real_file(str(PATHS[db_id].relative_to(ROOT)))
        rows = sqlite_rows(path, repair_gold(sql, db_id), timeout)
        out.update(status=classify(sql, db_id, rows), rows=len(rows), sample=str(rows[:2])[:200])
    except Exception as e:
        out.update(status="timeout" if str(e) == "interrupted" else "error", sample=str(e)[:200])
    out["seconds"] = round(time.monotonic() - t0, 2)
    return out


def run_sqlite():
    questions = json.loads(real_file("finch_dataset.json").read_text())
    jobs = sorted({(x["db_id"], x["SQL"], 60) for x in questions})
    for db_id in {j[0] for j in jobs}:  # download once, not once per worker
        real_file(str(PATHS[db_id].relative_to(ROOT)))
    results, t0 = {}, time.monotonic()
    with Pool(8) as pool:
        for i, r in enumerate(pool.imap_unordered(run_one, jobs, chunksize=8), 1):
            results[r["db"], r["sql"]] = r
            if i % 2000 == 0:
                print(f"{i:,}/{len(jobs):,} {time.monotonic() - t0:.0f}s", flush=True)
    slow = [k for k, r in results.items() if r["status"] == "timeout"]
    print(f"retrying {len(slow)} timeouts with a 300s limit", flush=True)
    with Pool(2) as pool:
        for r in pool.imap_unordered(run_one, [(*k, 300) for k in slow]):
            results[r["db"], r["sql"]] = r
    write("sqlite", questions, results)


def write(engine, questions, results):
    """Results plus the question counts render() needs. results: {(db_id, sql): row}."""
    for x in questions:
        r = results[x["db_id"], x["SQL"]]
        r.setdefault("source", x["db_name"])
        r.setdefault("question", x["question"])
        r["difficulty"] = Counter(r.get("difficulty", {})) + Counter([x["difficulty"]])
    (DATA / f"report_{engine}.json").write_text(json.dumps(
        dict(engine=engine, created=f"{datetime.datetime.now():%Y-%m-%d %H:%M}", results=list(results.values()))))


def render(engine):
    data = json.loads((DATA / f"report_{engine}.json").read_text())
    types = {}
    for r in data["results"]:
        t = types.setdefault((r["db"], template(r["sql"]), r["status"]),
                             dict(r, questions=0, queries=0, difficulty=Counter()))
        t["queries"] += 1
        t["questions"] += sum(r["difficulty"].values())
        t["difficulty"] += Counter(r["difficulty"])
    keep = ("db", "status", "questions", "queries", "difficulty", "question", "sql", "sample")
    page = dict(engine={"sqlite": "SQLite", "snowflake": "Snowflake"}.get(engine, engine), created=data["created"],
                rows=[{k: t[k] for k in keep} for t in types.values()])
    keys, joins = table_joins(json.loads(real_file("finch_dataset.json").read_text()))
    page["tables"] = [dict(db=d, t=t, pk=", ".join(k["pk"])) for d, ts in keys.items() for t, k in sorted(ts.items())]
    page["joins"] = [dict(db=d, a=j["child"], b=j["parent"], fk=bool(j["fk"]), found=bool(j.get("found")), q=j["questions"], v=j["verdict"], rows=j["rows"], hit=j["matched"],
                          on=" and ".join(f"{j['child']}.{x} = {j['parent']}.{y}" for x, y in zip(j["child_cols"], j["parent_cols"])))
                     for (d, *_), j in joins.items()]
    html = pathlib.Path(__file__).with_name("report_template.html").read_text()
    out = DATA / f"report_{engine}.html"
    out.write_text(html.replace("__DATA__", json.dumps(page).replace("<", "\\u003c")))
    print(f"wrote {out}")


def pretty(sql, dialect):
    """SQL laid out one clause per line, for reading only (the history keeps the SQL exactly as it ran)."""
    try:
        return sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)[0] if sql else sql
    except Exception:
        return sql


def render_runs():
    """results/history.sqlite -> results/report.html: every run, with the database changes made before it."""
    db = connect_history()
    db.row_factory = sqlite3.Row
    changes = [dict(c) for c in db.execute("select id, at, db_id, kind, description from changes order by id")]
    runs, prev = [], 0
    for r in db.execute("select * from runs order by run_no").fetchall():
        run = dict(r, name=f"Run {r['run_no']}", changes=[c for c in changes if prev < c["id"] <= (r["last_change"] or 0)])
        prev = r["last_change"] or prev
        run["results"] = [dict(x, gold=json.loads(x["gold_answer"] or "null"), pred=json.loads(x["pred_answer"] or "null"),
                               gold_sql=pretty(x["gold_sql"], "sqlite"), pred_sql=pretty(x["pred_sql"], "snowflake"))
                          for x in db.execute("select * from results where run_id = ? order by question_id", (r["run_id"],))]
        runs.append(run)
    page = dict(runs=runs, pending=[c for c in changes if c["id"] > prev])
    html = pathlib.Path(__file__).with_name("runs_template.html").read_text()
    out = HISTORY.with_name("report.html")
    out.write_text(html.replace("__DATA__", json.dumps(page).replace("<", "\\u003c")))
    print(f"wrote {out}: {len(runs)} runs, {len(page['pending'])} changes not yet tested by a run")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", metavar="ENGINE", help="only render data/report_ENGINE.json")
    ap.add_argument("--runs", action="store_true", help="only render model runs from results/history.sqlite into results/report.html")
    args = ap.parse_args()
    if args.runs:
        return render_runs()
    if not args.render:
        run_sqlite()
    render(args.render or "sqlite")


if __name__ == "__main__":
    main()
