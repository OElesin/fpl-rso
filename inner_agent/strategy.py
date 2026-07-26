"""
FPL Decision Strategy — The Inner Agent

THIS IS THE FILE THE OUTER LOOP REWRITES.

It contains the complete decision logic for one gameweek:
- select_transfers(): which players to buy/sell
- select_captain(): who to captain
- select_lineup(): starting XI and bench order
- select_chip(): whether to play a chip

The outer loop will propose modifications to these functions to improve
the agent's total points across backtested seasons.

Constraints enforced by the eval harness (not here):
- Squad: 15 players (2 GKP, 5 DEF, 5 MID, 3 FWD)
- Budget: 100.0m at start
- Max 3 players from any single team
- Starting XI: 1 GKP, at least 3 DEF, at least 1 FWD (formation rules)
- 1 free transfer per week (unused rolls over, max 2 banked)
- Each additional transfer costs 4 points
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field

from inner_agent.player import expected_points, value_score, captain_score


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Squad:
    """Current squad state."""

    players: list[int]  # player_ids (15 players)
    budget: float  # remaining budget in millions
    free_transfers: int  # 1 or 2
    captain_id: int | None = None
    vice_captain_id: int | None = None
    lineup: list[int] = field(default_factory=list)  # 11 player_ids
    bench: list[int] = field(default_factory=list)  # 4 player_ids (ordered)
    chips_available: list[str] = field(
        default_factory=lambda: ["wildcard", "bench_boost", "triple_captain", "freehit"]
    )
    chip_played: str | None = None


@dataclass
class GameweekDecision:
    """All decisions for one gameweek."""

    transfers_in: list[int]  # player_ids to buy
    transfers_out: list[int]  # player_ids to sell
    captain_id: int
    vice_captain_id: int
    lineup: list[int]  # 11 player_ids
    bench: list[int]  # 4 player_ids (ordered for auto-sub)
    chip: str | None = None  # chip to activate, or None
    hits: int = 0  # number of extra transfers (cost = hits * 4)


# ---------------------------------------------------------------------------
# Strategy parameters (tunable by outer loop)
# ---------------------------------------------------------------------------

FORM_WINDOW = 5  # gameweeks to average for form
MIN_FORM_THRESHOLD = 2.0  # don't buy players with form below this
TRANSFER_GAIN_THRESHOLD = 1.5  # min expected point gain to justify a transfer
HIT_THRESHOLD = 3.0  # min expected gain to take a -4 hit
MAX_HITS_PER_WEEK = 2  # never take more than this many hits
CAPTAIN_FORM_WEIGHT = 0.7  # weight of form in captain selection
CAPTAIN_FIXTURE_WEIGHT = 0.3  # weight of fixture in captain selection


# ---------------------------------------------------------------------------
# Core decision functions
# ---------------------------------------------------------------------------


def select_transfers(
    squad: Squad,
    player_pool: pd.DataFrame,
    form_data: pd.DataFrame,
    gameweek: int,
) -> tuple[list[int], list[int], int]:
    """
    Decide which transfers to make.

    Returns:
        (transfers_in, transfers_out, hits_taken)
    """
    if form_data.empty or player_pool.empty:
        return [], [], 0

    # Score current squad players
    squad_scores = _score_players(squad.players, form_data, player_pool)

    # Find worst player in squad
    if not squad_scores:
        return [], [], 0

    worst_players = sorted(squad_scores.items(), key=lambda x: x[1])

    transfers_in = []
    transfers_out = []
    hits = 0
    budget = squad.budget
    available_ft = squad.free_transfers

    for worst_id, worst_score in worst_players:
        if len(transfers_in) >= available_ft + MAX_HITS_PER_WEEK:
            break

        worst_player = player_pool[player_pool["player_id"] == worst_id]
        if worst_player.empty:
            continue
        worst_player = worst_player.iloc[0]
        worst_position = worst_player.get("position", "MID")
        worst_price = worst_player.get("price", 5.0)

        # Find best available replacement in same position
        candidates = form_data[
            (form_data["position"] == worst_position)
            & (~form_data["player_id"].isin(squad.players))
            & (form_data["form"] >= MIN_FORM_THRESHOLD)
        ].copy()

        if candidates.empty:
            continue

        # Score candidates
        candidates["xP"] = candidates.apply(
            lambda row: expected_points(row, row["form"]), axis=1
        )
        candidates = candidates[candidates["price"] <= budget + worst_price]

        if candidates.empty:
            continue

        best_candidate = candidates.sort_values("xP", ascending=False).iloc[0]
        gain = best_candidate["xP"] - worst_score

        # Decide whether the transfer is worth it
        threshold = TRANSFER_GAIN_THRESHOLD if len(transfers_in) < available_ft else HIT_THRESHOLD
        if gain >= threshold:
            transfers_in.append(int(best_candidate["player_id"]))
            transfers_out.append(int(worst_id))
            budget = budget + worst_price - best_candidate["price"]

            if len(transfers_in) > available_ft:
                hits += 1
        else:
            break  # Remaining players aren't bad enough to transfer

    return transfers_in, transfers_out, hits


def select_captain(
    squad: Squad,
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
) -> tuple[int, int]:
    """
    Select captain and vice-captain from squad.

    Returns:
        (captain_id, vice_captain_id)
    """
    scores = {}
    for pid in squad.players:
        player_form = form_data[form_data["player_id"] == pid]
        player_info = player_pool[player_pool["player_id"] == pid]

        if player_form.empty or player_info.empty:
            scores[pid] = 0.0
            continue

        form = player_form.iloc[0]["form"]
        player = player_info.iloc[0]
        fixture_diff = player.get("fixture_difficulty", 3)
        is_home = bool(player.get("is_home", False))

        scores[pid] = captain_score(player, form, fixture_diff, is_home)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    captain_id = ranked[0][0] if ranked else squad.players[0]
    vice_captain_id = ranked[1][0] if len(ranked) > 1 else squad.players[1]

    return captain_id, vice_captain_id


def select_lineup(
    squad: Squad,
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
) -> tuple[list[int], list[int]]:
    """
    Select starting XI and bench order from 15-player squad.

    Formation constraints:
    - Exactly 1 GKP
    - At least 3 DEF
    - At least 2 MID
    - At least 1 FWD
    - Total of 11 starters

    Returns:
        (lineup_11, bench_4_ordered)
    """
    # Score all squad players
    player_scores = []
    for pid in squad.players:
        pf = form_data[form_data["player_id"] == pid]
        pi = player_pool[player_pool["player_id"] == pid]

        form = pf.iloc[0]["form"] if not pf.empty else 0.0
        position = pi.iloc[0].get("position", "MID") if not pi.empty else "MID"
        player_row = pi.iloc[0] if not pi.empty else pd.Series()

        xP = expected_points(player_row, form) if not player_row.empty else form
        player_scores.append((pid, position, xP))

    # Sort by position group, then by score
    gkps = sorted([(p, s) for p, pos, s in player_scores if pos == "GKP"], key=lambda x: -x[1])
    defs = sorted([(p, s) for p, pos, s in player_scores if pos == "DEF"], key=lambda x: -x[1])
    mids = sorted([(p, s) for p, pos, s in player_scores if pos == "MID"], key=lambda x: -x[1])
    fwds = sorted([(p, s) for p, pos, s in player_scores if pos == "FWD"], key=lambda x: -x[1])

    # Fill minimum requirements
    lineup = []
    lineup.append(gkps[0][0] if gkps else squad.players[0])  # 1 GKP

    # At least 3 DEF
    for p, s in defs[:3]:
        lineup.append(p)

    # At least 2 MID
    for p, s in mids[:2]:
        lineup.append(p)

    # At least 1 FWD
    for p, s in fwds[:1]:
        lineup.append(p)

    # Fill remaining 4 spots with best available from remaining
    remaining = []
    remaining.extend(defs[3:])
    remaining.extend(mids[2:])
    remaining.extend(fwds[1:])
    remaining.sort(key=lambda x: -x[1])

    for p, s in remaining:
        if len(lineup) >= 11:
            break
        lineup.append(p)

    # Bench: everyone not in lineup, ordered by score
    bench_candidates = [(p, s) for p, pos, s in player_scores if p not in lineup]
    bench_candidates.sort(key=lambda x: -x[1])

    # Bench GKP goes last
    bench = []
    bench_gkp = None
    for p, s in bench_candidates:
        pos = next((pos for pid, pos, _ in player_scores if pid == p), "MID")
        if pos == "GKP":
            bench_gkp = p
        else:
            bench.append(p)

    if bench_gkp:
        bench.append(bench_gkp)

    return lineup[:11], bench[:4]


def select_chip(
    squad: Squad,
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
    gameweek: int,
    season_length: int = 38,
) -> str | None:
    """
    Decide whether to play a chip this gameweek.

    Baseline strategy: very conservative, saves chips for double gameweeks
    and end-of-season pushes. The outer loop should discover better timing.

    Returns:
        Chip name or None.
    """
    if not squad.chips_available:
        return None

    # Baseline: don't play chips (conservative default)
    # The outer loop is expected to discover chip timing logic
    # For now, just play bench boost in the last 5 GWs if available
    if gameweek >= season_length - 5 and "bench_boost" in squad.chips_available:
        # Only if bench is strong
        bench_form = []
        for pid in squad.bench:
            pf = form_data[form_data["player_id"] == pid]
            if not pf.empty:
                bench_form.append(pf.iloc[0]["form"])

        if bench_form and np.mean(bench_form) > 4.0:
            return "bench_boost"

    # Triple captain if best player has form > 8 in easy fixture in last 10 GWs
    if gameweek >= season_length - 10 and "triple_captain" in squad.chips_available:
        if form_data.empty:
            return None
        best_form = form_data[form_data["player_id"].isin(squad.players)]["form"].max()
        if best_form > 8.0:
            return "triple_captain"

    return None


# ---------------------------------------------------------------------------
# Main decision function (called by backtest engine)
# ---------------------------------------------------------------------------


def make_gameweek_decision(
    squad: Squad,
    player_pool: pd.DataFrame,
    form_data: pd.DataFrame,
    gameweek: int,
    season_length: int = 38,
) -> GameweekDecision:
    """
    Make all decisions for one gameweek. This is the single entry point
    called by the evaluation harness.
    """
    # 1. Transfers
    transfers_in, transfers_out, hits = select_transfers(
        squad, player_pool, form_data, gameweek
    )

    # Apply transfers to squad for lineup/captain selection
    current_players = list(squad.players)
    for out_id in transfers_out:
        if out_id in current_players:
            current_players.remove(out_id)
    for in_id in transfers_in:
        current_players.append(in_id)

    updated_squad = Squad(
        players=current_players,
        budget=squad.budget,
        free_transfers=squad.free_transfers,
        chips_available=squad.chips_available,
    )

    # 2. Captain
    captain_id, vice_captain_id = select_captain(updated_squad, form_data, player_pool)

    # 3. Lineup
    lineup, bench = select_lineup(updated_squad, form_data, player_pool)

    # 4. Chip
    updated_squad.bench = bench
    chip = select_chip(updated_squad, form_data, player_pool, gameweek, season_length)

    return GameweekDecision(
        transfers_in=transfers_in,
        transfers_out=transfers_out,
        captain_id=captain_id,
        vice_captain_id=vice_captain_id,
        lineup=lineup,
        bench=bench,
        chip=chip,
        hits=hits,
    )


def _score_players(
    player_ids: list[int],
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
) -> dict[int, float]:
    """Score a list of players by expected points."""
    scores = {}
    for pid in player_ids:
        pf = form_data[form_data["player_id"] == pid]
        pi = player_pool[player_pool["player_id"] == pid]

        if pf.empty or pi.empty:
            scores[pid] = 0.0
            continue

        form = pf.iloc[0]["form"]
        player = pi.iloc[0]
        scores[pid] = expected_points(player, form)

    return scores
