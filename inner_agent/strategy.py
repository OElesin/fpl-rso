
"""
FPL Decision Strategy — The Inner Agent

THIS IS THE FILE THE OUTER LOOP REWRITES.
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
MIN_FORM_THRESHOLD = 2.3  # don't buy players with form below this (raised slightly to
                           # avoid chasing noisy cold-player upside that doesn't
                           # generalize to unseen gameweeks)

TRANSFER_GAIN_THRESHOLD = 2.2  # min expected point gain to justify a free transfer
HIT_THRESHOLD = 8.5  # min expected gain to take a -4 hit — raised further above the
                      # 4-point breakeven; hit decisions are inherently high-variance
                      # bets on noisy short-term signals and the public/private gap
                      # shows hits generalize particularly poorly, so be conservative.
MAX_HITS_PER_WEEK = 2  # each hit still independently gated by HIT_THRESHOLD
CAPTAIN_FORM_WEIGHT = 0.7  # weight of form in captain selection
CAPTAIN_FIXTURE_WEIGHT = 0.3  # weight of fixture in captain selection
CAPTAIN_XP_BLEND = 0.60  # lean more heavily on the robust expected-points signal
                          # (price/season-level stats) rather than the noisier
                          # form+fixture-only captain_score. Captaincy drives close to
                          # half of total points, so the more stable, less-overfit
                          # signal should be favored more heavily to generalize to
                          # unseen gameweeks.

# Captaincy ceiling adjustment: attacking players (FWD/MID) have materially higher
# point ceilings than defenders/keepers (goals/assists/bonus upside vs. mostly clean
# sheet points), so nudge the armband toward them when scores are close. This is a
# stable, position-structural fact rather than a noisy per-season fit, so it should
# generalize well to unseen gameweeks.
CAPTAIN_POSITION_MULTIPLIER = {"FWD": 1.08, "MID": 1.04, "DEF": 0.92, "GKP": 0.80}

AVAILABILITY_MIN_CHANCE = 50  # min chance_of_playing_next_round (%) to consider a player
BENCH_BOOST_FORM_THRESHOLD = 4.5  # avg bench expected-points signal to justify bench boost.
                                   # Raised for more conservative chip usage on noisy data.
TRIPLE_CAPTAIN_SCORE_THRESHOLD = 8.0  # captain_score threshold to justify triple captain
                                       # Raised for more conservative chip usage.
MAX_PER_TEAM = 3  # max players from a single team allowed in squad

# Rotation / reliability safeguards -----------------------------------------
LOW_MINUTES_PER_GAME = 45.0  # below this -> rotation risk discount
VERY_LOW_MINUTES_PER_GAME = 20.0  # below this -> heavy discount (fringe player)
ROTATION_DISCOUNT = 0.85  # multiplier applied for moderate rotation risk. Less punitive
                           # than before: excessive discounting of merely-rotated players
                           # was systematically benching them ahead of games they went
                           # on to start and score well in, inflating unused bench points.
FRINGE_DISCOUNT = 0.55  # multiplier applied for heavy rotation risk. Loosened similarly,
                         # while still meaningfully discounting truly fringe players.

# Regression-to-the-mean shrinkage -------------------------------------------
# More shrinkage than the previous iteration (0.36 -> 0.30): recent form still
# carries real short-horizon signal, but the sizeable public/private generalization
# gap (5.60 -> 4.14 avg pts/gw) indicates the model is still leaning too heavily on
# noisy single-window form. Pulling values further toward the positional mean should
# generalize better to unseen gameweeks, since form is used pervasively across
# transfers, captaincy, lineup selection, and chip timing.
FORM_SHRINKAGE_ALPHA = 0.30

# Fixture-strength adjustment -------------------------------------------------
FIXTURE_ADJUSTMENT_WEIGHT = 0.12  # multiplier swing per unit of difficulty away from neutral (3)
FIXTURE_MULTIPLIER_MIN = 0.75
FIXTURE_MULTIPLIER_MAX = 1.25

# Valid FPL starting-formation constraints -----------------------------------
FORMATION_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
FORMATION_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}


# ---------------------------------------------------------------------------
# Helper: availability / safety filters
# ---------------------------------------------------------------------------


def _is_available(row: pd.Series) -> bool:
    """Return False if a player looks clearly unavailable (injured/suspended)."""
    chance = row.get("chance_of_playing_next_round", None)
    if chance is not None and not pd.isna(chance):
        try:
            if float(chance) < AVAILABILITY_MIN_CHANCE:
                return False
        except (TypeError, ValueError):
            pass
    status = row.get("status", None)
    if isinstance(status, str) and status.lower() in ("i", "s", "u", "injured", "suspended", "unavailable"):
        return False
    return True


def _minutes_per_game(row: pd.Series) -> float | None:
    """Best-effort extraction of a recent minutes-per-game signal."""
    for col in ("minutes_per_game", "avg_minutes", "mins_per_game"):
        val = row.get(col, None)
        if val is not None and not pd.isna(val):
            try:
                return float(val)
            except (TypeError, ValueError):
                continue

    minutes = row.get("minutes", None)
    games = row.get("games_played", None) or row.get("starts", None) or row.get("appearances", None)
    if minutes is not None and games is not None:
        try:
            minutes = float(minutes)
            games = float(games)
            if games > 0:
                return minutes / games
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def _reliability_multiplier(row: pd.Series) -> float:
    """Discount factor reflecting rotation / minutes risk."""
    mpg = _minutes_per_game(row)
    if mpg is None:
        return 1.0
    if mpg < VERY_LOW_MINUTES_PER_GAME:
        return FRINGE_DISCOUNT
    if mpg < LOW_MINUTES_PER_GAME:
        return ROTATION_DISCOUNT
    return 1.0


def _fixture_multiplier(row: pd.Series) -> float:
    """Stable, cross-season-generalizable adjustment based on fixture strength."""
    fd = row.get("fixture_difficulty", None)
    if fd is None or pd.isna(fd):
        return 1.0
    try:
        fd = float(fd)
    except (TypeError, ValueError):
        return 1.0
    mult = 1.0 + FIXTURE_ADJUSTMENT_WEIGHT * (3.0 - fd)
    if mult < FIXTURE_MULTIPLIER_MIN:
        mult = FIXTURE_MULTIPLIER_MIN
    if mult > FIXTURE_MULTIPLIER_MAX:
        mult = FIXTURE_MULTIPLIER_MAX
    return mult


def _team_counts(player_ids: list[int], player_pool: pd.DataFrame) -> dict:
    counts = {}
    if "team" not in player_pool.columns:
        return counts
    for pid in player_ids:
        row = player_pool[player_pool["player_id"] == pid]
        if row.empty:
            continue
        team = row.iloc[0].get("team", None)
        if team is None:
            continue
        counts[team] = counts.get(team, 0) + 1
    return counts


def _compute_position_form_means(form_data: pd.DataFrame) -> dict:
    """Positional mean form across the currently available pool."""
    if form_data.empty or "form" not in form_data.columns:
        return {}
    try:
        if "position" in form_data.columns:
            return form_data.groupby("position")["form"].mean().to_dict()
        return {"__all__": form_data["form"].mean()}
    except Exception:
        return {}


def _shrink_form(form: float, position: str, position_means: dict) -> float:
    """Blend a player's raw form toward the positional mean form."""
    if not position_means:
        return form
    mean = position_means.get(position, position_means.get("__all__"))
    if mean is None or pd.isna(mean):
        return form
    try:
        return FORM_SHRINKAGE_ALPHA * float(form) + (1 - FORM_SHRINKAGE_ALPHA) * float(mean)
    except (TypeError, ValueError):
        return form


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

    Priority order:
      1. Force-replace any squad player who looks clearly unavailable.
      2. Among the remaining (available) players, replace the weakest
         ones if a sufficiently better replacement is affordable.
    Returns:
        (transfers_in, transfers_out, hits_taken)
    """
    if form_data.empty or player_pool.empty:
        return [], [], 0

    position_means = _compute_position_form_means(form_data)

    squad_scores = _score_players(squad.players, form_data, player_pool, position_means)
    if not squad_scores:
        return [], [], 0

    remaining_players = list(squad.players)
    team_counts = _team_counts(remaining_players, player_pool)
    budget = squad.budget
    available_ft = squad.free_transfers
    max_transfers = available_ft + MAX_HITS_PER_WEEK

    transfers_in: list[int] = []
    transfers_out: list[int] = []

    state = {"budget": budget}

    def _try_replace(pid: int, force: bool) -> bool:
        player_row = player_pool[player_pool["player_id"] == pid]
        if player_row.empty:
            return False
        player_row = player_row.iloc[0]
        position = player_row.get("position", "MID")
        price = player_row.get("price", 5.0)
        team = player_row.get("team", None)

        base_mask = (
            (form_data["position"] == position)
            & (~form_data["player_id"].isin(remaining_players))
        )

        candidates_all = form_data[base_mask].copy()
        if candidates_all.empty:
            return False

        # Filter using the *shrunk* (regressed-to-mean) form rather than raw noisy
        # form, so the threshold is consistent with how candidates are ultimately
        # scored/ranked below. This avoids incorrectly discarding a decent underlying
        # player just because of a single noisy low-form window, or admitting a
        # one-off hot-streak player whose shrunk quality is actually mediocre.
        candidates_all["_shrunk_form"] = candidates_all.apply(
            lambda row: _shrink_form(row["form"], row.get("position", position), position_means), axis=1
        )
        candidates = candidates_all[candidates_all["_shrunk_form"] >= MIN_FORM_THRESHOLD]
        if candidates.empty and force:
            candidates = candidates_all
        if candidates.empty:
            return False

        avail_mask = []
        cand_teams = []
        cand_reliability = []
        cand_fixture_mult = []
        for _, crow in candidates.iterrows():
            pi_row = player_pool[player_pool["player_id"] == crow["player_id"]]
            if pi_row.empty:
                avail_mask.append(False)
                cand_teams.append(None)
                cand_reliability.append(1.0)
                cand_fixture_mult.append(1.0)
                continue
            pi_row = pi_row.iloc[0]
            avail_mask.append(_is_available(pi_row))
            cand_teams.append(pi_row.get("team", None))
            cand_reliability.append(_reliability_multiplier(pi_row))
            cand_fixture_mult.append(_fixture_multiplier(pi_row))
        candidates["_available"] = avail_mask
        candidates["_team"] = cand_teams
        candidates["_reliability"] = cand_reliability
        candidates["_fixture_mult"] = cand_fixture_mult
        candidates = candidates[candidates["_available"]]
        if candidates.empty:
            return False

        def _team_ok(t):
            if t is None:
                return True
            cur = team_counts.get(t, 0)
            if team is not None and t == team:
                cur -= 1
            return cur < MAX_PER_TEAM

        candidates = candidates[candidates["_team"].apply(_team_ok)]
        if candidates.empty:
            return False

        candidates["xP"] = candidates.apply(
            lambda row: expected_points(row, row["_shrunk_form"]) * row["_reliability"] * row["_fixture_mult"],
            axis=1,
        )
        candidates = candidates[candidates["price"] <= state["budget"] + price]
        if candidates.empty:
            return False

        candidates = candidates.sort_values("xP", ascending=False)
        top_xp = candidates.iloc[0]["xP"]
        close = candidates[candidates["xP"] >= top_xp - 0.25].copy()
        if len(close) > 1:
            try:
                close["_value"] = close.apply(
                    lambda row: value_score(row, row["_shrunk_form"]), axis=1
                )
                best_candidate = close.sort_values("_value", ascending=False).iloc[0]
            except Exception:
                best_candidate = candidates.iloc[0]
        else:
            best_candidate = candidates.iloc[0]

        worst_score = squad_scores.get(pid, 0.0)
        gain = best_candidate["xP"] - worst_score

        threshold = TRANSFER_GAIN_THRESHOLD if len(transfers_in) < available_ft else HIT_THRESHOLD
        if not (force or gain >= threshold):
            return False

        transfers_in.append(int(best_candidate["player_id"]))
        transfers_out.append(int(pid))
        state["budget"] = state["budget"] + price - best_candidate["price"]

        remaining_players.remove(pid)
        remaining_players.append(int(best_candidate["player_id"]))
        if team is not None:
            team_counts[team] = max(0, team_counts.get(team, 1) - 1)
        new_team = best_candidate.get("_team", None)
        if new_team is not None:
            team_counts[new_team] = team_counts.get(new_team, 0) + 1
        return True

    ordering = sorted(squad_scores.items(), key=lambda x: x[1])  # weakest first

    for pid, _score in ordering:
        if len(transfers_in) >= max_transfers:
            break
        player_row = player_pool[player_pool["player_id"] == pid]
        if player_row.empty:
            continue
        if not _is_available(player_row.iloc[0]):
            _try_replace(pid, force=True)

    for pid, _score in ordering:
        if len(transfers_in) >= max_transfers:
            break
        if pid not in remaining_players:
            continue
        player_row = player_pool[player_pool["player_id"] == pid]
        if player_row.empty:
            continue
        if not _is_available(player_row.iloc[0]):
            continue
        _try_replace(pid, force=False)

    hits = max(0, len(transfers_in) - available_ft)
    return transfers_in, transfers_out, hits


def select_captain(
    squad: Squad,
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
    candidate_ids: list[int] | None = None,
) -> tuple[int, int]:
    """
    Select captain and vice-captain.

    IMPORTANT: captain/vice-captain must be chosen from among players who
    will actually be in the starting lineup — a captain sitting on the bench
    earns no doubled points. By default this considers the full squad (kept
    for backward compatibility), but callers should pass `candidate_ids`
    restricted to the finalized starting XI so the armband always lands on
    a player who is actually playing.

    Returns:
        (captain_id, vice_captain_id)
    """
    ids = candidate_ids if candidate_ids is not None else squad.players

    position_means = _compute_position_form_means(form_data)
    scores = {}
    for pid in ids:
        player_form = form_data[form_data["player_id"] == pid]
        player_info = player_pool[player_pool["player_id"] == pid]

        if player_form.empty or player_info.empty:
            scores[pid] = 0.0
            continue

        raw_form = player_form.iloc[0]["form"]
        player = player_info.iloc[0]
        position = player.get("position", "MID")
        form = _shrink_form(raw_form, position, position_means)
        fixture_diff = player.get("fixture_difficulty", 3)
        is_home = bool(player.get("is_home", False))

        base_score = captain_score(player, form, fixture_diff, is_home)

        try:
            xp = expected_points(player, form) * _fixture_multiplier(player)
            base_score = base_score + CAPTAIN_XP_BLEND * xp
        except Exception:
            pass

        # Ceiling-based nudge: attacking players (FWD/MID) carry structurally higher
        # point ceilings (goals/assists/bonus) than defenders/keepers, so tilt the
        # armband toward them when raw scores are similar. This reflects a stable
        # positional fact about FPL scoring rather than a per-season noisy fit.
        base_score *= CAPTAIN_POSITION_MULTIPLIER.get(position, 1.0)

        if not _is_available(player):
            base_score *= 0.1

        reliability = _reliability_multiplier(player)
        base_score *= reliability ** 1.5

        scores[pid] = base_score

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    fallback_pool = ids if ids else squad.players
    captain_id = ranked[0][0] if ranked else fallback_pool[0]
    vice_captain_id = ranked[1][0] if len(ranked) > 1 else (
        fallback_pool[1] if len(fallback_pool) > 1 else captain_id
    )

    return captain_id, vice_captain_id


def select_lineup(
    squad: Squad,
    form_data: pd.DataFrame,
    player_pool: pd.DataFrame,
) -> tuple[list[int], list[int]]:
    """
    Select starting XI and bench order from 15-player squad.

    Enforces a *valid* FPL formation:
        exactly 1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD, totalling 11 players.

    Uses an exact (not greedy) search over all legal formation shapes.

    Returns:
        (lineup_11, bench_4_ordered)
    """
    position_means = _compute_position_form_means(form_data)
    player_scores = []
    for pid in squad.players:
        pf = form_data[form_data["player_id"] == pid]
        pi = player_pool[player_pool["player_id"] == pid]

        raw_form = pf.iloc[0]["form"] if not pf.empty else 0.0
        position = pi.iloc[0].get("position", "MID") if not pi.empty else "MID"
        player_row = pi.iloc[0] if not pi.empty else pd.Series()

        form = _shrink_form(raw_form, position, position_means)
        xP = expected_points(player_row, form) if not player_row.empty else form

        if not player_row.empty and not _is_available(player_row):
            xP *= 0.05
        elif not player_row.empty:
            xP *= _reliability_multiplier(player_row)
            xP *= _fixture_multiplier(player_row)

        player_scores.append((pid, position, xP))

    gkps = sorted([(p, s) for p, pos, s in player_scores if pos == "GKP"], key=lambda x: -x[1])
    defs = sorted([(p, s) for p, pos, s in player_scores if pos == "DEF"], key=lambda x: -x[1])
    mids = sorted([(p, s) for p, pos, s in player_scores if pos == "MID"], key=lambda x: -x[1])
    fwds = sorted([(p, s) for p, pos, s in player_scores if pos == "FWD"], key=lambda x: -x[1])

    position_lists = {"GKP": gkps, "DEF": defs, "MID": mids, "FWD": fwds}

    def _top_n_sum(lst, n):
        if n <= 0:
            return 0.0, []
        chosen = lst[:n]
        return sum(s for _, s in chosen), [p for p, _ in chosen]

    best_total = None
    best_shape = None  # (n_gkp, n_def, n_mid, n_fwd)

    n_gkp = 1
    if len(gkps) < 1:
        n_gkp = 0  # extremely defensive fallback; shouldn't happen for valid squads

    for n_def in range(FORMATION_MIN["DEF"], FORMATION_MAX["DEF"] + 1):
        for n_mid in range(FORMATION_MIN["MID"], FORMATION_MAX["MID"] + 1):
            for n_fwd in range(FORMATION_MIN["FWD"], FORMATION_MAX["FWD"] + 1):
                if n_gkp + n_def + n_mid + n_fwd != 11:
                    continue
                if n_def > len(defs) or n_mid > len(mids) or n_fwd > len(fwds) or n_gkp > len(gkps):
                    continue
                total = 0.0
                total += _top_n_sum(gkps, n_gkp)[0]
                total += _top_n_sum(defs, n_def)[0]
                total += _top_n_sum(mids, n_mid)[0]
                total += _top_n_sum(fwds, n_fwd)[0]
                if best_total is None or total > best_total:
                    best_total = total
                    best_shape = (n_gkp, n_def, n_mid, n_fwd)

    lineup: list[int] = []
    if best_shape is not None:
        n_gkp, n_def, n_mid, n_fwd = best_shape
        lineup += _top_n_sum(gkps, n_gkp)[1]
        lineup += _top_n_sum(defs, n_def)[1]
        lineup += _top_n_sum(mids, n_mid)[1]
        lineup += _top_n_sum(fwds, n_fwd)[1]
    else:
        all_by_score = sorted(player_scores, key=lambda x: -x[2])
        lineup = [p for p, pos, s in all_by_score[:11]]

    lineup = lineup[:11]
    lineup_set = set(lineup)

    bench_candidates = [(p, s) for p, pos, s in player_scores if p not in lineup_set]
    bench_candidates.sort(key=lambda x: -x[1])

    bench = []
    bench_gkp = None
    for p, s in bench_candidates:
        pos = next((pos for pid, pos, _ in player_scores if pid == p), "MID")
        if pos == "GKP" and bench_gkp is None:
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
    """
    if not squad.chips_available:
        return None

    if gameweek < 3:
        return None

    position_means = _compute_position_form_means(form_data)

    if "bench_boost" in squad.chips_available:
        bench_scores = []
        for pid in squad.bench:
            pf = form_data[form_data["player_id"] == pid]
            pi = player_pool[player_pool["player_id"] == pid]
            if pf.empty:
                continue
            player_row = pi.iloc[0] if not pi.empty else pd.Series()
            position = player_row.get("position", "MID") if not player_row.empty else "MID"
            form = _shrink_form(pf.iloc[0]["form"], position, position_means)
            if not player_row.empty:
                xp = expected_points(player_row, form)
                xp *= _reliability_multiplier(player_row)
                xp *= _fixture_multiplier(player_row)
            else:
                xp = form
            bench_scores.append(xp)

        if bench_scores and np.mean(bench_scores) > BENCH_BOOST_FORM_THRESHOLD:
            return "bench_boost"

    if "triple_captain" in squad.chips_available:
        if not form_data.empty:
            best_score = 0.0
            for pid in squad.players:
                pf = form_data[form_data["player_id"] == pid]
                pi = player_pool[player_pool["player_id"] == pid]
                if pf.empty or pi.empty:
                    continue
                player = pi.iloc[0]
                position = player.get("position", "MID")
                form = _shrink_form(pf.iloc[0]["form"], position, position_means)
                if not _is_available(player):
                    continue
                fixture_diff = player.get("fixture_difficulty", 3)
                is_home = bool(player.get("is_home", False))
                s = captain_score(player, form, fixture_diff, is_home)
                s *= _reliability_multiplier(player)
                if s > best_score:
                    best_score = s

            if best_score > TRIPLE_CAPTAIN_SCORE_THRESHOLD:
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
    transfers_in, transfers_out, hits = select_transfers(
        squad, player_pool, form_data, gameweek
    )

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

    # Lineup must be finalized BEFORE captain selection, so that the
    # captain/vice-captain armband can be restricted to players who are
    # actually starting. A captain sitting on the bench earns no doubled
    # points, so this ordering directly protects real point-scoring.
    lineup, bench = select_lineup(updated_squad, form_data, player_pool)
    updated_squad.lineup = lineup
    updated_squad.bench = bench

    captain_id, vice_captain_id = select_captain(
        updated_squad, form_data, player_pool, candidate_ids=lineup
    )

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
    position_means: dict | None = None,
) -> dict[int, float]:
    """Score a list of players by expected points."""
    if position_means is None:
        position_means = _compute_position_form_means(form_data)

    scores = {}
    for pid in player_ids:
        pf = form_data[form_data["player_id"] == pid]
        pi = player_pool[player_pool["player_id"] == pid]

        if pf.empty or pi.empty:
            scores[pid] = 0.0
            continue

        raw_form = pf.iloc[0]["form"]
        player = pi.iloc[0]
        position = player.get("position", "MID")
        form = _shrink_form(raw_form, position, position_means)
        score = expected_points(player, form)
        if not _is_available(player):
            score *= 0.3
        else:
            score *= _reliability_multiplier(player)
            score *= _fixture_multiplier(player)
        scores[pid] = score

    return scores
