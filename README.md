# Pineapple Core — Home Assistant integration

Delivers [Pineapple Core](https://github.com/PineappleEmperor/pineapple-core) reminders
**locally**, so a notification fires on time even if the connection to Core blips at the
firing instant.

Home Assistant polls Core's upcoming-reminders feed every few minutes and schedules each
one natively (`async_track_point_in_time`). When a reminder's time arrives it fires your
notify service from the **cached** queue — no live call to Core needed — then acks the
delivery back so Core doesn't re-queue it. A failed ack never causes a double-fire: HA
remembers what it delivered and retries only the ack.

> [!NOTE]
> **AI assistance:** I'm a programmer; this project is built with AI (Claude, via Claude
> Code) for implementation, code review, and QA — under human direction, guided by my
> [`ha-integration`](https://github.com/PineappleEmperor/pineapple-claude-hacs) skill.
> Architecture and final review are mine; every change is human-reviewed before it merges.

## What it creates

- A **Pineapple Core** service device with diagnostics: *Upcoming reminders*, *Next
  reminder*, *Core reachable*, and *Inbound webhook URL* (the value to paste into Core's
  `HA_WEBHOOK_URL` — see the cutover below).

## Setup

1. In Core → **Settings → API access**, mint an `api:full` service token.
2. In Home Assistant → **Settings → Devices & Services → Add Integration → Pineapple Core**.
3. Enter your Core **base URL**, the **token**, and the **notify service** to fire
   (e.g. `mobile_app_your_phone`).
4. Tune the **poll interval**, **look-ahead window**, and **entities to mirror to Core**
   any time via the integration's *Configure* (options).

## How delivery works

- Core materialises upcoming reminders (habits, todos, plants, meds) with fresh action
  tokens and serves them at `GET /api/reminders/upcoming`.
- This integration schedules + fires them locally, nags on Core's policy, and posts
  `POST /api/reminders/ack`.
- Core's own worker firing is disabled once delivery is handed to Home Assistant.

## Sole bridge — retiring the hand-written automations

From **0.3.0** the integration is the *only* bridge between Core and Home Assistant, so
the old `notify - core`, `callback - core`, and `callback - bins status to core`
automations can be deleted:

- **Core → HA push** (clears, `input_number` sets, the daily digest): the integration
  registers **its own webhook** (and a Nabu Casa cloudhook when cloud is active). Copy the
  *Inbound webhook URL* diagnostic into Core's `HA_WEBHOOK_URL` on **both** the worker and
  Pages. Core keeps POSTing there; the integration routes each `event` to the right service.
- **HA → Core helper mirror** (bin `input_number` done-state → logs the habit): add those
  entities under *Configure → Entities to mirror to Core*. On a numeric change the value is
  POSTed to `/api/integrations/ha/helper`.
- **Action taps** are already forwarded to Core's capability webhook by the integration.

**Cutover order:** install 0.3.0 → copy the webhook URL into Core's `HA_WEBHOOK_URL` (both
surfaces) → add any mirror entities → delete the three automations. Date-pushing
`rest_command`s (bin collection dates, Ocado deadline) stay — they set `next_at` on Core
and aren't part of the retired bridge.

## Status

Delivery + sole-bridge complete (0.3.0). See `custom_components/pineapple_core/quality_scale.yaml`
for the road to Platinum.
