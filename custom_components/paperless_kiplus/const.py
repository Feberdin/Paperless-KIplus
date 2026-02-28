"""Constants for the Paperless KIplus integration."""

from __future__ import annotations

DOMAIN = "paperless_kiplus"

CONF_COMMAND = "command"
CONF_WORKDIR = "workdir"
CONF_COOLDOWN_SECONDS = "cooldown_seconds"
CONF_METRICS_FILE = "metrics_file"
CONF_CONFIG_FILE = "config_file"
CONF_DRY_RUN = "dry_run"
CONF_ALL_DOCUMENTS = "all_documents"
CONF_MAX_DOCUMENTS = "max_documents"

DEFAULT_COMMAND = "python src/paperless_ai_sorter.py"
DEFAULT_WORKDIR = "/config/paperless-kiplus"
DEFAULT_COOLDOWN_SECONDS = 300
DEFAULT_METRICS_FILE = "run_metrics.json"
DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_DRY_RUN = False
DEFAULT_ALL_DOCUMENTS = False
DEFAULT_MAX_DOCUMENTS = 0

SERVICE_RUN = "run"

ATTR_FORCE = "force"
ATTR_WAIT = "wait"
ATTR_ENTRY_ID = "entry_id"
ATTR_CONFIG_FILE = "config_file"
ATTR_DRY_RUN = "dry_run"
ATTR_ALL_DOCUMENTS = "all_documents"
ATTR_MAX_DOCUMENTS = "max_documents"

SIGNAL_STATUS_UPDATED = f"{DOMAIN}_status_updated"
