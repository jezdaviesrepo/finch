"""Re-apply the fair-scoring rules to every run in results/history.sqlite, e.g. after adding to gold_review.json.

  - Gold arbitrary: gold SQL ending ORDER BY ... LIMIT with a tie at the cut-off; any tied answer counts as right.
  - Gold disputed: questions in gold_review.json; their reviewed answers count as right too.
  - Gold corrected: gold_review.json entries with replaces_gold; only the corrected answer counts.

Old runs keep only the first 10 rows of each answer, so they are judged on those (with the full row count). A run
whose score changes gets a line added to its notes.

Usage:
  python snowflake/rescore.py && python snowflake/report.py --runs
"""
import json

from evaluate import TIE_NOTE, also_right, gold_ties, reviewed, tie_ok
from history import connect
from load_finch import sqlite_file


def main():
    db, ties = connect(), {}
    with db:
        for run_no, run_id, old in db.execute("select run_no, run_id, correct from runs order by run_no").fetchall():
            for qid, db_id, gold_sql, correct, lenient, pred, pred_rows, flag in db.execute(
                    "select question_id, db_id, gold_sql, correct, lenient, pred_answer, pred_rows, gold_flag from results "
                    "where run_id = ? and gold_error is null", (run_id,)).fetchall():
                rows = json.loads(pred) if pred is not None else None
                if "limit" in gold_sql.lower():
                    t = ties.setdefault((db_id, gold_sql), gold_ties(sqlite_file(db_id), gold_sql))
                    if t:
                        flag = TIE_NOTE.format(n=t[0], tied=len(t[2]))
                        correct = correct or (rows is not None and tie_ok(t, rows, pred_rows))
                review = reviewed(db_id, qid)
                if review:
                    flag = ("Gold corrected: " if review.get("replaces_gold") else "Gold disputed: ") + review["issue"]
                    if rows is not None and len(rows) == pred_rows:  # whole answer stored
                        s, l = also_right(sqlite_file(db_id), review, rows)
                        correct, lenient = (s, l) if review.get("replaces_gold") else (correct or s, lenient or l)
                db.execute("update results set correct = ?, lenient = ?, gold_flag = ? where run_id = ? and question_id = ?",
                           (int(bool(correct)), int(bool(correct or lenient)), flag, run_id, qid))
            new, n = db.execute("select sum(correct), count(*) from results where run_id = ? and gold_error is null", (run_id,)).fetchone()
            db.execute("update runs set correct = ?, accuracy = ? where run_id = ?", (new, new / n, run_id))
            if new != old:
                db.execute("update runs set notes = coalesce(notes, '') || ? where run_id = ?", (
                    f"\n\nRescored after more gold answers were reviewed: {new - old:+d} question(s), now {new} of {n} strict "
                    f"({100 * new / n:.1f}%).", run_id))
            print(f"run {run_no}: strict {old} -> {new}")


if __name__ == "__main__":
    main()
