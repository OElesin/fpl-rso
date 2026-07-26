"""
Scoring engine for the backtest.

Computes actual FPL points for a gameweek decision given real outcomes.
Handles captain doubling, chip effects, auto-subs, and hit penalties.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass


@dataclass
class GameweekScore:
    """Detailed breakdown of points for one gameweek."""

    gameweek: int
    gross_points: int  # points before hits
    hit_penalty: int  # deducted for extra transfers
    net_points: int  # gross - penalty
    captain_points: int  # extra from captain doubling
    chip_bonus: int  # extra from bench boost / triple captain
    lineup_points: int  # sum of starting XI points
    bench_points: int  # points left on bench (for analysis)
    auto_sub_points: int  # points gained from auto-subs


def score_gameweek(
    lineup: list[int],
    bench: list[int],
    captain_id: int,
    vice_captain_id: int,
    actual_points: pd.DataFrame,
    hits: int = 0,
    chip: str | None = None,
) -> GameweekScore:
    """
    Score a gameweek decision against actual outcomes.

    Args:
        lineup: 11 player_ids in starting XI
        bench: 4 player_ids ordered for auto-sub
        captain_id: captain's player_id
        vice_captain_id: vice-captain's player_id
        actual_points: DataFrame with columns [player_id, points, minutes]
        hits: number of -4 hits taken
        chip: chip played this GW (None, 'bench_boost', 'triple_captain', 'freehit')
    """
    gameweek = int(actual_points["gameweek"].iloc[0]) if "gameweek" in actual_points.columns else 0

    def get_points(pid: int) -> int:
        row = actual_points[actual_points["player_id"] == pid]
        return int(row["points"].iloc[0]) if not row.empty else 0

    def get_minutes(pid: int) -> int:
        row = actual_points[actual_points["player_id"] == pid]
        if row.empty:
            return 0
        return int(row["minutes"].iloc[0]) if "minutes" in row.columns else 90

    # Calculate lineup points with auto-subs
    lineup_points = 0
    auto_sub_points = 0
    bench_idx = 0

    for pid in lineup:
        mins = get_minutes(pid)
        if mins > 0:
            lineup_points += get_points(pid)
        else:
            # Auto-sub: find first eligible bench player who played
            subbed = False
            for b_idx in range(bench_idx, len(bench)):
                bench_pid = bench[b_idx]
                if get_minutes(bench_pid) > 0:
                    sub_pts = get_points(bench_pid)
                    lineup_points += sub_pts
                    auto_sub_points += sub_pts
                    bench_idx = b_idx + 1
                    subbed = True
                    break
            # If no sub found, 0 points for this slot

    # Captain bonus
    captain_played = get_minutes(captain_id) > 0
    if captain_played:
        captain_pts = get_points(captain_id)
        captain_points = captain_pts  # doubled (already counted once in lineup)
    else:
        # Vice captain takes over
        if get_minutes(vice_captain_id) > 0:
            captain_pts = get_points(vice_captain_id)
            captain_points = captain_pts
            captain_id = vice_captain_id
        else:
            captain_points = 0

    # Apply captain doubling (add extra copy of captain's points)
    gross_points = lineup_points + captain_points

    # Chip effects
    chip_bonus = 0
    if chip == "bench_boost":
        # All bench players score
        for pid in bench:
            if get_minutes(pid) > 0:
                chip_bonus += get_points(pid)
        gross_points += chip_bonus
    elif chip == "triple_captain":
        # Captain gets 3x instead of 2x (add one more copy)
        chip_bonus = captain_points
        gross_points += chip_bonus

    # Hit penalty
    hit_penalty = hits * 4
    net_points = gross_points - hit_penalty

    # Bench points (for analysis — points left on bench)
    bench_points = sum(get_points(pid) for pid in bench)

    return GameweekScore(
        gameweek=gameweek,
        gross_points=gross_points,
        hit_penalty=hit_penalty,
        net_points=net_points,
        captain_points=captain_points,
        chip_bonus=chip_bonus,
        lineup_points=lineup_points,
        bench_points=bench_points,
        auto_sub_points=auto_sub_points,
    )


def compute_season_score(gw_scores: list[GameweekScore]) -> dict:
    """
    Aggregate gameweek scores into season-level metrics.
    """
    total_net = sum(s.net_points for s in gw_scores)
    total_gross = sum(s.gross_points for s in gw_scores)
    total_hits = sum(s.hit_penalty for s in gw_scores)
    total_captain = sum(s.captain_points for s in gw_scores)
    total_bench_left = sum(s.bench_points for s in gw_scores)
    total_auto_sub = sum(s.auto_sub_points for s in gw_scores)

    return {
        "total_points": total_net,
        "gross_points": total_gross,
        "hit_penalty": total_hits,
        "captain_points": total_captain,
        "bench_points_left": total_bench_left,
        "auto_sub_points": total_auto_sub,
        "gameweeks_played": len(gw_scores),
        "avg_points_per_gw": total_net / max(len(gw_scores), 1),
        "best_gw": max(s.net_points for s in gw_scores) if gw_scores else 0,
        "worst_gw": min(s.net_points for s in gw_scores) if gw_scores else 0,
    }
