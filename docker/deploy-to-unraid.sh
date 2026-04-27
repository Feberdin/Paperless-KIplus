#!/usr/bin/env bash
set -Eeuo pipefail

# Purpose:
# - Rollt den Unraid-Installer von macOS oder Linux per SSH auf einen
#   entfernten Unraid-Server aus.
# - Kopiert das eigentliche Host-Installationsskript auf den Server und fuehrt
#   es dort mit den uebergebenen Parametern aus.
#
# Input / Output:
# - Input: Unraid-Host, optionale SSH-Parameter und alle Installer-Parameter
#   fuer docker/install-unraid-worker.sh.
# - Output: Ein auf Unraid gestarteter oder aktualisierter Worker, ohne dass auf
#   dem lokalen macOS-Rechner versehentlich Docker-Container installiert werden.
#
# Wichtige Invarianten:
# - Installer-Argumente werden unveraendert an den Remote-Host weitergereicht.
# - Eine lokale --config-file wird vor der Ausfuehrung nach Unraid hochgeladen.
# - Bei Fehlern bleiben die Remote-Dateien standardmaessig fuer Debugging liegen.
#
# So debuggt man das Skript:
# - bash -n docker/deploy-to-unraid.sh
# - bash docker/deploy-to-unraid.sh --help
# - ssh root@<unraid> "docker logs paperless-kiplus-worker"

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_INSTALLER="$SCRIPT_DIR/install-unraid-worker.sh"

UNRAID_HOST=""
SSH_USER="root"
SSH_PORT="22"
SSH_KEY=""
REMOTE_DIR="/tmp/paperless-kiplus-installer"
KEEP_REMOTE_FILES="false"
LOCAL_CONFIG_FILE=""

declare -a FORWARDED_ARGS=()

usage() {
  cat <<USAGE
Verwendung:
  $SCRIPT_NAME --unraid-host HOST [Remote-Optionen] [Installer-Optionen]

Beispiel:
  bash docker/deploy-to-unraid.sh \
    --unraid-host 192.168.178.30 \
    --paperless-url http://192.168.178.20:8000 \
    --paperless-token PAPERLESS_TOKEN \
    --ai-api-key OPENAI_KEY \
    --ai-model gpt-4.1-mini

Beispiel mit lokaler config.yaml:
  bash docker/deploy-to-unraid.sh \
    --unraid-host 192.168.178.30 \
    --config-file ./worker-config.yaml \
    --skip-start true

Remote-Optionen:
  --unraid-host HOST             Zielhost oder IP von Unraid
  --ssh-user USER                SSH-Benutzer (Standard: root)
  --ssh-port PORT                SSH-Port (Standard: 22)
  --ssh-key PATH                 Optionaler privater SSH-Key
  --remote-dir PATH              Temp-Verzeichnis auf Unraid fuer den Upload
  --keep-remote-files BOOL       true = Upload-Dateien nach Erfolg behalten
  -h, --help                     Diese Hilfe anzeigen

Alle weiteren Parameter werden unveraendert an
docker/install-unraid-worker.sh auf dem Unraid-Host weitergereicht.
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

shell_quote() {
  printf '%q' "$1"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --unraid-host) UNRAID_HOST="$2"; shift 2 ;;
      --ssh-user) SSH_USER="$2"; shift 2 ;;
      --ssh-port) SSH_PORT="$2"; shift 2 ;;
      --ssh-key) SSH_KEY="$2"; shift 2 ;;
      --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
      --keep-remote-files) KEEP_REMOTE_FILES="$(as_bool "$2")"; shift 2 ;;
      --config-file)
        LOCAL_CONFIG_FILE="$2"
        FORWARDED_ARGS+=("--config-file" "__REMOTE_CONFIG_FILE__")
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        FORWARDED_ARGS+=("$1")
        shift
        ;;
    esac
  done
}

