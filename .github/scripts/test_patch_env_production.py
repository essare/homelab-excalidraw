#!/usr/bin/env python3
"""Tests for patch-env-production.py"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "patch-env-production.py"

SAMPLE = """MODE=\"production\"

VITE_APP_WS_SERVER_URL=https://oss-collab.excalidraw.com

VITE_APP_FIREBASE_CONFIG='{\"apiKey\":\"OLD\",\"projectId\":\"old\"}'

VITE_APP_ENABLE_TRACKING=false
"""


class PatchEnvProductionTests(unittest.TestCase):
    def run_patch(self, env_file: Path, ws: str, firebase: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["WS_SERVER_URL"] = ws
        env["FIREBASE_CONFIGURATION"] = firebase
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(env_file)],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_patches_ws_and_firebase_leaves_other_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.production"
            path.write_text(SAMPLE, encoding="utf-8")
            fb = '{"apiKey":"NEW","projectId":"new-proj"}'
            result = self.run_patch(path, "https://excalidraw-collab.example.com", fb)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "VITE_APP_WS_SERVER_URL=https://excalidraw-collab.example.com",
                text,
            )
            match = re.search(r"^VITE_APP_FIREBASE_CONFIG='(.*)'$", text, flags=re.M)
            self.assertIsNotNone(match)
            self.assertEqual(
                json.loads(match.group(1)),
                {"apiKey": "NEW", "projectId": "new-proj"},
            )
            self.assertIn("VITE_APP_ENABLE_TRACKING=false", text)
            self.assertNotIn("oss-collab.excalidraw.com", text)
            self.assertNotIn('"OLD"', text)

    def test_rejects_empty_ws(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.production"
            path.write_text(SAMPLE, encoding="utf-8")
            result = self.run_patch(path, "", '{"apiKey":"x"}')
            self.assertNotEqual(result.returncode, 0)

    def test_rejects_empty_firebase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.production"
            path.write_text(SAMPLE, encoding="utf-8")
            result = self.run_patch(path, "https://example.com", "")
            self.assertNotEqual(result.returncode, 0)

    def test_rejects_invalid_firebase_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.production"
            path.write_text(SAMPLE, encoding="utf-8")
            result = self.run_patch(path, "https://example.com", "not-json")
            self.assertNotEqual(result.returncode, 0)

    def test_rejects_missing_keys_in_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.production"
            path.write_text("MODE=production\n", encoding="utf-8")
            result = self.run_patch(
                path,
                "https://example.com",
                '{"apiKey":"x"}',
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
