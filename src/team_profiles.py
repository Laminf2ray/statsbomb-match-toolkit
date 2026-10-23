"""
Team style profiles: compare every team in the competition on attack, possession and defending.

Usage:
    python src/team_profiles.py

Run src/pipeline.py first so data/football.duckdb exists.

Outputs (in outputs/profiles/):
    team_metrics.csv     one row per team: raw metrics and percentile ranks
    style_map.png/.pdf   possession vs pressing intensity (PPDA), one dot per team
    xg_map.png/.pdf      non-penalty xG created vs conceded per 90 minutes
    teams/<Team>.png     one-page percentile profile for each team

Metrics are tournament totals scaled to 90 minutes (extra time counts as 30 extra minutes),
so teams that played more matches or extra time are compared fairly.

Data: StatsBomb Open Data (https://github.com/statsbomb/open-data).
"""

import logging
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from adjustText import adjust_text

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "football.duckdb"
OUT_DIR = ROOT / "outputs" / "profiles"

ACCENT = "#1D428A"
TEXT = "#1A1A1A"
MUTED = "#6B6B6B"
GRID = "#E6E6E6"
GOOD, MID, POOR = "#2E7D4F", "#C9A227", "#B23A3A"

STAGE_ORDER = ["Group Stage", "Round of 16", "Quarter-finals", "Semi-finals", "Final"]

