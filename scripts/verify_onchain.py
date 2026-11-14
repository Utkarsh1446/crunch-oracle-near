#!/usr/bin/env python3
"""Recompute commitments and Brier scores from the public NEAR export.

Does not read data/engine_state.json. Does not sign a transaction.

    python3 scripts/verify_onchain.py
    python3 scripts/verify_onchain.py --snapshot dashboard/snapshot.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oracle.commit import audit_public_state

ROOT = Path(__file__).resolve().parents[1]
RPC = "https://rpc.testnet.near.org"
DEFAULT_ACCOUNT = "crunch-oracle-8fa9a2b8.testnet"


def fetch_export(account_id: str) -> dict:
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
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = json.loads(response.read().decode())
    if "error" in payload:
        raise RuntimeError("rpc error")
    raw = bytes(payload["result"]["result"]).decode()
    return json.loads(raw)


def load_snapshot(path: Path) -> tuple[str, dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return document.get("contract_id") or DEFAULT_ACCOUNT, document["chain"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute Brier scores and commitment hashes from public state")
    parser.add_argument("--snapshot", type=Path, help="Read this snapshot instead of testnet RPC")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    args = parser.parse_args()
    if args.snapshot:
        account_id, state = load_snapshot(args.snapshot)
        source = str(args.snapshot)
    else:
        account_id = args.account
        state = fetch_export(account_id)
        source = RPC
    report = audit_public_state(state)
    report["account_id"] = account_id
    report["source"] = source
    print(json.dumps(report, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
