# Release Process

Releases are **automated with [semantic-release](https://semantic-release.gitbook.io/)**. You no longer hand-pick a version or write release notes — merging Conventional-Commit changes to `main` does it.

## How it works

On every push to `main`, `.github/workflows/release.yml` runs:

1. **`version` job** (Ubuntu) — semantic-release reads every commit since the last `v*` tag and decides the next version from the commit types (see mapping below). If a release is warranted it creates and pushes a `vX.Y.Z` tag and generates the release notes. If nothing user-facing changed, it is a no-op and the workflow ends.
2. **Publish** — exactly one downstream job creates the GitHub release, chosen by *which files changed* since the previous tag:
   - **`release-ha`** — only `custom_components/` changed → fast tag + GitHub release, **no build** (HACS bundles the integration straight from the tag, so no firmware/frontend/flash artifacts are needed).
   - **`release-full`** — anything else changed → builds the frontend, firmware for **all** `*-release` board environments, and the cross-platform flash tool, then publishes via **GoReleaser** with the firmware packs and `openapi.yaml` attached.

Before tagging, semantic-release stamps the new version into `custom_components/openneato/manifest.json` (`@semantic-release/exec`) and commits it to `main` as `chore(release): X.Y.Z [skip ci]` (`@semantic-release/git`); the tag points at that commit, so the HA integration always reports the same version as the release. **Never edit the manifest version by hand** — it is overwritten on the next release.

semantic-release itself never creates the GitHub *release* (it only pushes the tag), so there is no double-publish — whichever publish job runs owns the release.

## Version bump mapping

Configured in `.releaserc.json` (Conventional Commits preset). Versioning is standard `major.minor.patch` semver.

| Commit type | Bump | In release notes |
|-------------|------|------------------|
| `fix:`, `perf:` | patch | Bug Fixes / Improvements |
| `feat:`, `enhance:` | minor | New Features / Improvements |
| `BREAKING CHANGE:` footer or `!` | major | Breaking Changes |
| `chore:`, `docs:`, `ci:`, `build:`, `refactor:`, `style:`, `test:` | none | hidden |

So a release is cut only when at least one `feat`/`enhance`/`fix`/`perf` (or a breaking change) has landed since the last tag. Write commit messages accordingly — the changelog quality is now a direct function of commit message quality.

## Prereleases

Unchanged — comment `/prerelease` on a PR (collaborators only) or run `gh workflow run prerelease.yml -r <branch>`. See `prerelease.yml`. This builds full artifacts from a PR branch for testing before merge and does not interact with semantic-release.

## Manual fallbacks

Both manual workflows remain available via **Actions → Run workflow** (`workflow_dispatch`):

- **`release.yml`** — running it manually just forces a semantic-release check (no-op if there are no releasable commits).
- **`release-ha.yml`** — hand-tuned HA-only release with custom tag + notes; it validates that only `custom_components/` changed since the last tag.

## One-time migration: seed a semver tag

semantic-release determines the current version from git tags that are **valid semver** and match `vX.Y.Z`. The historical tags here are mostly two-part (`v1.9`, `v1.11`), which are **not** valid semver, so semantic-release will ignore them and would otherwise restart numbering far below the current release.

Before the first automated release, seed a valid-semver tag at the current latest release commit so versioning continues forward:

```bash
# point a 3-part tag at the same commit as the current latest release (v1.11)
git tag -a v1.11.0 "$(git rev-list -n1 v1.11)" -m "Seed semver baseline for semantic-release"
git push origin v1.11.0
```

After that, the next `feat`/`enhance` merge yields `v1.12.0`, the next `fix` yields `v1.11.1`, and a breaking change yields `v2.0.0`. The old two-part tags can stay; they are simply ignored.

## Troubleshooting

- **No release was cut after merging** — check the commit types; only `feat`/`enhance`/`fix`/`perf`/breaking trigger one. Inspect the `version` job log for the semantic-release analysis.
- **Version went backwards / restarted low** — the seed tag (above) was not created. Add `vX.Y.Z` at the latest release commit and re-run.
- **Full build ran for a HA-only change (or vice-versa)** — check the `Classify change scope` step in the `version` job; it diffs `<prev-tag>..HEAD` against `custom_components/`.
- **GoReleaser fails on dirty tree** — the firmware/frontend build must leave a clean working tree (web_assets.h regeneration is deterministic). Verify `npm run build` locally produces no diff at the tag.
- **PlatformIO build fails** — ensure the `*-release` environments build locally first (`pio run -e c3-release`).
