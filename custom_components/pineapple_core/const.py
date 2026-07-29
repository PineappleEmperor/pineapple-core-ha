"""Constants for the Pineapple Core integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "pineapple_core"

# Config entry data (set once, in the config flow)
CONF_BASE_URL = "base_url"
CONF_API_TOKEN = "api_token"  # noqa: S105 — a config-key name, not a secret value
CONF_NOTIFY_TARGET = "notify_target"

# Options (tunable after setup)
CONF_POLL_INTERVAL = "poll_interval"
CONF_WINDOW_HOURS = "window_hours"

# Defaults
DEFAULT_NOTIFY_TARGET = "mobile_app_korat"
DEFAULT_POLL_INTERVAL = timedelta(minutes=5)
DEFAULT_WINDOW_HOURS = 12

# Core REST endpoints (relative to base_url)
API_UPCOMING = "/api/reminders/upcoming"
API_ACK = "/api/reminders/ack"

# How long a single upcoming fetch may take before we treat Core as unreachable.
REQUEST_TIMEOUT = 15
