# Homelab Excalidraw Release Sync & Docker Publish Workflow

**Date:** 2026-08-06  
**Repo:** [essare/homelab-excalidraw](https://github.com/essare/homelab-excalidraw) (fork of [excalidraw/excalidraw](https://github.com/excalidraw/excalidraw))  
**Status:** Approved design

## Goal

Automatically detect new upstream Excalidraw GitHub releases on a weekly schedule, sync them into this fork’s `master`, build a production Docker image with homelab-specific Vite env overrides applied **only at build time**, and push the image to a private container registry using the upstream release tag (plus `:latest`).

## Non-goals

- Do not commit or push customized `.env.production` values (WS URL, Firebase config) to git.
- Do not multi-arch build (`linux/amd64` only).
- Do not modify upstream’s existing Docker Hub publish workflow behavior as part of v1 (it remains unused unless someone pushes a `release` branch).
- Do not run e2e collaboration tests in CI.
- Do not override other production env keys (backend URLs, Plus, AI, etc.) in v1.

## Decisions (from brainstorming)

| Topic | Choice |
|-------|--------|
| Trigger | Weekly cron + `workflow_dispatch` |
| When to build | Only when a new upstream **non-prerelease** release exists (not every `master` commit) |
| Image tags | Upstream release tag as-is (e.g. `v0.18.1`) **and** `:latest` |
| Image path | `${PRIVATE_CR}/homelab/excalidraw` |
| Fork git history | Sync: merge release into `master`, push `master` + matching `v*` tag |
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

1. Checkout `essare/homelab-excalidraw` with permissions to push (`contents: write`).
2. Configure git user for CI commits (merge commits only if needed).
3. Add remote `upstream` → `https://github.com/excalidraw/excalidraw.git` and fetch tags/releases metadata.
4. Resolve **latest stable upstream release tag** via GitHub API (`/repos/excalidraw/excalidraw/releases/latest` or equivalent), ignoring drafts and prereleases.
5. **Idempotency / force:**
   - If the fork already has that git tag and `force` is false → set `should_build=false` and exit successfully (no-op).
   - If the fork already has that git tag and `force` is true → skip merge/push (already synced); set `should_build=true`, `release_tag=<tag>` so job 2 rebuilds and re-pushes the image only.
6. If the tag is **not** on the fork yet:
   - Merge the upstream tag into local `master` (prefer fast-forward; create a merge commit only if required).
   - Push `master` to `origin`.
   - Create/push the same `v*` tag on `origin`.
   - Set outputs: `should_build=true`, `release_tag=<tag>`.
7. On merge conflict: fail the job; **never** force-push. Operator resolves manually.

Push auth: start with default `GITHUB_TOKEN`. If branch protection later blocks the token, switch to a PAT secret (e.g. `SYNC_TOKEN`) without changing the overall design.

#### 2. `build-and-push`

Runs only when `detect-and-sync` outputs `should_build=true`.

1. Checkout the synced ref (`master` or the release tag — must match the synced upstream release).
2. Patch `.env.production` **in the runner workspace only** (see Env override).
3. Fail fast if `WS_SERVER_URL` or `FIREBASE_CONFIGURATION` is empty.
4. Log in to the private registry:
   - Registry host: `${{ secrets.PRIVATE_CR }}`
   - Username: `${{ secrets.PRIVATE_CR_USERNAME }}`
   - Password: `${{ secrets.PRIVATE_CR_PASSWORD }}`
5. Build and push with Docker Buildx (single platform `linux/amd64`):
   - `${{ secrets.PRIVATE_CR }}/homelab/excalidraw:${{ release_tag }}`
   - `${{ secrets.PRIVATE_CR }}/homelab/excalidraw:latest`
6. Do **not** commit, stage, or push any changes to `.env.production`.

### Data flow

```
upstream GitHub release (vX.Y.Z)
  → detect-and-sync merges into fork master + pushes tag
  → build-and-push checks out synced tree
  → patch .env.production in workspace (secrets → file)
  → docker build (yarn build:app:docker bakes VITE_* into static assets)
  → push image tags to PRIVATE_CR/homelab/excalidraw
```

## Env override

Vite production config lives in `.env.production`. The upstream `Dockerfile` runs `yarn build:app:docker` and does **not** accept build-args for these values. Changing the Dockerfile would create a permanent fork delta and increase merge conflict risk on every sync.

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

Optional later: `SYNC_TOKEN` if `GITHUB_TOKEN` cannot push to `master`.

## Error handling

| Situation | Behavior |
|-----------|----------|
| No newer upstream release | Success no-op; skip build |
| Merge conflict | Fail; no force-push |
| Empty required secret | Fail before Docker build |
| Docker build fails after sync pushed | Fork already updated; re-run with `force: true` to rebuild/push |
| Registry login/push fails | Fail; same `force` recovery |
| Draft/prerelease as “latest” | Ignored; only stable latest release |

Notifications: GitHub Actions failure UI/email only (no Slack/webhook in v1).

## Testing / verification

1. Ensure all five secrets are configured; Firebase secret is unquoted raw JSON.
2. Manual `workflow_dispatch` with `force: false`: sync+build current latest release, or no-op if already tagged.
3. Pull `${PRIVATE_CR}/homelab/excalidraw:<tag>` and `:latest`; smoke-test that the app uses homelab WS + Firebase (not Excalidraw defaults).
4. Re-run with `force: false` → must no-op.
5. Re-run with `force: true` → rebuild and re-push same tags.

## Success criteria

- Weekly cron and manual dispatch both work.
- Fork `master` and matching `v*` tags track upstream stable releases.
- Registry contains `homelab/excalidraw:<upstream-tag>` and `:latest` for each processed release.
- Built image embeds custom WS + Firebase; git history never contains secret-derived env values.

## Implementation notes (for planning)

- Prefer pinned official actions (`actions/checkout`, `docker/setup-buildx-action`, `docker/login-action`, `docker/build-push-action`) consistent with existing workflow pin style in this repo.
- Use a small shell step (or `gh` + `jq`) for release detection and env line replacement; keep logic readable and fail-loud.
- Do not store registry URL, WS URL, or Firebase JSON in the workflow YAML.
- After implementation, document required secrets in a short comment at the top of the workflow file.
