#!/usr/bin/env python3
"""Deploy Crunch Oracle to NEAR testnet and record commit, reveal, and score transactions.

Uses a faucet-funded throwaway account. NEAR Intents is called only with dry=true.
Private keys stay in the near-cli keychain outside this repository.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oracle.commit import commitment_hex
from oracle.model import forecast_match
from oracle.openliga import refresh_cache
from oracle.select import select_demo_matches

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "docs" / "testnet.json"
SNAPSHOT = ROOT / "dashboard" / "snapshot.json"
WASM = ROOT / "contract" / "target" / "wasm32-unknown-unknown" / "release" / "crunch_oracle.wasm"
RPC = "https://rpc.testnet.near.org"
ONE_CLICK = "https://1click.chaindefuser.com/v0/quote"
USDC_ETH = "nep141:eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near"
USDC_ARB = "nep141:arb-0xaf88d065e77c8cc2239327c5edb3a432268e5831.omft.near"
WNEAR = "nep141:wrap.near"
NEAR = os.environ.get("NEAR_CLI", str(Path.home() / ".local/bin/near"))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_ms(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        head, fraction = text.split(".", 1)
        fraction = re.sub(r"[^0-9].*$", "", fraction)[:6]
        text = f"{head}.{fraction}+00:00"
    else:
        text = text + "+00:00"
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def redact(text: str) -> str:
    return re.sub(r"ed25519:[1-9A-HJ-NP-Za-km-z]+", "ed25519:[redacted]", text)


def rpc(method: str, params) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": "crunch", "method": method, "params": params}).encode()
    request = urllib.request.Request(RPC, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = json.loads(response.read().decode())
    if "error" in payload:
        raise RuntimeError(redact(json.dumps(payload["error"])))
    return payload["result"]


def block_time_ms() -> int:
    status = rpc("status", [])
    return iso_to_ms(status["sync_info"]["latest_block_time"])


def view(account_id: str, method: str, args: dict | None = None):
    encoded = base64.b64encode(json.dumps(args or {}).encode()).decode()
    last_error: Exception | None = None
    for _ in range(10):
        try:
            result = rpc(
                "query",
                {
                    "request_type": "call_function",
                    "finality": "optimistic",
                    "account_id": account_id,
                    "method_name": method,
                    "args_base64": encoded,
                },
            )
            raw = bytes(result["result"]).decode()
            return json.loads(raw) if raw else None
        except Exception as error:  # noqa: BLE001 - the RPC lags a fresh transaction
            last_error = error
            time.sleep(1)
    raise RuntimeError(redact(str(last_error)))


def run_near(args: list[str], timeout: int = 180) -> str:
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local/bin") + os.pathsep + env.get("PATH", "")
    completed = subprocess.run(
        [NEAR, *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        cwd=str(ROOT),
    )
    output = redact(completed.stdout + "\n" + completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(output[-4000:])
    return output


def tx_hash(output: str) -> str:
    match = re.search(r"https://explorer\.testnet\.near\.org/transactions/([1-9A-HJ-NP-Za-km-z]+)", output)
    if match:
        return match.group(1)
    match = re.search(r"Transaction ID:\s*([1-9A-HJ-NP-Za-km-z]+)", output)
    if not match:
        raise RuntimeError("no transaction hash in near-cli output:\n" + output[-2000:])
    return match.group(1)


def call(account_id: str, method: str, args: dict, gas: str = "80 Tgas") -> str:
    output = run_near(
        [
            "contract",
            "call-function",
            "as-transaction",
            account_id,
            method,
            "json-args",
            json.dumps(args, separators=(",", ":")),
            "prepaid-gas",
            gas,
            "attached-deposit",
            "0 NEAR",
            "sign-as",
            account_id,
            "network-config",
            "testnet",
            "sign-with-legacy-keychain",
            "send",
        ]
    )
    digest = tx_hash(output)
    print(f"{method} {digest}")
    return digest


def b58encode(data: bytes) -> str:
    alphabet = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    encoded = bytearray()
    while number:
        number, remainder = divmod(number, 58)
        encoded.append(alphabet[remainder])
    encoded.extend(b"1" * zeros)
    return encoded[::-1].decode()


def create_account() -> str:
    """Ask the public testnet faucet for a throwaway account and keep the key outside the repo."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    suffix = secrets.token_hex(4)
    account_id = f"crunch-oracle-{suffix}.testnet"
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key = "ed25519:" + b58encode(public)
    secret_key = "ed25519:" + b58encode(seed + public)
    print(f"creating {account_id}")
    request = urllib.request.Request(
        "https://helper.nearprotocol.com/account",
        data=json.dumps({"newAccountId": account_id, "newAccountPublicKey": public_key}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        faucet = json.loads(response.read().decode())
    status = faucet.get("final_execution_status") or faucet.get("status")
    if status not in {"EXECUTED", "EXECUTED_OPTIMISTIC", "FINAL"} and "receipts_outcome" not in faucet:
        raise RuntimeError("faucet did not return an execution status")
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            rpc(
                "query",
                {"request_type": "view_account", "finality": "optimistic", "account_id": account_id},
            )
            break
        except Exception:
            time.sleep(2)
    else:
        raise RuntimeError(f"{account_id} was not visible after the faucet call")
    key_dir = Path.home() / ".near-credentials" / "testnet"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / f"{account_id}.json"
    key_path.write_text(
        json.dumps({"account_id": account_id, "public_key": public_key, "private_key": secret_key}) + "\n",
        encoding="utf-8",
    )
    key_path.chmod(0o600)
    return account_id


def build_wasm() -> None:
    env = os.environ.copy()
    completed = subprocess.run(
        [
            "cargo",
            "+stable",
            "build",
            "--manifest-path",
            "contract/Cargo.toml",
            "--release",
            "--target",
            "wasm32-unknown-unknown",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(redact(completed.stderr[-4000:]))
    if not WASM.exists():
        raise RuntimeError(f"wasm missing at {WASM}")


def deploy(account_id: str) -> str:
    output = run_near(
        [
            "contract",
            "deploy",
            account_id,
            "use-file",
            str(WASM),
            "with-init-call",
            "new",
            "json-args",
            json.dumps({"owner": account_id, "oracle": account_id}, separators=(",", ":")),
            "prepaid-gas",
            "100 Tgas",
            "attached-deposit",
            "0 NEAR",
            "network-config",
            "testnet",
            "sign-with-legacy-keychain",
            "send",
        ],
        timeout=180,
    )
    return tx_hash(output)


def load_public() -> dict:
    if PUBLIC.exists():
        return json.loads(PUBLIC.read_text(encoding="utf-8"))
    return {}


def save_public(document: dict) -> None:
    PUBLIC.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def build_plan(matches: list[dict], now_ms: int) -> dict:
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    replays, live = select_demo_matches(matches, now)
    if len(replays) != 3 or live is None:
        raise RuntimeError(f"expected 3 replays and 1 upcoming match, got {len(replays)} and {live}")
    deadline = now_ms + 240_000
    chosen = []
    for match in replays:
        forecast = forecast_match(matches, match)
        chosen.append(plan_row(match, forecast, deadline, replay=True))
    live_forecast = forecast_match(matches, live)
    kickoff_ms = iso_to_ms(live["kickoff"])
    if kickoff_ms <= now_ms or kickoff_ms - now_ms > 21 * 24 * 60 * 60 * 1000:
        raise RuntimeError(f"upcoming kickoff is outside the contract window: {live['kickoff']}")
    chosen.append(plan_row(live, live_forecast, kickoff_ms, replay=False))
    return {"as_of": now, "deadline_ms": deadline, "matches": chosen}


def plan_row(match: dict, forecast: dict, deadline_ms: int, replay: bool) -> dict:
    salt = secrets.token_hex(16)
    home_bps = forecast["home_bps"]
    draw_bps = forecast["draw_bps"]
    away_bps = forecast["away_bps"]
    return {
        "match_id": match["id"],
        "competition": match["competition"],
        "season": match["season"],
        "home": match["home"],
        "away": match["away"],
        "source_kickoff_ms": iso_to_ms(match["kickoff"]),
        "kickoff": match["kickoff"],
        "deadline_ms": deadline_ms,
        "source_url": match["source_url"],
        "historical_replay": replay,
        "home_goals": match["home_goals"],
        "away_goals": match["away_goals"],
        "home_bps": home_bps,
        "draw_bps": draw_bps,
        "away_bps": away_bps,
        "salt": salt,
        "commitment": commitment_hex(match["id"], home_bps, draw_bps, away_bps, salt),
        "model": {
            "home_xg": forecast["home_xg"],
            "away_xg": forecast["away_xg"],
            "prior_matches": forecast["prior_matches"],
            "model": forecast["model"],
        },
        "transactions": {},
    }


def ensure_steps(account_id: str, document: dict) -> None:
    plan = document["plan"]
    state = view(account_id, "export_public")
    by_id = {match["match_id"]: match for match in state["matches"]}
    for row in plan["matches"]:
        chain = by_id.get(row["match_id"])
        txs = row.setdefault("transactions", {})
        if chain is None:
            txs["register"] = call(
                account_id,
                "register_match",
                {
                    "match_id": row["match_id"],
                    "competition": row["competition"],
                    "season": row["season"],
                    "home": row["home"],
                    "away": row["away"],
                    "source_kickoff_ms": row["source_kickoff_ms"],
                    "deadline_ms": row["deadline_ms"],
                    "source_url": row["source_url"],
                },
            )
            save_public(document)
            chain = view(account_id, "get_match", {"match_id": row["match_id"]})
        if not chain.get("commitment"):
            txs["commit"] = call(
                account_id,
                "commit_forecast",
                {"match_id": row["match_id"], "commitment": row["commitment"]},
            )
            save_public(document)
            chain = view(account_id, "get_match", {"match_id": row["match_id"]})
        if not chain.get("reveal"):
            txs["reveal"] = call(
                account_id,
                "reveal_forecast",
                {
                    "match_id": row["match_id"],
                    "home_bps": row["home_bps"],
                    "draw_bps": row["draw_bps"],
                    "away_bps": row["away_bps"],
                    "salt": row["salt"],
                },
            )
            save_public(document)
    if block_time_ms() < plan["deadline_ms"]:
        print(f"waiting for replay deadline {plan['deadline_ms']}", flush=True)
        while block_time_ms() < plan["deadline_ms"]:
            time.sleep(5)
        time.sleep(3)
    state = view(account_id, "export_public")
    by_id = {match["match_id"]: match for match in state["matches"]}
    for row in plan["matches"]:
        if not row["historical_replay"]:
            continue
        chain = by_id[row["match_id"]]
        txs = row.setdefault("transactions", {})
        if not chain.get("result"):
            txs["result"] = call(
                account_id,
                "post_result",
                {
                    "match_id": row["match_id"],
                    "home_goals": row["home_goals"],
                    "away_goals": row["away_goals"],
                },
            )
            save_public(document)
        if not chain.get("score"):
            txs["score"] = call(account_id, "score_forecast", {"match_id": row["match_id"]})
            save_public(document)


def quote_hash(fields: dict) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def numeric_text(value: str) -> str:
    match = re.match(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        raise RuntimeError(f"not a decimal amount: {value}")
    text = match.group(0)
    if "." in text:
        whole, fraction = text.split(".", 1)
        text = whole + "." + fraction[:8]
    return text[:32]


def request_quote(origin: str, amount: str) -> dict:
    # Random addresses are quote parameters only. dry=true must not create a deposit.
    body = {
        "dry": True,
        "swapType": "EXACT_INPUT",
        "slippageTolerance": 100,
        "originAsset": origin,
        "depositType": "ORIGIN_CHAIN",
        "destinationAsset": WNEAR,
        "amount": amount,
        "refundTo": "0x" + secrets.token_hex(20),
        "refundType": "ORIGIN_CHAIN",
        "recipient": secrets.token_hex(32),
        "recipientType": "DESTINATION_CHAIN",
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    if body["dry"] is not True:
        raise RuntimeError("refusing a non-dry quote")
    request = urllib.request.Request(
        ONE_CLICK,
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"1Click HTTP {error.code}: {detail}") from error
    if payload.get("quote", {}).get("depositAddress"):
        raise RuntimeError("dry quote returned a deposit address; refusing to continue")
    return payload


def priced_quote() -> dict:
    errors = []
    for origin in (USDC_ETH, USDC_ARB):
        for amount in ("2000000", "5000000", "10000000"):
            try:
                payload = request_quote(origin, amount)
                quote = payload["quote"]
                public = {
                    "dry": True,
                    "origin_asset": origin,
                    "destination_asset": WNEAR,
                    "amount_in": str(quote["amountIn"]),
                    "amount_out": str(quote["amountOut"]),
                    "amount_in_formatted": numeric_text(quote.get("amountInFormatted") or quote["amountIn"]),
                    "amount_out_formatted": numeric_text(quote.get("amountOutFormatted") or quote["amountOut"]),
                    "time_estimate_s": quote.get("timeEstimate"),
                }
                public["quote_hash"] = quote_hash(public)
                print(
                    f"dry quote {public['amount_in_formatted']} -> {public['amount_out_formatted']} wNEAR"
                )
                return public
            except Exception as error:  # noqa: BLE001 - try the next amount/asset
                errors.append(str(error)[:300])
    raise RuntimeError("all dry quotes failed:\n" + "\n".join(errors))


def record_quotes(account_id: str, document: dict) -> list[dict]:
    plan = document["plan"]
    recorded = list(plan.get("quotes") or [])
    done = {quote["buyer_label"] for quote in recorded}
    targets = [row for row in plan["matches"] if row["historical_replay"]][:1]
    targets.append(plan["matches"][-1])
    for row, label in zip(targets, ("replay-reader", "pre-kickoff-reader")):
        if label in done:
            continue
        public = priced_quote()
        digest = call(
            account_id,
            "record_access",
            {
                "buyer_label": label,
                "match_id": row["match_id"],
                "origin_asset": public["origin_asset"],
                "destination_asset": public["destination_asset"],
                "amount_in": public["amount_in"],
                "amount_out": public["amount_out"],
                "amount_in_formatted": public["amount_in_formatted"],
                "amount_out_formatted": public["amount_out_formatted"],
                "quote_hash": public["quote_hash"],
                "dry": True,
            },
        )
        public["buyer_label"] = label
        public["match_id"] = row["match_id"]
        public["transaction"] = digest
        recorded.append(public)
        plan["quotes"] = recorded
        save_public(document)
    plan["quotes"] = recorded
    return recorded


def write_snapshot(account_id: str, plan: dict, deploy_tx: str) -> None:
    chain = view(account_id, "export_public")
    transactions = {"deploy": deploy_tx, "matches": {}, "receipts": {}}
    notes = {}
    for row in plan["matches"]:
        transactions["matches"][row["match_id"]] = row.get("transactions", {})
        notes[row["match_id"]] = row["model"]
    for quote in plan.get("quotes", []):
        transactions["receipts"][quote["buyer_label"]] = quote["transaction"]
    snapshot = {
        "network": "testnet",
        "contract_id": account_id,
        "updated_at": now_iso(),
        "status": "recorded",
        "rpc": RPC,
        "chain": chain,
        "transactions": transactions,
        "model_notes": notes,
        "quotes": plan.get("quotes", []),
        "plan_as_of": plan.get("as_of"),
        "replay_deadline_ms": plan.get("deadline_ms"),
    }
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the Crunch Oracle testnet demo")
    parser.add_argument("--new-account", action="store_true", help="Ignore docs/testnet.json and create a new account")
    args = parser.parse_args()
    document = {} if args.new_account else load_public()
    account_id = document.get("account_id")
    if not account_id:
        account_id = create_account()
        document = {"account_id": account_id, "network": "testnet", "created_at": now_iso()}
        save_public(document)
    build_wasm()
    if not document.get("deploy_tx"):
        document["deploy_tx"] = deploy(account_id)
        save_public(document)
        print(f"deploy {document['deploy_tx']}")
    matches = refresh_cache(now_iso())
    if not document.get("plan"):
        document["plan"] = build_plan(matches, block_time_ms())
        save_public(document)
    ensure_steps(account_id, document)
    save_public(document)
    record_quotes(account_id, document)
    save_public(document)
    write_snapshot(account_id, document["plan"], document["deploy_tx"])
    state = view(account_id, "export_public")
    scored = [match for match in state["matches"] if match.get("score")]
    print(
        json.dumps(
            {
                "account_id": account_id,
                "deploy_tx": document["deploy_tx"],
                "scored": len(scored),
                "receipts": state["receipt_count"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
