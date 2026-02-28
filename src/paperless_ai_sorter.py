#!/usr/bin/env python3
"""Paperless KI Sorter.

Dieses Skript lädt Dokumente aus Paperless-ngx, lässt sie durch ein LLM
klassifizieren und schreibt die vorgeschlagenen Metadaten zurück.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import requests
import yaml


LOGGER = logging.getLogger("paperless_ai_sorter")


class ConfigError(Exception):
    """Fehler in der Konfiguration."""


class PaperlessApiError(Exception):
    """Fehler bei einem API-Request an Paperless."""


class AiClassificationError(Exception):
    """Fehler bei KI-Klassifizierung oder Antwortformat."""


@dataclass
class AppConfig:
    """Strukturierte Konfiguration für das Skript."""

    paperless_url: str
    paperless_token: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str
    max_documents: int
    dry_run: bool
    create_missing_entities: bool
    confidence_threshold: float
    request_timeout_seconds: int
    log_level: str


def load_config(config_path: str, cli_dry_run: bool) -> AppConfig:
    """Lädt YAML-Konfiguration und validiert Pflichtfelder.

    Wir werfen bewusst klare Fehlermeldungen, damit Setup-Probleme
    schnell sichtbar sind.
    """

    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Konfiguration ist kein valides YAML: {exc}") from exc

    missing = []
    for key in ("paperless_url", "paperless_token", "ai_api_key", "ai_model"):
        if not raw.get(key):
            missing.append(key)

    if missing:
        raise ConfigError(
            "Folgende Pflichtfelder fehlen in der Konfiguration: " + ", ".join(missing)
        )

    return AppConfig(
        paperless_url=str(raw["paperless_url"]).rstrip("/"),
        paperless_token=str(raw["paperless_token"]),
        ai_api_key=str(raw["ai_api_key"]),
        ai_model=str(raw["ai_model"]),
        ai_base_url=str(raw.get("ai_base_url", "https://api.openai.com/v1")).rstrip("/"),
        max_documents=int(raw.get("max_documents", 25)),
        dry_run=bool(raw.get("dry_run", False) or cli_dry_run),
        create_missing_entities=bool(raw.get("create_missing_entities", True)),
        confidence_threshold=float(raw.get("confidence_threshold", 0.70)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        log_level=str(raw.get("log_level", "INFO")),
    )


class PaperlessClient:
    """Minimaler API-Client für Paperless-ngx."""

    def __init__(self, config: AppConfig) -> None:
        self.base_url = config.paperless_url
        self.timeout = config.request_timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {config.paperless_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        """HTTP-Request mit einfachem Retry für transiente Fehler."""

        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    data=json.dumps(payload) if payload is not None else None,
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    raise PaperlessApiError(
                        f"{method} {path} fehlgeschlagen: HTTP {response.status_code} - {response.text}"
                    )

                if not response.content:
                    return {}
                return response.json()
            except (requests.RequestException, ValueError, PaperlessApiError) as exc:
                last_error = exc
                LOGGER.warning(
                    "Request fehlgeschlagen (Versuch %s/%s): %s %s | Fehler: %s",
                    attempt,
                    retries,
                    method,
                    path,
                    exc,
                )
                # Exponentielles Backoff reduziert Last und erhöht Robustheit.
                time.sleep(0.5 * (2 ** (attempt - 1)))

        raise PaperlessApiError(
            f"Request dauerhaft fehlgeschlagen: {method} {path} | Letzter Fehler: {last_error}"
        )

    def iter_documents(self, limit: int) -> Iterable[Dict[str, Any]]:
        """Lädt Dokumente seitenweise.

        Standardmäßig nutzen wir `ordering=-created`, damit zuerst neue Dokumente
        verarbeitet werden. Die Filterlogik kann später leicht erweitert werden.
        """

        next_url = "/api/documents/"
        params: Optional[Dict[str, Any]] = {
            "ordering": "-created",
            "page_size": min(limit, 100),
        }
        loaded = 0

        while next_url and loaded < limit:
            page = self._request("GET", next_url, params=params)
            params = None

            for doc in page.get("results", []):
                yield doc
                loaded += 1
                if loaded >= limit:
                    break

            absolute_next = page.get("next")
            if absolute_next:
                next_url = absolute_next.replace(self.base_url, "")
            else:
                next_url = ""

    def list_named_entities(self, path: str) -> Dict[str, int]:
        """Lädt Name->ID Mapping für Tags/Typen/Korrespondenten/Ablagepfade."""

        mapping: Dict[str, int] = {}
        next_url = path

        while next_url:
            page = self._request("GET", next_url, params={"page_size": 100})
            for item in page.get("results", []):
                name = str(item.get("name", "")).strip()
                if name:
                    mapping[name.lower()] = int(item["id"])

            absolute_next = page.get("next")
            if absolute_next:
                next_url = absolute_next.replace(self.base_url, "")
            else:
                next_url = ""

        return mapping

    def create_entity(self, path: str, name: str) -> int:
        """Erzeugt ein Metadaten-Objekt in Paperless und gibt dessen ID zurück."""

        created = self._request("POST", path, payload={"name": name})
        created_id = created.get("id")
        if created_id is None:
            raise PaperlessApiError(
                f"Entity wurde erstellt, aber ohne ID zurückgegeben: {path} | {name}"
            )
        return int(created_id)

    def update_document(self, document_id: int, patch_payload: Dict[str, Any]) -> None:
        """Schreibt klassifizierte Felder zurück auf das Dokument."""

        self._request("PATCH", f"/api/documents/{document_id}/", payload=patch_payload)


class AiClassifier:
    """Verwendet OpenAI-kompatible Chat-Completions für Klassifizierung."""

    def __init__(self, config: AppConfig) -> None:
        self.model = config.ai_model
        self.timeout = config.request_timeout_seconds
        self.base_url = config.ai_base_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.ai_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def classify(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """Sendet Dokumentkontext an KI und erwartet streng JSON als Antwort."""

        prompt = (
            "Du bist ein präziser Dokumenten-Klassifizierer für Paperless-ngx. "
            "Antworte ausschließlich als JSON mit den Feldern: "
            "document_type, correspondent, storage_path, tags (Liste), confidence (0-1), rationale. "
            "Keine zusätzlichen Schlüssel, keine Markdown-Ausgabe."
        )

        # Wir begrenzen den Text bewusst, um Tokenkosten und Latenz zu kontrollieren.
        content_preview = str(document.get("content") or "")[:6000]
        user_payload = {
            "title": document.get("title", ""),
            "content_preview": content_preview,
            "created": document.get("created"),
            "current_tags": document.get("tags", []),
        }

        req_body = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": (
                        "Klassifiziere dieses Dokument für eine Ablagestruktur.\n"
                        + json.dumps(user_payload, ensure_ascii=False)
                    ),
                },
            ],
            "temperature": 0.1,
        }

        try:
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                data=json.dumps(req_body),
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw = response.json()
            message = raw["choices"][0]["message"]["content"]
            parsed = json.loads(message)
            self._validate_model_output(parsed)
            return parsed
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise AiClassificationError(f"KI-Antwort ungültig oder Request fehlgeschlagen: {exc}") from exc

    @staticmethod
    def _validate_model_output(payload: Dict[str, Any]) -> None:
        """Prüft Minimalkonsistenz der KI-Ausgabe.

        Strenge Validierung hilft, stille Datenfehler früh zu erkennen.
        """

        required = ["document_type", "correspondent", "storage_path", "tags", "confidence"]
        missing = [key for key in required if key not in payload]
        if missing:
            raise AiClassificationError(
                "KI-Ausgabe fehlt Pflichtfelder: " + ", ".join(missing)
            )

        if not isinstance(payload["tags"], list):
            raise AiClassificationError("KI-Ausgabe: 'tags' muss eine Liste sein.")

        confidence = float(payload["confidence"])
        if confidence < 0 or confidence > 1:
            raise AiClassificationError("KI-Ausgabe: 'confidence' muss zwischen 0 und 1 liegen.")


def ensure_entity_id(
    client: PaperlessClient,
    mapping: Dict[str, int],
    name: Optional[str],
    endpoint: str,
    create_missing: bool,
) -> Optional[int]:
    """Löst Namen auf eine ID auf und legt Entity optional an.

    Rückgabe `None` bedeutet: Feld nicht setzen.
    """

    if not name:
        return None

    key = name.strip().lower()
    if not key:
        return None

    if key in mapping:
        return mapping[key]

    if not create_missing:
        LOGGER.info("Entity nicht vorhanden und Auto-Create deaktiviert: %s (%s)", name, endpoint)
        return None

    created_id = client.create_entity(endpoint, name.strip())
    mapping[key] = created_id
    LOGGER.info("Neue Entity angelegt: %s -> ID %s (%s)", name, created_id, endpoint)
    return created_id


def build_patch_payload(
    client: PaperlessClient,
    prediction: Dict[str, Any],
    tags_map: Dict[str, int],
    doc_types_map: Dict[str, int],
    correspondents_map: Dict[str, int],
    storage_paths_map: Dict[str, int],
    create_missing_entities: bool,
) -> Dict[str, Any]:
    """Konvertiert KI-Output in ein valides PATCH-Payload für Paperless."""

    doc_type_id = ensure_entity_id(
        client,
        doc_types_map,
        prediction.get("document_type"),
        "/api/document_types/",
        create_missing_entities,
    )
    correspondent_id = ensure_entity_id(
        client,
        correspondents_map,
        prediction.get("correspondent"),
        "/api/correspondents/",
        create_missing_entities,
    )
    storage_path_id = ensure_entity_id(
        client,
        storage_paths_map,
        prediction.get("storage_path"),
        "/api/storage_paths/",
        create_missing_entities,
    )

    tag_ids: List[int] = []
    for tag_name in prediction.get("tags", []):
        tag_id = ensure_entity_id(
            client,
            tags_map,
            str(tag_name),
            "/api/tags/",
            create_missing_entities,
        )
        if tag_id is not None:
            tag_ids.append(tag_id)

    payload: Dict[str, Any] = {}
    if doc_type_id is not None:
        payload["document_type"] = doc_type_id
    if correspondent_id is not None:
        payload["correspondent"] = correspondent_id
    if storage_path_id is not None:
        payload["storage_path"] = storage_path_id
    if tag_ids:
        payload["tags"] = sorted(set(tag_ids))

    return payload


def should_process_document(document: Dict[str, Any]) -> bool:
    """Definiert, welche Dokumente verarbeitet werden sollen.

    Aktuell verarbeiten wir primär Dokumente ohne Typ oder ohne Tags.
    Diese Heuristik kann project-spezifisch angepasst werden.
    """

    has_type = document.get("document_type") is not None
    has_tags = bool(document.get("tags"))
    return not (has_type and has_tags)


def process_documents(config: AppConfig) -> None:
    """Hauptablauf: Laden, KI-Klassifizieren, validieren, patchen."""

    client = PaperlessClient(config)
    classifier = AiClassifier(config)

    LOGGER.info("Lade Metadaten-Mappings aus Paperless...")
    tags_map = client.list_named_entities("/api/tags/")
    doc_types_map = client.list_named_entities("/api/document_types/")
    correspondents_map = client.list_named_entities("/api/correspondents/")
    storage_paths_map = client.list_named_entities("/api/storage_paths/")

    scanned = 0
    updated = 0
    skipped = 0
    failed = 0

    for document in client.iter_documents(config.max_documents):
        scanned += 1
        doc_id = document.get("id")
        title = document.get("title", "<ohne Titel>")

        if not should_process_document(document):
            LOGGER.debug("Skip Dokument %s (%s): bereits klassifiziert", doc_id, title)
            skipped += 1
            continue

        try:
            prediction = classifier.classify(document)
            confidence = float(prediction["confidence"])
            if confidence < config.confidence_threshold:
                LOGGER.info(
                    "Skip Dokument %s (%s): Confidence %.2f unter Schwellwert %.2f",
                    doc_id,
                    title,
                    confidence,
                    config.confidence_threshold,
                )
                skipped += 1
                continue

            patch_payload = build_patch_payload(
                client=client,
                prediction=prediction,
                tags_map=tags_map,
                doc_types_map=doc_types_map,
                correspondents_map=correspondents_map,
                storage_paths_map=storage_paths_map,
                create_missing_entities=config.create_missing_entities,
            )

            if not patch_payload:
                LOGGER.info("Skip Dokument %s (%s): Keine verwertbaren Felder im KI-Output", doc_id, title)
                skipped += 1
                continue

            if config.dry_run:
                LOGGER.info("DRY-RUN Dokument %s (%s) -> %s", doc_id, title, patch_payload)
            else:
                client.update_document(int(doc_id), patch_payload)
                LOGGER.info("Aktualisiert Dokument %s (%s)", doc_id, title)

            updated += 1
        except (AiClassificationError, PaperlessApiError, ValueError) as exc:
            failed += 1
            LOGGER.error("Fehler bei Dokument %s (%s): %s", doc_id, title, exc)

    LOGGER.info(
        "Fertig. Gescannt=%s, Aktualisiert=%s, Übersprungen=%s, Fehler=%s",
        scanned,
        updated,
        skipped,
        failed,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KI-gestützte Paperless-Dokumentklassifizierung")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Pfad zur YAML-Konfiguration (Default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Keine Änderungen schreiben, nur anzeigen",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_config(args.config, args.dry_run)
    except ConfigError as exc:
        print(f"[CONFIG-ERROR] {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    LOGGER.info("Starte Paperless KI Sorter | dry_run=%s", config.dry_run)

    try:
        process_documents(config)
    except Exception as exc:  # Breiter Catch für sauberen Exit + logischen Fehlercode.
        LOGGER.exception("Unerwarteter Fehler im Hauptablauf: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
