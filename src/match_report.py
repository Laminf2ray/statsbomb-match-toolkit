"""
Generate a one-page post-match report (PDF and PNG) for a match in the DuckDB database.

Usage:
    python src/match_report.py --list                 # show available matches and their IDs
    python src/match_report.py                        # report for the most recent match (default)
    python src/match_report.py --match 3943043        # report for a specific match
    python src/match_report.py --all                  # reports for every match

Run src/pipeline.py first so data/football.duckdb exists.

The report contains:
    - Header: score, scorers, competition stage, date and venue
    - Key stats: shots, shots on target, xG, possession, passing, progressive passes, field tilt, PPDA
    - Shot map, sized by xG
    - Cumulative xG timeline with goals marked
    - Pass networks for each team (longest spell without a substitution or red card)

Data: StatsBomb Open Data (https://github.com/statsbomb/open-data).
"""

import argparse
import logging
import warnings
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")  # render to files, no window needed
import matplotlib.pyplot as plt
import pandas as pd
from mplsoccer import Pitch, VerticalPitch
from statsbombpy import sb

warnings.filterwarnings("ignore", module="statsbombpy")

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "football.duckdb"
LINEUP_DIR = ROOT / "data" / "raw" / "lineups"
OUT_DIR = ROOT / "outputs" / "reports"

# Colours
HOME_COLOUR = "#C8102E"
AWAY_COLOUR = "#1D428A"
TEXT = "#1A1A1A"
MUTED = "#6B6B6B"
GRID = "#E6E6E6"
PITCH_LINES = "#B5B5B5"

ON_TARGET = {"Goal", "Saved", "Saved to Post"}
DEFENSIVE_ACTIONS = ("Interception", "Foul Committed")  # plus tackles (Duel with duel_type 'Tackle')

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("match_report")


# --------------------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------------------

def connect() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found. Run `python src/pipeline.py` first.")
    return duckdb.connect(str(DB_PATH), read_only=True)


def load_match(con, match_id: int) -> tuple[pd.Series, pd.DataFrame]:
    match = con.execute("SELECT * FROM matches WHERE match_id = ?", [match_id]).df()
    if match.empty:
        raise SystemExit(f"Match {match_id} is not in the database. Use --list to see available matches.")
    events = con.execute(
        "SELECT * FROM events WHERE match_id = ? AND period < 5 ORDER BY period, timestamp, \"index\"",
        [match_id],
    ).df()
    return match.iloc[0], events


