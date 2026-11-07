"""Wire QuickSight to the Athena views: one data source + one SPICE dataset per view.

Usage:
    ./.venv/bin/python scripts/quicksight_setup.py

What it creates (idempotent — re-running updates instead of duplicating):
  * DATA SOURCE  bd-trade-athena   -> Athena, workgroup ATHENA_WORKGROUP
  * DATASETS     one per view in VIEWS, imported into SPICE (QuickSight's in-memory
                 store), so dashboards don't re-query Athena on every page load.

Column names/types are read from the Glue Data Catalog — the catalog is the single
schema definition, so nothing is re-typed here by hand.

Prerequisite: the QuickSight service role can read the bucket (s3:GetObject on
processed/*, read/write on athena-results/*). QuickSight's managed Athena policy only
covers buckets named aws-athena-query-results-*, so this is a separate grant.
"""
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()
REGION = "us-east-1"
DATABASE = "bd_trade"
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "bd-trade")
SOURCE_ID = "bd-trade-athena"
VIEWS = [
    "v_export_kpis",
    "v_price_trend",
    "v_price_change",
    "v_destination_deviation",
    "v_diversification",
    "v_diversification_drivers",
    "v_market_penetration",
    "v_price_movers",
    "v_market_discount",
    "v_headline_kpis",
]
# Glue/Athena type -> QuickSight input column type
TYPE_MAP = {"string": "STRING", "varchar": "STRING", "int": "INTEGER", "bigint": "INTEGER",
            "double": "DECIMAL", "boolean": "BOOLEAN"}

SOURCE_ACTIONS = ["quicksight:DescribeDataSource", "quicksight:DescribeDataSourcePermissions",
                  "quicksight:PassDataSource", "quicksight:UpdateDataSource",
                  "quicksight:DeleteDataSource", "quicksight:UpdateDataSourcePermissions"]
DATASET_ACTIONS = ["quicksight:DescribeDataSet", "quicksight:DescribeDataSetPermissions",
                   "quicksight:PassDataSet", "quicksight:DescribeIngestion",
                   "quicksight:ListIngestions", "quicksight:UpdateDataSet",
                   "quicksight:DeleteDataSet", "quicksight:CreateIngestion",
                   "quicksight:CancelIngestion", "quicksight:UpdateDataSetPermissions"]


def aws(service, *args, check=True):
    out = subprocess.run(["aws", service, *args, "--region", REGION, "--output", "json"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        if check:
            sys.exit(out.stderr)
        return None, out.stderr
    return (json.loads(out.stdout) if out.stdout.strip() else {}), None


def create_or_update(kind, ident_flag, ident, body, perms):
    """create-<kind>; if it already exists, update-<kind> (permissions set on create)."""
    _, err = aws("quicksight", f"create-{kind}", "--cli-input-json",
                 json.dumps({**body, ident_flag: ident, "Permissions": perms}), check=False)
    if err and "ResourceExistsException" in err:
        update = {k: v for k, v in body.items() if k != "Type"}  # type is fixed at creation
        aws("quicksight", f"update-{kind}", "--cli-input-json",
            json.dumps({**update, ident_flag: ident}))
        return "updated"
    if err:
        sys.exit(err)
    return "created"


def glue_columns(view):
    t, _ = aws("glue", "get-table", "--database-name", DATABASE, "--name", view)
    return [{"Name": c["Name"], "Type": TYPE_MAP[c["Type"].split("(")[0]]}
            for c in t["Table"]["StorageDescriptor"]["Columns"]]


def main():
    account = aws("sts", "get-caller-identity")[0]["Account"]
    user = os.environ.get("QS_USER", account)
    principal = f"arn:aws:quicksight:{REGION}:{account}:user/default/{user}"
    base = {"AwsAccountId": account}

    status = create_or_update(
        "data-source", "DataSourceId", SOURCE_ID,
        {**base, "Name": "BD trade (Athena)", "Type": "ATHENA",
         "DataSourceParameters": {"AthenaParameters": {"WorkGroup": WORKGROUP}}},
        [{"Principal": principal, "Actions": SOURCE_ACTIONS}])
    print(f"data source {SOURCE_ID}: {status}")
    source_arn = f"arn:aws:quicksight:{REGION}:{account}:datasource/{SOURCE_ID}"
    for _ in range(30):  # data source creation is async
        ds, _ = aws("quicksight", "describe-data-source", "--aws-account-id", account,
                    "--data-source-id", SOURCE_ID)
        st = ds["DataSource"]["Status"]
        if st.endswith("SUCCESSFUL"):
            break
        if st.endswith("FAILED"):
            sys.exit(f"data source failed: {ds['DataSource'].get('ErrorInfo')}")
        time.sleep(2)

    for view in VIEWS:
        ds_id = f"bd-trade-{view.replace('_', '-')}"
        status = create_or_update(
            "data-set", "DataSetId", ds_id,
            {**base, "Name": view, "ImportMode": "SPICE",
             "PhysicalTableMap": {"t": {"RelationalTable": {
                 "DataSourceArn": source_arn, "Catalog": "AwsDataCatalog",
                 "Schema": DATABASE, "Name": view, "InputColumns": glue_columns(view)}}}},
            [{"Principal": principal, "Actions": DATASET_ACTIONS}])
        print(f"dataset {view}: {status}")

    # Each create/update starts a SPICE ingestion (Athena query -> in-memory copy). Wait.
    print("waiting for SPICE ingestion ...")
    for view in VIEWS:
        ds_id = f"bd-trade-{view.replace('_', '-')}"
        for _ in range(90):
            ing, _ = aws("quicksight", "list-ingestions", "--aws-account-id", account,
                         "--data-set-id", ds_id)
            latest = sorted(ing["Ingestions"], key=lambda i: i["CreatedTime"])[-1]
            if latest["IngestionStatus"] in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            time.sleep(3)
        rows = latest.get("RowInfo", {}).get("RowsIngested")
        err = latest.get("ErrorInfo", {}).get("Message", "")
        print(f"  {view:28s} {latest['IngestionStatus']:10s} rows={rows} {err}")


if __name__ == "__main__":
    main()
