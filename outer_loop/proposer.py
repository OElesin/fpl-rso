"""
Proposer — Strands Agent that generates candidate rewrites of the inner agent's strategy.

Unlike a single-shot LLM call, this uses a Strands Agent with tools to:
1. Read and analyze the current strategy code
2. Inspect backtest results and identify weaknesses
3. Review failed attempts to avoid repeating mistakes
4. Draft a rewrite targeting specific improvements
5. Self-validate the proposed code before returning

This multi-step reasoning approach improves acceptance rates by catching
errors and bad ideas before they reach the evaluation harness.
"""

import json
from pathlib import Path

from strands import Agent, tool
from strands.models import BedrockModel


# ---------------------------------------------------------------------------
# Shared state — populated before each agent invocation
# ---------------------------------------------------------------------------

_context = {
    "current_code": "",
    "eval_result": {},
    "failed_attempts": [],
    "iteration": 0,
    "max_iterations": 0,
    "candidate_code": None,  # set by the agent via write_candidate tool
}


# ---------------------------------------------------------------------------
# Tools available to the proposer agent
# ---------------------------------------------------------------------------


@tool
def read_current_strategy() -> str:
    """Read the current inner agent strategy code that needs to be improved.

    Returns the full Python source of the current strategy.py file.
    """
    code = _context["current_code"]
    if not code:
        return "ERROR: No strategy code loaded."
    return f"```python\n{code}\n```"


@tool
def read_eval_results() -> str:
    """Read the backtest evaluation results for the current strategy.

    Returns performance metrics including public score, private score,
    and per-season breakdowns. The PRIVATE score is what matters for selection.
    """
    result = _context["eval_result"]
    if not result:
        return "ERROR: No evaluation results available."

    output = [
        f"Public score (visible GWs): {result.get('public_score', 0):.2f} avg pts/GW",
        f"Private score (held-out GWs): {result.get('private_score', 0):.2f} avg pts/GW",
        f"Total: {result.get('total_score', 0):.2f} avg pts/GW",
        "",
        "Per-season breakdown:",
        json.dumps(result.get("season_results", {}), indent=2, default=str),
    ]
    return "\n".join(output)


@tool
def read_failed_attempts() -> str:
    """Read the history of previously failed improvement attempts.

    Use this to understand what has already been tried and rejected,
    so you can avoid repeating the same approaches.
    """
    attempts = _context["failed_attempts"]
    if not attempts:
        return "No failed attempts yet. This is the first iteration."

    # Show last 15 attempts
    recent = attempts[-15:]
    lines = [f"  Step {i+1}: {desc}" for i, desc in enumerate(recent)]
    header = f"Last {len(recent)} failed attempts (out of {len(attempts)} total):"
    return header + "\n" + "\n".join(lines)


@tool
def read_iteration_info() -> str:
    """Get current iteration number and total planned iterations.

    Useful for deciding how aggressive vs conservative to be with changes.
    Early iterations should try bigger changes, later iterations should fine-tune.
    """
    i = _context["iteration"]
    total = _context["max_iterations"]
    pct = (i / total * 100) if total > 0 else 0
    return (
        f"Iteration: {i} of {total} ({pct:.0f}% through the optimization run)\n"
        f"Strategy: {'Try bold changes' if pct < 30 else 'Fine-tune what works' if pct > 70 else 'Balanced exploration'}"
    )


@tool
def validate_code(code: str) -> str:
    """Validate that proposed Python code is syntactically correct and has required functions.

    Args:
        code: The complete Python code to validate.
    """
    errors = []

    # Syntax check
    try:
        compile(code, "<candidate>", "exec")
    except SyntaxError as e:
        errors.append(f"Syntax error on line {e.lineno}: {e.msg}")

    # Required function
    if "def make_gameweek_decision(" not in code:
        errors.append("Missing required function: make_gameweek_decision()")

    # Required classes/types
    if "GameweekDecision" not in code:
        errors.append("Missing GameweekDecision (must import or define it)")

    if "Squad" not in code:
        errors.append("Missing Squad (must import or define it)")

    # Check imports
    if "from inner_agent.player import" not in code and "import inner_agent" not in code:
        errors.append("Missing import from inner_agent.player (expected_points, etc.)")

    if errors:
        return "VALIDATION FAILED:\n" + "\n".join(f"  - {e}" for e in errors)
    else:
        return "VALIDATION PASSED: Code is syntactically valid and has all required components."


@tool
def write_candidate(code: str) -> str:
    """Submit the final candidate strategy code after validation passes.

    Call this ONLY after validate_code returns success.
    This is your final answer — the complete rewritten strategy.py.

    Args:
        code: The complete Python source code for the new strategy.py.
    """
    # Strip markdown fences if wrapped
    if code.startswith("```python"):
        code = code[len("```python"):].strip()
    if code.startswith("```"):
        code = code[3:].strip()
    if code.endswith("```"):
        code = code[:-3].strip()

    _context["candidate_code"] = code
    return "Candidate submitted successfully. The outer loop will now evaluate it."


# ---------------------------------------------------------------------------
# System prompt for the proposer agent
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the outer-loop optimizer in a recursive self-improvement system for Fantasy Premier League (FPL).

Your goal: rewrite the inner agent's strategy code to score MORE POINTS on held-out (private) gameweeks.

## Your Workflow

