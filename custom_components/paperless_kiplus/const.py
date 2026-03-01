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
CONF_MANAGED_CONFIG_ENABLED = "managed_config_enabled"
CONF_MANAGED_CONFIG_YAML = "managed_config_yaml"
CONF_INPUT_COST_PER_1K_TOKENS_EUR = "input_cost_per_1k_tokens_eur"
CONF_OUTPUT_COST_PER_1K_TOKENS_EUR = "output_cost_per_1k_tokens_eur"

DEFAULT_COMMAND = "python3 /config/custom_components/paperless_kiplus/paperless_ai_sorter.py"
DEFAULT_WORKDIR = "/config"
DEFAULT_COOLDOWN_SECONDS = 300
DEFAULT_METRICS_FILE = "run_metrics.json"
DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_DRY_RUN = False
DEFAULT_ALL_DOCUMENTS = False
DEFAULT_MAX_DOCUMENTS = 0
DEFAULT_MANAGED_CONFIG_ENABLED = True
DEFAULT_MANAGED_CONFIG_YAML = ""
DEFAULT_INPUT_COST_PER_1K_TOKENS_EUR = 0.0004
DEFAULT_OUTPUT_COST_PER_1K_TOKENS_EUR = 0.0016

SERVICE_RUN = "run"

ATTR_FORCE = "force"
ATTR_WAIT = "wait"
ATTR_ENTRY_ID = "entry_id"
ATTR_CONFIG_FILE = "config_file"
ATTR_DRY_RUN = "dry_run"
ATTR_ALL_DOCUMENTS = "all_documents"
ATTR_MAX_DOCUMENTS = "max_documents"

SIGNAL_STATUS_UPDATED = f"{DOMAIN}_status_updated"
