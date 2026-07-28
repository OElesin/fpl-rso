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
        state = {
            "run_id": run_id,
            "iteration": 0,
            "max_iterations": int(os.environ.get("MAX_ITERATIONS", "50")),
            "best_strategy": None,
            "best_score": 0.0,
            "baseline_score": 0.0,
            "failed_attempts": [],
            "history": [],
            "status": "running",
            "model": os.environ.get("MODEL", "claude-sonnet-5"),
            "seasons": json.loads(os.environ.get("SEASONS", '["2023-24"]')),
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
        "model": state["model"],
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

    if state["baseline_score"] == 0:
        state["baseline_score"] = result.get("baseline_score", new_score)

    if improvement > 0 and result.get("improvements_found", 0) > 0:
        state["best_strategy"] = result["best_strategy"]
        state["best_score"] = new_score
        state["history"].append({
            "iteration": current_iter,
            "private_score": new_score,
            "kept": True,
            "improvement": improvement,
        })
        print(f"[Orchestrator] IMPROVEMENT found! Score: {new_score:.2f} (+{improvement:.2f})")
    else:
        if new_score > 0 and state["best_score"] == 0:
            # First iteration — set baseline
            state["best_score"] = new_score
            state["best_strategy"] = result.get("best_strategy")

        state["history"].append({
            "iteration": current_iter,
            "private_score": new_score,
            "kept": False,
            "reason": "no_improvement",
        })
        print(f"[Orchestrator] No improvement. Score: {new_score:.2f}")

    # Save state
    put_state(state)

    return {
        "status": "iteration_complete",
        "iteration": current_iter,
        "best_score": state["best_score"],
        "improvements_so_far": sum(1 for h in state["history"] if h.get("kept")),
    }
