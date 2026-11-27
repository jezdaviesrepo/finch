"""Load FINCH into a Snowflake database.

Creates:
  <DB>.<SOURCE_DB>.*           one schema per FINCH SQLite database (33), identifiers upper-cased
  <DB>.BENCHMARK.NL_SQL_PAIRS  the 75,725 question/SQL pairs, with the gold SQL also
                               converted to Snowflake dialect (SQL_SNOWFLAKE), qualified by schema
  <DB>.BENCHMARK.TABLE_JOINS   how tables relate: declared foreign keys and the joins the gold SQL uses

Usage:
  python snowflake/load_finch.py --connection default --warehouse COMPUTE_WH [--database FINCH]
"""
import argparse
import atexit
import json
import pathlib
import re
import sqlite3
import urllib.request
from collections import Counter

import pandas as pd
import snowflake.connector
import sqlglot
from snowflake.connector.pandas_tools import write_pandas
from sqlglot import exp

from history import log_change

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"  # git-lfs files are fetched here; the repo itself only holds pointers
LFS_URL = "https://media.githubusercontent.com/media/finreflectkg/finch/main/"
CHUNK = 500_000
# FINCH's BookSQL file adds 178 empty tables to the paper's 7, and no gold query uses them.
# Not a general rule: gold queries in some other databases do use empty tables.
SKIP_EMPTY = {"accounting"}


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


def sqlite_file(db_id):
    """Local SQLite file for a database: FINCH's own, or a disguised copy built by disguise.py (data/disguised/)."""
    own = next((ROOT / "text2sql-db").rglob(f"{db_id}.sqlite"), None)
    return real_file(str(own.relative_to(ROOT))) if own else DATA / "disguised" / f"{db_id}.sqlite"


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


def lite(path):
    db = sqlite3.connect(path)
    db.text_factory = lambda b: b.decode("utf-8", "replace")
    return db


def table_names(db, db_id):
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    return [t for t in tables if db_id not in SKIP_EMPTY or db.execute(f"select 1 from {q(t)} limit 1").fetchone()]


# keys the data supports where SQLite's are wrong: BookSQL declares (id, businessID, name), unique only through id, and
# none on transactions. business + name is unique only for products and payment methods (see README).
KEYS = {"accounting": {
    "MASTER_TXN_TABLE": dict(pk=["TRANSACTION_ID", "ACCOUNT"]),
    "PRODUCTS": dict(pk=["ID"], unique=[["BUSINESSID", "PRODUCT_SERVICE"]]),
    "PAYMENT_METHOD": dict(pk=["ID"], unique=[["BUSINESSID", "PAYMENT_METHOD"]]),
    **{t: dict(pk=["ID"]) for t in ("CHART_OF_ACCOUNTS", "CUSTOMERS", "VENDORS", "EMPLOYEES")}}}


def table_keys(db, db_id):
    """{TABLE: dict(cols, pk, fks=[(cols, PARENT, parent_cols)], unique=[cols])} as declared in SQLite (or KEYS), names upper-cased."""
    out = {}
    for t in table_names(db, db_id):
        info = db.execute(f"pragma table_info({q(t)})").fetchall()
        fks = {}
        for r in db.execute(f"pragma foreign_key_list({q(t)})"):
            fks.setdefault(r[0], []).append(r)
        out[t.upper()] = dict(
            cols=[r[1].upper() for r in info],
            pk=[r[1].upper() for r in sorted((r for r in info if r[5]), key=lambda r: r[5])],
            fks=[([r[3].upper() for r in rows], rows[0][2].upper(), [r[4] and r[4].upper() for r in rows])
                 for rows in (sorted(v, key=lambda r: r[1]) for v in fks.values())])
    for t, k in KEYS.get(db_id, {}).items():
        out[t].update(k)
    for t in out.values():  # "references parent" without columns means the parent's primary key
        t["fks"] = [(c, p, out[p]["pk"] if None in pc else pc) for c, p, pc in t["fks"]]
    return out


