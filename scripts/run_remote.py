#!/usr/bin/env python3
"""
Trigger the full RSI loop on Bedrock AgentCore Runtime.

No local computation — everything runs in the cloud.
You just fire and collect the best strategy when it's done.

Usage:
    # Run 50 iterations on AgentCore (fire and forget)
    python scripts/run_remote.py --arn <agentcore-arn>

    # Custom config
    python scripts/run_remote.py --arn <arn> --iterations 100 --model claude-sonnet-5

    # With specific seasons
    python scripts/run_remote.py --arn <arn> --seasons 2022-23 2023-24 --iterations 50
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

import boto3
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "settings.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def run_remote(args):
    """Invoke the full RSI loop on AgentCore Runtime."""
    config = load_config()
    outer_cfg = config.get("outer_loop", {})
    bt_cfg = config.get("backtest", {})

    arn = args.arn or outer_cfg.get("agentcore_arn")
    if not arn:
        print("ERROR: No AgentCore ARN provided.")
        print("  Pass --arn or set agentcore_arn in config/settings.yaml")
        sys.exit(1)

    region = args.region or outer_cfg.get("region", "us-east-1")
    profile = args.profile or outer_cfg.get("profile")

    # Build payload
    payload = {
        "iterations": args.iterations or outer_cfg.get("max_iterations", 50),
        "seasons": args.seasons or bt_cfg.get("seasons", ["2022-23", "2023-24"]),
        "model": args.model or outer_cfg.get("model", "claude-sonnet-5"),
        "region": region,
        "improvement_threshold": outer_cfg.get("improvement_threshold", 0.1),
        "public_gw_ratio": bt_cfg.get("public_gw_ratio", 0.6),
        "random_seed": bt_cfg.get("random_seed", 42),
    }

    # Optionally pass custom starting strategy
    if args.strategy_file:
        strategy_path = Path(args.strategy_file)
        if strategy_path.exists():
            payload["strategy_code"] = strategy_path.read_text()
            print(f"Using custom starting strategy from: {args.strategy_file}")

    print(f"\n{'='*60}")
    print(f"  FPL-RSO Remote Execution (Bedrock AgentCore)")
    print(f"{'='*60}")
    print(f"  AgentCore ARN: {arn}")
    print(f"  Region: {region}")
    print(f"  Iterations: {payload['iterations']}")
    print(f"  Model: {payload['model']}")
    print(f"  Seasons: {payload['seasons']}")
    print(f"{'='*60}\n")
    print("Invoking AgentCore Runtime... (this may take a while)")
    print(f"  Estimated time: ~{payload['iterations'] * 2} minutes for {payload['iterations']} iterations\n")

    # Create client with extended timeout (loop can run for many minutes)
    session_kwargs = {}
    if profile:
        session_kwargs["profile_name"] = profile
    session = boto3.Session(**session_kwargs)

    from botocore.config import Config
    client = session.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(
            read_timeout=900,  # 15 minutes
            connect_timeout=30,
            retries={"max_attempts": 0},
        ),
    )

    # Invoke
    session_id = f"fpl-rso-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-0000000000"

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode(),
        )

        # AgentCore returns response in 'response' field (StreamingBody)
        if "response" in response:
            body = response["response"]
            if hasattr(body, "read"):
                raw = body.read()
            else:
                raw = body
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            result = json.loads(raw) if raw.strip() else {"status": "error", "message": "Empty response body"}
        elif "payload" in response:
            result = json.loads(response["payload"].read())
        else:
            result = {"status": "error", "message": f"Unexpected response keys: {list(response.keys())}"}

        print(f"  HTTP Status: {response.get('statusCode', 'N/A')}")

    except Exception as e:
        print(f"ERROR: AgentCore invocation failed: {e}")
        sys.exit(1)

    # Display results
    if result.get("status") == "success":
        print(f"\n{'='*60}")
        print(f"  RSI Loop Complete")
        print(f"{'='*60}")
        print(f"  Iterations run: {result['iterations_run']}")
        print(f"  Improvements found: {result['improvements_found']}")
        print(f"  Acceptance rate: {result['acceptance_rate']*100:.0f}%")
        print(f"  Baseline score: {result['baseline_score']:.2f} avg pts/GW")
        print(f"  Best score: {result['best_score']:.2f} avg pts/GW")
        print(f"  Improvement: +{result['improvement']:.2f} pts/GW")
        print(f"  Best iteration: {result.get('best_iteration', 'N/A')}")
        print(f"{'='*60}\n")

        # Save best strategy locally
        output_dir = PROJECT_ROOT / "outer_loop" / "runs" / session_id
        output_dir.mkdir(parents=True, exist_ok=True)

        best_strategy_path = output_dir / "best_strategy.py"
        best_strategy_path.write_text(result["best_strategy"])
        print(f"Best strategy saved to: {best_strategy_path}")

        # Save full history
        history_path = output_dir / "history.json"
        history_path.write_text(json.dumps(result["history"], indent=2))
        print(f"History saved to: {history_path}")

        # Optionally install as active strategy
        if args.install:
            active_path = PROJECT_ROOT / "inner_agent" / "strategy.py"
            active_path.write_text(result["best_strategy"])
            print(f"\nInstalled as active strategy: {active_path}")

        print(f"\nMessage: {result['message']}")

    else:
        print(f"\nERROR: {result.get('message', 'Unknown error')}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Run FPL-RSO loop remotely on Bedrock AgentCore Runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_remote.py --arn arn:aws:bedrock:us-east-1:123456:agent-runtime/fpl-rso
  python scripts/run_remote.py --arn <arn> --iterations 100 --model claude-sonnet-5
  python scripts/run_remote.py --arn <arn> --install  # Auto-install best strategy locally
        """,
    )

    parser.add_argument(
        "--arn", type=str, default=None,
        help="AgentCore Runtime ARN (required)"
    )
    parser.add_argument(
        "--iterations", type=int, default=None,
        help="Number of RSI iterations (default: 50)"
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Bedrock model (default: claude-sonnet-5)"
    )
    parser.add_argument(
        "--region", type=str, default=None,
        help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--profile", type=str, default=None,
        help="AWS profile name"
    )
    parser.add_argument(
        "--seasons", nargs="+", default=None,
        help="Seasons to backtest on"
    )
    parser.add_argument(
        "--strategy-file", type=str, default=None,
        help="Path to custom starting strategy (default: use built-in baseline)"
    )
    parser.add_argument(
        "--install", action="store_true",
        help="Auto-install the best discovered strategy as active agent"
    )

    args = parser.parse_args()
    run_remote(args)


if __name__ == "__main__":
    main()
