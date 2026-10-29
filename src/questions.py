"""
Three analysis questions on Euro 2024 event data, each answered with a chart and a 95% confidence interval.

    Q1  Does pressing intensity drop as matches go on?
    Q2  Does the scoreline change how teams play? (game state: leading, level, trailing)
    Q3  Do possessions that start high up the pitch produce better chances?

Usage:
    python src/questions.py

Run src/pipeline.py first so data/football.duckdb exists.
Outputs: outputs/questions/*.png and results.json (the numbers quoted in docs/questions.md).

Method notes
    - Ratios are pooled across all matches (total of numerators / total of denominators), not averages
      of per-match ratios, so low-count matches don't distort them.
    - 95% confidence intervals come from a bootstrap that resamples whole matches (2,000 resamples).
      Events within a match are not independent, so resampling events would give intervals that are too narrow.
    - Penalty shoot-outs (period 5) are excluded throughout; extra time is excluded from Q1 so every
      match contributes the same 90 minutes.

Data: StatsBomb Open Data (https://github.com/statsbomb/open-data).
"""

import json
import logging
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "football.duckdb"
OUT_DIR = ROOT / "outputs" / "questions"

N_BOOT = 2000
SEED = 42

ACCENT = "#1D428A"
SECOND = "#C8102E"
TEXT = "#1A1A1A"
MUTED = "#6B6B6B"
GRID = "#E6E6E6"

