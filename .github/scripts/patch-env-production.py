#!/usr/bin/env python3
"""Patch Excalidraw .env.production WS + Firebase lines for CI Docker builds.

Reads WS_SERVER_URL and FIREBASE_CONFIGURATION from the environment.
FIREBASE_CONFIGURATION must be a raw JSON object (no surrounding quotes).
Writes VITE_APP_FIREBASE_CONFIG='<json>' to match upstream file style.

Does not run git. Intended for ephemeral CI workspaces only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def main() -> int:
    env_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env.production")
    ws = os.environ.get("WS_SERVER_URL", "").strip()
    firebase_raw = os.environ.get("FIREBASE_CONFIGURATION", "").strip()

    if not ws:
        print("error: WS_SERVER_URL is empty", file=sys.stderr)
        return 1
    if not firebase_raw:
        print("error: FIREBASE_CONFIGURATION is empty", file=sys.stderr)
        return 1

    try:
        parsed = json.loads(firebase_raw)
    except json.JSONDecodeError as exc:
        print(f"error: FIREBASE_CONFIGURATION is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(parsed, dict):
        print("error: FIREBASE_CONFIGURATION must be a JSON object", file=sys.stderr)
        return 1

    # Canonical compact JSON (preserves secret content; stable quoting for .env)
    firebase_json = json.dumps(parsed, separators=(",", ":"))
    if "'" in firebase_json:
        print(
            "error: Firebase JSON contains single quotes; refusing to write .env line",
            file=sys.stderr,
        )
        return 1

    if not env_path.is_file():
        print(f"error: env file not found: {env_path}", file=sys.stderr)
        return 1

    text = env_path.read_text(encoding="utf-8")

    if not re.search(r"^VITE_APP_WS_SERVER_URL=.*$", text, flags=re.M):
        print("error: VITE_APP_WS_SERVER_URL not found in env file", file=sys.stderr)
        return 1
    if not re.search(r"^VITE_APP_FIREBASE_CONFIG=.*$", text, flags=re.M):
        print("error: VITE_APP_FIREBASE_CONFIG not found in env file", file=sys.stderr)
        return 1

    text = re.sub(
        r"^VITE_APP_WS_SERVER_URL=.*$",
        f"VITE_APP_WS_SERVER_URL={ws}",
        text,
        count=1,
        flags=re.M,
    )
    text = re.sub(
        r"^VITE_APP_FIREBASE_CONFIG=.*$",
        f"VITE_APP_FIREBASE_CONFIG='{firebase_json}'",
        text,
        count=1,
        flags=re.M,
    )

    env_path.write_text(text, encoding="utf-8")
    print(f"patched {env_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
