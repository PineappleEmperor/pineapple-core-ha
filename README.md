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
  reminder*, and *Core reachable*.

## Setup

1. In Core → **Settings → API access**, mint an `api:full` service token.
2. In Home Assistant → **Settings → Devices & Services → Add Integration → Pineapple Core**.
3. Enter your Core **base URL**, the **token**, and the **notify service** to fire
   (e.g. `mobile_app_your_phone`).
4. Tune the **poll interval** and **look-ahead window** any time via the integration's
   *Configure* (options).

## How delivery works

- Core materialises upcoming reminders (habits, todos, plants, meds) with fresh action
  tokens and serves them at `GET /api/reminders/upcoming`.
- This integration schedules + fires them locally and posts `POST /api/reminders/ack`.
- Core's own worker firing is disabled once delivery is handed to Home Assistant.

## Status

Early development (0.1.0). See `custom_components/pineapple_core/quality_scale.yaml` for
the road to Platinum.