DEFENSIVE_ACTION = """(type IN ('Interception', 'Foul Committed') OR (type = 'Duel' AND duel_type = 'Tackle'))"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("questions")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def bootstrap_ratio(per_match: pd.DataFrame, num: str, den: str, groups: str | None = None,
                    scale: float = 1.0) -> pd.DataFrame:
    """Pooled ratio sum(num) / sum(den) with a match-level bootstrap 95% interval.

    per_match has one row per match (and per group, if given) with numerator and denominator counts.
    """
    rng = np.random.default_rng(SEED)
    matches = per_match["match_id"].unique()
    keys = [None] if groups is None else sorted(per_match[groups].unique(), key=str)

    # matrix of counts: match x group
    def pivot(col):
        if groups is None:
            return per_match.groupby("match_id")[col].sum().reindex(matches, fill_value=0).to_numpy()[:, None]
        return (per_match.pivot_table(index="match_id", columns=groups, values=col, aggfunc="sum")
                .reindex(index=matches, columns=keys).fillna(0).to_numpy())

    n, d = pivot(num), pivot(den)
    est = n.sum(0) / d.sum(0) * scale
    idx = rng.integers(0, len(matches), size=(N_BOOT, len(matches)))
    boot = n[idx].sum(1) / d[idx].sum(1) * scale
    lo, hi = np.percentile(boot, [2.5, 97.5], axis=0)
    out = pd.DataFrame({"estimate": est, "low": lo, "high": hi}, index=keys)
    out.attrs["boot"] = boot
    return out


def style_axes(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(labelsize=9, colors=MUTED)
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)


def finish(fig, title: str, subtitle: str, path: Path, note: str) -> None:
    fig.text(0.07, 0.95, title, fontsize=15, fontweight="bold", color=TEXT)
    fig.text(0.07, 0.905, subtitle, fontsize=9.5, color=MUTED)
    fig.text(0.07, 0.02, note + "\nData: StatsBomb Open Data · UEFA Euro 2024 · "
             "github.com/Laminf2ray/statsbomb-match-toolkit", fontsize=7.5, color=MUTED, linespacing=1.6)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    log.info("Saved %s", path.relative_to(ROOT))


def fmt_ci(row, f="{:.1f}") -> str:
    return f"{f.format(row['estimate'])} (95% CI {f.format(row['low'])}–{f.format(row['high'])})"


# --------------------------------------------------------------------------------------
# Q1: pressing by match period
# --------------------------------------------------------------------------------------

Q1_SQL = f"""
WITH ev AS (
    SELECT e.*,
           CASE WHEN period = 1 THEN LEAST(minute, 44) ELSE LEAST(GREATEST(minute, 45), 89) END AS m
    FROM events e WHERE period IN (1, 2)          -- regular time only; stoppage time joins the last bin
),
binned AS (
    SELECT *, CAST(FLOOR(m / 15) AS INT) AS bin FROM ev
),
stoppage AS (                                     -- minutes of stoppage time in each half
    SELECT match_id, period,
           GREATEST(MAX(minute * 60 + second) / 60.0 - CASE WHEN period = 1 THEN 45 ELSE 90 END, 0) AS extra
    FROM ev GROUP BY match_id, period
),
bins AS (                                         -- length of each 15-minute bin, plus stoppage for the last of each half
    SELECT s.match_id, b.bin,
           15 + CASE WHEN b.bin = 2 AND s.period = 1 THEN s.extra
                     WHEN b.bin = 5 AND s.period = 2 THEN s.extra ELSE 0 END AS minutes
    FROM stoppage s
    JOIN (SELECT UNNEST([0, 1, 2]) AS bin, 1 AS period UNION ALL SELECT UNNEST([3, 4, 5]), 2) b USING (period)
),
teams AS (
    SELECT match_id, home_team AS team, away_team AS opponent FROM matches
    UNION ALL SELECT match_id, away_team, home_team FROM matches
),
counts AS (
    SELECT t.match_id, t.team, b.bin,
           -- opponent passes in their own 60% of the pitch, allowed by this team
           COUNT(*) FILTER (WHERE b.team = t.opponent AND b.type = 'Pass' AND b.x <= 72)     AS opp_passes,
           -- this team's defensive actions in its attacking 60% (tackles, interceptions, fouls)
           COUNT(*) FILTER (WHERE b.team = t.team AND b.x >= 48 AND (b.type IN ('Interception', 'Foul Committed')
                            OR (b.type = 'Duel' AND b.duel_type = 'Tackle')))              AS def_actions,
           COUNT(*) FILTER (WHERE b.team = t.team AND b.x >= 48 AND (b.type = 'Interception'
                            OR (b.type = 'Duel' AND b.duel_type = 'Tackle')))              AS def_actions_no_fouls,
           COUNT(*) FILTER (WHERE b.team = t.team AND b.type = 'Pressure' AND b.x >= 60)     AS high_pressures,
           COUNT(*) FILTER (WHERE b.team = t.team AND b.type = 'Foul Committed')             AS fouls
    FROM teams t JOIN binned b ON b.match_id = t.match_id
    GROUP BY ALL
)
SELECT c.*, bins.minutes FROM counts c JOIN bins USING (match_id, bin)
"""

BIN_LABELS = ["0–15", "15–30", "30–45+", "45–60", "60–75", "75–90+"]


def q1(con) -> dict:
    df = con.execute(Q1_SQL).df()
    res = {
        "PPDA": bootstrap_ratio(df, "opp_passes", "def_actions", groups="bin"),
        "PPDA without fouls": bootstrap_ratio(df, "opp_passes", "def_actions_no_fouls", groups="bin"),
        "High pressures per 15 min": bootstrap_ratio(df, "high_pressures", "minutes", groups="bin", scale=15),
        "Opponent build-up passes per 15 min": bootstrap_ratio(df, "opp_passes", "minutes", groups="bin", scale=15),
        "Fouls per 15 min": bootstrap_ratio(df, "fouls", "minutes", groups="bin", scale=15),
    }
    for d in res.values():
        d.index = BIN_LABELS

    def change(name):  # last 15 vs first 15, % change with its own interval
        d, b = res[name], res[name].attrs["boot"]
        boot = (b[:, 5] / b[:, 0] - 1) * 100
        return {"estimate": float(d["estimate"].iloc[5] / d["estimate"].iloc[0] - 1) * 100,
                "low": float(np.percentile(boot, 2.5)), "high": float(np.percentile(boot, 97.5))}
    changes = {name: change(name) for name in res}

    fig, axes = plt.subplots(1, 3, figsize=(12, 5.4))
    x = np.arange(len(BIN_LABELS))
    panels = [
        (axes[0], ["PPDA", "PPDA without fouls"], [ACCENT, "#9AA5B8"], "Passes allowed per defensive action",
         "lower = more defending per opponent pass", "{:.0f}"),
        (axes[1], ["High pressures per 15 min"], [ACCENT], "Pressures in the opponent's half",
         "per team, per 15 minutes of play", "{:.1f}"),
        (axes[2], ["Opponent build-up passes per 15 min"], [SECOND], "Why PPDA misleads: less build-up",
         "opponent passes in their own 60%, per 15 minutes", "{:.0f}"),
    ]
    for ax, names, colours, title, sub, fmt in panels:
        style_axes(ax)
        for name, colour in zip(names, colours):
            d = res[name]
            ax.errorbar(x, d["estimate"], yerr=[d["estimate"] - d["low"], d["high"] - d["estimate"]],
                        fmt="o-", color=colour, ecolor=colour, elinewidth=1.1, capsize=3.5, markersize=6,
                        linewidth=2, label=name)
            for xi, v in zip(x, d["estimate"]):
                ax.annotate(fmt.format(v), (xi, v), textcoords="offset points", xytext=(7, 5),
                            fontsize=8, color=TEXT)
        ax.axvline(2.5, color=GRID, linewidth=1, linestyle="--")
        ax.set_xticks(x, BIN_LABELS, fontsize=8.5)
        ax.set_xlabel("Match minute", fontsize=9, color=MUTED)
        ax.set_title(title, fontsize=10.5, color=TEXT, loc="left", pad=18)
        ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=8.5, color=MUTED)
        ax.margins(y=0.18)
        if len(names) > 1:
            ax.legend(frameon=False, fontsize=8, loc="lower left")
    fig.subplots_adjust(left=0.05, right=0.98, top=0.76, bottom=0.25, wspace=0.25)
    hp, bu = changes["High pressures per 15 min"], changes["Opponent build-up passes per 15 min"]
    finish(fig, "Q1. Teams press about a third less in the last 15 minutes",
           f"Pressures in the opponent's half {hp['estimate']:+.0f}% vs the first 15 minutes "
           f"(95% CI {hp['low']:+.0f}% to {hp['high']:+.0f}%). PPDA hides this: opponents' build-up passes also fall "
           f"{abs(bu['estimate']):.0f}%",
           OUT_DIR / "q1_pressing_by_minute.png",
           "Regular time only, 51 matches; stoppage time joins the last bin of each half and rates allow for its length. "
           "Dashed line = half-time.\nWhiskers = 95% bootstrap interval, resampling matches.")

    return {name: {k: {c: round(float(v), 2) for c, v in r.items()} for k, r in d.iterrows()}
            for name, d in res.items()} | {
        "change_last15_vs_first15_pct": {n: {k: round(v, 1) for k, v in c.items()} for n, c in changes.items()}}


# --------------------------------------------------------------------------------------
# Q2: game state
# --------------------------------------------------------------------------------------

Q2_SQL = """
SELECT match_id, period, minute, second, "index", team, type, x,
       duel_type, shot_type, shot_outcome, shot_statsbomb_xg
