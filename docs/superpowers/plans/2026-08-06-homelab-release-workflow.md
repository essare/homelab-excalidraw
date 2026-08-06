# Homelab Release Sync & Docker Publish Implementation Plan

> **Amendment (2026-08-06, post-review — strategy A):** A whole-branch review
> found the original `detect-and-sync` design (merge upstream release into
> fork `master`, then build from `master`) risked merge conflicts and
> force-push on a recurring cron job. Adopted fix: **do not** merge upstream
> into `master`. `detect-and-sync` now only resolves the latest upstream tag
> and, if missing on `origin`, pushes that tag straight from `upstream` to
> `origin` for provenance (no merge/checkout/pull). `build-and-push` now
> checks out the **upstream release tag tree** directly, then does a second
> sparse checkout of fork `master` (path `.ci-fork`, `sparse-checkout:
> .github/scripts`) to bring in `patch-env-production.py`, copies it into
> `.github/scripts/` in the release tree, and deletes `.ci-fork` before
> `docker build` (the `Dockerfile` does `COPY . .`, so a leftover `.ci-fork`
> would otherwise land in the image). `contents: write` permission is now
> scoped to `detect-and-sync` only; `build-and-push` runs with `contents:
> read`. A `concurrency` group (`homelab-release`, `cancel-in-progress:
> false`) was added to prevent overlapping runs. See the amended design doc
> (`docs/superpowers/specs/2026-08-06-homelab-release-workflow-design.md`)
> for full details. The embedded YAML in Task 2 below reflects the
> **original** (superseded) design and is kept for historical reference only
> — the actual workflow file in the repo follows the amendment above.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GitHub Actions workflow that weekly syncs the latest upstream Excalidraw release into this fork and publishes an amd64 Docker image to the private registry with CI-only env overrides.

**Architecture:** One workflow (`.github/workflows/homelab-release.yml`) with two jobs: `detect-and-sync` (resolve latest stable upstream release, merge/tag into fork `master` when new, honor `force`) and `build-and-push` (patch `.env.production` from secrets in the runner only, build `linux/amd64`, push `:tag` and `:latest`). A small Python helper patches env safely (JSON-friendly) and is unit-tested locally.

**Tech Stack:** GitHub Actions, `gh` + git, Docker Buildx, Python 3 (env patch), existing repo `Dockerfile` / Vite `.env.production`

## Global Constraints

- Never commit or push customized `.env.production` values derived from secrets
- Image path: `${{ secrets.PRIVATE_CR }}/homelab/excalidraw`
- Tags: upstream release tag as-is (e.g. `v0.18.1`) **and** `:latest`
- Platform: `linux/amd64` only
- Schedule: `0 6 * * 0` (Sundays 06:00 UTC) + `workflow_dispatch` with boolean `force`
- Secrets only: `PRIVATE_CR`, `PRIVATE_CR_USERNAME`, `PRIVATE_CR_PASSWORD`, `WS_SERVER_URL`, `FIREBASE_CONFIGURATION`
- Do not hardcode registry host, WS URL, or Firebase JSON in YAML
- Do not modify upstream `publish-docker.yml` / `Dockerfile` in v1
- Never force-push on merge conflicts
- Pin GitHub Actions to the same commit SHAs already used in `.github/workflows/publish-docker.yml` where applicable

---

## File Structure

| File | Responsibility |
|------|----------------|
| `.github/scripts/patch-env-production.py` | Rewrite `VITE_APP_WS_SERVER_URL` and `VITE_APP_FIREBASE_CONFIG` in `.env.production` from env vars; validate Firebase JSON; never touch git |
| `.github/scripts/test_patch_env_production.py` | Local unit tests for the patcher |
| `.github/workflows/homelab-release.yml` | Cron + dispatch workflow: detect/sync upstream release, then build/push image |

No other files are created or modified for v1.

---

### Task 1: Env production patcher script + tests

**Files:**
- Create: `.github/scripts/patch-env-production.py`
- Create: `.github/scripts/test_patch_env_production.py`

**Interfaces:**
- Consumes: env vars `WS_SERVER_URL`, `FIREBASE_CONFIGURATION`; optional CLI arg path to env file (default `.env.production`)
- Produces: in-place updated env file; exit `0` on success; exit non-zero if missing vars, invalid JSON, or missing target keys

- [ ] **Step 1: Write the failing tests**

Create `.github/scripts/test_patch_env_production.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3 .github/scripts/test_patch_env_production.py -v
```