1. FIRST: Call read_current_strategy() to see the current code
2. THEN: Call read_eval_results() to understand current performance
3. THEN: Call read_failed_attempts() to see what's already been tried
4. THEN: Call read_iteration_info() to calibrate how aggressive to be
5. ANALYZE: Identify the biggest weakness in the current strategy
6. DRAFT: Write improved code targeting that specific weakness
7. VALIDATE: Call validate_code() with your proposed code
8. IF validation fails: Fix the issues and validate again
9. SUBMIT: Call write_candidate() with the validated code

## Strategy Improvement Ideas

- Captain selection: form-weighted, fixture-adjusted, home advantage, ceiling-based
- Transfer logic: form decay detection, value hunting, fixture targeting
- Lineup selection: matchup-based, minutes filter, consistency weighting
- Chip timing: bench boost when bench is strong, TC in easy fixtures for in-form premiums
- Search policy: which players to evaluate as candidates
- Risk management: when hits are worth it, how many to take

## Rules

- MUST preserve: make_gameweek_decision(squad, player_pool, form_data, gameweek, season_length) -> GameweekDecision
- MUST preserve: Squad and GameweekDecision dataclasses (import from inner_agent.strategy)
- MUST preserve: imports from inner_agent.player
- MAY change: any logic, weights, thresholds, algorithms, helper functions
- MUST NOT: add external dependencies beyond pandas, numpy
- MUST NOT: try to game the evaluation harness
- Focus on ONE or TWO specific improvements per iteration — don't rewrite everything"""


# ---------------------------------------------------------------------------
# Bedrock model configurations
# ---------------------------------------------------------------------------

BEDROCK_MODELS = {
    "claude-sonnet-5": "us.anthropic.claude-sonnet-5",
    "claude-sonnet": "us.anthropic.claude-sonnet-4-6",
    "claude-haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-opus": "us.anthropic.claude-opus-4-7",
    "nova-pro": "us.amazon.nova-pro-v1:0",
    "nova-lite": "us.amazon.nova-lite-v1:0",
    "nova-premier": "us.amazon.nova-premier-v1:0",
    "llama4-maverick": "us.meta.llama4-maverick-17b-instruct-v1:0",
    "llama4-scout": "us.meta.llama4-scout-17b-instruct-v1:0",
}

DEFAULT_MODEL = "claude-sonnet-5"


def _resolve_model_id(model: str) -> str:
    """Resolve a short model name to a full Bedrock model ID."""
    return BEDROCK_MODELS.get(model, model)


# ---------------------------------------------------------------------------
# Main proposer function
# ---------------------------------------------------------------------------


def generate_candidate(
    current_code: str,
    eval_result: dict,
    iteration: int,
    max_iterations: int,
    failed_attempts: list[str],
    model: str = "claude-sonnet-5",
    region: str = "us-east-1",
    profile: str | None = None,
) -> str | None:
    """
    Use a Strands Agent to propose a rewrite of the strategy code.

    The agent has tools to read context, validate code, and submit candidates.
    It reasons through the problem step-by-step rather than doing a single-shot generation.

    Args:
        current_code: Current strategy.py content
        eval_result: Dict with public_score, private_score, total_score, season_results
        iteration: Current outer loop step
        max_iterations: Total planned steps
        failed_attempts: List of brief descriptions of what was tried and failed
        model: Bedrock model short name or full model ID
        region: AWS region for Bedrock
        profile: AWS profile name (optional)

    Returns:
        New strategy code as string, or None if generation failed.
    """
    # Populate shared context for tools
    _context["current_code"] = current_code
    _context["eval_result"] = eval_result
    _context["failed_attempts"] = failed_attempts
    _context["iteration"] = iteration
    _context["max_iterations"] = max_iterations
    _context["candidate_code"] = None

    # Configure Bedrock model
    model_id = _resolve_model_id(model)

    model_kwargs = {
        "model_id": model_id,
        "region_name": region,
        "max_tokens": 16000,
        "additional_request_fields": {},
    }

    # Some models (e.g., claude-sonnet-5) don't support temperature
    if "sonnet-5" not in model_id and "opus-5" not in model_id:
        model_kwargs["temperature"] = 0.7

    # Increase timeout for large code generation responses
    import botocore.config
    model_kwargs["boto_client_config"] = botocore.config.Config(
        read_timeout=300,
        connect_timeout=30,
    )

    bedrock_model = BedrockModel(**model_kwargs)

    # Create the Strands agent with tools
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

    # Run the agent
    try:
        agent(
            "Improve the FPL strategy. Follow your workflow: read the code, "
            "analyze results, check failed attempts, draft an improvement, "
            "validate it, and submit it via write_candidate."
        )

        # Return whatever the agent submitted
        return _context["candidate_code"]

    except Exception as e:
        print(f"  [Proposer] Agent failed: {e}")
        return None


def validate_candidate(code: str) -> tuple[bool, str]:
    """
    External validation (called by optimizer after agent returns).
    Ensures the code is syntactically valid and has required entry points.
    """
    try:
        compile(code, "<candidate>", "exec")
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    if "def make_gameweek_decision(" not in code:
        return False, "Missing make_gameweek_decision function"

    if "GameweekDecision" not in code:
        return False, "Missing GameweekDecision reference"

    if "Squad" not in code:
        return False, "Missing Squad reference"

    return True, "OK"
