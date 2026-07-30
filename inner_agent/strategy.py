

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
MIN_FORM_THRESHOLD = 1.0  # don't buy players with form below this (widened from 2.0
                           # so genuinely high-xP replacements aren't excluded just
                           # because their trailing form average dipped)
TRANSFER_GAIN_THRESHOLD = 1.0  # min expected point gain to justify a free transfer
HIT_THRESHOLD = 6.0  # min expected gain to take a hit (raised from 5.0 to keep the
                      # expected-value bar higher now that we allow up to two hits
                      # per week -- each additional hit must independently clear a
                      # more comfortable margin above its -4 cost)
MAX_HITS_PER_WEEK = 2  # allow fixing up to two weak starters per week when each
                        # individually clears HIT_THRESHOLD (previously capped at 1,
                        # which could leave a second clearly-bad starter unaddressed
                        # for an extra week even when the data strongly supported
                        # upgrading them too)
MAX_PER_TEAM = 3  # FPL rule: max players from a single real-world team

# Captain scoring is an ensemble of the dedicated captain heuristic
# (captain_score, which factors in fixture/home advantage) and the general
# expected-points model. Blending the two reduces variance from either
# single model being miscalibrated for a specific player/fixture. The
# weight is tilted toward captain_score (0.65 vs 0.35, up from 0.6/0.4)
# because it is the metric specifically designed for captaincy decisions
# (fixture difficulty + home advantage baked in), while the generic xP
# model is optimized for overall point projection rather than
# captain-specific ceiling/ownership considerations. This keeps xP as a
# meaningful hedge without diluting the specialized signal as much.
CAPTAIN_SCORE_WEIGHT = 0.65
CAPTAIN_XP_WEIGHT = 0.35

# Chip trigger thresholds under "normal" (non-forced) circumstances.
# These are intentionally somewhat opportunistic: chips that are never
# played are pure wasted value, so the bar should be "good enough", not
# "perfect". Bench boost and triple captain bars have been relaxed
# slightly from their original values (4.0 -> 3.3, 6.5 -> 6.0) because a
# chip sitting completely unused through a whole season is a guaranteed
# zero, while a slightly-less-than-ideal week still captures real
# positive expected value. The relaxation is modest enough that it still
# requires a genuinely good (not just average) week to trigger.
BENCH_BOOST_FORM_THRESHOLD = 3.3  # avg bench xP required to consider bench boost
TRIPLE_CAPTAIN_FORM_THRESHOLD = 6.0  # best captain-ensemble score required for triple captain

# A chip that is never played by the end of the season is worth exactly
# zero. Rather than risk that outcome because the opportunistic threshold
# above was never met, we relax the bar substantially in the final few
# gameweeks of the season so that any remaining unused chip still gets
# some (positive expected value) use instead of none.
FORCE_CHIP_WINDOW = 4  # gameweeks before season end to start forcing chip usage
BENCH_BOOST_FORCE_THRESHOLD = 2.0
TRIPLE_CAPTAIN_FORCE_THRESHOLD = 3.0


# ---------------------------------------------------------------------------
# Helper: team-limit bookkeeping
# ---------------------------------------------------------------------------


def _get_team(pid: int, player_pool: pd.DataFrame):
    """Safely fetch a player's team id/name, or None if unavailable."""
    row = player_pool[player_pool["player_id"] == pid]
    if row.empty:
        return None
    return row.iloc[0].get("team")


def _team_counts(player_ids, player_pool: pd.DataFrame) -> dict:
    counts: dict = {}
    for pid in player_ids:
        team = _get_team(pid, player_pool)
        if team is None:
            continue
        counts[team] = counts.get(team, 0) + 1
    return counts


def _player_xp(pid: int, form_data: pd.DataFrame, player_pool: pd.DataFrame):
    """Return the model's expected-points estimate for a player, or None
    if we lack the data needed to compute it. Using the same expected_points
    model that drives lineup/transfer decisions (rather than raw trailing
    form) makes chip-trigger decisions consistent with the rest of the
    strategy's notion of player quality, since it already folds in
    fixture-aware adjustments that raw form does not."""
    pf = form_data[form_data["player_id"] == pid]
    pi = player_pool[player_pool["player_id"] == pid]
    if pf.empty or pi.empty:
        return None
    form = pf.iloc[0]["form"]
    player = pi.iloc[0]
    try:
        return expected_points(player, form)
    except Exception:
        return None


