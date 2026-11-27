# Running the FINCH evaluation on your own (work) Snowflake account

Step by step, from an empty Snowflake account to the runs page. It sets up the `financial` database only (the useful
one), its disguised copy, and the scripts to evaluate Cortex models on them. Allow about an hour, most of it waiting
for approvals and downloads.

## 0. What it costs

| Step | Cost |
|---|---|
| Loading `financial` and `financial_disguised` | A few minutes on an X-Small warehouse (well under 1 credit) |
| One model run (106 questions) | About 0.6-0.8 Cortex AI credits (Opus costs more than Sonnet; longer descriptions cost more), plus 4-12 minutes of warehouse time |
| Storage | About 1 GB of table data: a small monthly charge |

Start every new kind of run with `--sample 10` (10 questions) to check it works before running all 106.

## 1. Check with your Snowflake / platform team first

Ask for, or confirm:

1. **Permission to use Cortex AI functions** (`SNOWFLAKE.CORTEX.COMPLETE`) and which models are approved. Some
   companies restrict models or keep data in-region; the models you can call may differ from the list in this repo's
   runs.
2. **A role** that can create a database and use a warehouse, and has the `SNOWFLAKE.CORTEX_USER` database role.
3. **A dedicated warehouse** for this work. The scripts suspend the warehouse they use as soon as they finish, to save
   credits; on a shared warehouse that would interrupt colleagues.
4. **How you log in from scripts**: usually SSO in the browser (`externalbrowser`), or a key pair.
5. **Whether you may load public test data** (FINCH is a public benchmark: synthetic and anonymised bank data).

If you have the rights yourself, step 4 has the SQL to set it all up.

## 2. Get the code

```bash
git clone https://github.com/jezdaviesrepo/finch.git
cd finch
git checkout snowflake-eval
```

If GitHub is blocked at work, use the zip of the `snowflake-eval` branch instead and unzip it.

## 3. Python environment (Python 3.9 or later)

```bash
python3 -m venv .venv
.venv/bin/pip install -r snowflake/requirements.txt
```

If pip can't reach the internet, ask for your company's package mirror and add
`--index-url <mirror URL>`.

## 4. Snowflake objects (run in a worksheet, as a role that may create them)

Change the names to fit your company's conventions; the rest of this guide uses these.

```sql
use role sysadmin;  -- or the role your team gives you

-- a small dedicated warehouse that stops after 60 seconds idle
create warehouse if not exists FINCH_WH
  warehouse_size = xsmall auto_suspend = 60 auto_resume = true initially_suspended = true;

create database if not exists FINCH;

-- optional but recommended: cap warehouse spend (does NOT cap Cortex AI credits)
use role accountadmin;
create resource monitor if not exists FINCH_CAP with credit_quota = 10 frequency = monthly
  start_timestamp = immediately triggers on 90 percent do notify on 100 percent do suspend;
alter warehouse FINCH_WH set resource_monitor = FINCH_CAP;

-- let your role call Cortex models (replace MY_ROLE)
grant database role snowflake.cortex_user to role MY_ROLE;
grant usage on warehouse FINCH_WH to role MY_ROLE;
grant ownership on database FINCH to role MY_ROLE;
```

Check which models you can actually call. A model can be listed but not callable:

```sql
show models in snowflake.models;
select snowflake.cortex.complete('claude-opus-5-5', 'Reply with OK');
select snowflake.cortex.complete('claude-sonnet-4-5', 'Reply with OK');
```

If a model says "unavailable" or "unknown model", it isn't served in your region. Your account admin can allow
cross-region calls (`alter account set cortex_enabled_cross_region = 'ANY_REGION';`), but that is a company decision
because the prompts then leave your region.

## 5. Connection from your machine

Create `~/.snowflake/connections.toml` (the file is read by the scripts; never commit it):

```toml
[work]
account = "<your account identifier, e.g. myorg-myaccount>"
user = "<your Snowflake user>"
authenticator = "externalbrowser"   # SSO in the browser; or use key-pair settings from your team
role = "MY_ROLE"
warehouse = "FINCH_WH"
database = "FINCH"
```

With SSO, every script opens a browser login. Approve it within about 2 minutes or the script stops with
"Unable to receive the OAuth message within a given timeout"; just run it again.

## 6. The data files

The scripts download the FINCH files they need into `data/` the first time (from GitHub's file storage,
`media.githubusercontent.com`). For `financial` only, that is two files, about 110 MB:

- `data/finch_dataset.json` (36 MB): all 75,725 questions and their gold SQL
- `data/text2sql-db/text2sql/bird/financial.sqlite` (71 MB): the database, also used as the answer key

If that site is blocked at work, copy those two files from another machine into the same paths, then the scripts use
them and skip the download.

## 7. Start your own run history

`results/history.sqlite` holds the runs made while building this. To number your runs from 1, move it aside:

```bash
mv results/history.sqlite results/history_original.sqlite
```