def query_joins(sql, keys):
    """{(table, table, ((col, col), ...))} equality joins between base tables in one gold query."""
    found = set()
    tree = sqlglot.parse_one(sql, read="sqlite")
    for sel in tree.find_all(exp.Select):
        joins = sel.args.get("joins") or []
        frm = sel.args.get("from_") or sel.args.get("from")
        src = {}
        for node in [frm.this if frm else None] + [j.this for j in joins]:
            if isinstance(node, exp.Table) and node.name.upper() in keys:
                src[node.alias_or_name.upper()] = node.name.upper()

        def resolve(col):
            if col.table:
                alias = col.table.upper()
                return alias if alias in src else None
            hits = [a for a, t in src.items() if col.name.upper() in keys[t]["cols"]]
            return hits[0] if len(hits) == 1 else None

        pairs = {}
        where = sel.args.get("where")
        for cond in [j.args.get("on") for j in joins] + [where]:
            for eq in cond.find_all(exp.EQ) if cond else ():
                if eq.find_ancestor(exp.Select) is not sel or not (isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column)):
                    continue
                a, b = resolve(eq.left), resolve(eq.right)
                if a and b and a != b:
                    x, y = (a, eq.left.name.upper()), (b, eq.right.name.upper())
                    if (src[a], a) > (src[b], b):
                        x, y = y, x
                    pairs.setdefault((x[0], y[0]), set()).add((x[1], y[1]))
        for (a, b), cs in pairs.items():
            found.add((src[a], src[b], tuple(sorted(cs))))
    return found


BLANK = {"", "--", "nan", "none", "null"}  # how FINCH data writes "no value"
MATCH = 0.95  # share of child rows that must find their parent for a link to count as a key
VERDICTS = ["key", "same table", "partial", "not unique", "no values"]  # best first


def norm(v):
    """Compare values the way a typed database does: '2' = 2 = 2.0, and FINCH's blanks are null."""
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in BLANK:
            return None
        try:
            v = float(s)
        except ValueError:
            return s
    if isinstance(v, float):
        return None if v != v else int(v) if v.is_integer() else v
    return v


def value_counts(db, table, cols):
    names = ", ".join(map(q, cols))
    out = Counter()
    for *vals, n in db.execute(f"select {names}, count(*) from {q(table)} group by {names}"):
        k = tuple(map(norm, vals))
        if None not in k:
            out[k] += n
    return out


def check_link(counts, child, cc, parent, pc):
    """Does child(cc) -> parent(pc) make sense in the data? The parent side must be unique (else a join repeats
    rows) and at least MATCH of child rows must find a parent."""
    c, p = counts(child, cc), counts(parent, pc)
    rows, matched = sum(c.values()), sum(n for k, n in c.items() if k in p)
    verdict = ("same table" if (child, cc) == (parent, pc) else "no values" if not rows
               else "not unique" if not p or max(p.values()) > 1 else "key" if matched >= MATCH * rows else "partial")
    return dict(child=child, child_cols=cc, parent=parent, parent_cols=pc, rows=rows, matched=matched, verdict=verdict)


IDISH = re.compile(r"(ID|CODE|KEY|NO|NBR|NUM|NUMBER)$")


def find_links(keys, counts, known):
    """Links nobody declared: a column named like another table's key column (customer_id -> customers.customer_id, or
    -> customers.id) that passes check_link. Skipped: a column that already has a key, a parent column that itself
    points elsewhere, and a child table that more keys point to than the parent
    (unless the parent column is its table's primary key). Each column gets one parent: the table most keys already point to, else the one with the most
    distinct values."""
    single = lambda t: t[:-3] + "Y" if t.endswith("IES") else t[:-1] if t.endswith("S") else t
    hub = Counter(l["parent"] for l in known)
    linked = {(l["child"], tuple(l["child_cols"])) for l in known}
    best = {}
    for P, k in keys.items():
        for p in [c for c in k["cols"] if k["pk"] == [c] or IDISH.search(c)]:
            names = {p, f"{P}_{p}", f"{single(P)}_{p}", f"{single(P)}{p}"} | ({f"{P}_ID", f"{single(P)}_ID", f"{single(P)}ID"} if p == "ID" else set())
            for C, ck in keys.items():
                for c in ck["cols"]:
                    if C == P or c not in names or c == p == "ID" or (C, (c,)) in linked or (P, (p,)) in linked or (hub[C] > hub[P] and k["pk"] != [p]):
                        continue
                    l = check_link(counts, C, [c], P, [p])
                    rank = (hub[P], len(counts(P, [p])), l["matched"] / l["rows"]) if l["verdict"] == "key" else None
                    if rank and rank > best.get((C, c), (None, ()))[1]:
                        best[C, c] = l, rank
    out = {}
    for l, _ in best.values():  # a one-to-one pair can be found both ways: keep one
        out.setdefault(frozenset([(l["child"], l["child_cols"][0]), (l["parent"], l["parent_cols"][0])]), l)
    return list(out.values())


