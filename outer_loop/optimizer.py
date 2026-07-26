"""
Outer Loop Optimizer — the meta-agent that rewrites the inner agent.

This is the RSI engine. It:
1. Evaluates the current inner agent via backtest
2. Invokes a Strands Agent (with tools) to reason about and propose a rewrite
3. Evaluates the candidate against held-out gameweeks
4. Keeps the candidate ONLY if it improves the private score
5. Repeats

Mirrors the AIDE² design:
- Selection is based on private (held-out) score only
- Public score is feedback to the proposer but not the selection signal
- Failed attempts are tracked to avoid repeating mistakes
- Each iteration produces agent_k

Powered by Strands Agents SDK — the proposer is a multi-step reasoning
agent that reads code, analyzes results, self-validates, and submits.
"""

import json
import shutil
import time
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict

import boto3

from data.loader import load_seasons, SeasonData
from eval.backtest import evaluate_agent, BacktestConfig, BacktestResult, load_strategy_from_file
from outer_loop.proposer import generate_candidate, validate_candidate


@dataclass
class LoopState:
    """Persistent state of the outer optimization loop."""

    iteration: int = 0
    best_private_score: float = 0.0
    best_iteration: int = 0
    improvements: list[int] = None  # iterations where improvement was found
    failed_attempts: list[str] = None
    history: list[dict] = None

    def __post_init__(self):
        if self.improvements is None:
            self.improvements = []
        if self.failed_attempts is None:
            self.failed_attempts = []
        if self.history is None:
            self.history = []


@dataclass
class OuterLoopConfig:
    """Configuration for the outer optimization loop."""

    max_iterations: int = 50
    model: str = "claude-sonnet-5"  # Bedrock model short name or full model ID
    region: str = "us-east-1"  # AWS region for Bedrock
    profile: str | None = None  # AWS profile (uses default creds if None)
    backtest_seasons: list[str] = None
    public_gw_ratio: float = 0.6
    random_seed: int = 42
    output_dir: str = "outer_loop/runs"
    improvement_threshold: float = 0.1  # min private score improvement to keep
    # AgentCore remote execution
    agentcore_arn: str | None = None  # If set, invoke proposer via AgentCore Runtime
    agentcore_session_prefix: str = "fpl-rso"  # Session ID prefix for AgentCore

    def __post_init__(self):
        if self.backtest_seasons is None:
            self.backtest_seasons = ["2022-23", "2023-24"]


