"""
FPL-RSO Step Function Orchestrator — EventBridge triggered, DynamoDB state.

Solves two problems:
1. State between iterations: stored in DynamoDB (best strategy, score, failed attempts)
2. Long-running loop: each iteration is a single AgentCore invocation (~3-5 min),
   triggered by EventBridge on a schedule (e.g., every 5 min) or Step Functions.

Architecture:
    EventBridge Rule (every 5 min)
        → Lambda (orchestrator)
            → Read state from DynamoDB
            → Invoke AgentCore Runtime (1 iteration)
            → Write updated state to DynamoDB
            → If max iterations reached, stop the rule

DynamoDB Table Schema:
    PK: run_id (str)
    Attributes:
        - iteration (int): current iteration count
        - max_iterations (int): when to stop
        - best_strategy (str): best strategy code so far
        - best_score (float): best private score
        - baseline_score (float): starting score
        - failed_attempts (list[str]): history of failures
        - history (list[dict]): full iteration history
        - status (str): running | completed | failed
        - model (str): Bedrock model to use
        - seasons (list[str]): seasons to backtest
        - created_at (str): ISO timestamp
        - updated_at (str): ISO timestamp
"""

import json
import os
from datetime import datetime
from decimal import Decimal

import boto3
from botocore.config import Config

# Config from environment variables
TABLE_NAME = os.environ.get("STATE_TABLE", "fpl-rso-state")
AGENTCORE_ARN = os.environ.get("AGENTCORE_ARN")
REGION = os.environ.get("AWS_REGION", "us-east-1")
RULE_NAME = os.environ.get("EVENTBRIDGE_RULE", "fpl-rso-iteration")


def decimal_default(obj):
    """JSON serializer for DynamoDB Decimal types."""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def convert_decimals(obj):
    """Recursively convert Decimal to float in nested structures."""
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_decimals(i) for i in obj]
    return obj


def get_state(run_id: str) -> dict | None:
    """Read current run state from DynamoDB."""
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(TABLE_NAME)

    response = table.get_item(Key={"run_id": run_id})
    item = response.get("Item")
    if item:
        return convert_decimals(item)
    return None


def put_state(state: dict):
    """Write run state to DynamoDB."""
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(TABLE_NAME)

    state["updated_at"] = datetime.utcnow().isoformat()
    # Convert to JSON string first (handles any non-serializable types), then parse with Decimal
    state_json = json.dumps(state, default=decimal_default)
    table.put_item(Item=json.loads(state_json, parse_float=Decimal))


def invoke_agentcore(payload: dict) -> dict:
    """Invoke the AgentCore Runtime with one iteration."""
    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(read_timeout=600, connect_timeout=30),
    )

    session_id = f"fpl-rso-iter-{payload.get('iteration', payload.get('iterations', 0)):04d}-{datetime.utcnow().strftime('%H%M%S')}-pad000000"

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )

    # Parse response
    if "response" in response:
        body = response["response"]
        raw = body.read() if hasattr(body, "read") else body
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw) if raw.strip() else {"status": "error", "message": "Empty response"}
    return {"status": "error", "message": "No response field"}


def disable_rule():
    """Disable the EventBridge rule when iterations are complete."""
    events = boto3.client("events", region_name=REGION)
    try:
        events.disable_rule(Name=RULE_NAME)
        print(f"Disabled EventBridge rule: {RULE_NAME}")
    except Exception as e:
        print(f"Could not disable rule: {e}")


# Model strategy: Sonnet for first 40 iters (cheap exploration),
# then Opus for remaining iters (plateau-breaking).
# Opus phase uses early-stopping: stops as soon as an improvement is found.
MODEL_SONNET = "us.anthropic.claude-sonnet-5"
MODEL_OPUS = "us.anthropic.claude-opus-5"
SONNET_ITERS = 40  # First 40 iterations use Sonnet (~$8)
# Remaining iterations (41-50) use Opus with early-stop (~$17 max, often less)


