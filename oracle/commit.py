"""Canonical commit payload and multiclass Brier score shared with the NEAR contract.

The stored score is the standard sum of squared errors over the three outcomes,
in millionths:

    brier_micro = sum_i (p_i_bps - o_i_bps)^2 // 100

Probabilities are basis points from 0 to 10000. The observed outcome is 10000
and the other two outcomes are 0. Dividing the stored integer by 1_000_000
returns

    (p_home - o_home)^2 + (p_draw - o_draw)^2 + (p_away - o_away)^2

with each probability in [0, 1]. The contract does not divide by 3.

A uniform one-third forecast has standard sum 2/3 (0.6666...). The per-class
mean of that sum is 2/9 (0.2222...). That per-class mean is not stored.
"""

from __future__ import annotations

import hashlib

UNIFORM_SUM = 2 / 3
UNIFORM_PER_CLASS = 2 / 9


def canonical_payload(match_id: str, home_bps: int, draw_bps: int, away_bps: int, salt: str) -> str:
    return f"{match_id}|{home_bps}|{draw_bps}|{away_bps}|{salt}"


def commitment_hex(match_id: str, home_bps: int, draw_bps: int, away_bps: int, salt: str) -> str:
    payload = canonical_payload(match_id, home_bps, draw_bps, away_bps, salt)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def brier_micro(home_bps: int, draw_bps: int, away_bps: int, outcome: str) -> int:
    observed = {"home": (10000, 0, 0), "draw": (0, 10000, 0), "away": (0, 0, 10000)}[outcome.lower()]
    total = 0
    for probability, target in zip((home_bps, draw_bps, away_bps), observed):
        delta = probability - target
        total += delta * delta
    return total // 100


def brier_sum(micro: int) -> float:
    """Standard multiclass Brier. The contract stores this, in millionths."""
    return micro / 1_000_000


def per_class_mean(micro: int) -> float:
    """Standard sum divided by 3. The contract does not store this."""
    return brier_sum(micro) / 3


def audit_public_state(state: dict) -> dict:
    """Recompute commitments and Brier scores from a public contract export.

    `state` is the JSON from `export_public`. This does not read any local
    model file.
    """
    problems: list[str] = []
    rows: list[dict] = []
    for match in state.get("matches") or []:
        reveal = match.get("reveal")
        if reveal and match.get("commitment"):
            got = commitment_hex(
                match["match_id"],
                int(reveal["home_bps"]),
                int(reveal["draw_bps"]),
                int(reveal["away_bps"]),
                reveal["salt"],
            )
            if got != match["commitment"]:
                problems.append(f"commitment {match['match_id']}")
        result = match.get("result")
        score = match.get("score")
        if reveal and result and score:
            micro = brier_micro(
                int(reveal["home_bps"]),
                int(reveal["draw_bps"]),
                int(reveal["away_bps"]),
                str(result["outcome"]),
            )
            stored = int(score["brier_micro"])
            if micro != stored:
                problems.append(f"brier {match['match_id']}")
            rows.append(
                {
                    "match_id": match["match_id"],
                    "outcome": str(result["outcome"]).lower(),
                    "home_bps": int(reveal["home_bps"]),
                    "draw_bps": int(reveal["draw_bps"]),
                    "away_bps": int(reveal["away_bps"]),
                    "brier_micro": micro,
                    "standard_sum": brier_sum(micro),
                    "per_class_mean": per_class_mean(micro),
                }
            )
    total = sum(row["brier_micro"] for row in rows)
    count = len(rows)
    contract_mean = (total // count) if count else None
    agents = state.get("agents") or []
    if agents and count:
        agent = agents[0]
        if int(agent.get("brier_micro_sum")) != total:
            problems.append("agent sum")
        if int(agent.get("mean_brier_micro")) != contract_mean:
            problems.append("agent mean")
        if int(agent.get("scored") or 0) != count:
            problems.append("agent scored count")
    exact_mean = (total / count / 1_000_000) if count else None
    class_mean = (total / count / 3 / 1_000_000) if count else None
    return {
        "ok": not problems,
        "problems": problems,
        "rows": rows,
        "scored": count,
        "brier_micro_sum": total,
        "contract_mean_micro": contract_mean,
        "standard_mean": exact_mean,
        "per_class_mean": class_mean,
        "uniform_sum": UNIFORM_SUM,
        "uniform_per_class": UNIFORM_PER_CLASS,
    }
