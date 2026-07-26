#!/usr/bin/env python3
"""
FPL-RSO — Main entry point for the recursive self-improvement loop.

Usage:
    # Run the full outer loop (default: 50 iterations)
    python scripts/run.py

    # Run with custom settings
    python scripts/run.py --iterations 100 --model claude-sonnet --region us-east-1

    # Evaluate the current agent without running the loop
    python scripts/run.py --eval-only

    # Fetch data first
    python scripts/run.py --fetch-data
"""

import os
import sys
import argparse
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml


def load_config() -> dict:
    """Load settings from config/settings.yaml."""
    config_path = PROJECT_ROOT / "config" / "settings.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


def fetch_data(seasons: list[str] | None = None):
    """Download FPL historical data."""
    from data.fetcher import fetch_all

    config = load_config()
    if seasons is None:
        seasons = config.get("data", {}).get("seasons_to_fetch")
    fetch_all(seasons)


def eval_only(seasons: list[str] | None = None):
    """Evaluate the current inner agent without running the outer loop."""
    from data.loader import load_seasons
    from eval.backtest import evaluate_agent, BacktestConfig

    config = load_config()
    bt_config = config.get("backtest", {})

    if seasons is None:
        seasons = bt_config.get("seasons", ["2023-24"])

    print(f"Evaluating current agent on seasons: {seasons}")
    seasons_data = load_seasons(seasons)

    if not seasons_data:
        print("ERROR: No data found. Run with --fetch-data first.")
        sys.exit(1)

    backtest_cfg = BacktestConfig(
        seasons=seasons,
        public_gw_ratio=bt_config.get("public_gw_ratio", 0.6),
        random_seed=bt_config.get("random_seed", 42),
        start_gameweek=bt_config.get("start_gameweek", 5),
        form_window=bt_config.get("form_window", 5),
    )

    result = evaluate_agent(backtest_cfg, seasons_data)

    print(f"\n{'='*50}")
    print(f"  Agent Evaluation Results")
    print(f"{'='*50}")
    print(f"  Public score:  {result.public_score:.2f} avg pts/GW")
    print(f"  Private score: {result.private_score:.2f} avg pts/GW")
    print(f"  Total score:   {result.total_score:.2f} avg pts/GW")
    print(f"\n  Per-season breakdown:")
    for season, stats in result.season_results.items():
        print(f"    {season}: {stats['total_points']} total pts "
              f"({stats['avg_points_per_gw']:.1f}/GW, "
              f"best={stats['best_gw']}, worst={stats['worst_gw']})")
    print(f"{'='*50}")


def run_loop(args):
    """Run the outer optimization loop."""
    from outer_loop.optimizer import OuterLoopOptimizer, OuterLoopConfig

    config = load_config()
    outer_cfg = config.get("outer_loop", {})
    bt_cfg = config.get("backtest", {})

    # CLI args override config file
    loop_config = OuterLoopConfig(
        max_iterations=args.iterations or outer_cfg.get("max_iterations", 50),
        model=args.model or outer_cfg.get("model", "claude-sonnet-5"),
        region=args.region or outer_cfg.get("region", "us-east-1"),
        profile=args.profile or outer_cfg.get("profile"),
        backtest_seasons=args.seasons or bt_cfg.get("seasons", ["2022-23", "2023-24"]),
        public_gw_ratio=bt_cfg.get("public_gw_ratio", 0.6),
        random_seed=bt_cfg.get("random_seed", 42),
        output_dir=config.get("output", {}).get("run_dir", "outer_loop/runs"),
        improvement_threshold=outer_cfg.get("improvement_threshold", 0.1),
        agentcore_arn=args.agentcore_arn or outer_cfg.get("agentcore_arn"),
    )

    optimizer = OuterLoopOptimizer(loop_config)
    optimizer.run()


def main():
    parser = argparse.ArgumentParser(
        description="FPL-RSO: Recursive Self-Optimizing FPL Agent (powered by Amazon Bedrock)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available Bedrock models (short names):
  claude-sonnet     Claude Sonnet 4 (default, best balance)
  claude-haiku      Claude 3.5 Haiku (fast, cheap)
  claude-opus       Claude Opus 4 (most capable)
  nova-pro          Amazon Nova Pro
  nova-lite         Amazon Nova Lite (cheapest)
  llama4-maverick   Meta Llama 4 Maverick
  llama4-scout      Meta Llama 4 Scout

  Or pass a full Bedrock model ID directly.

Examples:
  python scripts/run.py --fetch-data                          # Download data first
  python scripts/run.py --eval-only                           # Evaluate current agent
  python scripts/run.py                                       # Run outer loop (50 iters)
  python scripts/run.py --iterations 100 --model claude-opus  # Use Opus
  python scripts/run.py --model nova-pro --region us-west-2   # Use Nova in us-west-2
  python scripts/run.py --profile my-aws-profile              # Use named AWS profile
  python scripts/run.py --agentcore-arn arn:aws:bedrock:...    # Use AgentCore Runtime
        """,
    )

    parser.add_argument(
        "--fetch-data", action="store_true",
        help="Download FPL historical data and exit"
    )
    parser.add_argument(
        "--eval-only", action="store_true",
        help="Evaluate current agent without running outer loop"
    )
    parser.add_argument(
        "--iterations", type=int, default=None,
        help="Number of outer loop iterations (default: 50)"
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Bedrock model: claude-sonnet, claude-opus, nova-pro, etc. (default: claude-sonnet)"
    )
    parser.add_argument(
        "--region", type=str, default=None,
        help="AWS region for Bedrock (default: us-east-1)"
    )
    parser.add_argument(
        "--profile", type=str, default=None,
        help="AWS profile name (default: use default credentials)"
    )
    parser.add_argument(
        "--seasons", nargs="+", default=None,
        help="Seasons to backtest on (e.g., 2022-23 2023-24)"
    )
    parser.add_argument(
        "--agentcore-arn", type=str, default=None,
        help="AgentCore Runtime ARN to run proposer remotely (default: run locally)"
    )

    args = parser.parse_args()

    if args.fetch_data:
        fetch_data(args.seasons)
    elif args.eval_only:
        eval_only(args.seasons)
    else:
        run_loop(args)


if __name__ == "__main__":
    main()