def _select_model(state: dict) -> str:
    """
    Hybrid strategy: Sonnet first (cheap), Opus when stuck (powerful).
    Opus phase has early-stopping — exits as soon as improvement is found.
    """
    current_iter = int(state.get("iteration", 1))

    if current_iter <= SONNET_ITERS:
        model = MODEL_SONNET
        print(f"[Model] Iter {current_iter}: {model} (exploration phase)")
    else:
        model = MODEL_OPUS
        print(f"[Model] Iter {current_iter}: {model} (plateau-breaking phase)")

    return model


def _should_early_stop(state: dict) -> bool:
    """
    Early-stop during Opus phase: if Opus found an improvement, stop the run.
    Saves cost — no need to keep running expensive Opus after it breaks through.
    """
    current_iter = int(state.get("iteration", 1))

    # Only apply early-stop during Opus phase
    if current_iter <= SONNET_ITERS:
        return False

    # Check if any improvement was found during Opus phase (iter > SONNET_ITERS)
    history = state.get("history", [])
    opus_improvements = [
        h for h in history
        if h.get("kept") and int(str(h.get("iteration", 0))) > SONNET_ITERS
    ]

    if opus_improvements:
        print(f"[Model] Early-stop: Opus found improvement, stopping run to save cost")
        return True

    return False


def _get_previous_best_strategy(current_run_id: str) -> str | None:
    """
    Find the best strategy CODE from any previously completed run.
    Only returns the code — the score threshold is set by evaluating
    that code on the current split (different seasons/splits produce
    different scores for the same strategy).
    """
    try:
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.Table(TABLE_NAME)

        scan = table.scan()
        items = scan.get("Items", [])

        # Find completed runs that aren't the current one
        completed = [
            i for i in items
            if i.get("status") == "completed"
            and i.get("run_id") != current_run_id
            and i.get("best_strategy")
        ]

        if not completed:
            return None

        # Pick the one with the highest best_score
        best_run = max(completed, key=lambda x: float(str(x.get("best_score", 0))))
        strategy = best_run.get("best_strategy", "")
        score = float(str(best_run.get("best_score", 0)))

        if strategy:
            print(f"[Orchestrator] Previous best: {best_run['run_id']} ({score:.2f} pts/GW)")
            return strategy

        return None

    except Exception as e:
        print(f"[Orchestrator] Error loading previous strategy: {e}")
        return None