FROM events
WHERE period < 5
ORDER BY match_id, period, "index"
"""


def game_state_events(con) -> pd.DataFrame:
    """Add each event team's game state (leading / level / trailing) just before the event."""
    ev = con.execute(Q2_SQL).df()
    teams = con.execute("SELECT match_id, home_team, away_team FROM matches").df()
    ev = ev.merge(teams, on="match_id")

    goal_for = (ev["type"] == "Shot") & (ev["shot_outcome"] == "Goal")
    own_goal = ev["type"] == "Own Goal For"            # recorded against the team that benefits
    scorer = ev["team"].where(goal_for | own_goal)
    ev["home_goal"] = (scorer == ev["home_team"]).astype(int)
    ev["away_goal"] = (scorer == ev["away_team"]).astype(int)

    g = ev.groupby("match_id")
    # score BEFORE the event: cumulative goals minus the event's own goal
    ev["home_score"] = g["home_goal"].cumsum() - ev["home_goal"]
    ev["away_score"] = g["away_goal"].cumsum() - ev["away_goal"]
    diff = np.where(ev["team"] == ev["home_team"], ev["home_score"] - ev["away_score"],
                    ev["away_score"] - ev["home_score"])
    ev["state"] = np.select([diff > 0, diff < 0], ["Leading", "Trailing"], default="Level")

    # Time spent in each state: seconds until the next event in the same match and period
    ev["t"] = ev["minute"] * 60 + ev["second"]
    ev["dt"] = (ev.groupby(["match_id", "period"])["t"].shift(-1) - ev["t"]).fillna(0).clip(lower=0)
    return ev