def _safe_value_score(row):
    """Best-effort wrapper around value_score. The exact call signature of
    value_score isn't guaranteed by this module (it's an ensemble-quality
    metric usually meant to express points-per-cost efficiency), so we try
    the (player, form) signature first -- consistent with how
    expected_points/captain_score are called elsewhere in this file -- and
    fall back to a single-argument call, and finally to 0.0 if neither
    works. This is only ever used as a tie-breaker, never as the primary
    transfer-decision signal, so a wrong/neutral fallback value is safe.
    """
    try:
        return float(value_score(row, row.get("form", 0.0)))
    except Exception:
        pass
    try:
        return float(value_score(row))
    except Exception:
        return 0.0


def _player_captain_ensemble(pid: int, form_data: pd.DataFrame, player_pool: pd.DataFrame):
    """Same ensemble blend used in select_captain, exposed as a helper so
    chip logic (triple captain) can reuse the identical notion of captain
    quality rather than a separate, potentially inconsistent metric."""
    pf = form_data[form_data["player_id"] == pid]
    pi = player_pool[player_pool["player_id"] == pid]
    if pf.empty or pi.empty:
        return None
    form = pf.iloc[0]["form"]
    player = pi.iloc[0]
    fixture_diff = player.get("fixture_difficulty", 3)
    is_home = bool(player.get("is_home", False))
    try:
        cs = captain_score(player, form, fixture_diff, is_home)
        xp = expected_points(player, form)
    except Exception:
        return None
    return CAPTAIN_SCORE_WEIGHT * cs + CAPTAIN_XP_WEIGHT * xp


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

    # Prioritize upgrading projected STARTERS over bench-warmers.
    # A bench player is only worth points via auto-subs or a bench-boost
    # chip, so spending a scarce transfer (or a hit) to fix the
    # weakest bench player ahead of a mediocre starter is usually a much
    # lower-value move than fixing the weakest starter. We compute the
    # squad's *current* projected lineup (before any transfers) purely to
    # classify players as starter/bench for prioritization purposes, then
    # sort so that all starters (worst-first) are considered before any
    # bench players (worst-first).
    try:
        projected_lineup, _ = select_lineup(squad, form_data, player_pool)
        starter_set = set(projected_lineup)
    except Exception:
        starter_set = set(squad.players)

    worst_players = sorted(
        squad_scores.items(),
        key=lambda x: (0 if x[0] in starter_set else 1, x[1]),
    )

    transfers_in: list[int] = []
    transfers_out: list[int] = []
    hits = 0
    budget = squad.budget
    available_ft = squad.free_transfers
    max_transfers = available_ft + MAX_HITS_PER_WEEK

    # Track the hypothetical squad composition as we make decisions this
    # week, so we can enforce the max-3-players-per-team rule when
    # evaluating replacement candidates.
    current_ids = list(squad.players)

    for worst_id, worst_score in worst_players:
        if len(transfers_in) >= max_transfers:
            break

        worst_player = player_pool[player_pool["player_id"] == worst_id]
        if worst_player.empty:
            continue
        worst_player = worst_player.iloc[0]
        worst_position = worst_player.get("position", "MID")
        worst_price = worst_player.get("price", 5.0)

        # Exclude players already in squad AND players already bought this week
        excluded_ids = set(squad.players) | set(transfers_in)

        # Find best available replacement in same position
        candidates = form_data[
            (form_data["position"] == worst_position)
            & (~form_data["player_id"].isin(excluded_ids))
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

        # Rank primarily by expected points, but use a value/efficiency
        # metric as a tie-breaker among near-equal xP candidates (bucketed
        # to 0.1 pt resolution) so that, when two replacements project
        # similarly, we prefer the one that is a better use of budget --
        # this preserves flexibility for future transfers without ever
        # overriding a genuinely stronger xP pick.
        candidates["_value_tiebreak"] = candidates.apply(_safe_value_score, axis=1)
        candidates["_xp_bucket"] = candidates["xP"].round(1)
        candidates = candidates.sort_values(
            ["_xp_bucket", "_value_tiebreak"], ascending=[False, False]
        )

        # Walk down the ranked candidates until we find one that doesn't
        # break the max-3-per-team constraint (enforcing this here avoids
        # proposing transfers that the harness would have to reject/undo).
        hypothetical_ids_without_worst = [p for p in current_ids if p != worst_id]
        existing_counts = _team_counts(hypothetical_ids_without_worst, player_pool)

        chosen = None
        for _, cand in candidates.iterrows():
            cand_id = int(cand["player_id"])
            cand_team = _get_team(cand_id, player_pool)
            if cand_team is not None and existing_counts.get(cand_team, 0) + 1 > MAX_PER_TEAM:
                continue
            chosen = cand
            break

        if chosen is None:
            continue

        gain = chosen["xP"] - worst_score

        # Decide whether the transfer is worth it
        threshold = TRANSFER_GAIN_THRESHOLD if len(transfers_in) < available_ft else HIT_THRESHOLD
        if gain >= threshold:
            chosen_id = int(chosen["player_id"])
            transfers_in.append(chosen_id)
            transfers_out.append(int(worst_id))
            budget = budget + worst_price - chosen["price"]
            current_ids = [p for p in current_ids if p != worst_id] + [chosen_id]
        # Do NOT break here: a different (less-bad) squad player might still
        # have a strong replacement available even if this one didn't.
        # Continue scanning the rest of the squad instead of bailing out.

            if len(transfers_in) > available_ft:
                hits += 1

    return transfers_in, transfers_out, hits


def select_captain(
    squad: Squad,
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
) -> tuple[int, int]:
    """
    Select captain and vice-captain.

    Captaincy MUST go to a player who is actually starting, so the pool of
    candidates is restricted to squad.lineup when it has been populated
    (falls back to the full squad if lineup hasn't been set yet).

    Returns:
        (captain_id, vice_captain_id)
    """
    candidate_ids = squad.lineup if squad.lineup else squad.players

    scores = {}
    for pid in candidate_ids:
        player_form = form_data[form_data["player_id"] == pid]
        player_info = player_pool[player_pool["player_id"] == pid]

        if player_form.empty or player_info.empty:
            scores[pid] = 0.0
            continue

        form = player_form.iloc[0]["form"]
        player = player_info.iloc[0]
        fixture_diff = player.get("fixture_difficulty", 3)
        is_home = bool(player.get("is_home", False))

        cs = captain_score(player, form, fixture_diff, is_home)
        xp = expected_points(player, form)

        # Ensemble blend: hedge the fixture/home-aware captain heuristic
        # against the general expected-points model.
        scores[pid] = CAPTAIN_SCORE_WEIGHT * cs + CAPTAIN_XP_WEIGHT * xp

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    if ranked:
        captain_id = ranked[0][0]
        vice_captain_id = ranked[1][0] if len(ranked) > 1 else (
            candidate_ids[1] if len(candidate_ids) > 1 else candidate_ids[0]
        )
    else:
        captain_id = candidate_ids[0] if candidate_ids else squad.players[0]
        vice_captain_id = (
            candidate_ids[1] if len(candidate_ids) > 1 else
            (squad.players[1] if len(squad.players) > 1 else captain_id)
        )

    return captain_id, vice_captain_id


def select_lineup(
    squad: Squad,
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
) -> tuple[list[int], list[int]]:
    """
    Select starting XI and bench order from 15-player squad.

    Uses an exhaustive search over all legal formations
    (1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD, 10 outfield) to find the
    combination that maximizes total expected points -- rather than a
    greedy "fill minimums, then best remaining" heuristic, which can
    leave points on the bench when the optimal formation differs from
    the minimum-satisfying one (e.g. 3-5-2 vs 3-4-3).

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

    gkps = sorted([(p, s) for p, pos, s in player_scores if pos == "GKP"], key=lambda x: -x[1])
    defs = sorted([(p, s) for p, pos, s in player_scores if pos == "DEF"], key=lambda x: -x[1])
    mids = sorted([(p, s) for p, pos, s in player_scores if pos == "MID"], key=lambda x: -x[1])
    fwds = sorted([(p, s) for p, pos, s in player_scores if pos == "FWD"], key=lambda x: -x[1])

    def _topsum(lst, n):
        chosen = lst[:n]
        return sum(s for _, s in chosen), [p for p, s in chosen]

    if not gkps:
        # No GKP found (shouldn't happen) -- fall back to first player.
        gk_id = squad.players[0] if squad.players else None
        gk_score = 0.0
    else:
        gk_id, gk_score = gkps[0][0], gkps[0][1]

    best_total = -np.inf
    best_combo = None  # (def_ids, mid_ids, fwd_ids)

    for def_n in range(3, 6):
        if def_n > len(defs):
            continue
        for mid_n in range(2, 6):
            if mid_n > len(mids):
                continue
            for fwd_n in range(1, 4):
                if fwd_n > len(fwds):
                    continue
                if def_n + mid_n + fwd_n != 10:
                    continue

                def_sum, def_ids = _topsum(defs, def_n)
                mid_sum, mid_ids = _topsum(mids, mid_n)
                fwd_sum, fwd_ids = _topsum(fwds, fwd_n)
                total = def_sum + mid_sum + fwd_sum

                if total > best_total:
                    best_total = total
                    best_combo = (def_ids, mid_ids, fwd_ids)

    if best_combo is None:
        # Extreme fallback: just take the greedy minimums (shouldn't
        # normally trigger given a legal 15-man squad).
        def_ids = [p for p, s in defs[:3]]
        mid_ids = [p for p, s in mids[:2]]
        fwd_ids = [p for p, s in fwds[:1]]
        best_combo = (def_ids, mid_ids, fwd_ids)

    def_ids, mid_ids, fwd_ids = best_combo

    lineup = []
    if gk_id is not None:
        lineup.append(gk_id)
    lineup.extend(def_ids)
    lineup.extend(mid_ids)
    lineup.extend(fwd_ids)

    # Bench: everyone not in lineup, ordered by score descending. Bench
    # GKP always goes last (can only be auto-subbed for the starting GKP).
    lineup_set = set(lineup)
    bench_candidates = [(p, s) for p, pos, s in player_scores if p not in lineup_set]
    bench_candidates.sort(key=lambda x: -x[1])

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

    Chips are valuable and unused chips are wasted value, so we evaluate
    them on merit throughout the whole season rather than restricting to
    a narrow end-of-season window. Additionally, since an unplayed chip
    at the end of the season is worth exactly zero, we substantially
    relax the trigger bar in the final few gameweeks so that any
    still-unused chip gets *some* value captured instead of none.

    Bench boost and triple captain triggers are now evaluated using the
    same fixture-aware expected-points / captain-ensemble metrics that
    drive the rest of the strategy (rather than raw trailing form), so
    the chip-timing decision is consistent with how player quality is
    assessed everywhere else. We also relax the "every player must have
    data" requirement to "at least 3 of 4 bench players must have data",
    since a single missing data point (e.g. a very new signing) should
    not zero out an otherwise-clear bench boost opportunity.

    Returns:
        Chip name or None.
    """
    if not squad.chips_available:
        return None

    remaining_gws = season_length - gameweek
    forcing_soon = remaining_gws <= FORCE_CHIP_WINDOW

    bb_threshold = BENCH_BOOST_FORCE_THRESHOLD if forcing_soon else BENCH_BOOST_FORM_THRESHOLD
    tc_threshold = TRIPLE_CAPTAIN_FORCE_THRESHOLD if forcing_soon else TRIPLE_CAPTAIN_FORM_THRESHOLD

    # Bench boost: play whenever the bench (all 4 players) is in strong
    # enough projected form (bar relaxed near season end to avoid wasting
    # the chip). Uses fixture-aware expected points rather than raw form.
    if "bench_boost" in squad.chips_available and squad.bench:
        bench_xps = []
        for pid in squad.bench:
            xp = _player_xp(pid, form_data, player_pool)
            if xp is not None:
                bench_xps.append(xp)

        if len(bench_xps) >= max(3, len(squad.bench) - 1) and bench_xps and np.mean(bench_xps) > bb_threshold:
            return "bench_boost"

    # Triple captain: play when the best starting player's captain-quality
    # ensemble score (fixture/home aware, same metric select_captain uses)
    # is high enough (bar relaxed near season end to avoid wasting the chip).
    if "triple_captain" in squad.chips_available:
        candidate_ids = squad.lineup if squad.lineup else squad.players
        tc_scores = []
        for pid in candidate_ids:
            s = _player_captain_ensemble(pid, form_data, player_pool)
            if s is not None:
                tc_scores.append(s)

        if tc_scores:
            best_score = max(tc_scores)
            if best_score > tc_threshold:
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

    # 2. Lineup is chosen BEFORE captaincy so that the captain armband can
    # never land on a bench player. (Fixture-aware captain heuristics can
    # otherwise rank a benched player above a starter with a slightly
    # lower raw score, which would be an invalid/wasted armband.)
    lineup, bench = select_lineup(updated_squad, form_data, player_pool)
    updated_squad.lineup = lineup
    updated_squad.bench = bench

    # 3. Captain (restricted to the chosen lineup)
    captain_id, vice_captain_id = select_captain(updated_squad, form_data, player_pool)

    # 4. Chip
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
