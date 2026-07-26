"""
Player evaluation utilities for the inner agent.

Provides scoring functions that the strategy uses to rank players
for transfers, captaincy, and lineup decisions.
"""

import pandas as pd
import numpy as np


def expected_points(
    player: pd.Series,
    form: float,
    fixture_difficulty: int = 3,
    is_home: bool = False,
) -> float:
    """
    Estimate expected points for a player in a given gameweek.

    This is the core evaluation function that the outer loop can optimize.
    The baseline uses a simple weighted combination of form, price, and fixture.

    Args:
        player: Player row with position, price, etc.
        form: Rolling average points (e.g., last 5 GWs)
        fixture_difficulty: 1 (easiest) to 5 (hardest)
        is_home: Whether player is at home
    """
    if pd.isna(form) or form == 0:
        return 0.0

    # Base expected points from form
    xP = form

    # Fixture adjustment: easier fixtures boost expected points
    fixture_multiplier = 1.0 + (3 - fixture_difficulty) * 0.08
    xP *= fixture_multiplier

    # Home advantage
    if is_home:
        xP *= 1.05

    # Position-specific adjustments
    position = player.get("position", "MID")
    if position == "GKP":
        xP *= 0.95  # GKs are more consistent but lower ceiling
    elif position == "DEF":
        xP *= 0.98
    elif position == "FWD":
        xP *= 1.02  # Forwards are slightly more explosive

    return max(xP, 0.0)


def value_score(player: pd.Series, form: float) -> float:
    """
    Points-per-million metric for transfer evaluation.
    Helps identify underpriced players.
    """
    price = player.get("price", 5.0)
    if price <= 0 or pd.isna(form) or form == 0:
        return 0.0
    return form / price


def ownership_differential(player: pd.Series) -> float:
    """
    Returns a differential score — lower ownership = higher differential upside.
    Useful for rank-chasing strategies.
    """
    ownership = player.get("ownership", 50.0)
    if pd.isna(ownership):
        return 0.5
    # Normalize to 0-1 where 1 = maximum differential (0% owned)
    return 1.0 - (ownership / 100.0)


def captain_score(
    player: pd.Series,
    form: float,
    fixture_difficulty: int = 3,
    is_home: bool = False,
) -> float:
    """
    Score a player for captaincy consideration.
    Captaincy favors high-ceiling players with good fixtures.
    """
    xP = expected_points(player, form, fixture_difficulty, is_home)

    # Captaincy bonus for proven high scorers (ceiling matters more)
    # Use form variance proxy: higher form = more likely to have big hauls
    ceiling_bonus = max(0, form - 5.0) * 0.1

    return xP + ceiling_bonus
