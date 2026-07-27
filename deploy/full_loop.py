"""
FPL-RSO Full Loop on AgentCore — runs the ENTIRE RSI loop remotely.

This deploys the complete optimization loop (propose → evaluate → select → repeat)
as a single AgentCore Runtime service. No local execution needed.

The loop:
1. Fetches FPL data from GitHub (inside the container)
2. Evaluates baseline strategy
3. Runs N iterations of: propose rewrite → backtest → keep/reject
4. Returns the best discovered strategy + full history

Deploy with:
    agentcore deploy

Invoke with:
    agentcore invoke --payload '{"iterations": 50, "seasons": ["2023-24"]}'

Or via boto3:
    client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId="run-001",
        payload=json.dumps({"iterations": 50}).encode()
    )
"""

import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from data.fetcher import fetch_all
from data.loader import load_seasons
from eval.backtest import evaluate_agent, BacktestConfig, BacktestResult, load_strategy_from_file
from outer_loop.proposer import generate_candidate, validate_candidate

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """
    Run the full RSI loop on AgentCore.

    Expected payload:
    {
        "iterations": int,            # Number of outer loop steps (default: 50)
        "seasons": [str],             # Seasons to backtest on (default: ["2022-23", "2023-24"])
        "model": str,                 # Bedrock model (default: "claude-sonnet-5")
        "region": str,                # AWS region (default: "us-east-1")
        "improvement_threshold": float, # Min gain to keep (default: 0.1)
        "public_gw_ratio": float,     # Public/private split (default: 0.6)
        "random_seed": int,           # Deterministic split (default: 42)
        "strategy_code": str | None   # Starting strategy (null = use default baseline)
    }

    Returns:
    {
        "status": "success" | "error",
        "best_strategy": str,         # Best discovered strategy code
        "baseline_score": float,      # Starting private score
        "best_score": float,          # Best private score achieved
        "improvement": float,         # best - baseline
        "iterations_run": int,
        "improvements_found": int,
        "acceptance_rate": float,
        "history": [dict],            # Per-iteration results
        "message": str
    }
    """
    # Parse config from payload
    iterations = payload.get("iterations", 50)
    seasons = payload.get("seasons", ["2022-23", "2023-24"])
    model = payload.get("model", "claude-sonnet-5")
    region = payload.get("region", "us-east-1")
    threshold = payload.get("improvement_threshold", 0.1)
    public_gw_ratio = payload.get("public_gw_ratio", 0.6)
    random_seed = payload.get("random_seed", 42)
    starting_code = payload.get("strategy_code", None)

    try:
        # Step 1: Fetch data (downloads from GitHub into container)
        print(f"[FPL-RSO] Fetching data for seasons: {seasons}")
        fetch_all(seasons)

        # Step 2: Load season data
        print(f"[FPL-RSO] Loading season data...")
        seasons_data = load_seasons(seasons)
        if not seasons_data:
            return {
                "status": "error",
                "message": f"No data loaded for seasons {seasons}",
            }

        # Step 3: Get baseline strategy code
        if starting_code:
            current_code = starting_code
        else:
            strategy_path = PROJECT_ROOT / "inner_agent" / "strategy.py"
            current_code = strategy_path.read_text()

        # Step 4: Evaluate baseline
        print(f"[FPL-RSO] Evaluating baseline...")
        bt_config = BacktestConfig(
            seasons=seasons,
            public_gw_ratio=public_gw_ratio,
            random_seed=random_seed,
        )
        baseline_result = evaluate_agent(bt_config, seasons_data)
        baseline_score = baseline_result.private_score

        print(f"[FPL-RSO] Baseline: public={baseline_result.public_score:.2f}, "
              f"private={baseline_score:.2f}")

        # Step 5: Run the loop
        best_code = current_code
        best_score = baseline_score
        best_iteration = 0
        improvements = []
        failed_attempts = []
        history = [{
            "iteration": 0,
            "public_score": baseline_result.public_score,
            "private_score": baseline_score,
            "kept": True,
            "reason": "baseline",
        }]

        for i in range(1, iterations + 1):
            print(f"[FPL-RSO] Step {i}/{iterations}...")

            # Propose
            eval_dict = {
                "public_score": baseline_result.public_score,
                "private_score": baseline_result.private_score,
                "total_score": baseline_result.total_score,
                "season_results": baseline_result.season_results,
            }

            candidate_code = generate_candidate(
                current_code=best_code,
                eval_result=eval_dict,
                iteration=i,
                max_iterations=iterations,
                failed_attempts=failed_attempts,
                model=model,
                region=region,
            )

            if candidate_code is None:
                failed_attempts.append(f"Step {i}: agent failed to produce candidate")
                history.append({
                    "iteration": i,
                    "kept": False,
                    "reason": "agent_failed",
                })
                continue

            # Validate
            valid, reason = validate_candidate(candidate_code)
            if not valid:
                failed_attempts.append(f"Step {i}: invalid code - {reason}")
                history.append({
                    "iteration": i,
                    "kept": False,
                    "reason": f"invalid: {reason}",
                })
                continue

            # Evaluate candidate
            candidate_path = Path(f"/tmp/candidate_{i}.py")
            candidate_path.write_text(candidate_code)

            try:
                strategy_module = load_strategy_from_file(str(candidate_path))
                candidate_result = evaluate_agent(bt_config, seasons_data, strategy_module)
            except Exception as e:
                failed_attempts.append(f"Step {i}: eval crashed - {str(e)[:80]}")
                history.append({
                    "iteration": i,
                    "kept": False,
                    "reason": f"eval_crash: {str(e)[:80]}",
                })
                continue

            # Selection
            improvement = candidate_result.private_score - best_score

            if improvement >= threshold:
                print(f"[FPL-RSO] Step {i}: KEPT (+{improvement:.2f})")
                best_code = candidate_code
                best_score = candidate_result.private_score
                best_iteration = i
                improvements.append(i)
                baseline_result = candidate_result

                history.append({
                    "iteration": i,
                    "public_score": candidate_result.public_score,
                    "private_score": candidate_result.private_score,
                    "kept": True,
                    "improvement": improvement,
                })
            else:
                failed_attempts.append(
                    f"Step {i}: private={candidate_result.private_score:.2f} (Δ={improvement:+.2f})"
                )
                history.append({
                    "iteration": i,
                    "public_score": candidate_result.public_score,
                    "private_score": candidate_result.private_score,
                    "kept": False,
                    "reason": f"insufficient (Δ={improvement:+.2f})",
                })

        # Final result
        total_improvement = best_score - baseline_score
        acceptance_rate = len(improvements) / iterations if iterations > 0 else 0

        print(f"[FPL-RSO] Complete. {len(improvements)} improvements found. "
              f"Best: {best_score:.2f} (+{total_improvement:.2f})")

        return {
            "status": "success",
            "best_strategy": best_code,
            "baseline_score": baseline_score,
            "best_score": best_score,
            "improvement": total_improvement,
            "best_iteration": best_iteration,
            "iterations_run": iterations,
            "improvements_found": len(improvements),
            "acceptance_rate": acceptance_rate,
            "history": history,
            "message": (
                f"Completed {iterations} iterations. "
                f"Found {len(improvements)} improvements. "
                f"Private score: {baseline_score:.2f} → {best_score:.2f} "
                f"(+{total_improvement:.2f} pts/GW)"
            ),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"Loop failed: {str(e)}",
        }


if __name__ == "__main__":
    app.run()