Expected: FAIL (e.g. `FileNotFoundError` / cannot run missing `patch-env-production.py`, or import/script errors)

- [ ] **Step 3: Implement the patcher**

Create `.github/scripts/patch-env-production.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python3 .github/scripts/test_patch_env_production.py -v
```

Expected: all 4 tests `OK`

- [ ] **Step 5: Commit**

```bash
git add .github/scripts/patch-env-production.py .github/scripts/test_patch_env_production.py
git commit -m "$(cat <<'EOF'
feat(ci): add .env.production patcher for homelab Docker builds

EOF
)"
```

---

### Task 2: Create `homelab-release.yml` (detect-and-sync + build-and-push)

**Files:**
- Create: `.github/workflows/homelab-release.yml`
- Consumes: `.github/scripts/patch-env-production.py` from Task 1

**Interfaces:**
- Job `detect-and-sync` outputs: `should_build` (`true`|`false`), `release_tag` (e.g. `v0.18.1` or empty)
- Job `build-and-push` needs: `detect-and-sync.outputs.should_build == 'true'`; uses `release_tag` for image tags

- [ ] **Step 1: Create the workflow file**

Create `.github/workflows/homelab-release.yml` with **exactly** this content:

```yaml
# Homelab: sync latest upstream Excalidraw release → build/push private image.
#
# Required repository secrets:
#   PRIVATE_CR              - registry host (no scheme), e.g. registry.example.com
#   PRIVATE_CR_USERNAME     - registry username
#   PRIVATE_CR_PASSWORD     - registry password/token
#   WS_SERVER_URL           - VITE_APP_WS_SERVER_URL value for production build
#   FIREBASE_CONFIGURATION  - raw JSON object for VITE_APP_FIREBASE_CONFIG (no wrapping quotes)
#
# Env overrides are applied only on the runner before docker build and are NEVER committed.
name: Homelab Release Sync & Publish

on:
  schedule:
    - cron: "0 6 * * 0" # Sundays 06:00 UTC
  workflow_dispatch:
    inputs:
      force:
        description: "Rebuild and push even if this release tag already exists on the fork"
        type: boolean
        default: false

permissions:
  contents: write

jobs:
  detect-and-sync:
    runs-on: ubuntu-latest
    outputs:
      should_build: ${{ steps.plan.outputs.should_build }}
      release_tag: ${{ steps.plan.outputs.release_tag }}
    steps:
      - name: Checkout fork
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
        with:
          fetch-depth: 0
          token: ${{ secrets.GITHUB_TOKEN }}

      - name: Configure git identity
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

      - name: Resolve latest upstream release and sync
        id: plan
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          FORCE: ${{ github.event_name == 'workflow_dispatch' && inputs.force == true }}
        run: |
          set -euo pipefail

          RELEASE_TAG="$(gh api repos/excalidraw/excalidraw/releases/latest --jq .tag_name)"
          if [[ -z "${RELEASE_TAG}" || "${RELEASE_TAG}" == "null" ]]; then
            echo "error: could not resolve upstream latest release tag" >&2
            exit 1
          fi
          echo "Upstream latest release: ${RELEASE_TAG}"

          git remote add upstream https://github.com/excalidraw/excalidraw.git 2>/dev/null || git remote set-url upstream https://github.com/excalidraw/excalidraw.git
          git fetch upstream "refs/tags/${RELEASE_TAG}:refs/tags/upstream-${RELEASE_TAG}"
          # Also fetch the real tag name locally for merge if missing
          if ! git rev-parse -q --verify "refs/tags/${RELEASE_TAG}" >/dev/null; then
            git fetch upstream "refs/tags/${RELEASE_TAG}:refs/tags/${RELEASE_TAG}"
          fi

          TAG_ON_ORIGIN="false"
          if git ls-remote --exit-code --tags origin "refs/tags/${RELEASE_TAG}" >/dev/null 2>&1; then
            TAG_ON_ORIGIN="true"
          fi
          echo "Tag on origin: ${TAG_ON_ORIGIN}; force: ${FORCE}"

          if [[ "${TAG_ON_ORIGIN}" == "true" && "${FORCE}" != "true" ]]; then
            echo "Release ${RELEASE_TAG} already on fork; skipping sync and build."
            echo "should_build=false" >> "${GITHUB_OUTPUT}"
            echo "release_tag=" >> "${GITHUB_OUTPUT}"
            exit 0
          fi

          if [[ "${TAG_ON_ORIGIN}" == "true" && "${FORCE}" == "true" ]]; then
            echo "Force rebuild for existing tag ${RELEASE_TAG}; skipping git sync."
            echo "should_build=true" >> "${GITHUB_OUTPUT}"
            echo "release_tag=${RELEASE_TAG}" >> "${GITHUB_OUTPUT}"
            exit 0
          fi

          git checkout master
          git pull --ff-only origin master

          if git merge-base --is-ancestor "${RELEASE_TAG}" HEAD; then
            echo "master already contains ${RELEASE_TAG}; ensuring tag is pushed."
          else
            if git merge --ff-only "${RELEASE_TAG}"; then
              echo "Fast-forwarded master to ${RELEASE_TAG}"
            else
              git merge --no-ff -m "chore: sync upstream release ${RELEASE_TAG}" "${RELEASE_TAG}"
            fi
            git push origin master
          fi

          if ! git ls-remote --exit-code --tags origin "refs/tags/${RELEASE_TAG}" >/dev/null 2>&1; then
            git push origin "refs/tags/${RELEASE_TAG}"
          fi

          echo "should_build=true" >> "${GITHUB_OUTPUT}"
          echo "release_tag=${RELEASE_TAG}" >> "${GITHUB_OUTPUT}"

  build-and-push:
    needs: detect-and-sync
    if: needs.detect-and-sync.outputs.should_build == 'true'
    runs-on: ubuntu-latest
    steps:
      - name: Checkout synced tree
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
        with:
          ref: ${{ needs.detect-and-sync.outputs.release_tag }}
          fetch-depth: 1

      - name: Validate required secrets
        env:
          WS_SERVER_URL: ${{ secrets.WS_SERVER_URL }}
          FIREBASE_CONFIGURATION: ${{ secrets.FIREBASE_CONFIGURATION }}
          PRIVATE_CR: ${{ secrets.PRIVATE_CR }}
          PRIVATE_CR_USERNAME: ${{ secrets.PRIVATE_CR_USERNAME }}
          PRIVATE_CR_PASSWORD: ${{ secrets.PRIVATE_CR_PASSWORD }}
        run: |
          set -euo pipefail
          missing=0
          for name in WS_SERVER_URL FIREBASE_CONFIGURATION PRIVATE_CR PRIVATE_CR_USERNAME PRIVATE_CR_PASSWORD; do
            if [[ -z "${!name}" ]]; then
              echo "error: secret ${name} is empty or not set" >&2
              missing=1
            fi
          done
          exit "${missing}"

      - name: Patch .env.production for build (workspace only)
        env:
          WS_SERVER_URL: ${{ secrets.WS_SERVER_URL }}
          FIREBASE_CONFIGURATION: ${{ secrets.FIREBASE_CONFIGURATION }}
        run: |
          set -euo pipefail
          python3 .github/scripts/patch-env-production.py .env.production
          # Prove we did not stage anything for commit (safety check)
          if ! git diff --quiet -- .env.production; then
            echo "env file patched in workspace (expected; will NOT be committed)"
          fi
          git status --porcelain
          # Explicitly do not git add / commit / push

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f # v3

      - name: Login to private registry
        uses: docker/login-action@465a07811f14bebb1938fbed4728c6a1ff8901fc # v2
        with:
          registry: ${{ secrets.PRIVATE_CR }}
          username: ${{ secrets.PRIVATE_CR_USERNAME }}
          password: ${{ secrets.PRIVATE_CR_PASSWORD }}

      - name: Build and push
        uses: docker/build-push-action@ca052bb54ab0790a636c9b5f226502c73d547a25 # v5
        with:
          context: .
          push: true
          platforms: linux/amd64
          tags: |
            ${{ secrets.PRIVATE_CR }}/homelab/excalidraw:${{ needs.detect-and-sync.outputs.release_tag }}
            ${{ secrets.PRIVATE_CR }}/homelab/excalidraw:latest
```