def table_joins(questions):
    """Every way tables are related: declared foreign keys, joins the gold SQL uses (with question counts) and links
    found in the data (see find_links), each checked against the data (see check_link)."""
    paths = {p.stem: p for p in (ROOT / "text2sql-db").rglob("*.sqlite")}
    keys, rows, cache, dbs = {}, {}, {}, {}
    for db_id in sorted({x["db_id"] for x in questions}):
        dbs[db_id] = lite(real_file(str(paths[db_id].relative_to(ROOT))))
        keys[db_id] = table_keys(dbs[db_id], db_id)
        for t, k in keys[db_id].items():
            for c, parent, pc in k["fks"]:
                left, right, cs = (t, parent, zip(c, pc)) if t <= parent else (parent, t, zip(pc, c))
                rows[db_id, left, right, tuple(sorted(cs))] = dict(fk=f"fk {c} {parent}", child=t, questions=0)
    for x in questions:
        key = x["db_id"], x["SQL"]
        if key not in cache:
            try:
                cache[key] = query_joins(x["SQL"], keys[x["db_id"]])
            except Exception:
                cache[key] = set()
        for j in cache[key]:
            rows.setdefault((x["db_id"], *j), dict(fk=None, child=None, questions=0))["questions"] += 1
    seen = {}
    for (d, a, b, cs), r in rows.items():
        counts = lambda t, c: seen.get((d, t, tuple(c))) or seen.setdefault((d, t, tuple(c)), value_counts(dbs[d], t, c))
        ac, bc = [x for x, _ in cs], [y for _, y in cs]
        ways = [check_link(counts, a, ac, b, bc), check_link(counts, b, bc, a, ac)]
        if r["child"]:  # a foreign key's direction is given
            r.update(ways[r["child"] != a])
        else:
            r.update(min(ways, key=lambda w: (VERDICTS.index(w["verdict"]), -w["matched"] / (w["rows"] or 1))))
    for d in keys:
        counts = lambda t, c: seen.get((d, t, tuple(c))) or seen.setdefault((d, t, tuple(c)), value_counts(dbs[d], t, c))
        known = [r for (x, *_), r in rows.items() if x == d and r["verdict"] == "key"]
        for l in find_links(keys[d], counts, known):
            a, b = sorted([l["child"], l["parent"]])
            cs = ((l["child_cols"][0], l["parent_cols"][0]) if a == l["child"] else (l["parent_cols"][0], l["child_cols"][0]),)
            rows.setdefault((d, a, b, cs), dict(fk=None, found=True, questions=0)).update(l)  # may turn round a backwards foreign key
    return keys, rows


