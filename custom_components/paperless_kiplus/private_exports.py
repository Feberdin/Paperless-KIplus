"""Resolve exports inside configured, authenticated Home Assistant media storage.

Input: HA media directories and one allowlisted export filename.
Output: filesystem path and authenticated API URL; never a public /local URL.
Debug: check Settings > Media and homeassistant.media_dirs; no secrets are logged.
"""

from pathlib import Path


EXPORT_FILENAMES = {
    "paperless_kiplus_last_log.txt",
    "paperless_kiplus_worker_config.yaml",
}


def export_destination(hass, filename: str) -> tuple[Path, str]:
    """Prefer a dedicated private source, then HA's standard local media source."""
    if filename not in EXPORT_FILENAMES:
        raise ValueError("Unbekannte Paperless-Exportdatei; Dateinamen prüfen.")
    directories = hass.config.media_dirs
    if not directories:
        raise ValueError("Kein geschütztes Medienverzeichnis eingerichtet; homeassistant.media_dirs prüfen.")
    source = next((key for key in ("private", "local") if key in directories), sorted(directories)[0])
    root = Path(directories[source]).resolve()
    if "www" in root.parts:
        raise ValueError("Medienexport verweigert: Das Medienverzeichnis liegt im öffentlichen www-Ordner.")
    destination = root / "paperless_kiplus" / filename
    if not destination.resolve().is_relative_to(root):
        raise ValueError("Exportziel verlaesst das konfigurierte private Verzeichnis.")
    url = f"/api/paperless_kiplus/exports/{filename}"
    return destination, url
