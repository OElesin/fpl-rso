"""
Backtest engine — simulates a full FPL season.

Runs the inner agent's strategy through a season of historical data,
enforcing constraints and scoring each gameweek against real outcomes.

Key design (from AIDE²):
- Public/private gameweek split: the inner agent's optimization signal
  comes from public GWs, but selection uses private GWs.
- The outer loop only keeps agent rewrites that improve private score.
"""

import importlib
import importlib.util
import sys
import random
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass

from data.loader import SeasonData, compute_rolling_form, get_gameweek_players
from eval.scorer import score_gameweek, compute_season_score, GameweekScore
from eval.constraints import (
    validate_squad,
    validate_lineup,
    compute_hit_penalty,
    update_free_transfers,
    STARTING_BUDGET,
    POSITION_LIMITS,
    MAX_PER_TEAM,
)


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    seasons: list[str]
    public_gw_ratio: float = 0.6  # fraction of GWs used as public score
    random_seed: int = 42
    start_gameweek: int = 5  # skip first N GWs (need form history)
    form_window: int = 5


@dataclass
class BacktestResult:
    """Results from backtesting one agent version."""

    public_score: float  # avg points/GW on public gameweeks
    private_score: float  # avg points/GW on private gameweeks (selection signal)
    total_score: float  # overall avg points/GW across all GWs
    season_results: dict[str, dict]  # per-season breakdown
    public_gameweeks: dict[str, list[int]]  # which GWs were public
    private_gameweeks: dict[str, list[int]]  # which GWs were private


def split_gameweeks(
    gameweeks: list[int], public_ratio: float, seed: int
) -> tuple[list[int], list[int]]:
    """
    Split gameweeks into public (visible to inner agent) and private (held-out).
    The split is deterministic given the seed, ensuring fair comparison between agents.
    """
    rng = random.Random(seed)
    shuffled = list(gameweeks)
    rng.shuffle(shuffled)

    split_idx = int(len(shuffled) * public_ratio)
    public = sorted(shuffled[:split_idx])
    private = sorted(shuffled[split_idx:])

    return public, private


def initialize_squad(season_data: SeasonData, start_gw: int) -> tuple[list[int], float]:
    """
    Create a starting squad using the first few GWs of data.
    Picks the best available players within budget constraints.
    """
    # Get form data heading into start_gw
    form = compute_rolling_form(season_data, start_gw)
    if form.empty:
        return [], STARTING_BUDGET

    # Get player pool at start_gw
    pool = get_gameweek_players(season_data, start_gw)
    if pool.empty:
        return [], STARTING_BUDGET

    # Merge form into pool
    if "form" not in pool.columns:
        pool = pool.merge(form[["player_id", "form"]], on="player_id", how="left")
        pool["form"] = pool["form"].fillna(0)

    # Greedy squad selection by position
    squad = []
    budget = STARTING_BUDGET
    team_counts: dict = {}

    for position, count in POSITION_LIMITS.items():
        candidates = pool[
            (pool["position"] == position) & (~pool["player_id"].isin(squad))
        ].copy()

        if candidates.empty:
            continue

        candidates = candidates.sort_values("form", ascending=False)

        selected = 0
        for _, player in candidates.iterrows():
            if selected >= count:
                break

            price = player.get("price", 5.0)
            team = player.get("team", "Unknown")

            # Budget check
            if price > budget:
                continue

            # Team limit check
            if team_counts.get(team, 0) >= MAX_PER_TEAM:
                continue

            squad.append(int(player["player_id"]))
            budget -= price
            team_counts[team] = team_counts.get(team, 0) + 1
            selected += 1

    return squad, budget


