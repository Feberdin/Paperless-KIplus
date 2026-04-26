#!/usr/bin/env bash
set -Eeuo pipefail

# Purpose:
# - Installiert oder aktualisiert den standalone Paperless KIplus Worker auf Unraid.
# - Legt Verzeichnisse an, erzeugt auf Wunsch eine startfaehige YAML,
#   schreibt einen Compose-Stack und startet den Container.
#
# Input / Output:
# - Input: CLI-Parameter wie Paperless-URL, Tokens, Modell, Port und Appdata-Pfad.
# - Output: persistente Worker-Dateien unter /mnt/user/appdata/..., ein
#   laufender Docker-Container und ein einfacher Health-Check gegen /api/status.
#
# Wichtige Invarianten:
# - Bestehende Konfigurations- und Compose-Dateien werden vor Ueberschreiben gesichert.
# - Ohne gueltige Pflichtwerte wird keine neue YAML geschrieben.
# - Der Worker wird ueber ein veroeffentlichtes GHCR-Image installiert, nicht
#   ueber einen fragilen lokalen Build auf dem NAS.
#
# So debuggt man das Skript:
# - bash -n docker/install-unraid-worker.sh
# - docker logs paperless-kiplus-worker
# - curl http://<unraid>:8787/api/status

SCRIPT_NAME="$(basename "$0")"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

DATA_DIR="/mnt/user/appdata/paperless-kiplus-worker"
CONTAINER_NAME="paperless-kiplus-worker"
IMAGE="ghcr.io/feberdin/paperless-kiplus-worker:latest"
PORT="8787"
BIND_HOST="0.0.0.0"
WORKER_TOKEN=""
PAPERLESS_URL=""
PAPERLESS_TOKEN=""
AI_API_KEY=""
AI_MODEL="gpt-4.1-mini"
AI_BASE_URL="https://api.openai.com/v1"
ENABLE_TAX_ENRICHMENT="false"
TAX_AI_API_KEY=""
TAX_AI_MODEL=""
TAX_AI_BASE_URL=""
SOURCE_CONFIG_FILE=""
REGENERATE_CONFIG="false"
SKIP_START="false"
DRY_RUN_DEFAULT="true"
PROCESS_ONLY_TAG="#NEU"

COMPOSE_STYLE=""

usage() {
  cat <<USAGE
Verwendung:
  $SCRIPT_NAME [Optionen]

Wichtige Standardwerte:
  --data-dir        $DATA_DIR
  --container-name  $CONTAINER_NAME
  --image           $IMAGE
  --port            $PORT

Beispiel Cloud-Setup:
  bash docker/install-unraid-worker.sh \
    --paperless-url http://192.168.178.20:8000 \
    --paperless-token PAPERLESS_TOKEN \
    --ai-api-key OPENAI_KEY \
    --ai-model gpt-4.1-mini

Beispiel mit lokalem Tax-LLM:
  bash docker/install-unraid-worker.sh \
    --paperless-url http://192.168.178.20:8000 \
    --paperless-token PAPERLESS_TOKEN \
    --ai-api-key OPENAI_KEY \
    --ai-model gpt-4.1-mini \
    --enable-tax-enrichment true \
    --tax-ai-api-key dummy \
    --tax-ai-model qwen2.5:7b \
    --tax-ai-base-url http://192.168.178.30:11434/v1

Optionen:
  --data-dir PATH                Appdata-Verzeichnis fuer Config, Logs und State
  --container-name NAME          Docker-Container-Name
  --image IMAGE                  Docker-Image des Workers
  --port PORT                    Host- und Container-Port fuer Web UI und API
  --bind-host HOST               Bind-Adresse im Container (Standard 0.0.0.0)
  --worker-token TOKEN           Optionaler Bearer-Token fuer Remote-API und Web UI
  --paperless-url URL            Paperless-ngx Basis-URL
  --paperless-token TOKEN        Paperless API-Token
  --ai-api-key KEY               API-Key fuer OpenAI-kompatiblen Hauptprovider
  --ai-model NAME                Hauptmodell fuer Dokumentklassifikation
  --ai-base-url URL              OpenAI-kompatible Base-URL des Hauptproviders
  --enable-tax-enrichment BOOL   true/false fuer Tax Enrichment
  --tax-ai-api-key KEY           Optionaler separater Key fuer Tax Enrichment
  --tax-ai-model NAME            Optionales separates Tax-Modell
  --tax-ai-base-url URL          Optionaler separater OpenAI-kompatibler Tax-Endpoint
  --config-file PATH             Vorhandene YAML als Worker-Konfiguration uebernehmen
  --regenerate-config BOOL       true = vorhandene config.yaml aus Parametern neu schreiben
  --dry-run-default BOOL         Defaultwert fuer dry_run in neu erzeugter YAML
  --process-only-tag TAG         Defaultwert fuer process_only_tag in neu erzeugter YAML
  --skip-start BOOL              true = Dateien nur vorbereiten, Container nicht starten
  -h, --help                     Diese Hilfe anzeigen
USAGE
}

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

