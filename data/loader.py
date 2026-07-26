"""
Loads and normalizes FPL historical data into structured DataFrames
ready for the backtest engine.

Key abstractions:
- SeasonData: all player-gameweek rows for one season, with normalized columns
- PlayerPool: available players at any given gameweek with their attributes
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass

DATA_DIR = Path(__file__).parent / "raw"

# Standard column mapping (vaastav repo columns vary slightly across seasons)
COLUMN_MAP = {
    "name": "name",
    "position": "position",
    "team": "team",
    "total_points": "points",
    "minutes": "minutes",
    "goals_scored": "goals",
    "assists": "assists",
    "clean_sheets": "clean_sheets",
    "bonus": "bonus",
    "bps": "bps",
    "value": "price",  # in tenths of millions (e.g., 55 = 5.5m)
    "selected": "ownership",
    "transfers_in": "transfers_in",
    "transfers_out": "transfers_out",
    "GW": "gameweek",
    "round": "gameweek",
    "element": "player_id",
    "was_home": "is_home",
    "opponent_team": "opponent",
    "fixture": "fixture_id",
    "influence": "influence",
    "creativity": "creativity",
    "threat": "threat",
    "ict_index": "ict_index",
    "expected_goals": "xG",
    "expected_assists": "xA",
    "expected_goal_involvements": "xGI",
}

# Position mapping
POSITION_MAP = {
    1: "GKP",
    2: "DEF",
    3: "MID",
    4: "FWD",
    "GK": "GKP",
    "GKP": "GKP",
    "DEF": "DEF",
    "MID": "MID",
    "FWD": "FWD",
}


@dataclass
class SeasonData:
    """Normalized data for one FPL season."""

    season: str
    players: pd.DataFrame  # player-gameweek level data
    num_gameweeks: int
    teams: pd.DataFrame | None = None

    @property
    def gameweeks(self) -> list[int]:
        return sorted(self.players["gameweek"].unique().tolist())


def load_season(season: str) -> SeasonData:
    """
    Load and normalize a single season's data.

    Returns a SeasonData with a DataFrame containing one row per player per gameweek,
    with standardized column names.
    """
    season_dir = DATA_DIR / season
    merged_path = season_dir / "merged_gw.csv"

    if not merged_path.exists():
        raise FileNotFoundError(
            f"No data for {season}. Run `python -m data.fetcher` first."
        )

    df = pd.read_csv(merged_path)

    # Normalize column names
    rename = {}
    for orig, standard in COLUMN_MAP.items():
        if orig in df.columns and standard not in df.columns:
            rename[orig] = standard
    df = df.rename(columns=rename)

    # Ensure gameweek column exists
    if "gameweek" not in df.columns:
        # Try to infer from 'round' or 'GW'
        for col in ["round", "GW"]:
            if col in df.columns:
                df["gameweek"] = df[col]
                break

    # Normalize position
    if "position" in df.columns:
        df["position"] = df["position"].map(
            lambda x: POSITION_MAP.get(x, x) if pd.notna(x) else x
        )
    elif "element_type" in df.columns:
        df["position"] = df["element_type"].map(POSITION_MAP)

    # Normalize price to millions (from tenths)
    if "price" in df.columns:
        # If max price > 200, it's probably in tenths already
        if df["price"].max() > 200:
            df["price"] = df["price"] / 10.0

    # Ensure player_id exists
    if "player_id" not in df.columns and "element" in df.columns:
        df["player_id"] = df["element"]
    elif "player_id" not in df.columns:
        # Create synthetic ID from name
        df["player_id"] = pd.factorize(df.get("name", df.index.astype(str)))[0]

    # Ensure points column
    if "points" not in df.columns and "total_points" in df.columns:
        df["points"] = df["total_points"]

    # Load teams if available
    teams_path = season_dir / "teams.csv"
    teams = pd.read_csv(teams_path) if teams_path.exists() else None

    num_gws = int(df["gameweek"].max()) if "gameweek" in df.columns else 38

    return SeasonData(
        season=season,
        players=df,
        num_gameweeks=num_gws,
        teams=teams,
    )


def load_seasons(seasons: list[str]) -> dict[str, SeasonData]:
    """Load multiple seasons, returning a dict keyed by season string."""
    result = {}
    for season in seasons:
        try:
            result[season] = load_season(season)
            print(
                f"  Loaded {season}: {len(result[season].players)} player-gameweek rows, "
                f"{result[season].num_gameweeks} gameweeks"
            )
        except FileNotFoundError as e:
            print(f"  Skipping {season}: {e}")
    return result


def get_gameweek_players(season_data: SeasonData, gameweek: int) -> pd.DataFrame:
    """Get all available players for a specific gameweek."""
    return season_data.players[season_data.players["gameweek"] == gameweek].copy()


def get_player_history(
    season_data: SeasonData, player_id: int, up_to_gw: int
) -> pd.DataFrame:
    """Get a player's historical data up to (but not including) a gameweek."""
    mask = (season_data.players["player_id"] == player_id) & (
        season_data.players["gameweek"] < up_to_gw
    )
    return season_data.players[mask].copy()


def compute_rolling_form(
    season_data: SeasonData, gameweek: int, window: int = 5
) -> pd.DataFrame:
    """
    Compute rolling average points for each player heading into a gameweek.
    This is what the inner agent uses to evaluate players.
    """
    history = season_data.players[season_data.players["gameweek"] < gameweek].copy()

    if history.empty:
        return pd.DataFrame()

    form = (
        history.sort_values("gameweek")
        .groupby("player_id")["points"]
        .apply(lambda x: x.tail(window).mean())
        .reset_index()
        .rename(columns={"points": "form"})
    )

    # Merge with latest known player info
    latest_gw = history["gameweek"].max()
    latest_info = history[history["gameweek"] == latest_gw][
        ["player_id", "name", "position", "team", "price"]
    ].drop_duplicates("player_id")

    form = form.merge(latest_info, on="player_id", how="left")
    return form


if __name__ == "__main__":
    # Test loading
    import sys

    seasons = sys.argv[1:] if len(sys.argv) > 1 else ["2023-24"]
    data = load_seasons(seasons)
    for season, sd in data.items():
        print(f"\n{season}:")
        print(f"  Columns: {list(sd.players.columns[:15])}...")
        print(f"  Gameweeks: {sd.gameweeks[:5]}...{sd.gameweeks[-5:]}")