def q2(con) -> dict:
    ev = game_state_events(con)

    # The clock: minutes each team spent in each state (from the home team's view, mirrored for away)
    home_state = np.select([ev["home_score"] > ev["away_score"], ev["home_score"] < ev["away_score"]],
                           ["Leading", "Trailing"], default="Level")
    mirror = {"Leading": "Trailing", "Trailing": "Leading", "Level": "Level"}
    clock = pd.concat([
        pd.DataFrame({"match_id": ev["match_id"], "state": home_state, "dt": ev["dt"]}),
        pd.DataFrame({"match_id": ev["match_id"], "state": pd.Series(home_state).map(mirror), "dt": ev["dt"]}),
    ]).groupby(["match_id", "state"])["dt"].sum().div(60).rename("minutes")

    passes = ev[ev["type"] == "Pass"]
    shots = ev[(ev["type"] == "Shot") & (ev["shot_type"] != "Penalty")]

    # Possession share in a state = team's passes / (team's + opponent's passes while the team is in that state)
    own = passes.groupby(["match_id", "state"]).size().rename("own_passes")
    opp = passes.assign(state=passes["state"].map(mirror)).groupby(["match_id", "state"]).size().rename("opp_passes")

    # PPDA by state: opponent passes in own 60% vs this team's defensive actions in attacking 60%
    defensive = ev[(ev["x"] >= 48) & (ev["type"].isin(["Interception", "Foul Committed"])
                                      | ((ev["type"] == "Duel") & (ev["duel_type"] == "Tackle")))]
    def_actions = defensive.groupby(["match_id", "state"]).size().rename("def_actions")
    opp_deep = passes[passes["x"] <= 72].assign(state=lambda d: d["state"].map(mirror)) \
        .groupby(["match_id", "state"]).size().rename("opp_deep_passes")

    npxg = shots.groupby(["match_id", "state"])["shot_statsbomb_xg"].sum().rename("npxg")
    n_shots = shots.groupby(["match_id", "state"]).size().rename("shots")

    per = pd.concat([clock, own, opp, def_actions, opp_deep, npxg, n_shots], axis=1).fillna(0).reset_index()
    per["all_passes"] = per["own_passes"] + per["opp_passes"]

    order = ["Trailing", "Level", "Leading"]
    res = {
        "Possession": bootstrap_ratio(per, "own_passes", "all_passes", groups="state", scale=100),
        "PPDA": bootstrap_ratio(per, "opp_deep_passes", "def_actions", groups="state"),
        "Shots per 90": bootstrap_ratio(per, "shots", "minutes", groups="state", scale=90),
        "npxG per 90": bootstrap_ratio(per, "npxg", "minutes", groups="state", scale=90),
    }
    minutes = per.groupby("state")["minutes"].sum() / 2  # each team-minute counted once per side

    # Trailing minus leading, with an interval from the same bootstrap resamples
    keys = sorted(per["state"].unique(), key=str)
    diffs = {}
    for name, d in res.items():
        b = d.attrs["boot"]
        boot = b[:, keys.index("Trailing")] - b[:, keys.index("Leading")]
        diffs[name] = {"estimate": float(d.loc["Trailing", "estimate"] - d.loc["Leading", "estimate"]),
                       "low": float(np.percentile(boot, 2.5)), "high": float(np.percentile(boot, 97.5))}

    fig, axes = plt.subplots(1, 4, figsize=(12, 4.8))
    colours = {"Trailing": SECOND, "Level": "#9AA5B8", "Leading": ACCENT}
    for ax, (name, data) in zip(axes, res.items()):
        style_axes(ax)
        data = data.loc[order]
        xs = np.arange(3)
        ax.bar(xs, data["estimate"], color=[colours[s] for s in order], width=0.6, zorder=2)
        ax.errorbar(xs, data["estimate"], yerr=[data["estimate"] - data["low"], data["high"] - data["estimate"]],
                    fmt="none", ecolor=TEXT, elinewidth=1, capsize=4, zorder=3)
        f = "{:.0f}%" if name == "Possession" else "{:.2f}" if "xG" in name else "{:.1f}"
        for xi, v, hi in zip(xs, data["estimate"], data["high"]):
            ax.text(xi, hi, f.format(v), ha="center", va="bottom", fontsize=9, color=TEXT,
                    fontweight="bold")
        ax.set_xticks(xs, order, fontsize=9)
        ax.set_title(name + ("  (lower = more pressing)" if name == "PPDA" else ""), fontsize=10,
                     color=TEXT, loc="left")
        ax.margins(y=0.15)
    fig.subplots_adjust(left=0.05, right=0.98, top=0.76, bottom=0.2, wspace=0.3)
    dp, ds = diffs["Possession"], diffs["Shots per 90"]
    finish(fig, "Q2. Trailing teams have more of the ball and shoot more, but the effect is modest",
           f"Trailing minus leading: possession {dp['estimate']:+.0f} points (95% CI {dp['low']:+.0f} to "
           f"{dp['high']:+.0f}), shots per 90 {ds['estimate']:+.1f} ({ds['low']:+.1f} to {ds['high']:+.1f}). "
           "Both intervals include zero",
           OUT_DIR / "q2_game_state.png",
           f"Game state = scoreline before each event (penalty shoot-outs excluded). Minutes spent leading / level / "
           f"trailing per team: {minutes['Leading']:.0f} / {minutes['Level']:.0f} / {minutes['Trailing']:.0f}.\n"
           "npxG and shots exclude penalties. Whiskers = 95% bootstrap interval, resampling matches.")

    return {name: {s: {c: round(float(v), 3) for c, v in r.items()} for s, r in d.loc[order].iterrows()}
            for name, d in res.items()} | {
        "minutes_by_state": {k: round(float(v)) for k, v in minutes.items()},
        "trailing_minus_leading": {n: {k: round(v, 3) for k, v in d.items()} for n, d in diffs.items()}}


