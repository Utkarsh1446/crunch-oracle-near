"""Leak-free walk-forward ranking model and parameter tuner."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import DEFAULT_REGISTRY, sport_for_competition
from .horizons import FOOTBALL_COMPETITIONS as DRAW_COMPETITIONS
from .horizons import _RegistryView

OUTCOMES = ("home", "draw", "away")
TARGET_ACCURACY = 0.90

#: Typical winning-margin scale per sport (``ranking_margin_scale`` in each
#: sport JSON config). Raw margins are converted to football-equivalent units
#: so the Elo margin multiplier saturates comparably across low- and
#: high-scoring sports. Live view kept for backwards compatibility.
MARGIN_SCALES = _RegistryView("ranking_margin_scale")

#: Historical anchor: football's margin scale, the unit margins normalize to.
_BASELINE_MARGIN_SCALE = 1.45


def _has_draw(competition: str) -> bool:
    return bool(DEFAULT_REGISTRY.config_for_competition(competition)["has_draw"])


def normalized_margin(competition: str, margin: float) -> float:
    scale = float(DEFAULT_REGISTRY.config_for_competition(competition)["ranking_margin_scale"])
    return margin * (_BASELINE_MARGIN_SCALE / max(scale, 0.0001))

WEIGHT_PROFILES = (
    {"elo": 0.65, "form": 0.15, "h2h": 0.08, "long_term": 0.12},
    {"elo": 0.50, "form": 0.25, "h2h": 0.10, "long_term": 0.15},
    {"elo": 0.78, "form": 0.10, "h2h": 0.04, "long_term": 0.08},
)


def parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def logistic_elo(difference: float, scale: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, -difference / max(scale, 1.0)))


def outcome_probabilities(decisive_home: float, draw_probability: float) -> dict[str, float]:
    draw = clamp(draw_probability, 0.0, 0.60)
    decisive = 1.0 - draw
    home = decisive * clamp(decisive_home, 0.01, 0.99)
    return {"home": home, "draw": draw, "away": decisive - home}


def new_ranking_state(form_window: int) -> dict[str, Any]:
    return {
        "ratings": defaultdict(lambda: 1500.0),
        "forms": defaultdict(lambda: deque(maxlen=form_window)),
        "totals": defaultdict(lambda: [0.0, 0]),
        "h2h": defaultdict(lambda: deque(maxlen=12)),
        "latest_game_time": None,
        "processed_games": 0,
    }


def _team_form(state: dict[str, Any], team_id: str) -> float:
    values = state["forms"][team_id]
    return sum(values) / len(values) if values else 0.5


def _team_long_term(state: dict[str, Any], team_id: str) -> float:
    points, games = state["totals"][team_id]
    return (points + 2.0) / (games + 4.0)


def _h2h_home_rate(state: dict[str, Any], home_id: str, away_id: str) -> tuple[float, int]:
    key = tuple(sorted((home_id, away_id)))
    meetings = state["h2h"][key]
    if not meetings:
        return 0.5, 0
    score = 1.0
    for row in meetings:
        if row["outcome"] == "draw":
            score += 0.5
        else:
            winner_id = row["home_team_id"] if row["outcome"] == "home" else row["away_team_id"]
            score += 1.0 if winner_id == home_id else 0.0
    return score / (len(meetings) + 2.0), len(meetings)


def ranking_probabilities(
    state: dict[str, Any],
    competition: str,
    home_team_id: str,
    away_team_id: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    home_rating = float(state["ratings"][home_team_id])
    away_rating = float(state["ratings"][away_team_id])
    rating_difference = home_rating - away_rating + float(parameters["home_advantage"])
    elo_home = logistic_elo(rating_difference, float(parameters["elo_scale"]))

    home_form = _team_form(state, home_team_id)
    away_form = _team_form(state, away_team_id)
    form_logit = (home_form - away_form) * 3.0 + float(parameters["home_advantage"]) / 400.0
    form_home = 1.0 / (1.0 + math.exp(-form_logit))

    home_long = _team_long_term(state, home_team_id)
    away_long = _team_long_term(state, away_team_id)
    long_home = clamp(0.5 + (home_long - away_long) * 0.75 + float(parameters["home_advantage"]) / 1600.0, 0.05, 0.95)
    h2h_home, h2h_matches = _h2h_home_rate(state, home_team_id, away_team_id)

    weights = parameters["weights"]
    decisive_home = (
        elo_home * float(weights["elo"])
        + form_home * float(weights["form"])
        + h2h_home * float(weights["h2h"])
        + long_home * float(weights["long_term"])
    )
    weight_total = sum(float(value) for value in weights.values())
    decisive_home /= max(weight_total, 0.0001)

    draw_probability = 0.0
    if _has_draw(competition):
        draw_probability = float(parameters["draw_base"]) * math.exp(-abs(rating_difference) / 900.0)
        draw_probability = clamp(draw_probability, 0.12, 0.38)
    probabilities = outcome_probabilities(decisive_home, draw_probability)
    return {
        "probabilities": {key: round(value, 8) for key, value in probabilities.items()},
        "components": {
            "elo_home": round(elo_home, 8),
            "form_home": round(form_home, 8),
            "h2h_home": round(h2h_home, 8),
            "long_term_home": round(long_home, 8),
            "draw": round(draw_probability, 8),
            "h2h_matches": h2h_matches,
            "home_rating": round(home_rating, 3),
            "away_rating": round(away_rating, 3),
        },
    }


def update_ranking_state(state: dict[str, Any], game: dict[str, Any], parameters: dict[str, Any]) -> None:
    home_id = str(game["home_team_id"])
    away_id = str(game["away_team_id"])
    outcome = str(game["outcome"])
    actual_home = 1.0 if outcome == "home" else 0.5 if outcome == "draw" else 0.0
    rating_difference = (
        float(state["ratings"][home_id])
        - float(state["ratings"][away_id])
        + float(parameters["home_advantage"])
    )
    expected_home = logistic_elo(rating_difference, float(parameters["elo_scale"]))
    margin = abs(float(game.get("home_score", 0)) - float(game.get("away_score", 0)))
    margin = normalized_margin(str(game.get("competition") or ""), margin)
    margin_multiplier = 1.0 + 0.25 * min(math.log1p(margin), 2.0)
    delta = float(parameters["k_factor"]) * margin_multiplier * (actual_home - expected_home)
    state["ratings"][home_id] += delta
    state["ratings"][away_id] -= delta
    state["forms"][home_id].append(actual_home)
    state["forms"][away_id].append(1.0 - actual_home)
    state["totals"][home_id][0] += actual_home
    state["totals"][home_id][1] += 1
    state["totals"][away_id][0] += 1.0 - actual_home
    state["totals"][away_id][1] += 1
    state["h2h"][tuple(sorted((home_id, away_id)))].append({
        "home_team_id": home_id,
        "away_team_id": away_id,
        "outcome": outcome,
    })
    state["processed_games"] += 1


def _prediction_row(
    game: dict[str, Any],
    prediction: dict[str, Any],
    latest_feature_game: str | None,
    prior_games: int,
) -> dict[str, Any]:
    probabilities = prediction["probabilities"]
    predicted = max(probabilities, key=probabilities.get)
    kickoff = parse_time(game["start_time"])
    return {
        "prediction_id": "wf_" + str(game["game_id"]),
        "fixture_id": str(game["game_id"]),
        "competition": str(game["competition"]),
        "home_team": {"id": str(game["home_team_id"]), "name": str(game["home_team"])},
        "away_team": {"id": str(game["away_team_id"]), "name": str(game["away_team"])},
        "score": f"{game['home_score']}-{game['away_score']}",
        "kickoff_utc": iso_time(kickoff),
        "prediction_frozen_at": iso_time(kickoff - timedelta(minutes=60)),
        "latest_feature_game_utc": latest_feature_game,
        "prior_games": prior_games,
        "predicted_winner": predicted,
        "actual_winner": str(game["outcome"]),
        "correct": predicted == str(game["outcome"]),
        "confidence": round(float(probabilities[predicted]), 8),
        "probabilities": probabilities,
        "components": prediction["components"],
        "provider_event_id": game.get("provider_event_id"),
        "source_url": game.get("source_url"),
        "pre_result_prediction": True,
    }


def simulate_walk_forward(
    games: list[dict[str, Any]],
    parameters: dict[str, Any],
    score_from: str | None = None,
    score_until: str | None = None,
    detailed: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(games, key=lambda row: (str(row["start_time"]), str(row["game_id"])))
    state = new_ranking_state(int(parameters["form_window"]))
    rows: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    lower = parse_time(score_from) if score_from else None
    upper = parse_time(score_until) if score_until else None
    index = 0
    while index < len(ordered):
        batch_time = str(ordered[index]["start_time"])
        batch: list[dict[str, Any]] = []
        while index < len(ordered) and str(ordered[index]["start_time"]) == batch_time:
            batch.append(ordered[index])
            index += 1
        kickoff = parse_time(batch_time)
        latest_feature = state["latest_game_time"]
        latest_feature_iso = iso_time(latest_feature) if latest_feature else None
        if latest_feature and latest_feature >= kickoff:
            violations.append({"kickoff_utc": iso_time(kickoff), "latest_feature_game_utc": latest_feature_iso})
        frozen = []
        for game in batch:
            prediction = ranking_probabilities(
                state,
                str(game["competition"]),
                str(game["home_team_id"]),
                str(game["away_team_id"]),
                parameters,
            )
            frozen.append((game, prediction, int(state["processed_games"])))
        for game, prediction, prior_games in frozen:
            if (lower is None or kickoff >= lower) and (upper is None or kickoff < upper):
                if detailed:
                    rows.append(_prediction_row(game, prediction, latest_feature_iso, prior_games))
                else:
                    probabilities = prediction["probabilities"]
                    predicted = max(probabilities, key=probabilities.get)
                    rows.append({
                        "fixture_id": str(game["game_id"]),
                        "kickoff_utc": str(game["start_time"]),
                        "actual_winner": str(game["outcome"]),
                        "predicted_winner": predicted,
                        "correct": predicted == str(game["outcome"]),
                        "confidence": round(float(probabilities[predicted]), 8),
                        "probabilities": probabilities,
                    })
        for game, _, _ in frozen:
            update_ranking_state(state, game, parameters)
        state["latest_game_time"] = kickoff
    return rows, violations, state


def _row_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {"accuracy": 0.0, "log_loss": 0.0, "brier": 0.0}
    correct = sum(1 for row in rows if row["correct"])
    log_loss = 0.0
    brier = 0.0
    for row in rows:
        actual = row["actual_winner"]
        probability = clamp(float(row["probabilities"].get(actual, 0.0)), 1e-9, 1.0)
        log_loss -= math.log(probability)
        brier += sum(
            (float(row["probabilities"].get(key, 0.0)) - (1.0 if actual == key else 0.0)) ** 2
            for key in OUTCOMES
        )
    return {
        "accuracy": correct / len(rows),
        "log_loss": log_loss / len(rows),
        "brier": brier / len(rows),
    }


def parameter_candidates(competition: str) -> list[dict[str, Any]]:
    draw_values = (0.22, 0.28, 0.32) if _has_draw(competition) else (0.0,)
    candidates = []
    for weights in WEIGHT_PROFILES:
        for home_advantage in (0, 45, 75, 105):
            for k_factor in (16, 28, 40):
                for elo_scale in (350, 450):
                    for form_window in (5, 10):
                        for draw_base in draw_values:
                            candidates.append({
                                "home_advantage": home_advantage,
                                "k_factor": k_factor,
                                "elo_scale": elo_scale,
                                "form_window": form_window,
                                "draw_base": draw_base,
                                "weights": dict(weights),
                            })
    return candidates


def _split_times(games: list[dict[str, Any]]) -> tuple[str, str]:
    times = sorted({str(game["start_time"]) for game in games})
    if len(times) < 12:
        raise ValueError("At least 12 distinct historical timestamps are required")
    holdout_index = min(len(times) - 1, max(2, int(len(times) * 0.75)))
    holdout_start = times[holdout_index]
    training_times = times[:holdout_index]
    warmup_index = min(len(training_times) - 1, max(1, int(len(training_times) * 0.20)))
    return training_times[warmup_index], holdout_start


def _wilson_lower(correct: int, total: int, z_score: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    probability = correct / total
    denominator = 1.0 + z_score * z_score / total
    centre = probability + z_score * z_score / (2.0 * total)
    spread = z_score * math.sqrt((probability * (1.0 - probability) + z_score * z_score / (4.0 * total)) / total)
    return (centre - spread) / denominator


WILSON_TARGET_LOWER = 0.65


def _threshold_choice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    minimum = max(10, min(40, math.ceil(len(rows) * 0.05)))
    candidates = []
    for threshold in (0.50, 0.525, 0.55, 0.575, 0.60, 0.625, 0.65, 0.675, 0.70, 0.725, 0.75, 0.775, 0.80, 0.825, 0.85, 0.875, 0.90):
        selected = [row for row in rows if float(row["confidence"]) >= threshold]
        if len(selected) < minimum:
            continue
        correct = sum(1 for row in selected if row["correct"])
        accuracy = correct / len(selected)
        lower_bound = _wilson_lower(correct, len(selected))
        candidates.append({"threshold": threshold, "games": len(selected), "accuracy": accuracy, "wilson_lower": lower_bound})
    target = next((row for row in candidates if row["accuracy"] >= TARGET_ACCURACY and row["wilson_lower"] >= WILSON_TARGET_LOWER), None)
    if target:
        return {**target, "selection_reason": "training_target_reached", "minimum_games": minimum}
    if candidates:
        best = max(candidates, key=lambda row: (row["wilson_lower"], row["games"], -row["threshold"]))
        return {**best, "selection_reason": "best_training_wilson_lower", "minimum_games": minimum}
    return {"threshold": 1.0, "games": 0, "accuracy": 0.0, "wilson_lower": 0.0, "selection_reason": "insufficient_training_predictions", "minimum_games": minimum}


def _validation_split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: (str(row["kickoff_utc"]), str(row["fixture_id"])))
    split_index = max(1, int(len(ordered) * 0.75))
    return ordered[:split_index], ordered[split_index:]


def _validate_threshold(validation_rows: list[dict[str, Any]], threshold: dict[str, Any]) -> dict[str, Any]:
    selected = [row for row in validation_rows if float(row["confidence"]) >= float(threshold["threshold"])]
    correct = sum(1 for row in selected if row["correct"])
    accuracy = correct / len(selected) if selected else 0.0
    lower_bound = _wilson_lower(correct, len(selected))
    passed = len(selected) >= 10 and lower_bound >= 0.50
    return {
        "games": len(selected),
        "correct": correct,
        "accuracy": round(accuracy * 100.0, 2),
        "wilson_lower": round(lower_bound * 100.0, 2),
        "passed": passed,
        "rule": "At least 10 validation predictions with a Wilson 95% lower bound of 50%+ before the threshold is trusted on holdout.",
    }


def tune_competition(games: list[dict[str, Any]], competition: str) -> dict[str, Any]:
    ordered = sorted(games, key=lambda row: (str(row["start_time"]), str(row["game_id"])))
    warmup_start, holdout_start = _split_times(ordered)
    training = [game for game in ordered if str(game["start_time"]) < holdout_start]
    best: dict[str, Any] | None = None
    for parameters in parameter_candidates(competition):
        rows, violations, _ = simulate_walk_forward(training, parameters, score_from=warmup_start, detailed=False)
        metrics = _row_metrics(rows)
        objective = metrics["accuracy"] - 0.02 * metrics["log_loss"] - 0.01 * metrics["brier"]
        candidate = {
            "parameters": parameters,
            "rows": rows,
            "violations": violations,
            "metrics": metrics,
            "objective": objective,
        }
        if best is None or (candidate["objective"], -metrics["log_loss"]) > (best["objective"], -best["metrics"]["log_loss"]):
            best = candidate
    if best is None:
        raise ValueError(f"Could not tune {competition}")
    tuning_rows, validation_rows = _validation_split(best["rows"])
    threshold = _threshold_choice(tuning_rows)
    validation = _validate_threshold(validation_rows, threshold)
    parameters = {**best["parameters"], "confidence_threshold": threshold["threshold"]}
    holdout_rows, violations, _ = simulate_walk_forward(ordered, parameters, score_from=holdout_start)
    selected = [row for row in holdout_rows if float(row["confidence"]) >= float(threshold["threshold"])]
    all_metrics = _row_metrics(holdout_rows)
    selected_metrics = _row_metrics(selected)
    selected_correct = sum(1 for row in selected if row["correct"])
    selected_wilson = _wilson_lower(selected_correct, len(selected))
    training_cutoff = max(str(game["start_time"]) for game in training)
    leakage = best["violations"] + violations
    return {
        "competition": competition,
        "games": len(ordered),
        "training_games": len(training),
        "tuning_predictions": len(best["rows"]),
        "holdout_games": len(holdout_rows),
        "selected_games": len(selected),
        "all_game_accuracy": round(all_metrics["accuracy"] * 100.0, 2),
        "selective_accuracy": round(selected_metrics["accuracy"] * 100.0, 2) if selected else None,
        "selective_wilson_lower": round(selected_wilson * 100.0, 2),
        "coverage": round(len(selected) / max(len(holdout_rows), 1) * 100.0, 2),
        "log_loss": round(all_metrics["log_loss"], 6),
        "brier": round(all_metrics["brier"], 6),
        "target_reached": bool(
            selected
            and len(selected) >= 30
            and selected_metrics["accuracy"] >= TARGET_ACCURACY
            and selected_wilson >= WILSON_TARGET_LOWER
            and validation["passed"]
        ),
        "parameters": parameters,
        "threshold_training": threshold,
        "validation_gate": validation,
        "warmup_start": warmup_start,
        "training_cutoff": training_cutoff,
        "holdout_start": holdout_start,
        "holdout_untouched_during_tuning": training_cutoff < holdout_start,
        "leakage_violations": leakage,
        "holdout_predictions": holdout_rows,
    }


def run_walk_forward_backtest(games: list[dict[str, Any]]) -> dict[str, Any]:
    by_competition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        by_competition[str(game["competition"])].append(game)
    per_league = []
    skipped = []
    for competition in sorted(by_competition):
        competition_games = by_competition[competition]
        if len(competition_games) < 40:
            skipped.append({"competition": competition, "games": len(competition_games), "reason": "fewer than 40 verified games"})
            continue
        try:
            per_league.append(tune_competition(competition_games, competition))
        except ValueError as exc:
            skipped.append({"competition": competition, "games": len(competition_games), "reason": str(exc)})

    holdout = sorted(
        [row for league in per_league for row in league["holdout_predictions"]],
        key=lambda row: (row["kickoff_utc"], row["fixture_id"]),
    )
    thresholds = {league["competition"]: league["parameters"]["confidence_threshold"] for league in per_league}
    selected = [row for row in holdout if float(row["confidence"]) >= float(thresholds[row["competition"]])]
    all_metrics = _row_metrics(holdout)
    selected_metrics = _row_metrics(selected)
    leakage = [violation for league in per_league for violation in league["leakage_violations"]]
    running = []
    correct = 0
    for index, row in enumerate(holdout, 1):
        correct += 1 if row["correct"] else 0
        running.append(round(correct / index * 100.0, 2))
    peak = 0.0
    drawdown = []
    for value in running:
        peak = max(peak, value)
        drawdown.append(round(value - peak, 2))
    selective_accuracy = selected_metrics["accuracy"] * 100.0 if selected else 0.0
    selective_correct = sum(1 for row in selected if row["correct"])
    selective_wilson = _wilson_lower(selective_correct, len(selected))
    coverage = len(selected) / max(len(holdout), 1) * 100.0
    validation_gates_passed = all(league["validation_gate"]["passed"] for league in per_league) if per_league else False
    target_reached = (
        len(selected) >= 30
        and selective_accuracy >= TARGET_ACCURACY * 100.0
        and selective_wilson >= WILSON_TARGET_LOWER
        and not leakage
    )
    return {
        "mode": "walk_forward_frozen_predictions",
        "target": {
            "accuracy": 90.0,
            "reached": target_reached,
            "scope": "high-confidence untouched holdout",
            "minimum_predictions": 30,
            "note": "The target is reported only when achieved on later unseen games; it is never forced by reading holdout results.",
        },
        "metrics": [
            {"label": "Verified Games", "value": str(len(games)), "color": "var(--text)"},
            {"label": "Holdout Games", "value": str(len(holdout)), "color": "var(--text2)"},
            {"label": "All-Game Accuracy", "value": f"{all_metrics['accuracy'] * 100.0:.1f}%", "color": "var(--info)"},
            {"label": "High-Confidence Accuracy", "value": f"{selective_accuracy:.1f}%", "color": "var(--accent)" if target_reached else "var(--warning)"},
            {"label": "Prediction Coverage", "value": f"{coverage:.1f}%", "color": "var(--text2)"},
            {"label": "Wilson 95% Lower Bound", "value": f"{selective_wilson * 100.0:.1f}%", "color": "var(--accent)" if selective_wilson >= WILSON_TARGET_LOWER else "var(--warning)"},
            {"label": "Leakage Violations", "value": str(len(leakage)), "color": "var(--accent)" if not leakage else "var(--danger)"},
        ],
        "summary": {
            "verified_games": len(games),
            "holdout_games": len(holdout),
            "correct": sum(1 for row in holdout if row["correct"]),
            "all_game_accuracy": round(all_metrics["accuracy"] * 100.0, 2),
            "selected_games": len(selected),
            "selective_correct": sum(1 for row in selected if row["correct"]),
            "selective_accuracy": round(selective_accuracy, 2),
            "selective_wilson_lower": round(selective_wilson * 100.0, 2),
            "validation_gates_passed": validation_gates_passed,
            "coverage": round(coverage, 2),
            "log_loss": round(all_metrics["log_loss"], 6),
            "brier": round(all_metrics["brier"], 6),
        },
        "series": {"accuracy": running, "drawdown": drawdown, "pnl": running},
        "games": holdout,
        "per_league": [
            {key: value for key, value in league.items() if key not in {"holdout_predictions", "leakage_violations"}}
            for league in per_league
        ],
        "parameters": {league["competition"]: league["parameters"] for league in per_league},
        "leakage_audit": {
            "passed": not leakage,
            "violations": leakage,
            "rule": "Every prediction is frozen before kickoff and uses only games with an earlier kickoff; simultaneous games are predicted as one batch before any result update.",
        },
        "skipped": skipped,
    }


def future_ranking_prediction(
    games: list[dict[str, Any]],
    competition: str,
    home_team_id: str,
    away_team_id: str,
    kickoff_utc: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    eligible = [
        game for game in games
        if str(game.get("competition")) == competition and str(game.get("start_time")) < kickoff_utc
    ]
    _, violations, state = simulate_walk_forward(eligible, parameters, score_from=kickoff_utc, detailed=False)
    prediction = ranking_probabilities(state, competition, home_team_id, away_team_id, parameters)
    return {
        "verified": bool(eligible) and not violations,
        "source": "walk_forward_historical_rankings",
        "as_of": iso_time(state["latest_game_time"]) if state["latest_game_time"] else None,
        "training_games": len(eligible),
        "team_ids": {"home": home_team_id, "away": away_team_id},
        "probabilities": prediction["probabilities"],
        "components": prediction["components"],
        "parameters": parameters,
        "leakage_violations": violations,
    }
