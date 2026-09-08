"""
Fetches FPL historical data from the vaastav/Fantasy-Premier-League GitHub repository.

This gives us complete player-level gameweek data for backtesting:
- Points scored, minutes, goals, assists, clean sheets, bonus, etc.
- Fixture difficulty, opponent, home/away
- Price changes, transfers in/out, ownership
"""

import os
import io
import zipfile
import requests
import pandas as pd
from pathlib import Path

REPO_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master"
DATA_DIR = Path(__file__).parent / "raw"

# Seasons available in the repo (2016-17 onwards)
AVAILABLE_SEASONS = [
    "2016-17",
    "2017-18",
    "2018-19",
    "2019-20",
    "2020-21",
    "2021-22",
    "2022-23",
    "2023-24",
    "2024-25",
    "2025-26",
    "2026-27",
]

# The current live season — fetched from the FPL API (vaastav lags), not the repo.
CURRENT_SEASON = "2026-27"


def fetch_season_data(season: str, force: bool = False) -> Path:
    """
    Download player gameweek data for a given season.
    Returns path to the season's data directory.
    """
    season_dir = DATA_DIR / season
    merged_gw_path = season_dir / "merged_gw.csv"

    if merged_gw_path.exists() and not force:
        print(f"  [{season}] Already downloaded, skipping.")
        return season_dir

    season_dir.mkdir(parents=True, exist_ok=True)

    # Try merged_gw.csv first (single file with all gameweek data)
    url = f"{REPO_BASE}/data/{season}/gws/merged_gw.csv"
    print(f"  [{season}] Fetching {url}")
    resp = requests.get(url, timeout=30)

    if resp.status_code == 200:
        merged_gw_path.write_bytes(resp.content)
        print(f"  [{season}] Saved merged_gw.csv ({len(resp.content) // 1024} KB)")
    else:
        print(f"  [{season}] merged_gw.csv not found, trying individual GW files...")
        _fetch_individual_gws(season, season_dir)

    # Also fetch player ID mapping
    players_url = f"{REPO_BASE}/data/{season}/players_raw.csv"
    resp = requests.get(players_url, timeout=30)
    if resp.status_code == 200:
        (season_dir / "players_raw.csv").write_bytes(resp.content)

    # Fetch team data
    teams_url = f"{REPO_BASE}/data/{season}/teams.csv"
    resp = requests.get(teams_url, timeout=30)
    if resp.status_code == 200:
        (season_dir / "teams.csv").write_bytes(resp.content)

    # Fetch fixture data
    fixtures_url = f"{REPO_BASE}/data/{season}/fixtures.csv"
    resp = requests.get(fixtures_url, timeout=30)
    if resp.status_code == 200:
        (season_dir / "fixtures.csv").write_bytes(resp.content)

    return season_dir


def _fetch_individual_gws(season: str, season_dir: Path):
    """Fallback: fetch individual GW files and merge them."""
    all_gws = []
    for gw_num in range(1, 39):
        url = f"{REPO_BASE}/data/{season}/gws/gw{gw_num}.csv"
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            df = pd.read_csv(io.StringIO(resp.text))
            df["GW"] = gw_num
            all_gws.append(df)
        else:
            break  # No more gameweeks

    if all_gws:
        merged = pd.concat(all_gws, ignore_index=True)
        merged.to_csv(season_dir / "merged_gw.csv", index=False)
        print(f"  [{season}] Merged {len(all_gws)} gameweeks")
    else:
        print(f"  [{season}] WARNING: No data found")


FPL_API = "https://fantasy.premierleague.com/api"
_UA = {"User-Agent": "Mozilla/5.0"}

