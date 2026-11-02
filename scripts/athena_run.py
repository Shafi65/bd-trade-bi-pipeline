"""Run a .sql file (or an inline query) on Athena and print the results.

Usage:
    ./.venv/bin/python scripts/athena_run.py sql/create_external_tables.sql
    ./.venv/bin/python scripts/athena_run.py -q "SELECT count(*) FROM bd_trade.fact_trade"
    ./.venv/bin/python scripts/athena_run.py -q "SELECT ..." --csv out.csv

Why a script and not the console: the DDL and views are code, so deploying them
should be repeatable — one command rebuilds the whole catalog. Statements are split
on ';' and run in order. REPLACE_ME_BUCKET is substituted from the ATHENA_BUCKET env
var (set in .env) so the bucket name never lives in the (public) repo.

Uses the AWS CLI (already configured for the IAM user) rather than adding boto3.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()  # ATHENA_BUCKET lives in the git-ignored .env (template: .env.example)
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "bd-trade")
BUCKET = os.environ.get("ATHENA_BUCKET", "REPLACE_ME_BUCKET")


def aws(*args):
    out = subprocess.run(["aws", "athena", *args, "--output", "json"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(out.stderr)
    return json.loads(out.stdout) if out.stdout.strip() else {}


def run(sql):
    qid = aws("start-query-execution", "--work-group", WORKGROUP,
              "--query-string", sql)["QueryExecutionId"]
    while True:
        ex = aws("get-query-execution", "--query-execution-id", qid)["QueryExecution"]
        state = ex["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if state != "SUCCEEDED":
        sys.exit(f"{state}: {ex['Status'].get('StateChangeReason')}\n--- SQL ---\n{sql[:600]}")
    scanned = ex["Statistics"].get("DataScannedInBytes", 0)
    rows, token = [], None
    while True:
        args = ["get-query-results", "--query-execution-id", qid]
        if token:
            args += ["--next-token", token]
        res = aws(*args)
        rows += [[c.get("VarCharValue") for c in r["Data"]] for r in res["ResultSet"]["Rows"]]
        token = res.get("NextToken")
        if not token:
            break
    return rows, scanned


def split_statements(text):
    """Split on ';' outside strings and '--' comments (both may contain ';')."""
    stmts, buf, in_str, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if not in_str and text.startswith("--", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        i += 1
        if ch == "'":
            in_str = not in_str
        if ch == ";" and not in_str:
            stmts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    stmts.append("".join(buf))
    return [s.strip() for s in stmts if s.strip()]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("file", nargs="?")
    p.add_argument("-q", "--query")
    p.add_argument("--csv", help="write the last statement's result to this CSV")
    p.add_argument("--limit", type=int, default=25, help="rows to print")
    a = p.parse_args()
    text = a.query if a.query else open(a.file).read()
    stmts = split_statements(text.replace("REPLACE_ME_BUCKET", BUCKET))
    for sql in stmts:
        head = " ".join(sql.split())[:80]
        rows, scanned = run(sql)
        print(f"OK  [{scanned / 1e6:.2f} MB scanned]  {head}")
        if rows and len(rows) > 1 or (rows and not sql.upper().startswith(("CREATE", "DROP"))):
            for r in rows[: a.limit + 1]:
                print("   ", " | ".join("" if v is None else v for v in r))
            if len(rows) > a.limit + 1:
                print(f"    ... {len(rows) - 1} rows total")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            csv.writer(f).writerows(rows)


if __name__ == "__main__":
    main()
