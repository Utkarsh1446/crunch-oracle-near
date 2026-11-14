"""Command-line interface for the superpumped_crunch analysis engine.

Usage examples::

    python -m superpumped_crunch sports
    python -m superpumped_crunch validate path/to/sport.json
    python -m superpumped_crunch market-backtest --state data/engine_state.json
    python -m superpumped_crunch ranking-backtest --state data/engine_state.json --out report.json

The engine is sport-agnostic: point ``--sports-dir`` (or the
``SUPERPUMPED_SPORTS_DIR`` environment variable) at a directory of sport JSON
configs to analyze any sport with zero code changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import DEFAULT_REGISTRY, SportConfigError, validate_sport_config
from .market_backtest import run_expanded_market_backtest
from .ranking_backtest import run_walk_forward_backtest


def _load_games(state_path: str) -> list[dict[str, Any]]:
    path = Path(state_path)
    if not path.is_file():
        raise SystemExit(f"state file not found: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read state file {path}: {error}")
    if isinstance(state, dict):
        games = state.get("historical_games", [])
    elif isinstance(state, list):
        games = state
    else:
        raise SystemExit(f"{path}: expected an object with 'historical_games' or a list of games")
    return [game for game in games if isinstance(game, dict)]


def _verified(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        game for game in games
        if game.get("verified") is True
        and game.get("home_score") is not None
        and game.get("away_score") is not None
    ]


def _write_out(report: dict[str, Any], out: str | None) -> None:
    if out:
        Path(out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"full report written to {out}")


def _cmd_sports(args: argparse.Namespace) -> int:
    registry = _registry(args)
    for sport_id in registry.list_sports():
        config = registry.sport_config(sport_id)
        competitions = ", ".join(config["competitions"])
        draw = "draw" if config["has_draw"] else "no-draw"
        print(f"{sport_id:12s} {config['label']:12s} {config['distribution']:8s} {draw:8s} "
              f"markets={len(config['markets'])} competitions=[{competitions}]")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    failures = 0
    for source in args.paths:
        path = Path(source)
        candidates = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for file_path in candidates:
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                config = validate_sport_config(raw, source=str(file_path))
            except (OSError, json.JSONDecodeError) as error:
                print(f"FAIL {file_path}: {error}")
                failures += 1
            except SportConfigError as error:
                print(f"FAIL {error}")
                failures += 1
            else:
                print(f"OK   {file_path} -> sport '{config['id']}' ({config['label']})")
    return 1 if failures else 0


def _cmd_market_backtest(args: argparse.Namespace) -> int:
    _registry(args)
    games = _load_games(args.state)
    report = run_expanded_market_backtest(games)
    summary = report["summary"]
    target = report["target"]
    print(f"verified games:        {summary['verified_games']}")
    print(f"qualified predictions: {summary['qualified_predictions']}")
    print(f"qualified accuracy:    {summary['qualified_accuracy']}%")
    print(f"coverage:              {summary['coverage']}%")
    print(f"leakage audit passed:  {report['leakage_audit']['passed']}")
    print(f"target reached:        {target['reached']} "
          f"(accuracy > {target['accuracy']}%, coverage > {target['minimum_coverage']}%)")
    _write_out(report, args.out)
    return 0


def _cmd_ranking_backtest(args: argparse.Namespace) -> int:
    _registry(args)
    games = _verified(_load_games(args.state))
    report = run_walk_forward_backtest(games)
    summary = report["summary"]
    target = report["target"]
    print(f"verified games:           {summary['verified_games']}")
    print(f"holdout games:            {summary['holdout_games']}")
    print(f"all-game accuracy:        {summary['all_game_accuracy']}%")
    print(f"high-confidence accuracy: {summary['selective_accuracy']}% "
          f"({summary['selected_games']} predictions)")
    print(f"leakage audit passed:     {report['leakage_audit']['passed']}")
    print(f"target reached:           {target['reached']} (accuracy >= {target['accuracy']}%)")
    _write_out(report, args.out)
    return 0


def _registry(args: argparse.Namespace):
    for directory in getattr(args, "sports_dir", None) or []:
        DEFAULT_REGISTRY.load_dir(directory)
    return DEFAULT_REGISTRY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="superpumped_crunch",
        description="Sport-agnostic, leak-free sports analysis engine.",
    )
    parser.add_argument(
        "--sports-dir",
        action="append",
        default=None,
        help="extra directory of sport JSON configs (repeatable; also honors "
             "the SUPERPUMPED_SPORTS_DIR environment variable)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sports = subparsers.add_parser("sports", help="list registered sports")
    sports.set_defaults(func=_cmd_sports)

    validate = subparsers.add_parser("validate", help="validate sport config JSON files")
    validate.add_argument("paths", nargs="+", help="sport JSON files or directories")
    validate.set_defaults(func=_cmd_validate)

    market = subparsers.add_parser("market-backtest", help="run the expanded market backtest")
    market.add_argument("--state", default="data/engine_state.json", help="engine state JSON path")
    market.add_argument("--out", default=None, help="write the full JSON report to this path")
    market.set_defaults(func=_cmd_market_backtest)

    ranking = subparsers.add_parser("ranking-backtest", help="run the walk-forward ranking backtest")
    ranking.add_argument("--state", default="data/engine_state.json", help="engine state JSON path")
    ranking.add_argument("--out", default=None, help="write the full JSON report to this path")
    ranking.set_defaults(func=_cmd_ranking_backtest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SportConfigError as error:
        print(f"sport config error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