- [ ] **Step 2: Validate YAML parses**

Run:

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/homelab-release.yml')); print('YAML OK')"
```

If PyYAML is missing:

```bash
python3 -c "import json,urllib.request; print('skip pyyaml')" ; npx --yes yaml-lint .github/workflows/homelab-release.yml 2>/dev/null || ruby -ryaml -e "YAML.load_file('.github/workflows/homelab-release.yml'); puts 'YAML OK'"
```

Expected: `YAML OK` (or equivalent success). Fix syntax errors before continuing.

Optional if `actionlint` is installed:

```bash
actionlint .github/workflows/homelab-release.yml
```

Expected: no errors.

- [ ] **Step 3: Re-run patcher unit tests (regression)**

Run:

```bash
python3 .github/scripts/test_patch_env_production.py -v
```

Expected: all tests `OK`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/homelab-release.yml
git commit -m "$(cat <<'EOF'
feat(ci): add weekly upstream release sync and private image publish

EOF
)"
```

---

### Task 3: Manual verification runbook (secrets + first Actions run)

**Files:**
- No code changes required unless verification finds a bug

**Interfaces:**
- Consumes: workflow from Task 2; repo secrets listed in Global Constraints

- [ ] **Step 1: Confirm secrets in GitHub**

In `essare/homelab-excalidraw` → Settings → Secrets and variables → Actions, verify these exist and are non-empty:

