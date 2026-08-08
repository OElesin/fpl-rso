"""
Called by CodeBuild (BUILD_PHASE=deploy_bot) to update the bot Lambda
with the latest strategy from a completed RSI run.

Finds the most recent completed run in DynamoDB, extracts the strategy,
and hot-swaps it into the existing Lambda deployment.
"""

import os
import sys
import json
import zipfile
import tempfile
import io

import boto3

REGION = os.environ.get("REGION", "us-east-1")
STATE_TABLE = os.environ.get("STATE_TABLE", "fpl-rso-state")
LAMBDA_BOT = os.environ.get("LAMBDA_BOT", "fpl-rso-service-TelegramWebhook-7XLhspJ9YcHa")
LAMBDA_API = os.environ.get("LAMBDA_API", "fpl-rso-service-FplRsoApi-GeTgg76wRBIi")


def find_latest_completed_run():
    """Find the most recently completed RSI run."""
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(STATE_TABLE)

    scan = table.scan()
    items = scan.get("Items", [])

    completed = [i for i in items if i.get("status") == "completed"]
    if not completed:
        print("ERROR: No completed runs found")
        sys.exit(1)

    completed.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return completed[0]


def main():
    print("Finding latest completed RSI run...")
    state = find_latest_completed_run()

    run_id = state.get("run_id", "unknown")
    best_score = float(str(state.get("best_score", 0)))
    baseline = float(str(state.get("baseline_score", 0)))
    strategy = state.get("best_strategy", "")

    print(f"  Run: {run_id}")
    print(f"  Score: {baseline:.2f} → {best_score:.2f} pts/GW")

    if not strategy:
        print("ERROR: No strategy in state")
        sys.exit(1)

    # Fix import path for the service repo
    strategy = strategy.replace("from inner_agent.player", "from trained_strategy.player")

    lambda_client = boto3.client("lambda", region_name=REGION)

    # Download existing Lambda code, replace strategy, re-upload
    for func_name in [LAMBDA_BOT, LAMBDA_API]:
        print(f"  Updating {func_name}...")

        # Get current code
        response = lambda_client.get_function(FunctionName=func_name)
        code_url = response["Code"]["Location"]

        import urllib.request
        code_zip_data, _ = urllib.request.urlretrieve(code_url)

        # Read existing zip, replace strategy file
        new_zip_buffer = io.BytesIO()
        with zipfile.ZipFile(code_zip_data, "r") as old_zip:
            with zipfile.ZipFile(new_zip_buffer, "w", zipfile.ZIP_DEFLATED) as new_zip:
                for item in old_zip.namelist():
                    if item == "trained_strategy/strategy.py":
                        new_zip.writestr(item, strategy)
                    else:
                        new_zip.writestr(item, old_zip.read(item))

        # Upload
        new_zip_buffer.seek(0)
        lambda_client.update_function_code(
            FunctionName=func_name,
            ZipFile=new_zip_buffer.read(),
        )

    print(f"\n✅ Bot updated with strategy from {run_id} ({best_score:.2f} pts/GW)")


if __name__ == "__main__":
    main()