def apply_keys(sf, database, db_id, keys, links):
    """Declare keys in Snowflake (informational: NORELY, not enforced): SQLite's primary keys, plus a foreign key for
    every link that checks out as a key. Old keys are dropped first. Returns {name: 'declared' or error}."""
    schema, done, cur = q(db_id), {}, sf.cursor()
    table = lambda t: f"{database}.{schema}.{q(t)}"
    cols = lambda cs: f"({', '.join(map(q, cs))})"

    def run(name, sql):
        try:
            cur.execute(sql)
            done[name] = "declared"
        except Exception as e:
            done[name] = "declared" if "already exists" in str(e) else str(e).replace("\n", " ")[:300]

    for t, name, _ in cur.execute(f"select table_name, constraint_name, constraint_type from {database}.information_schema.table_constraints "
                                     f"where table_schema = %s and constraint_type in ('FOREIGN KEY', 'PRIMARY KEY', 'UNIQUE') order by 3", (db_id.upper(),)).fetchall():
        cur.execute(f"alter table {table(t)} drop constraint {q(name)}")
    # a foreign key needs the same type on both sides: turn the text side into a number when every value converts
    types = {(t, c): ty for t, c, ty in cur.execute(f"select table_name, column_name, data_type from {database}.information_schema.columns "
                                                    f"where table_schema = %s", (db_id.upper(),)).fetchall()}
    for l in links:
        for c, pc in zip(l["child_cols"], l["parent_cols"]):
            side = {types[l["child"], c], types[l["parent"], pc]}
            if len(side) == 2 and "TEXT" in side:
                to = "FLOAT" if "FLOAT" in side else "NUMBER(38,0)"
                t, col = (l["child"], c) if types[l["child"], c] == "TEXT" else (l["parent"], pc)
                lost = cur.execute(f"select count(*) from {table(t)} where {q(col)} is not null and try_cast({q(col)} as {to}) is null").fetchone()[0]
                if lost:
                    done[f"type {t}.{col}"] = f"left as text: {lost} values are not numbers"
                    continue
                select = ", ".join(f"try_cast({q(x)} as {to}) as {q(x)}" if x == col else q(x) for x in keys[t]["cols"])
                run(f"type {t}.{col}", f"create or replace table {table(t)} copy grants as select {select} from {table(t)}")
                if done[f"type {t}.{col}"] == "declared":
                    log_change(db_id, "type", f"{t}.{col} changed from text to {to}, to match {l['parent']}.{pc} for a foreign key")
                types[l["child"], c] = types[l["parent"], pc] = to.split("(")[0]
    for t, k in keys.items():
        if k["pk"]:
            run(f"pk {t}", f"alter table {table(t)} add constraint {q('PK_' + t)} primary key {cols(k['pk'])} norely")
        for u in k.get("unique", []):
            run(f"unique {t} {u}", f"alter table {table(t)} add constraint {q(f'UQ_{t}_' + '_'.join(u))} unique {cols(u)} norely")
    for l in links:
        child, parent = l["child"], l["parent"]
        cc, pc = l["child_cols"], l["parent_cols"]
        if sorted(pc) == sorted(keys[parent]["pk"]):  # list the columns in the primary key's order
            cc, pc = zip(*sorted(zip(cc, pc), key=lambda x: keys[parent]["pk"].index(x[1])))
        else:  # Snowflake needs a primary or unique key on the parent side; check_link showed it is unique
            run(f"unique {parent} {list(pc)}", f"alter table {table(parent)} add constraint {q(f'UQ_{parent}_' + '_'.join(pc))} unique {cols(pc)} norely")
        l["key"] = f"FK_{child}_{parent}_{'_'.join(cc)}"[:200]
        run(l["key"], f"alter table {table(child)} add constraint {q(l['key'])} foreign key {cols(cc)} references {table(parent)} {cols(pc)} norely")
    return done


SAYS = {"key": "a key", "same table": "compares rows of one table, not a link", "partial": "too few child rows find a parent",
        "not unique": "the parent side repeats, so the join repeats rows", "no values": "the child column holds no values"}