def handler(event, context):
    """
    Lambda handler — triggered by EventBridge every N minutes.
    Runs one iteration of the RSI loop using AgentCore, saves state to DynamoDB.
    """
    # Get run_id from event or environment
    run_id = event.get("run_id", os.environ.get("RUN_ID", "default-run"))

    print(f"[Orchestrator] Run: {run_id}")

    # Load state
    state = get_state(run_id)
    if not state:
        print(f"[Orchestrator] No state found for {run_id}. Creating initial state.")

        # Load best strategy CODE from previous completed run (compound learning)
        previous_strategy = _get_previous_best_strategy(run_id)
        if previous_strategy:
            print(f"[Orchestrator] Seeding with previous best strategy ({len(previous_strategy)} chars)")
        else:
            print(f"[Orchestrator] No previous strategy found, starting from baseline")

        state = {
            "run_id": run_id,
            "iteration": 0,
            "max_iterations": int(os.environ.get("MAX_ITERATIONS", "50")),
            "best_strategy": previous_strategy,
            "best_score": 0.0,       # Set by first AgentCore evaluation on current split
            "baseline_score": 0.0,   # Set by first AgentCore evaluation on current split
            "failed_attempts": [],
            "history": [],
            "status": "running",
            "model": os.environ.get("MODEL", "claude-sonnet-5"),
            "seasons": json.loads(os.environ.get("SEASONS", '["2025-26"]')),
            "created_at": datetime.utcnow().isoformat(),
        }

    # Check if already done
    if state["status"] == "completed":
        print(f"[Orchestrator] Run {run_id} already completed. Disabling trigger.")
        disable_rule()
        return {"status": "already_completed", "best_score": state["best_score"]}

    if state["iteration"] >= state["max_iterations"]:
        state["status"] = "completed"
        put_state(state)
        disable_rule()
        print(f"[Orchestrator] Max iterations reached. Final score: {state['best_score']}")
        return {"status": "completed", "best_score": state["best_score"]}

    # Increment iteration
    state["iteration"] += 1
    current_iter = state["iteration"]
    print(f"[Orchestrator] Running iteration {current_iter}/{state['max_iterations']}")

    # Build payload for AgentCore
    payload = {
        "iterations": 1,  # Always 1 per invocation
        "seasons": state["seasons"],
        "model": _select_model(state),
        "region": REGION,
        "improvement_threshold": 0.1,
        "public_gw_ratio": 0.6,
        "random_seed": 42,
    }

    # Pass current best strategy if we have one
    if state["best_strategy"]:
        payload["strategy_code"] = state["best_strategy"]

    # Invoke AgentCore
    try:
        result = invoke_agentcore(payload)
    except Exception as e:
        state["failed_attempts"].append(f"Iter {current_iter}: AgentCore error - {str(e)[:100]}")
        state["history"].append({
            "iteration": current_iter,
            "kept": False,
            "reason": f"invocation_error: {str(e)[:100]}",
        })
        put_state(state)
        return {"status": "error", "iteration": current_iter, "error": str(e)}

    if result.get("status") != "success":
        state["failed_attempts"].append(f"Iter {current_iter}: {result.get('message', 'unknown')}")
        state["history"].append({
            "iteration": current_iter,
            "kept": False,
            "reason": result.get("message", "unknown"),
        })
        put_state(state)
        return {"status": "iteration_failed", "iteration": current_iter}

    # Check if improvement was found
    new_score = result.get("best_score", 0)
    improvement = result.get("improvement", 0)

    # On first iteration, set baseline from AgentCore's evaluation of the seed strategy
    if state["baseline_score"] == 0:
        state["baseline_score"] = result.get("baseline_score", new_score)
        state["best_score"] = result.get("baseline_score", new_score)
        print(f"[Orchestrator] Baseline set from evaluation: {state['baseline_score']:.2f} pts/GW")

    # Only accept if the new score BEATS our current best on THIS split
    if new_score > state["best_score"]:
        actual_improvement = new_score - state["best_score"]
        state["best_strategy"] = result["best_strategy"]
        state["best_score"] = new_score
        state["history"].append({
            "iteration": current_iter,
            "private_score": new_score,
            "kept": True,
            "improvement": actual_improvement,
            "model": payload["model"],
        })
        print(f"[Orchestrator] IMPROVEMENT! {new_score - actual_improvement:.2f} → {new_score:.2f} (+{actual_improvement:.2f}) [{payload['model']}]")
    else:
        state["history"].append({
            "iteration": current_iter,
            "private_score": new_score,
            "kept": False,
            "reason": f"no_improvement (got {new_score:.2f}, need >{state['best_score']:.2f})",
            "model": payload["model"],
        })
        print(f"[Orchestrator] Rejected. Score {new_score:.2f} vs best {state['best_score']:.2f} [{payload['model']}]")

    # Save state
    put_state(state)

    # Early-stop check: if Opus found an improvement, stop the run to save cost
    if _should_early_stop(state):
        state["status"] = "completed"
        put_state(state)
        disable_rule()
        print(f"[Orchestrator] Early-stop triggered. Best: {state['best_score']:.2f} pts/GW")
        return {"status": "early_stop", "best_score": state["best_score"]}

    return {
        "status": "iteration_complete",
        "iteration": current_iter,
        "best_score": state["best_score"],
        "improvements_so_far": sum(1 for h in state["history"] if h.get("kept")),
    }
