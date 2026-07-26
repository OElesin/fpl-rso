"""
FPL-RSO AgentCore Deployment — Proposer Agent as a Service.

Deploys the Strands proposer agent to Amazon Bedrock AgentCore Runtime.
The agent receives strategy code + eval results, reasons about improvements,
and returns a validated candidate rewrite.

Deploy with:
    agentcore deploy

Or test locally:
    python deploy/app.py
    curl -X POST http://localhost:8080/invocations \
        -H "Content-Type: application/json" \
        -d '{"current_code": "...", "eval_result": {...}}'
"""

import sys
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from outer_loop.proposer import (
    read_current_strategy,
    read_eval_results,
    read_failed_attempts,
    read_iteration_info,
    validate_code,
    write_candidate,
    SYSTEM_PROMPT,
    BEDROCK_MODELS,
    _context,
    _resolve_model_id,
)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """
    Run one RSI proposer iteration.

    Expected payload:
    {
        "current_code": str,        # Current strategy.py content
        "eval_result": {            # Backtest results
            "public_score": float,
            "private_score": float,
            "total_score": float,
            "season_results": dict
        },
        "failed_attempts": [str],   # Previous failed attempt descriptions
        "iteration": int,           # Current iteration number
        "max_iterations": int,      # Total planned iterations
        "model": str                # Optional: model short name or ID
    }

    Returns:
    {
        "candidate_code": str | None,  # Proposed strategy code, or None
        "status": "success" | "error",
        "message": str
    }
    """
    # Populate shared context for tools
    _context["current_code"] = payload.get("current_code", "")
    _context["eval_result"] = payload.get("eval_result", {})
    _context["failed_attempts"] = payload.get("failed_attempts", [])
    _context["iteration"] = payload.get("iteration", 1)
    _context["max_iterations"] = payload.get("max_iterations", 50)
    _context["candidate_code"] = None

    # Resolve model
    model_name = payload.get("model", "claude-sonnet")
    model_id = _resolve_model_id(model_name)

    try:
        bedrock_model = BedrockModel(
            model_id=model_id,
            temperature=0.7,
            max_tokens=16000,
        )

        agent = Agent(
            model=bedrock_model,
            tools=[
                read_current_strategy,
                read_eval_results,
                read_failed_attempts,
                read_iteration_info,
                validate_code,
                write_candidate,
            ],
            system_prompt=SYSTEM_PROMPT,
        )

        agent(
            "Improve the FPL strategy. Follow your workflow: read the code, "
            "analyze results, check failed attempts, draft an improvement, "
            "validate it, and submit it via write_candidate."
        )

        candidate = _context["candidate_code"]

        if candidate:
            return {
                "candidate_code": candidate,
                "status": "success",
                "message": f"Candidate generated at iteration {_context['iteration']}",
            }
        else:
            return {
                "candidate_code": None,
                "status": "error",
                "message": "Agent completed but did not submit a candidate",
            }

    except Exception as e:
        return {
            "candidate_code": None,
            "status": "error",
            "message": f"Agent failed: {str(e)}",
        }


if __name__ == "__main__":
    app.run()
