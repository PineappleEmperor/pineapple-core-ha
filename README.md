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

- A service device **named after your Core instance** — the first label of its base URL,
  so `https://budgets.example.com` becomes *Budgets* — with diagnostics: *Upcoming
  reminders*, *Next reminder*, *Mirrored to Core*, and *Core reachable*.

The inbound webhook URL (the value to paste into Core's `HA_WEBHOOK_URL` — see the cutover
below) is **not** a sensor: a cloudhook URL is a secret, so it is logged at DEBUG on the
`custom_components.pineapple_core` logger instead.

## Setup

1. In Core → **Settings → API access**, mint an `api:full` service token.
2. In Home Assistant → **Settings → Devices & Services → Add Integration → Pineapple Core**.
3. Enter your Core **base URL**, the **token**, and the **notify service** to fire
   (e.g. `mobile_app_your_phone`).
4. Tune the **poll interval**, **look-ahead window**, and **entities to mirror to Core**
   any time via the integration's *Configure* (options).

## Multiple Core instances

Add the integration once per Core base URL — `core.example.com` and
`budgets.example.com` can run side by side. Each entry keeps its own token, notify
target, poll cadence, mirror list, and inbound webhook, and they stay out of each other's
way:

- **Names** come from the first host label, so the two devices are *Core* and *Budgets*
  (and their entities `sensor.core_next_reminder`, `sensor.budgets_next_reminder`).
- **Notification tags** are prefixed with the same label (`core_bins-2026-08-01`), so two
  instances can share one notify target without one's notification replacing the other's.
- **Tapped actions** are forwarded only by the entry that sent the notification — the
  companion app's tap event is global, but a Core is never handed another's action token.

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

The mirror handles **both** halves of the old rest_commands:
- **numeric** state (`input_number` done-state) → `{entity, value}` — logs the linked habit/todo.
- **date** state or a `next_collection` attribute → `{entity, next_at}` — Ocado's ISO deadline
  is read from the state; UKBinCollectionData's `DD/MM/YYYY` `next_collection` attribute is read
  and normalized to ISO. So `core_helper_value` **and** `core_helper_schedule` both retire.

**Cutover order:** install the latest → copy the webhook URL into Core's `HA_WEBHOOK_URL`
(both surfaces) → add the bin/Ocado entities under *Entities to mirror to Core* → delete the
three bridge automations **and** the two `core_helper_*` rest_commands + their trigger automations.

## Status

Delivery + sole-bridge complete (0.3.0). See `custom_components/pineapple_core/quality_scale.yaml`
for the road to Platinum.
