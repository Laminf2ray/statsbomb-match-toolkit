-- Analysis views built on the raw events table.
-- Pitch coordinates: 120 x 80, every team attacks left to right (x = 120 is the opponent's goal line).
-- Penalty shoot-outs (period 5) are excluded throughout.

-- One row per shot
CREATE OR REPLACE VIEW shots AS
SELECT
    match_id,
    period,
    minute,
    team,
    player,
    x,
    y,
    shot_statsbomb_xg            AS xg,
    shot_outcome                 AS outcome,
    shot_outcome = 'Goal'        AS is_goal,
    shot_type,
    shot_body_part               AS body_part
FROM events
WHERE type = 'Shot' AND period < 5;

-- One row per pass, with completion and progression flags
CREATE OR REPLACE VIEW passes AS
SELECT
    match_id,
    period,
    minute,
    team,
    player,
    pass_recipient               AS recipient,
    x, y, pass_end_x, pass_end_y,
    pass_outcome IS NULL         AS is_complete,   -- StatsBomb leaves outcome empty for completed passes
    -- Progressive: completed, outside own defensive third, moves the ball at least 25% closer to goal
    (pass_outcome IS NULL
     AND x >= 40
     AND (120 - pass_end_x) <= 0.75 * (120 - x))  AS is_progressive,
    pass_end_x >= 80             AS into_final_third
FROM events
WHERE type = 'Pass' AND period < 5;

-- Team totals per match: the starting point for match reports and team profiles
CREATE OR REPLACE VIEW team_match_summary AS
WITH shot_totals AS (
    SELECT match_id, team,
           COUNT(*)                        AS shots,
           SUM(CAST(is_goal AS INT))       AS goals,
           ROUND(SUM(xg), 2)               AS xg
    FROM shots GROUP BY match_id, team
),
pass_totals AS (
    SELECT match_id, team,
           COUNT(*)                                     AS passes,
           ROUND(AVG(CAST(is_complete AS INT)), 3)      AS pass_completion,
           SUM(CAST(is_progressive AS INT))             AS progressive_passes,
           SUM(CAST(into_final_third AS INT) * CAST(is_complete AS INT)) AS final_third_passes
    FROM passes GROUP BY match_id, team
)
SELECT
    m.match_id,
    m.match_date,
    m.home_team,
    m.away_team,
    p.team,
    COALESCE(s.shots, 0)  AS shots,
    COALESCE(s.goals, 0)  AS goals,
    COALESCE(s.xg, 0)     AS xg,
    p.passes,
    p.pass_completion,
    p.progressive_passes,
    -- Field tilt: share of the match's completed final-third passes made by this team
    ROUND(p.final_third_passes * 1.0
          / SUM(p.final_third_passes) OVER (PARTITION BY p.match_id), 3) AS field_tilt
FROM pass_totals p
JOIN matches m USING (match_id)
LEFT JOIN shot_totals s USING (match_id, team);
