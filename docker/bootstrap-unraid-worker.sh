#!/usr/bin/env bash
set -Eeuo pipefail

# Purpose:
# - Wird direkt im Unraid-Terminal ausgefuehrt, auch ohne lokales Repo-Checkout.
# - Legt einen persistenten Ordner an, laedt den eigentlichen Installer von
#   GitHub herunter und fuehrt ihn lokal auf Unraid aus.
#
# Input / Output:
# - Input: optionale Bootstrap-Parameter wie --target-dir und --ref sowie alle
#   nachfolgenden Installer-Parameter fuer install-unraid-worker.sh.
# - Output: Ein lokal auf Unraid abgelegtes Installer-Skript im Zielordner und
#   ein gestarteter oder aktualisierter Worker-Container.
#
# Wichtige Invarianten:
# - Der Zielordner wird vor dem Download angelegt.
# - Es wird immer die Installationsdatei in diesem Zielordner erneuert, damit
#   der spaetere manuelle Aufruf reproduzierbar bleibt.
# - Alle nicht vom Bootstrap selbst benoetigten Parameter werden unveraendert an
#   den produktiven Installer weitergereicht.
#
# So debuggt man das Skript:
# - bash -n bootstrap-unraid-worker.sh
# - pruefen, ob curl oder wget auf Unraid vorhanden ist
# - pruefen, ob die Datei im Zielordner abgelegt wurde

SCRIPT_NAME="$(basename "$0")"
REPO_OWNER="Feberdin"
REPO_NAME="Paperless-KIplus"
REPO_REF="main"
TARGET_DIR="/boot/config/custom/paperless-kiplus"
INSTALLER_NAME="install-unraid-worker.sh"

declare -a FORWARDED_ARGS=()

usage() {
  cat <<USAGE
Verwendung:
  $SCRIPT_NAME [Bootstrap-Optionen] [Installer-Optionen]

Beispiel:
  bash -c "\$(curl -fsSL https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/main/docker/bootstrap-unraid-worker.sh)" -- \
    --paperless-url http://192.168.178.20:8000 \
    --paperless-token PAPERLESS_TOKEN \
    --ai-api-key OPENAI_KEY \
    --ai-model gpt-4.1-mini

Bootstrap-Optionen:
  --target-dir PATH              Persistenter Zielordner fuer den Installer
  --ref REF                      Git-Ref oder Tag, z. B. main oder v1.4.4
  --repo-owner NAME              Standard: ${REPO_OWNER}
  --repo-name NAME               Standard: ${REPO_NAME}
  -h, --help                     Diese Hilfe anzeigen

Alle weiteren Parameter werden an install-unraid-worker.sh weitergereicht.
USAGE
}

log() {
  printf '[INFO] %s\n' "$*"
}

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

download_file() {
  local url="$1"
  local output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$output"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "$output" "$url"
    return
  fi
  fail "Weder curl noch wget ist verfuegbar. Bitte eines der Tools auf Unraid nutzen."
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --target-dir) TARGET_DIR="$2"; shift 2 ;;
      --ref) REPO_REF="$2"; shift 2 ;;
      --repo-owner) REPO_OWNER="$2"; shift 2 ;;
      --repo-name) REPO_NAME="$2"; shift 2 ;;
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
  if [ "$(uname -s)" = "Darwin" ]; then
    fail "Dieses Bootstrap-Skript ist fuer die direkte Ausfuehrung auf Unraid gedacht. Auf macOS nutze bitte docker/deploy-to-unraid.sh."
  fi
  [ -n "$TARGET_DIR" ] || fail "--target-dir darf nicht leer sein"
  [ -n "$REPO_REF" ] || fail "--ref darf nicht leer sein"
  [ -n "$REPO_OWNER" ] || fail "--repo-owner darf nicht leer sein"
  [ -n "$REPO_NAME" ] || fail "--repo-name darf nicht leer sein"
}

main() {
  parse_args "$@"
  validate_inputs

  local installer_url installer_path
  installer_url="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_REF}/docker/${INSTALLER_NAME}"
  installer_path="${TARGET_DIR%/}/${INSTALLER_NAME}"

  mkdir -p "$TARGET_DIR"
  log "Zielordner vorbereitet: $TARGET_DIR"
  log "Lade Installer herunter: $installer_url"
  download_file "$installer_url" "$installer_path"
  chmod +x "$installer_path"
  log "Installer gespeichert: $installer_path"
  log "Starte Installer direkt auf Unraid"
  bash "$installer_path" "${FORWARDED_ARGS[@]}"
}

main "$@"
