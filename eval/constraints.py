"""
FPL rule enforcement and squad validation.

Ensures all decisions comply with official FPL rules:
- Squad composition (2 GKP, 5 DEF, 5 MID, 3 FWD)
- Budget limit (100.0m starting)
- Max 3 players from any one team
- Valid formations (1 GKP, 3+ DEF, 2+ MID, 1+ FWD in starting XI)
- Transfer rules (1 free per week, max 2 banked, -4 per extra)
"""

import pandas as pd
from dataclasses import dataclass


# Squad composition requirements
SQUAD_SIZE = 15
POSITION_LIMITS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_TEAM = 3
STARTING_BUDGET = 100.0

# Formation: minimum starters per position
MIN_STARTERS = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
STARTING_XI = 11
BENCH_SIZE = 4

# Transfer rules
BASE_FREE_TRANSFERS = 1
MAX_BANKED_TRANSFERS = 2
HIT_COST = 4  # points deducted per extra transfer


@dataclass
class ValidationResult:
    """Result of validating a squad or decision."""

    valid: bool
    errors: list[str]

    def __bool__(self):
        return self.valid


def validate_squad(player_ids: list[int], player_pool: pd.DataFrame) -> ValidationResult:
    """
    Validate that a 15-player squad meets all FPL constraints.
    """
    errors = []

    # Check squad size
    if len(player_ids) != SQUAD_SIZE:
        errors.append(f"Squad has {len(player_ids)} players, need {SQUAD_SIZE}")

    # Check for duplicates
    if len(set(player_ids)) != len(player_ids):
        errors.append("Squad contains duplicate players")

    # Get player info
    squad_info = player_pool[player_pool["player_id"].isin(player_ids)]

    if len(squad_info) < len(player_ids):
        missing = set(player_ids) - set(squad_info["player_id"])
        errors.append(f"Players not found in pool: {missing}")

    # Check position limits
    if "position" in squad_info.columns:
        pos_counts = squad_info["position"].value_counts().to_dict()
        for pos, required in POSITION_LIMITS.items():
            actual = pos_counts.get(pos, 0)
            if actual != required:
                errors.append(f"Need {required} {pos}, have {actual}")

    # Check max per team
    if "team" in squad_info.columns:
        team_counts = squad_info["team"].value_counts()
        over_limit = team_counts[team_counts > MAX_PER_TEAM]
        for team, count in over_limit.items():
            errors.append(f"Too many from {team}: {count} (max {MAX_PER_TEAM})")

    # Check budget
    if "price" in squad_info.columns:
        total_cost = squad_info["price"].sum()
        if total_cost > STARTING_BUDGET:
            errors.append(f"Squad costs {total_cost:.1f}m, budget is {STARTING_BUDGET:.1f}m")

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def validate_lineup(
    lineup: list[int], bench: list[int], player_pool: pd.DataFrame
) -> ValidationResult:
    """Validate a starting XI + bench selection."""
    errors = []

    if len(lineup) != STARTING_XI:
        errors.append(f"Lineup has {len(lineup)} players, need {STARTING_XI}")

    if len(bench) != BENCH_SIZE:
        errors.append(f"Bench has {len(bench)} players, need {BENCH_SIZE}")

    # Check formation
    lineup_info = player_pool[player_pool["player_id"].isin(lineup)]
    if "position" in lineup_info.columns:
        pos_counts = lineup_info["position"].value_counts().to_dict()
        for pos, min_count in MIN_STARTERS.items():
            actual = pos_counts.get(pos, 0)
            if actual < min_count:
                errors.append(f"Need at least {min_count} {pos} in lineup, have {actual}")

        # Exactly 1 GKP
        if pos_counts.get("GKP", 0) != 1:
            errors.append(f"Need exactly 1 GKP in lineup, have {pos_counts.get('GKP', 0)}")

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def validate_transfers(
    transfers_in: list[int],
    transfers_out: list[int],
    current_squad: list[int],
    player_pool: pd.DataFrame,
    budget: float,
    free_transfers: int,
) -> ValidationResult:
    """Validate a set of transfers."""
    errors = []

    if len(transfers_in) != len(transfers_out):
        errors.append(
            f"Transfers in ({len(transfers_in)}) != transfers out ({len(transfers_out)})"
        )

    # Check players being sold are in squad
    for pid in transfers_out:
        if pid not in current_squad:
            errors.append(f"Cannot sell player {pid} — not in squad")

    # Check players being bought are not already in squad
    for pid in transfers_in:
        if pid in current_squad:
            errors.append(f"Cannot buy player {pid} — already in squad")

    # Check budget after transfers
    out_info = player_pool[player_pool["player_id"].isin(transfers_out)]
    in_info = player_pool[player_pool["player_id"].isin(transfers_in)]

    money_out = out_info["price"].sum() if "price" in out_info.columns else 0
    money_in = in_info["price"].sum() if "price" in in_info.columns else 0

    new_budget = budget + money_out - money_in
    if new_budget < 0:
        errors.append(f"Cannot afford transfers: budget would be {new_budget:.1f}m")

    # Validate resulting squad
    new_squad = [p for p in current_squad if p not in transfers_out] + transfers_in
    squad_validation = validate_squad(new_squad, player_pool)
    errors.extend(squad_validation.errors)

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def compute_hit_penalty(num_transfers: int, free_transfers: int) -> int:
    """Compute the points penalty for extra transfers."""
    extra = max(0, num_transfers - free_transfers)
    return extra * HIT_COST


def update_free_transfers(transfers_made: int, current_free: int) -> int:
    """Calculate free transfers for next gameweek."""
    if transfers_made == 0:
        # Unused transfer rolls over (max 2)
        return min(current_free + 1, MAX_BANKED_TRANSFERS)
    else:
        # Used some or all, reset to base
        return BASE_FREE_TRANSFERS