as_bool() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|on|ON|ja|JA) printf 'true' ;;
    0|false|FALSE|False|no|NO|off|OFF|nein|NEIN|'') printf 'false' ;;
    *) fail "Ungültiger Boolean-Wert: $1 (erlaubt: true/false)" ;;
  esac
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Befehl nicht gefunden: $1"
}

yaml_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

backup_file() {
  local path="$1"
  if [ -f "$path" ]; then
    local backup_path="${path}.bak.${TIMESTAMP}"
    cp "$path" "$backup_path"
    log "Backup erstellt: $backup_path"
  fi
}

compose() {
  if [ "$COMPOSE_STYLE" = "docker-compose" ]; then
    docker-compose -f "$COMPOSE_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

check_unraid_context() {
  if [ -f /etc/unraid-version ]; then
    log "Unraid erkannt: $(cat /etc/unraid-version)"
  else
    warn "Unraid wurde nicht eindeutig erkannt. Das Skript funktioniert auch auf normalem Linux, ist aber fuer Unraid optimiert."
  fi
}

choose_compose_backend() {
  require_command docker
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_STYLE="docker-compose-plugin"
    log "Docker Compose Plugin erkannt."
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_STYLE="docker-compose"
    log "Klassisches docker-compose erkannt."
    return
  fi
  fail "Weder 'docker compose' noch 'docker-compose' ist verfuegbar."
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --data-dir) DATA_DIR="$2"; shift 2 ;;
      --container-name) CONTAINER_NAME="$2"; shift 2 ;;
      --image) IMAGE="$2"; shift 2 ;;
      --port) PORT="$2"; shift 2 ;;
      --bind-host) BIND_HOST="$2"; shift 2 ;;
      --worker-token) WORKER_TOKEN="$2"; shift 2 ;;
      --paperless-url) PAPERLESS_URL="$2"; shift 2 ;;
      --paperless-token) PAPERLESS_TOKEN="$2"; shift 2 ;;
      --ai-api-key) AI_API_KEY="$2"; shift 2 ;;
      --ai-model) AI_MODEL="$2"; shift 2 ;;
      --ai-base-url) AI_BASE_URL="$2"; shift 2 ;;
      --enable-tax-enrichment) ENABLE_TAX_ENRICHMENT="$(as_bool "$2")"; shift 2 ;;
      --tax-ai-api-key) TAX_AI_API_KEY="$2"; shift 2 ;;
      --tax-ai-model) TAX_AI_MODEL="$2"; shift 2 ;;
      --tax-ai-base-url) TAX_AI_BASE_URL="$2"; shift 2 ;;
      --config-file) SOURCE_CONFIG_FILE="$2"; shift 2 ;;
      --regenerate-config) REGENERATE_CONFIG="$(as_bool "$2")"; shift 2 ;;
      --dry-run-default) DRY_RUN_DEFAULT="$(as_bool "$2")"; shift 2 ;;
      --process-only-tag) PROCESS_ONLY_TAG="$2"; shift 2 ;;
      --skip-start) SKIP_START="$(as_bool "$2")"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "Unbekannte Option: $1. Nutze --help fuer die Liste aller Optionen." ;;
    esac
  done
}

