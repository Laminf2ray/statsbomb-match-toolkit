"""
Ingest StatsBomb open data for one competition/season into a local DuckDB database.

Usage:
    python src/pipeline.py                      # Euro 2024 (default)
    python src/pipeline.py --competition 55 --season 282
    python src/pipeline.py --limit 5            # quick test on 5 matches
    python src/pipeline.py --refresh            # ignore cached files and re-download

Steps:
    1. Download matches and events (cached as Parquet in data/raw/ so re-runs are fast)
    2. Flatten coordinate columns into numeric x/y fields
    3. Load tables into data/football.duckdb
    4. Build analysis views from sql/views.sql

Data: StatsBomb Open Data (https://github.com/statsbomb/open-data).
"""

import argparse
import json
import logging
import warnings
from pathlib import Path

import duckdb
import pandas as pd
from statsbombpy import sb

warnings.filterwarnings("ignore", module="statsbombpy")  # silences the no-credentials notice

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "football.duckdb"
VIEWS_SQL = ROOT / "sql" / "views.sql"

# StatsBomb stores locations as [x, y] lists on a 120 x 80 pitch.
LOCATION_COLS = {
    "location": ("x", "y"),
    "pass_end_location": ("pass_end_x", "pass_end_y"),
    "carry_end_location": ("carry_end_x", "carry_end_y"),
    "shot_end_location": ("shot_end_x", "shot_end_y"),  # may also hold a z value
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("pipeline")


def get_matches(competition_id: int, season_id: int, refresh: bool) -> pd.DataFrame:
    cache = RAW_DIR / f"matches_{competition_id}_{season_id}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    matches = sb.matches(competition_id=competition_id, season_id=season_id)
    matches.to_parquet(cache, index=False)
    return matches


def get_events(match_id: int, refresh: bool) -> pd.DataFrame:
    cache = RAW_DIR / "events" / f"{match_id}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    events = sb.events(match_id=match_id)
    events["match_id"] = match_id
    events = clean_events(events)
    cache.parent.mkdir(parents=True, exist_ok=True)
    events.to_parquet(cache, index=False)
    return events


def clean_events(events: pd.DataFrame) -> pd.DataFrame:
    """Split coordinate lists into numeric columns; store any other nested values as JSON text."""
    for col, (x_name, y_name) in LOCATION_COLS.items():
        if col in events.columns:
            coords = events[col].apply(lambda v: v if isinstance(v, (list, tuple)) else [None, None])
            events[x_name] = pd.to_numeric(coords.str[0], errors="coerce")
            events[y_name] = pd.to_numeric(coords.str[1], errors="coerce")
            events = events.drop(columns=col)

    for col in events.columns:
        if events[col].map(lambda v: isinstance(v, (list, dict))).any():
            events[col] = events[col].map(
                lambda v: json.dumps(v) if isinstance(v, (list, dict)) else v
            )
    return events


def load_to_duckdb(matches: pd.DataFrame, events: pd.DataFrame) -> None:
    con = duckdb.connect(str(DB_PATH))
    try:
        con.register("matches_df", matches)
        con.register("events_df", events)
        con.execute("CREATE OR REPLACE TABLE matches AS SELECT * FROM matches_df")
        con.execute("CREATE OR REPLACE TABLE events AS SELECT * FROM events_df")
        con.execute(VIEWS_SQL.read_text())

        n_matches = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        n_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        log.info("Loaded %s matches and %s events into %s", n_matches, f"{n_events:,}", DB_PATH.name)
    finally:
        con.close()


def run_checks(matches: pd.DataFrame, events: pd.DataFrame) -> None:
    """Basic data-quality checks: every match has events, and goals match the final scores."""
    missing = set(matches["match_id"]) - set(events["match_id"])
    if missing:
        log.warning("%d matches have no events: %s", len(missing), sorted(missing))

    # Goals from shots only (own goals are a separate event type), excluding penalty shoot-outs (period 5)
    shots = events[(events["type"] == "Shot") & (events["period"] < 5)]
    goals = shots[shots["shot_outcome"] == "Goal"].groupby("match_id").size()
    own_goals = events[(events["type"] == "Own Goal For") & (events["period"] < 5)].groupby("match_id").size()
    event_goals = goals.add(own_goals, fill_value=0)
    score_goals = matches.set_index("match_id").eval("home_score + away_score")
    mismatches = (event_goals.reindex(score_goals.index, fill_value=0) != score_goals).sum()
    if mismatches:
        log.warning("%d matches where event goals differ from the final score", mismatches)
    else:
        log.info("Check passed: goals in event data match final scores for all matches")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competition", type=int, default=55, help="StatsBomb competition_id (55 = UEFA Euro)")
    parser.add_argument("--season", type=int, default=282, help="StatsBomb season_id (282 = 2024)")
    parser.add_argument("--limit", type=int, help="only load the first N matches (for testing)")
    parser.add_argument("--refresh", action="store_true", help="re-download instead of using cached files")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    matches = get_matches(args.competition, args.season, args.refresh)
    matches = matches.sort_values(["match_date", "kick_off"]).reset_index(drop=True)
    if args.limit:
        matches = matches.head(args.limit)
    log.info("Fetching events for %d matches", len(matches))

    frames = []
    for i, match_id in enumerate(matches["match_id"], start=1):
        frames.append(get_events(int(match_id), args.refresh))
        if i % 10 == 0 or i == len(matches):
            log.info("  %d/%d matches", i, len(matches))

    events = pd.concat(frames, ignore_index=True)
    run_checks(matches, events)
    load_to_duckdb(matches, events)


if __name__ == "__main__":
    main()
