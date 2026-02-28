"""Constants for the Paperless KIplus integration."""

from __future__ import annotations

DOMAIN = "paperless_kiplus"

CONF_COMMAND = "command"
CONF_WORKDIR = "workdir"
CONF_COOLDOWN_SECONDS = "cooldown_seconds"

DEFAULT_COMMAND = "python src/paperless_ai_sorter.py"
DEFAULT_WORKDIR = "/config/paperless-kiplus"
DEFAULT_COOLDOWN_SECONDS = 300

SERVICE_RUN = "run"

ATTR_FORCE = "force"
ATTR_WAIT = "wait"
ATTR_ENTRY_ID = "entry_id"

SIGNAL_STATUS_UPDATED = f"{DOMAIN}_status_updated"
