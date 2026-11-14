#!/usr/bin/env python3
"""Plan, or after an explicit --execute, commit forecasts for one Bundesliga matchday.

Default sends nothing. It does not create an account, post a result, or call 1Click.

The OpenLigaDB cache fetched 2026-11-14 has no Bundesliga kickoff from
2026-11-14 through 2026-11-16. The default window is the next matchday:

    2026-11-20T00:00:00Z inclusive to 2026-11-23T00:00:00Z exclusive

FC Bayern München vs 1. FC Köln (bl1-2026-83237) is already on chain and is skipped.

    python3 scripts/commit_weekend.py
    python3 scripts/commit_weekend.py --execute

--execute is for the owner after a separate yes. It registers, commits, and
reveals on the testnet account in docs/testnet.json. Salts are created only
in that mode. The contract rejects a kickoff that is not in the future or is
more than 21 days ahead.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oracle.openliga import CACHE, load_cache, refresh_cache
from oracle.select import plan_forecasts

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "docs" / "testnet.json"
SNAPSHOT = ROOT / "dashboard" / "snapshot.json"
RPC = "https://rpc.testnet.near.org"
DEFAULT_FROM = "2026-11-20T00:00:00Z"
DEFAULT_UNTIL = "2026-11-23T00:00:00Z"
EMPTY_WEEKEND_FROM = "2026-11-14T00:00:00Z"
EMPTY_WEEKEND_UNTIL = "2026-11-17T00:00:00Z"
MAX_AHEAD_MS = 21 * 24 * 60 * 60 * 1000
MAX_MATCHES = 32


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def view_match_ids(account_id: str) -> list[str] | None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "crunch",
            "method": "query",
            "params": {
                "request_type": "call_function",
                "finality": "final",
                "account_id": account_id,
                "method_name": "export_public",
                "args_base64": base64.b64encode(b"{}").decode(),
            },
        }
    ).encode()
    request = urllib.request.Request(RPC, data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode())
        raw = bytes(payload["result"]["result"]).decode()
        state = json.loads(raw)
    except Exception:
        return None
    return [match["match_id"] for match in state.get("matches") or []]


def snapshot_match_ids() -> list[str]:
    if not SNAPSHOT.exists():
        return []
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return [match["match_id"] for match in document.get("chain", {}).get("matches", [])]


def load_public() -> dict:
    if PUBLIC.exists():
        return json.loads(PUBLIC.read_text(encoding="utf-8"))
    return {}


def save_public(document: dict) -> None:
    PUBLIC.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def annotate(rows: list[dict], now: datetime) -> list[dict]:
    annotated = []
    for row in rows:
        kickoff = parse_iso(row["kickoff"])
        ahead_ms = int((kickoff - now).total_seconds() * 1000)
        eligible = 0 < ahead_ms <= MAX_AHEAD_MS and row["home_name_ok"] and row["away_name_ok"]
        annotated.append({**row, "ahead_ms": ahead_ms, "eligible_now": eligible})
    return annotated


def execute(rows: list[dict], account_id: str) -> None:
    if not account_id.endswith(".testnet"):
        raise SystemExit("refusing a non-testnet account")
    import importlib.util

    spec = importlib.util.spec_from_file_location("testnet_demo", ROOT / "scripts" / "testnet_demo.py")
    if spec is None or spec.loader is None:
        raise SystemExit("could not load scripts/testnet_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    document = load_public()
    bucket = document.setdefault(
        "weekend_plan",
        {"window_from": DEFAULT_FROM, "window_until": DEFAULT_UNTIL, "matches": []},
    )
    saved = {row["match_id"]: row for row in bucket.get("matches") or []}
    state = demo.view(account_id, "export_public")
    by_id = {match["match_id"]: match for match in state["matches"]}
    new_ids = [row["match_id"] for row in rows if row["match_id"] not in by_id]
    if len(by_id) + len(new_ids) > MAX_MATCHES:
        raise SystemExit(f"match cap is {MAX_MATCHES}")
    now_ms = demo.block_time_ms()
    for row in rows:
        if not row["home_name_ok"] or not row["away_name_ok"]:
            raise SystemExit(f"team name rejected by the contract charset: {row['match_id']}")
        kickoff_ms = demo.iso_to_ms(row["kickoff"])
        if kickoff_ms <= now_ms:
            print(f"skip {row['match_id']} kickoff is not in the future")
            continue
        if kickoff_ms - now_ms > MAX_AHEAD_MS:
            print(f"skip {row['match_id']} kickoff is more than 21 days ahead")
            continue
        chain = by_id.get(row["match_id"])
        record = saved.get(row["match_id"]) or {
            "match_id": row["match_id"],
            "home": row["home"],
            "away": row["away"],
            "kickoff": row["kickoff"],
            "home_bps": row["home_bps"],
            "draw_bps": row["draw_bps"],
            "away_bps": row["away_bps"],
            "source_url": row["source_url"],
            "season": row["season"],
            "competition": row["competition"],
            "model": {
                "home_xg": row["home_xg"],
                "away_xg": row["away_xg"],
                "prior_matches": row["prior_matches"],
                "model": row["model"],
            },
            "transactions": {},
        }
        txs = record.setdefault("transactions", {})
        if chain is None:
            txs["register"] = demo.call(
                account_id,
                "register_match",
                {
                    "match_id": row["match_id"],
                    "competition": row["competition"],
                    "season": row["season"],
                    "home": row["home"],
                    "away": row["away"],
                    "source_kickoff_ms": kickoff_ms,
                    "deadline_ms": kickoff_ms,
                    "source_url": row["source_url"],
                },
            )
            saved[row["match_id"]] = record
            bucket["matches"] = list(saved.values())
            save_public(document)
            chain = demo.view(account_id, "get_match", {"match_id": row["match_id"]})
            by_id[row["match_id"]] = chain
        if not chain.get("commitment"):
            if not record.get("salt"):
                import secrets

                record["salt"] = secrets.token_hex(16)
                record["commitment"] = demo.commitment_hex(
                    row["match_id"], row["home_bps"], row["draw_bps"], row["away_bps"], record["salt"]
                )
                saved[row["match_id"]] = record
                bucket["matches"] = list(saved.values())
                save_public(document)
            txs["commit"] = demo.call(
                account_id,
                "commit_forecast",
                {"match_id": row["match_id"], "commitment": record["commitment"]},
            )
            saved[row["match_id"]] = record
            bucket["matches"] = list(saved.values())
            save_public(document)
            chain = demo.view(account_id, "get_match", {"match_id": row["match_id"]})
        if not chain.get("reveal"):
            if not record.get("salt"):
                raise SystemExit(f"{row['match_id']} is committed and this script has no saved salt")
            txs["reveal"] = demo.call(
                account_id,
                "reveal_forecast",
                {
                    "match_id": row["match_id"],
                    "home_bps": row["home_bps"],
                    "draw_bps": row["draw_bps"],
                    "away_bps": row["away_bps"],
                    "salt": record["salt"],
                },
            )
            saved[row["match_id"]] = record
            bucket["matches"] = list(saved.values())
            save_public(document)
    state = demo.view(account_id, "export_public")
    if SNAPSHOT.exists():
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        snapshot["chain"] = state
        snapshot["updated_at"] = now_iso()
        snapshot.setdefault("transactions", {}).setdefault("matches", {})
        snapshot.setdefault("model_notes", {})
        for record in saved.values():
            snapshot["transactions"]["matches"][record["match_id"]] = record.get("transactions", {})
            if record.get("model"):
                snapshot["model_notes"][record["match_id"]] = record["model"]
        SNAPSHOT.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        print(f"updated {SNAPSHOT}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print or commit the next Bundesliga matchday. Default sends nothing.")
    parser.add_argument("--execute", action="store_true", help="Sign register, commit, and reveal on testnet. Omit to print only.")
    parser.add_argument("--from-kickoff", default=DEFAULT_FROM, dest="start")
    parser.add_argument("--until-kickoff", default=DEFAULT_UNTIL, dest="until")
    parser.add_argument("--refresh", action="store_true", help="Fetch OpenLigaDB before planning")
    args = parser.parse_args()
    if args.refresh:
        matches = refresh_cache(now_iso())
    elif CACHE.exists():
        matches = load_cache()
    else:
        matches = refresh_cache(now_iso())
    document = load_public()
    account_id = document.get("account_id") or "crunch-oracle-8fa9a2b8.testnet"
    live_ids = view_match_ids(account_id)
    skip_ids = live_ids if live_ids is not None else snapshot_match_ids()
    from oracle.select import select_kickoff_window

    empty = select_kickoff_window(matches, EMPTY_WEEKEND_FROM, EMPTY_WEEKEND_UNTIL)
    rows = annotate(plan_forecasts(matches, args.start, args.until, skip_ids), datetime.now(timezone.utc))
    plan = {
        "execute": bool(args.execute),
        "transactions_sent": 0,
        "account_id": account_id,
        "skip_source": "rpc" if live_ids is not None else "snapshot",
        "already_on_chain": skip_ids,
        "calendar_weekend_2026_11_14_count": len(empty),
        "window_from": args.start,
        "window_until": args.until,
        "planned": rows,
    }
    print(json.dumps(plan, indent=2))
    if not args.execute:
        print("no transaction sent", file=sys.stderr)
        return
    if not document.get("account_id"):
        raise SystemExit("docs/testnet.json has no account_id")
    if any(not row["eligible_now"] for row in rows):
        raise SystemExit("at least one planned match is outside the contract window; nothing sent")
    execute(rows, document["account_id"])


if __name__ == "__main__":
    main()