validate_inputs() {
  require_command ssh
  require_command scp
  [ -n "$UNRAID_HOST" ] || fail "--unraid-host ist erforderlich"
  case "$SSH_PORT" in
    ''|*[!0-9]*) fail "SSH-Port muss numerisch sein: $SSH_PORT" ;;
  esac
  [ -f "$LOCAL_INSTALLER" ] || fail "Lokaler Installer nicht gefunden: $LOCAL_INSTALLER"
  if [ -n "$SSH_KEY" ] && [ ! -f "$SSH_KEY" ]; then
    fail "Angegebener SSH-Key nicht gefunden: $SSH_KEY"
  fi
  if [ -n "$LOCAL_CONFIG_FILE" ] && [ ! -f "$LOCAL_CONFIG_FILE" ]; then
    fail "Angegebene lokale config.yaml nicht gefunden: $LOCAL_CONFIG_FILE"
  fi
}

build_ssh_options() {
  SSH_OPTIONS=(-p "$SSH_PORT" -o ConnectTimeout=10)
  SCP_OPTIONS=(-P "$SSH_PORT" -o ConnectTimeout=10)
  if [ -n "$SSH_KEY" ]; then
    SSH_OPTIONS+=(-i "$SSH_KEY")
    SCP_OPTIONS+=(-i "$SSH_KEY")
  fi
}

remote_exec() {
  ssh "${SSH_OPTIONS[@]}" "${SSH_USER}@${UNRAID_HOST}" "$@"
}

remote_prepare() {
  log "Pruefe SSH-Verbindung zu ${SSH_USER}@${UNRAID_HOST}:${SSH_PORT}"
  remote_exec "uname -s >/dev/null"
  remote_exec "mkdir -p $(shell_quote "$REMOTE_DIR")"
}

upload_files() {
  REMOTE_INSTALLER_PATH="$REMOTE_DIR/install-unraid-worker.sh"
  log "Lade Installer nach Unraid hoch: $REMOTE_INSTALLER_PATH"
  scp "${SCP_OPTIONS[@]}" "$LOCAL_INSTALLER" "${SSH_USER}@${UNRAID_HOST}:${REMOTE_INSTALLER_PATH}" >/dev/null
  remote_exec "chmod +x $(shell_quote "$REMOTE_INSTALLER_PATH")"

  if [ -n "$LOCAL_CONFIG_FILE" ]; then
    REMOTE_CONFIG_PATH="$REMOTE_DIR/source-config.yaml"
    log "Lade lokale config.yaml nach Unraid hoch: $REMOTE_CONFIG_PATH"
    scp "${SCP_OPTIONS[@]}" "$LOCAL_CONFIG_FILE" "${SSH_USER}@${UNRAID_HOST}:${REMOTE_CONFIG_PATH}" >/dev/null
  else
    REMOTE_CONFIG_PATH=""
  fi
}

build_remote_command() {
  local -a remote_command=(bash "$REMOTE_INSTALLER_PATH")
  local arg
  for arg in "${FORWARDED_ARGS[@]}"; do
    if [ "$arg" = "__REMOTE_CONFIG_FILE__" ]; then
      remote_command+=("$REMOTE_CONFIG_PATH")
    else
      remote_command+=("$arg")
    fi
  done

  local rendered=""
  for arg in "${remote_command[@]}"; do
    rendered="${rendered} $(shell_quote "$arg")"
  done
  printf '%s' "${rendered# }"
}

cleanup_remote_files() {
  if [ "$KEEP_REMOTE_FILES" = "true" ]; then
    log "Remote-Dateien bleiben erhalten: $REMOTE_DIR"
    return
  fi
  remote_exec "rm -rf $(shell_quote "$REMOTE_DIR")"
  log "Remote-Temp-Dateien entfernt: $REMOTE_DIR"
}

main() {
  parse_args "$@"
  validate_inputs
  build_ssh_options
  remote_prepare
  upload_files

  local remote_command
  remote_command="$(build_remote_command)"
  log "Starte Remote-Installation auf ${UNRAID_HOST}"
  if ! remote_exec "$remote_command"; then
    warn "Remote-Installation fehlgeschlagen. Upload-Dateien bleiben auf dem Server fuer Debugging liegen: $REMOTE_DIR"
    exit 1
  fi

  cleanup_remote_files
  cat <<SUMMARY

Remote-Installation abgeschlossen.

Zielhost:
- ${SSH_USER}@${UNRAID_HOST}:${SSH_PORT}

Naechste sinnvolle Schritte:
1. Weboberflaeche des Workers auf Unraid oeffnen.
2. Beim ersten Lauf mit dry_run pruefen.
3. Danach Home Assistant optional auf remote_worker umstellen.
SUMMARY
}

main "$@"