class OuterLoopOptimizer:
    """
    The recursive self-improvement engine.

    Runs the outer loop: propose → evaluate → select cycle.
    """

    def __init__(self, config: OuterLoopConfig):
        self.config = config
        self.state = LoopState()

        # Paths
        self.project_root = Path(__file__).parent.parent
        self.strategy_path = self.project_root / "inner_agent" / "strategy.py"
        self.player_path = self.project_root / "inner_agent" / "player.py"
        self.run_dir = self.project_root / config.output_dir / self._run_id()
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Load data once
        print("Loading season data...")
        self.seasons_data = load_seasons(config.backtest_seasons)
        if not self.seasons_data:
            raise RuntimeError(
                f"No data found for seasons {config.backtest_seasons}. "
                "Run `python -m data.fetcher` first."
            )

    def _run_id(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def run(self):
        """Execute the full outer loop."""
        mode = "AgentCore Remote" if self.config.agentcore_arn else "Strands Agent Local"
        print(f"\n{'='*60}")
        print(f"  FPL-RSO Outer Loop ({mode})")
        print(f"  Max iterations: {self.config.max_iterations}")
        print(f"  Model: {self.config.model} ({self.config.region})")
        if self.config.agentcore_arn:
            print(f"  AgentCore ARN: {self.config.agentcore_arn}")
        print(f"  Seasons: {self.config.backtest_seasons}")
        print(f"  Output: {self.run_dir}")
        print(f"{'='*60}\n")

        # Step 0: Evaluate baseline
        print("[Step 0] Evaluating baseline agent...")
        baseline_result = self._evaluate_current_strategy()
        self.state.best_private_score = baseline_result.private_score
        self.state.history.append(self._make_history_entry(0, baseline_result, kept=True))

        print(f"  Baseline: public={baseline_result.public_score:.2f}, "
              f"private={baseline_result.private_score:.2f}")

        # Save baseline
        self._save_agent(0, self.strategy_path.read_text())

        # Main loop
        for i in range(1, self.config.max_iterations + 1):
            self.state.iteration = i
            print(f"\n[Step {i}/{self.config.max_iterations}] ", end="")

            # Propose
            current_code = self.strategy_path.read_text()
            eval_dict = {
                "public_score": baseline_result.public_score,
                "private_score": baseline_result.private_score,
                "total_score": baseline_result.total_score,
                "season_results": baseline_result.season_results,
            }

            print("Proposing rewrite (Strands Agent)...", end=" ")
            candidate_code = self._propose_candidate(current_code, eval_dict, i)

            if candidate_code is None:
                print("FAILED (agent could not produce a candidate)")
                self.state.failed_attempts.append(f"Strands agent failed to produce candidate")
                continue

            # Validate
            valid, reason = validate_candidate(candidate_code)
            if not valid:
                print(f"INVALID ({reason})")
                self.state.failed_attempts.append(f"Invalid code: {reason}")
                continue

            # Save candidate to temp file and evaluate
            candidate_path = self.run_dir / f"candidate_{i}.py"
            candidate_path.write_text(candidate_code)

            print("Evaluating...", end=" ")
            try:
                candidate_result = self._evaluate_candidate(str(candidate_path))
            except Exception as e:
                print(f"ERROR ({e})")
                self.state.failed_attempts.append(f"Eval crashed: {str(e)[:80]}")
                continue

            # Selection: keep only if private score improves
            improvement = candidate_result.private_score - self.state.best_private_score

            if improvement >= self.config.improvement_threshold:
                print(
                    f"KEPT (+{improvement:.2f}) "
                    f"private: {self.state.best_private_score:.2f} → {candidate_result.private_score:.2f}"
                )

                # Install new strategy
                self.strategy_path.write_text(candidate_code)
                self.state.best_private_score = candidate_result.private_score
                self.state.best_iteration = i
                self.state.improvements.append(i)
                baseline_result = candidate_result

                # Save accepted agent
                self._save_agent(i, candidate_code)
                self.state.history.append(
                    self._make_history_entry(i, candidate_result, kept=True)
                )
            else:
                print(
                    f"REJECTED (Δ={improvement:+.2f}) "
                    f"private: {candidate_result.private_score:.2f} vs best {self.state.best_private_score:.2f}"
                )
                # Brief description for failed attempts log
                self.state.failed_attempts.append(
                    f"private={candidate_result.private_score:.2f} (Δ={improvement:+.2f})"
                )
                self.state.history.append(
                    self._make_history_entry(i, candidate_result, kept=False)
                )

            # Save state periodically
            if i % 5 == 0:
                self._save_state()

        # Final summary
        self._save_state()
        self._print_summary()

    def _evaluate_current_strategy(self) -> BacktestResult:
        """Evaluate the current strategy.py."""
        bt_config = BacktestConfig(
            seasons=self.config.backtest_seasons,
            public_gw_ratio=self.config.public_gw_ratio,
            random_seed=self.config.random_seed,
        )
        return evaluate_agent(bt_config, self.seasons_data)

    def _propose_candidate(self, current_code: str, eval_dict: dict, iteration: int) -> str | None:
        """
        Generate a candidate rewrite — either locally or via AgentCore Runtime.
        """
        if self.config.agentcore_arn:
            return self._propose_via_agentcore(current_code, eval_dict, iteration)
        else:
            return generate_candidate(
                current_code=current_code,
                eval_result=eval_dict,
                iteration=iteration,
                max_iterations=self.config.max_iterations,
                failed_attempts=self.state.failed_attempts,
                model=self.config.model,
                region=self.config.region,
                profile=self.config.profile,
            )

    def _propose_via_agentcore(self, current_code: str, eval_dict: dict, iteration: int) -> str | None:
        """
        Invoke the proposer agent deployed on Bedrock AgentCore Runtime.
        """
        session_kwargs = {}
        if self.config.profile:
            session_kwargs["profile_name"] = self.config.profile

        session = boto3.Session(**session_kwargs)
        client = session.client("bedrock-agentcore", region_name=self.config.region)

        payload = json.dumps({
            "current_code": current_code,
            "eval_result": eval_dict,
            "failed_attempts": self.state.failed_attempts[-15:],
            "iteration": iteration,
            "max_iterations": self.config.max_iterations,
            "model": self.config.model,
        })

        session_id = f"{self.config.agentcore_session_prefix}-{iteration}"

        try:
            response = client.invoke_agent_runtime(
                agentRuntimeArn=self.config.agentcore_arn,
                runtimeSessionId=session_id,
                payload=payload.encode(),
            )

            result = json.loads(response["payload"].read())

            if result.get("status") == "success":
                return result.get("candidate_code")
            else:
                print(f"[AgentCore] {result.get('message', 'Unknown error')}")
                return None

        except Exception as e:
            print(f"[AgentCore] Invocation failed: {e}")
            return None

    def _evaluate_candidate(self, filepath: str) -> BacktestResult:
        """Evaluate a candidate strategy file."""
        strategy_module = load_strategy_from_file(filepath)
        bt_config = BacktestConfig(
            seasons=self.config.backtest_seasons,
            public_gw_ratio=self.config.public_gw_ratio,
            random_seed=self.config.random_seed,
        )
        return evaluate_agent(bt_config, self.seasons_data, strategy_module)

    def _save_agent(self, iteration: int, code: str):
        """Save an accepted agent version."""
        agent_dir = self.run_dir / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / f"agent_{iteration}.py").write_text(code)

    def _save_state(self):
        """Persist loop state to disk."""
        state_path = self.run_dir / "state.json"
        state_dict = {
            "iteration": self.state.iteration,
            "best_private_score": self.state.best_private_score,
            "best_iteration": self.state.best_iteration,
            "improvements": self.state.improvements,
            "num_failed": len(self.state.failed_attempts),
            "history": self.state.history,
        }
        state_path.write_text(json.dumps(state_dict, indent=2, default=str))

    def _make_history_entry(self, iteration: int, result: BacktestResult, kept: bool) -> dict:
        return {
            "iteration": iteration,
            "public_score": result.public_score,
            "private_score": result.private_score,
            "total_score": result.total_score,
            "kept": kept,
            "timestamp": datetime.now().isoformat(),
        }

    def _print_summary(self):
        """Print final summary of the optimization run."""
        print(f"\n{'='*60}")
        print(f"  Outer Loop Complete")
        print(f"{'='*60}")
        print(f"  Total iterations: {self.state.iteration}")
        print(f"  Improvements found: {len(self.state.improvements)}")
        print(f"  Best iteration: {self.state.best_iteration}")
        print(f"  Best private score: {self.state.best_private_score:.2f} avg pts/GW")
        print(f"  Acceptance rate: {len(self.state.improvements)}/{self.state.iteration} "
              f"({100*len(self.state.improvements)/max(self.state.iteration,1):.0f}%)")
        print(f"  Results saved to: {self.run_dir}")
        print(f"{'='*60}")
