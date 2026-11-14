"""Leak-free walk-forward validation for sport-specific secondary markets."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timezone
from itertools import groupby
from typing import Any

from .config import sport_config, sport_for_competition
from .horizons import (
    MARKET_LABELS,
    MARKETS_BY_SPORT,
    SPORT_CONFIG,
    _RegistryView,
    spread_magnitude_for_market,
    total_line_for_market,
)


TARGET_ACCURACY = 0.80
TARGET_COVERAGE = 0.20
WINNER_TARGET_COVERAGE = 0.20
TRAINING_FRACTION = 0.60
VALIDATION_FRACTION = 0.15
CERTIFICATION_MIN_PICKS = 20
CONFIDENCE_THRESHOLDS = tuple(round(0.50 + step * 0.025, 3) for step in range(20)) + (0.99,)

#: Backwards-compatible live view; values come from each sport's JSON config.
SCORE_ADJUSTMENTS = _RegistryView("score_adjustments")

def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _score(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _mean(values: deque[float], fallback: float) -> float:
    return sum(values) / len(values) if values else fallback


def _shrunk(values: deque[float], fallback: float, prior: float = 6.0) -> float:
    if not values:
        return fallback
    weight = len(values) / (len(values) + prior)
    return weight * _mean(values, fallback) + (1.0 - weight) * fallback


def _poisson_probabilities(mean: float, cap: int) -> list[float]:
    probabilities = [math.exp(-max(mean, 0.001))]
    for value in range(1, cap + 1):
        probabilities.append(probabilities[-1] * mean / value)
    return probabilities


def _poisson_over(mean: float, line: float, cap: int) -> float:
    probabilities = _poisson_probabilities(mean, cap)
    return min(max(1.0 - sum(probabilities[: math.floor(line) + 1]), 0.0), 1.0)


def _poisson_at_least(mean: float, line: float, cap: int) -> float:
    probabilities = _poisson_probabilities(mean, cap)
    return min(max(1.0 - sum(probabilities[: math.floor(line) + 1]), 0.0), 1.0)


def _normal_cdf(value: float, mean: float, standard_deviation: float) -> float:
    z_score = (value - mean) / max(standard_deviation, 0.0001)
    return 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))


def _over_probability(feature: dict[str, Any], mean: float, line: float, total: bool = True) -> float:
    config = feature["config"]
    if config["distribution"] == "poisson":
        cap = int(config["score_cap"]) * (2 if total else 1)
        return _poisson_over(mean, line, cap)
    deviation = float(config["total_sd"] if total else config["team_sd"])
    return 1.0 - _normal_cdf(line, mean, deviation)


def _cover_probability(feature: dict[str, Any], home: float, away: float, home_handicap: float) -> float:
    config = feature["config"]
    threshold = -home_handicap
    if config["distribution"] == "normal":
        return 1.0 - _normal_cdf(threshold, home - away, float(config["margin_sd"]))
    cap = int(config["score_cap"])
    home_probabilities = _poisson_probabilities(home, cap)
    away_probabilities = _poisson_probabilities(away, cap)
    represented = 0.0
    covered = 0.0
    for home_score, home_probability in enumerate(home_probabilities):
        for away_score, away_probability in enumerate(away_probabilities):
            joint = home_probability * away_probability
            represented += joint
            if home_score - away_score + home_handicap > 0:
                covered += joint
    return min(max(covered / max(represented, 0.0001), 0.0), 1.0)


def _winner_probabilities(feature: dict[str, Any], home: float, away: float) -> dict[str, float]:
    config = feature["config"]
    if config["distribution"] == "normal":
        home_probability = 1.0 - _normal_cdf(0.0, home - away, float(config["margin_sd"]))
        return {"home": home_probability, "draw": 0.0, "away": 1.0 - home_probability}

    cap = int(config["score_cap"])
    home_scores = _poisson_probabilities(home, cap)
    away_scores = _poisson_probabilities(away, cap)
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


def _game_features(competition: str, games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sport = sport_for_competition(competition)
    config = sport_config(sport)
    recent_window = int(config["recent_games"])
    team_scored: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=recent_window))
    team_allowed: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=recent_window))
    ratings: dict[str, float] = defaultdict(lambda: 1500.0)
    league_count = 0
    league_home_sum = 0.0
    league_away_sum = 0.0
    latest_feature_game: str | None = None
    features = []

    for batch_time, batch_iterator in groupby(games, key=lambda row: str(row.get("start_time") or "")):
        pending_updates = []
        for game in list(batch_iterator):
            home_score = _score(game.get("home_score"))
            away_score = _score(game.get("away_score"))
            if home_score is None or away_score is None:
                continue
            home_id = str(game.get("home_team_id") or game.get("home_team") or "")
            away_id = str(game.get("away_team_id") or game.get("away_team") or "")
            if not home_id or not away_id:
                continue

            league_home = league_home_sum / league_count if league_count else float(config["fallback_home"])
            league_away = league_away_sum / league_count if league_count else float(config["fallback_away"])
            league_team = max((league_home + league_away) / 2.0, 0.05)
            home_attack = _shrunk(team_scored[home_id], league_team)
            home_defence = _shrunk(team_allowed[home_id], league_team)
            away_attack = _shrunk(team_scored[away_id], league_team)
            away_defence = _shrunk(team_allowed[away_id], league_team)
            raw_home = math.sqrt(max(home_attack * away_defence, 0.001)) * (league_home / league_team)
            raw_away = math.sqrt(max(away_attack * home_defence, 0.001)) * (league_away / league_team)
            projected_total = min(max(raw_home + raw_away, float(config["minimum_score"]) * 2), float(config["maximum_total"]))

            elo_diff = ratings[home_id] - ratings[away_id] + 65.0
            home_win_probability = 1.0 / (1.0 + math.pow(10.0, -elo_diff / 400.0))
            probability_margin = 2.0 * home_win_probability - 1.0
            model_margin = probability_margin * float(config["margin_scale"])
            projected_margin = 0.65 * (raw_home - raw_away) + 0.35 * model_margin
            margin_limit = max(projected_total - float(config["minimum_score"]) * 2, 0.0)
            projected_margin = min(max(projected_margin, -margin_limit), margin_limit)
            projected_home = max((projected_total + projected_margin) / 2.0, float(config["minimum_score"]))
            projected_away = max((projected_total - projected_margin) / 2.0, float(config["minimum_score"]))

            features.append({
                "game_id": game.get("game_id"),
                "provider_event_id": game.get("provider_event_id"),
                "competition": competition,
                "sport": sport,
                "start_time": game.get("start_time"),
                "latest_feature_game_utc": latest_feature_game,
                "prior_games": league_count,
                "home_team": game.get("home_team"),
                "away_team": game.get("away_team"),
                "actual_home": home_score,
                "actual_away": away_score,
                "home": projected_home,
                "away": projected_away,
                "total": projected_home + projected_away,
                "margin": projected_home - projected_away,
                "config": config,
            })
            pending_updates.append((home_id, away_id, home_score, away_score, home_win_probability))

        # A shared kickoff batch is fully predicted before any result in it updates state.
        for home_id, away_id, home_score, away_score, home_win_probability in pending_updates:
            actual_result = 1.0 if home_score > away_score else 0.0 if home_score < away_score else 0.5
            rating_delta = 24.0 * (actual_result - home_win_probability)
            ratings[home_id] += rating_delta
            ratings[away_id] -= rating_delta
            team_scored[home_id].append(home_score)
            team_allowed[home_id].append(away_score)
            team_scored[away_id].append(away_score)
            team_allowed[away_id].append(home_score)
            league_count += 1
            league_home_sum += home_score
            league_away_sum += away_score
        if pending_updates:
            latest_feature_game = batch_time or latest_feature_game
    return features


def chronological_features(historical_games: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for game in historical_games:
        if game.get("verified") is not True or not game.get("competition") or not _timestamp(game.get("start_time")):
            continue
        if _score(game.get("home_score")) is None or _score(game.get("away_score")) is None:
            continue
        grouped[str(game["competition"])].append(game)
    return {
        competition: _game_features(
            competition,
            sorted(games, key=lambda row: (str(row.get("start_time") or ""), str(row.get("game_id") or ""))),
        )
        for competition, games in grouped.items()
    }


def _candidate_parameters(sport: str, market: str) -> list[dict[str, float]]:
    if market == "winner" or market.startswith("spread"):
        return [{"margin_scale": scale} for scale in (0.40, 0.60, 0.80, 1.0, 1.20, 1.40, 1.60, 1.80)]
    return [{"score_adjustment": adjustment} for adjustment in sport_config(sport)["score_adjustments"]]


def evaluate_market(feature: dict[str, Any], market: str, parameters: dict[str, float]) -> dict[str, Any]:
    config = feature["config"]
    actual_home = float(feature["actual_home"])
    actual_away = float(feature["actual_away"])
    adjustment = float(parameters.get("score_adjustment", 0.0))
    home = max(float(feature["home"]) + adjustment / 2.0, float(config["minimum_score"]))
    away = max(float(feature["away"]) + adjustment / 2.0, float(config["minimum_score"]))
    line: float | None = None

    if market == "winner":
        margin_scale = float(parameters.get("margin_scale", 1.0))
        adjusted_margin = float(feature["margin"]) * margin_scale
        total = float(feature["total"])
        home = max((total + adjusted_margin) / 2.0, float(config["minimum_score"]))
        away = max((total - adjusted_margin) / 2.0, float(config["minimum_score"]))
        probabilities = _winner_probabilities(feature, home, away)
        supported = ("home", "draw", "away") if config["has_draw"] else ("home", "away")
        selection = max(supported, key=lambda key: probabilities[key])
        actual = "home" if actual_home > actual_away else "away" if actual_home < actual_away else "draw"
        return {
            "selection": selection,
            "confidence": probabilities[selection],
            "correct": selection == actual,
            "line": None,
            "actual": actual,
        }
    if market in {"total_ultra_safe", "total_safe", "total_low", "total", "total_high"}:
        line = total_line_for_market(feature["sport"], market)
        yes_probability = _over_probability(feature, home + away, line)
        yes_key, no_key = "over", "under"
        actual_yes = actual_home + actual_away > line
    elif market == "btts":
        cap = int(config["score_cap"])
        yes_probability = _poisson_at_least(home, 0.5, cap) * _poisson_at_least(away, 0.5, cap)
        yes_key, no_key = "yes", "no"
        actual_yes = actual_home > 0 and actual_away > 0
    elif market in {"home_team_total", "away_team_total"}:
        side = "home" if market.startswith("home") else "away"
        team_total_lines = config.get("team_total_lines") or {"home": 1.5, "away": 0.5}
        line = float(team_total_lines[side])
        mean = home if side == "home" else away
        actual = actual_home if side == "home" else actual_away
        yes_probability = _over_probability(feature, mean, line, total=False)
        yes_key, no_key = "over", "under"
        actual_yes = actual > line
    elif market == "both_teams_threshold":
        line = float(config["team_threshold"])
        home_probability = _over_probability(feature, home, line, total=False)
        away_probability = _over_probability(feature, away, line, total=False)
        yes_probability = home_probability * away_probability
        yes_key, no_key = "yes", "no"
        actual_yes = actual_home > line and actual_away > line
    elif market.startswith("spread"):
        margin_scale = float(parameters.get("margin_scale", 1.0))
        adjusted_margin = float(feature["margin"]) * margin_scale
        total = float(feature["total"])
        home = max((total + adjusted_margin) / 2.0, float(config["minimum_score"]))
        away = max((total - adjusted_margin) / 2.0, float(config["minimum_score"]))
        spread_magnitude = spread_magnitude_for_market(feature["sport"], market)
        home_handicap = -spread_magnitude if adjusted_margin >= 0 else spread_magnitude
        line = home_handicap
        yes_probability = _cover_probability(feature, home, away, home_handicap)
        yes_key, no_key = "home", "away"
        actual_yes = actual_home - actual_away + home_handicap > 0
    else:
        raise ValueError(f"Unsupported market: {market}")

    yes_probability = min(max(yes_probability, 0.0), 1.0)
    predicted_yes = yes_probability >= 0.5
    confidence = yes_probability if predicted_yes else 1.0 - yes_probability
    selection = yes_key if predicted_yes else no_key
    return {
        "selection": selection,
        "confidence": confidence,
        "correct": bool(predicted_yes == actual_yes),
        "line": line,
        "actual": yes_key if actual_yes else no_key,
    }


def _wilson_lower(correct: int, total: int, z_score: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    probability = correct / total
    denominator = 1.0 + z_score * z_score / total
    centre = probability + z_score * z_score / (2.0 * total)
    spread = z_score * math.sqrt((probability * (1.0 - probability) + z_score * z_score / (4.0 * total)) / total)
    return (centre - spread) / denominator


def tune_market(training: list[dict[str, Any]], sport: str, market: str) -> dict[str, Any]:
    minimum_selected = max(30, int(len(training) * 0.015))
    candidates = []
    for parameters in _candidate_parameters(sport, market):
        evaluated = [evaluate_market(feature, market, parameters) for feature in training]
        for threshold in CONFIDENCE_THRESHOLDS:
            selected = [row for row in evaluated if row["confidence"] >= threshold]
            if len(selected) < minimum_selected:
                continue
            correct = sum(1 for row in selected if row["correct"])
            accuracy = correct / len(selected)
            coverage = len(selected) / max(len(evaluated), 1)
            lower_bound = _wilson_lower(correct, len(selected))
            winner_lower_bound = 0.65 if market == "winner" else 0.72
            candidates.append({
                "parameters": parameters,
                "confidence_threshold": threshold,
                "predictions": len(selected),
                "correct": correct,
                "accuracy": accuracy,
                "coverage": coverage,
                "wilson_lower": lower_bound,
                "training_target_reached": accuracy > TARGET_ACCURACY and lower_bound >= winner_lower_bound,
            })
    if not candidates:
        return {
            "parameters": _candidate_parameters(sport, market)[0],
            "confidence_threshold": 1.0,
            "predictions": 0,
            "correct": 0,
            "accuracy": 0.0,
            "coverage": 0.0,
            "wilson_lower": 0.0,
            "training_target_reached": False,
        }
    target_candidates = [row for row in candidates if row["training_target_reached"]]
    pool = target_candidates or candidates
    if target_candidates:
        return max(pool, key=lambda row: (row["predictions"], row["wilson_lower"], row["accuracy"]))
    return max(pool, key=lambda row: (row["wilson_lower"], row["coverage"], row["predictions"]))


def _round_tuning(tuning: dict[str, Any]) -> dict[str, Any]:
    return {
        "parameters": tuning["parameters"],
        "confidence_threshold": tuning["confidence_threshold"],
        "predictions": tuning["predictions"],
        "correct": tuning["correct"],
        "accuracy": round(tuning["accuracy"] * 100.0, 2),
        "coverage": round(tuning["coverage"] * 100.0, 2),
        "wilson_lower": round(tuning["wilson_lower"] * 100.0, 2),
        "training_target_reached": tuning["training_target_reached"],
    }


def run_expanded_market_backtest(historical_games: list[dict[str, Any]]) -> dict[str, Any]:
    features_by_competition = chronological_features(historical_games)
    per_market = []
    frozen_parameters: dict[str, dict[str, Any]] = {}
    qualified_games = []
    all_holdout_predictions = 0
    all_holdout_correct = 0
    holdout_game_ids: set[str] = set()
    training_game_ids: set[str] = set()
    violations = []

    for competition, features in sorted(features_by_competition.items()):
        if len(features) < 20:
            continue
        sport = sport_for_competition(competition)
        training_end = max(10, min(len(features) - 2, int(len(features) * TRAINING_FRACTION)))
        validation_end = max(training_end + 1, min(len(features) - 1, int(len(features) * (TRAINING_FRACTION + VALIDATION_FRACTION))))
        training = features[:training_end]
        validation = features[training_end:validation_end]
        holdout = features[validation_end:]
        training_game_ids.update(str(row["game_id"]) for row in training + validation)
        holdout_game_ids.update(str(row["game_id"]) for row in holdout)
        frozen_parameters[competition] = {}

        for feature in holdout:
            latest = _timestamp(feature.get("latest_feature_game_utc"))
            kickoff = _timestamp(feature.get("start_time"))
            if latest and kickoff and latest >= kickoff:
                violations.append({
                    "game_id": feature.get("game_id"),
                    "competition": competition,
                    "latest_feature_game_utc": feature.get("latest_feature_game_utc"),
                    "kickoff_utc": feature.get("start_time"),
                })

        for market in MARKETS_BY_SPORT[sport]:
            tuning = tune_market(training, sport, market)
            validation_evaluated = [evaluate_market(feature, market, tuning["parameters"]) for feature in validation]
            validation_selected = [
                row for row in validation_evaluated
                if tuning["training_target_reached"] and row["confidence"] >= tuning["confidence_threshold"]
            ]
            validation_correct = sum(1 for row in validation_selected if row["correct"])
            validation_accuracy = validation_correct / len(validation_selected) if validation_selected else 0.0
            gate_validated = len(validation_selected) >= CERTIFICATION_MIN_PICKS and validation_accuracy > TARGET_ACCURACY
            frozen_parameters[competition][market] = {
                **tuning["parameters"],
                "confidence_threshold": tuning["confidence_threshold"],
                "training_target_reached": tuning["training_target_reached"],
                "training_accuracy": round(tuning["accuracy"], 6),
                "training_predictions": tuning["predictions"],
                "validation_predictions": len(validation_selected),
                "validation_accuracy": round(validation_accuracy, 6),
                "validated": gate_validated,
            }
            evaluated = [evaluate_market(feature, market, tuning["parameters"]) for feature in holdout]
            all_correct = sum(1 for row in evaluated if row["correct"])
            all_holdout_predictions += len(evaluated)
            all_holdout_correct += all_correct
            selected_indices = [
                index for index, row in enumerate(evaluated)
                if gate_validated and row["confidence"] >= tuning["confidence_threshold"]
            ]
            selected = [evaluated[index] for index in selected_indices]
            selected_correct = sum(1 for row in selected if row["correct"])
            for index in selected_indices:
                feature = holdout[index]
                result = evaluated[index]
                qualified_games.append({
                    "game_id": feature["game_id"],
                    "provider_event_id": feature.get("provider_event_id"),
                    "competition": competition,
                    "sport": sport,
                    "market": market,
                    "market_label": MARKET_LABELS[market],
                    "home_team": feature.get("home_team"),
                    "away_team": feature.get("away_team"),
                    "score": f"{int(feature['actual_home'])}-{int(feature['actual_away'])}",
                    "kickoff_utc": feature.get("start_time"),
                    "latest_feature_game_utc": feature.get("latest_feature_game_utc"),
                    "prior_games": feature.get("prior_games"),
                    "selection": result["selection"],
                    "actual": result["actual"],
                    "line": result["line"],
                    "confidence": round(result["confidence"], 6),
                    "correct": result["correct"],
                    "pre_result_prediction": True,
                })
            selected_accuracy = selected_correct / len(selected) if selected else 0.0
            frozen_parameters[competition][market].update({
                "holdout_predictions": len(selected),
                "holdout_accuracy": round(selected_accuracy, 6),
            })
            per_market.append({
                "competition": competition,
                "sport": sport,
                "market": market,
                "market_label": MARKET_LABELS[market],
                "training_games": len(training),
                "holdout_games": len(holdout),
                "all_predictions": len(evaluated),
                "all_accuracy": round(all_correct / max(len(evaluated), 1) * 100.0, 2),
                "qualified_predictions": len(selected),
                "qualified_correct": selected_correct,
                "qualified_accuracy": round(selected_accuracy * 100.0, 2),
                "coverage": round(len(selected) / max(len(evaluated), 1) * 100.0, 2),
                "target_reached": gate_validated,
                "frozen_tuning": _round_tuning(tuning),
                "training_cutoff": training[-1]["start_time"],
                "validation_games": len(validation),
                "validation_start": validation[0]["start_time"],
                "validation_predictions": len(validation_selected),
                "validation_accuracy": round(validation_accuracy * 100.0, 2),
                "holdout_start": holdout[0]["start_time"],
                "holdout_untouched_during_tuning_and_gate_selection": True,
            })

    validated_gates = {
        (row["competition"], row["market"])
        for row in per_market
        if row["target_reached"]
    }
    training_qualified_predictions = qualified_predictions = len(qualified_games)
    training_qualified_correct = qualified_correct = sum(1 for row in qualified_games if row["correct"])
    qualified_accuracy = qualified_correct / qualified_predictions if qualified_predictions else 0.0
    all_accuracy = all_holdout_correct / all_holdout_predictions if all_holdout_predictions else 0.0

    by_market = []
    for market in sorted({row["market"] for row in qualified_games} | {row["market"] for row in per_market}):
        rows = [row for row in qualified_games if row["market"] == market]
        correct = sum(1 for row in rows if row["correct"])
        evaluated = sum(row["all_predictions"] for row in per_market if row["market"] == market)
        accuracy = correct / max(len(rows), 1)
        by_market.append({
            "market": market,
            "market_label": MARKET_LABELS[market],
            "evaluated_predictions": evaluated,
            "qualified_predictions": len(rows),
            "correct": correct,
            "accuracy": round(accuracy * 100.0, 2),
            "coverage": round(len(rows) / max(evaluated, 1) * 100.0, 2),
            "deployment_qualified": len(rows) >= CERTIFICATION_MIN_PICKS and accuracy > TARGET_ACCURACY,
        })

    deployment_markets = {
        row["market"] for row in by_market if row["deployment_qualified"]
    }
    for row in per_market:
        row["deployment_enabled"] = bool(row["target_reached"] and row["market"] in deployment_markets)
        frozen_parameters[row["competition"]][row["market"]]["deployment_enabled"] = row["deployment_enabled"]

    deployment_games = [row for row in qualified_games if row["market"] in deployment_markets]
    deployment_correct = sum(1 for row in deployment_games if row["correct"])
    deployment_predictions = len(deployment_games)
    deployment_accuracy = deployment_correct / max(deployment_predictions, 1)
    deployment_coverage = deployment_predictions / max(all_holdout_predictions, 1)
    ordered_deployment = sorted(
        deployment_games,
        key=lambda row: (str(row.get("kickoff_utc") or ""), str(row.get("game_id") or ""), str(row.get("market") or "")),
    )
    rolling_accuracy = []
    target_downside = []
    net_correct = []
    cumulative = 0
    for index, row in enumerate(ordered_deployment):
        cumulative += 1 if row["correct"] else -1
        net_correct.append(cumulative)
        window = ordered_deployment[max(0, index - 99): index + 1]
        accuracy = sum(1 for item in window if item["correct"]) / len(window) * 100.0
        rolling_accuracy.append(round(accuracy, 2))
        target_downside.append(round(min(0.0, accuracy - TARGET_ACCURACY * 100.0), 2) if len(window) == 100 else 0.0)

    coverage = qualified_predictions / max(all_holdout_predictions, 1)
    winner_row = next((row for row in by_market if row["market"] == "winner"), None) or {}
    winner_accuracy = float(winner_row.get("accuracy", 0.0)) / 100.0
    winner_coverage = float(winner_row.get("coverage", 0.0)) / 100.0
    winner_target = {
        "accuracy": TARGET_ACCURACY * 100.0,
        "minimum_coverage": WINNER_TARGET_COVERAGE * 100.0,
        "predictions": int(winner_row.get("qualified_predictions", 0)),
        "correct": int(winner_row.get("correct", 0)),
        "actual_accuracy": round(winner_accuracy * 100.0, 2),
        "actual_coverage": round(winner_coverage * 100.0, 2),
        "reached": winner_accuracy > TARGET_ACCURACY and winner_coverage > WINNER_TARGET_COVERAGE,
        "scope": "straight match-winner predictions only",
    }
    return {
        "mode": "sports_predictions_train_validate_test_v3",
        "target": {
            "accuracy": TARGET_ACCURACY * 100.0,
            "reached": qualified_predictions >= 100 and qualified_accuracy > TARGET_ACCURACY and coverage > TARGET_COVERAGE and not violations,
            "minimum_predictions": 100,
            "minimum_coverage": TARGET_COVERAGE * 100.0,
            "scope": "validation-qualified winner, totals, and handicap predictions on untouched final test games",
            "note": "Parameters are selected on training games, gates are approved on the next chronological validation block, and headline metrics use only the final untouched test block.",
        },
        "deployment_target": {
            "accuracy": TARGET_ACCURACY * 100.0,
            "minimum_coverage": TARGET_COVERAGE * 100.0,
            "predictions": deployment_predictions,
            "correct": deployment_correct,
            "actual_accuracy": round(deployment_accuracy * 100.0, 2),
            "actual_coverage": round(deployment_coverage * 100.0, 2),
            "reached": deployment_predictions >= 100 and deployment_accuracy > TARGET_ACCURACY and deployment_coverage > TARGET_COVERAGE,
            "rule": "A validation-approved prediction type is paused for future deployment if final-test monitoring is at or below 80%.",
        },
        "winner_target": winner_target,
        "summary": {
            "verified_games": len(training_game_ids | holdout_game_ids),
            "training_games": len(training_game_ids),
            "holdout_games": len(holdout_game_ids),
            "evaluated_holdout_predictions": all_holdout_predictions,
            "all_accuracy": round(all_accuracy * 100.0, 2),
            "training_qualified_predictions": training_qualified_predictions,
            "training_qualified_correct": training_qualified_correct,
            "training_qualified_accuracy": round(training_qualified_correct / max(training_qualified_predictions, 1) * 100.0, 2),
            "validation_approved_gates": len(validated_gates),
            "qualified_predictions": qualified_predictions,
            "qualified_correct": qualified_correct,
            "qualified_losses": qualified_predictions - qualified_correct,
            "net_wins": qualified_correct - (qualified_predictions - qualified_correct),
            "qualified_accuracy": round(qualified_accuracy * 100.0, 2),
            "coverage": round(coverage * 100.0, 2),
            "all_validation_gates_above_target": bool(validated_gates) and all(
                row["validation_accuracy"] > TARGET_ACCURACY * 100.0
                for row in per_market if row["target_reached"]
            ),
            "uncertified_action": "withheld",
        },
        "certified_gates": [
            {
                "competition": competition,
                "market": market,
                "market_label": MARKET_LABELS[market],
            }
            for competition, market in sorted(validated_gates)
        ],
        "by_market": by_market,
        "per_market": per_market,
        "parameters": frozen_parameters,
        "games": qualified_games,
        "deployment_markets": sorted(deployment_markets),
        "series": {
            "rolling_accuracy_100": rolling_accuracy,
            "target_downside": target_downside,
            "net_correct": net_correct,
        },
        "stability": {
            "rolling_window": 100,
            "maximum_accuracy_downside_pp": round(min(target_downside), 2) if target_downside else 0.0,
            "definition": "Rolling accuracy percentage points below the fixed 80% target after a 100-prediction warmup.",
        },
        "profitability": {
            "status": "not_measurable_without_prices_and_costs",
            "reason": "Sports-only analysis excludes moneyline, odds, staking, execution costs, and trading inputs.",
        },
        "leakage_audit": {
            "passed": not violations,
            "violations": violations,
            "rule": "For each game, rolling form, league scoring, and Elo are read before that result updates state; final test results do not select parameters or gates.",
        },
    }
