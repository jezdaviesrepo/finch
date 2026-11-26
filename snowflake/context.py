"""Add descriptions (context) to tables and columns in Snowflake, from snowflake/context/<db_id>.json.

They are stored as Snowflake COMMENTs, logged in results/history.sqlite, and evaluate.py puts them in the model's prompt.

  {"TABLE": {"comment": "what the table is", "columns": {"COLUMN": "what the column means"}}}

Usage:
  python snowflake/context.py financial
"""
import argparse
import json
import pathlib

from history import log_change
from load_finch import connect, connection_args, q


def literal(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    connection_args(ap)
    ap.add_argument("db_id", help="e.g. financial; reads snowflake/context/<db_id>.json")
    args = ap.parse_args()
    context = json.loads((pathlib.Path(__file__).with_name("context") / f"{args.db_id}.json").read_text())
    cur = connect(args).cursor()
    schema = f"{args.database}.{q(args.db_id)}"
    for table, ctx in context.items():
        if ctx.get("comment"):
            cur.execute(f"comment on table {schema}.{q(table)} is {literal(ctx['comment'])}")
        for col, text in ctx.get("columns", {}).items():
            cur.execute(f"comment on column {schema}.{q(table)}.{q(col)} is {literal(text)}")
        n = len(ctx.get("columns", {}))
        print(f"{args.db_id}.{table}: table description and {n} column descriptions added")
        log_change(args.db_id, "context", f"{table}: table description and {n} column descriptions added "
                   f"(business meaning, units, value ranges, examples)", detail=ctx)


if __name__ == "__main__":
    main()
