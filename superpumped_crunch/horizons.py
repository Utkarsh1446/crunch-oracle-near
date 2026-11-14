"""Point-in-time score projections and secondary-market research.

The module is deterministic and dependency-free. Every historical row used by
the model must precede the supplied fixture cutoff.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .config import (
    DEFAULT_REGISTRY,
    sport_config,
    sport_for_competition,
)


class _RegistryView(Mapping):
    """Live, read-only mapping over the sport registry.

    Kept for backwards compatibility with the historical module-level dicts
    (``SPORT_CONFIG`` / ``MARKETS_BY_SPORT``). Sports registered at runtime
    appear automatically.
    """

    def __init__(self, field: str | None = None) -> None:
        self._field = field

    def __getitem__(self, sport_id: str) -> Any:
        config = DEFAULT_REGISTRY.sport_config(sport_id)
        return config if self._field is None else config[self._field]

    def __iter__(self):
        return iter(DEFAULT_REGISTRY.list_sports())

    def __len__(self) -> int:
        return len(DEFAULT_REGISTRY.list_sports())


SPORT_CONFIG: Mapping[str, dict[str, Any]] = _RegistryView()
MARKETS_BY_SPORT: Mapping[str, tuple[str, ...]] = _RegistryView("markets")


class _FootballCompetitions:
    """Live view of the competitions claimed by draw-capable sports."""

    def _competitions(self) -> set[str]:
        return {
            competition
            for competition, sport_id in DEFAULT_REGISTRY.competition_map.items()
            if DEFAULT_REGISTRY.sport_config(sport_id)["has_draw"]
        }

    def __contains__(self, competition: object) -> bool:
        return competition in self._competitions()

    def __iter__(self):
        return iter(sorted(self._competitions()))

    def __len__(self) -> int:
        return len(self._competitions())


FOOTBALL_COMPETITIONS = _FootballCompetitions()

MARKET_LABELS = {
    "winner": "Match Winner",
    "total_ultra_safe": "Game Total - Deep Safety Line",
    "total_safe": "Game Total - Safety Line",
    "total_low": "Game Total - Lower Line",
    "total": "Game Total - Main Line",
    "total_high": "Game Total - Higher Line",
    "btts": "Both Teams To Score",
    "home_team_total": "Home Team Total",
    "away_team_total": "Away Team Total",
    "spread": "Spread / Handicap - Main Line",
    "spread_alt": "Spread / Handicap - Alternate Line",
    "spread_safe": "Spread / Handicap - Safety Line",
    "both_teams_threshold": "Both Teams Scoring Threshold",
}


def total_line_for_market(sport: str, market: str, base_line: float | None = None) -> float:
    config = sport_config(sport)
    line = float(config["total_line"] if base_line is None else base_line)
    if market == "total_ultra_safe":
        return float(config["ultra_safe_total_line"])
    if market == "total_safe":
        return float(config["safe_total_line"])
    delta = float(config["alternate_delta"])
    if market == "total_low":
        return max(0.5, line - delta)
    if market == "total_high":
        return line + delta
    return line


def spread_magnitude_for_market(sport: str, market: str) -> float:
    config = sport_config(sport)
    magnitude = float(config["spread_line"])
    if market == "spread_safe":
        return float(config["safe_spread_line"])
    if market == "spread_alt":
        magnitude += float(config["spread_alternate_delta"])
    return magnitude


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _name_tokens(value: Any) -> set[str]:
    raw = "".join(
        character for character in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(character)
    ).lower()
    tokens = {"".join(character for character in token if character.isalnum()) for token in raw.split()}
    generic = {"fc", "cf", "afc", "ac", "sc", "sv", "club", "de", "the"}
    return {token for token in tokens if token and token not in generic}


def _same_team(left: Any, right: Any) -> bool:
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    shared = left_tokens & right_tokens
    shorter = min(len(left_tokens), len(right_tokens))
    return len(shared) >= 2 or (shorter == 1 and len(shared) == 1)


def _score(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _mean(values: list[float], fallback: float) -> float:
    return sum(values) / len(values) if values else fallback


def _shrunk_mean(values: list[float], fallback: float, prior_games: float = 6.0) -> float:
    if not values:
        return fallback
    weight = len(values) / (len(values) + prior_games)
    return weight * _mean(values, fallback) + (1.0 - weight) * fallback


def _team_form(games: list[dict[str, Any]], team_name: str, window: int) -> dict[str, Any]:
    rows: list[tuple[float, float]] = []
    for game in reversed(games):
        home_score = _score(game.get("home_score"))
        away_score = _score(game.get("away_score"))
        if home_score is None or away_score is None:
            continue
        if _same_team(game.get("home_team"), team_name):
            rows.append((home_score, away_score))
        elif _same_team(game.get("away_team"), team_name):
            rows.append((away_score, home_score))
        if len(rows) >= window:
            break
    return {
        "games": len(rows),
        "scored": [row[0] for row in rows],
        "allowed": [row[1] for row in rows],
    }


def _score_projection(
    competition: str,
    home_name: str,
    away_name: str,
    outcome_probabilities: dict[str, float],
    historical_games: list[dict[str, Any]],
    as_of: str | None,
) -> dict[str, Any]:
    sport = sport_for_competition(competition)
    config = sport_config(sport)
    cutoff = _timestamp(as_of) or datetime.now(timezone.utc)
    eligible = []
    for game in historical_games:
        game_time = _timestamp(game.get("start_time"))
        if (
            game.get("competition") == competition
            and game.get("verified") is True
            and game_time is not None
            and game_time < cutoff
            and _score(game.get("home_score")) is not None
            and _score(game.get("away_score")) is not None
        ):
            eligible.append(game)
    eligible.sort(key=lambda row: (str(row.get("start_time") or ""), str(row.get("game_id") or "")))

    league_home_scores = [float(game["home_score"]) for game in eligible]
    league_away_scores = [float(game["away_score"]) for game in eligible]
    league_home = _mean(league_home_scores, float(config["fallback_home"]))
    league_away = _mean(league_away_scores, float(config["fallback_away"]))
    league_team = max((league_home + league_away) / 2.0, 0.05)

    home_form = _team_form(eligible, home_name, int(config["recent_games"]))
    away_form = _team_form(eligible, away_name, int(config["recent_games"]))
    home_attack = _shrunk_mean(home_form["scored"], league_team)
    home_defence = _shrunk_mean(home_form["allowed"], league_team)
    away_attack = _shrunk_mean(away_form["scored"], league_team)
    away_defence = _shrunk_mean(away_form["allowed"], league_team)

    raw_home = math.sqrt(max(home_attack * away_defence, 0.001)) * (league_home / league_team)
    raw_away = math.sqrt(max(away_attack * home_defence, 0.001)) * (league_away / league_team)
    projected_total = min(max(raw_home + raw_away, float(config["minimum_score"]) * 2), float(config["maximum_total"]))

    home_probability = max(float(outcome_probabilities.get("home", 0.0)), 0.0)
    away_probability = max(float(outcome_probabilities.get("away", 0.0)), 0.0)
    if config["has_draw"]:
        probability_margin = home_probability - away_probability
    else:
        decisive_total = max(home_probability + away_probability, 0.0001)
        probability_margin = (home_probability - away_probability) / decisive_total
    model_margin = probability_margin * float(config["margin_scale"])
    projected_margin = 0.65 * (raw_home - raw_away) + 0.35 * model_margin
    margin_limit = max(projected_total - float(config["minimum_score"]) * 2, 0.0)
    projected_margin = min(max(projected_margin, -margin_limit), margin_limit)
    projected_home = max((projected_total + projected_margin) / 2.0, float(config["minimum_score"]))
    projected_away = max((projected_total - projected_margin) / 2.0, float(config["minimum_score"]))

    return {
        "sport": sport,
        "config": config,
        "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "games": eligible,
        "max_game_time": max((str(game.get("start_time")) for game in eligible), default=None),
        "league_games": len(eligible),
        "home_team_games": home_form["games"],
        "away_team_games": away_form["games"],
        "league_home_average": league_home,
        "league_away_average": league_away,
        "home": projected_home,
        "away": projected_away,
        "total": projected_home + projected_away,
        "margin": projected_home - projected_away,
    }


def _poisson_probabilities(mean: float, cap: int) -> list[float]:
    probabilities = [math.exp(-mean)]
    for value in range(1, cap + 1):
        probabilities.append(probabilities[-1] * mean / value)
    return probabilities


def _poisson_over(mean: float, line: float, cap: int) -> float:
    probabilities = _poisson_probabilities(mean, cap)
    under_or_equal = sum(probabilities[: math.floor(line) + 1])
    return min(max(1.0 - under_or_equal, 0.0), 1.0)


def _poisson_at_least(mean: float, threshold: float, cap: int) -> float:
    minimum = math.floor(threshold) + 1
    probabilities = _poisson_probabilities(mean, cap)
    return min(max(1.0 - sum(probabilities[:minimum]), 0.0), 1.0)


def _normal_cdf(value: float, mean: float, standard_deviation: float) -> float:
    z_score = (value - mean) / max(standard_deviation, 0.0001)
    return 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))


def _total_over_probability(projection: dict[str, Any], line: float) -> float:
    config = projection["config"]
    if config["distribution"] == "poisson":
        return _poisson_over(projection["total"], line, int(config["score_cap"]) * 2)
    return 1.0 - _normal_cdf(line, projection["total"], float(config["total_sd"]))


def _team_over_probability(projection: dict[str, Any], side: str, line: float) -> float:
    config = projection["config"]
    mean = float(projection[side])
    if config["distribution"] == "poisson":
        return _poisson_at_least(mean, line, int(config["score_cap"]))
    return 1.0 - _normal_cdf(line, mean, float(config["team_sd"]))


def _home_cover_probability(projection: dict[str, Any], home_handicap: float) -> float:
    config = projection["config"]
    threshold = -home_handicap
    if config["distribution"] == "normal":
        return 1.0 - _normal_cdf(threshold, projection["margin"], float(config["margin_sd"]))
    cap = int(config["score_cap"])
    home_probabilities = _poisson_probabilities(projection["home"], cap)
    away_probabilities = _poisson_probabilities(projection["away"], cap)
    covered = 0.0
    represented = 0.0
    for home_score, home_probability in enumerate(home_probabilities):
        for away_score, away_probability in enumerate(away_probabilities):
            joint = home_probability * away_probability
            represented += joint
            if (home_score - away_score) + home_handicap > 0:
                covered += joint
    return min(max(covered / max(represented, 0.0001), 0.0), 1.0)


def _winner_probabilities(projection: dict[str, Any]) -> dict[str, float]:
    config = projection["config"]
    if config["distribution"] == "normal":
        home = 1.0 - _normal_cdf(0.0, projection["margin"], float(config["margin_sd"]))
        return {"home": home, "draw": 0.0, "away": 1.0 - home}

    cap = int(config["score_cap"])
    home_scores = _poisson_probabilities(projection["home"], cap)
    away_scores = _poisson_probabilities(projection["away"], cap)
    outcomes = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for home_score, home_probability in enumerate(home_scores):
        for away_score, away_probability in enumerate(away_scores):
            outcome = "home" if home_score > away_score else "away" if home_score < away_score else "draw"
            outcomes[outcome] += home_probability * away_probability
    if not config["has_draw"]:
        decisive = max(outcomes["home"] + outcomes["away"], 0.0001)
        return {"home": outcomes["home"] / decisive, "draw": 0.0, "away": outcomes["away"] / decisive}
    represented = max(sum(outcomes.values()), 0.0001)
    return {key: value / represented for key, value in outcomes.items()}


def _signed_line(value: float) -> str:
    return f"{value:+.1f}"


def _binary_market(
    key: str,
    label: str,
    line: float | None,
    selections: list[tuple[str, str, float]],
    verified_market: dict[str, Any] | None = None,
    alternate_lines: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    rendered_selections = []
    for selection_key, selection_label, probability in selections:
        probability = min(max(probability, 0.0), 1.0)
        rendered_selections.append({
            "key": selection_key,
            "label": selection_label,
            "probability": round(probability, 6),
        })
    lean = max(rendered_selections, key=lambda row: row["probability"])
    confidence = "high" if lean["probability"] >= 0.70 else "medium" if lean["probability"] >= 0.58 else "balanced"
    return {
        "key": key,
        "label": label,
        "line": round(line, 3) if line is not None else None,
        "status": "sports_model_line",
        "source": "provider_verified_sports_history",
        "selections": rendered_selections,
        "lean": {
            "selection_key": lean["key"],
            "selection": lean["label"],
            "probability": lean["probability"],
            "confidence": confidence,
            "action": "withheld",
            "guardrails": ["requires_dedicated_walk_forward_validation"],
        },
        "alternate_lines": alternate_lines or [],
    }


def _top_scorelines(projection: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    config = projection["config"]
    if config["distribution"] != "poisson":
        return []
    cap = min(int(config["score_cap"]), 10)
    home_probabilities = _poisson_probabilities(projection["home"], cap)
    away_probabilities = _poisson_probabilities(projection["away"], cap)
    rows = [
        {"score": f"{home_score}-{away_score}", "probability": round(home_probability * away_probability, 6)}
        for home_score, home_probability in enumerate(home_probabilities)
        for away_score, away_probability in enumerate(away_probabilities)
    ]
    return sorted(rows, key=lambda row: row["probability"], reverse=True)[:limit]


def build_market_horizons(
    competition: str,
    home_name: str,
    away_name: str,
    outcome_probabilities: dict[str, float],
    historical_games: list[dict[str, Any]],
    as_of: str | None = None,
    verified_markets: dict[str, dict[str, Any]] | None = None,
    tuned_parameters: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build sport-specific research markets from pre-cutoff scores only."""
    # The argument remains for API compatibility, but prices are intentionally
    # ignored. These projections are derived only from sports results/features.
    verified_markets = {}
    tuned_parameters = tuned_parameters or {}
    projection = _score_projection(
        competition,
        home_name,
        away_name,
        outcome_probabilities,
        historical_games,
        as_of,
    )
    config = projection["config"]
    sport = projection["sport"]

    def tuned_projection(market_key: str) -> dict[str, Any]:
        tuned = tuned_parameters.get(market_key) or {}
        adjusted = dict(projection)
        if market_key == "winner" or market_key.startswith("spread"):
            margin = projection["margin"] * float(tuned.get("margin_scale", 1.0))
            adjusted["home"] = max((projection["total"] + margin) / 2.0, float(config["minimum_score"]))
            adjusted["away"] = max((projection["total"] - margin) / 2.0, float(config["minimum_score"]))
        else:
            score_adjustment = float(tuned.get("score_adjustment", 0.0)) / 2.0
            adjusted["home"] = max(projection["home"] + score_adjustment, float(config["minimum_score"]))
            adjusted["away"] = max(projection["away"] + score_adjustment, float(config["minimum_score"]))
        adjusted["total"] = adjusted["home"] + adjusted["away"]
        adjusted["margin"] = adjusted["home"] - adjusted["away"]
        return adjusted

    winner_projection = tuned_projection("winner")
    winner_probabilities = _winner_probabilities(winner_projection)
    winner_selections = [
        ("home", f"{home_name} to win", winner_probabilities["home"]),
        ("away", f"{away_name} to win", winner_probabilities["away"]),
    ]
    if config["has_draw"]:
        winner_selections.insert(1, ("draw", "Draw", winner_probabilities["draw"]))
    markets = [_binary_market(
        "winner",
        "Match Winner",
        None,
        winner_selections,
        verified_markets.get("winner"),
    )]

    total_verified = verified_markets.get("total") or {}
    base_total_line = float(total_verified.get("line", config["total_line"]))
    total_label = str(config["total_label"])
    total_markets = [("total_safe", "Safety"), ("total_low", "Lower"), ("total", "Main"), ("total_high", "Higher")]
    if "total_ultra_safe" in config["markets"]:
        total_markets.insert(0, ("total_ultra_safe", "Deep Safety"))
    for total_key, line_suffix in total_markets:
        total_line = total_line_for_market(sport, total_key, base_total_line)
        total_projection = tuned_projection(total_key)
        total_over = _total_over_probability(total_projection, total_line)
        markets.append(_binary_market(
            total_key,
            f"{total_label} - {line_suffix} Line",
            total_line,
            [
                ("over", f"Over {total_line:.1f}", total_over),
                ("under", f"Under {total_line:.1f}", 1.0 - total_over),
            ],
            verified_markets.get(total_key) or (total_verified if total_key == "total" else None),
        ))

    spread_verified = verified_markets.get("spread") or {}
    spread_projection = tuned_projection("spread")
    if spread_verified.get("line") is not None:
        base_home_handicap = float(spread_verified["line"])
    else:
        base_magnitude = spread_magnitude_for_market(sport, "spread")
        base_home_handicap = -base_magnitude if spread_projection["margin"] >= 0 else base_magnitude
    spread_sign = -1.0 if base_home_handicap < 0 else 1.0

    def append_spread(market_key: str, label_suffix: str) -> None:
        projection_for_spread = tuned_projection(market_key)
        magnitude = abs(base_home_handicap) if market_key == "spread" else spread_magnitude_for_market(sport, market_key)
        home_handicap = spread_sign * magnitude
        home_cover = _home_cover_probability(projection_for_spread, home_handicap)
        markets.append(_binary_market(
            market_key,
            f"{config['spread_label']} - {label_suffix} Line",
            home_handicap,
            [
                ("home", f"{home_name} {_signed_line(home_handicap)}", home_cover),
                ("away", f"{away_name} {_signed_line(-home_handicap)}", 1.0 - home_cover),
            ],
            verified_markets.get(market_key) or (spread_verified if market_key == "spread" else None),
        ))

    append_spread("spread", "Main")
    append_spread("spread_alt", "Alternate")
    append_spread("spread_safe", "Safety")

    if config["market_family"] == "team_totals":
        cap = int(config["score_cap"])
        btts_projection = tuned_projection("btts")
        btts_yes = _poisson_at_least(btts_projection["home"], 0.5, cap) * _poisson_at_least(btts_projection["away"], 0.5, cap)
        markets.append(_binary_market(
            "btts",
            "Both Teams To Score",
            None,
            [("yes", "Yes", btts_yes), ("no", "No", 1.0 - btts_yes)],
            verified_markets.get("btts"),
        ))
        team_total_lines = config["team_total_lines"]
        for side in ("home", "away"):
            line = float(team_total_lines[side])
            market_key = f"{side}_team_total"
            over = _team_over_probability(tuned_projection(market_key), side, line)
            markets.append(_binary_market(
                market_key,
                f"{home_name if side == 'home' else away_name} Team Total",
                line,
                [("over", f"Over {line:.1f}", over), ("under", f"Under {line:.1f}", 1.0 - over)],
                verified_markets.get(f"{side}_team_total"),
            ))
    else:
        threshold = float(config["team_threshold"])
        threshold_projection = tuned_projection("both_teams_threshold")
        both_probability = _team_over_probability(threshold_projection, "home", threshold) * _team_over_probability(threshold_projection, "away", threshold)
        markets.append(_binary_market(
            "both_teams_threshold",
            str(config["team_threshold_label"]),
            threshold,
            [("yes", "Yes", both_probability), ("no", "No", 1.0 - both_probability)],
        ))

    for market in markets:
        validation = tuned_parameters.get(market["key"])
        if not validation:
            continue
        threshold = float(validation.get("confidence_threshold", 1.0))
        deployment_enabled = bool(validation.get("deployment_enabled", validation.get("validated")))
        qualified = bool(validation.get("validated")) and deployment_enabled and market["lean"]["probability"] >= threshold
        market["validation"] = {
            "validated": bool(validation.get("validated")),
            "deployment_enabled": deployment_enabled,
            "training_target_reached": bool(validation.get("training_target_reached")),
            "training_accuracy": round(float(validation.get("training_accuracy", 0.0)) * 100.0, 2),
            "training_predictions": int(validation.get("training_predictions", 0)),
            "validation_accuracy": round(float(validation.get("validation_accuracy", validation.get("holdout_accuracy", 0.0))) * 100.0, 2),
            "validation_predictions": int(validation.get("validation_predictions", validation.get("holdout_predictions", 0))),
            "final_test_accuracy": round(float(validation.get("holdout_accuracy", 0.0)) * 100.0, 2),
            "final_test_predictions": int(validation.get("holdout_predictions", 0)),
            "confidence_threshold": threshold,
        }
        market["lean"]["action"] = "qualified_prediction" if qualified else "withheld"
        market["lean"]["guardrails"] = [] if qualified else [
            "analysis_not_validated_above_target" if not validation.get("validated")
            else "prediction_type_paused_by_forward_monitoring" if not deployment_enabled
            else "confidence_below_frozen_threshold"
        ]

    return {
        "model_version": "historical_score_horizons_v1",
        "sport": sport,
        "sport_label": config["label"],
        "teams": {"home": home_name, "away": away_name},
        "source": "provider_verified_completed_games",
        "as_of": projection["cutoff"],
        "no_lookahead": (
            projection["max_game_time"] is None
            or (_timestamp(projection["max_game_time"]) or datetime.min.replace(tzinfo=timezone.utc))
            < (_timestamp(projection["cutoff"]) or datetime.max.replace(tzinfo=timezone.utc))
        ),
        "latest_feature_game_utc": projection["max_game_time"],
        "sample": {
            "league_games": projection["league_games"],
            "home_team_games": projection["home_team_games"],
            "away_team_games": projection["away_team_games"],
        },
        "league_scoring": {
            "home_average": round(projection["league_home_average"], 3),
            "away_average": round(projection["league_away_average"], 3),
        },
        "projection": {
            "home": round(projection["home"], 2),
            "away": round(projection["away"], 2),
            "total": round(projection["total"], 2),
            "margin": round(projection["margin"], 2),
            "unit": config["unit"],
        },
        "markets": markets,
        "top_scorelines": _top_scorelines(projection),
        "recommendation_policy": "qualified_only_when_training_tuning_and_chronological_validation_pass_before_final_test",
        "minimum_certified_win_rate": 0.80,
        "uncertified_action": "withheld",
        "analysis_policy": {
            "mode": "sports_only",
            "moneyline_used": False,
            "odds_used": False,
            "trading_factors_used": False,
        },
    }