def load_lineups(match_id: int) -> pd.DataFrame:
    """Player nicknames and shirt numbers (cached locally after the first download)."""
    cache = LINEUP_DIR / f"{match_id}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    frames = []
    for team, df in sb.lineups(match_id=match_id).items():
        df = df[["player_id", "player_name", "player_nickname", "jersey_number"]].copy()
        df["team"] = team
        frames.append(df)
    lineups = pd.concat(frames, ignore_index=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    lineups.to_parquet(cache, index=False)
    return lineups


def short_name(player_name: str, nickname) -> str:
    """Display name: nickname if StatsBomb has one, otherwise the player's surname."""
    if isinstance(nickname, str) and nickname.strip():
        return nickname
    return str(player_name).split()[-1]


def name_lookup(lineups: pd.DataFrame) -> dict:
    return {r.player_name: short_name(r.player_name, r.player_nickname) for r in lineups.itertuples()}


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------

def team_stats(events: pd.DataFrame, team: str, opponent: str) -> dict:
    shots = events[(events["type"] == "Shot") & (events["team"] == team)]
    passes = events[(events["type"] == "Pass") & (events["team"] == team)]
    all_passes = events[events["type"] == "Pass"]
    completed = passes["pass_outcome"].isna()

    progressive = (
        completed
        & (passes["x"] >= 40)
        & ((120 - passes["pass_end_x"]) <= 0.75 * (120 - passes["x"]))
    )

    # Field tilt: share of completed final-third passes in the match
    final_third = all_passes[all_passes["pass_outcome"].isna() & (all_passes["pass_end_x"] >= 80)]
    tilt = (final_third["team"] == team).mean() if len(final_third) else float("nan")

    # PPDA: opponent passes in their own 60% of the pitch, divided by our defensive actions
    # in the same zone (our attacking 60%). Lower = more intense pressing.
    opp_passes = all_passes[(all_passes["team"] == opponent) & (all_passes["x"] <= 72)]
    defensive = events[
        (events["team"] == team)
        & (events["x"] >= 48)
        & (
            events["type"].isin(DEFENSIVE_ACTIONS)
            | ((events["type"] == "Duel") & (events["duel_type"] == "Tackle"))
        )
    ]
    ppda = len(opp_passes) / len(defensive) if len(defensive) else float("nan")

    return {
        "Goals": int((shots["shot_outcome"] == "Goal").sum()),
        "xG": shots["shot_statsbomb_xg"].sum(),
        "Shots": len(shots),
        "On target": int(shots["shot_outcome"].isin(ON_TARGET).sum()),
        "Possession": len(passes) / len(all_passes) if len(all_passes) else float("nan"),
        "Passes": len(passes),
        "Pass accuracy": completed.mean() if len(passes) else float("nan"),
        "Progressive passes": int(progressive.sum()),
        "Field tilt": tilt,
        "PPDA": ppda,
    }


def scorers(events: pd.DataFrame, team: str, opponent: str, lineups: pd.DataFrame) -> list[str]:
    """Goal scorers with minutes, including own goals scored by the opponent."""
    names = name_lookup(lineups)
    lines = []
    goals = events[(events["type"] == "Shot") & (events["team"] == team) & (events["shot_outcome"] == "Goal")]
    for g in goals.itertuples():
        pen = " (pen)" if g.shot_type == "Penalty" else ""
        lines.append((g.minute, f"{names.get(g.player, g.player)} {g.minute + 1}'{pen}"))
    own = events[(events["type"] == "Own Goal Against") & (events["team"] == opponent)]
    for g in own.itertuples():
        lines.append((g.minute, f"{names.get(g.player, g.player)} {g.minute + 1}' (og)"))
    return [text for _, text in sorted(lines)]


# --------------------------------------------------------------------------------------
# Chart panels
# --------------------------------------------------------------------------------------

def draw_header(fig, match: pd.Series, events: pd.DataFrame, lineups: pd.DataFrame) -> None:
    home, away = match["home_team"], match["away_team"]
    fig.text(0.5, 0.965, f"{home}  {match['home_score']} – {match['away_score']}  {away}",
             ha="center", va="center", fontsize=22, fontweight="bold", color=TEXT)

    stage = match.get("competition_stage", "")
    venue = match.get("stadium", "")
    details = " · ".join(str(v) for v in [match["competition"], stage, match["match_date"], venue]
                         if isinstance(v, str) and v)
    fig.text(0.5, 0.937, details, ha="center", va="center", fontsize=9.5, color=MUTED)

    home_goals = scorers(events, home, away, lineups)
    away_goals = scorers(events, away, home, lineups)
    fig.text(0.30, 0.915, "\n".join(home_goals), ha="right", va="top", fontsize=8.5, color=HOME_COLOUR)
    fig.text(0.70, 0.915, "\n".join(away_goals), ha="left", va="top", fontsize=8.5, color=AWAY_COLOUR)


def draw_stats(ax, home: str, away: str, home_stats: dict, away_stats: dict) -> None:
    """Head-to-head stat bars."""
    ax.set_xlim(-1, 1)
    rows = [
        ("xG", "{:.2f}"), ("Shots", "{}"), ("On target", "{}"), ("Possession", "{:.0%}"),
        ("Passes", "{}"), ("Pass accuracy", "{:.0%}"), ("Progressive passes", "{}"),
        ("Field tilt", "{:.0%}"), ("PPDA", "{:.1f}"),
    ]
    ax.set_ylim(len(rows) - 0.4, -0.9)
    ax.axis("off")

    ax.text(-1, -0.75, home, ha="left", fontsize=10, fontweight="bold", color=HOME_COLOUR)
    ax.text(1, -0.75, away, ha="right", fontsize=10, fontweight="bold", color=AWAY_COLOUR)

    for i, (label, fmt) in enumerate(rows):
        h, a = home_stats[label], away_stats[label]
        ax.text(0, i - 0.22, label, ha="center", va="center", fontsize=8.5, color=MUTED)
        ax.text(-1, i - 0.22, fmt.format(h), ha="left", va="center", fontsize=9.5, fontweight="bold", color=TEXT)
        ax.text(1, i - 0.22, fmt.format(a), ha="right", va="center", fontsize=9.5, fontweight="bold", color=TEXT)

        total = (h + a) if label != "PPDA" else (1 / h + 1 / a)  # lower PPDA = more pressing
        if not total or pd.isna(total):
            continue
        share = (h / total) if label != "PPDA" else ((1 / h) / total)
        ax.barh(i + 0.12, -share, left=0, height=0.16, color=HOME_COLOUR, alpha=0.85)
        ax.barh(i + 0.12, 1 - share, left=0, height=0.16, color=AWAY_COLOUR, alpha=0.85)
        ax.barh(i + 0.12, -1, left=0, height=0.16, color=GRID, zorder=0)
        ax.barh(i + 0.12, 1, left=0, height=0.16, color=GRID, zorder=0)


def draw_shot_map(ax, events: pd.DataFrame, home: str, away: str) -> None:
    pitch = Pitch(pitch_type="statsbomb", line_color=PITCH_LINES, linewidth=1, goal_type="box")
    pitch.draw(ax=ax)
    shots = events[events["type"] == "Shot"]

    for team, colour, flip in [(home, HOME_COLOUR, True), (away, AWAY_COLOUR, False)]:
        s = shots[shots["team"] == team]
        x = 120 - s["x"] if flip else s["x"]   # home team shoots right to left
        y = 80 - s["y"] if flip else s["y"]
        goal = s["shot_outcome"] == "Goal"
        size = 40 + s["shot_statsbomb_xg"] * 900
        pitch.scatter(x[~goal], y[~goal], s=size[~goal], ax=ax, facecolor="none",
                      edgecolor=colour, linewidth=1.3, alpha=0.8, zorder=2)
        pitch.scatter(x[goal], y[goal], s=size[goal], ax=ax, facecolor=colour,
                      edgecolor="white", linewidth=1, zorder=3)

    ax.set_title("Shot map  (size = xG, filled = goal)", fontsize=10, color=TEXT, loc="left", pad=4)
    ax.text(30, 83, f"← {home} attacking", ha="center", va="top", fontsize=8, color=HOME_COLOUR)
    ax.text(90, 83, f"{away} attacking →", ha="center", va="top", fontsize=8, color=AWAY_COLOUR)


def draw_xg_timeline(ax, events: pd.DataFrame, home: str, away: str) -> None:
    shots = events[events["type"] == "Shot"]
    end = max(90, int(events["minute"].max()) + 1)

    for team, colour in [(home, HOME_COLOUR), (away, AWAY_COLOUR)]:
        s = shots[shots["team"] == team].sort_values(["minute", "second"])
        minutes = [0] + s["minute"].tolist() + [end]
        xg = [0] + s["shot_statsbomb_xg"].cumsum().tolist()
        xg = xg + [xg[-1]]
        ax.step(minutes, xg, where="post", color=colour, linewidth=2, label=f"{team}  {xg[-1]:.2f}")
        goals = s[s["shot_outcome"] == "Goal"]
        goal_xg = s["shot_statsbomb_xg"].cumsum()[goals.index]
        ax.scatter(goals["minute"], goal_xg, color=colour, s=45, zorder=3, edgecolor="white")

    for m in (45, 90, 105):
        if m < end:
            ax.axvline(m, color=GRID, linewidth=1, zorder=0)
    ax.set_xlim(0, end)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Minute", fontsize=8.5, color=MUTED)
    ax.set_ylabel("Cumulative xG", fontsize=8.5, color=MUTED)
    ax.tick_params(labelsize=8, colors=MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.legend(frameon=False, fontsize=8.5, loc="upper left")
    ax.set_title("xG timeline  (dots = goals)", fontsize=10, color=TEXT, loc="left", pad=4)


def draw_pass_network(ax, events: pd.DataFrame, team: str, colour: str, lineups: pd.DataFrame) -> None:
    """Average positions and passing links for the starting XI until the team's first substitution."""
    pitch = VerticalPitch(pitch_type="statsbomb", line_color=PITCH_LINES, linewidth=1)
    pitch.draw(ax=ax)

    team_events = events[events["team"] == team]

    # Use the longest spell with an unchanged XI (between substitutions or red cards),
    # so an early injury substitution doesn't leave only a few minutes of data.
    changes = team_events[
        (team_events["type"] == "Substitution")
        | team_events["foul_committed_card"].isin(["Red Card", "Second Yellow"])
    ]["minute"]
    match_end = int(events["minute"].max()) + 1
    bounds = [0] + sorted(changes.unique().tolist()) + [match_end]
    start, cutoff = max(zip(bounds[:-1], bounds[1:]), key=lambda w: w[1] - w[0])

    passes = team_events[
        (team_events["type"] == "Pass")
        & team_events["pass_outcome"].isna()
        & (team_events["minute"] >= start)
        & (team_events["minute"] < cutoff)
        & team_events["pass_recipient"].notna()
    ]

    # Average location: where the player passes from and receives the ball
    locs = pd.concat([
        passes[["player", "x", "y"]],
        passes[["pass_recipient", "pass_end_x", "pass_end_y"]].set_axis(["player", "x", "y"], axis=1),
    ])
    nodes = locs.groupby("player").agg(x=("x", "mean"), y=("y", "mean"), touches=("x", "size"))

    links = (passes.assign(a=passes[["player", "pass_recipient"]].min(axis=1),
                           b=passes[["player", "pass_recipient"]].max(axis=1))
             .groupby(["a", "b"]).size().reset_index(name="count"))
    links = links[links["count"] >= 3]

    max_count = links["count"].max() if not links.empty else 1
    for link in links.itertuples():
        if link.a not in nodes.index or link.b not in nodes.index:
            continue
        p1, p2 = nodes.loc[link.a], nodes.loc[link.b]
        pitch.lines(p1.x, p1.y, p2.x, p2.y, ax=ax, color=colour,
                    lw=0.5 + 5 * link.count / max_count, alpha=0.25 + 0.5 * link.count / max_count, zorder=1)

    team_lineup = lineups[lineups["team"] == team].set_index("player_name")
    pitch.scatter(nodes.x, nodes.y, s=80 + nodes.touches * 6, ax=ax, color=colour,
                  edgecolor="white", linewidth=1.2, zorder=2)
    for player, node in nodes.iterrows():
        number = team_lineup["jersey_number"].get(player, "")
        pitch.annotate(str(number), (node.x, node.y), ax=ax, ha="center", va="center",
                       fontsize=7.5, color="white", fontweight="bold", zorder=3)

    # Key: shirt numbers to names, ordered by number
    names = name_lookup(lineups)
    key = sorted(
        (int(team_lineup["jersey_number"].get(p, 0)), names.get(p, p))
        for p in nodes.index if p in team_lineup.index
    )
    entries = [f"{n} {name}" for n, name in key]
    key_text = "\n".join("   ".join(entries[i:i + 4]) for i in range(0, len(entries), 4))
    ax.text(40, -3, key_text, ha="center", va="top", fontsize=6.5, color=MUTED, linespacing=1.5)

    window = "full match" if (start, cutoff) == (0, match_end) else f"{int(start)}'–{int(cutoff)}'"
    ax.set_title(f"{team} pass network ({window})", fontsize=10, color=colour, pad=4)


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------

def build_report(con, match_id: int) -> Path:
    match, events = load_match(con, match_id)
    lineups = load_lineups(match_id)
    home, away = match["home_team"], match["away_team"]
    home_stats = team_stats(events, home, away)
    away_stats = team_stats(events, away, home)

    fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
    fig.patch.set_facecolor("white")
    grid = fig.add_gridspec(
        nrows=3, ncols=2, left=0.08, right=0.95, top=0.855, bottom=0.115,
        height_ratios=[1.0, 0.75, 1.3], hspace=0.28, wspace=0.15,
    )

    draw_header(fig, match, events, lineups)
    draw_stats(fig.add_subplot(grid[0, 0]), home, away, home_stats, away_stats)
    draw_shot_map(fig.add_subplot(grid[0, 1]), events, home, away)
    draw_xg_timeline(fig.add_subplot(grid[1, :]), events, home, away)
    draw_pass_network(fig.add_subplot(grid[2, 0]), events, home, HOME_COLOUR, lineups)
    draw_pass_network(fig.add_subplot(grid[2, 1]), events, away, AWAY_COLOUR, lineups)

    fig.text(0.08, 0.034, "Possession = share of passes  ·  Field tilt = share of completed final-third passes  ·  "
             "PPDA = opponent passes per defensive action (lower = more pressing)\n"
             "Pass networks: longest spell without a substitution; node size = involvement, "
             "links = 3+ completed passes",
             fontsize=6, color=MUTED, linespacing=1.6)
    fig.text(0.08, 0.012, "Data: StatsBomb Open Data", fontsize=6.5, color=MUTED)
    fig.text(0.95, 0.012, "github.com/Laminf2ray/statsbomb-match-toolkit", ha="right", fontsize=6.5, color=MUTED)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{match['match_date']}_{home}_v_{away}".replace(" ", "-")
    pdf_path = OUT_DIR / f"{stem}.pdf"
    fig.savefig(pdf_path)
    fig.savefig(OUT_DIR / f"{stem}.png", dpi=150)
    plt.close(fig)
    log.info("Saved %s", pdf_path.relative_to(ROOT))
    return pdf_path


def list_matches(con) -> None:
    df = con.execute("""
        SELECT match_id, match_date, competition_stage AS stage,
               home_team || ' ' || home_score || '-' || away_score || ' ' || away_team AS result
        FROM matches ORDER BY match_date, kick_off
    """).df()
    print(df.to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--match", type=int, help="StatsBomb match_id (default: most recent match)")
    parser.add_argument("--all", action="store_true", help="build reports for every match")
    parser.add_argument("--list", action="store_true", help="list available matches")
    args = parser.parse_args()

    con = connect()
    try:
        if args.list:
            list_matches(con)
            return
        if args.all:
            ids = [r[0] for r in con.execute("SELECT match_id FROM matches ORDER BY match_date").fetchall()]
        elif args.match:
            ids = [args.match]
        else:
            ids = [con.execute("SELECT match_id FROM matches ORDER BY match_date DESC, kick_off DESC LIMIT 1").fetchone()[0]]
        for match_id in ids:
            build_report(con, match_id)
    finally:
        con.close()


if __name__ == "__main__":
    main()
