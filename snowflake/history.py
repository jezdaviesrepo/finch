"""Our own record of every model run and every change made to the FINCH database in Snowflake: results/history.sqlite.

  changes  one row per change to the Snowflake database (load, keys, types, comments, ...)
  runs     one row per model run, with LAST_CHANGE = the newest change it ran against
  results  one row per question per run: both SQL queries, both answers (first 10 rows) and the verdict

So "what changed between run 1 and run 2" is: changes with run1.last_change < id <= run2.last_change.
"""
import datetime
import json
import pathlib
import sqlite3

PATH = pathlib.Path(__file__).resolve().parent.parent / "results" / "history.sqlite"
SCHEMA = """
create table if not exists changes (
    id integer primary key, at text, db_id text, kind text, description text, detail text);
create table if not exists runs (
    run_id text primary key, run_no integer unique, at text, label text, model text, shots integer, db_filter text,
    questions integer, correct integer, sql_errors integer, skipped integer, accuracy real,
    last_change integer references changes(id));
create table if not exists results (
    run_id text references runs(run_id), question_id integer, db_id text, difficulty text, question text,
    gold_sql text, gold_answer text, gold_rows integer, gold_error text,
    prompt text, pred_sql text, pred_answer text, pred_rows integer, pred_error text, correct integer,
    primary key (run_id, question_id));
"""
RESULT_COLS = ["question_id", "db_id", "difficulty", "question", "gold_sql", "gold_answer", "gold_rows", "gold_error",
               "prompt", "pred_sql", "pred_answer", "pred_rows", "pred_error", "correct"]


def connect():
    PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(PATH)
    db.executescript(SCHEMA)
    return db


def now():
    return f"{datetime.datetime.now():%Y-%m-%d %H:%M}"


def log_change(db_id, kind, description, detail=None, at=None):
    """Record a change to the Snowflake database. db_id None = all databases."""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    db = connect()
    with db:
        db.execute("insert into changes (at, db_id, kind, description, detail) values (?, ?, ?, ?, ?)",
                   (at or now(), db_id, kind, description, detail))
    db.close()


def save_run(run, results):
    """run: dict(run_id, label, model, shots, db_filter[, at]); results: dicts with RESULT_COLS (answers as row lists).
    Returns the run number."""
    scored = [r for r in results if not r["gold_error"]]
    correct = sum(bool(r["correct"]) for r in scored)
    db = connect()
    with db:
        no = db.execute("select coalesce(max(run_no), 0) + 1 from runs").fetchone()[0]
        last = db.execute("select max(id) from changes").fetchone()[0]
        db.execute("insert into runs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            run["run_id"], no, run.get("at") or now(), run["label"], run["model"], run["shots"], run["db_filter"],
            len(results), correct, sum(bool(r["pred_error"]) for r in results), len(results) - len(scored),
            correct / len(scored) if scored else None, last))
        db.executemany(f"insert into results (run_id, {', '.join(RESULT_COLS)}) values ({', '.join('?' * (len(RESULT_COLS) + 1))})",
                       [[run["run_id"]] + [json.dumps(r[c], default=str) if c.endswith("_answer") and r[c] is not None
                                            else r[c] for c in RESULT_COLS] for r in results])
    db.close()
    return no
