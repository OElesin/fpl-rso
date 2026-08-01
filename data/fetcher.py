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
]


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


def fetch_all(seasons: list[str] | None = None, force: bool = False):
    """Download data for all (or specified) seasons."""
    seasons = seasons or AVAILABLE_SEASONS
    print(f"Fetching FPL data for {len(seasons)} seasons...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for season in seasons:
        if season not in AVAILABLE_SEASONS:
            print(f"  [{season}] Not a known season, skipping.")
            continue
        fetch_season_data(season, force=force)

    print("Done.")


if __name__ == "__main__":
    fetch_all()