# Live-season position mapping (FPL API element_type -> label)
_LIVE_POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def fetch_current_season_live(season: str, force: bool = False) -> Path:
    """
    Build a backtest-ready merged_gw.csv for the CURRENT season directly from the
    live FPL API, covering all FINISHED gameweeks.

    The community vaastav dataset lags the live season by several weeks, so for
    current-season optimization we pull completed gameweeks from the official API
    (/event/{gw}/live/) and normalize them into the same schema the loader expects
    (one row per player per gameweek, with points/minutes/xG/etc.).

    Returns the season directory path.
    """
    season_dir = DATA_DIR / season
    merged_gw_path = season_dir / "merged_gw.csv"
    season_dir.mkdir(parents=True, exist_ok=True)

    boot = requests.get(f"{FPL_API}/bootstrap-static/", headers=_UA, timeout=30).json()
    finished_gws = [e["id"] for e in boot["events"] if e["finished"]]

    if not finished_gws:
        print(f"  [{season}] No finished gameweeks yet in the live API.")
        return season_dir

    # If we already have all finished GWs and not forcing, skip.
    if merged_gw_path.exists() and not force:
        try:
            existing = pd.read_csv(merged_gw_path)
            have = set(existing["GW"].unique().tolist()) if "GW" in existing else set()
            if set(finished_gws).issubset(have):
                print(f"  [{season}] Live data already current (GWs {sorted(have)}).")
                return season_dir
        except Exception:
            pass  # fall through and rebuild

    # Static player + team info
    elements = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}

    # Build fixture difficulty / home-away lookup per (gw, team)
    fixtures = requests.get(f"{FPL_API}/fixtures/", headers=_UA, timeout=30).json()
    fdr = {}  # (gw, team_id) -> (difficulty, is_home, opponent_team_id)
    for fx in fixtures:
        gw = fx.get("event")
        if gw is None:
            continue
        fdr[(gw, fx["team_h"])] = (fx.get("team_h_difficulty", 3), True, fx["team_a"])
        fdr[(gw, fx["team_a"])] = (fx.get("team_a_difficulty", 3), False, fx["team_h"])

    rows = []
    for gw in finished_gws:
        live = requests.get(f"{FPL_API}/event/{gw}/live/", headers=_UA, timeout=30).json()
        for el in live["elements"]:
            pid = el["id"]
            info = elements.get(pid, {})
            st = el["stats"]
            team_id = info.get("team")
            diff, is_home, _opp = fdr.get((gw, team_id), (3, False, None))
            rows.append({
                "element": pid,
                "name": f"{info.get('first_name','')} {info.get('second_name','')}".strip(),
                "position": _LIVE_POS.get(info.get("element_type"), "MID"),
                "team": teams.get(team_id, "UNK"),
                "GW": gw,
                "total_points": st.get("total_points", 0),
                "minutes": st.get("minutes", 0),
                "goals_scored": st.get("goals_scored", 0),
                "assists": st.get("assists", 0),
                "clean_sheets": st.get("clean_sheets", 0),
                "bonus": st.get("bonus", 0),
                "bps": st.get("bps", 0),
                "value": info.get("now_cost", 0),  # tenths; loader divides by 10
                "selected": float(info.get("selected_by_percent", 0) or 0),
                "transfers_in": info.get("transfers_in_event", 0),
                "transfers_out": info.get("transfers_out_event", 0),
                "was_home": is_home,
                "opponent_team": _opp,
                "influence": st.get("influence", 0),
                "creativity": st.get("creativity", 0),
                "threat": st.get("threat", 0),
                "ict_index": st.get("ict_index", 0),
                "expected_goals": st.get("expected_goals", 0),
                "expected_assists": st.get("expected_assists", 0),
                "expected_goal_involvements": st.get("expected_goal_involvements", 0),
            })

    merged = pd.DataFrame(rows)
    merged.to_csv(merged_gw_path, index=False)
    print(f"  [{season}] Built merged_gw.csv from live API: "
          f"{len(merged)} rows across GWs {finished_gws}")

    # Persist teams for completeness
    teams_df = pd.DataFrame(boot["teams"])
    teams_df.to_csv(season_dir / "teams.csv", index=False)

    return season_dir


def fetch_all(seasons: list[str] | None = None, force: bool = False):
    """Download data for all (or specified) seasons."""
    seasons = seasons or AVAILABLE_SEASONS
    print(f"Fetching FPL data for {len(seasons)} seasons...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for season in seasons:
        if season not in AVAILABLE_SEASONS:
            print(f"  [{season}] Not a known season, skipping.")
            continue
        if season == CURRENT_SEASON:
            # Current season: use the live FPL API (community repo lags weeks behind).
            fetch_current_season_live(season, force=force)
        else:
            fetch_season_data(season, force=force)

    print("Done.")


if __name__ == "__main__":
    fetch_all()