# (column, label, format, higher_is_better)
METRICS = {
    "Attack": [
        ("npxg_90", "Non-penalty xG per 90", "{:.2f}", True),
        ("shots_90", "Shots per 90", "{:.1f}", True),
        ("npxg_per_shot", "npxG per shot", "{:.3f}", True),
        ("prog_passes_90", "Progressive passes per 90", "{:.1f}", True),
        ("field_tilt", "Field tilt", "{:.0%}", True),
    ],
    "Possession": [
        ("possession", "Possession", "{:.0%}", True),
        ("pass_accuracy", "Pass accuracy", "{:.0%}", True),
    ],
    "Defending": [
        ("ppda", "PPDA (lower = more pressing)", "{:.1f}", False),
        ("high_regains_90", "High regains per 90", "{:.1f}", True),
        ("npxg_against_90", "npxG conceded per 90", "{:.2f}", False),
    ],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("team_profiles")


# --------------------------------------------------------------------------------------
# Metrics (SQL does the counting; pandas does the ratios and ranks)
# --------------------------------------------------------------------------------------

METRICS_SQL = """
WITH ev AS (
    SELECT * FROM events WHERE period < 5           -- exclude penalty shoot-outs
),
teams AS (                                          -- one row per team per match, with the opponent
    SELECT match_id, home_team AS team, away_team AS opponent FROM matches
    UNION ALL
    SELECT match_id, away_team, home_team FROM matches
),
duration AS (                                       -- 120 minutes if the match went to extra time
    SELECT match_id, CASE WHEN MAX(period) >= 3 THEN 120 ELSE 90 END AS minutes
    FROM ev GROUP BY match_id
),
shots AS (
    SELECT match_id, team,
           COUNT(*) FILTER (WHERE shot_type <> 'Penalty')                          AS np_shots,
           SUM(shot_statsbomb_xg) FILTER (WHERE shot_type <> 'Penalty')            AS npxg
    FROM ev WHERE type = 'Shot' GROUP BY ALL
),
passes AS (
    SELECT match_id, team,
           COUNT(*)                                                                AS passes,
           COUNT(*) FILTER (WHERE pass_outcome IS NULL)                            AS completed,
           COUNT(*) FILTER (WHERE pass_outcome IS NULL AND x >= 40
                            AND (120 - pass_end_x) <= 0.75 * (120 - x))            AS prog_passes,
           COUNT(*) FILTER (WHERE pass_outcome IS NULL AND pass_end_x >= 80)       AS final_third,
           COUNT(*) FILTER (WHERE x <= 72)                                         AS own_60_passes
    FROM ev WHERE type = 'Pass' GROUP BY ALL
),
defending AS (
    SELECT match_id, team,
           -- defensive actions in the team's attacking 60%, for PPDA
           COUNT(*) FILTER (WHERE x >= 48 AND (type IN ('Interception', 'Foul Committed')
                            OR (type = 'Duel' AND duel_type = 'Tackle')))          AS def_actions,
           -- ball won back in the opponent's final third
           COUNT(*) FILTER (WHERE x >= 80 AND (
                   (type = 'Ball Recovery' AND ball_recovery_recovery_failure IS NULL)
                OR type = 'Interception'
                OR (type = 'Duel' AND duel_outcome IN ('Won', 'Success In Play', 'Success Out'))
           ))                                                                      AS high_regains
    FROM ev GROUP BY ALL
)
SELECT
    t.match_id, t.team, t.opponent, d.minutes,
    COALESCE(s.npxg, 0)          AS npxg,
    COALESCE(s.np_shots, 0)      AS np_shots,
    COALESCE(so.npxg, 0)         AS npxg_against,
    p.passes, p.completed, p.prog_passes, p.final_third,
    po.passes                    AS opp_passes,
    po.final_third               AS opp_final_third,
    po.own_60_passes             AS opp_own_60_passes,
    df.def_actions, df.high_regains
FROM teams t
JOIN duration d USING (match_id)
LEFT JOIN shots s      ON s.match_id = t.match_id  AND s.team = t.team
LEFT JOIN shots so     ON so.match_id = t.match_id AND so.team = t.opponent
LEFT JOIN passes p     ON p.match_id = t.match_id  AND p.team = t.team
LEFT JOIN passes po    ON po.match_id = t.match_id AND po.team = t.opponent
LEFT JOIN defending df ON df.match_id = t.match_id AND df.team = t.team
"""

RESULTS_SQL = """
WITH r AS (
    SELECT match_id, competition_stage AS stage, home_team AS team,
           home_score AS gf, away_score AS ga FROM matches
    UNION ALL
    SELECT match_id, competition_stage, away_team, away_score, home_score FROM matches
)
SELECT team, COUNT(*) AS matches,
       SUM(CAST(gf > ga AS INT)) AS won, SUM(CAST(gf = ga AS INT)) AS drawn, SUM(CAST(gf < ga AS INT)) AS lost,
       SUM(gf) AS goals_for, SUM(ga) AS goals_against, LIST(DISTINCT stage) AS stages
FROM r GROUP BY team
"""


def team_metrics(con) -> pd.DataFrame:
    m = con.execute(METRICS_SQL).df()
    g = m.groupby("team").sum(numeric_only=True)
    per90 = 90 / g["minutes"]

    df = pd.DataFrame({
        "npxg_90": g["npxg"] * per90,
        "shots_90": g["np_shots"] * per90,
        "npxg_per_shot": g["npxg"] / g["np_shots"],
        "prog_passes_90": g["prog_passes"] * per90,
        "field_tilt": g["final_third"] / (g["final_third"] + g["opp_final_third"]),
        "possession": g["passes"] / (g["passes"] + g["opp_passes"]),
        "pass_accuracy": g["completed"] / g["passes"],
        # Tournament totals, not an average of per-match ratios (which over-weights low-count games)
        "ppda": g["opp_own_60_passes"] / g["def_actions"],
        "high_regains_90": g["high_regains"] * per90,
        "npxg_against_90": g["npxg_against"] * per90,
        "minutes": g["minutes"],
    })

    results = con.execute(RESULTS_SQL).df().set_index("team")
    results["furthest_stage"] = results["stages"].apply(
        lambda s: max(s, key=STAGE_ORDER.index) if s is not None else "Group Stage")
    df = df.join(results.drop(columns="stages"))

    # Percentile rank among all teams (100 = best), flipped where lower is better
    for group in METRICS.values():
        for col, _, _, higher_better in group:
            pct = df[col].rank(pct=True, ascending=higher_better)
            df[f"{col}_pct"] = (pct * 100).round()
    return df.sort_values("npxg_90", ascending=False)


def style_label(row: pd.Series, med_poss: float, med_ppda: float) -> str:
    """Four broad styles from the two axes of the style map."""
    has_ball = row["possession"] >= med_poss
    presses = row["ppda"] <= med_ppda
    if has_ball and presses:
        return "Possession and high press"
    if has_ball:
        return "Possession, patient without the ball"
    if presses:
        return "Pressing, direct with the ball"
    return "Deep block and counter"


# --------------------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------------------

def style_axes(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(labelsize=8.5, colors=MUTED)
    ax.grid(color=GRID, linewidth=0.6, zorder=0)


def scatter_map(df, x, y, xlabel, ylabel, title, subtitle, quadrants, path, invert_y=False,
                xfmt=None, diagonal=False) -> None:
    fig, ax = plt.subplots(figsize=(9, 7))
    style_axes(ax)
    mx, my = df[x].median(), df[y].median()
    ax.axvline(mx, color=MUTED, linewidth=0.8, linestyle="--", zorder=1)
    ax.axhline(my, color=MUTED, linewidth=0.8, linestyle="--", zorder=1)

    finalists = df["furthest_stage"].isin(["Semi-finals", "Final"])
    ax.scatter(df.loc[~finalists, x], df.loc[~finalists, y], s=60, color="#9AA5B8", zorder=3,
               edgecolor="white", linewidth=0.8, label="Out before semi-finals")
    ax.scatter(df.loc[finalists, x], df.loc[finalists, y], s=80, color=ACCENT, zorder=4,
               edgecolor="white", linewidth=0.8, label="Semi-finalists")

    pad = (df[x].max() - df[x].min()) * 0.08
    ax.set_xlim(df[x].min() - pad, df[x].max() + pad)
    if diagonal:  # created = conceded, drawn only across the visible x range
        lo, hi = ax.get_xlim()
        ax.plot([lo, hi], [lo, hi], color=GRID, linewidth=1.2, zorder=1)
        ax.set_xlim(lo, hi)
        ypad = (df[y].max() - df[y].min()) * 0.08
        ax.set_ylim(df[y].max() + ypad, df[y].min() - 2.2 * ypad)  # flipped; extra room for corner labels

    labels = [ax.text(r[x], r[y], team, fontsize=8, color=TEXT if f else MUTED,
                      fontweight="bold" if f else "normal", zorder=5)
              for (team, r), f in zip(df.iterrows(), finalists)]
    if not diagonal:
        ypad = (df[y].max() - df[y].min()) * 0.08
        lo_y, hi_y = df[y].min() - 2.2 * ypad, df[y].max() + 2.2 * ypad  # room for corner labels
        ax.set_ylim((hi_y, lo_y) if invert_y else (lo_y, hi_y))
    ax.figure.canvas.draw()
    adjust_text(labels, ax=ax, expand=(1.2, 1.4), arrowprops=dict(arrowstyle="-", color=GRID, lw=0.6))

    # Quadrant names in the corners
    for (qx, qy, text) in quadrants:
        ax.text(qx, qy, text, transform=ax.transAxes, fontsize=9, color=MUTED, style="italic",
                ha="left" if qx < 0.5 else "right", va="top" if qy > 0.5 else "bottom")

    ax.set_xlabel(xlabel, fontsize=9.5, color=MUTED)
    ax.set_ylabel(ylabel, fontsize=9.5, color=MUTED)
    if xfmt:
        ax.xaxis.set_major_formatter(xfmt)
    fig.legend(*ax.get_legend_handles_labels(), frameon=False, fontsize=8.5, loc="lower right",
               bbox_to_anchor=(0.97, 0.005), ncol=2)
    fig.text(0.07, 0.955, title, fontsize=15, fontweight="bold", color=TEXT)
    fig.text(0.07, 0.92, subtitle, fontsize=9.5, color=MUTED)
    fig.text(0.07, 0.015, "Data: StatsBomb Open Data · UEFA Euro 2024 · dashed lines = median of all 24 teams",
             fontsize=7.5, color=MUTED)
    fig.subplots_adjust(left=0.09, right=0.97, top=0.88, bottom=0.14)
    fig.savefig(path.with_suffix(".png"), dpi=150)
    fig.savefig(path.with_suffix(".pdf"))
    plt.close(fig)
    log.info("Saved %s", path.with_suffix(".png").relative_to(ROOT))


def team_page(team: str, row: pd.Series, style: str, path: Path) -> None:
    """One-page percentile profile: a bar per metric, coloured by percentile."""
    rows = [(group, *m) for group, ms in METRICS.items() for m in ms]
    fig, ax = plt.subplots(figsize=(8.27, 6.2))
    ax.set_xlim(0, 100)
    ax.set_ylim(len(rows) + len(METRICS) - 0.3, -0.7)
    ax.axis("off")

    y = 0
    for group, ms in METRICS.items():
        ax.text(0, y, group.upper(), fontsize=8.5, fontweight="bold", color=MUTED, va="center")
        y += 1
        for col, label, fmt, _ in ms:
            pct = row[f"{col}_pct"]
            colour = GOOD if pct >= 67 else MID if pct >= 34 else POOR
            ax.barh(y, 100, height=0.55, color=GRID, zorder=1)
            ax.barh(y, pct, height=0.55, color=colour, zorder=2)
            for q in (25, 50, 75):
                ax.plot([q, q], [y - 0.28, y + 0.28], color="white", linewidth=1, zorder=3)
            ax.text(-2, y, label, ha="right", va="center", fontsize=9, color=TEXT)
            ax.text(102, y, f"{fmt.format(row[col])}", ha="left", va="center", fontsize=9,
                    fontweight="bold", color=TEXT)
            ax.text(pct - 1.5 if pct > 12 else pct + 1.5, y, f"{pct:.0f}", va="center", fontsize=7.5,
                    ha="right" if pct > 12 else "left", color="white" if pct > 12 else TEXT, zorder=4)
            y += 1

    n = {k: int(row[k]) for k in ("matches", "won", "drawn", "lost", "goals_for", "goals_against")}
    record = (f"P{n['matches']}  W{n['won']} D{n['drawn']} L{n['lost']}  ·  "
              f"Goals {n['goals_for']}–{n['goals_against']}  (shoot-outs count as draws)")
    fig.text(0.05, 0.94, team, fontsize=20, fontweight="bold", color=TEXT)
    fig.text(0.05, 0.895, f"{record}  ·  Reached: {row['furthest_stage']}", fontsize=9.5, color=MUTED)
    fig.text(0.05, 0.86, f"Style: {style}", fontsize=10, color=ACCENT, fontweight="bold")
    fig.text(0.05, 0.025, "Bars = percentile among the 24 Euro 2024 teams (100 = best). PPDA and xG conceded are "
             "flipped, so lower values rank higher.\nValues on the right are per 90 minutes; penalties are excluded "
             "from xG. Style = quadrant on the possession vs PPDA map.\n"
             "Data: StatsBomb Open Data · github.com/Laminf2ray/statsbomb-match-toolkit",
             fontsize=6.8, color=MUTED, linespacing=1.6)
    fig.subplots_adjust(left=0.36, right=0.86, top=0.82, bottom=0.15)
    fig.savefig(path, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------------------

def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found. Run `python src/pipeline.py` first.")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        df = team_metrics(con)
    finally:
        con.close()

    med_poss, med_ppda = df["possession"].median(), df["ppda"].median()
    df["style"] = df.apply(style_label, axis=1, med_poss=med_poss, med_ppda=med_ppda)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "teams").mkdir(exist_ok=True)
    df.round(3).to_csv(OUT_DIR / "team_metrics.csv", index_label="team")
    log.info("Saved %s (%d teams)", (OUT_DIR / "team_metrics.csv").relative_to(ROOT), len(df))

    pct = matplotlib.ticker.PercentFormatter(1.0, decimals=0)
    scatter_map(
        df, "possession", "ppda", "Possession (share of passes)", "PPDA (lower = more intense pressing)",
        "How each team played: with the ball and without it",
        "Right = more of the ball · Up = harder pressing (PPDA axis flipped)",
        [(0.02, 0.98, "Pressing, direct with the ball"), (0.98, 0.98, "Possession and high press"),
         (0.02, 0.02, "Deep block and counter"), (0.98, 0.02, "Possession, patient without the ball")],
        OUT_DIR / "style_map", invert_y=True, xfmt=pct,
    )
    scatter_map(
        df, "npxg_90", "npxg_against_90", "Non-penalty xG created per 90", "Non-penalty xG conceded per 90",
        "Chance quality: created vs conceded",
        "Right = created more · Up = conceded less (axis flipped) · grey line = created equals conceded",
        [(0.02, 0.98, "Solid, short of chances"), (0.98, 0.98, "Strong at both ends"),
         (0.02, 0.02, "Struggling at both ends"), (0.98, 0.02, "Open: create and concede")],
        OUT_DIR / "xg_map", invert_y=True, diagonal=True,
    )

    for team, row in df.iterrows():
        team_page(team, row, row["style"], OUT_DIR / "teams" / f"{team.replace(' ', '-')}.png")
    log.info("Saved %d team profiles to %s", len(df), (OUT_DIR / "teams").relative_to(ROOT))


if __name__ == "__main__":
    main()