validate_runtime_inputs() {
  case "$PORT" in
    ''|*[!0-9]*) fail "Port muss numerisch sein: $PORT" ;;
  esac
  if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    fail "Port ausserhalb des gueltigen Bereichs 1..65535: $PORT"
  fi
  if [ -n "$SOURCE_CONFIG_FILE" ] && [ ! -f "$SOURCE_CONFIG_FILE" ]; then
    fail "Angegebene config.yaml nicht gefunden: $SOURCE_CONFIG_FILE"
  fi
}

prepare_paths() {
  DATA_DIR="${DATA_DIR%/}"
  CONFIG_DIR="$DATA_DIR/config"
  STATE_DIR="$DATA_DIR/state"
  LOGS_DIR="$DATA_DIR/logs"
  EXPORTS_DIR="$DATA_DIR/exports"
  COMPOSE_DIR="$DATA_DIR/compose"
  CONFIG_FILE="$CONFIG_DIR/config.yaml"
  ENV_FILE="$COMPOSE_DIR/.worker.env"
  COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"

  mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOGS_DIR" "$EXPORTS_DIR" "$COMPOSE_DIR"
}

require_generated_config_values() {
  [ -n "$PAPERLESS_URL" ] || fail "--paperless-url fehlt fuer eine neu erzeugte config.yaml"
  [ -n "$PAPERLESS_TOKEN" ] || fail "--paperless-token fehlt fuer eine neu erzeugte config.yaml"
  [ -n "$AI_API_KEY" ] || fail "--ai-api-key fehlt fuer eine neu erzeugte config.yaml"
  [ -n "$AI_MODEL" ] || fail "--ai-model fehlt fuer eine neu erzeugte config.yaml"
}