def run_backtest(
    season_data: SeasonData,
    config: BacktestConfig,
    strategy_module=None,
) -> tuple[list[GameweekScore], list[int], list[int]]:
    """
    Run a full season backtest using the inner agent's strategy.

    Args:
        season_data: Loaded season data
        config: Backtest configuration
        strategy_module: The strategy module to use (default: inner_agent.strategy)

    Returns:
        (gameweek_scores, public_gws, private_gws)
    """
    if strategy_module is None:
        from inner_agent import strategy as strategy_module

    # Initialize squad
    squad_ids, budget = initialize_squad(season_data, config.start_gameweek)
    if not squad_ids:
        return [], [], []

    # Import strategy types
    Squad = strategy_module.Squad

    # Split gameweeks
    all_gws = [gw for gw in season_data.gameweeks if gw >= config.start_gameweek]
    public_gws, private_gws = split_gameweeks(all_gws, config.public_gw_ratio, config.random_seed)

    # Run through season
    free_transfers = 1
    chips_available = ["wildcard", "bench_boost", "triple_captain", "freehit"]
    gw_scores = []

    for gw in all_gws:
        # Get data available to agent at decision time
        form_data = compute_rolling_form(season_data, gw, window=config.form_window)
        player_pool = get_gameweek_players(season_data, gw)

        if form_data.empty or player_pool.empty:
            continue

        # Build squad state
        squad = Squad(
            players=list(squad_ids),
            budget=budget,
            free_transfers=free_transfers,
            chips_available=list(chips_available),
        )

        # Agent makes decisions
        try:
            decision = strategy_module.make_gameweek_decision(
                squad=squad,
                player_pool=player_pool,
                form_data=form_data,
                gameweek=gw,
                season_length=season_data.num_gameweeks,
            )
        except Exception as e:
            # Agent error — score 0 for this GW
            gw_scores.append(
                GameweekScore(
                    gameweek=gw,
                    gross_points=0,
                    hit_penalty=0,
                    net_points=0,
                    captain_points=0,
                    chip_bonus=0,
                    lineup_points=0,
                    bench_points=0,
                    auto_sub_points=0,
                )
            )
            continue

        # Apply transfers
        for out_id in decision.transfers_out:
            if out_id in squad_ids:
                # Recover sell price
                out_player = player_pool[player_pool["player_id"] == out_id]
                if not out_player.empty:
                    budget += out_player.iloc[0].get("price", 0)
                squad_ids.remove(out_id)

        for in_id in decision.transfers_in:
            in_player = player_pool[player_pool["player_id"] == in_id]
            if not in_player.empty:
                budget -= in_player.iloc[0].get("price", 0)
            squad_ids.append(in_id)

        # Apply chip
        if decision.chip and decision.chip in chips_available:
            chips_available.remove(decision.chip)

        # Score against actual results
        actual = player_pool[["player_id", "points", "minutes", "gameweek"]].copy()

        gw_score = score_gameweek(
            lineup=decision.lineup,
            bench=decision.bench,
            captain_id=decision.captain_id,
            vice_captain_id=decision.vice_captain_id,
            actual_points=actual,
            hits=decision.hits,
            chip=decision.chip,
        )
        gw_score.gameweek = gw
        gw_scores.append(gw_score)

        # Update state for next GW
        num_transfers = len(decision.transfers_in)
        free_transfers = update_free_transfers(num_transfers, free_transfers)

    return gw_scores, public_gws, private_gws


def evaluate_agent(
    config: BacktestConfig,
    seasons_data: dict[str, SeasonData],
    strategy_module=None,
) -> BacktestResult:
    """
    Full evaluation of an agent across multiple seasons.
    Returns public and private scores (the private score is the selection signal).
    """
    all_public_scores = []
    all_private_scores = []
    all_scores = []
    season_results = {}
    public_gws_map = {}
    private_gws_map = {}

    for season, season_data in seasons_data.items():
        gw_scores, public_gws, private_gws = run_backtest(
            season_data, config, strategy_module
        )

        if not gw_scores:
            continue

        # Split scores into public/private
        public_scores = [s for s in gw_scores if s.gameweek in public_gws]
        private_scores = [s for s in gw_scores if s.gameweek in private_gws]

        all_public_scores.extend(public_scores)
        all_private_scores.extend(private_scores)
        all_scores.extend(gw_scores)

        season_results[season] = compute_season_score(gw_scores)
        public_gws_map[season] = public_gws
        private_gws_map[season] = private_gws

    # Compute aggregate scores
    public_avg = (
        np.mean([s.net_points for s in all_public_scores]) if all_public_scores else 0.0
    )
    private_avg = (
        np.mean([s.net_points for s in all_private_scores]) if all_private_scores else 0.0
    )
    total_avg = np.mean([s.net_points for s in all_scores]) if all_scores else 0.0

    return BacktestResult(
        public_score=float(public_avg),
        private_score=float(private_avg),
        total_score=float(total_avg),
        season_results=season_results,
        public_gameweeks=public_gws_map,
        private_gameweeks=private_gws_map,
    )


def load_strategy_from_file(filepath: str):
    """
    Dynamically load a strategy module from a file path.
    Used by the outer loop to evaluate candidate rewrites.
    """
    spec = importlib.util.spec_from_file_location("candidate_strategy", filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
