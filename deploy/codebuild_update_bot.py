"""
Called by CodeBuild (BUILD_PHASE=deploy_bot) to update the bot Lambda
with the latest strategy from a completed RSI run.

Finds the most recent completed run in DynamoDB, extracts the strategy,
packages it with dependencies, and deploys to both Lambda functions.
"""

import os
import sys
import json
import zipfile
import subprocess
import tempfile
from pathlib import Path

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

    # Sort by updated_at
    completed.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return completed[0]


def main():
    print("Finding latest completed RSI run...")
    state = find_latest_completed_run()

    run_id = state.get("run_id", "unknown")
    best_score = float(str(state.get("best_score", 0)))
    baseline = float(str(state.get("baseline_score", 0)))
    iterations = state.get("iteration", 0)
    strategy = state.get("best_strategy", "")

    print(f"  Run: {run_id}")
    print(f"  Score: {baseline:.2f} → {best_score:.2f} pts/GW")
    print(f"  Iterations: {iterations}")

    if not strategy:
        print("ERROR: No strategy in state")
        sys.exit(1)

    # Write strategy to trained_strategy/
    strategy_path = Path("trained_strategy/strategy.py")
    strategy_path.parent.mkdir(exist_ok=True)
    strategy_path.write_text(strategy)
    print(f"  Strategy written ({len(strategy)} chars)")

    # Fix import path
    content = strategy_path.read_text()
    content = content.replace("from inner_agent.player", "from trained_strategy.player")
    strategy_path.write_text(content)

    # Install dependencies into a temp dir and build zip
    print("Building Lambda package...")
    pkg_dir = tempfile.mkdtemp()

    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--target", pkg_dir,
            "--platform", "manylinux2014_x86_64",
            "--implementation", "cp",
            "--python-version", "3.13",
            "--only-binary=:all:",
            "python-telegram-bot", "pandas", "numpy", "requests",
            "cachetools", "mangum", "fastapi", "pydantic",
        ],
        check=True,
        capture_output=True,
    )

    # Copy our source code
    for src_dir in ["api", "bot", "trained_strategy"]:
        subprocess.run(["cp", "-r", src_dir, pkg_dir], check=True)

    # Create zip
    zip_path = "/tmp/fpl-bot-deploy.zip"
    subprocess.run(
        ["bash", "-c", f"cd {pkg_dir} && zip -r {zip_path} . -x '*__pycache__*' -x '*.dist-info/*' -x '*/tests/*'"],
        check=True,
        capture_output=True,
    )

    zip_size = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"  Package size: {zip_size:.1f} MB")

    # Deploy to both Lambdas
    lambda_client = boto3.client("lambda", region_name=REGION)

    print(f"  Deploying to {LAMBDA_BOT}...")
    with open(zip_path, "rb") as f:
        lambda_client.update_function_code(
            FunctionName=LAMBDA_BOT, ZipFile=f.read()
        )

    print(f"  Deploying to {LAMBDA_API}...")
    with open(zip_path, "rb") as f:
        lambda_client.update_function_code(
            FunctionName=LAMBDA_API, ZipFile=f.read()
        )

    print(f"\n✅ Bot updated with strategy from {run_id} ({best_score:.2f} pts/GW)")


if __name__ == "__main__":
    main()
