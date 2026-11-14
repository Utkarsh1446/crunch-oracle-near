"""Choose the three most recent scorable replays and the next kickoff."""

from __future__ import annotations

from oracle.model import forecast_match


def contract_name_ok(name: str) -> bool:
    """Same character rule as the contract's team-name check."""
    if not (2 <= len(name) <= 48):
        return False
    return all(character.isalnum() or character in " .-'" for character in name)


def select_kickoff_window(
    matches: list[dict],
    start_iso: str,
    until_iso: str,
    skip_ids: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    """Unfinished matches with kickoff in [start_iso, until_iso), skipping ids already on chain."""
    skip = set(skip_ids)
    chosen = [
        match
        for match in matches
        if start_iso <= match["kickoff"] < until_iso and not match.get("finished") and match["id"] not in skip
    ]
    chosen.sort(key=lambda match: (match["kickoff"], match["id"]))
    return chosen


def plan_forecasts(
    matches: list[dict],
    start_iso: str,
    until_iso: str,
    skip_ids: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    rows = []
    for match in select_kickoff_window(matches, start_iso, until_iso, skip_ids):
        forecast = forecast_match(matches, match)
        rows.append(
            {
                "match_id": match["id"],
                "kickoff": match["kickoff"],
                "home": match["home"],
                "away": match["away"],
                "source_url": match["source_url"],
                "season": match["season"],
                "competition": match["competition"],
                "home_bps": forecast["home_bps"],
                "draw_bps": forecast["draw_bps"],
                "away_bps": forecast["away_bps"],
                "prior_matches": forecast["prior_matches"],
                "home_xg": forecast["home_xg"],
                "away_xg": forecast["away_xg"],
                "model": forecast["model"],
                "home_name_ok": contract_name_ok(match["home"]),
                "away_name_ok": contract_name_ok(match["away"]),
            }
        )
    return rows


def select_demo_matches(matches: list[dict], now_iso: str) -> tuple[list[dict], dict | None]:
    finished = [match for match in matches if match["finished"] and match["kickoff"] < now_iso]
    replays: list[dict] = []
    for match in reversed(finished):
        try:
            forecast = forecast_match(matches, match)
        except ValueError:
            continue
        if forecast["prior_matches"] < 40:
            continue
        replays.append(match)
        if len(replays) == 3:
            break
    replays.reverse()
    upcoming = [match for match in matches if (not match["finished"]) and match["kickoff"] > now_iso]
    upcoming.sort(key=lambda match: (match["kickoff"], match["id"]))
    return replays, upcoming[0] if upcoming else None