# --------------------------------------------------------------------------------------
# Q3: where possessions start vs what they produce
# --------------------------------------------------------------------------------------

Q3_SQL = """
WITH ev AS (
    SELECT * FROM events WHERE period < 5
),
starts AS (   -- first located event by the team in possession = where it won or started the ball
    SELECT match_id, possession, possession_team, play_pattern,
           ARG_MIN(x, "index") AS start_x
    FROM ev
    WHERE team = possession_team AND x IS NOT NULL
    GROUP BY ALL
),
outcomes AS (
    SELECT match_id, possession,
           COUNT(*) FILTER (WHERE type = 'Shot' AND shot_type <> 'Penalty')                 AS shots,
           COALESCE(SUM(shot_statsbomb_xg) FILTER (WHERE type = 'Shot' AND shot_type <> 'Penalty'), 0) AS npxg
    FROM ev WHERE team = possession_team GROUP BY ALL
)
SELECT s.match_id, s.possession, s.play_pattern, s.start_x,
       CASE WHEN s.start_x < 40 THEN 'Own third' WHEN s.start_x < 80 THEN 'Middle third'
            ELSE 'Final third' END AS zone,
       o.shots, o.npxg, CAST(o.shots > 0 AS INT) AS ended_in_shot, 1 AS possessions
FROM starts s JOIN outcomes o USING (match_id, possession)
WHERE s.play_pattern IN ('Regular Play', 'From Counter')        -- open play only, no set pieces
"""


