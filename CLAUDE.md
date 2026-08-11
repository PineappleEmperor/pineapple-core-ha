# pineapple-core-ha

Home Assistant custom integration that delivers [Pineapple Core](https://github.com/PineappleEmperor/pineapple-core)
reminders locally: it polls Core's `/api/reminders/upcoming`, schedules each with
`async_track_point_in_time`, fires the notify service on time (from the cached queue,
so a Core blip at firing time doesn't drop it), then acks back to Core.

## AI sessions

Before writing or modifying integration code (config flow, platforms, manifest,
coordinator, services…), invoke the `ha-integration` skill. Re-invoke it after any
`/compact`, since compaction can drop the skill's guidance from context.

## Architecture

- `api.py` — thin authed client for `/upcoming` + `/ack` (uses HA's shared aiohttp session).
- `coordinator.py` — polls the queue, (re)arms one native fire callback per reminder,
  delivers + acks. Locally remembers fired tags so a failed ack can't double-fire.
- `config_flow.py` — base URL + token + notify target; options for poll interval + window; reauth.
- `sensor.py` / `binary_sensor.py` — diagnostics (queue size, next reminder, Core reachable).

## Releasing

**Publish the drafted release in the GitHub UI. Do NOT push a `v*.*.*` tag.**

`hacs.json` sets `zip_release: true`, so HACS installs the `pineapple_core.zip`
asset and fails with `Could not download` when it's missing. That asset is built by
`release.yml`, which triggers on `release: published` — and GitHub **suppresses**
that event for a release created by `GITHUB_TOKEN`. Pushing a tag runs
`semantic_release.yml`, which does exactly that, so `release.yml` never fires and
the release ships with no asset. Both v0.4.0 and the first v0.4.1 were published
this way and had to be redone.

1. Merge the PR (its last commit bumps `manifest.json`).
2. Release Drafter has a draft ready on push to main — check its tag matches the
   manifest version, since the version-resolver reads PR labels and a `feat:` PR
   drafts a minor bump.
3. Publish it from the UI. A human token fires `release: published`, `release.yml`
   builds the zip, and the tag is created at the target commit.
4. Verify: `gh release view vX.Y.Z --json assets -q '[.assets[].name]'` →
   `["pineapple_core.zip"]`.

No draft to publish (deleted, or consumed by an earlier release)? Create one, then
publish it in the UI — `--target` needs a branch or a full SHA, not an abbreviated one:

```
gh release create vX.Y.Z --draft --target main --generate-notes --title vX.Y.Z
```

## Conventions

- Conventional Commits; single version bump as the last commit before merge.
- **No AI-attribution trailers.** This repo is human-attributed (see the README
  disclaimer): commits must NOT carry `Co-Authored-By: Claude`, `Claude-Session`,
  "Generated with Claude", or a 🤖 trailer. The `.githooks/commit-msg` hook enforces
  this — enable it once per clone with `git config core.hooksPath .githooks`.
- Target Platinum quality scale — see `custom_components/pineapple_core/quality_scale.yaml`.
- ruff + pyright (standard) clean; Python floor tracks HA's current minimum.
