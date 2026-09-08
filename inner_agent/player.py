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


# ---------------------------------------------------------------------------
# Effective-ownership / rank-aware scoring
#
# FPL rank is a RELATIVE game: your rank moves based on how you score versus
# what the field owns. A haul from a highly-owned "template" player barely
# moves rank; the same haul from a low-owned differential moves it a lot, while
# a template player blanking hurts less (everyone else blanks too).
#
# These weights are DELIBERATELY exposed so the outer RSI loop can discover the
# right balance by rewriting the calls in strategy.py — we do NOT hardcode a
# strong opinion here. Defaults are near-neutral so behavior is unchanged until
# the loop tunes them.
# ---------------------------------------------------------------------------

# Default weights (near-neutral). strategy.py may override per-decision.
DEFAULT_DIFFERENTIAL_WEIGHT = 0.0   # >0 rewards low ownership (rank chasing)
DEFAULT_TEMPLATE_SAFETY_WEIGHT = 0.0  # >0 rewards high ownership (protect rank)


def rank_adjusted_score(
    base_score: float,
    player: pd.Series,
    differential_weight: float = DEFAULT_DIFFERENTIAL_WEIGHT,
    template_safety_weight: float = DEFAULT_TEMPLATE_SAFETY_WEIGHT,
) -> float:
    """
    Adjust a base expected-points/captaincy score by ownership to reflect the
    relative (rank) game rather than the absolute-points game.

    - differential_weight (>0): boosts LOW-owned players (upside for climbing rank).
    - template_safety_weight (>0): boosts HIGH-owned players (defends rank vs field).

    The two pull in opposite directions; the RSI loop can favor either stance
    (aggressive differential vs safe template) or blend them. With both weights
    at 0 (default), this returns base_score unchanged.

    base_score is scaled multiplicatively so the adjustment is proportional and
    does not swamp the underlying expected points.
    """
    if base_score <= 0:
        return base_score

    ownership = player.get("ownership", 50.0)
    if pd.isna(ownership):
        ownership = 50.0
    own_frac = max(0.0, min(1.0, ownership / 100.0))

    differential = 1.0 - own_frac          # high when rarely owned
    template = own_frac                    # high when widely owned

    multiplier = 1.0 + differential_weight * differential + template_safety_weight * template
    return base_score * multiplier


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