| Secret | Expected shape |
|--------|----------------|
| `PRIVATE_CR` | host only, e.g. `registry.ecsvc.dev` |
| `PRIVATE_CR_USERNAME` | registry user |
| `PRIVATE_CR_PASSWORD` | registry password/token |
| `WS_SERVER_URL` | `https://…` (no quotes) |
| `FIREBASE_CONFIGURATION` | raw JSON object `{"apiKey":...}` with **no** surrounding single quotes |

- [ ] **Step 2: Push branch (if not yet on origin) and run workflow_dispatch**

```bash
git push -u origin HEAD
gh workflow run "Homelab Release Sync & Publish" --ref master -f force=false
gh run watch
```

Expected outcomes:

- If fork does not yet have the latest upstream release tag: `detect-and-sync` merges/pushes, `build-and-push` runs and pushes images.
- If tag already exists on fork: `detect-and-sync` no-op, `build-and-push` skipped.

- [ ] **Step 3: Verify images**

```bash
# Login using the same credentials as CI (do not paste secrets into shell history if avoidable)
docker pull "${PRIVATE_CR}/homelab/excalidraw:${RELEASE_TAG}"
docker pull "${PRIVATE_CR}/homelab/excalidraw:latest"
docker image inspect "${PRIVATE_CR}/homelab/excalidraw:${RELEASE_TAG}" --format '{{.Id}}'
```

Expected: both tags exist; `:latest` points at the just-built image for that run.

Smoke-test by running the container and confirming the app loads; collaboration should target `WS_SERVER_URL` / your Firebase (not Excalidraw defaults). Exact UI check is manual.

- [ ] **Step 4: Verify git was not polluted with secrets**

```bash
git fetch origin
git show origin/master:.env.production | grep VITE_APP_WS_SERVER_URL
git show origin/master:.env.production | grep VITE_APP_FIREBASE_CONFIG
```

Expected: still upstream Excalidraw defaults (not your secret values).

- [ ] **Step 5: Idempotency and force**

```bash
gh workflow run "Homelab Release Sync & Publish" --ref master -f force=false
# expect no-op / skipped build

gh workflow run "Homelab Release Sync & Publish" --ref master -f force=true
# expect build-and-push runs again for same tag
```

- [ ] **Step 6: Commit only if verification required fixes**

If bugs were found, fix in follow-up commits with messages like `fix(ci): …`. If verification passed with no code changes, no commit.

---

## Spec coverage checklist (plan self-review)

| Spec requirement | Task |
|------------------|------|
| Weekly cron + workflow_dispatch | Task 2 |
| Only on new stable upstream release | Task 2 (`gh api …/releases/latest`, tag-on-origin check) |
| Tags `v*` + `:latest` | Task 2 build-and-push |
| Image `${PRIVATE_CR}/homelab/excalidraw` | Task 2 |
| Sync fork master + push tag | Task 2 detect-and-sync |
| amd64 only | Task 2 `platforms: linux/amd64` |
| Secrets for WS + Firebase + registry | Task 2 + Task 3 |
| Env patch CI-only, never commit | Task 1 script + Task 2 explicit non-commit + Task 3 git show check |
| `force` rebuild without re-sync | Task 2 |
| No-op when tag exists | Task 2 |
| Fail on merge conflict / no force-push | Task 2 (`git merge` without `-f`) |
| Leave upstream docker workflows alone | No task modifies them |
| Action pins like existing publish-docker | Task 2 uses same SHAs |

**Placeholder scan:** none remaining.  
**Type/output consistency:** `should_build` / `release_tag` names match between jobs.