write_generated_config() {
  local paperless_url_escaped ai_key_escaped paperless_token_escaped ai_model_escaped ai_base_url_escaped
  paperless_url_escaped="$(yaml_escape "$PAPERLESS_URL")"
  paperless_token_escaped="$(yaml_escape "$PAPERLESS_TOKEN")"
  ai_key_escaped="$(yaml_escape "$AI_API_KEY")"
  ai_model_escaped="$(yaml_escape "$AI_MODEL")"
  ai_base_url_escaped="$(yaml_escape "$AI_BASE_URL")"

  cat > "$CONFIG_FILE" <<EOF_CONFIG
# Diese Datei wurde automatisch durch $SCRIPT_NAME erzeugt.
#
# Sichere Defaults:
# - dry_run startet bewusst mit true
# - process_only_tag ist standardmaessig auf #NEU gesetzt
# - ein separater Backfill kann spaeter kontrolliert ueber Web UI oder Home Assistant gestartet werden

paperless_url: "$paperless_url_escaped"
paperless_token: "$paperless_token_escaped"
ai_api_key: "$ai_key_escaped"
ai_model: "$ai_model_escaped"
ai_base_url: "$ai_base_url_escaped"

max_documents: 25
dry_run: $DRY_RUN_DEFAULT
create_missing_entities: true
confidence_threshold: 0.70
request_timeout_seconds: 30
log_level: "INFO"
enable_token_precheck: false
min_remaining_tokens: 1500
custom_prompt_instructions: |
  Nutze vorhandene Tags und Speicherpfade bevorzugt.
  Erzeuge nur dann neue Werte, wenn sie fachlich klar belegt sind.

basis_config:
  people:
    owner:
      full_name: ""
      aliases: []
      address:
        street: ""
        postal_code: ""
        city: ""
      contact:
        mobile: ""
      tax:
        tax_number: ""
    household:
      children: []
      relatives: []
    contacts: []
  organizations:
    employer_current:
      name: ""
      preferred_storage_path: ""
    employer_former:
      name: ""
      locations: []
      preferred_storage_path: ""
      only_if_clear_business_context: true
    clubs: []
  identifiers:
    meters: []
  classification_rules:
    document_type:
      invoice_addressed_to_owner: "Rechnung"
      legal_documents_force_type:
        type: "Rechtsanwalt"
        trigger_terms: ["Rechtsanwalt", "Gericht", "Klage", "Beschluss", "Einspruch", "Aktenzeichen"]
    correspondent:
      normalize: []
    storage_path:
      mappings: []
      default: "Privat"
    tags:
      add_year_tag_for_invoices: true
      add_customer_number_tag_for_contracts: true
      legal_case_tag_prefers_case_reference: true
      keep_sparse: true
    date:
      prefer_document_date_over_upload_date: true
  guardrails:
    forbidden_path_assignments: []

process_only_tag: "$(yaml_escape "$PROCESS_ONLY_TAG")"
include_existing_entities_in_prompt: true
enable_ai_notes: true
ai_notes_max_chars: 800
enable_ai_note_summary: true
ai_note_summary_max_chars: 220
enable_custom_field_enrichment: false
create_missing_custom_fields: true
enable_secondbrain_custom_fields: false
secondbrain_custom_fields_overwrite_existing: false
secondbrain_custom_fields_attach_empty_when_unknown: false
secondbrain_custom_fields_confidence_threshold: 0.70
secondbrain_custom_fields_log_missing_fields: true
metrics_file: "run_metrics.json"
input_cost_per_1k_tokens_eur: 0.0
output_cost_per_1k_tokens_eur: 0.0
quarantine_failed_documents: true
failed_document_cooldown_hours: 24
failed_documents_file: "failed_documents.json"
failed_tags_only_cooldown_hours: 168
failed_patch_cache_file: "failed_patch_cache.json"
enable_tag_bypass_on_tags_500: true
tag_bypass_file: "tag_bypass_documents.json"
already_classified_skip: true
already_classified_require_ki_tag: true
precheck_min_content_chars: 120
precheck_min_word_count: 20
precheck_min_alnum_ratio: 0.40
precheck_blocked_filename_patterns: ["smime", ".p7m", ".p7s", "winmail.dat", "ATT00001"]
precheck_image_only_gate: true
precheck_duplicate_hash_gate: true
precheck_duplicate_apply_metadata: true
reprocess_ki_tagged_documents: false
enable_parallel_ai: false
max_parallel_ai_jobs: 1
enable_tax_enrichment: $ENABLE_TAX_ENRICHMENT
tax_export_dir: "tax_exports"
tax_export_years:
  - 2025
tax_process_ki_tagged_documents: false
tax_personal_context: |
  Steuerpflichtiger:
  Familienstand:
  Kinder:
  Betreuungsmodell:
  Sonstige steuerlich wichtige Hinweise:
EOF_CONFIG

  if [ -n "$TAX_AI_API_KEY" ] || [ -n "$TAX_AI_MODEL" ] || [ -n "$TAX_AI_BASE_URL" ]; then
    printf '\n# Optionaler separater KI-Provider nur fuer Tax Enrichment\n' >> "$CONFIG_FILE"
    printf 'tax_ai_api_key: "%s"\n' "$(yaml_escape "${TAX_AI_API_KEY:-$AI_API_KEY}")" >> "$CONFIG_FILE"
    printf 'tax_ai_model: "%s"\n' "$(yaml_escape "${TAX_AI_MODEL:-$AI_MODEL}")" >> "$CONFIG_FILE"
    printf 'tax_ai_base_url: "%s"\n' "$(yaml_escape "${TAX_AI_BASE_URL:-$AI_BASE_URL}")" >> "$CONFIG_FILE"
  fi
}

prepare_config() {
  if [ -n "$SOURCE_CONFIG_FILE" ]; then
    backup_file "$CONFIG_FILE"
    cp "$SOURCE_CONFIG_FILE" "$CONFIG_FILE"
    log "Vorhandene YAML uebernommen: $SOURCE_CONFIG_FILE -> $CONFIG_FILE"
    return
  fi

  if [ "$REGENERATE_CONFIG" = "true" ] || [ ! -f "$CONFIG_FILE" ]; then
    require_generated_config_values
    backup_file "$CONFIG_FILE"
    write_generated_config
    log "Neue Worker-Konfiguration geschrieben: $CONFIG_FILE"
    return
  fi

  log "Bestehende Worker-Konfiguration bleibt unveraendert: $CONFIG_FILE"
}

