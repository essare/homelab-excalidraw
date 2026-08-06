# Homelab Excalidraw Release Sync & Docker Publish Workflow

**Date:** 2026-08-06 (amended same day — see note below)  
**Repo:** [essare/homelab-excalidraw](https://github.com/essare/homelab-excalidraw) (fork of [excalidraw/excalidraw](https://github.com/excalidraw/excalidraw))  
**Status:** Approved design (amended: strategy A)

> **Amendment (strategy A):** A whole-branch review found that merging upstream
> releases into fork `master` (as originally designed below) creates real
> merge-conflict and force-push risk on a recurring cron job, and mixes
> upstream history with fork-only CI files. The approved fix: **do not** merge
> upstream releases into `master`. Instead, build the Docker image directly
> from the **upstream release tag's tree**, and bring in the fork's
> `.github/scripts` patch script via a **second, sparse checkout** of fork
> `master` (path `.ci-fork`, sparse-checkout limited to `.github/scripts`).
> The script is copied into the release tree's `.github/scripts/` and
> `.ci-fork` is deleted before `docker build` so it never pollutes the build
> context (the `Dockerfile` does `COPY . .`). The same release tag is still
> optionally pushed to `origin` (the fork) for provenance/traceability only —
> `master` itself is never touched. Sections below are updated in place to
> reflect this; where "sync into master" language remains it describes the
> **superseded** original design and is kept only for historical context.

## Goal

Automatically detect new upstream Excalidraw GitHub releases on a weekly schedule, build a production Docker image directly from that release tag's tree with homelab-specific Vite env overrides applied **only at build time**, and push the image to a private container registry using the upstream release tag (plus `:latest`). The fork's `master` branch is never merged with upstream history as part of this workflow; the release tag may optionally be pushed to the fork for provenance only.

## Non-goals

- Do not commit or push customized `.env.production` values (WS URL, Firebase config) to git.
- Do not multi-arch build (`linux/amd64` only).
- Do not modify upstream’s existing Docker Hub publish workflow behavior as part of v1 (it remains unused unless someone pushes a `release` branch).
- Do not run e2e collaboration tests in CI.
- Do not override other production env keys (backend URLs, Plus, AI, etc.) in v1.

## Decisions (from brainstorming, amended for strategy A)

| Topic | Choice |
|-------|--------|
| Trigger | Weekly cron + `workflow_dispatch` |
| When to build | Only when a new upstream **non-prerelease** release exists (not every `master` commit) |
| Image tags | Upstream release tag as-is (e.g. `v0.18.1`) **and** `:latest` |
| Image path | `${PRIVATE_CR}/homelab/excalidraw` |
| Build source tree | Upstream release tag tree directly — **no merge into fork `master`** |
| Fork git history | Push matching `v*` tag to `origin` for provenance only; `master` untouched |
| CI script delivery | Second sparse checkout of fork `master` (path `.ci-fork`, `sparse-checkout: .github/scripts`); script copied into release tree, `.ci-fork` deleted before `docker build` |
| Platforms | `linux/amd64` only |
| WS URL | Secret `WS_SERVER_URL` |
| Firebase | Secret `FIREBASE_CONFIGURATION` (raw JSON object) |
| Workflow shape | Single workflow, two jobs (Approach 1) |

## Architecture

**New file:** `.github/workflows/homelab-release.yml`  
Upstream workflows under `.github/workflows/` are left in place; this workflow uses a distinct name to minimize merge conflicts.

### Triggers

```yaml
on:
  schedule:
    - cron: "0 6 * * 0"  # Sundays 06:00 UTC
  workflow_dispatch:
    inputs:
      force:
        description: "Rebuild and push even if this release tag already exists on the fork"
        type: boolean
        default: false
```

### Jobs

#### 1. `detect-and-sync`

Job-level `permissions: contents: write` (needed only to push the provenance tag; the other job stays `contents: read`).

1. Checkout `essare/homelab-excalidraw` (`fetch-depth: 1` is sufficient — no merge/ancestry walk is needed).
2. Resolve **latest stable upstream release tag** via GitHub API (`gh api repos/excalidraw/excalidraw/releases/latest --jq .tag_name`), which already excludes drafts/prereleases.
3. Check whether that tag exists on `origin` (the fork) via `git ls-remote --tags origin`.
4. **Idempotency / force:**
   - Tag on origin, `force=false` → `should_build=false`, `release_tag=""` (no-op).
   - Tag on origin, `force=true` → `should_build=true`, `release_tag=<tag>`; no git sync needed beyond the tag already existing.
   - Tag **not** on origin → fetch the tag object from `upstream` and `git push origin refs/tags/<tag>` (provenance only — **no merge, no checkout of `master`, no `git pull`**), then `should_build=true`, `release_tag=<tag>`.
5. There is no merge step, so there is no merge-conflict failure mode for this job anymore.

Push auth: default `GITHUB_TOKEN`, scoped to `contents: write` on this job only. If branch protection ever blocks pushing the provenance tag, switch to a PAT secret (e.g. `SYNC_TOKEN`) without changing the overall design.

#### 2. `build-and-push`

Runs only when `detect-and-sync` outputs `should_build=true`. Job-level `permissions: contents: read` (no push needed).

1. **First checkout:** `ref: ${{ needs.detect-and-sync.outputs.release_tag }}` at the workspace root — this is the **upstream release tree**, unmodified.
2. **Second checkout:** fork `master`, `path: .ci-fork`, `sparse-checkout: .github/scripts` — pulls in only the fork's CI scripts, without pulling in the rest of fork/upstream history or files.
3. Copy the patch script into the release tree and delete the sparse checkout: `mkdir -p .github/scripts && cp .ci-fork/.github/scripts/patch-env-production.py .github/scripts/ && rm -rf .ci-fork`. This keeps `.ci-fork` out of the Docker build context — the `Dockerfile` does `COPY . .`, so any leftover `.ci-fork` directory would otherwise be baked into the image.
4. Fail fast if `WS_SERVER_URL` or `FIREBASE_CONFIGURATION` is empty.
5. Patch `.env.production` **in the runner workspace only** by running `python3 .github/scripts/patch-env-production.py .env.production` (see Env override).
6. Log in to the private registry:
   - Registry host: `${{ secrets.PRIVATE_CR }}`
   - Username: `${{ secrets.PRIVATE_CR_USERNAME }}`
   - Password: `${{ secrets.PRIVATE_CR_PASSWORD }}`
7. Build and push with Docker Buildx (single platform `linux/amd64`):
   - `${{ secrets.PRIVATE_CR }}/homelab/excalidraw:${{ release_tag }}`
   - `${{ secrets.PRIVATE_CR }}/homelab/excalidraw:latest`
8. Do **not** commit, stage, or push any changes to `.env.production` — this job checks out a tag tree, not fork history, so there is nothing to push back to regardless.

### Concurrency

```yaml
concurrency:
  group: homelab-release
  cancel-in-progress: false
```

Prevents overlapping cron + manual-dispatch runs from racing on the same provenance tag push; queues rather than cancels so a build already underway is never killed mid-push.

### Data flow

```
upstream GitHub release (vX.Y.Z)
  → detect-and-sync resolves latest tag, pushes it to origin for provenance (no master merge)
  → build-and-push checks out the upstream release tag tree
  → sparse-checkouts fork master's .github/scripts into .ci-fork, copies script in, deletes .ci-fork
  → patch .env.production in workspace (secrets → file)
  → docker build (yarn build:app:docker bakes VITE_* into static assets)
  → push image tags to PRIVATE_CR/homelab/excalidraw
```

## Env override

Vite production config lives in `.env.production`. The upstream `Dockerfile` runs `yarn build:app:docker` and does **not** accept build-args for these values. Changing the Dockerfile would create a permanent fork delta; instead the release tag tree is used unmodified and the patch script is layered in at build time via the sparse checkout described above.

**Therefore:** CI rewrites two lines in `.env.production` immediately before `docker build`:

| Variable | Source | Notes |
|----------|--------|--------|
| `VITE_APP_WS_SERVER_URL` | `secrets.WS_SERVER_URL` | Full URL string, e.g. `https://excalidraw-collab.essare.me` |
| `VITE_APP_FIREBASE_CONFIG` | `secrets.FIREBASE_CONFIGURATION` | Secret holds raw JSON `{"apiKey":...}`. Workflow writes `VITE_APP_FIREBASE_CONFIG='<json>'` to match existing file quoting. |

All other keys remain upstream defaults.

**Hard rule:** customized env values are used only to build and push the image. They must never be committed or pushed to the fork.

## Secrets

| Name | Purpose |
|------|---------|
| `PRIVATE_CR` | Private registry host (e.g. `registry.ecsvc.dev`) — not hardcoded in YAML |
| `PRIVATE_CR_USERNAME` | Registry username |
| `PRIVATE_CR_PASSWORD` | Registry password/token |
| `WS_SERVER_URL` | Collaboration WebSocket/server URL for production build |
| `FIREBASE_CONFIGURATION` | Firebase config JSON object for room persistence |

Optional later: `SYNC_TOKEN` if `GITHUB_TOKEN` cannot push the provenance tag to `origin`.

## Error handling

| Situation | Behavior |
|-----------|----------|
| No newer upstream release | Success no-op; skip build |
| Empty required secret | Fail before Docker build |
| Docker build fails after provenance tag pushed | Fork tag already pushed; re-run with `force: true` to rebuild/push the image only |
| Registry login/push fails | Fail; same `force` recovery |
| Draft/prerelease as “latest” | Ignored; only stable latest release |

There is no merge-conflict failure mode: `detect-and-sync` never merges into `master`, so there is nothing to force-push or resolve manually.

Notifications: GitHub Actions failure UI/email only (no Slack/webhook in v1).

## Testing / verification

1. Ensure all five secrets are configured; Firebase secret is unquoted raw JSON.
2. Manual `workflow_dispatch` with `force: false`: detects current latest release and builds from its tag tree, or no-op if already tagged on the fork.
3. Pull `${PRIVATE_CR}/homelab/excalidraw:<tag>` and `:latest`; smoke-test that the app uses homelab WS + Firebase (not Excalidraw defaults).
4. Re-run with `force: false` → must no-op.
5. Re-run with `force: true` → rebuild and re-push same tags without touching `master`.

## Success criteria

- Weekly cron and manual dispatch both work.
- Matching `v*` provenance tags land on the fork (`origin`) for processed upstream releases; fork `master` is never merged or force-pushed by this workflow.
- Registry contains `homelab/excalidraw:<upstream-tag>` and `:latest` for each processed release.
- Built image embeds custom WS + Firebase; git history never contains secret-derived env values.

## Implementation notes (for planning)

- Prefer pinned official actions (`actions/checkout`, `docker/setup-buildx-action`, `docker/login-action`, `docker/build-push-action`) consistent with existing workflow pin style in this repo.
- Use a small shell step (or `gh` + `jq`) for release detection and env line replacement; keep logic readable and fail-loud.
- Do not store registry URL, WS URL, or Firebase JSON in the workflow YAML.
- After implementation, document required secrets in a short comment at the top of the workflow file.
