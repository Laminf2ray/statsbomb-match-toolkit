# StatsBomb Match & Team Analysis Toolkit

A reproducible Python and SQL pipeline for football event data, with automated match reports and team style profiles. It uses StatsBomb Open Data for UEFA Euro 2024 (51 matches, about 188,000 events).

*Independent portfolio project.*

## What it does

| Stage | Status | Output |
|---|---|---|
| 1. Pipeline: download, clean and load event data into DuckDB, then check it | ✅ Done | `data/football.duckdb` |
| 2. Automated post-match report: shot map, xG timeline, pass network, field tilt | ⏳ Next | `outputs/reports/` |
| 3. Team style profiles: pressing, possession, chance quality | ⏳ Planned | `notebooks/` |
| 4. Ad-hoc analysis questions, written up | ⏳ Planned | `docs/` |

## Quick start

```bash
git clone https://github.com/Laminf2ray/statsbomb-match-toolkit.git
cd statsbomb-match-toolkit
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python src/pipeline.py             # all Euro 2024 matches, about 2 minutes on first run
python src/pipeline.py --limit 5   # quick test
```

Downloads are cached in `data/raw/`, so later runs take seconds. To load another competition, find its IDs with `statsbombpy.sb.competitions()` and pass them in, for example `--competition 43 --season 106` for the 2022 World Cup.

## Data model

**Tables**
- `matches`: one row per match (teams, score, date, stage, stadium)
- `events`: one row per on-ball event (passes, shots, carries, pressures and so on), with coordinates flattened into numeric `x`/`y` columns

**Views** (`sql/views.sql`)
- `shots`: shots with xG, outcome, body part and location
- `passes`: passes with completion, **progressive** and **into-final-third** flags
- `team_match_summary`: team totals per match, covering shots, goals, xG, passes, completion, progressive passes and **field tilt**

Coordinates use StatsBomb's 120 × 80 pitch. Every team attacks left to right. Penalty shoot-outs are excluded.

## Data quality checks

Every pipeline run checks that:
- each match has event data
- goals counted from the event data (shots plus own goals) equal the final score for every match

## Example: the Euro 2024 final

```sql
SELECT team, shots, goals, xg, pass_completion, progressive_passes, field_tilt
FROM team_match_summary
WHERE match_date = (SELECT MAX(match_date) FROM matches);
```

| team | shots | goals | xg | pass completion | progressive passes | field tilt |
|---|---|---|---|---|---|---|
| Spain | 16 | 2 | 1.79 | 87.5% | 62 | 71% |
| England | 9 | 1 | 0.73 | 78.6% | 28 | 29% |

## Metric definitions

- **xG (expected goals):** StatsBomb's estimate of the probability that a shot is scored.
- **Progressive pass:** a completed pass, starting outside the team's own defensive third, that moves the ball at least 25% closer to the opponent's goal line.
- **Field tilt:** a team's share of the completed final-third passes in a match, used as a measure of territorial dominance.

## Project structure

```
statsbomb-match-toolkit/
├── src/pipeline.py      # ingestion, cleaning, checks, load
├── sql/views.sql        # analysis views
├── notebooks/           # exploration and team profiles
├── docs/                # write-ups and example reports
├── data/                # created by the pipeline, not committed
├── requirements.txt
└── README.md
```

## Data source

Data provided by [StatsBomb Open Data](https://github.com/statsbomb/open-data). This project follows StatsBomb's open data terms: please credit StatsBomb wherever the data or outputs are published.
