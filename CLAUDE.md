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

## Conventions

- Conventional Commits; single version bump as the last commit before merge.
- Target Platinum quality scale — see `custom_components/pineapple_core/quality_scale.yaml`.
- ruff + pyright (standard) clean; Python floor tracks HA's current minimum.