def load_keys(sf, database, questions):
    """Declare keys on every loaded schema and write BENCHMARK.TABLE_JOINS."""
    keys, rows = table_joins(questions)
    for db_id in keys:
        links = [r for (d, *_), r in rows.items() if d == db_id and r["verdict"] == "key"]
        done = apply_keys(sf, database, db_id, keys[db_id], links)
        bad = {k: v for k, v in done.items() if v != "declared"}
        print(f"{db_id}: {len(done) - len(bad)} declared" + "".join(f"\n    {k}: {v[:160]}" for k, v in bad.items()), flush=True)
        for r in links:
            r["snowflake"] = "foreign key " + r["key"] if done[r["key"]] == "declared" else "failed: " + done[r["key"]]
    out = pd.DataFrame([dict(
        DB_ID=d, CHILD_TABLE=r["child"], CHILD_COLUMNS=", ".join(r["child_cols"]), PARENT_TABLE=r["parent"],
        PARENT_COLUMNS=", ".join(r["parent_cols"]), SOURCE="foreign key in FINCH" if r["fk"] else "found in data" if r.get("found") else "gold SQL only",
        GOLD_QUESTIONS=r["questions"], CHILD_ROWS=r["rows"], MATCHED_ROWS=r["matched"],
        VERDICT=r["verdict"], WHY=SAYS[r["verdict"]],
        SNOWFLAKE_KEY=r.get("snowflake", "not declared")) for (d, *_), r in rows.items()])
    sf.cursor().execute(f"create schema if not exists {database}.BENCHMARK")
    write_pandas(sf, out, "TABLE_JOINS", database=database, schema="BENCHMARK", auto_create_table=True, overwrite=True)
    print(f"BENCHMARK.TABLE_JOINS: {len(out)} rows, {out.VERDICT.value_counts().to_dict()}")
    fks = sum(r.get("snowflake", "").startswith("foreign key") for r in rows.values())
    log_change(None, "keys", f"Keys redeclared from checked links: {fks} foreign keys; BENCHMARK.TABLE_JOINS rewritten "
               f"({len(out)} links: {out.VERDICT.value_counts().to_dict()})", detail=out.to_dict("records"))


def load_sqlite(sf, database, db_id, path):
    schema = db_id.upper()
    sf.cursor().execute(f"create or replace schema {database}.{q(schema)}")
    db = lite(path)
    for table in table_names(db, db_id):
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
    log_change(db_id, "load", f"{schema} recreated from SQLite: {len(table_names(db, db_id))} tables")


# BookSQL questions are relative to "today"; its data ends on this date
TODAY = {"accounting": "2023-04-26"}
# numbers stored as text with '--' for "none": SQLite counts '--' as 0 in arithmetic, Snowflake raises an error
TEXT_NUMBERS = {"ACCOUNTING": {"QUANTITY", "RATE"}}
DOW = ["su", "mo", "tu", "we", "th", "fr", "sa"]  # SQLite 'weekday N': 0 = Sunday


def repair_gold(sql, db_id):
    """Undo damage from FINCH lower-casing its gold SQL, string literals included:
    - 'east bohemia' vs data 'east Bohemia': compare string predicates case-insensitively
    - strftime('%y') was '%Y' (4-digit year)
    and pin CURRENT_DATE / 'now' for BookSQL, whose data stops at TODAY."""
    sql = re.sub(r"(strftime\(\s*')([^']*)'", lambda m: m[1] + m[2].replace("%y", "%Y") + "'", sql, flags=re.I)
    if db_id in TODAY:
        sql = re.sub(r"\bcurrent_(date|timestamp)\b|'now'", f"'{TODAY[db_id]}'", sql, flags=re.I)
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


def compares_text_with_number(sql, db_id):
    """Gold bug: STRFTIME() returns text, which SQLite never treats as equal to, or less than, a number. So
    STRFTIME('%Y', d) >= STRFTIME('%Y', 'now') - 1 is always true and the gold answer is meaningless."""
    def is_strftime(e):
        e = (e.this if isinstance(e, exp.Lower) else e).unnest()
        return e.sql("sqlite").upper().startswith("STRFTIME(")

    def is_number(e):
        e = e.unnest()
        return isinstance(e, (exp.Add, exp.Sub, exp.Neg)) or (isinstance(e, exp.Literal) and not e.is_string)

    for n in sqlglot.parse_one(repair_gold(sql, db_id), read="sqlite").find_all(
            exp.EQ, exp.NEQ, exp.LT, exp.GT, exp.LTE, exp.GTE, exp.Between, exp.In):
        others = ([n.args["low"], n.args["high"]] if isinstance(n, exp.Between)
                  else n.expressions if isinstance(n, exp.In) else [n.expression])
        if (is_strftime(n.this) and any(map(is_number, others))) or (
                len(others) == 1 and is_strftime(others[0]) and is_number(n.this)):
            return True
    return False


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