Every load, description and run from here on is logged in the new history automatically.

## 8. Load `financial` into Snowflake

```bash
.venv/bin/python snowflake/load_finch.py --connection work --warehouse FINCH_WH --database FINCH --only financial
```

This creates schema `FINCH.FINANCIAL` (8 tables), `FINCH.BENCHMARK.NL_SQL_PAIRS` (its 106 questions) and the keys, and
writes `FINCH.BENCHMARK.TABLE_JOINS`. Leave out `--only financial` to load all 33 FINCH databases (2 GB, much slower).

## 9. Build and load the disguised copy

The disguised copy renames the `district` columns so a model can't rely on remembering this public dataset:

```bash
.venv/bin/python snowflake/disguise.py --load --connection work --warehouse FINCH_WH --database FINCH
```

It checks that all 106 rewritten gold queries give the same answers, then creates `FINCH.FINANCIAL_DISGUISED` with its
questions and keys.

## 10. Add the table descriptions (context)

The approved descriptions are in `snowflake/context/`:

```bash
.venv/bin/python snowflake/context.py financial_disguised --connection work --warehouse FINCH_WH --database FINCH \
  --why "Logical descriptions for all tables"
.venv/bin/python snowflake/context.py financial --connection work --warehouse FINCH_WH --database FINCH \
  --why "Logical descriptions for all tables"
```

Skip `financial` if you want a no-description baseline there first.

## 11. Run evaluations

Test with 10 questions first, then run all 106 (`--sample 0`):

```bash
# smoke test
.venv/bin/python snowflake/evaluate.py --connection work --warehouse FINCH_WH --database FINCH \
  --model claude-opus-5-5 --db financial_disguised --sample 10 --keys --label "Smoke test"

# the fair test: disguised copy, descriptions and keys in the prompt
.venv/bin/python snowflake/evaluate.py --connection work --warehouse FINCH_WH --database FINCH \
  --model claude-opus-5-5 --db financial_disguised --sample 0 --keys \
  --label "financial_disguised, Opus 5.5, descriptions plus keys"
```

Useful options:

| Option | What it does |
|---|---|
| `--model NAME` | Any Cortex model you can call, e.g. `claude-sonnet-4-5`, `openai-gpt-5.5` |
| `--db financial` / `financial_disguised` | Which database's questions |
| `--keys` | Show primary and foreign keys in the prompt |
| `--no-schema` | Leave the tables out (tests what the model remembers) |
| `--sample N` | Number of questions (0 = all) |
| `--label "..."` | What the run is, shown on the page |

Each run prints progress with time elapsed and time left, saves everything to `results/history.sqlite`, and suspends
the warehouse at the end. Run the same thing twice to see how much the score moves by chance (about +/- 3 questions
of 106).

## 12. The runs page

```bash
.venv/bin/python snowflake/report.py --runs
open results/report.html
```

It shows every run (strict and lenient scores, by difficulty, each question with the gold and model SQL, the prompt),
a chart of accuracy by run, and the database changes made before each run. Add notes to a run with
`.venv/bin/python snowflake/history.py note 1 "what this run showed"`.

## 13. Disputed gold answers

Some FINCH gold answers are wrong. The reviewed ones are in `snowflake/gold_review.json` and are applied
automatically. When you find another, add it there and re-score the stored runs:

```bash
.venv/bin/python snowflake/rescore.py && .venv/bin/python snowflake/report.py --runs
```

## 14. Watch the cost

```sql
-- Cortex AI credits per day
select usage_date, service_type, sum(credits_used) as credits
from snowflake.account_usage.metering_daily_history
where usage_date >= dateadd(day, -14, current_date)
group by 1, 2 order by 1, 2;

-- by model
select model_name, sum(tokens) as tokens, sum(token_credits) as credits
from snowflake.account_usage.cortex_functions_usage_history
where start_time >= dateadd(day, -14, current_timestamp)
group by 1 order by credits desc;
```

These views lag by an hour or more. In Snowsight: Admin, then Cost Management, then Consumption, service type
"AI Services".

## 15. Clean up

```sql
drop database FINCH;            -- all tables, questions and results in Snowflake
drop warehouse FINCH_WH;
drop resource monitor FINCH_CAP;
```

## Troubleshooting

| Message | Fix |
|---|---|
| `Unable to receive the OAuth message within a given timeout` | The browser login wasn't approved in time; run again and approve within 2 minutes |
| `Model "..." is unavailable` / `unknown model` | Not served in your region or not allowed; pick another model or ask about cross-region (step 4) |
| `Not authorized to access model` | Your role lacks `SNOWFLAKE.CORTEX_USER` or the model isn't on your account's allow list |
| `Object ... does not exist or not authorized` | Wrong `--database`, or your role can't see the schema |
| Download errors for `data/...` | `media.githubusercontent.com` is blocked; copy the files in by hand (step 6) |
| `warehouse ... not suspended: Invalid state` | Harmless: it was already suspended |
