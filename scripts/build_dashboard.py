#!/usr/bin/env python3
"""Copy the static dashboard into dashboard/dist for Cloudflare Pages."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dashboard"
DIST = SOURCE / "dist"
FILES = ("index.html", "styles.css", "app.js", "snapshot.json")
BANNED = ("engine_state", "ranking_parameters", "market_parameters")


def main() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        source = SOURCE / name
        if not source.exists():
            raise SystemExit(f"missing {source}")
        text = source.read_text(encoding="utf-8")
        for token in BANNED:
            if token in text:
                raise SystemExit(f"{name} contains {token}")
        shutil.copyfile(source, DIST / name)
    print(f"wrote {DIST}")


if __name__ == "__main__":
    main()