def q3(con) -> dict:
    df = con.execute(Q3_SQL).df()
    order = ["Own third", "Middle third", "Final third"]
    res = {
        "npxG per possession": bootstrap_ratio(df, "npxg", "possessions", groups="zone"),
        "Possessions ending in a shot": bootstrap_ratio(df, "ended_in_shot", "possessions", groups="zone", scale=100),
        "npxG per shot": bootstrap_ratio(df, "npxg", "shots", groups="zone"),
    }
    counts = df.groupby("zone").agg(possessions=("possessions", "sum"), shots=("shots", "sum")).loc[order]

    xg = res["npxG per possession"].loc[order]
    b = res["npxG per possession"].attrs["boot"]
    keys = sorted(df["zone"].unique(), key=str)
    ratio_boot = b[:, keys.index("Final third")] / b[:, keys.index("Own third")]
    ratio = {"estimate": float(xg.loc["Final third", "estimate"] / xg.loc["Own third", "estimate"]),
             "low": float(np.percentile(ratio_boot, 2.5)), "high": float(np.percentile(ratio_boot, 97.5))}

    fig, axes = plt.subplots(1, 3, figsize=(11.5, 5.3))
    shades = ["#C3CBD9", "#7D8FB3", ACCENT]
    for ax, (name, data) in zip(axes, res.items()):
        style_axes(ax)
        data = data.loc[order]
        xs = np.arange(3)
        ax.bar(xs, data["estimate"], color=shades, width=0.6, zorder=2)
        ax.errorbar(xs, data["estimate"], yerr=[data["estimate"] - data["low"], data["high"] - data["estimate"]],
                    fmt="none", ecolor=TEXT, elinewidth=1, capsize=4, zorder=3)
        f = "{:.0f}%" if "%" in name or "shot" in name and "per" not in name else "{:.3f}"
        if name == "Possessions ending in a shot":
            f = "{:.1f}%"
        for xi, v, hi in zip(xs, data["estimate"], data["high"]):
            ax.text(xi, hi, f.format(v), ha="center", va="bottom", fontsize=9, color=TEXT, fontweight="bold")
        ax.set_xticks(xs, [z.replace(" third", "\nthird") for z in order], fontsize=9)
        ax.set_title(name, fontsize=10.5, color=TEXT, loc="left")
        ax.margins(y=0.15)
    for ax in axes:
        ax.set_xlabel("Where the possession started", fontsize=8.5, color=MUTED)
    fig.subplots_adjust(left=0.06, right=0.98, top=0.78, bottom=0.28, wspace=0.28)
    finish(fig, "Q3. Winning the ball high creates more chances, not better ones",
           f"Open-play possessions starting in the final third produce {ratio['estimate']:.1f}× the npxG of those "
           f"starting in a team's own third (95% CI {ratio['low']:.1f}–{ratio['high']:.1f}×)",
           OUT_DIR / "q3_possession_start.png",
           f"Open play only (StatsBomb 'Regular Play' and 'From Counter'): "
           f"{int(counts['possessions'].sum()):,} possessions, {int(counts['shots'].sum()):,} non-penalty shots. "
           f"Final-third starts: {int(counts.loc['Final third', 'possessions']):,} possessions.\n"
           "Start = first located event by the team in possession. Whiskers = 95% bootstrap interval, resampling matches.")

    return {name: {z: {c: round(float(v), 4) for c, v in r.items()} for z, r in d.loc[order].iterrows()}
            for name, d in res.items()} | {
        "final_vs_own_third_npxg_ratio": {k: round(v, 2) for k, v in ratio.items()},
        "counts": {z: {k: int(v) for k, v in r.items()} for z, r in counts.iterrows()},
    }


# --------------------------------------------------------------------------------------

def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found. Run `python src/pipeline.py` first.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        results = {"q1_pressing_by_minute": q1(con), "q2_game_state": q2(con), "q3_possession_start": q3(con)}
    finally:
        con.close()
    (OUT_DIR / "results.json").write_text(json.dumps(results, indent=2))
    log.info("Saved %s", (OUT_DIR / "results.json").relative_to(ROOT))


if __name__ == "__main__":
    main()
