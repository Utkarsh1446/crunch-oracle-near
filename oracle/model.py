"""Independent Poisson 1X2 model.

Only matches that kicked off before the target are used. The target itself is
excluded. That is the whole leakage control for this open model.

home_xg = league_home_gpg * home_attack * away_defense
away_xg = league_away_gpg * away_attack * home_defense

Attack and defense are goals scored and conceded per game, shrunk toward the
league average with `prior_games` pseudo-games. Home and away rates stay
separate so home advantage is the observed home/away scoring split.
"""

from __future__ import annotations

import math
from typing import Iterable

PRIOR_GAMES = 8.0
MAX_GOALS = 10
MIN_LAMBDA = 0.15
MAX_LAMBDA = 4.5


def poisson_pmf(goals: int, lam: float) -> float:
    return math.exp(-lam) * (lam**goals) / math.factorial(goals)


def to_bps(home: float, draw: float, away: float) -> tuple[int, int, int]:
    total = home + draw + away
    if total <= 0:
        raise ValueError("probabilities must be positive")
    scaled = [part / total * 10000 for part in (home, draw, away)]
    bps = [math.floor(part) for part in scaled]
    remainder = 10000 - sum(bps)
    order = sorted(range(3), key=lambda index: scaled[index] - bps[index], reverse=True)
    for step in range(remainder):
        bps[order[step]] += 1
    return bps[0], bps[1], bps[2]


def outcome_probabilities(home_xg: float, away_xg: float, max_goals: int = MAX_GOALS) -> tuple[float, float, float]:
    home_xg = min(MAX_LAMBDA, max(MIN_LAMBDA, home_xg))
    away_xg = min(MAX_LAMBDA, max(MIN_LAMBDA, away_xg))
    home_mass = [poisson_pmf(goals, home_xg) for goals in range(max_goals + 1)]
    away_mass = [poisson_pmf(goals, away_xg) for goals in range(max_goals + 1)]
    home_scale = sum(home_mass)
    away_scale = sum(away_mass)
    home_mass = [mass / home_scale for mass in home_mass]
    away_mass = [mass / away_scale for mass in away_mass]
    home = draw = away = 0.0
    for home_goals, home_probability in enumerate(home_mass):
        for away_goals, away_probability in enumerate(away_mass):
            probability = home_probability * away_probability
            if home_goals > away_goals:
                home += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away += probability
    return home, draw, away


def _blank():
    return {"games": 0.0, "scored": 0.0, "conceded": 0.0}


def prior_matches(matches: Iterable[dict], kickoff: str, match_id: str) -> list[dict]:
    chosen = []
    for match in matches:
        if not match.get("finished"):
            continue
        if match["id"] == match_id:
            continue
        if match["kickoff"] >= kickoff:
            continue
        chosen.append(match)
    chosen.sort(key=lambda match: (match["kickoff"], match["id"]))
    return chosen


def forecast_match(matches: Iterable[dict], target: dict, prior_games: float = PRIOR_GAMES) -> dict:
    history = prior_matches(matches, target["kickoff"], target["id"])
    if len(history) < 20:
        raise ValueError(f"need at least 20 prior matches, found {len(history)}")
    home_goals = sum(match["home_goals"] for match in history)
    away_goals = sum(match["away_goals"] for match in history)
    count = float(len(history))
    league_home = home_goals / count
    league_away = away_goals / count
    league_average = (league_home + league_away) / 2
    teams: dict[str, dict[str, float]] = {}
    for match in history:
        for name, scored, conceded in (
            (match["home"], match["home_goals"], match["away_goals"]),
            (match["away"], match["away_goals"], match["home_goals"]),
        ):
            row = teams.setdefault(name, _blank())
            row["games"] += 1
            row["scored"] += scored
            row["conceded"] += conceded

    def rates(name: str) -> tuple[float, float]:
        row = teams.get(name, _blank())
        games = row["games"] + prior_games
        attack = ((row["scored"] + prior_games * league_average) / games) / league_average
        defense = ((row["conceded"] + prior_games * league_average) / games) / league_average
        return attack, defense

    home_attack, home_defense = rates(target["home"])
    away_attack, away_defense = rates(target["away"])
    home_xg = league_home * home_attack * away_defense
    away_xg = league_away * away_attack * home_defense
    home, draw, away = outcome_probabilities(home_xg, away_xg)
    home_bps, draw_bps, away_bps = to_bps(home, draw, away)
    return {
        "match_id": target["id"],
        "home": target["home"],
        "away": target["away"],
        "kickoff": target["kickoff"],
        "prior_matches": len(history),
        "home_xg": round(home_xg, 4),
        "away_xg": round(away_xg, 4),
        "home_probability": home,
        "draw_probability": draw,
        "away_probability": away,
        "home_bps": home_bps,
        "draw_bps": draw_bps,
        "away_bps": away_bps,
        "model": "independent-poisson-v1",
    }
