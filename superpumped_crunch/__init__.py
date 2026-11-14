"""superpumped_crunch — sport-agnostic analysis engine.

The engine ingests verified historical results for any sport described by a
flat JSON config (see ``sports/*.json`` and :mod:`superpumped_crunch.config`)
and produces leak-free walk-forward backtests, market horizons, and ranking
predictions. Adding a sport requires zero code changes: drop a JSON file into
a sports directory or call :func:`register_sport` at runtime.
"""

from __future__ import annotations

from .config import (
    BASE_REQUIRED_KEYS,
    BUILTIN_SPORTS_DIR,
    DEFAULT_REGISTRY,
    KNOWN_MARKETS,
    SPORTS_DIR_ENV,
    SportConfigError,
    SportRegistry,
    list_sports,
    register_sport,
    sport_config,
    sport_for_competition,
    validate_sport_config,
)
from .horizons import (
    FOOTBALL_COMPETITIONS,
    MARKET_LABELS,
    MARKETS_BY_SPORT,
    SPORT_CONFIG,
    build_market_horizons,
    spread_magnitude_for_market,
    total_line_for_market,
)
from .market_backtest import (
    CERTIFICATION_MIN_PICKS,
    CONFIDENCE_THRESHOLDS,
    SCORE_ADJUSTMENTS,
    TARGET_ACCURACY,
    TARGET_COVERAGE,
    TRAINING_FRACTION,
    VALIDATION_FRACTION,
    WINNER_TARGET_COVERAGE,
    chronological_features,
    evaluate_market,
    run_expanded_market_backtest,
    tune_market,
)
from .ranking_backtest import (
    DRAW_COMPETITIONS,
    MARGIN_SCALES,
    WEIGHT_PROFILES,
    future_ranking_prediction,
    normalized_margin,
    run_walk_forward_backtest,
    simulate_walk_forward,
    tune_competition,
)

__version__ = "1.0.0"

__all__ = [
    "BASE_REQUIRED_KEYS",
    "BUILTIN_SPORTS_DIR",
    "CERTIFICATION_MIN_PICKS",
    "CONFIDENCE_THRESHOLDS",
    "DEFAULT_REGISTRY",
    "DRAW_COMPETITIONS",
    "FOOTBALL_COMPETITIONS",
    "KNOWN_MARKETS",
    "MARGIN_SCALES",
    "MARKET_LABELS",
    "MARKETS_BY_SPORT",
    "SCORE_ADJUSTMENTS",
    "SPORT_CONFIG",
    "SPORTS_DIR_ENV",
    "SportConfigError",
    "SportRegistry",
    "TARGET_ACCURACY",
    "TARGET_COVERAGE",
    "TRAINING_FRACTION",
    "VALIDATION_FRACTION",
    "WEIGHT_PROFILES",
    "WINNER_TARGET_COVERAGE",
    "build_market_horizons",
    "chronological_features",
    "evaluate_market",
    "future_ranking_prediction",
    "list_sports",
    "normalized_margin",
    "register_sport",
    "run_expanded_market_backtest",
    "run_walk_forward_backtest",
    "simulate_walk_forward",
    "sport_config",
    "sport_for_competition",
    "spread_magnitude_for_market",
    "total_line_for_market",
    "tune_competition",
    "tune_market",
    "validate_sport_config",
]