write_env_file() {
  cat > "$ENV_FILE" <<EOF_ENV
PAPERLESS_KIPLUS_DATA_DIR=/data
PAPERLESS_KIPLUS_HOST=$BIND_HOST
PAPERLESS_KIPLUS_PORT=$PORT
PAPERLESS_KIPLUS_TOKEN=$WORKER_TOKEN
EOF_ENV
  chmod 600 "$ENV_FILE"
}

write_compose_file() {
  backup_file "$COMPOSE_FILE"
  cat > "$COMPOSE_FILE" <<EOF_COMPOSE
services:
  paperless-kiplus-worker:
    image: $IMAGE
    container_name: $CONTAINER_NAME
    restart: unless-stopped
    env_file:
      - .worker.env
    ports:
      - "$PORT:$PORT"
    volumes:
      - "$DATA_DIR:/data"
EOF_COMPOSE
}

pull_image_if_possible() {
  log "Pruefe Worker-Image: $IMAGE"
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    log "Image lokal vorhanden. Versuche trotzdem ein Update vom Registry-Stand zu holen."
  fi
  if ! compose pull; then
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
      warn "Image-Pull fehlgeschlagen. Nutze vorhandenes lokales Image weiter."
      return
    fi
    fail "Image konnte nicht geladen werden und lokal ist kein Fallback vorhanden: $IMAGE"
  fi
}

start_or_update_stack() {
  compose config >/dev/null
  pull_image_if_possible
  compose up -d --remove-orphans
}

healthcheck() {
  local url="http://127.0.0.1:$PORT/api/status"
  local auth_header=()
  if [ -n "$WORKER_TOKEN" ]; then
    auth_header=(-H "Authorization: Bearer $WORKER_TOKEN")
  fi

  if command -v curl >/dev/null 2>&1; then
    local attempt
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
      if curl -fsS --max-time 5 "${auth_header[@]}" "$url" >/dev/null 2>&1; then
        log "Health-Check erfolgreich: $url"
        return 0
      fi
      sleep 2
    done
    return 1
  fi

  warn "curl ist nicht verfuegbar. Health-Check wird uebersprungen."
  return 0
}

print_summary() {
  cat <<SUMMARY

Installation abgeschlossen.

Wichtige Pfade:
- Datenverzeichnis: $DATA_DIR
- Worker-YAML:    $CONFIG_FILE
- Compose-Stack:  $COMPOSE_FILE
- Env-Datei:      $ENV_FILE
- Log-Datei:      $LOGS_DIR/worker.log

Aufrufe:
- Weboberflaeche: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/
- Lokaler Status: http://127.0.0.1:$PORT/api/status

Wenn du Home Assistant anbinden willst:
- execution_mode: remote_worker
- remote_worker_url: http://<unraid-server>:$PORT
- remote_worker_token: ${WORKER_TOKEN:-<leer>}
- remote_worker_sync_config: true

Naechste sinnvolle Schritte:
1. Weboberflaeche oeffnen und auf Status/Config achten.
2. Beim ersten Lauf mit dry_run: true testen.
3. Danach dry_run in $CONFIG_FILE auf false setzen oder per Home Assistant exportieren.
SUMMARY
}

main() {
  parse_args "$@"
  validate_runtime_inputs
  check_unraid_context
  choose_compose_backend
  prepare_paths
  write_env_file
  write_compose_file
  prepare_config

  if [ "$SKIP_START" = "true" ]; then
    log "skip-start=true: Dateien wurden vorbereitet, der Container wurde nicht gestartet."
    print_summary
    exit 0
  fi

  start_or_update_stack
  if ! healthcheck; then
    warn "Der Container wurde gestartet, aber der Health-Check gegen /api/status war nicht rechtzeitig erfolgreich."
    warn "Bitte pruefe: docker logs $CONTAINER_NAME"
  fi
  print_summary
}

main "$@"