def distinct_order_by(sel):
    """SELECT DISTINCT a ORDER BY b: Snowflake rejects it unless b is selected; rewrite as GROUP BY a ORDER BY MAX/MIN(b)."""
    order = sel.args.get("order")
    if not (sel.args.get("distinct") and order) or sel.args.get("group") or sel.find(exp.AggFunc, exp.Window):
        return
    cols = [e.unalias() for e in sel.expressions]
    names = {e.alias_or_name.lower() for e in sel.expressions}
    if any(isinstance(c, exp.Star) for c in cols) or all(
        o.this in cols or (isinstance(o.this, exp.Column) and o.this.name.lower() in names) or isinstance(o.this, exp.Literal)
        for o in order.expressions
    ):
        return
    sel.set("distinct", None)
    sel.set("group", exp.Group(expressions=[c.copy() for c in cols]))
    for o in order.expressions:
        if o.this not in cols and not (isinstance(o.this, exp.Column) and o.this.name.lower() in names):
            # ponytail: assumes b is constant per a (SQLite keeps an arbitrary one); exclude the question if it isn't
            o.set("this", (exp.Max if o.args.get("desc") else exp.Min)(this=o.this.copy()))


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
    for sel in tree.find_all(exp.Select):
        distinct_order_by(sel)
    for col in list(tree.find_all(exp.Column)):
        if col.name.upper() in TEXT_NUMBERS.get(schema, ()) and isinstance(
                col.parent, (exp.Sum, exp.Avg, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Cast)):
            c = col.sql("snowflake")
            col.replace(sqlglot.parse_one(f"IFF({c} IS NULL, NULL, COALESCE(TRY_TO_DOUBLE({c}), 0))", read="snowflake"))
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


def load_pairs(sf, database, only=None):
    pairs = pd.DataFrame(json.loads(real_file("finch_dataset.json").read_text()))
    if only:
        pairs = pairs[pairs["db_id"].isin(only)]
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
    log_change(None, "pairs", f"BENCHMARK.NL_SQL_PAIRS reloaded: {len(pairs):,} questions, {sum(e is not None for e in errs)} conversion errors")
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
    atexit.register(suspend, sf, warehouse)  # also after an error; not after kill -9
    return sf


def suspend(sf, warehouse):
    """Stop paying for the warehouse as soon as the script ends, instead of waiting for its auto-suspend."""
    try:
        sf.cursor().execute(f"alter warehouse {q(warehouse)} suspend")
        print(f"warehouse {warehouse} suspended")
    except Exception as e:  # already suspended, or the connection is gone
        print(f"warehouse {warehouse} not suspended: {str(e).splitlines()[0][:120]}")


def main():
    ap = argparse.ArgumentParser()
    connection_args(ap)
    ap.add_argument("--pairs-only", action="store_true", help="reload BENCHMARK.NL_SQL_PAIRS only")
    ap.add_argument("--keys-only", action="store_true", help="declare primary/foreign keys and write BENCHMARK.TABLE_JOINS only")
    ap.add_argument("--only", nargs="+", metavar="DB_ID", help="load only these databases, e.g. --only financial (saves time and credits)")
    args = ap.parse_args()

    sf = connect(args)
    questions = json.loads(real_file("finch_dataset.json").read_text())
    if args.only:
        questions = [x for x in questions if x["db_id"] in args.only]
    if args.keys_only:
        load_keys(sf, args.database, questions)
        return
    sf.cursor().execute(f"create database if not exists {args.database}")

    db_ids = load_pairs(sf, args.database, args.only)
    if args.pairs_only:
        return
    for path in sorted((ROOT / "text2sql-db").rglob("*.sqlite")):
        if path.stem not in db_ids:  # e.g. stray bird/bird.sqlite, not referenced by any question
            print(f"skipping {path.relative_to(ROOT)} (no questions use it)")
            continue
        print(path.stem)
        load_sqlite(sf, args.database, path.stem, real_file(str(path.relative_to(ROOT))))
    load_keys(sf, args.database, questions)


if __name__ == "__main__":
    main()
