#!/usr/bin/env python3
"""Paperless KI Sorter.

Dieses Skript lädt Dokumente aus Paperless-ngx, lässt sie durch ein LLM
klassifizieren und schreibt die vorgeschlagenen Metadaten zurück.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
    enable_token_precheck: bool
    min_remaining_tokens: int
    custom_prompt_instructions: str
    basis_config: Dict[str, Any]
    process_only_tag: str
    include_existing_entities_in_prompt: bool
    enable_ai_notes: bool
    ai_notes_max_chars: int
    enable_ai_note_summary: bool
    ai_note_summary_max_chars: int


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
        enable_token_precheck=bool(raw.get("enable_token_precheck", False)),
        min_remaining_tokens=int(raw.get("min_remaining_tokens", 1500)),
        custom_prompt_instructions=str(raw.get("custom_prompt_instructions", "")).strip(),
        basis_config=dict(raw.get("basis_config", {})),
        process_only_tag=str(raw.get("process_only_tag", "")).strip(),
        include_existing_entities_in_prompt=bool(
            raw.get("include_existing_entities_in_prompt", True)
        ),
        enable_ai_notes=bool(raw.get("enable_ai_notes", True)),
        ai_notes_max_chars=int(raw.get("ai_notes_max_chars", 800)),
        enable_ai_note_summary=bool(raw.get("enable_ai_note_summary", True)),
        ai_note_summary_max_chars=int(raw.get("ai_note_summary_max_chars", 220)),
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
                "User-Agent": "paperless-kiplus/0.1",
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

        # `path` kann entweder ein relativer API-Pfad oder bereits eine absolute URL sein.
        url = path if path.startswith("http://") or path.startswith("https://") else f"{self.base_url}{path}"
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
                    extra_hint = ""
                    # Typischer Setup-Fehler bei Paperless hinter Reverse-Proxy:
                    # Die angefragte Host-URL passt nicht zu PAPERLESS_URL/ALLOWED_HOSTS.
                    if response.status_code == 400:
                        if method == "POST" and "/api/storage_paths/" in path and (
                            "\"path\"" in response.text or "\"name\"" in response.text
                        ):
                            extra_hint = (
                                " | Hinweis: Beim Anlegen von Storage Paths erwartet Paperless "
                                "je nach API-Version unterschiedliche Felder ('path' oder 'name')."
                            )
                        else:
                            extra_hint = (
                                " | Hinweis: HTTP 400 bei Paperless deutet oft auf eine falsche "
                                "paperless_url oder Host/Proxy-Konfiguration hin "
                                "(PAPERLESS_URL, ALLOWED_HOSTS, Reverse-Proxy Host Header)."
                            )
                    if response.status_code == 406:
                        extra_hint = (
                            " | Hinweis: HTTP 406 kommt oft von vorgeschalteten Proxies/WAF "
                            "(z. B. Cloudflare) für bestimmte Pfade oder Header."
                        )
                    raise PaperlessApiError(
                        f"{method} {path} fehlgeschlagen: HTTP {response.status_code} - {response.text}{extra_hint}"
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

    def iter_documents(
        self,
        limit: int,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Iterable[Dict[str, Any]]:
        """Lädt Dokumente seitenweise.

        Standardmäßig nutzen wir `ordering=-created`, damit zuerst neue Dokumente
        verarbeitet werden. Die Filterlogik kann später leicht erweitert werden.
        """

        next_url = "/api/documents/"
        params: Optional[Dict[str, Any]] = {
            "ordering": "-created",
            "page_size": min(limit, 100),
        }
        if extra_params:
            params.update(extra_params)
        loaded = 0

        while next_url and loaded < limit:
            page = self._request("GET", next_url, params=params)
            params = None

            for doc in page.get("results", []):
                yield doc
                loaded += 1
                if loaded >= limit:
                    break

            next_url = str(page.get("next") or "")

    def preflight_check(self) -> None:
        """Prüft frühzeitig, ob die Paperless-API grundsätzlich erreichbar ist.

        Einige Deployments liefern auf `/api/` (API-Root) über Proxy/WAF ein 406.
        Deshalb testen wir direkt einen echten JSON-Endpoint.
        """

        self._request("GET", "/api/documents/", params={"page_size": 1})

    def list_named_entities(self, path: str) -> Dict[str, int]:
        """Lädt Name->ID Mapping für Tags/Typen/Korrespondenten/Ablagepfade."""

        mapping: Dict[str, int] = {}
        next_url: str = path
        params: Optional[Dict[str, Any]] = {"page_size": 100}

        while next_url:
            page = self._request("GET", next_url, params=params)
            # Ab der zweiten Seite steckt die Pagination bereits in `next`.
            params = None
            for item in page.get("results", []):
                # Storage Paths nutzen oft `path` statt `name`.
                label = str(item.get("name") or item.get("path") or "").strip()
                if label:
                    mapping[label.lower()] = int(item["id"])

            next_url = str(page.get("next") or "")

        return mapping

    def create_entity(self, path: str, name: str) -> int:
        """Erzeugt ein Metadaten-Objekt in Paperless und gibt dessen ID zurück."""
        if path == "/api/storage_paths/":
            # Paperless-Versionen unterscheiden sich: manche erwarten `path`, andere `name`.
            last_exc: Optional[Exception] = None
            for payload in ({"path": name}, {"name": name}):
                try:
                    created = self._request("POST", path, payload=payload, retries=1)
                    created_id = created.get("id")
                    if created_id is None:
                        raise PaperlessApiError(
                            f"Storage Path erstellt ohne ID: {path} | {name} | payload={payload}"
                        )
                    return int(created_id)
                except PaperlessApiError as exc:
                    last_exc = exc
            raise PaperlessApiError(
                f"Storage Path konnte nicht erstellt werden ({name}). Letzter Fehler: {last_exc}"
            )

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

    def add_document_note(self, document_id: int, note: str) -> None:
        """Fügt eine Notiz über den dedizierten Notes-Endpoint hinzu."""

        self._request("POST", f"/api/documents/{document_id}/notes/", payload={"note": note})


class AiClassifier:
    """Verwendet OpenAI-kompatible Chat-Completions für Klassifizierung."""

    def __init__(self, config: AppConfig) -> None:
        self.model = config.ai_model
        self.timeout = config.request_timeout_seconds
        self.base_url = config.ai_base_url
        self.enable_token_precheck = config.enable_token_precheck
        self.min_remaining_tokens = config.min_remaining_tokens
        self.custom_prompt_instructions = config.custom_prompt_instructions
        self.basis_config = config.basis_config
        self.include_existing_entities_in_prompt = config.include_existing_entities_in_prompt
        self.known_document_types: List[str] = []
        self.known_correspondents: List[str] = []
        self.known_storage_paths: List[str] = []
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.ai_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "paperless-kiplus/0.1",
            }
        )

    def set_known_entities(
        self,
        *,
        document_types: List[str],
        correspondents: List[str],
        storage_paths: List[str],
    ) -> None:
        """Setzt bekannte Paperless-Werte für den Prompt-Kontext."""

        self.known_document_types = sorted(document_types)
        self.known_correspondents = sorted(correspondents)
        self.known_storage_paths = sorted(storage_paths)

    def preflight_token_budget(self) -> None:
        """Prüft optional verfügbare Token laut RateLimit-Header des Anbieters.

        Wichtig: Das sind API-RateLimits, nicht dein ChatGPT-Web-Abo-Kontingent.
        Einige Anbieter liefern die Header nicht; dann loggen wir nur einen Hinweis.
        """

        if not self.enable_token_precheck:
            LOGGER.info("Token-Precheck deaktiviert (enable_token_precheck=false).")
            return

        probe_body = {
            "model": self.model,
            "messages": [{"role": "user", "content": "healthcheck"}],
            "max_tokens": 1,
            "temperature": 0,
        }

        try:
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                data=json.dumps(probe_body),
                timeout=self.timeout,
            )
            response.raise_for_status()

            remaining_raw = response.headers.get("x-ratelimit-remaining-tokens")
            if remaining_raw is None:
                LOGGER.warning(
                    "Token-Precheck: Provider liefert keinen Header "
                    "'x-ratelimit-remaining-tokens'. Prüfen daher nicht möglich."
                )
                return

            remaining = int(remaining_raw)
            LOGGER.info(
                "Token-Precheck: verbleibende API-Tokens laut Header = %s (Schwellwert=%s)",
                remaining,
                self.min_remaining_tokens,
            )
            if remaining < self.min_remaining_tokens:
                raise AiClassificationError(
                    "Zu wenig verbleibende API-Tokens vor Start. "
                    f"Remaining={remaining}, benötigt mindestens={self.min_remaining_tokens}. "
                    "Lauf wird abgebrochen."
                )
        except (requests.RequestException, ValueError) as exc:
            raise AiClassificationError(
                f"Token-Precheck fehlgeschlagen (API nicht erreichbar/ungültige Header): {exc}"
            ) from exc

    def classify(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """Sendet Dokumentkontext an KI und erwartet streng JSON als Antwort."""

        prompt = (
            "Du bist ein präziser Dokumenten-Klassifizierer für Paperless-ngx. "
            "Antworte ausschließlich als JSON mit den Feldern: "
            "document_type, correspondent, storage_path, tags (Liste), "
            "document_date (YYYY-MM-DD oder null), summary, confidence (0-1), rationale. "
            "Keine zusätzlichen Schlüssel, keine Markdown-Ausgabe."
        )
        if self.custom_prompt_instructions:
            prompt += (
                "\n\nZusätzliche projektspezifische Regeln (hoch priorisiert):\n"
                f"{self.custom_prompt_instructions}"
            )
        if self.basis_config:
            prompt += (
                "\n\nStrukturierte Basis-Konfiguration (priorisiert, kompakt):\n"
                + json.dumps(self.basis_config, ensure_ascii=False, separators=(",", ":"))
            )
        if self.include_existing_entities_in_prompt:
            known = {
                "known_document_types": self.known_document_types,
                "known_correspondents": self.known_correspondents,
                "known_storage_paths": self.known_storage_paths,
            }
            prompt += (
                "\n\nBevorzuge vorhandene Werte aus diesem Bestand und erfinde nichts "
                "unnötig neu:\n"
                + json.dumps(known, ensure_ascii=False)
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

        document_date = payload.get("document_date")
        if document_date is not None and not isinstance(document_date, str):
            raise AiClassificationError(
                "KI-Ausgabe: 'document_date' muss YYYY-MM-DD oder null sein."
            )
        summary = payload.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise AiClassificationError("KI-Ausgabe: 'summary' muss ein String oder null sein.")


def normalize_iso_date(value: Optional[str]) -> Optional[str]:
    """Normalisiert Datumswerte auf YYYY-MM-DD oder gibt None zurück."""

    if not value:
        return None

    candidate = str(value).strip()
    if not candidate:
        return None

    # Erlaubt auch ISO-Datetime und schneidet Datumsteil ab.
    if "T" in candidate:
        candidate = candidate.split("T", 1)[0]
    if " " in candidate:
        candidate = candidate.split(" ", 1)[0]

    try:
        return dt.date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def sanitize_prediction(
    prediction: Dict[str, Any],
    storage_paths_map: Dict[str, int],
) -> Dict[str, Any]:
    """Bereinigt offensichtliche Fehlwerte aus der KI-Antwort.

    Beispiel: `correspondent = Privat` ist fast immer ein Mapping-Fehler,
    da `Privat` ein Speicherpfad ist. Solche Werte werden verworfen.
    """

    sanitized = dict(prediction)
    correspondent = str(sanitized.get("correspondent") or "").strip()
    if correspondent and correspondent.lower() in storage_paths_map:
        LOGGER.warning(
            "KI-Vorschlag verworfen: Korrespondent '%s' entspricht einem Speicherpfad.",
            correspondent,
        )
        sanitized["correspondent"] = None
    return sanitized


def ensure_entity_id(
    client: PaperlessClient,
    mapping: Dict[str, int],
    name: Optional[str],
    endpoint: str,
    create_missing: bool,
    created_entities: Optional[Dict[str, List[str]]] = None,
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
    if created_entities is not None:
        created_entities.setdefault(endpoint, []).append(name.strip())
    return created_id


def build_patch_payload(
    client: PaperlessClient,
    prediction: Dict[str, Any],
    tags_map: Dict[str, int],
    doc_types_map: Dict[str, int],
    correspondents_map: Dict[str, int],
    storage_paths_map: Dict[str, int],
    create_missing_entities: bool,
    created_entities: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Konvertiert KI-Output in ein valides PATCH-Payload für Paperless."""

    doc_type_id = ensure_entity_id(
        client,
        doc_types_map,
        prediction.get("document_type"),
        "/api/document_types/",
        create_missing_entities,
        created_entities,
    )
    correspondent_id = ensure_entity_id(
        client,
        correspondents_map,
        prediction.get("correspondent"),
        "/api/correspondents/",
        create_missing_entities,
        created_entities,
    )
    storage_path_id = ensure_entity_id(
        client,
        storage_paths_map,
        prediction.get("storage_path"),
        "/api/storage_paths/",
        create_missing_entities,
        created_entities,
    )

    tag_ids: List[int] = []
    for tag_name in prediction.get("tags", []):
        tag_id = ensure_entity_id(
            client,
            tags_map,
            str(tag_name),
            "/api/tags/",
            create_missing_entities,
            created_entities,
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

    normalized_date = normalize_iso_date(prediction.get("document_date"))
    if normalized_date is not None:
        # Paperless verwendet `created` als Dokumentdatum.
        payload["created"] = normalized_date

    return payload


def apply_forced_tag_rules(
    *,
    patch_payload: Dict[str, Any],
    current_tag_ids: set[int],
    ki_tag_id: Optional[int],
    remove_neu_tag_id: Optional[int],
) -> None:
    """Erzwingt globale Tag-Regeln bei jeder Änderung.

    Regeln:
    - Tag `KI` hinzufügen (falls verfügbar)
    - Tag `#NEU` entfernen (falls vorhanden)
    """

    final_tag_ids = set(current_tag_ids)
    final_tag_ids.update(int(tag_id) for tag_id in patch_payload.get("tags", []))

    if remove_neu_tag_id is not None:
        final_tag_ids.discard(remove_neu_tag_id)
    if ki_tag_id is not None:
        final_tag_ids.add(ki_tag_id)

    if final_tag_ids != set(current_tag_ids):
        patch_payload["tags"] = sorted(final_tag_ids)


def build_ai_note_entry(
    *,
    prediction: Dict[str, Any],
    patch_payload: Dict[str, Any],
    doc_type_id_to_label: Dict[int, str],
    correspondent_id_to_label: Dict[int, str],
    storage_path_id_to_label: Dict[int, str],
    tag_id_to_label: Dict[int, str],
    max_chars: int,
    include_summary: bool,
    summary_max_chars: int,
) -> str:
    """Erstellt einen kompakten KI-Notizeintrag mit Begründung und Änderungen."""

    def _value_to_label(field: str, value: Any) -> str:
        if value is None:
            return "-"
        if field == "document_type":
            return doc_type_id_to_label.get(int(value), f"id:{value}")
        if field == "correspondent":
            return correspondent_id_to_label.get(int(value), f"id:{value}")
        if field == "storage_path":
            return storage_path_id_to_label.get(int(value), f"id:{value}")
        if field == "tags":
            labels = [tag_id_to_label.get(int(tag_id), f"id:{tag_id}") for tag_id in value]
            return ", ".join(sorted(labels)) if labels else "-"
        return str(value)

    lines: List[str] = []
    for field in ("document_type", "correspondent", "storage_path", "created", "tags"):
        if field in patch_payload:
            lines.append(f"- {field}: {_value_to_label(field, patch_payload[field])}")

    rationale = str(prediction.get("rationale") or "Keine Begründung angegeben.").strip()
    if len(rationale) > max_chars:
        rationale = rationale[: max_chars - 3] + "..."

    summary_line = ""
    if include_summary:
        summary = str(prediction.get("summary") or "").strip()
        if not summary:
            summary = "Keine Kurz-Zusammenfassung verfügbar."
        if len(summary) > summary_max_chars:
            summary = summary[: summary_max_chars - 3] + "..."
        summary_line = f"Kurz-Zusammenfassung: {summary}\n"

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    note = (
        f"[KI-Update {timestamp}]\n"
        f"{summary_line}"
        f"Begründung: {rationale}\n"
        f"Änderungen:\n"
        + ("\n".join(lines) if lines else "- keine")
    )
    return note


def log_dry_run_change(
    document: Dict[str, Any],
    prediction: Dict[str, Any],
    patch_payload: Dict[str, Any],
    note_will_be_added: bool,
    tag_id_to_label: Dict[int, str],
    doc_type_id_to_label: Dict[int, str],
    correspondent_id_to_label: Dict[int, str],
    storage_path_id_to_label: Dict[int, str],
) -> None:
    """Gibt im Dry-Run eine Feld-für-Feld-Diff-Ansicht aus."""

    doc_id = document.get("id")
    title = document.get("title", "<ohne Titel>")
    confidence = prediction.get("confidence")

    def _label_or_none(entity_id: Optional[int], id_to_label: Dict[int, str]) -> str:
        if entity_id is None:
            return "keiner"
        return id_to_label.get(int(entity_id), f"id:{entity_id}")

    def _tags_to_label(tags_value: Any) -> str:
        if not tags_value:
            return "keine"
        labels: List[str] = []
        for tag_id in tags_value:
            labels.append(tag_id_to_label.get(int(tag_id), f"id:{tag_id}"))
        return ", ".join(sorted(labels))

    rows: List[tuple[str, str, str]] = []
    if "document_type" in patch_payload:
        resolved_doc_type = _label_or_none(
            patch_payload.get("document_type"),
            doc_type_id_to_label,
        )
        rows.append(
            (
                "Dokumenttyp",
                _label_or_none(document.get("document_type"), doc_type_id_to_label),
                resolved_doc_type,
            )
        )
    if "correspondent" in patch_payload:
        resolved_correspondent = _label_or_none(
            patch_payload.get("correspondent"),
            correspondent_id_to_label,
        )
        rows.append(
            (
                "Korrespondent",
                _label_or_none(document.get("correspondent"), correspondent_id_to_label),
                resolved_correspondent,
            )
        )
    if "storage_path" in patch_payload:
        resolved_storage_path = _label_or_none(
            patch_payload.get("storage_path"),
            storage_path_id_to_label,
        )
        rows.append(
            (
                "Speicherpfad",
                _label_or_none(document.get("storage_path"), storage_path_id_to_label),
                resolved_storage_path,
            )
        )
    if "tags" in patch_payload:
        rows.append(
            (
                "Tags",
                _tags_to_label(document.get("tags", [])),
                _tags_to_label(patch_payload.get("tags", [])),
            )
        )
    if "created" in patch_payload:
        current_created = normalize_iso_date(str(document.get("created") or ""))
        rows.append(
            (
                "Dokumentdatum",
                current_created or "keiner",
                str(patch_payload.get("created") or "keiner"),
            )
        )
    if note_will_be_added:
        rows.append(("Notiz", "bestehend", "KI-Notiz wird ergänzt"))

    if not rows:
        LOGGER.info("DRY-RUN Dokument %s | %s | Keine Feldänderung erkannt.", doc_id, title)
        return

    field_width = 14
    old_width = 42
    new_width = 42

    def _shorten(text: str, width: int) -> str:
        if len(text) <= width:
            return text
        return text[: width - 3] + "..."

    header = f"{'Feld':<{field_width}} | {'Aktuell':<{old_width}} | {'Neu':<{new_width}}"
    sep = "-" * len(header)

    LOGGER.info("DRY-RUN Dokument %s | Titel: %s | Confidence: %s", doc_id, title, confidence)
    LOGGER.info(sep)
    LOGGER.info(header)
    LOGGER.info(sep)
    for field, old_value, new_value in rows:
        LOGGER.info(
            f"{_shorten(field, field_width):<{field_width}} | "
            f"{_shorten(old_value, old_width):<{old_width}} | "
            f"{_shorten(new_value, new_width):<{new_width}}"
        )
    LOGGER.info(sep)
    LOGGER.info("DRY-RUN Patch an Paperless: %s", patch_payload)


def log_run_details(
    *,
    created_entities: Dict[str, List[str]],
    error_details: List[Dict[str, Any]],
) -> None:
    """Gibt am Laufende eine kompakte Übersicht zu Neu-Anlagen und Fehlern aus."""

    LOGGER.info("################## DEBUG ####################")
    LOGGER.info("Kopierbereich startet hier (inklusive Fehlerdetails).")
    endpoint_labels = {
        "/api/correspondents/": "Korrespondent neu erstellt",
        "/api/document_types/": "Dokumenttyp neu erstellt",
        "/api/storage_paths/": "Speicherpfad neu erstellt",
        "/api/tags/": "Tag neu erstellt",
    }

    LOGGER.info("----- Zusammenfassung: Neu angelegte Entitäten -----")
    for endpoint, label in endpoint_labels.items():
        entries = sorted(set(created_entities.get(endpoint, [])))
        if entries:
            LOGGER.info("%s: %s", label, ", ".join(entries))
        else:
            LOGGER.info("%s: keine", label)

    LOGGER.info("----- Zusammenfassung: Fehlerdetails -----")
    if not error_details:
        LOGGER.info("Fehlerdetails: keine")
        LOGGER.info("################ ENDE DEBUG #################")
        return

    for idx, detail in enumerate(error_details, start=1):
        payload_hint = detail.get("patch_payload")
        payload_text = f" | PatchPayload={payload_hint}" if payload_hint else ""
        LOGGER.error(
            "[Fehler %s] Dokument %s (%s) | Typ=%s | Meldung=%s%s",
            idx,
            detail.get("id"),
            detail.get("title"),
            detail.get("error_type"),
            detail.get("message"),
            payload_text,
        )
    LOGGER.info("################ ENDE DEBUG #################")


def should_process_document(document: Dict[str, Any]) -> bool:
    """Definiert, welche Dokumente verarbeitet werden sollen.

    Aktuell verarbeiten wir primär Dokumente ohne Typ oder ohne Tags.
    Diese Heuristik kann project-spezifisch angepasst werden.
    """

    has_type = document.get("document_type") is not None
    has_tags = bool(document.get("tags"))
    return not (has_type and has_tags)


def process_documents(config: AppConfig, process_all_documents: bool = False) -> None:
    """Hauptablauf: Laden, KI-Klassifizieren, validieren, patchen."""

    client = PaperlessClient(config)
    classifier = AiClassifier(config)

    LOGGER.info("Prüfe KI-Token-Budget...")
    classifier.preflight_token_budget()
    LOGGER.info("Prüfe Paperless-API Erreichbarkeit...")
    client.preflight_check()
    LOGGER.info("Lade Metadaten-Mappings aus Paperless...")
    tags_map = client.list_named_entities("/api/tags/")
    doc_types_map = client.list_named_entities("/api/document_types/")
    correspondents_map = client.list_named_entities("/api/correspondents/")
    storage_paths_map = client.list_named_entities("/api/storage_paths/")
    classifier.set_known_entities(
        document_types=list(doc_types_map.keys()),
        correspondents=list(correspondents_map.keys()),
        storage_paths=list(storage_paths_map.keys()),
    )
    tag_id_to_label = {entity_id: label for label, entity_id in tags_map.items()}
    doc_type_id_to_label = {entity_id: label for label, entity_id in doc_types_map.items()}
    correspondent_id_to_label = {entity_id: label for label, entity_id in correspondents_map.items()}
    storage_path_id_to_label = {entity_id: label for label, entity_id in storage_paths_map.items()}

    scanned = 0
    updated = 0
    skipped = 0
    failed = 0
    created_entities: Dict[str, List[str]] = {}
    error_details: List[Dict[str, Any]] = []
    can_create_entities = config.create_missing_entities and not config.dry_run
    ki_tag_id = ensure_entity_id(
        client,
        tags_map,
        "KI",
        "/api/tags/",
        can_create_entities,
        created_entities,
    )
    remove_neu_tag_id = tags_map.get("#neu")
    only_tag_id: Optional[int] = None
    only_tag_name = config.process_only_tag.strip()
    doc_query_params: Dict[str, Any] = {}
    if process_all_documents:
        LOGGER.info(
            "All-Documents Modus aktiv: Tag-Filter und Standard-Skip-Regeln werden ignoriert."
        )
    elif only_tag_name:
        only_tag_id = tags_map.get(only_tag_name.lower())
        if only_tag_id is None:
            LOGGER.error(
                "Filter-Tag '%s' wurde in Paperless nicht gefunden. "
                "Prüfe Schreibweise oder lege den Tag an.",
                only_tag_name,
            )
            return
        LOGGER.info("Tag-Filter aktiv: Verarbeite nur Dokumente mit Tag '%s'.", only_tag_name)
        # Direkter API-Filter: lädt nur passende Dokumente.
        doc_query_params["tags__id"] = only_tag_id

    for document in client.iter_documents(config.max_documents, extra_params=doc_query_params):
        scanned += 1
        doc_id = document.get("id")
        title = document.get("title", "<ohne Titel>")
        doc_tags = {int(tag_id) for tag_id in document.get("tags", [])}
        patch_payload_for_error: Optional[Dict[str, Any]] = None

        # Defensive Prüfung bleibt aktiv, falls API-Filter je nach Version anders reagiert.
        if not process_all_documents and only_tag_id is not None and only_tag_id not in doc_tags:
            skipped += 1
            continue

        if not process_all_documents and only_tag_id is None and not should_process_document(document):
            LOGGER.debug("Skip Dokument %s (%s): bereits klassifiziert", doc_id, title)
            skipped += 1
            continue

        try:
            prediction = classifier.classify(document)
            prediction = sanitize_prediction(prediction, storage_paths_map)
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
                # Im Dry-Run niemals neue Entities anlegen.
                create_missing_entities=can_create_entities,
                created_entities=created_entities,
            )
            patch_payload_for_error = dict(patch_payload)

            if not patch_payload:
                LOGGER.info("Skip Dokument %s (%s): Keine verwertbaren Felder im KI-Output", doc_id, title)
                skipped += 1
                continue

            # Erzwinge globale Tag-Regeln auf Basis des aktuellen Dokuments.
            apply_forced_tag_rules(
                patch_payload=patch_payload,
                current_tag_ids=doc_tags,
                ki_tag_id=ki_tag_id,
                remove_neu_tag_id=remove_neu_tag_id,
            )

            # Nach möglichen Neuanlagen Mappings aktualisieren, damit Logs/Notizen
            # die finalen Namen statt nur IDs anzeigen.
            tag_id_to_label = {entity_id: label for label, entity_id in tags_map.items()}
            doc_type_id_to_label = {entity_id: label for label, entity_id in doc_types_map.items()}
            correspondent_id_to_label = {entity_id: label for label, entity_id in correspondents_map.items()}
            storage_path_id_to_label = {entity_id: label for label, entity_id in storage_paths_map.items()}

            if config.enable_ai_notes:
                note_entry = build_ai_note_entry(
                    prediction=prediction,
                    patch_payload=patch_payload,
                    doc_type_id_to_label=doc_type_id_to_label,
                    correspondent_id_to_label=correspondent_id_to_label,
                    storage_path_id_to_label=storage_path_id_to_label,
                    tag_id_to_label=tag_id_to_label,
                    max_chars=config.ai_notes_max_chars,
                    include_summary=config.enable_ai_note_summary,
                    summary_max_chars=config.ai_note_summary_max_chars,
                )

            if config.dry_run:
                log_dry_run_change(
                    document=document,
                    prediction=prediction,
                    patch_payload=patch_payload,
                    note_will_be_added=config.enable_ai_notes,
                    tag_id_to_label=tag_id_to_label,
                    doc_type_id_to_label=doc_type_id_to_label,
                    correspondent_id_to_label=correspondent_id_to_label,
                    storage_path_id_to_label=storage_path_id_to_label,
                )
            else:
                client.update_document(int(doc_id), patch_payload)
                if config.enable_ai_notes:
                    try:
                        client.add_document_note(int(doc_id), note_entry)
                    except PaperlessApiError as note_exc:
                        LOGGER.error(
                            "Dokument %s (%s) aktualisiert, aber KI-Notiz konnte nicht gespeichert werden: %s",
                            doc_id,
                            title,
                            note_exc,
                        )
                LOGGER.info("Aktualisiert Dokument %s (%s)", doc_id, title)

            updated += 1
        except (AiClassificationError, PaperlessApiError, ValueError) as exc:
            failed += 1
            error_details.append(
                {
                    "id": doc_id,
                    "title": title,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "patch_payload": patch_payload_for_error,
                }
            )
            LOGGER.error("Fehler bei Dokument %s (%s): %s", doc_id, title, exc)

    log_run_details(created_entities=created_entities, error_details=error_details)
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
    parser.add_argument(
        "--all-documents",
        action="store_true",
        help="Einmal alle Dokumente durchsuchen (ignoriert Tag-Filter und Standard-Skip-Regeln)",
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
        process_documents(config, process_all_documents=args.all_documents)
    except Exception as exc:  # Breiter Catch für sauberen Exit + logischen Fehlercode.
        LOGGER.exception("Unerwarteter Fehler im Hauptablauf: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
