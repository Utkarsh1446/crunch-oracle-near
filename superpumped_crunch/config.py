"""Sport configuration registry for superpumped_crunch.

Every sport is described by a flat JSON document (see ``sports/*.json``).
Adding a sport requires zero code changes: drop a JSON file into a sports
directory (built-in ``sports/``, a directory named by the
``SUPERPUMPED_SPORTS_DIR`` environment variable, or any directory/file/dict
passed to :func:`register_sport`).

The module is stdlib-only and dependency-free.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

BUILTIN_SPORTS_DIR = Path(__file__).resolve().parent / "sports"
SPORTS_DIR_ENV = "SUPERPUMPED_SPORTS_DIR"

#: Keys every sport config must define, regardless of distribution family.
BASE_REQUIRED_KEYS = (
    "id",
    "label",
    "unit",
    "distribution",
    "fallback_home",
    "fallback_away",
    "recent_games",
    "margin_scale",
    "total_line",
    "safe_total_line",
    "alternate_delta",
    "spread_line",
    "spread_alternate_delta",
    "safe_spread_line",
    "spread_label",
    "minimum_score",
    "maximum_total",
    "competitions",
    "has_draw",
    "total_label",
    "markets",
    "market_family",
    "score_adjustments",
)

KNOWN_MARKETS = (
    "winner",
    "total_ultra_safe",
    "total_safe",
    "total_low",
    "total",
    "total_high",
    "spread",
    "spread_alt",
    "spread_safe",
    "btts",
    "home_team_total",
    "away_team_total",
    "both_teams_threshold",
)

DISTRIBUTIONS = ("poisson", "normal")
MARKET_FAMILIES = ("team_totals", "both_teams_threshold")

DEFAULT_DRAW_PRIOR_PREDICTION = 0.26
DEFAULT_DRAW_PRIOR_SCORECARD = 0.24


class SportConfigError(ValueError):
    """Raised when a sport configuration file is invalid."""


def validate_sport_config(raw: dict[str, Any], source: str = "<dict>") -> dict[str, Any]:
    """Validate a raw sport config and return a normalized deep copy."""
    if not isinstance(raw, dict):
        raise SportConfigError(f"{source}: sport config must be a JSON object")
    config = copy.deepcopy(raw)

    missing = [key for key in BASE_REQUIRED_KEYS if key not in config]
    if missing:
        raise SportConfigError(f"{source}: missing required keys: {', '.join(sorted(missing))}")

    sport_id = str(config["id"])
    if not sport_id or not sport_id.replace("_", "").isalnum():
        raise SportConfigError(f"{source}: 'id' must be a non-empty alphanumeric/underscore string")
    config["id"] = sport_id

    distribution = config["distribution"]
    if distribution not in DISTRIBUTIONS:
        raise SportConfigError(f"{source}: 'distribution' must be one of {DISTRIBUTIONS}, got {distribution!r}")
    if distribution == "poisson" and "score_cap" not in config:
        raise SportConfigError(f"{source}: poisson sports require 'score_cap'")
    if distribution == "normal":
        for key in ("total_sd", "margin_sd", "team_sd"):
            if key not in config:
                raise SportConfigError(f"{source}: normal sports require '{key}'")

    competitions = config["competitions"]
    if not isinstance(competitions, list) or not competitions or not all(
        isinstance(item, str) and item for item in competitions
    ):
        raise SportConfigError(f"{source}: 'competitions' must be a non-empty list of strings")
    config["competitions"] = [str(item) for item in competitions]

    markets = config["markets"]
    if not isinstance(markets, list) or "winner" not in markets:
        raise SportConfigError(f"{source}: 'markets' must be a list containing at least 'winner'")
    unknown = [market for market in markets if market not in KNOWN_MARKETS]
    if unknown:
        raise SportConfigError(f"{source}: unknown markets: {', '.join(unknown)}")
    config["markets"] = tuple(str(market) for market in markets)
    if "total_ultra_safe" in config["markets"] and "ultra_safe_total_line" not in config:
        raise SportConfigError(f"{source}: 'total_ultra_safe' market requires 'ultra_safe_total_line'")

    family = config["market_family"]
    if family not in MARKET_FAMILIES:
        raise SportConfigError(f"{source}: 'market_family' must be one of {MARKET_FAMILIES}, got {family!r}")
    if family == "team_totals":
        lines = config.get("team_total_lines")
        if not isinstance(lines, dict) or "home" not in lines or "away" not in lines:
            raise SportConfigError(f"{source}: market_family 'team_totals' requires 'team_total_lines' with 'home' and 'away'")
    else:
        for key in ("team_threshold", "team_threshold_label"):
            if key not in config:
                raise SportConfigError(f"{source}: market_family 'both_teams_threshold' requires '{key}'")

    adjustments = config["score_adjustments"]
    if not isinstance(adjustments, list) or not adjustments:
        raise SportConfigError(f"{source}: 'score_adjustments' must be a non-empty list of numbers")
    try:
        config["score_adjustments"] = tuple(float(value) for value in adjustments)
    except (TypeError, ValueError) as error:
        raise SportConfigError(f"{source}: 'score_adjustments' must be numeric: {error}") from error

    config["has_draw"] = bool(config["has_draw"])
    if config["has_draw"]:
        config.setdefault("draw_prior_prediction", DEFAULT_DRAW_PRIOR_PREDICTION)
        config.setdefault("draw_prior_scorecard", DEFAULT_DRAW_PRIOR_SCORECARD)
    config.setdefault("ranking_margin_scale", float(config["margin_scale"]))
    return config


class SportRegistry:
    """Holds every known sport config and the competition -> sport mapping."""

    def __init__(
        self,
        include_builtin: bool = True,
        extra_dirs: list[str | Path] | None = None,
        use_env_dir: bool = True,
    ) -> None:
        self._sports: dict[str, dict[str, Any]] = {}
        self._competition_map: dict[str, str] = {}
        if include_builtin and BUILTIN_SPORTS_DIR.is_dir():
            self.load_dir(BUILTIN_SPORTS_DIR)
        if use_env_dir:
            env_dir = os.environ.get(SPORTS_DIR_ENV, "").strip()
            if env_dir:
                self.load_dir(env_dir)
        for directory in extra_dirs or []:
            self.load_dir(directory)

    def load_dir(self, directory: str | Path) -> list[str]:
        path = Path(directory)
        if not path.is_dir():
            raise SportConfigError(f"sports directory not found: {path}")
        loaded = []
        for file_path in sorted(path.glob("*.json")):
            loaded.append(self.register_sport(file_path))
        return loaded

    def register_sport(self, source: dict[str, Any] | str | Path) -> str:
        if isinstance(source, (str, Path)):
            path = Path(source)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SportConfigError(f"{path}: cannot load sport config: {error}") from error
            config = validate_sport_config(raw, source=str(path))
        else:
            config = validate_sport_config(source)
        sport_id = config["id"]
        for competition in config["competitions"]:
            owner = self._competition_map.get(competition)
            if owner is not None and owner != sport_id:
                raise SportConfigError(
                    f"competition {competition!r} claimed by both {owner!r} and {sport_id!r}"
                )
        self._sports[sport_id] = config
        for competition in config["competitions"]:
            self._competition_map[competition] = sport_id
        return sport_id

    def list_sports(self) -> list[str]:
        return sorted(self._sports)

    def has_sport(self, sport_id: str) -> bool:
        return sport_id in self._sports

    def sport_config(self, sport_id: str) -> dict[str, Any]:
        try:
            return self._sports[sport_id]
        except KeyError:
            raise SportConfigError(
                f"unknown sport {sport_id!r}; registered: {', '.join(self.list_sports()) or '<none>'}"
            ) from None

    def sport_for_competition(self, competition: str) -> str:
        sport = self._competition_map.get(competition)
        if sport is not None:
            return sport
        # Historical behavior: competitions equal to a sport id map to that
        # sport; anything unknown falls back to football-style handling.
        if competition in self._sports:
            return competition
        return "football"

    def config_for_competition(self, competition: str) -> dict[str, Any]:
        return self.sport_config(self.sport_for_competition(competition))

    @property
    def competition_map(self) -> dict[str, str]:
        return dict(self._competition_map)


DEFAULT_REGISTRY = SportRegistry()


def sport_config(sport_id: str) -> dict[str, Any]:
    return DEFAULT_REGISTRY.sport_config(sport_id)


def sport_for_competition(competition: str) -> str:
    return DEFAULT_REGISTRY.sport_for_competition(competition)


def register_sport(source: dict[str, Any] | str | Path) -> str:
    return DEFAULT_REGISTRY.register_sport(source)


def list_sports() -> list[str]:
    return DEFAULT_REGISTRY.list_sports()
