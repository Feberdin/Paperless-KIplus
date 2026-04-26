#!/usr/bin/env python3
"""Paperless KI Sorter.

Dieses Skript lädt Dokumente aus Paperless-ngx, lässt sie durch ein LLM
klassifizieren und schreibt die vorgeschlagenen Metadaten zurück.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import requests
import yaml

from tax_enrichment import (
    TaxEnrichmentError,
    TaxPauseRequested,
    TaxEnrichmentService,
    TaxExportCollector,
    build_tax_tag_labels,
)


LOGGER = logging.getLogger("paperless_ai_sorter")
UUID_TAG_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MONETARY_SANITIZE_PATTERN = re.compile(r"[^0-9,.\-]")
RETRY_AFTER_SECONDS_PATTERN = re.compile(r"Please try again in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
RUN_STATE_VERSION = 1
RUN_STATE_FILE_DEFAULT = "paperless_kiplus_run_state.json"
STOP_REQUEST_FILE_DEFAULT = "paperless_kiplus_stop.request"
RUNTIME_EVENT_MARKER = "PAPERLESS_RUNTIME_EVENT "
RUN_PAUSE_EXIT_CODE = 75
SHORT_RATE_LIMIT_WAIT_SECONDS = 15.0
DEFAULT_AUTO_RESUME_WAIT_SECONDS = 300.0
SUPPORTED_CUSTOM_FIELD_TYPES = {
    "string",
    "date",
    "monetary",
    "integer",
    "float",
    "boolean",
    "url",
    "select",
    "documentlink",
}


class ConfigError(Exception):
    """Fehler in der Konfiguration."""


class PaperlessApiError(Exception):
    """Fehler bei einem API-Request an Paperless."""


class AiClassificationError(Exception):
    """Fehler bei KI-Klassifizierung oder Antwortformat."""


class AiTemporaryPauseError(AiClassificationError):
    """Signalisiert eine geplante Laufpause statt eines permanenten Fehlers.

    Warum wir diese eigene Fehlerart haben:
    - 429/Quota-Fälle sollen nicht als Dokumentfehler in Quarantäne laufen.
    - Der Runner kann diese Fehlerart gezielt in "Pause jetzt, später weiter"
      übersetzen.
    """

    def __init__(
        self,
        message: str,
        *,
        pause_reason: str,
        retry_after_seconds: float | None = None,
        document_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.pause_reason = pause_reason
        self.retry_after_seconds = retry_after_seconds
        self.document_context = document_context or {}


class RunPausedError(Exception):
    """Kontrollierter Stopp des Gesamtlaufs mit gespeichertem Resume-Zustand."""

    def __init__(
        self,
        message: str,
        *,
        pause_reason: str,
        retry_after_seconds: float | None = None,
        pause_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.pause_reason = pause_reason
        self.retry_after_seconds = retry_after_seconds
        self.pause_state = pause_state or {}


@dataclass(frozen=True)
class CustomFieldDefinition:
    """Beschreibt ein unterstütztes Paperless-Custom-Field.

    Warum das als feste Struktur existiert:
    - SecondBrain braucht stabile, wiedererkennbare Felder statt freier Ad-hoc-
      Schlüssel pro Dokument.
    - Paperless verlangt je Feld einen festen Datentyp. Diesen wollen wir
      zentral pflegen und nicht verstreut im Code nachbauen.
    """

    key: str
    paperless_name: str
    data_type: str
    note_label: str
    description: str
    allowed_labels: tuple[str, ...] = ()


DEFAULT_CUSTOM_FIELD_DEFINITIONS: Dict[str, CustomFieldDefinition] = {
    # Vertragsbezogene Felder helfen SecondBrain beim Aufbau von Vertrags- und
    # Fristenübersichten.
    "contract_number": CustomFieldDefinition(
        key="contract_number",
        paperless_name="Vertragsnummer",
        data_type="string",
        note_label="Vertragsnummer",
        description="Eindeutige Vertragsnummer oder Vertragskonto.",
    ),
    "customer_number": CustomFieldDefinition(
        key="customer_number",
        paperless_name="Kundennummer",
        data_type="string",
        note_label="Kundennummer",
        description="Kunden- oder Debitorennummer.",
    ),
    "contract_start_date": CustomFieldDefinition(
        key="contract_start_date",
        paperless_name="Vertragsbeginn",
        data_type="date",
        note_label="Vertragsbeginn",
        description="Startdatum des Vertrags.",
    ),
    "contract_end_date": CustomFieldDefinition(
        key="contract_end_date",
        paperless_name="Vertragsende",
        data_type="date",
        note_label="Vertragsende",
        description="Enddatum oder reguläres Laufzeitende des Vertrags.",
    ),
    "cancellation_deadline": CustomFieldDefinition(
        key="cancellation_deadline",
        paperless_name="Kündigen bis",
        data_type="date",
        note_label="Kündigen bis",
        description="Spätester Kündigungstermin laut Dokument.",
    ),
    "monthly_cost": CustomFieldDefinition(
        key="monthly_cost",
        paperless_name="Monatliche Aufwendungen",
        data_type="monetary",
        note_label="Monatliche Aufwendungen",
        description="Monatlich wiederkehrender Betrag.",
    ),
    # Lohnabrechnungen: Diese Felder helfen bei Finance- und Einkommens-
    # Auswertungen in SecondBrain.
    "payroll_gross": CustomFieldDefinition(
        key="payroll_gross",
        paperless_name="Brutto",
        data_type="monetary",
        note_label="Brutto",
        description="Bruttolohn oder Bruttogehalt.",
    ),
    "payroll_net": CustomFieldDefinition(
        key="payroll_net",
        paperless_name="Netto",
        data_type="monetary",
        note_label="Netto",
        description="Auszahlungsbetrag netto.",
    ),
    "payroll_bonus": CustomFieldDefinition(
        key="payroll_bonus",
        paperless_name="Boni",
        data_type="monetary",
        note_label="Boni",
        description="Boni, Prämien oder Sonderzahlungen.",
    ),
    "payroll_other_benefits": CustomFieldDefinition(
        key="payroll_other_benefits",
        paperless_name="Sonstige Bezüge",
        data_type="monetary",
        note_label="Sonstige Bezüge",
        description="Weitere Bezüge außerhalb des Grundlohns.",
    ),
    "payroll_taxes_social_security": CustomFieldDefinition(
        key="payroll_taxes_social_security",
        paperless_name="Steuern/Sozialabgaben",
        data_type="monetary",
        note_label="Steuern/Sozialabgaben",
        description="Summierte Steuern und Sozialabgaben.",
    ),
    "payroll_other_deductions": CustomFieldDefinition(
        key="payroll_other_deductions",
        paperless_name="Sonstige Abzüge",
        data_type="monetary",
        note_label="Sonstige Abzüge",
        description="Weitere Abzüge, z. B. Vorschüsse oder Pfändungen.",
    ),
    "payroll_total_deductions": CustomFieldDefinition(
        key="payroll_total_deductions",
        paperless_name="Abgaben gesamt",
        data_type="monetary",
        note_label="Abgaben gesamt",
        description="Gesamtsumme aller Abzüge.",
    ),
}


SECOND_BRAIN_SELECT_FIELD_LABELS: Dict[str, tuple[str, ...]] = {
    "sb_document_category": (
        "Rechnung",
        "Vertrag",
        "Bescheid",
        "Steuer",
        "Versicherung",
        "Recht",
        "Bank",
        "Gehalt",
        "Energie",
        "Fahrzeug",
        "Gesundheit",
        "Immobilie",
        "Garantie",
        "Kommunikation",
        "Sonstiges",
    ),
    "sb_life_area": (
        "Privat",
        "Arbeit",
        "Haus",
        "Auto",
        "Finanzen",
        "Steuer",
        "Recht",
        "Gesundheit",
        "Versicherung",
        "Energie",
        "Familie",
        "Technik",
    ),
    "sb_action_status": (
        "Offen",
        "In Prüfung",
        "Wartet auf Rückmeldung",
        "Erledigt",
        "Bezahlt",
        "Widersprochen",
        "Weitergeleitet",
        "Archiviert",
    ),
    "sb_action_owner": (
        "Ich",
        "Steuerberater",
        "Anwalt",
        "Arbeitgeber",
        "Versicherung",
        "Behörde",
        "Bank",
        "Sonstige",
    ),
    "sb_legal_relevance": (
        "Keine",
        "Niedrig",
        "Mittel",
        "Hoch",
        "Fristkritisch",
    ),
    "sb_financial_relevance": (
        "Keine",
        "Einnahme",
        "Ausgabe",
        "Erstattung",
        "Nachzahlung",
        "Forderung",
    ),
    "sb_tax_type": (
        "Einkommensteuer",
        "Gewerbesteuer",
        "Umsatzsteuer",
        "Lohnsteuer",
        "Grundsteuer",
        "Kapitalertragsteuer",
        "Kfz-Steuer",
        "Sonstige Steuer",
    ),
    "sb_energy_type": (
        "Strom",
        "Gas",
        "Wasser",
        "PV",
        "Einspeisung",
        "Wallbox",
        "Sonstige",
    ),
    "sb_vehicle": (
        "Tesla Model 3",
        "Anderes Fahrzeug",
        "Nicht fahrzeugbezogen",
    ),
    "sb_confidence": (
        "Manuell geprüft",
        "KI sicher",
        "KI unsicher",
        "OCR unsicher",
        "Ungeprüft",
    ),
    "sb_source_quality": (
        "Original-PDF",
        "Scan gut",
        "Scan schlecht",
        "Foto",
        "E-Mail",
        "Import",
    ),
}


def _make_secondbrain_definition(
    key: str,
    data_type: str,
    description: str,
) -> CustomFieldDefinition:
    """Erzeugt eine zentrale Felddefinition für bestehende `sb_`-Felder.

    Warum diese Hilfsfunktion existiert:
    - Alle SecondBrain-Felder folgen demselben Namensmuster `sb_*`.
    - Der sichtbare Paperless-Name ist identisch mit dem technischen Schlüssel.
      Dadurch vermeiden wir doppelte Listen mit potenziell abweichenden Namen.
    """

    return CustomFieldDefinition(
        key=key,
        paperless_name=key,
        data_type=data_type,
        note_label=key,
        description=description,
        allowed_labels=SECOND_BRAIN_SELECT_FIELD_LABELS.get(key, ()),
    )


SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS: Dict[str, CustomFieldDefinition] = {
    # Klassifizierung
    "sb_document_category": _make_secondbrain_definition(
        "sb_document_category",
        "select",
        "Übergeordnete Dokumentklasse für SecondBrain.",
    ),
    "sb_life_area": _make_secondbrain_definition(
        "sb_life_area",
        "select",
        "Lebensbereich, in den das Dokument hauptsächlich gehört.",
    ),
    # Referenzen
    "sb_case_reference": _make_secondbrain_definition("sb_case_reference", "string", "Aktenzeichen oder Vorgangsnummer."),
    "sb_contract_number": _make_secondbrain_definition("sb_contract_number", "string", "Vertragsnummer."),
    "sb_customer_number": _make_secondbrain_definition("sb_customer_number", "string", "Kundennummer oder Debitorennummer."),
    "sb_invoice_number": _make_secondbrain_definition("sb_invoice_number", "string", "Rechnungsnummer."),
    "sb_policy_number": _make_secondbrain_definition("sb_policy_number", "string", "Versicherungsnummer oder Policennummer."),
    "sb_meter_number": _make_secondbrain_definition("sb_meter_number", "string", "Zählernummer."),
    "sb_provider_name": _make_secondbrain_definition("sb_provider_name", "string", "Leistungserbringer oder Anbieter."),
    "sb_person_involved": _make_secondbrain_definition("sb_person_involved", "string", "Beteiligte Person."),
    "sb_object_reference": _make_secondbrain_definition("sb_object_reference", "string", "Objektbezug, z. B. Immobilie oder Vertragseinheit."),
    "sb_bank_account_hint": _make_secondbrain_definition("sb_bank_account_hint", "string", "IBAN-/Kontohinweis oder Kontoalias."),
    # Beträge
    "sb_amount_total": _make_secondbrain_definition("sb_amount_total", "monetary", "Gesamtbetrag."),
    "sb_amount_net": _make_secondbrain_definition("sb_amount_net", "monetary", "Nettobetrag."),
    "sb_amount_tax": _make_secondbrain_definition("sb_amount_tax", "monetary", "Steuer- oder Mehrwertsteuerbetrag."),
    # Datumsfelder
    "sb_due_date": _make_secondbrain_definition("sb_due_date", "date", "Fälligkeits- oder Fristdatum."),
    "sb_document_date": _make_secondbrain_definition("sb_document_date", "date", "Dokumentdatum."),
    "sb_period_start": _make_secondbrain_definition("sb_period_start", "date", "Beginn eines Leistungs- oder Abrechnungszeitraums."),
    "sb_period_end": _make_secondbrain_definition("sb_period_end", "date", "Ende eines Leistungs- oder Abrechnungszeitraums."),
    "sb_effective_from": _make_secondbrain_definition("sb_effective_from", "date", "Wirksam ab."),
    "sb_effective_until": _make_secondbrain_definition("sb_effective_until", "date", "Wirksam bis."),
    # Aufgaben / Status
    "sb_requires_action": _make_secondbrain_definition("sb_requires_action", "boolean", "Ob das Dokument eine konkrete Folgeaktion verlangt."),
    "sb_action_status": _make_secondbrain_definition("sb_action_status", "select", "Bearbeitungsstatus."),
    "sb_action_owner": _make_secondbrain_definition("sb_action_owner", "select", "Zuständige Person oder Stelle."),
    "sb_next_action": _make_secondbrain_definition("sb_next_action", "string", "Nächster sinnvoller Arbeitsschritt."),
    # Recht / Finanzen / Steuer
    "sb_legal_relevance": _make_secondbrain_definition("sb_legal_relevance", "select", "Rechtliche Relevanz."),
    "sb_financial_relevance": _make_secondbrain_definition("sb_financial_relevance", "select", "Finanzielle Wirkung."),
    "sb_tax_year": _make_secondbrain_definition("sb_tax_year", "integer", "Zugeordnetes Steuerjahr."),
    "sb_tax_type": _make_secondbrain_definition("sb_tax_type", "select", "Steuerart."),
    # Energie / Fahrzeug
    "sb_energy_type": _make_secondbrain_definition("sb_energy_type", "select", "Energieart."),
    "sb_vehicle": _make_secondbrain_definition("sb_vehicle", "select", "Fahrzeugbezug."),
    # Qualität / Steuerung
    "sb_confidence": _make_secondbrain_definition("sb_confidence", "select", "Vertrauensniveau der strukturierten Klassifikation."),
    "sb_source_quality": _make_secondbrain_definition("sb_source_quality", "select", "Qualität bzw. Herkunft der Quelle."),
    "sb_sensitive": _make_secondbrain_definition("sb_sensitive", "boolean", "Dokument enthält sensible Daten."),
    "sb_export_to_secondbrain": _make_secondbrain_definition("sb_export_to_secondbrain", "boolean", "Dokument für SecondBrain exportieren."),
    "sb_ignore_by_secondbrain": _make_secondbrain_definition("sb_ignore_by_secondbrain", "boolean", "Dokument für SecondBrain ignorieren."),
    # Verknüpfungen
    "sb_related_documents": _make_secondbrain_definition("sb_related_documents", "documentlink", "Verknüpfte Dokument-IDs."),
    "sb_external_url": _make_secondbrain_definition("sb_external_url", "url", "Externe Referenz-URL."),
}


SECOND_BRAIN_NOTE_KEYS = (
    "sb_document_category",
    "sb_life_area",
    "sb_case_reference",
    "sb_amount_total",
    "sb_due_date",
    "sb_requires_action",
    "sb_action_status",
    "sb_confidence",
)


@dataclass
class SecondBrainFieldSuggestion:
    """Normierte Feldempfehlung für einen einzelnen `sb_`-Wert.

    Diese Zwischenstruktur trennt bewusst:
    - Rohwerte aus der KI
    - regelbasierte Ergänzungen (z. B. Tax Enrichment)
    - spätere Auflösung gegen echte Paperless-Custom-Field-Metadaten
    """

    key: str
    value: Any
    confidence: float
    reason: str
    source: str


def validate_new_tag_name(tag_name: str) -> tuple[bool, str]:
    """Validiert neue KI-Tags, um technische Artefakte zu blockieren.

    Ziel: Keine automatisch erzeugten ID-/UUID-/Nummern-Tags in Paperless anlegen.
    Bereits vorhandene Tags im System werden nicht blockiert.
    """

    normalized = str(tag_name or "").strip()
    if not normalized:
        return False, "leer"
    if len(normalized) > 80:
        return False, "zu lang (>80 Zeichen)"
    if UUID_TAG_PATTERN.match(normalized):
        return False, "UUID-Muster erkannt"

    has_letter = any(ch.isalpha() for ch in normalized)
    if not has_letter:
        return False, "kein Buchstabe enthalten (rein numerisch/technisch)"

    digits = sum(1 for ch in normalized if ch.isdigit())
    alnum = sum(1 for ch in normalized if ch.isalnum())
    if alnum > 0 and (digits / alnum) >= 0.85 and digits >= 6:
        return False, "überwiegend numerisches Muster"

    return True, "ok"


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parst boolesche Werte robust aus YAML/Strings/Numbers.

    Wichtig für Home-Assistant-verwaltete YAMLs: Dort können Werte als String
    ankommen (z. B. "false"), was mit `bool("false")` sonst fälschlich `True`
    wäre.
    """

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "ja"}:
            return True
        if normalized in {"0", "false", "no", "off", "nein", ""}:
            return False
    return default


def normalize_confidence(value: Any, default: float = 0.0) -> float:
    """Klemmt Confidence-Werte robust auf den Bereich 0.0 bis 1.0."""

    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return max(0.0, min(1.0, confidence))


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
    metrics_file: str
    input_cost_per_1k_tokens_eur: float
    output_cost_per_1k_tokens_eur: float
    quarantine_failed_documents: bool
    failed_document_cooldown_hours: int
    failed_documents_file: str
    failed_tags_only_cooldown_hours: int
    failed_patch_cache_file: str
    enable_tag_bypass_on_tags_500: bool
    tag_bypass_file: str
    already_classified_skip: bool
    already_classified_require_ki_tag: bool
    precheck_min_content_chars: int
    precheck_min_word_count: int
    precheck_min_alnum_ratio: float
    precheck_blocked_filename_patterns: List[str]
    precheck_image_only_gate: bool
    precheck_duplicate_hash_gate: bool
    precheck_duplicate_apply_metadata: bool
    reprocess_ki_tagged_documents: bool
    enable_parallel_ai: bool
    max_parallel_ai_jobs: int
    enable_tax_enrichment: bool
    tax_export_dir: str
    tax_export_years: List[int]
    tax_personal_context: str
    tax_process_ki_tagged_documents: bool
    enable_custom_field_enrichment: bool
    create_missing_custom_fields: bool
    enable_secondbrain_custom_fields: bool
    secondbrain_custom_fields_overwrite_existing: bool
    secondbrain_custom_fields_attach_empty_when_unknown: bool
    secondbrain_custom_fields_confidence_threshold: float
    secondbrain_custom_fields_log_missing_fields: bool


@dataclass
class PendingAiDocument:
    """Dokumentkontext für spätere (ggf. parallele) KI-Klassifizierung."""

    document: Dict[str, Any]
    doc_id: Optional[int]
    doc_key: Optional[str]
    title: str
    doc_tags: set[int]
    enrichment_only: bool = False

    def to_state_dict(self) -> Dict[str, Any]:
        """Serialisiert einen Pending-Eintrag für Pause/Resume-Zwischenstände."""

        return {
            "document": self.document,
            "doc_id": self.doc_id,
            "doc_key": self.doc_key,
            "title": self.title,
            "doc_tags": sorted(self.doc_tags),
            "enrichment_only": self.enrichment_only,
        }

    @classmethod
    def from_state_dict(cls, payload: Dict[str, Any]) -> "PendingAiDocument":
        """Stellt einen Pending-Eintrag aus der State-Datei wieder her."""

        return cls(
            document=dict(payload.get("document") or {}),
            doc_id=payload.get("doc_id"),
            doc_key=payload.get("doc_key"),
            title=str(payload.get("title") or "<ohne Titel>"),
            doc_tags={
                int(tag_id)
                for tag_id in (payload.get("doc_tags") or [])
                if str(tag_id).strip()
            },
            enrichment_only=bool(payload.get("enrichment_only", False)),
        )


def load_config(config_path: str, cli_dry_run: bool, cli_max_documents: int | None = None) -> AppConfig:
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

    max_documents = int(raw.get("max_documents", 25))
    if cli_max_documents is not None and cli_max_documents > 0:
        max_documents = cli_max_documents

    blocked_patterns_raw = raw.get(
        "precheck_blocked_filename_patterns",
        ["smime", ".p7m", ".p7s", "winmail.dat", "att00001"],
    )
    tax_export_years_raw = raw.get("tax_export_years", [2025])
    if isinstance(blocked_patterns_raw, str):
        blocked_patterns = [part.strip() for part in blocked_patterns_raw.split(",") if part.strip()]
    elif isinstance(blocked_patterns_raw, list):
        blocked_patterns = [str(part).strip() for part in blocked_patterns_raw if str(part).strip()]
    else:
        blocked_patterns = ["smime", ".p7m", ".p7s", "winmail.dat", "att00001"]
    if isinstance(tax_export_years_raw, list):
        tax_export_years = []
        for value in tax_export_years_raw:
            try:
                tax_export_years.append(int(value))
            except (TypeError, ValueError):
                continue
    elif tax_export_years_raw in (None, "", []):
        tax_export_years = []
    else:
        try:
            tax_export_years = [int(tax_export_years_raw)]
        except (TypeError, ValueError):
            tax_export_years = [2025]

    secondbrain_raw = raw.get("secondbrain_custom_fields", {})
    if not isinstance(secondbrain_raw, dict):
        secondbrain_raw = {}

    return AppConfig(
        paperless_url=str(raw["paperless_url"]).rstrip("/"),
        paperless_token=str(raw["paperless_token"]),
        ai_api_key=str(raw["ai_api_key"]),
        ai_model=str(raw["ai_model"]),
        ai_base_url=str(raw.get("ai_base_url", "https://api.openai.com/v1")).rstrip("/"),
        max_documents=max_documents,
        dry_run=parse_bool(raw.get("dry_run", False), False) or cli_dry_run,
        create_missing_entities=parse_bool(raw.get("create_missing_entities", True), True),
        confidence_threshold=float(raw.get("confidence_threshold", 0.70)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 30)),
        log_level=str(raw.get("log_level", "INFO")),
        enable_token_precheck=parse_bool(raw.get("enable_token_precheck", False), False),
        min_remaining_tokens=int(raw.get("min_remaining_tokens", 1500)),
        custom_prompt_instructions=str(raw.get("custom_prompt_instructions", "")).strip(),
        basis_config=dict(raw.get("basis_config", {})),
        process_only_tag=str(raw.get("process_only_tag", "")).strip(),
        include_existing_entities_in_prompt=parse_bool(
            raw.get("include_existing_entities_in_prompt", True),
            True,
        ),
        enable_ai_notes=parse_bool(raw.get("enable_ai_notes", True), True),
        ai_notes_max_chars=int(raw.get("ai_notes_max_chars", 800)),
        enable_ai_note_summary=parse_bool(raw.get("enable_ai_note_summary", True), True),
        ai_note_summary_max_chars=int(raw.get("ai_note_summary_max_chars", 220)),
        metrics_file=str(raw.get("metrics_file", "run_metrics.json")).strip(),
        input_cost_per_1k_tokens_eur=float(raw.get("input_cost_per_1k_tokens_eur", 0.0)),
        output_cost_per_1k_tokens_eur=float(raw.get("output_cost_per_1k_tokens_eur", 0.0)),
        quarantine_failed_documents=parse_bool(raw.get("quarantine_failed_documents", True), True),
        failed_document_cooldown_hours=int(raw.get("failed_document_cooldown_hours", 24)),
        failed_documents_file=str(raw.get("failed_documents_file", "failed_documents.json")).strip(),
        failed_tags_only_cooldown_hours=int(raw.get("failed_tags_only_cooldown_hours", 168)),
        failed_patch_cache_file=str(raw.get("failed_patch_cache_file", "failed_patch_cache.json")).strip(),
        enable_tag_bypass_on_tags_500=parse_bool(raw.get("enable_tag_bypass_on_tags_500", True), True),
        tag_bypass_file=str(raw.get("tag_bypass_file", "tag_bypass_documents.json")).strip(),
        already_classified_skip=parse_bool(raw.get("already_classified_skip", True), True),
        already_classified_require_ki_tag=parse_bool(raw.get("already_classified_require_ki_tag", True), True),
        precheck_min_content_chars=int(raw.get("precheck_min_content_chars", 120)),
        precheck_min_word_count=int(raw.get("precheck_min_word_count", 20)),
        precheck_min_alnum_ratio=float(raw.get("precheck_min_alnum_ratio", 0.40)),
        precheck_blocked_filename_patterns=blocked_patterns,
        precheck_image_only_gate=parse_bool(raw.get("precheck_image_only_gate", True), True),
        precheck_duplicate_hash_gate=parse_bool(raw.get("precheck_duplicate_hash_gate", True), True),
        precheck_duplicate_apply_metadata=parse_bool(
            raw.get("precheck_duplicate_apply_metadata", True),
            True,
        ),
        reprocess_ki_tagged_documents=parse_bool(
            raw.get("reprocess_ki_tagged_documents", False),
            False,
        ),
        enable_parallel_ai=parse_bool(raw.get("enable_parallel_ai", False), False),
        max_parallel_ai_jobs=max(1, int(raw.get("max_parallel_ai_jobs", 5))),
        enable_tax_enrichment=parse_bool(raw.get("enable_tax_enrichment", False), False),
        tax_export_dir=str(raw.get("tax_export_dir", "tax_exports")).strip() or "tax_exports",
        tax_export_years=sorted(set(tax_export_years)),
        tax_personal_context=str(raw.get("tax_personal_context", "")).strip(),
        tax_process_ki_tagged_documents=parse_bool(
            raw.get("tax_process_ki_tagged_documents", False),
            False,
        ),
        enable_custom_field_enrichment=parse_bool(
            raw.get("enable_custom_field_enrichment", False),
            False,
        ),
        create_missing_custom_fields=parse_bool(
            raw.get("create_missing_custom_fields", True),
            True,
        ),
        enable_secondbrain_custom_fields=parse_bool(
            secondbrain_raw.get(
                "enabled",
                raw.get("enable_secondbrain_custom_fields", False),
            ),
            False,
        ),
        secondbrain_custom_fields_overwrite_existing=parse_bool(
            secondbrain_raw.get(
                "overwrite_existing",
                raw.get("secondbrain_custom_fields_overwrite_existing", False),
            ),
            False,
        ),
        secondbrain_custom_fields_attach_empty_when_unknown=parse_bool(
            secondbrain_raw.get(
                "attach_empty_when_unknown",
                raw.get("secondbrain_custom_fields_attach_empty_when_unknown", False),
            ),
            False,
        ),
        secondbrain_custom_fields_confidence_threshold=float(
            secondbrain_raw.get(
                "confidence_threshold",
                raw.get("secondbrain_custom_fields_confidence_threshold", 0.70),
            )
        ),
        secondbrain_custom_fields_log_missing_fields=parse_bool(
            secondbrain_raw.get(
                "log_missing_fields",
                raw.get("secondbrain_custom_fields_log_missing_fields", True),
            ),
            True,
        ),
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
        limit: Optional[int],
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Iterable[Dict[str, Any]]:
        """Lädt Dokumente seitenweise.

        Standardmäßig nutzen wir `ordering=-created`, damit zuerst neue Dokumente
        verarbeitet werden. Die Filterlogik kann später leicht erweitert werden.
        """

        next_url = "/api/documents/"
        page_size = 100 if limit is None else min(limit, 100)
        params: Optional[Dict[str, Any]] = {
            "ordering": "-created",
            "page_size": page_size,
        }
        if extra_params:
            params.update(extra_params)
        loaded = 0

        while next_url and (limit is None or loaded < limit):
            page = self._request("GET", next_url, params=params)
            params = None

            for doc in page.get("results", []):
                yield doc
                loaded += 1
                if limit is not None and loaded >= limit:
                    break

            next_url = str(page.get("next") or "")

    def count_documents(self, extra_params: Optional[Dict[str, Any]] = None) -> int:
        """Liest die Paperless-Gesamtzahl für die aktuelle Dokumentabfrage.

        Warum diese Hilfsfunktion existiert:
        - Für eine echte Fortschrittsanzeige brauchen wir eine belastbare
          Zielgröße.
        - Wir fragen nur die erste Seite ab und nutzen den `count`-Wert der API,
          statt alle Dokumente doppelt laden zu müssen.
        """

        params: Dict[str, Any] = {
            "ordering": "-created",
            "page_size": 1,
        }
        if extra_params:
            params.update(extra_params)
        page = self._request("GET", "/api/documents/", params=params)
        try:
            return max(0, int(page.get("count", 0) or 0))
        except (TypeError, ValueError):
            return 0

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

    def list_custom_fields(self) -> Dict[str, Dict[str, Any]]:
        """Lädt bestehende Paperless-Custom-Fields inkl. Datentypen und Optionen.

        Rückgabe:
        - Key: normalisierter Feldname (`lower()`)
        - Value: `{id, name, data_type, extra_data, select_options_by_label}`
        """

        mapping: Dict[str, Dict[str, Any]] = {}
        next_url: str = "/api/custom_fields/"
        params: Optional[Dict[str, Any]] = {"page_size": 100}

        while next_url:
            page = self._request("GET", next_url, params=params)
            params = None
            for item in page.get("results", []):
                field_name = str(item.get("name") or "").strip()
                if not field_name:
                    continue
                extra_data = item.get("extra_data")
                if not isinstance(extra_data, dict):
                    extra_data = {}
                mapping[field_name.lower()] = {
                    "id": int(item["id"]),
                    "name": field_name,
                    "data_type": str(item.get("data_type") or "").strip().lower(),
                    "extra_data": extra_data,
                    "select_options_by_label": build_select_option_lookup(extra_data),
                }

            next_url = str(page.get("next") or "")

        return mapping

    def get_custom_fields_by_name(self) -> Dict[str, Dict[str, Any]]:
        """Alias mit sprechenderem Namen für die Aufrufer-Logik."""

        return self.list_custom_fields()

    def find_classified_duplicate(
        self,
        *,
        current_document_id: int,
        checksum: str,
    ) -> Optional[Dict[str, Any]]:
        """Sucht ein bereits klassifiziertes Dokument mit gleicher checksum."""

        if not checksum:
            return None

        query_variants = (
            {"checksum": checksum, "page_size": 50, "ordering": "-created"},
            {"checksum__exact": checksum, "page_size": 50, "ordering": "-created"},
        )
        for params in query_variants:
            try:
                page = self._request("GET", "/api/documents/", params=params, retries=1)
            except PaperlessApiError:
                continue
            for item in page.get("results", []):
                try:
                    item_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if item_id == int(current_document_id):
                    continue
                if item.get("document_type") is None:
                    continue
                if not item.get("tags"):
                    continue
                return item
        return None

    def create_entity(self, path: str, name: str) -> int:
        """Erzeugt ein Metadaten-Objekt in Paperless und gibt dessen ID zurück."""
        if path == "/api/storage_paths/":
            # Paperless-Versionen unterscheiden sich: manche erwarten `path`,
            # manche `name`, manche beide Felder gleichzeitig.
            last_exc: Optional[Exception] = None
            for payload in (
                {"name": name, "path": name},
                {"path": name},
                {"name": name},
            ):
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

    def create_custom_field(self, definition: CustomFieldDefinition) -> Dict[str, Any]:
        """Legt ein neues Paperless-Custom-Field mit festem Datentyp an."""

        if definition.data_type not in SUPPORTED_CUSTOM_FIELD_TYPES:
            raise PaperlessApiError(
                f"Nicht unterstützter Custom-Field-Datentyp: {definition.data_type}"
            )
        created = self._request(
            "POST",
            "/api/custom_fields/",
            payload={
                "name": definition.paperless_name,
                "data_type": definition.data_type,
            },
        )
        created_id = created.get("id")
        if created_id is None:
            raise PaperlessApiError(
                f"Custom Field wurde erstellt, aber ohne ID zurückgegeben: {definition.paperless_name}"
            )
        return {
            "id": int(created_id),
            "name": definition.paperless_name,
            "data_type": definition.data_type,
        }

    def patch_document_custom_fields(
        self,
        document_id: int,
        values: Dict[int, Any],
        *,
        empty_custom_field_ids: Optional[List[int]] = None,
        remove_custom_field_ids: Optional[List[int]] = None,
    ) -> None:
        """Schreibt Custom-Field-Werte auf ein Dokument.

        Primär versuchen wir ein normales Dokument-PATCH. Falls die lokale
        Paperless-Version das nicht akzeptiert, fällt die Methode auf den
        dokumentierten Bulk-Edit-Weg `modify_custom_fields` für genau ein
        Dokument zurück.
        """

        empty_ids = sorted(
            {
                int(custom_field_id)
                for custom_field_id in (empty_custom_field_ids or [])
            }
        )
        remove_ids = sorted(
            {
                int(custom_field_id)
                for custom_field_id in (remove_custom_field_ids or [])
            }
        )
        if not values and not empty_ids and not remove_ids:
            return

        if not empty_ids and not remove_ids:
            try:
                self._request(
                    "PATCH",
                    f"/api/documents/{document_id}/",
                    payload={"custom_fields": values},
                    retries=2,
                )
                return
            except PaperlessApiError as exc:
                LOGGER.warning(
                    "Direktes PATCH für Custom Fields fehlgeschlagen, nutze Bulk-Edit-Fallback: %s",
                    exc,
                )

        if values or remove_ids:
            self._request(
                "POST",
                "/api/documents/bulk_edit/",
                payload={
                    "documents": [int(document_id)],
                    "method": "modify_custom_fields",
                    "parameters": {
                        "add_custom_fields": values,
                        "remove_custom_fields": remove_ids,
                    },
                },
                retries=2,
            )
        if empty_ids:
            self._request(
                "POST",
                "/api/documents/bulk_edit/",
                payload={
                    "documents": [int(document_id)],
                    "method": "modify_custom_fields",
                    "parameters": {
                        "add_custom_fields": empty_ids,
                        "remove_custom_fields": [],
                    },
                },
                retries=2,
            )

    def update_document_custom_fields(
        self,
        document_id: int,
        custom_fields_payload: Dict[int, Any],
        *,
        empty_custom_field_ids: Optional[List[int]] = None,
        remove_custom_field_ids: Optional[List[int]] = None,
    ) -> None:
        """Abwärtskompatibler Wrapper für bestehende Aufrufer."""

        self.patch_document_custom_fields(
            document_id,
            custom_fields_payload,
            empty_custom_field_ids=empty_custom_field_ids,
            remove_custom_field_ids=remove_custom_field_ids,
        )

    def update_document(self, document_id: int, patch_payload: Dict[str, Any]) -> None:
        """Schreibt klassifizierte Felder zurück auf das Dokument."""
        metadata_payload = {
            key: value
            for key, value in patch_payload.items()
            if key not in {"custom_fields", "custom_fields_empty", "custom_fields_remove"}
        }
        custom_fields_payload = patch_payload.get("custom_fields")
        empty_custom_fields_payload = patch_payload.get("custom_fields_empty")
        remove_custom_fields_payload = patch_payload.get("custom_fields_remove")
        try:
            if metadata_payload:
                self._request("PATCH", f"/api/documents/{document_id}/", payload=metadata_payload)
            if (
                isinstance(custom_fields_payload, dict)
                and custom_fields_payload
            ) or (
                isinstance(empty_custom_fields_payload, list)
                and empty_custom_fields_payload
            ) or (
                isinstance(remove_custom_fields_payload, list)
                and remove_custom_fields_payload
            ):
                self.update_document_custom_fields(
                    document_id,
                    custom_fields_payload if isinstance(custom_fields_payload, dict) else {},
                    empty_custom_field_ids=(
                        empty_custom_fields_payload
                        if isinstance(empty_custom_fields_payload, list)
                        else []
                    ),
                    remove_custom_field_ids=(
                        remove_custom_fields_payload
                        if isinstance(remove_custom_fields_payload, list)
                        else []
                    ),
                )
            return
        except PaperlessApiError as exc:
            # Einige Paperless-Installationen liefern sporadisch 500 bei bestimmten
            # Feldkombinationen. Wir versuchen dann gezielte Fallback-Payloads.
            if "HTTP 500" not in str(exc):
                raise

            fallback_candidates: List[tuple[str, Dict[str, Any]]] = []
            if "created" in metadata_payload:
                p = dict(metadata_payload)
                p.pop("created", None)
                fallback_candidates.append(("ohne created", p))
            if "tags" in metadata_payload:
                p = dict(metadata_payload)
                p.pop("tags", None)
                fallback_candidates.append(("ohne tags", p))
            if "created" in metadata_payload and "tags" in metadata_payload:
                p = dict(metadata_payload)
                p.pop("created", None)
                p.pop("tags", None)
                fallback_candidates.append(("ohne created+tags", p))

            tried: set[str] = set()
            last_error: Exception = exc
            for label, candidate in fallback_candidates:
                if not candidate:
                    continue
                signature = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
                if signature in tried:
                    continue
                tried.add(signature)
                try:
                    self._request(
                        "PATCH",
                        f"/api/documents/{document_id}/",
                        payload=candidate,
                        retries=2,
                    )
                    LOGGER.warning(
                        "PATCH-Fallback erfolgreich für Dokument %s (%s). Originalpayload führte zu HTTP 500.",
                        document_id,
                        label,
                    )
                    return
                except PaperlessApiError as fallback_exc:
                    last_error = fallback_exc

            # Letzte Eskalationsstufe: Feld-für-Feld patchen, um wenigstens
            # einen Teil der Änderungen zu übernehmen und den "Problem-Key" einzugrenzen.
            field_order = ["document_type", "correspondent", "storage_path", "created", "tags"]
            partial_success = False
            field_failures: List[str] = []
            for key in field_order:
                if key not in metadata_payload:
                    continue
                single_payload = {key: metadata_payload[key]}
                try:
                    self._request(
                        "PATCH",
                        f"/api/documents/{document_id}/",
                        payload=single_payload,
                        retries=2,
                    )
                    partial_success = True
                except PaperlessApiError as field_exc:
                    field_failures.append(f"{key}: {field_exc}")

            if partial_success:
                if (
                    isinstance(custom_fields_payload, dict)
                    and custom_fields_payload
                ) or (
                    isinstance(empty_custom_fields_payload, list)
                    and empty_custom_fields_payload
                ) or (
                    isinstance(remove_custom_fields_payload, list)
                    and remove_custom_fields_payload
                ):
                    self.update_document_custom_fields(
                        document_id,
                        custom_fields_payload if isinstance(custom_fields_payload, dict) else {},
                        empty_custom_field_ids=(
                            empty_custom_fields_payload
                            if isinstance(empty_custom_fields_payload, list)
                            else []
                        ),
                        remove_custom_field_ids=(
                            remove_custom_fields_payload
                            if isinstance(remove_custom_fields_payload, list)
                            else []
                        ),
                    )
                LOGGER.warning(
                    "PATCH nur teilweise erfolgreich für Dokument %s. Fehlgeschlagene Felder: %s",
                    document_id,
                    "; ".join(field_failures) if field_failures else "keine",
                )
                return

            field_failure_text = "; ".join(field_failures) if field_failures else "keine"
            raise PaperlessApiError(
                f"{exc} | Fallback-PATCH ebenfalls fehlgeschlagen: {last_error} | "
                f"Feldanalyse: {field_failure_text}"
            )

    def add_document_note(self, document_id: int, note: str) -> None:
        """Fügt eine Notiz über den dedizierten Notes-Endpoint hinzu."""

        response = self._request(
            "POST",
            f"/api/documents/{document_id}/notes/",
            payload={"note": note},
        )
        note_id = None
        if isinstance(response, dict):
            note_id = response.get("id")
        note_preview = note.splitlines()[0].strip() if note else ""
        if len(note_preview) > 120:
            note_preview = note_preview[:117] + "..."
        LOGGER.info(
            "Notiz gespeichert für Dokument %s | Note-ID=%s | Vorschau=%s",
            document_id,
            note_id if note_id is not None else "-",
            note_preview or "-",
        )

    def has_ki_summary_note(self, document_id: int) -> bool:
        """Prüft, ob ein Dokument eine KI-Update-Notiz mit Kurz-Zusammenfassung hat."""

        next_path: Optional[str] = f"/api/documents/{document_id}/notes/"
        while next_path:
            page = self._request(
                "GET",
                next_path,
                params={"page_size": 100} if next_path.startswith("/api/") else None,
                retries=1,
            )
            # API-Varianten:
            # - paginiert: {"results": [...], "next": ...}
            # - direkt: [...]
            if isinstance(page, dict):
                results = page.get("results") or []
                next_url = page.get("next")
            elif isinstance(page, list):
                results = page
                next_url = None
            else:
                break
            if not isinstance(results, list):
                break

            for item in results:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("note") or "")
                text_lower = text.lower()
                if "[ki-update" in text_lower and "kurz-zusammenfassung:" in text_lower:
                    return True

            if not next_url:
                break
            if next_url.startswith(self.base_url):
                next_path = next_url[len(self.base_url) :]
            else:
                next_path = str(next_url)
        return False


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
        self.enable_custom_field_enrichment = config.enable_custom_field_enrichment
        self.enable_secondbrain_custom_fields = config.enable_secondbrain_custom_fields
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

    @staticmethod
    def _build_pause_error_from_http_error(exc: requests.HTTPError) -> Optional[AiTemporaryPauseError]:
        """Übersetzt 429-Antworten in eine geplante Laufpause statt Dokumentfehler."""

        response = getattr(exc, "response", None)
        if response is None:
            return None
        try:
            status_code = int(response.status_code)
        except (TypeError, ValueError):
            return None
        if status_code != 429:
            return None

        response_text = getattr(response, "text", "") or str(exc)
        retry_after_seconds = extract_retry_after_seconds_from_error(
            response_text,
            getattr(response, "headers", None),
        )
        normalized_text = response_text.lower()
        if "insufficient_quota" in normalized_text:
            return AiTemporaryPauseError(
                "KI-Quota erschöpft. Lauf wird pausiert und kann später fortgesetzt werden.",
                pause_reason="quota_exhausted",
                retry_after_seconds=retry_after_seconds or DEFAULT_AUTO_RESUME_WAIT_SECONDS,
            )
        if "rate_limit" in normalized_text or "tokens per min" in normalized_text:
            return AiTemporaryPauseError(
                "KI-Rate-Limit erreicht. Lauf wird kontrolliert pausiert.",
                pause_reason="rate_limit_wait",
                retry_after_seconds=retry_after_seconds or DEFAULT_AUTO_RESUME_WAIT_SECONDS,
            )
        return AiTemporaryPauseError(
            "KI-Provider meldet 429. Lauf wird kontrolliert pausiert.",
            pause_reason="provider_backoff",
            retry_after_seconds=retry_after_seconds or DEFAULT_AUTO_RESUME_WAIT_SECONDS,
        )

    def classify(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """Sendet Dokumentkontext an KI und erwartet streng JSON als Antwort."""

        prompt = (
            "Du bist ein präziser Dokumenten-Klassifizierer für Paperless-ngx. "
            "Antworte ausschließlich als JSON mit den Feldern: "
            "document_type, correspondent, storage_path, tags (Liste), "
            "document_date (YYYY-MM-DD oder null), summary, confidence (0-1), rationale. "
            "Keine zusätzlichen Schlüssel, keine Markdown-Ausgabe."
        )
        if self.enable_custom_field_enrichment:
            custom_field_specs = [
                {
                    "key": definition.key,
                    "field_name": definition.paperless_name,
                    "type": definition.data_type,
                    "description": definition.description,
                }
                for definition in DEFAULT_CUSTOM_FIELD_DEFINITIONS.values()
            ]
            prompt += (
                "\n\nOptional darfst du zusätzlich das Feld `custom_fields` liefern. "
                "Das muss ein JSON-Objekt sein. Nutze nur die unten aufgeführten "
                "Schlüssel, nur wenn der Wert im Dokument klar erkennbar ist. "
                "Lasse irrelevante Schlüssel weg. Datumswerte immer als YYYY-MM-DD. "
                "Monetäre Werte als String im Format EUR12.34."
                "\nUnterstützte benutzerdefinierte Felder:\n"
                + json.dumps(custom_field_specs, ensure_ascii=False)
            )
        if self.enable_secondbrain_custom_fields:
            secondbrain_specs = [
                {
                    "key": definition.key,
                    "field_name": definition.paperless_name,
                    "type": definition.data_type,
                    "description": definition.description,
                    "allowed_labels": list(definition.allowed_labels),
                }
                for definition in SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS.values()
            ]
            prompt += (
                "\n\nOptional darfst du zusätzlich das Feld `secondbrain_custom_fields` liefern. "
                "Das muss ein JSON-Objekt sein. Jeder Schlüssel muss einem bekannten `sb_`-Feld "
                "entsprechen und als Wert ein Objekt mit `value`, `confidence` und `reason` haben. "
                "Nutze nur Felder, die durch das Dokument klar begründet sind. "
                "Lasse unsichere Felder lieber ganz weg. "
                "Datumswerte immer als YYYY-MM-DD. "
                "Monetäre Werte als Dezimalzahl mit Punkt, z. B. 123.45. "
                "Boolean-Werte nur true/false. Integer-Werte als ganze Zahl. "
                "Bei Select-Feldern bitte das sichtbare Label verwenden, nicht eine ID."
                "\nUnterstützte SecondBrain-Felder:\n"
                + json.dumps(secondbrain_specs, ensure_ascii=False)
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

        max_attempts = 3
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(req_body),
                    timeout=self.timeout,
                )
                status_code = int(response.status_code)
                if status_code == 429 or status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {status_code}: {response.text}",
                        response=response,
                    )
                response.raise_for_status()
                raw = response.json()
                message = raw["choices"][0]["message"]["content"]
                parsed = json.loads(message)
                self._validate_model_output(parsed)
                usage = raw.get("usage") or {}
                parsed["_meta_usage"] = {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "total_tokens": int(usage.get("total_tokens", 0) or 0),
                }
                return parsed
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_exc = exc
                if isinstance(exc, requests.HTTPError):
                    pause_exc = self._build_pause_error_from_http_error(exc)
                    if pause_exc is not None:
                        wait_seconds = float(pause_exc.retry_after_seconds or 0.0)
                        if wait_seconds > 0.0 and wait_seconds <= SHORT_RATE_LIMIT_WAIT_SECONDS:
                            LOGGER.warning(
                                "KI-Rate-Limit erreicht (Versuch %s/%s), warte %.2fs und versuche es erneut.",
                                attempt,
                                max_attempts,
                                wait_seconds,
                            )
                            time.sleep(wait_seconds)
                            continue
                        raise pause_exc from exc
                if attempt < max_attempts:
                    wait_seconds = 0.7 * (2 ** (attempt - 1))
                    LOGGER.warning(
                        "KI-Request fehlgeschlagen (Versuch %s/%s), Retry in %.1fs: %s",
                        attempt,
                        max_attempts,
                        wait_seconds,
                        exc,
                    )
                    time.sleep(wait_seconds)
                    continue
                break
            except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
                # Nicht-transiente Fehler (z. B. ungültige Antwortstruktur) direkt zurückgeben.
                raise AiClassificationError(
                    f"KI-Antwort ungültig oder Request fehlgeschlagen: {exc}"
                ) from exc

        raise AiClassificationError(
            f"KI-Antwort ungültig oder Request fehlgeschlagen: {last_exc}"
        ) from last_exc

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
        custom_fields = payload.get("custom_fields")
        if custom_fields is not None and not isinstance(custom_fields, dict):
            raise AiClassificationError("KI-Ausgabe: 'custom_fields' muss ein Objekt oder null sein.")
        secondbrain_custom_fields = payload.get("secondbrain_custom_fields")
        if secondbrain_custom_fields is not None and not isinstance(secondbrain_custom_fields, dict):
            raise AiClassificationError(
                "KI-Ausgabe: 'secondbrain_custom_fields' muss ein Objekt oder null sein."
            )


def normalize_monetary_value(value: Any, *, output_format: str = "paperless") -> Optional[str]:
    """Normalisiert Geldbeträge entweder für Paperless oder als Dezimalzahl.

    Beispiel:
    - `12,34 €` -> `EUR12.34` (Paperless)
    - `12,34 €` -> `12.34` (decimal)
    - `EUR 49.9` -> `EUR49.90`
    - `{"currency": "EUR", "amount": "12,34"}` -> `EUR12.34`
    """

    if value in (None, ""):
        return None

    currency = "EUR"
    raw_amount: Any = value
    if isinstance(value, dict):
        raw_currency = str(value.get("currency") or "").strip().upper()
        if len(raw_currency) == 3:
            currency = raw_currency
        raw_amount = value.get("amount")
    elif isinstance(value, str):
        normalized = value.strip()
        if "€" in normalized or "eur" in normalized.lower():
            currency = "EUR"
        currency_match = re.search(r"\b([A-Z]{3})\b", normalized.upper())
        if currency_match:
            currency = currency_match.group(1)
        raw_amount = normalized

    if raw_amount in (None, ""):
        return None

    amount_text = MONETARY_SANITIZE_PATTERN.sub("", str(raw_amount).strip())
    if not amount_text:
        return None
    if "," in amount_text and "." in amount_text:
        if amount_text.rfind(",") > amount_text.rfind("."):
            amount_text = amount_text.replace(".", "").replace(",", ".")
        else:
            amount_text = amount_text.replace(",", "")
    elif "," in amount_text:
        amount_text = amount_text.replace(".", "").replace(",", ".")
    try:
        normalized_amount = Decimal(amount_text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if output_format == "decimal":
        return f"{normalized_amount}"
    return f"{currency}{normalized_amount}"


def normalize_optional_bool(value: Any) -> Optional[bool]:
    """Parst boolesche Werte ohne stillen Fallback auf False.

    Warum eine eigene Funktion:
    - Für Custom-Field-Werte bedeutet `False` fachlich etwas anderes als
      "unbekannt".
    - `parse_bool(..., False)` wäre hier zu aggressiv und würde invalide Werte
      versehentlich zu `False` machen.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "ja"}:
            return True
        if normalized in {"0", "false", "no", "off", "nein"}:
            return False
    return None


def normalize_document_link_value(value: Any) -> Optional[List[int]]:
    """Normalisiert Document-Link-Werte auf eine stabile ID-Liste."""

    if value in (None, "", []):
        return None

    raw_items: List[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = [part.strip() for part in value.split(",") if part.strip()]
    else:
        raw_items = [value]

    normalized: List[int] = []
    seen: set[int] = set()
    for item in raw_items:
        try:
            doc_id = int(str(item).strip())
        except (TypeError, ValueError):
            return None
        if doc_id not in seen:
            normalized.append(doc_id)
            seen.add(doc_id)
    return normalized or None


def build_select_option_lookup(extra_data: Dict[str, Any]) -> Dict[str, int]:
    """Indexiert Select-Optionen tolerant nach sichtbarem Label.

    Paperless API v7 liefert laut offizieller Doku `select_options` als Liste
    von Objekten mit `id` und `label`. Für Robustheit akzeptieren wir zusätzlich
    Varianten wie `value` oder `name`, falls ältere oder angepasste Payloads
    auftauchen.
    """

    options = extra_data.get("select_options")
    if not isinstance(options, list):
        return {}

    by_label: Dict[str, int] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        try:
            option_id = int(option.get("id"))
        except (TypeError, ValueError):
            continue
        label = str(
            option.get("label")
            or option.get("value")
            or option.get("name")
            or ""
        ).strip()
        if not label:
            continue
        by_label[label.lower()] = option_id
    return by_label


def normalize_custom_field_value(definition: CustomFieldDefinition, value: Any) -> Any:
    """Normalisiert einen KI-Wert gemäß dem erwarteten Paperless-Datentyp."""

    if value in (None, "", []):
        return None

    if definition.data_type == "string":
        normalized = str(value).strip()
        return normalized or None
    if definition.data_type == "date":
        return normalize_iso_date(value)
    if definition.data_type == "monetary":
        return normalize_monetary_value(value)
    if definition.data_type == "integer":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
    if definition.data_type == "float":
        try:
            return float(str(value).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None
    if definition.data_type == "boolean":
        return normalize_optional_bool(value)
    if definition.data_type == "url":
        normalized = str(value).strip()
        return normalized or None
    if definition.data_type == "select":
        if isinstance(value, (int, float)):
            return int(value)
        normalized = str(value).strip()
        return normalized or None
    if definition.data_type == "documentlink":
        return normalize_document_link_value(value)
    return None


def normalize_prediction_custom_fields(
    prediction: Dict[str, Any],
    definitions: Dict[str, CustomFieldDefinition],
) -> Dict[str, Any]:
    """Filtert und normalisiert die von der KI gelieferten Custom-Field-Werte.

    Warum das wichtig ist:
    - Das Modell darf nur bekannte Schlüssel zurückgeben.
    - Paperless akzeptiert Datentypen streng; fehlerhafte Rohwerte würden sonst
      erst spät beim PATCH scheitern.
    """

    raw_custom_fields = prediction.get("custom_fields")
    if not isinstance(raw_custom_fields, dict):
        return {}

    normalized_payload: Dict[str, Any] = {}
    for raw_key, raw_value in raw_custom_fields.items():
        definition = definitions.get(str(raw_key).strip())
        if definition is None:
            LOGGER.warning(
                "Unbekanntes Custom-Field aus KI-Antwort ignoriert: %s",
                raw_key,
            )
            continue
        normalized_value = normalize_custom_field_value(definition, raw_value)
        if normalized_value is None:
            continue
        normalized_payload[definition.key] = normalized_value
    return normalized_payload


def normalize_secondbrain_prediction_fields(
    prediction: Dict[str, Any],
    definitions: Dict[str, CustomFieldDefinition],
) -> Dict[str, SecondBrainFieldSuggestion]:
    """Normalisiert KI-Vorschläge für `sb_`-Custom-Fields.

    Erwartetes Format:
    {
      "sb_field": {"value": ..., "confidence": 0.91, "reason": "..."}
    }

    Für Robustheit akzeptieren wir zusätzlich nackte Rohwerte; dann wird die
    Haupt-Confidence der Dokumentklassifikation als Fallback verwendet.
    """

    raw_fields = prediction.get("secondbrain_custom_fields")
    if not isinstance(raw_fields, dict):
        return {}

    default_confidence = normalize_confidence(prediction.get("confidence"), 0.0)
    suggestions: Dict[str, SecondBrainFieldSuggestion] = {}
    for raw_key, raw_entry in raw_fields.items():
        key = str(raw_key).strip()
        definition = definitions.get(key)
        if definition is None:
            LOGGER.warning(
                "Unbekanntes SecondBrain-Custom-Field aus KI-Antwort ignoriert: %s",
                raw_key,
            )
            continue

        if isinstance(raw_entry, dict):
            raw_value = raw_entry.get("value")
            confidence = normalize_confidence(raw_entry.get("confidence"), default_confidence)
            reason = str(raw_entry.get("reason") or "").strip()
        else:
            raw_value = raw_entry
            confidence = default_confidence
            reason = "KI lieferte einen Rohwert ohne Detailobjekt."

        if raw_value in (None, "", []):
            suggestions[key] = SecondBrainFieldSuggestion(
                key=key,
                value=None,
                confidence=confidence,
                reason=reason or "Kein belastbarer Wert erkennbar.",
                source="ai",
            )
            continue

        if definition.data_type == "monetary":
            normalized_value = normalize_monetary_value(raw_value, output_format="decimal")
        else:
            normalized_value = normalize_custom_field_value(definition, raw_value)
        if normalized_value is None:
            LOGGER.warning(
                "SecondBrain-KI-Wert konnte nicht normalisiert werden: %s=%r",
                key,
                raw_value,
            )
            continue

        suggestions[key] = SecondBrainFieldSuggestion(
            key=key,
            value=normalized_value,
            confidence=confidence,
            reason=reason or "Aus Dokumentinhalt abgeleitet.",
            source="ai",
        )
    return suggestions


def has_meaningful_custom_field_value(value: Any) -> bool:
    """Prüft, ob ein bestehender Custom-Field-Wert fachlich als gesetzt gilt."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return bool(value)
    return True


def resolve_custom_field_value(
    field: Dict[str, Any],
    raw_value: Any,
) -> tuple[Any, Optional[str]]:
    """Übersetzt einen normierten Rohwert in einen Paperless-kompatiblen API-Wert.

    Rückgabe:
    - `(resolved_value, None)` bei Erfolg
    - `(None, reason)` bei ungültigem oder nicht auflösbarem Wert
    """

    data_type = str(field.get("data_type") or "").strip().lower()
    field_name = str(field.get("name") or field.get("id") or "<unbekannt>")

    if raw_value is None:
        return None, None
    if data_type == "string":
        normalized = str(raw_value).strip()
        return (normalized or None), ("leerer String" if not normalized else None)
    if data_type == "date":
        normalized = normalize_iso_date(raw_value)
        return normalized, None if normalized is not None else "ungueltiges Datum"
    if data_type == "monetary":
        normalized = normalize_monetary_value(raw_value, output_format="paperless")
        return normalized, None if normalized is not None else "ungueltiger Geldbetrag"
    if data_type == "integer":
        try:
            return int(str(raw_value).strip()), None
        except (TypeError, ValueError):
            return None, "ungueltige Ganzzahl"
    if data_type == "float":
        try:
            return float(str(raw_value).strip().replace(",", ".")), None
        except (TypeError, ValueError):
            return None, "ungueltige Fließkommazahl"
    if data_type == "boolean":
        normalized_bool = normalize_optional_bool(raw_value)
        return normalized_bool, None if normalized_bool is not None else "ungueltiger Boolean"
    if data_type == "url":
        normalized = str(raw_value).strip()
        if normalized.startswith(("http://", "https://")):
            return normalized, None
        return None, "ungueltige URL"
    if data_type == "documentlink":
        normalized_links = normalize_document_link_value(raw_value)
        return normalized_links, None if normalized_links is not None else "ungueltige Dokumentverknüpfung"
    if data_type == "select":
        if isinstance(raw_value, (int, float)):
            return int(raw_value), None
        select_lookup = field.get("select_options_by_label") or {}
        if not isinstance(select_lookup, dict):
            select_lookup = {}
        label = str(raw_value).strip()
        if not label:
            return None, "leerer Select-Wert"
        option_id = select_lookup.get(label.lower())
        if option_id is None:
            return None, f"Select-Option nicht gefunden fuer '{label}' in Feld '{field_name}'"
        return int(option_id), None
    return None, f"nicht unterstützter Datentyp '{data_type}'"


def build_secondbrain_sync_report() -> Dict[str, Any]:
    """Erzeugt die Sammelstruktur für Debug-Informationen zum `sb_`-Sync."""

    return {
        "enabled": False,
        "prepared": {},
        "written": {},
        "cleared": [],
        "below_threshold": {},
        "preserved_existing": {},
        "missing_fields": [],
        "unresolved_selects": {},
        "invalid_values": {},
        "api_errors": [],
    }


def collect_populated_secondbrain_fields(
    document: Dict[str, Any],
    custom_fields_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """Sammelt bereits befüllte `sb_`-Felder eines Dokuments.

    Warum diese Prüfung wichtig ist:
    - Backfill-Läufe sollen keine neuen KI-Tokens verbrauchen, wenn das
      Dokument für SecondBrain bereits vorbereitet ist.
    - Gleichzeitig wollen wir tolerant gegenüber unterschiedlichen
      Paperless-Payload-Formaten bleiben.

    Beispiel:
    - Input: Dokument mit `sb_document_category=Rechnung`
    - Output: `["sb_document_category"]`
    """

    populated: List[str] = []
    existing_values = extract_document_custom_field_values(document)

    if custom_fields_map:
        for definition in SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS.values():
            field = custom_fields_map.get(definition.paperless_name.lower())
            if field is None:
                continue
            current_value = get_existing_custom_field_value(document, field, definition)
            if has_meaningful_custom_field_value(current_value):
                populated.append(definition.key)

    if populated:
        return sorted(set(populated))

    for raw_key, raw_value in existing_values.items():
        normalized_key = str(raw_key).strip().lower()
        if not normalized_key.startswith("sb_"):
            continue
        if has_meaningful_custom_field_value(raw_value):
            populated.append(normalized_key)
    return sorted(set(populated))


def should_mark_secondbrain_ready(sync_report: Optional[Dict[str, Any]]) -> bool:
    """Entscheidet, ob ein Dokument als SecondBrain-vorbereitet markiert werden soll."""

    if not isinstance(sync_report, dict):
        return False
    return bool((sync_report.get("written") or {}) or (sync_report.get("preserved_existing") or {}))


def set_secondbrain_suggestion_if_missing(
    suggestions: Dict[str, SecondBrainFieldSuggestion],
    *,
    key: str,
    value: Any,
    confidence: float,
    reason: str,
    source: str,
) -> None:
    """Ergänzt nur fehlende `sb_`-Werte, um KI-Vorschläge nicht blind zu verdrängen."""

    if key in suggestions or value in (None, "", []):
        return
    suggestions[key] = SecondBrainFieldSuggestion(
        key=key,
        value=value,
        confidence=confidence,
        reason=reason,
        source=source,
    )


def _collect_document_context_text(document: Dict[str, Any], prediction: Dict[str, Any]) -> str:
    """Baut einen robusten Volltext für regelbasierte SecondBrain-Heuristiken."""

    parts = [
        str(document.get("title") or ""),
        str(document.get("content") or "")[:4000],
        str(prediction.get("document_type") or ""),
        str(prediction.get("correspondent") or ""),
        str(prediction.get("summary") or ""),
        str(prediction.get("rationale") or ""),
    ]
    return " \n ".join(part for part in parts if part).lower()


def infer_secondbrain_document_category(context_text: str) -> Optional[str]:
    """Leitet eine grobe SecondBrain-Dokumentklasse aus klaren Keywords ab."""

    keyword_map = (
        ("Steuer", ("steuer", "finanzamt", "elster", "steuerbescheid", "lohnsteuer", "umsatzsteuer")),
        ("Versicherung", ("versicherung", "police", "schaden", "leistungsabrechnung")),
        ("Recht", ("gericht", "anwalt", "klage", "einspruch", "anhörung", "bußgeld", "aktenzeichen")),
        ("Gehalt", ("lohnabrechnung", "gehaltsabrechnung", "entgeltabrechnung", "brutto", "netto")),
        ("Energie", ("strom", "gas", "wasser", "einspeisung", "pv", "wallbox", "zaehler", "zähler")),
        ("Fahrzeug", ("tesla", "fahrzeug", "kfz", "autohaus", "führerschein", "fuehrerschein")),
        ("Gesundheit", ("arzt", "zahnarzt", "apotheke", "kranken", "medizin", "rezept")),
        ("Immobilie", ("immobilie", "miete", "hausverwaltung", "grundsteuer", "nebenkosten")),
        ("Garantie", ("garantie", "gewährleistung", "gewaehrleistung", "widerruf")),
        ("Kommunikation", ("telekom", "vodafone", "mail", "e-mail", "anschreiben", "mitteilung")),
        ("Bank", ("bank", "kontoauszug", "iban", "kredit", "darlehen")),
        ("Vertrag", ("vertrag", "vertragsbeginn", "vertragsende", "kündigung", "kuendigung")),
        ("Bescheid", ("bescheid", "bewilligung", "ablehnung", "mahnung")),
        ("Rechnung", ("rechnung", "rechnungsnummer", "gesamtbetrag", "fällig", "faellig")),
    )
    for label, keywords in keyword_map:
        if any(keyword in context_text for keyword in keywords):
            return label
    return None


def infer_secondbrain_life_area(context_text: str, document_category: Optional[str]) -> Optional[str]:
    """Ordnet das Dokument vorsichtig einem Lebensbereich zu."""

    if document_category == "Steuer":
        return "Steuer"
    if document_category == "Recht":
        return "Recht"
    if document_category == "Versicherung":
        return "Versicherung"
    if document_category == "Energie":
        return "Energie"
    if document_category == "Fahrzeug":
        return "Auto"
    if document_category == "Gesundheit":
        return "Gesundheit"
    if document_category == "Gehalt":
        return "Arbeit"
    if document_category == "Bank":
        return "Finanzen"
    if "familie" in context_text or "kind" in context_text or "kita" in context_text:
        return "Familie"
    if "haus" in context_text or "wohnung" in context_text or "miete" in context_text:
        return "Haus"
    if "computer" in context_text or "technik" in context_text or "software" in context_text:
        return "Technik"
    return "Privat"


def infer_secondbrain_source_quality(document: Dict[str, Any]) -> str:
    """Schätzt die Dokumentherkunft für SecondBrain grob ein.

    Die Heuristik ist bewusst einfach und transparent:
    - Bildformate gelten als Foto
    - Mail-/EML-Hinweise gelten als E-Mail
    - Längerer OCR-Text spricht eher für brauchbare Original-PDFs oder gute Scans
    """

    title_lower = str(document.get("title") or "").strip().lower()
    content_length = len(str(document.get("content") or "").strip())

    if title_lower.endswith((".jpg", ".jpeg", ".png", ".heic")):
        return "Foto"
    if title_lower.endswith((".eml", ".msg")) or "mail" in title_lower:
        return "E-Mail"
    if content_length >= 1200:
        return "Original-PDF"
    if content_length >= 250:
        return "Scan gut"
    if content_length > 0:
        return "Scan schlecht"
    return "Import"


def infer_secondbrain_confidence_label(
    prediction: Dict[str, Any],
    secondbrain_suggestions: Dict[str, SecondBrainFieldSuggestion],
) -> str:
    """Verdichtet technische Confidence-Werte in ein Select-Label."""

    main_confidence = normalize_confidence(prediction.get("confidence"), 0.0)
    lowest_field_confidence = min(
        (suggestion.confidence for suggestion in secondbrain_suggestions.values()),
        default=main_confidence,
    )
    summary = str(prediction.get("summary") or "").lower()
    if "ocr" in summary and lowest_field_confidence < 0.70:
        return "OCR unsicher"
    if main_confidence >= 0.85 and lowest_field_confidence >= 0.80:
        return "KI sicher"
    if main_confidence > 0:
        return "KI unsicher"
    return "Ungeprüft"


def build_secondbrain_rule_based_suggestions(
    *,
    document: Dict[str, Any],
    prediction: Dict[str, Any],
    tax_enrichment: Optional[Any],
) -> Dict[str, SecondBrainFieldSuggestion]:
    """Erzeugt vorsichtige, nachvollziehbare `sb_`-Fallbacks.

    Wichtig:
    - Diese Regeln ersetzen keine KI-Auswertung, sondern stabilisieren Felder,
      die wir deterministisch aus vorhandenem Kontext ableiten können.
    - Sie greifen nur ergänzend, damit die bestehende Hauptklassifikation
      unverändert bleibt.
    """

    context_text = _collect_document_context_text(document, prediction)
    suggestions: Dict[str, SecondBrainFieldSuggestion] = {}
    category = infer_secondbrain_document_category(context_text)
    life_area = infer_secondbrain_life_area(context_text, category)
    source_quality = infer_secondbrain_source_quality(document)
    main_confidence = normalize_confidence(prediction.get("confidence"), 0.0)

    if category:
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_document_category",
            value=category,
            confidence=max(main_confidence, 0.78),
            reason="Aus Titel, OCR-Text und Standardklassifikation abgeleitet.",
            source="rules",
        )
    if life_area:
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_life_area",
            value=life_area,
            confidence=max(main_confidence, 0.72),
            reason="Lebensbereich aus Dokumentklasse und Kontext abgeleitet.",
            source="rules",
        )

    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_source_quality",
        value=source_quality,
        confidence=0.90,
        reason="Aus Dokumenttitel und OCR-Textlänge abgeleitet.",
        source="rules",
    )
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_document_date",
        value=normalize_iso_date(prediction.get("document_date") or document.get("created")),
        confidence=0.95,
        reason="Aus vorhandenem Dokumentdatum übernommen.",
        source="rules",
    )

    requires_action = False
    if any(keyword in context_text for keyword in ("frist", "fällig", "faellig", "mah", "kuendigung", "kündigung")):
        requires_action = True
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_requires_action",
        value=requires_action,
        confidence=0.80,
        reason="Auf Fristen-, Fälligkeits- oder Eskalationshinweise geprüft.",
        source="rules",
    )
    if requires_action:
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_action_status",
            value="Offen",
            confidence=0.78,
            reason="Dokument verlangt voraussichtlich eine Folgeaktion.",
            source="rules",
        )

    if category == "Rechnung":
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_financial_relevance",
            value="Ausgabe",
            confidence=0.76,
            reason="Rechnungen sind im Regelfall Ausgaben, sofern keine Erstattung erkennbar ist.",
            source="rules",
        )
    elif category == "Gehalt":
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_financial_relevance",
            value="Einnahme",
            confidence=0.88,
            reason="Gehaltsdokumente stehen im Regelfall für Einnahmen.",
            source="rules",
        )
    elif any(keyword in context_text for keyword in ("erstattung", "rückzahlung", "rueckzahlung")):
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_financial_relevance",
            value="Erstattung",
            confidence=0.80,
            reason="Erstattungs- oder Rückzahlungsbezug erkannt.",
            source="rules",
        )

    if category == "Recht" or any(keyword in context_text for keyword in ("anwalt", "gericht", "einspruch", "aktenzeichen")):
        legal_label = "Fristkritisch" if requires_action else "Hoch"
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_legal_relevance",
            value=legal_label,
            confidence=0.84,
            reason="Rechtliche Schlagworte und mögliche Fristen erkannt.",
            source="rules",
        )
    elif any(keyword in context_text for keyword in ("bescheid", "behörde", "behoerde", "finanzamt")):
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_legal_relevance",
            value="Mittel",
            confidence=0.74,
            reason="Behördlichen oder bescheidbezogenen Kontext erkannt.",
            source="rules",
        )

    if any(keyword in context_text for keyword in ("tesla model 3", "tesla")):
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_vehicle",
            value="Tesla Model 3",
            confidence=0.92,
            reason="Tesla-Bezug im Dokument erkannt.",
            source="rules",
        )
    elif category == "Fahrzeug":
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_vehicle",
            value="Anderes Fahrzeug",
            confidence=0.75,
            reason="Fahrzeugbezug erkannt, aber kein Tesla Model 3 eindeutig genannt.",
            source="rules",
        )

    if category == "Energie":
        energy_label = "Sonstige"
        for label, keywords in (
            ("Strom", ("strom", "kwh")),
            ("Gas", ("gas",)),
            ("Wasser", ("wasser",)),
            ("PV", ("pv", "photovoltaik", "powerocean")),
            ("Einspeisung", ("einspeisung",)),
            ("Wallbox", ("wallbox",)),
        ):
            if any(keyword in context_text for keyword in keywords):
                energy_label = label
                break
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_energy_type",
            value=energy_label,
            confidence=0.82,
            reason="Energieart aus erkannten Fachbegriffen abgeleitet.",
            source="rules",
        )

    if category == "Gesundheit":
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_sensitive",
            value=True,
            confidence=0.96,
            reason="Gesundheitsbezug erkannt; Dokument wird vorsorglich als sensibel markiert.",
            source="rules",
        )

    ignore_document = any(keyword in context_text for keyword in ("spam", "newsletter", "testdokument"))
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_ignore_by_secondbrain",
        value=ignore_document,
        confidence=0.95,
        reason="Nur offensichtliche Test-/Spam-Muster werden automatisch ignoriert.",
        source="rules",
    )
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_export_to_secondbrain",
        value=not ignore_document,
        confidence=0.95,
        reason="Klassifizierte Dokumente werden standardmäßig an SecondBrain weitergereicht.",
        source="rules",
    )

    return suggestions


def apply_tax_enrichment_to_secondbrain_suggestions(
    suggestions: Dict[str, SecondBrainFieldSuggestion],
    tax_enrichment: Any,
) -> None:
    """Überträgt vorhandene Tax-Enrichment-Daten in passende `sb_`-Felder."""

    if tax_enrichment is None:
        return

    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_tax_year",
        value=getattr(tax_enrichment, "tax_year", None),
        confidence=0.95,
        reason="Aus Tax Enrichment übernommen.",
        source="tax",
    )
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_document_date",
        value=getattr(tax_enrichment, "document_date", None),
        confidence=0.95,
        reason="Aus Tax Enrichment übernommen.",
        source="tax",
    )
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_period_start",
        value=getattr(tax_enrichment, "service_period_from", None),
        confidence=0.92,
        reason="Aus Tax Enrichment übernommen.",
        source="tax",
    )
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_period_end",
        value=getattr(tax_enrichment, "service_period_to", None),
        confidence=0.92,
        reason="Aus Tax Enrichment übernommen.",
        source="tax",
    )
    set_secondbrain_suggestion_if_missing(
        suggestions,
        key="sb_provider_name",
        value=getattr(tax_enrichment, "issuer", None),
        confidence=0.90,
        reason="Leistungserbringer aus Tax Enrichment übernommen.",
        source="tax",
    )
    tax_amount = getattr(tax_enrichment, "total_amount", None)
    if tax_amount is not None:
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_amount_total",
            value=f"{float(tax_amount):.2f}",
            confidence=0.90,
            reason="Gesamtbetrag aus Tax Enrichment übernommen.",
            source="tax",
        )

    tax_category = str(getattr(tax_enrichment, "tax_category", "") or "").strip().lower()
    if tax_category and tax_category not in {"nicht_steuerrelevant", "unklar"}:
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_document_category",
            value="Steuer" if tax_category in {"sonderausgaben", "werbungskosten", "kinderbetreuungskosten"} else None,
            confidence=0.74,
            reason="Steuerlich relevantes Dokument wurde im Tax Enrichment erkannt.",
            source="tax",
        )
    flags = set(getattr(tax_enrichment, "flags", []) or [])
    if flags.intersection({"needs_review", "needs_payment_proof", "possible_finanzamt_query"}):
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_requires_action",
            value=True,
            confidence=0.82,
            reason="Tax Enrichment verlangt Nachprüfung oder Folgeaktion.",
            source="tax",
        )
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key="sb_action_status",
            value="In Prüfung",
            confidence=0.78,
            reason="Tax Enrichment markiert das Dokument als prüfbedürftig.",
            source="tax",
        )


def build_secondbrain_suggestions(
    *,
    document: Dict[str, Any],
    prediction: Dict[str, Any],
    tax_enrichment: Optional[Any],
) -> Dict[str, SecondBrainFieldSuggestion]:
    """Kombiniert KI-, Tax- und Regelquellen zu einer finalen Vorschlagsmenge."""

    suggestions = normalize_secondbrain_prediction_fields(
        prediction,
        SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS,
    )

    rule_based = build_secondbrain_rule_based_suggestions(
        document=document,
        prediction=prediction,
        tax_enrichment=tax_enrichment,
    )
    for key, suggestion in rule_based.items():
        set_secondbrain_suggestion_if_missing(
            suggestions,
            key=key,
            value=suggestion.value,
            confidence=suggestion.confidence,
            reason=suggestion.reason,
            source=suggestion.source,
        )

    apply_tax_enrichment_to_secondbrain_suggestions(suggestions, tax_enrichment)

    # Steuerungsfelder setzen wir bewusst zuletzt, weil sie keine freien
    # Dokumentinterpretationen sind, sondern Lauf-/Qualitätsmetadaten.
    suggestions["sb_confidence"] = SecondBrainFieldSuggestion(
        key="sb_confidence",
        value=infer_secondbrain_confidence_label(prediction, suggestions),
        confidence=0.95,
        reason="Aus Hauptklassifikation und Feldsicherheit verdichtet.",
        source="rules",
    )

    return suggestions


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
    custom_field_definitions: Optional[Dict[str, CustomFieldDefinition]] = None,
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
    if custom_field_definitions:
        sanitized["custom_fields"] = normalize_prediction_custom_fields(
            sanitized,
            custom_field_definitions,
        )
    return sanitized


def extract_usage(prediction: Dict[str, Any]) -> tuple[int, int, int]:
    """Extrahiert API-Token-Usage aus internen Metadaten."""

    usage = prediction.get("_meta_usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0)
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def load_metrics(metrics_path: Path) -> Dict[str, Any]:
    """Lädt bestehende Lauf-Metriken oder liefert Defaults."""

    default = {
        "last_run": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_eur": 0.0,
            "bypass_skipped": 0,
            "finished_at": None,
            "model": None,
        },
        "totals": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_eur": 0.0,
            "bypass_skipped": 0,
            "runs": 0,
        },
    }
    if not metrics_path.exists():
        return default
    try:
        loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Metrics-Datei konnte nicht gelesen werden, nutze Defaults: %s", metrics_path)
    return default


def load_failed_documents(failed_docs_path: Path) -> Dict[str, float]:
    """Lädt fehlgeschlagene Dokument-IDs mit nächstem Retry-Zeitpunkt."""

    if not failed_docs_path.exists():
        return {}
    try:
        loaded = json.loads(failed_docs_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return {}
        result: Dict[str, float] = {}
        for key, value in loaded.items():
            try:
                result[str(int(key))] = float(value)
            except (TypeError, ValueError):
                continue
        return result
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed-Docs-Datei konnte nicht gelesen werden: %s", failed_docs_path)
        return {}


def save_failed_documents(failed_docs_path: Path, payload: Dict[str, float]) -> None:
    """Speichert fehlgeschlagene Dokument-IDs mit nächstem Retry-Zeitpunkt."""

    try:
        failed_docs_path.parent.mkdir(parents=True, exist_ok=True)
        failed_docs_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.error("Failed-Docs-Datei konnte nicht geschrieben werden: %s | %s", failed_docs_path, exc)


def load_failed_patch_cache(cache_path: Path) -> Dict[str, Dict[str, Any]]:
    """Lädt zwischengespeicherte Patch-Payloads für Retry-Läufe ohne KI."""

    if not cache_path.exists():
        return {}
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for key, value in loaded.items():
            try:
                doc_key = str(int(key))
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                result[doc_key] = value
        return result
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed-Patch-Cache konnte nicht gelesen werden: %s", cache_path)
        return {}


def save_failed_patch_cache(cache_path: Path, payload: Dict[str, Dict[str, Any]]) -> None:
    """Speichert zwischengespeicherte Patch-Payloads für Retry-Läufe ohne KI."""

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.error("Failed-Patch-Cache konnte nicht geschrieben werden: %s | %s", cache_path, exc)


def load_tag_bypass_documents(bypass_path: Path) -> Dict[str, Dict[str, Any]]:
    """Lädt Dokumente, die wegen tags-only 500 im Bypass laufen."""

    if not bypass_path.exists():
        return {}
    try:
        loaded = json.loads(bypass_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for key, value in loaded.items():
            try:
                doc_key = str(int(key))
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                result[doc_key] = value
        return result
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Tag-Bypass-Datei konnte nicht gelesen werden: %s", bypass_path)
        return {}


def save_tag_bypass_documents(bypass_path: Path, payload: Dict[str, Dict[str, Any]]) -> None:
    """Speichert Dokumente, die wegen tags-only 500 im Bypass laufen."""

    try:
        bypass_path.parent.mkdir(parents=True, exist_ok=True)
        bypass_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.error("Tag-Bypass-Datei konnte nicht geschrieben werden: %s | %s", bypass_path, exc)


def save_metrics(metrics_path: Path, payload: Dict[str, Any]) -> None:
    """Speichert Lauf-Metriken als JSON für externe Systeme (z. B. Home Assistant)."""

    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        LOGGER.error("Metrics-Datei konnte nicht geschrieben werden: %s | %s", metrics_path, exc)


def resolve_runtime_path(path_value: str, base_dir: Optional[Path] = None) -> Path:
    """Löst Pfade für Laufzeitdateien robust relativ zum Arbeitsverzeichnis auf."""

    path = Path(str(path_value).strip() or RUN_STATE_FILE_DEFAULT)
    if path.is_absolute():
        return path
    resolved_base = base_dir or Path.cwd()
    return resolved_base / path


def load_json_file(path: Path) -> Dict[str, Any]:
    """Lädt kleine JSON-Hilfsdateien tolerant und liefert immer ein Dict zurück."""

    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("JSON-Datei konnte nicht gelesen werden: %s", path)
        return {}


def save_json_file(path: Path, payload: Dict[str, Any]) -> None:
    """Schreibt kleine JSON-Hilfsdateien robust auf Disk."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        LOGGER.error("JSON-Datei konnte nicht geschrieben werden: %s | %s", path, exc)


def delete_runtime_file(path: Path) -> None:
    """Entfernt Laufzeitdateien defensiv, falls sie existieren."""

    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        LOGGER.warning("Laufzeitdatei konnte nicht entfernt werden: %s | %s", path, exc)


def load_run_state(path: Path) -> Dict[str, Any]:
    """Lädt den gespeicherten Resume-Zustand eines pausierten Laufs."""

    payload = load_json_file(path)
    if not payload:
        return {}
    version = int(payload.get("version", 0) or 0)
    if version != RUN_STATE_VERSION:
        LOGGER.warning(
            "Run-State-Datei %s hat Version %s statt %s und wird ignoriert.",
            path,
            version,
            RUN_STATE_VERSION,
        )
        return {}
    return payload


def save_run_state(path: Path, payload: Dict[str, Any]) -> None:
    """Speichert den Resume-Zustand für einen kontrolliert pausierten Lauf."""

    payload = dict(payload)
    payload["version"] = RUN_STATE_VERSION
    payload["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json_file(path, payload)


def request_manual_stop(path: Path) -> None:
    """Schreibt eine kleine Stop-Anfrage-Datei für den laufenden Prozess."""

    save_json_file(
        path,
        {
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reason": "manual_stop",
        },
    )


def is_stop_requested(path: Path) -> bool:
    """Prüft, ob ein manueller Stop für den aktuellen Lauf angefordert wurde."""

    return path.exists()


def extract_retry_after_seconds_from_error(
    message: str,
    headers: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Extrahiert Retry-After-Zeiten aus Headern oder OpenAI-Fehlermeldungen.

    Beispiel:
    - Input: `Please try again in 2.533s`
    - Output: `2.533`
    """

    if headers:
        for key in ("retry-after", "Retry-After"):
            raw = headers.get(key)
            if raw in (None, ""):
                continue
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                continue

    match = RETRY_AFTER_SECONDS_PATTERN.search(str(message or ""))
    if not match:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except (TypeError, ValueError):
        return None


def emit_runtime_event(kind: str, **payload: Any) -> None:
    """Schreibt maschinenlesbare Laufzeitereignisse ins normale Skript-Log.

    Home Assistant liest diese Marker live mit, ohne dass wir ein zweites IPC-
    Protokoll pflegen müssen.
    """

    event = {"kind": kind, **payload}
    LOGGER.info("%s%s", RUNTIME_EVENT_MARKER, json.dumps(event, ensure_ascii=False, sort_keys=True))


def extract_document_custom_field_values(document: Dict[str, Any]) -> Dict[str, Any]:
    """Liest bereits gesetzte Custom Fields tolerant aus einem Dokumentpayload.

    Paperless-ngx hat über Versionen und Endpunkte hinweg leicht unterschiedliche
    Strukturen für Custom Fields. Diese Funktion akzeptiert bewusst mehrere
    Varianten, damit Diff-Checks und Dry-Runs nicht von einem genauen
    Payload-Format abhängen.
    """

    raw_custom_fields = document.get("custom_fields")
    extracted: Dict[str, Any] = {}

    if isinstance(raw_custom_fields, dict):
        for raw_key, raw_value in raw_custom_fields.items():
            normalized_key = str(raw_key).strip()
            if not normalized_key:
                continue
            if isinstance(raw_value, dict) and "value" in raw_value:
                extracted[normalized_key] = raw_value.get("value")
            else:
                extracted[normalized_key] = raw_value
        return extracted

    if not isinstance(raw_custom_fields, list):
        return extracted

    for item in raw_custom_fields:
        if not isinstance(item, dict):
            continue
        field_name = str(
            item.get("name")
            or item.get("field_name")
            or item.get("custom_field")
            or item.get("field")
            or item.get("id")
            or ""
        ).strip()
        if not field_name:
            continue
        if "value" in item:
            extracted[field_name] = item.get("value")
            continue
        for value_key in (
            "value_text",
            "value_date",
            "value_bool",
            "value_int",
            "value_float",
            "value_monetary",
            "value_url",
            "value_document_ids",
        ):
            if value_key in item and item.get(value_key) not in (None, "", []):
                extracted[field_name] = item.get(value_key)
                break
    return extracted


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

    if endpoint == "/api/tags/":
        is_valid_tag, reason = validate_new_tag_name(name.strip())
        if not is_valid_tag:
            LOGGER.warning(
                "Tag-Erstellung blockiert: '%s' (%s).",
                name.strip(),
                reason,
            )
            return None

    if not create_missing:
        LOGGER.info("Entity nicht vorhanden und Auto-Create deaktiviert: %s (%s)", name, endpoint)
        return None

    created_id = client.create_entity(endpoint, name.strip())
    mapping[key] = created_id
    LOGGER.info("Neue Entity angelegt: %s -> ID %s (%s)", name, created_id, endpoint)
    if created_entities is not None:
        created_entities.setdefault(endpoint, []).append(name.strip())
    return created_id


def ensure_custom_field_id(
    client: PaperlessClient,
    custom_fields_map: Dict[str, Dict[str, Any]],
    definition: CustomFieldDefinition,
    create_missing_custom_fields: bool,
    created_entities: Optional[Dict[str, List[str]]] = None,
    custom_field_id_to_definition: Optional[Dict[int, CustomFieldDefinition]] = None,
) -> Optional[int]:
    """Löst ein Paperless-Custom-Field auf oder legt es bei Bedarf an."""

    key = definition.paperless_name.strip().lower()
    existing = custom_fields_map.get(key)
    if existing is not None:
        try:
            existing_id = int(existing.get("id"))
        except (TypeError, ValueError):
            existing_id = None
        existing_type = str(existing.get("data_type") or "").strip().lower()
        if existing_type and existing_type != definition.data_type:
            LOGGER.warning(
                "Custom Field '%s' existiert bereits mit Datentyp '%s' statt '%s'. "
                "Wert wird nicht automatisch gesetzt.",
                definition.paperless_name,
                existing_type,
                definition.data_type,
            )
            return None
        if existing_id is not None and custom_field_id_to_definition is not None:
            custom_field_id_to_definition[existing_id] = definition
        return existing_id

    if not create_missing_custom_fields:
        LOGGER.info(
            "Custom Field nicht vorhanden und Auto-Create deaktiviert: %s",
            definition.paperless_name,
        )
        return None

    created = client.create_custom_field(definition)
    custom_fields_map[key] = created
    created_id = int(created["id"])
    if custom_field_id_to_definition is not None:
        custom_field_id_to_definition[created_id] = definition
    LOGGER.info(
        "Neues Custom Field angelegt: %s -> ID %s (%s)",
        definition.paperless_name,
        created_id,
        definition.data_type,
    )
    if created_entities is not None:
        created_entities.setdefault("/api/custom_fields/", []).append(definition.paperless_name)
    return created_id


def get_existing_custom_field_value(
    document: Dict[str, Any],
    field: Dict[str, Any],
    definition: Optional[CustomFieldDefinition] = None,
) -> Any:
    """Liest einen bestehenden Custom-Field-Wert möglichst tolerant aus.

    Warum diese Hilfsfunktion wichtig ist:
    - Paperless liefert Custom Fields je nach Endpoint/Version leicht anders.
    - Für den Überschreibschutz wollen wir nicht an einem einzelnen Payload-
      Format hängen.
    """

    current_custom_fields = extract_document_custom_field_values(document)
    candidate_keys = [
        str(field.get("id") or ""),
        str(field.get("name") or ""),
        str(field.get("name") or "").lower(),
    ]
    if definition is not None:
        candidate_keys.extend(
            [
                definition.paperless_name,
                definition.paperless_name.lower(),
                definition.key,
            ]
        )
    for candidate_key in candidate_keys:
        if candidate_key and candidate_key in current_custom_fields:
            return current_custom_fields[candidate_key]
    return None


def build_secondbrain_custom_fields_payload(
    *,
    document: Dict[str, Any],
    prediction: Dict[str, Any],
    tax_enrichment: Optional[Any],
    custom_fields_map: Dict[str, Dict[str, Any]],
    overwrite_existing: bool,
    attach_empty_when_unknown: bool,
    confidence_threshold: float,
    log_missing_fields: bool,
    custom_field_id_to_definition: Optional[Dict[int, CustomFieldDefinition]] = None,
    sync_report: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[int, Any], List[int], List[int]]:
    """Baut den Patch-Teil für `sb_`-Felder inklusive Schutzlogik.

    Rückgabe:
    - dict mit zu schreibenden Feldwerten
    - Liste von Feld-IDs, die leer angehängt werden sollen
    - Liste von Feld-IDs, die entfernt/geleert werden sollen
    """

    suggestions = build_secondbrain_suggestions(
        document=document,
        prediction=prediction,
        tax_enrichment=tax_enrichment,
    )
    if sync_report is not None:
        sync_report["enabled"] = True
        sync_report["prepared"] = {
            key: {
                "value": suggestion.value,
                "confidence": round(float(suggestion.confidence), 3),
                "source": suggestion.source,
                "reason": suggestion.reason,
            }
            for key, suggestion in suggestions.items()
        }

    values: Dict[int, Any] = {}
    empty_field_ids: List[int] = []
    remove_field_ids: List[int] = []

    for key, suggestion in suggestions.items():
        definition = SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS.get(key)
        if definition is None:
            continue

        if suggestion.confidence < confidence_threshold:
            if sync_report is not None:
                sync_report["below_threshold"][key] = {
                    "confidence": round(float(suggestion.confidence), 3),
                    "value": suggestion.value,
                }
            continue

        field = custom_fields_map.get(definition.paperless_name.lower())
        if field is None:
            if log_missing_fields:
                LOGGER.info(
                    "SecondBrain-Custom-Field fehlt in Paperless und wird übersprungen: %s",
                    definition.paperless_name,
                )
            if sync_report is not None:
                sync_report["missing_fields"].append(definition.paperless_name)
            continue

        try:
            field_id = int(field.get("id"))
        except (TypeError, ValueError):
            continue
        if custom_field_id_to_definition is not None:
            custom_field_id_to_definition[field_id] = definition

        current_value = get_existing_custom_field_value(document, field, definition)
        current_has_value = has_meaningful_custom_field_value(current_value)
        if current_has_value and not overwrite_existing:
            if sync_report is not None:
                sync_report["preserved_existing"][key] = current_value
            continue

        if suggestion.value is None:
            if not attach_empty_when_unknown:
                continue
            if current_has_value and overwrite_existing:
                remove_field_ids.append(field_id)
                if sync_report is not None:
                    sync_report["cleared"].append(key)
                continue
            if not current_has_value:
                empty_field_ids.append(field_id)
                if sync_report is not None:
                    sync_report["written"][key] = {
                        "value": None,
                        "mode": "empty",
                        "confidence": round(float(suggestion.confidence), 3),
                    }
            continue

        resolved_value, error_reason = resolve_custom_field_value(field, suggestion.value)
        if error_reason:
            if sync_report is not None:
                target_bucket = (
                    "unresolved_selects"
                    if str(field.get("data_type") or "").strip().lower() == "select"
                    else "invalid_values"
                )
                sync_report[target_bucket][key] = {
                    "value": suggestion.value,
                    "reason": error_reason,
                }
            continue
        values[field_id] = resolved_value
        if sync_report is not None:
            sync_report["written"][key] = {
                "value": resolved_value,
                "mode": "set",
                "confidence": round(float(suggestion.confidence), 3),
            }

    return values, sorted(set(empty_field_ids)), sorted(set(remove_field_ids))


def build_patch_payload(
    client: PaperlessClient,
    document: Dict[str, Any],
    prediction: Dict[str, Any],
    tags_map: Dict[str, int],
    doc_types_map: Dict[str, int],
    correspondents_map: Dict[str, int],
    storage_paths_map: Dict[str, int],
    custom_fields_map: Optional[Dict[str, Dict[str, Any]]],
    custom_field_definitions: Optional[Dict[str, CustomFieldDefinition]],
    create_missing_entities: bool,
    create_missing_custom_fields: bool,
    include_standard_metadata: bool,
    enable_secondbrain_custom_fields: bool,
    secondbrain_overwrite_existing: bool,
    secondbrain_attach_empty_when_unknown: bool,
    secondbrain_confidence_threshold: float,
    secondbrain_log_missing_fields: bool,
    tax_enrichment: Optional[Any] = None,
    created_entities: Optional[Dict[str, List[str]]] = None,
    custom_field_id_to_definition: Optional[Dict[int, CustomFieldDefinition]] = None,
    secondbrain_sync_report: Optional[Dict[str, Any]] = None,
    secondbrain_ready_tag_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Konvertiert KI-Output in ein valides PATCH-Payload für Paperless."""

    payload: Dict[str, Any] = {}

    if include_standard_metadata:
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

    if enable_secondbrain_custom_fields and custom_fields_map is not None:
        sb_values, sb_empty_ids, sb_remove_ids = build_secondbrain_custom_fields_payload(
            document=document,
            prediction=prediction,
            tax_enrichment=tax_enrichment,
            custom_fields_map=custom_fields_map,
            overwrite_existing=secondbrain_overwrite_existing,
            attach_empty_when_unknown=secondbrain_attach_empty_when_unknown,
            confidence_threshold=secondbrain_confidence_threshold,
            log_missing_fields=secondbrain_log_missing_fields,
            custom_field_id_to_definition=custom_field_id_to_definition,
            sync_report=secondbrain_sync_report,
        )
        if sb_values:
            payload["custom_fields"] = dict(sb_values)
        if sb_empty_ids:
            payload["custom_fields_empty"] = list(sb_empty_ids)
        if sb_remove_ids:
            payload["custom_fields_remove"] = list(sb_remove_ids)

    if custom_fields_map is not None and custom_field_definitions:
        normalized_custom_fields = normalize_prediction_custom_fields(
            prediction,
            custom_field_definitions,
        )
        custom_fields_payload: Dict[int, Any] = {}
        for field_key, field_value in normalized_custom_fields.items():
            definition = custom_field_definitions.get(field_key)
            if definition is None:
                continue
            custom_field_id = ensure_custom_field_id(
                client,
                custom_fields_map,
                definition,
                create_missing_custom_fields,
                created_entities,
                custom_field_id_to_definition,
            )
            if custom_field_id is None:
                continue
            custom_fields_payload[int(custom_field_id)] = field_value
        if custom_fields_payload:
            payload.setdefault("custom_fields", {}).update(custom_fields_payload)

    if secondbrain_ready_tag_id is not None and should_mark_secondbrain_ready(secondbrain_sync_report):
        current_tag_ids = {int(tag_id) for tag_id in document.get("tags", [])}
        next_tag_ids = set(current_tag_ids)
        next_tag_ids.add(int(secondbrain_ready_tag_id))
        if next_tag_ids != current_tag_ids:
            payload["tags"] = sorted(next_tag_ids)

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


def filter_unchanged_patch_fields(
    *,
    document: Dict[str, Any],
    patch_payload: Dict[str, Any],
    custom_field_id_to_definition: Optional[Dict[int, CustomFieldDefinition]] = None,
) -> Dict[str, Any]:
    """Entfernt unveränderte Felder aus dem Patch, um unnötige PATCHes zu vermeiden."""

    filtered = dict(patch_payload)

    if "document_type" in filtered:
        if (document.get("document_type") or None) == (filtered.get("document_type") or None):
            filtered.pop("document_type", None)

    if "correspondent" in filtered:
        if (document.get("correspondent") or None) == (filtered.get("correspondent") or None):
            filtered.pop("correspondent", None)

    if "storage_path" in filtered:
        if (document.get("storage_path") or None) == (filtered.get("storage_path") or None):
            filtered.pop("storage_path", None)

    if "created" in filtered:
        current_created = normalize_iso_date(document.get("created"))
        next_created = normalize_iso_date(filtered.get("created"))
        if current_created == next_created:
            filtered.pop("created", None)

    if "tags" in filtered:
        current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
        next_tags = {int(tag_id) for tag_id in filtered.get("tags", [])}
        if current_tags == next_tags:
            filtered.pop("tags", None)

    if "custom_fields" in filtered:
        current_custom_fields = extract_document_custom_field_values(document)
        next_custom_fields = dict(filtered.get("custom_fields") or {})
        unchanged_custom_field_ids: List[int] = []
        for custom_field_id, next_value in next_custom_fields.items():
            try:
                custom_field_id_int = int(custom_field_id)
            except (TypeError, ValueError):
                continue
            definition = (custom_field_id_to_definition or {}).get(custom_field_id_int)
            candidate_keys = [str(custom_field_id_int)]
            if definition is not None:
                candidate_keys.extend([definition.paperless_name, definition.paperless_name.lower()])
            current_value = None
            for candidate_key in candidate_keys:
                if candidate_key in current_custom_fields:
                    current_value = current_custom_fields[candidate_key]
                    break
            if definition is not None:
                current_value = normalize_custom_field_value(definition, current_value)
                next_value = normalize_custom_field_value(definition, next_value)
            if current_value == next_value:
                unchanged_custom_field_ids.append(custom_field_id_int)
        for custom_field_id in unchanged_custom_field_ids:
            filtered["custom_fields"].pop(custom_field_id, None)
        if not filtered["custom_fields"]:
            filtered.pop("custom_fields", None)

    if "custom_fields_empty" in filtered:
        current_custom_fields = extract_document_custom_field_values(document)
        remaining_empty_ids: List[int] = []
        for custom_field_id in filtered.get("custom_fields_empty", []) or []:
            definition = (custom_field_id_to_definition or {}).get(int(custom_field_id))
            candidate_keys = [str(custom_field_id)]
            if definition is not None:
                candidate_keys.extend([definition.paperless_name, definition.paperless_name.lower()])
            current_value = None
            key_found = False
            for candidate_key in candidate_keys:
                if candidate_key in current_custom_fields:
                    key_found = True
                    current_value = current_custom_fields[candidate_key]
                    break
            if key_found and not has_meaningful_custom_field_value(current_value):
                continue
            remaining_empty_ids.append(int(custom_field_id))
        if remaining_empty_ids:
            filtered["custom_fields_empty"] = remaining_empty_ids
        else:
            filtered.pop("custom_fields_empty", None)

    if "custom_fields_remove" in filtered:
        current_custom_fields = extract_document_custom_field_values(document)
        remaining_remove_ids: List[int] = []
        for custom_field_id in filtered.get("custom_fields_remove", []) or []:
            definition = (custom_field_id_to_definition or {}).get(int(custom_field_id))
            candidate_keys = [str(custom_field_id)]
            if definition is not None:
                candidate_keys.extend([definition.paperless_name, definition.paperless_name.lower()])
            current_value = None
            for candidate_key in candidate_keys:
                if candidate_key in current_custom_fields:
                    current_value = current_custom_fields[candidate_key]
                    break
            if has_meaningful_custom_field_value(current_value):
                remaining_remove_ids.append(int(custom_field_id))
        if remaining_remove_ids:
            filtered["custom_fields_remove"] = remaining_remove_ids
        else:
            filtered.pop("custom_fields_remove", None)

    return filtered


def build_ai_note_entry(
    *,
    prediction: Dict[str, Any],
    patch_payload: Dict[str, Any],
    doc_type_id_to_label: Dict[int, str],
    correspondent_id_to_label: Dict[int, str],
    storage_path_id_to_label: Dict[int, str],
    tag_id_to_label: Dict[int, str],
    custom_field_id_to_definition: Optional[Dict[int, CustomFieldDefinition]],
    secondbrain_sync_report: Optional[Dict[str, Any]],
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
    if isinstance(patch_payload.get("custom_fields"), dict):
        for custom_field_id, custom_value in sorted(
            patch_payload["custom_fields"].items(),
            key=lambda item: (
                (custom_field_id_to_definition or {}).get(int(item[0])).note_label
                if (custom_field_id_to_definition or {}).get(int(item[0]))
                else f"id:{item[0]}"
            ),
        ):
            try:
                definition = (custom_field_id_to_definition or {}).get(int(custom_field_id))
            except (TypeError, ValueError):
                definition = None
            label = definition.note_label if definition is not None else f"custom_field_{custom_field_id}"
            lines.append(f"- {label}: {custom_value}")
    secondbrain_lines: List[str] = []
    written_fields = {}
    if isinstance(secondbrain_sync_report, dict):
        written_fields = secondbrain_sync_report.get("written") or {}
    if isinstance(written_fields, dict):
        for key in SECOND_BRAIN_NOTE_KEYS:
            entry = written_fields.get(key)
            if not isinstance(entry, dict):
                continue
            secondbrain_lines.append(f"- {key}: {entry.get('value')}")

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
        + (
            "\nSecondBrain-Felder:\n" + "\n".join(secondbrain_lines)
            if secondbrain_lines
            else ""
        )
    )
    return note


def log_secondbrain_sync_report(
    *,
    doc_id: Optional[int],
    title: str,
    sync_report: Dict[str, Any],
) -> None:
    """Schreibt nachvollziehbare Debug-Infos zum `sb_`-Sync ins Laufprotokoll."""

    if not sync_report.get("enabled"):
        LOGGER.info(
            "SecondBrain-Custom-Field-Sync inaktiv fuer Dokument %s (%s).",
            doc_id,
            title,
        )
        return

    LOGGER.info(
        "SecondBrain-Custom-Field-Sync aktiv fuer Dokument %s (%s). Geschrieben=%s | "
        "BelowThreshold=%s | BestehendeWerteBehalten=%s | FehlendeFelder=%s | "
        "SelectProbleme=%s | UngueltigeWerte=%s | ApiFehler=%s",
        doc_id,
        title,
        ", ".join(sorted((sync_report.get("written") or {}).keys())) or "keine",
        ", ".join(sorted((sync_report.get("below_threshold") or {}).keys())) or "keine",
        ", ".join(sorted((sync_report.get("preserved_existing") or {}).keys())) or "keine",
        ", ".join(sorted(set(sync_report.get("missing_fields") or []))) or "keine",
        ", ".join(sorted((sync_report.get("unresolved_selects") or {}).keys())) or "keine",
        ", ".join(sorted((sync_report.get("invalid_values") or {}).keys())) or "keine",
        ", ".join(sync_report.get("api_errors") or []) or "keine",
    )


def build_error_note_entry(
    *,
    error_message: str,
    patch_payload: Optional[Dict[str, Any]],
) -> str:
    """Erstellt einen kompakten Fehler-Notizeintrag für fehlgeschlagene Dokumente."""

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    compact_error = " ".join(str(error_message).split())
    if len(compact_error) > 800:
        compact_error = compact_error[:797] + "..."

    payload_text = "-"
    if patch_payload:
        payload_text = str(patch_payload)
        if len(payload_text) > 900:
            payload_text = payload_text[:897] + "..."

    return (
        f"[KI-Fehler {timestamp}]\n"
        "Bei der automatischen Verarbeitung ist ein Fehler aufgetreten.\n"
        f"Fehler: {compact_error}\n"
        f"Geplantes PatchPayload: {payload_text}"
    )


def build_skip_note_entry(
    *,
    prediction: Dict[str, Any],
    confidence_threshold: float,
) -> str:
    """Erstellt eine Notiz für wegen niedriger Confidence übersprungene Dokumente."""

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    confidence = float(prediction.get("confidence", 0.0) or 0.0)
    doc_type = str(prediction.get("document_type") or "-")
    correspondent = str(prediction.get("correspondent") or "-")
    storage_path = str(prediction.get("storage_path") or "-")
    tags = prediction.get("tags") or []
    tags_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) and tags else "-"
    rationale = " ".join(str(prediction.get("rationale") or "Keine Begründung geliefert.").split())
    if len(rationale) > 600:
        rationale = rationale[:597] + "..."

    return (
        f"[KI-Skip {timestamp}]\n"
        f"Automatisch übersprungen: Confidence {confidence:.2f} unter Schwellwert {confidence_threshold:.2f}.\n"
        "KI-Vorschlag (nicht angewendet):\n"
        f"- document_type: {doc_type}\n"
        f"- correspondent: {correspondent}\n"
        f"- storage_path: {storage_path}\n"
        f"- tags: {tags_text}\n"
        f"Begründung der KI: {rationale}"
    )


def build_precheck_skip_note_entry(
    *,
    reason: str,
    details: str,
) -> str:
    """Notiztext für Precheck-Skips vor der eigentlichen KI-Klassifizierung."""

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    compact_details = " ".join(str(details).split())
    if len(compact_details) > 900:
        compact_details = compact_details[:897] + "..."
    return (
        f"[KI-Precheck-Skip {timestamp}]\n"
        f"Dokument wurde vor der KI-Klassifizierung übersprungen.\n"
        f"Grund: {reason}\n"
        f"Details: {compact_details}"
    )


def collect_document_text(document: Dict[str, Any]) -> str:
    """Liest den OCR-/Inhaltstext robust aus dem Dokumentobjekt."""

    return str(document.get("content") or document.get("content_preview") or "").strip()


def calc_alnum_ratio(text: str) -> float:
    """Berechnet den Anteil alphanumerischer Zeichen an allen Nicht-Whitespace-Zeichen."""

    if not text:
        return 0.0
    compact = [char for char in text if not char.isspace()]
    if not compact:
        return 0.0
    alnum = sum(1 for char in compact if char.isalnum())
    return alnum / float(len(compact))


def collect_document_names(document: Dict[str, Any]) -> List[str]:
    """Sammelt alle relevanten Dateinamen-/Titelquellen für Pattern-Checks."""

    result: List[str] = []
    for key in ("original_file_name", "original_filename", "filename", "title", "archive_filename"):
        value = document.get(key)
        if value:
            result.append(str(value))
    return result


def log_dry_run_change(
    document: Dict[str, Any],
    prediction: Dict[str, Any],
    patch_payload: Dict[str, Any],
    note_will_be_added: bool,
    tag_id_to_label: Dict[int, str],
    doc_type_id_to_label: Dict[int, str],
    correspondent_id_to_label: Dict[int, str],
    storage_path_id_to_label: Dict[int, str],
    custom_field_id_to_definition: Optional[Dict[int, CustomFieldDefinition]] = None,
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

    current_custom_fields = extract_document_custom_field_values(document)

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
    if isinstance(patch_payload.get("custom_fields"), dict):
        for custom_field_id, new_value in sorted(patch_payload["custom_fields"].items()):
            try:
                definition = (custom_field_id_to_definition or {}).get(int(custom_field_id))
            except (TypeError, ValueError):
                definition = None
            field_label = definition.note_label if definition is not None else f"CustomField {custom_field_id}"
            old_value = "keiner"
            if definition is not None:
                for candidate_key in (
                    definition.paperless_name,
                    definition.paperless_name.lower(),
                    str(custom_field_id),
                ):
                    if candidate_key in current_custom_fields:
                        old_value = str(current_custom_fields[candidate_key])
                        break
            rows.append((field_label, old_value, str(new_value)))
    if isinstance(patch_payload.get("custom_fields_empty"), list):
        for custom_field_id in patch_payload.get("custom_fields_empty", []):
            try:
                definition = (custom_field_id_to_definition or {}).get(int(custom_field_id))
            except (TypeError, ValueError):
                definition = None
            field_label = definition.note_label if definition is not None else f"CustomField {custom_field_id}"
            rows.append((field_label, "nicht angehängt", "leer anhängen"))
    if isinstance(patch_payload.get("custom_fields_remove"), list):
        for custom_field_id in patch_payload.get("custom_fields_remove", []):
            try:
                definition = (custom_field_id_to_definition or {}).get(int(custom_field_id))
            except (TypeError, ValueError):
                definition = None
            field_label = definition.note_label if definition is not None else f"CustomField {custom_field_id}"
            rows.append((field_label, "gesetzt", "Wert entfernen"))
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
        "/api/custom_fields/": "Benutzerdefiniertes Feld neu erstellt",
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


def process_documents(
    config: AppConfig,
    process_all_documents: bool = False,
    backfill_existing_documents: bool = False,
    document_limit_override: Optional[int] = None,
    run_state_file: str = RUN_STATE_FILE_DEFAULT,
    stop_request_file: str = STOP_REQUEST_FILE_DEFAULT,
    resume_run: bool = False,
) -> None:
    """Hauptablauf: Laden, KI-Klassifizieren, validieren, patchen."""

    client = PaperlessClient(config)
    classifier = AiClassifier(config)
    tax_service: Optional[TaxEnrichmentService] = None
    tax_export_collector: Optional[TaxExportCollector] = None
    tax_enrichment_errors = 0
    generic_custom_field_sync_enabled = bool(config.enable_custom_field_enrichment)
    secondbrain_sync_enabled = bool(config.enable_secondbrain_custom_fields)
    runtime_base_dir = Path.cwd()
    run_state_path = resolve_runtime_path(run_state_file, runtime_base_dir)
    stop_request_path = resolve_runtime_path(stop_request_file, runtime_base_dir)
    existing_run_state = load_run_state(run_state_path) if resume_run else {}
    if resume_run and existing_run_state:
        LOGGER.info(
            "Resume-Modus aktiv: gespeicherter Laufzustand aus %s wird fortgesetzt.",
            run_state_path,
        )
    elif resume_run:
        LOGGER.warning(
            "Resume-Modus angefordert, aber kein Laufzustand unter %s gefunden. Starte regulären Lauf.",
            run_state_path,
        )
    custom_field_definitions = (
        DEFAULT_CUSTOM_FIELD_DEFINITIONS if generic_custom_field_sync_enabled else {}
    )
    if config.enable_tax_enrichment:
        tax_service = TaxEnrichmentService(
            ai_model=config.ai_model,
            ai_api_key=config.ai_api_key,
            ai_base_url=config.ai_base_url,
            request_timeout_seconds=config.request_timeout_seconds,
            basis_config=config.basis_config,
            personal_context=config.tax_personal_context,
        )
        tax_export_collector = TaxExportCollector(
            basis_config=config.basis_config,
            export_years=config.tax_export_years,
        )
        LOGGER.info(
            "Tax Enrichment aktiv: Export-Verzeichnis=%s | Steuerjahre=%s",
            config.tax_export_dir,
            ", ".join(str(year) for year in config.tax_export_years) if config.tax_export_years else "alle",
        )
    if generic_custom_field_sync_enabled:
        LOGGER.info(
            "Custom-Field-Enrichment aktiv: %s definierte Felder fuer Vertrag/Lohnabrechnung.",
            len(custom_field_definitions),
        )
    if secondbrain_sync_enabled:
        LOGGER.info(
            "SecondBrain-Custom-Fields aktiv: overwrite_existing=%s | "
            "attach_empty_when_unknown=%s | confidence_threshold=%.2f",
            config.secondbrain_custom_fields_overwrite_existing,
            config.secondbrain_custom_fields_attach_empty_when_unknown,
            config.secondbrain_custom_fields_confidence_threshold,
        )

    LOGGER.info("Prüfe KI-Token-Budget...")
    classifier.preflight_token_budget()
    LOGGER.info("Prüfe Paperless-API Erreichbarkeit...")
    client.preflight_check()
    LOGGER.info("Lade Metadaten-Mappings aus Paperless...")
    tags_map = client.list_named_entities("/api/tags/")
    doc_types_map = client.list_named_entities("/api/document_types/")
    correspondents_map = client.list_named_entities("/api/correspondents/")
    storage_paths_map = client.list_named_entities("/api/storage_paths/")
    if generic_custom_field_sync_enabled or secondbrain_sync_enabled:
        try:
            custom_fields_map = client.get_custom_fields_by_name()
        except PaperlessApiError as exc:
            LOGGER.error(
                "Custom-Field-Sync konnte nicht initialisiert werden. "
                "Die normale Dokumentklassifikation läuft weiter, aber Custom Fields werden "
                "für diesen Lauf übersprungen. Prüfe /api/custom_fields/, Rechte und "
                "Paperless-Version. Fehler: %s",
                exc,
            )
            custom_fields_map = {}
            generic_custom_field_sync_enabled = False
            secondbrain_sync_enabled = False
    else:
        custom_fields_map = {}
    classifier.set_known_entities(
        document_types=list(doc_types_map.keys()),
        correspondents=list(correspondents_map.keys()),
        storage_paths=list(storage_paths_map.keys()),
    )
    tag_id_to_label = {entity_id: label for label, entity_id in tags_map.items()}
    doc_type_id_to_label = {entity_id: label for label, entity_id in doc_types_map.items()}
    correspondent_id_to_label = {entity_id: label for label, entity_id in correspondents_map.items()}
    storage_path_id_to_label = {entity_id: label for label, entity_id in storage_paths_map.items()}
    failed_docs_path = Path(config.failed_documents_file)
    failed_patch_cache_path = Path(config.failed_patch_cache_file)
    tag_bypass_path = Path(config.tag_bypass_file)
    failed_docs_until: Dict[str, float] = {}
    failed_patch_cache: Dict[str, Dict[str, Any]] = {}
    tag_bypass_docs: Dict[str, Dict[str, Any]] = {}
    failed_docs_cooldown_seconds = max(0, int(config.failed_document_cooldown_hours)) * 3600
    failed_tags_only_cooldown_seconds = max(0, int(config.failed_tags_only_cooldown_hours)) * 3600
    if config.quarantine_failed_documents:
        failed_docs_until = load_failed_documents(failed_docs_path)
        failed_patch_cache = load_failed_patch_cache(failed_patch_cache_path)
    if config.enable_tag_bypass_on_tags_500:
        tag_bypass_docs = load_tag_bypass_documents(tag_bypass_path)
    if config.quarantine_failed_documents:
        now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
        # Abgelaufene Einträge direkt entfernen, damit die Datei klein bleibt.
        failed_docs_until = {
            doc_key: retry_ts
            for doc_key, retry_ts in failed_docs_until.items()
            if float(retry_ts) > now_ts
        }

    scanned = 0
    updated = 0
    skipped = 0
    failed = 0
    bypassed = 0
    bypass_skipped = 0
    skipped_with_neu_still_set = 0
    created_entities: Dict[str, List[str]] = {}
    error_details: List[Dict[str, Any]] = []
    run_prompt_tokens = 0
    run_completion_tokens = 0
    run_total_tokens = 0
    run_cost_eur = 0.0
    perf_apply_seconds = 0.0
    perf_ai_seconds = 0.0
    perf_ai_batches = 0
    perf_ai_docs = 0
    prefilt_ki_tagged = 0
    prefilt_secondbrain_ready = 0
    completed_document_ids: set[int] = set()
    restored_pending_documents: List[PendingAiDocument] = []
    progress_total_documents = 0
    can_create_entities = config.create_missing_entities and not config.dry_run
    can_create_custom_fields = config.create_missing_custom_fields and not config.dry_run
    ki_tag_id = ensure_entity_id(
        client,
        tags_map,
        "KI",
        "/api/tags/",
        can_create_entities,
        created_entities,
    )
    error_tag_id = ensure_entity_id(
        client,
        tags_map,
        "KI_FEHLER",
        "/api/tags/",
        can_create_entities,
        created_entities,
    )
    skip_tag_id = ensure_entity_id(
        client,
        tags_map,
        "KI_SKIP",
        "/api/tags/",
        can_create_entities,
        created_entities,
    )
    skip_precheck_tag_id = ensure_entity_id(
        client,
        tags_map,
        "KI_SKIP_PRECHECK",
        "/api/tags/",
        can_create_entities,
        created_entities,
    )
    tax_not_relevant_tag_id: Optional[int] = None
    secondbrain_ready_tag_id: Optional[int] = None
    if config.enable_tax_enrichment:
        tax_not_relevant_tag_id = ensure_entity_id(
            client,
            tags_map,
            "KI nicht Steuerrelevant",
            "/api/tags/",
            can_create_entities,
            created_entities,
        )
    if secondbrain_sync_enabled:
        secondbrain_ready_tag_id = ensure_entity_id(
            client,
            tags_map,
            "SB",
            "/api/tags/",
            can_create_entities,
            created_entities,
        )
    resumed_mode = existing_run_state.get("mode") or {}
    if resumed_mode:
        process_all_documents = bool(resumed_mode.get("process_all_documents", process_all_documents))
        backfill_existing_documents = bool(
            resumed_mode.get("backfill_existing_documents", backfill_existing_documents)
        )
        if resumed_mode.get("document_limit_override") not in (None, ""):
            try:
                document_limit_override = int(resumed_mode.get("document_limit_override"))
            except (TypeError, ValueError):
                document_limit_override = document_limit_override
    effective_process_all_documents = process_all_documents or backfill_existing_documents
    remove_neu_tag_id = tags_map.get("#neu")
    only_tag_id: Optional[int] = None
    only_tag_name = config.process_only_tag.strip()
    doc_query_params: Dict[str, Any] = {}
    if document_limit_override is not None and int(document_limit_override) > 0:
        target_documents: Optional[int] = max(1, int(document_limit_override))
    elif backfill_existing_documents:
        target_documents = None
    else:
        target_documents = max(1, int(config.max_documents))

    fetch_limit: Optional[int] = target_documents
    if fetch_limit is not None and (
        config.quarantine_failed_documents
        or config.enable_tag_bypass_on_tags_500
        or not config.reprocess_ki_tagged_documents
    ):
        # Zusätzlicher Puffer: Quarantäne/Bypass-Dokumente sollen nicht gegen das
        # max_documents-Limit zählen, daher laden wir mehr Kandidaten nach.
        # Dasselbe gilt für KI-Tag-Vorfilter.
        fetch_limit = max(target_documents * 10, target_documents + 100)
    budget_used = 0
    if existing_run_state:
        progress_state = existing_run_state.get("progress") or {}
        scanned = int(progress_state.get("scanned", scanned) or scanned)
        updated = int(progress_state.get("updated", updated) or updated)
        skipped = int(progress_state.get("skipped", skipped) or skipped)
        failed = int(progress_state.get("failed", failed) or failed)
        bypassed = int(progress_state.get("bypassed", bypassed) or bypassed)
        bypass_skipped = int(progress_state.get("bypass_skipped", bypass_skipped) or bypass_skipped)
        skipped_with_neu_still_set = int(
            progress_state.get("skipped_with_neu_still_set", skipped_with_neu_still_set)
            or skipped_with_neu_still_set
        )
        prefilt_ki_tagged = int(progress_state.get("prefilt_ki_tagged", prefilt_ki_tagged) or prefilt_ki_tagged)
        budget_used = int(progress_state.get("budget_used", budget_used) or budget_used)
        run_prompt_tokens = int(progress_state.get("run_prompt_tokens", run_prompt_tokens) or run_prompt_tokens)
        run_completion_tokens = int(
            progress_state.get("run_completion_tokens", run_completion_tokens) or run_completion_tokens
        )
        run_total_tokens = int(progress_state.get("run_total_tokens", run_total_tokens) or run_total_tokens)
        run_cost_eur = float(progress_state.get("run_cost_eur", run_cost_eur) or run_cost_eur)
        perf_apply_seconds = float(progress_state.get("perf_apply_seconds", perf_apply_seconds) or perf_apply_seconds)
        perf_ai_seconds = float(progress_state.get("perf_ai_seconds", perf_ai_seconds) or perf_ai_seconds)
        perf_ai_batches = int(progress_state.get("perf_ai_batches", perf_ai_batches) or perf_ai_batches)
        perf_ai_docs = int(progress_state.get("perf_ai_docs", perf_ai_docs) or perf_ai_docs)
        progress_total_documents = int(
            progress_state.get("total_documents", progress_total_documents) or progress_total_documents
        )
        completed_document_ids = {
            int(doc_id)
            for doc_id in (existing_run_state.get("completed_document_ids") or [])
            if str(doc_id).strip()
        }
        restored_pending_documents = [
            PendingAiDocument.from_state_dict(item)
            for item in (existing_run_state.get("pending_documents") or [])
            if isinstance(item, dict)
        ]
    if backfill_existing_documents:
        LOGGER.info(
            "Backfill-Modus aktiv: Bestehende Paperless-Datenbank wird erneut "
            "für neue Anreicherungen durchsucht. Bereits KI-getaggte Dokumente "
            "werden nur für Zusatzfunktionen aktualisiert."
        )
        if target_documents is None:
            LOGGER.info(
                "Backfill läuft ohne Dokumentlimit. Mit --max-documents lässt sich "
                "der Gesamtdurchlauf bei Bedarf in mehrere Chargen aufteilen."
            )
        else:
            LOGGER.info("Backfill-Limit aktiv: maximal %s Dokument(e).", target_documents)
    elif effective_process_all_documents:
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

    pending_ai_documents: List[PendingAiDocument] = []
    parallel_ai_enabled = bool(config.enable_parallel_ai and config.max_parallel_ai_jobs > 1)
    parallel_ai_workers = max(1, int(config.max_parallel_ai_jobs))
    if parallel_ai_enabled:
        LOGGER.info(
            "Parallele KI-Verarbeitung aktiv: max_parallel_ai_jobs=%s",
            parallel_ai_workers,
        )

    if progress_total_documents <= 0:
        if target_documents is not None:
            progress_total_documents = int(target_documents)
        else:
            progress_total_documents = client.count_documents(doc_query_params)

    def _progress_payload(
        *,
        current_document_id: Optional[int] = None,
        current_document_title: str = "",
        status: str = "running",
        pause_reason: Optional[str] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Baut einen serialisierbaren Laufzustand für Fortschritt und Resume.

        Enthalten sind:
        - Fortschrittszähler für UI und Debugging
        - bereits abgeschlossene Dokumente
        - aktuell noch offene Batch-Dokumente
        - Modus-Informationen, damit Resume denselben Lauf fortsetzen kann
        """

        total = max(0, int(progress_total_documents))
        if target_documents is None:
            completed = len(completed_document_ids)
        else:
            completed = max(0, min(int(target_documents), int(budget_used) - len(pending_ai_documents)))
        percent = round((completed / total) * 100.0, 2) if total else 0.0
        return {
            "version": RUN_STATE_VERSION,
            "status": status,
            "pause_reason": pause_reason,
            "retry_after_seconds": retry_after_seconds,
            "mode": {
                "process_all_documents": bool(process_all_documents),
                "backfill_existing_documents": bool(backfill_existing_documents),
                "document_limit_override": document_limit_override,
            },
            "progress": {
                "total_documents": total,
                "completed_documents": completed,
                "percent": percent,
                "scanned": scanned,
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
                "bypassed": bypassed,
                "bypass_skipped": bypass_skipped,
                "prefilt_ki_tagged": prefilt_ki_tagged,
                "prefilt_secondbrain_ready": prefilt_secondbrain_ready,
                "skipped_with_neu_still_set": skipped_with_neu_still_set,
                "budget_used": budget_used,
                "run_prompt_tokens": run_prompt_tokens,
                "run_completion_tokens": run_completion_tokens,
                "run_total_tokens": run_total_tokens,
                "run_cost_eur": round(run_cost_eur, 6),
                "perf_apply_seconds": round(perf_apply_seconds, 6),
                "perf_ai_seconds": round(perf_ai_seconds, 6),
                "perf_ai_batches": perf_ai_batches,
                "perf_ai_docs": perf_ai_docs,
            },
            "completed_document_ids": sorted(completed_document_ids),
            "pending_documents": [
                item.to_state_dict()
                for item in pending_ai_documents
            ],
            "current_document": {
                "id": current_document_id,
                "title": current_document_title,
            },
        }

    def _persist_run_state(
        *,
        current_document_id: Optional[int] = None,
        current_document_title: str = "",
        status: str = "running",
        pause_reason: Optional[str] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Schreibt den aktuellen Laufzustand auf Disk und gibt ihn zurück."""

        payload = _progress_payload(
            current_document_id=current_document_id,
            current_document_title=current_document_title,
            status=status,
            pause_reason=pause_reason,
            retry_after_seconds=retry_after_seconds,
        )
        save_run_state(run_state_path, payload)
        return payload

    def _emit_progress(
        *,
        current_document_id: Optional[int] = None,
        current_document_title: str = "",
        status: str = "running",
    ) -> None:
        """Sendet einen Live-Fortschrittspunkt für Home Assistant ins Log."""

        payload = _progress_payload(
            current_document_id=current_document_id,
            current_document_title=current_document_title,
            status=status,
        )
        emit_runtime_event("progress", **payload)

    def _mark_completed(
        *,
        document_id: Optional[int],
        document_title: str,
        status: str = "running",
    ) -> None:
        """Markiert ein Dokument final als erledigt und aktualisiert Fortschritt."""

        if document_id is not None:
            completed_document_ids.add(int(document_id))
        _persist_run_state(
            current_document_id=document_id,
            current_document_title=document_title,
            status=status,
        )
        _emit_progress(
            current_document_id=document_id,
            current_document_title=document_title,
            status=status,
        )

    def _pause_run(
        *,
        pause_reason: str,
        retry_after_seconds: Optional[float],
        current_document_id: Optional[int],
        current_document_title: str,
        pending_items: Optional[List[PendingAiDocument]] = None,
    ) -> None:
        """Speichert Pause-Zustand und bricht den Lauf kontrolliert ab."""

        original_pending = list(pending_ai_documents)
        if pending_items is not None:
            pending_ai_documents.clear()
            pending_ai_documents.extend(pending_items)
        pause_state = _persist_run_state(
            current_document_id=current_document_id,
            current_document_title=current_document_title,
            status="paused",
            pause_reason=pause_reason,
            retry_after_seconds=retry_after_seconds,
        )
        emit_runtime_event(
            "paused",
            **pause_state,
        )
        delete_runtime_file(stop_request_path)
        pending_ai_documents.clear()
        pending_ai_documents.extend(original_pending)
        raise RunPausedError(
            f"Lauf pausiert: {pause_reason}",
            pause_reason=pause_reason,
            retry_after_seconds=retry_after_seconds,
            pause_state=pause_state,
        )

    def _check_for_manual_stop(
        *,
        current_document_id: Optional[int] = None,
        current_document_title: str = "",
    ) -> None:
        """Hält den Lauf an einem sicheren Punkt an, wenn ein Stop angefordert wurde."""

        if not is_stop_requested(stop_request_path):
            return
        _pause_run(
            pause_reason="manual_stop",
            retry_after_seconds=None,
            current_document_id=current_document_id,
            current_document_title=current_document_title,
        )

    if restored_pending_documents:
        LOGGER.info(
            "Resume enthält %s noch nicht abgeschlossene Batch-Dokument(e), die zuerst fortgesetzt werden.",
            len(restored_pending_documents),
        )
    _persist_run_state(status="running")
    _emit_progress(status="running")

    def _build_existing_prediction(document: Dict[str, Any]) -> Dict[str, Any]:
        """Builds a classification-like object from already stored Paperless metadata.

        Why this exists:
        - Existing KI-tagged documents should be tax-enriched without forcing a
          second full standard classification run.
        - The tax AI still benefits from structured context that looks like the
          normal classifier output.
        """

        def _label_from_id(entity_id: Any, mapping: Dict[int, str]) -> Optional[str]:
            try:
                if entity_id is None:
                    return None
                return mapping.get(int(entity_id))
            except (TypeError, ValueError):
                return None

        tag_labels: List[str] = []
        for tag_id in document.get("tags", []) or []:
            try:
                label = tag_id_to_label.get(int(tag_id))
            except (TypeError, ValueError):
                label = None
            if label:
                tag_labels.append(label)

        return {
            "document_type": _label_from_id(document.get("document_type"), doc_type_id_to_label),
            "correspondent": _label_from_id(document.get("correspondent"), correspondent_id_to_label),
            "storage_path": _label_from_id(document.get("storage_path"), storage_path_id_to_label),
            "tags": sorted(tag_labels),
            "document_date": normalize_iso_date(document.get("created")),
            "summary": "Bestehende Paperless-Metadaten als Kontext fuer Tax Enrichment verwendet.",
            "confidence": 1.0,
            "rationale": "Dokument war bereits KI-getaggt; fuer die Steueranalyse wurden die vorhandenen Metadaten wiederverwendet.",
        }

    def _apply_tax_tags(
        *,
        document: Dict[str, Any],
        enrichment: Any,
    ) -> None:
        """Mirrors the tax result into Paperless tags without breaking the main run.

        We keep this best-effort:
        - Tax exports remain usable even if a Paperless tag update fails.
        - Existing classification must not fail just because a tax tag could not
          be written.
        """

        if config.dry_run:
            return
        doc_id_local = document.get("id")
        if doc_id_local is None:
            return

        current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
        desired_tags = set(current_tags)
        relevant_tag_ids = {
            int(entity_id)
            for label, entity_id in tags_map.items()
            if label.startswith("ki steuerrelevant ")
        }

        desired_tags.difference_update(relevant_tag_ids)
        if tax_not_relevant_tag_id is not None:
            desired_tags.discard(int(tax_not_relevant_tag_id))

        for tag_label in build_tax_tag_labels(enrichment):
            tag_id = ensure_entity_id(
                client,
                tags_map,
                tag_label,
                "/api/tags/",
                can_create_entities,
                created_entities,
            )
            if tag_id is not None:
                desired_tags.add(int(tag_id))

        if desired_tags == current_tags:
            return
        try:
            client.update_document(int(doc_id_local), {"tags": sorted(desired_tags)})
            document["tags"] = sorted(desired_tags)
        except PaperlessApiError as tax_tag_exc:
            LOGGER.warning(
                "Tax-Tags konnten fuer Dokument %s (%s) nicht gesetzt werden: %s",
                doc_id_local,
                document.get("title", "<ohne Titel>"),
                tax_tag_exc,
            )

    def _run_tax_only_for_document(
        *,
        document: Dict[str, Any],
        doc_id: Optional[int],
        title: str,
    ) -> bool:
        """Runs tax enrichment for an already KI-tagged document.

        Returns True if a tax analysis was performed, False if the feature is not
        active. This path intentionally skips the normal classification update.
        """

        nonlocal updated, failed, tax_enrichment_errors

        if tax_service is None or tax_export_collector is None:
            return False

        try:
            enrichment = tax_service.enrich(
                document=document,
                classification_prediction=_build_existing_prediction(document),
            )
            tax_export_collector.add(enrichment)
            _apply_tax_tags(document=document, enrichment=enrichment)
            LOGGER.info(
                "Tax Enrichment fuer bereits KI-getaggtes Dokument %s (%s) abgeschlossen",
                doc_id,
                title,
            )
            updated += 1
            return True
        except TaxPauseRequested as tax_pause_exc:
            raise AiTemporaryPauseError(
                str(tax_pause_exc),
                pause_reason=tax_pause_exc.pause_reason,
                retry_after_seconds=tax_pause_exc.retry_after_seconds,
                document_context={"document_id": doc_id, "title": title},
            ) from tax_pause_exc
        except TaxEnrichmentError as tax_exc:
            tax_enrichment_errors += 1
            failed += 1
            LOGGER.warning(
                "Tax Enrichment fehlgeschlagen fuer KI-getaggtes Dokument %s (%s): %s",
                doc_id,
                title,
                tax_exc,
            )
            return True

    def _classify_pending_documents(
        items: List[PendingAiDocument],
    ) -> List[tuple[PendingAiDocument, Optional[Dict[str, Any]], Optional[Exception]]]:
        """Klassifiziert gepufferte Dokumente seriell oder parallel.

        Bei Parallelmodus verwenden wir pro Worker einen eigenen AiClassifier,
        damit Request-Sessions nicht zwischen Threads geteilt werden.
        """

        nonlocal perf_ai_seconds, perf_ai_batches, perf_ai_docs

        if not items:
            return []

        batch_started = time.perf_counter()
        if not parallel_ai_enabled:
            result: List[tuple[PendingAiDocument, Optional[Dict[str, Any]], Optional[Exception]]] = []
            for item in items:
                try:
                    result.append((item, classifier.classify(item.document), None))
                except Exception as exc:  # noqa: BLE001
                    result.append((item, None, exc))
            perf_ai_seconds += max(0.0, time.perf_counter() - batch_started)
            perf_ai_batches += 1
            perf_ai_docs += len(items)
            return result

        known_doc_types = list(classifier.known_document_types)
        known_correspondents = list(classifier.known_correspondents)
        known_storage_paths = list(classifier.known_storage_paths)

        def _worker_classify(item: PendingAiDocument) -> Dict[str, Any]:
            worker = AiClassifier(config)
            worker.set_known_entities(
                document_types=known_doc_types,
                correspondents=known_correspondents,
                storage_paths=known_storage_paths,
            )
            return worker.classify(item.document)

        results_map: Dict[int, tuple[Optional[Dict[str, Any]], Optional[Exception]]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_ai_workers) as executor:
            futures = {
                executor.submit(_worker_classify, item): idx
                for idx, item in enumerate(items)
            }
            for future in concurrent.futures.as_completed(futures):
                idx = futures[future]
                try:
                    results_map[idx] = (future.result(), None)
                except Exception as exc:  # noqa: BLE001
                    results_map[idx] = (None, exc)

        ordered: List[tuple[PendingAiDocument, Optional[Dict[str, Any]], Optional[Exception]]] = []
        for idx, item in enumerate(items):
            prediction, exc = results_map.get(idx, (None, AiClassificationError("Parallel-Worker lieferte kein Ergebnis")))
            ordered.append((item, prediction, exc))
        perf_ai_seconds += max(0.0, time.perf_counter() - batch_started)
        perf_ai_batches += 1
        perf_ai_docs += len(items)
        return ordered

    def _flush_pending_batch(items: List[PendingAiDocument]) -> None:
        """Verarbeitet einen KI-Batch vollständig oder pausiert kontrolliert.

        Verhalten:
        - erfolgreiche Dokumente werden direkt angewendet
        - Dokumente mit planbarer Pause bleiben als Pending-State erhalten
        - danach wird der Gesamtlauf mit Resume-Information pausiert
        """

        if not items:
            return
        _check_for_manual_stop(
            current_document_id=items[0].doc_id,
            current_document_title=items[0].title,
        )
        batch_results = _classify_pending_documents(items)
        pause_items: List[PendingAiDocument] = []
        pause_exc: Optional[AiTemporaryPauseError] = None
        for pending, prediction, pred_exc in batch_results:
            if isinstance(pred_exc, AiTemporaryPauseError):
                if pause_exc is None:
                    pause_exc = pred_exc
                pause_items.append(pending)
                continue
            _apply_ai_result(pending, prediction, pred_exc)
        if pause_exc is not None:
            _pause_run(
                pause_reason=pause_exc.pause_reason,
                retry_after_seconds=pause_exc.retry_after_seconds,
                current_document_id=pause_items[0].doc_id if pause_items else None,
                current_document_title=pause_items[0].title if pause_items else "",
                pending_items=pause_items,
            )

    def _apply_ai_result(
        pending: PendingAiDocument,
        prediction: Optional[Dict[str, Any]],
        classification_exc: Optional[Exception],
    ) -> None:
        """Verarbeitet ein KI-Ergebnis inkl. Patch/Skip/Fehlerbehandlung."""

        nonlocal updated, skipped, failed, bypassed
        nonlocal run_prompt_tokens, run_completion_tokens, run_total_tokens, run_cost_eur
        nonlocal perf_apply_seconds
        nonlocal skipped_with_neu_still_set
        nonlocal tax_enrichment_errors

        document = pending.document
        doc_id = pending.doc_id
        doc_key = pending.doc_key
        title = pending.title
        doc_tags = pending.doc_tags
        patch_payload_for_error: Optional[Dict[str, Any]] = None
        tax_enrichment_result: Optional[Any] = None
        secondbrain_sync_report = build_secondbrain_sync_report()
        mark_completed_on_exit = True

        try:
            if classification_exc is not None:
                if isinstance(classification_exc, AiTemporaryPauseError):
                    raise classification_exc
                raise classification_exc
            if prediction is None:
                raise AiClassificationError("KI lieferte kein Ergebnis.")

            prediction = sanitize_prediction(
                prediction,
                storage_paths_map,
                custom_field_definitions if generic_custom_field_sync_enabled else None,
            )
            prompt_tokens, completion_tokens, total_tokens = extract_usage(prediction)
            run_prompt_tokens += prompt_tokens
            run_completion_tokens += completion_tokens
            run_total_tokens += total_tokens
            run_cost_eur += (
                (prompt_tokens / 1000.0) * config.input_cost_per_1k_tokens_eur
                + (completion_tokens / 1000.0) * config.output_cost_per_1k_tokens_eur
            )
            if tax_service is not None and tax_export_collector is not None:
                try:
                    tax_enrichment_result = tax_service.enrich(
                        document=document,
                        classification_prediction=prediction,
                    )
                    tax_export_collector.add(tax_enrichment_result)
                    _apply_tax_tags(document=document, enrichment=tax_enrichment_result)
                except TaxPauseRequested as tax_pause_exc:
                    raise AiTemporaryPauseError(
                        str(tax_pause_exc),
                        pause_reason=tax_pause_exc.pause_reason,
                        retry_after_seconds=tax_pause_exc.retry_after_seconds,
                        document_context={"document_id": doc_id, "title": title},
                    ) from tax_pause_exc
                except TaxEnrichmentError as tax_exc:
                    tax_enrichment_errors += 1
                    LOGGER.warning(
                        "Tax Enrichment fehlgeschlagen fuer Dokument %s (%s): %s",
                        doc_id,
                        title,
                        tax_exc,
                    )
            confidence = float(prediction["confidence"])
            if confidence < config.confidence_threshold and not pending.enrichment_only:
                if not config.dry_run and doc_id is not None:
                    current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                    new_tags = set(current_tags)
                    if remove_neu_tag_id is not None:
                        new_tags.discard(int(remove_neu_tag_id))
                    if skip_tag_id is not None:
                        new_tags.add(int(skip_tag_id))
                    if ki_tag_id is not None:
                        new_tags.add(int(ki_tag_id))
                    if new_tags != current_tags:
                        try:
                            client._request(
                                "PATCH",
                                f"/api/documents/{int(doc_id)}/",
                                payload={"tags": sorted(new_tags)},
                                retries=1,
                            )
                        except PaperlessApiError as skip_tag_exc:
                            LOGGER.error(
                                "KI_SKIP-Tag konnte für Dokument %s (%s) nicht gesetzt werden: %s",
                                doc_id,
                                title,
                                skip_tag_exc,
                            )
                    try:
                        client.add_document_note(
                            int(doc_id),
                            build_skip_note_entry(
                                prediction=prediction,
                                confidence_threshold=config.confidence_threshold,
                            ),
                        )
                    except PaperlessApiError as skip_note_exc:
                        LOGGER.error(
                            "KI-Skip-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                            doc_id,
                            title,
                            skip_note_exc,
                        )
                LOGGER.info(
                    "Skip Dokument %s (%s): Confidence %.2f unter Schwellwert %.2f",
                    doc_id,
                    title,
                    confidence,
                    config.confidence_threshold,
                )
                skipped += 1
                return
            if confidence < config.confidence_threshold and pending.enrichment_only:
                LOGGER.info(
                    "Backfill Dokument %s (%s): Confidence %.2f unter Schwellwert %.2f, "
                    "Zusatzfelder werden dennoch geprüft.",
                    doc_id,
                    title,
                    confidence,
                    config.confidence_threshold,
                )

            custom_field_id_to_definition: Dict[int, CustomFieldDefinition] = {}
            patch_payload = build_patch_payload(
                client=client,
                document=document,
                prediction=prediction,
                tags_map=tags_map,
                doc_types_map=doc_types_map,
                correspondents_map=correspondents_map,
                storage_paths_map=storage_paths_map,
                custom_fields_map=custom_fields_map if (generic_custom_field_sync_enabled or secondbrain_sync_enabled) else None,
                custom_field_definitions=custom_field_definitions if generic_custom_field_sync_enabled else None,
                create_missing_entities=can_create_entities,
                create_missing_custom_fields=can_create_custom_fields,
                include_standard_metadata=not pending.enrichment_only,
                enable_secondbrain_custom_fields=secondbrain_sync_enabled,
                secondbrain_overwrite_existing=config.secondbrain_custom_fields_overwrite_existing,
                secondbrain_attach_empty_when_unknown=config.secondbrain_custom_fields_attach_empty_when_unknown,
                secondbrain_confidence_threshold=config.secondbrain_custom_fields_confidence_threshold,
                secondbrain_log_missing_fields=config.secondbrain_custom_fields_log_missing_fields,
                tax_enrichment=tax_enrichment_result,
                created_entities=created_entities,
                custom_field_id_to_definition=custom_field_id_to_definition,
                secondbrain_sync_report=secondbrain_sync_report,
                secondbrain_ready_tag_id=secondbrain_ready_tag_id,
            )
            patch_payload_for_error = dict(patch_payload)

            if not patch_payload:
                LOGGER.info("Skip Dokument %s (%s): Keine verwertbaren Felder im KI-Output", doc_id, title)
                if secondbrain_sync_enabled:
                    log_secondbrain_sync_report(
                        doc_id=doc_id,
                        title=title,
                        sync_report=secondbrain_sync_report,
                    )
                skipped += 1
                return

            if not pending.enrichment_only:
                apply_forced_tag_rules(
                    patch_payload=patch_payload,
                    current_tag_ids=doc_tags,
                    ki_tag_id=ki_tag_id,
                    remove_neu_tag_id=remove_neu_tag_id,
                )
            patch_payload = filter_unchanged_patch_fields(
                document=document,
                patch_payload=patch_payload,
                custom_field_id_to_definition=custom_field_id_to_definition,
            )
            patch_payload_for_error = dict(patch_payload)

            if not patch_payload:
                LOGGER.info(
                    "Skip Dokument %s (%s): Keine effektiven Änderungen nach Diff-Filter",
                    doc_id,
                    title,
                )
                if secondbrain_sync_enabled:
                    log_secondbrain_sync_report(
                        doc_id=doc_id,
                        title=title,
                        sync_report=secondbrain_sync_report,
                    )
                skipped += 1
                return

            tag_id_to_label_local = {entity_id: label for label, entity_id in tags_map.items()}
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
                    tag_id_to_label=tag_id_to_label_local,
                    custom_field_id_to_definition=custom_field_id_to_definition,
                    secondbrain_sync_report=secondbrain_sync_report,
                    max_chars=config.ai_notes_max_chars,
                    include_summary=config.enable_ai_note_summary,
                    summary_max_chars=config.ai_note_summary_max_chars,
                )

            if secondbrain_sync_enabled:
                log_secondbrain_sync_report(
                    doc_id=doc_id,
                    title=title,
                    sync_report=secondbrain_sync_report,
                )

            if config.dry_run:
                log_dry_run_change(
                    document=document,
                    prediction=prediction,
                    patch_payload=patch_payload,
                    note_will_be_added=config.enable_ai_notes,
                    tag_id_to_label=tag_id_to_label_local,
                    doc_type_id_to_label=doc_type_id_to_label,
                    correspondent_id_to_label=correspondent_id_to_label,
                    storage_path_id_to_label=storage_path_id_to_label,
                    custom_field_id_to_definition=custom_field_id_to_definition,
                )
            else:
                apply_started = time.perf_counter()
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
                perf_apply_seconds += max(0.0, time.perf_counter() - apply_started)

            if config.quarantine_failed_documents and doc_key is not None:
                failed_patch_cache.pop(doc_key, None)
            updated += 1
        except AiTemporaryPauseError:
            mark_completed_on_exit = False
            raise
        except (AiClassificationError, PaperlessApiError, ValueError, Exception) as exc:  # noqa: BLE001
            if secondbrain_sync_enabled:
                secondbrain_sync_report["api_errors"].append(str(exc))
                log_secondbrain_sync_report(
                    doc_id=doc_id,
                    title=title,
                    sync_report=secondbrain_sync_report,
                )
            error_text = str(exc)
            tags_only_patch = bool(
                patch_payload_for_error
                and set(patch_payload_for_error.keys()) == {"tags"}
            )
            tags_only_500 = tags_only_patch and (
                ("Feldanalyse: tags:" in error_text) or ("HTTP 500" in error_text)
            )
            if (
                config.enable_tag_bypass_on_tags_500
                and doc_key is not None
                and tags_only_500
            ):
                tag_bypass_docs[doc_key] = {
                    "document_id": int(doc_id) if doc_id is not None else None,
                    "title": title,
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "reason": "tags-only PATCH führt zu HTTP 500",
                    "patch_payload": dict(patch_payload_for_error or {}),
                }
                failed_docs_until.pop(doc_key, None)
                failed_patch_cache.pop(doc_key, None)
                bypassed += 1
                updated += 1
                LOGGER.warning(
                    "Tag-Bypass aktiv für Dokument %s (%s): tags-only PATCH erzeugt HTTP 500. "
                    "Dokument wird als verarbeitet markiert, Tag-Änderung bleibt offen.",
                    doc_id,
                    title,
                )
                if not config.dry_run and doc_id is not None:
                    try:
                        client.add_document_note(
                            int(doc_id),
                            (
                                "[KI-Bypass] Tag-Update konnte nicht gespeichert werden "
                                "(tags-only PATCH -> HTTP 500). "
                                "Dokument wurde zur Vermeidung weiterer KI-Kosten in den "
                                "Tag-Bypass übernommen."
                            ),
                        )
                    except PaperlessApiError as bypass_note_exc:
                        LOGGER.error(
                            "Bypass-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                            doc_id,
                            title,
                            bypass_note_exc,
                        )
                return

            failed += 1
            if (
                config.quarantine_failed_documents
                and doc_key is not None
                and failed_docs_cooldown_seconds > 0
            ):
                retry_delay_seconds = failed_docs_cooldown_seconds
                if (
                    tags_only_patch
                    and "Feldanalyse: tags:" in error_text
                    and failed_tags_only_cooldown_seconds > retry_delay_seconds
                ):
                    retry_delay_seconds = failed_tags_only_cooldown_seconds
                    if patch_payload_for_error:
                        failed_patch_cache[doc_key] = dict(patch_payload_for_error)
                    LOGGER.warning(
                        "Dokument %s (%s): Tags-only-Fehler erkannt, setze verlängerte Quarantäne auf %s Stunden.",
                        doc_id,
                        title,
                        int(config.failed_tags_only_cooldown_hours),
                    )

                retry_after_ts = dt.datetime.now(dt.timezone.utc).timestamp() + retry_delay_seconds
                failed_docs_until[doc_key] = retry_after_ts
                retry_after_text = dt.datetime.fromtimestamp(
                    retry_after_ts, tz=dt.timezone.utc
                ).isoformat()
                LOGGER.warning(
                    "Dokument %s (%s) in Fehler-Quarantäne bis %s (UTC).",
                    doc_id,
                    title,
                    retry_after_text,
                )
            if not config.dry_run and doc_id is not None:
                current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                new_tags = set(current_tags)
                if remove_neu_tag_id is not None:
                    new_tags.discard(int(remove_neu_tag_id))
                if error_tag_id is not None:
                    new_tags.add(int(error_tag_id))
                if ki_tag_id is not None:
                    new_tags.add(int(ki_tag_id))
                if new_tags != current_tags:
                    try:
                        client._request(
                            "PATCH",
                            f"/api/documents/{int(doc_id)}/",
                            payload={"tags": sorted(new_tags)},
                            retries=1,
                        )
                    except PaperlessApiError as mark_tag_exc:
                        LOGGER.error(
                            "Fehler-Tag konnte für Dokument %s (%s) nicht gesetzt werden: %s",
                            doc_id,
                            title,
                            mark_tag_exc,
                        )
                try:
                    client.add_document_note(
                        int(doc_id),
                        build_error_note_entry(
                            error_message=str(exc),
                            patch_payload=patch_payload_for_error,
                        ),
                    )
                except PaperlessApiError as mark_note_exc:
                    LOGGER.error(
                        "Fehler-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                        doc_id,
                        title,
                        mark_note_exc,
                    )
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
        finally:
            if mark_completed_on_exit:
                _mark_completed(
                    document_id=doc_id,
                    document_title=title,
                )

    if restored_pending_documents:
        pending_ai_documents = list(restored_pending_documents)
        _flush_pending_batch(pending_ai_documents)
        pending_ai_documents.clear()

    for document in client.iter_documents(fetch_limit, extra_params=doc_query_params):
        doc_id = document.get("id")
        doc_key = str(doc_id) if doc_id is not None else None
        title = document.get("title", "<ohne Titel>")
        doc_tags = {int(tag_id) for tag_id in document.get("tags", [])}
        patch_payload_for_error: Optional[Dict[str, Any]] = None
        has_ki_tag = bool(ki_tag_id is not None and int(ki_tag_id) in doc_tags)
        _check_for_manual_stop(
            current_document_id=int(doc_id) if doc_id is not None else None,
            current_document_title=title,
        )
        if doc_id is not None and int(doc_id) in completed_document_ids:
            continue

        if backfill_existing_documents and secondbrain_sync_enabled:
            existing_secondbrain_fields = collect_populated_secondbrain_fields(
                document,
                custom_fields_map,
            )
            if existing_secondbrain_fields:
                prefilt_secondbrain_ready += 1
                if not config.dry_run and doc_id is not None and secondbrain_ready_tag_id is not None:
                    desired_tags = set(doc_tags)
                    desired_tags.add(int(secondbrain_ready_tag_id))
                    if desired_tags != doc_tags:
                        try:
                            client.update_document(
                                int(doc_id),
                                {"tags": sorted(desired_tags)},
                            )
                            doc_tags = set(desired_tags)
                            LOGGER.info(
                                "Backfill-Dokument %s (%s) als SecondBrain-vorbereitet markiert: SB-Tag gesetzt.",
                                doc_id,
                                title,
                            )
                        except PaperlessApiError as secondbrain_tag_exc:
                            LOGGER.warning(
                                "SB-Tag konnte für bereits vorbereitete Backfill-Datei %s (%s) nicht gesetzt werden: %s",
                                doc_id,
                                title,
                                secondbrain_tag_exc,
                            )
                LOGGER.info(
                    "Skip Dokument %s (%s): Backfill secondbrain_ready_prefilter (%s)",
                    doc_id,
                    title,
                    ", ".join(existing_secondbrain_fields[:6])
                    + (" ..." if len(existing_secondbrain_fields) > 6 else ""),
                )
                _mark_completed(
                    document_id=int(doc_id) if doc_id is not None else None,
                    document_title=title,
                )
                continue

        # Harte Vorfilter-Regel: Dokumente mit KI-Tag werden gar nicht angefasst,
        # außer Reprocessing wurde explizit aktiviert.
        if (
            has_ki_tag
            and not backfill_existing_documents
            and not config.reprocess_ki_tagged_documents
            and not (config.enable_tax_enrichment and config.tax_process_ki_tagged_documents)
        ):
            prefilt_ki_tagged += 1
            _mark_completed(
                document_id=int(doc_id) if doc_id is not None else None,
                document_title=title,
            )
            continue

        scanned += 1
        _emit_progress(
            current_document_id=int(doc_id) if doc_id is not None else None,
            current_document_title=title,
            status="running",
        )

        if config.enable_tag_bypass_on_tags_500 and doc_key is not None and doc_key in tag_bypass_docs:
            # Bypass-Dokumente sollen keine KI-Tokens verbrauchen.
            # Zusätzlich versuchen wir bei jedem Lauf, die gewünschte
            # Tag-Markierung herzustellen: #NEU entfernen, KI_SKIP setzen.
            # Wenn das gelingt, kann der Bypass-Eintrag entfernt werden.
            if not config.dry_run and doc_id is not None:
                current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                desired_tags = set(current_tags)
                if remove_neu_tag_id is not None:
                    desired_tags.discard(int(remove_neu_tag_id))
                if skip_tag_id is not None:
                    desired_tags.add(int(skip_tag_id))
                if desired_tags != current_tags:
                    try:
                        client.update_document(
                            int(doc_id),
                            {"tags": sorted(desired_tags)},
                        )
                        tag_bypass_docs.pop(doc_key, None)
                        doc_tags = set(desired_tags)
                        LOGGER.info(
                            "Bypass-Dokument %s (%s) markiert: #NEU entfernt, KI_SKIP gesetzt. "
                            "Bypass-Eintrag wurde entfernt.",
                            doc_id,
                            title,
                        )
                    except PaperlessApiError as bypass_mark_exc:
                        skipped_with_neu_still_set += 1
                        LOGGER.warning(
                            "Bypass-Dokument %s (%s) konnte nicht auf KI_SKIP/#NEU aktualisiert werden: %s",
                            doc_id,
                            title,
                            bypass_mark_exc,
                        )
            LOGGER.info(
                "Skip Dokument %s (%s): Tag-Bypass aktiv (tags-only 500).",
                doc_id,
                title,
            )
            bypass_skipped += 1
            skipped += 1
            _mark_completed(
                document_id=int(doc_id) if doc_id is not None else None,
                document_title=title,
            )
            continue

        if config.quarantine_failed_documents and doc_key is not None and doc_key in failed_patch_cache:
            retry_payload = failed_patch_cache.get(doc_key) or {}
            if config.dry_run:
                LOGGER.info(
                    "Dry-Run Retry ohne KI für Dokument %s (%s): %s",
                    doc_id,
                    title,
                    retry_payload,
                )
                skipped += 1
                _mark_completed(
                    document_id=int(doc_id) if doc_id is not None else None,
                    document_title=title,
                )
                continue
            try:
                client.update_document(int(doc_id), retry_payload)
                failed_patch_cache.pop(doc_key, None)
                failed_docs_until.pop(doc_key, None)
                LOGGER.info("Aktualisiert Dokument %s (%s) via Retry ohne KI", doc_id, title)
                updated += 1
                _mark_completed(
                    document_id=int(doc_id) if doc_id is not None else None,
                    document_title=title,
                )
                continue
            except PaperlessApiError as retry_exc:
                failed += 1
                retry_after_ts = (
                    dt.datetime.now(dt.timezone.utc).timestamp()
                    + max(failed_docs_cooldown_seconds, failed_tags_only_cooldown_seconds)
                )
                failed_docs_until[doc_key] = retry_after_ts
                retry_after_text = dt.datetime.fromtimestamp(
                    retry_after_ts, tz=dt.timezone.utc
                ).isoformat()
                LOGGER.warning(
                    "Retry ohne KI fehlgeschlagen für Dokument %s (%s), Quarantäne bis %s (UTC): %s",
                    doc_id,
                    title,
                    retry_after_text,
                    retry_exc,
                )
                error_details.append(
                    {
                        "id": doc_id,
                        "title": title,
                        "error_type": type(retry_exc).__name__,
                        "message": f"Retry ohne KI fehlgeschlagen: {retry_exc}",
                        "patch_payload": retry_payload,
                    }
                )
                _mark_completed(
                    document_id=int(doc_id) if doc_id is not None else None,
                    document_title=title,
                )
                continue

        if config.quarantine_failed_documents and doc_key is not None:
            retry_after_ts = float(failed_docs_until.get(doc_key, 0.0) or 0.0)
            now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
            if retry_after_ts > now_ts:
                if not config.dry_run and doc_id is not None:
                    current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                    desired_tags = set(current_tags)
                    if remove_neu_tag_id is not None:
                        desired_tags.discard(int(remove_neu_tag_id))
                    if error_tag_id is not None:
                        desired_tags.add(int(error_tag_id))
                    if desired_tags != current_tags:
                        try:
                            client.update_document(
                                int(doc_id),
                                {"tags": sorted(desired_tags)},
                            )
                            doc_tags = set(desired_tags)
                            LOGGER.info(
                                "Quarantäne-Dokument %s (%s) markiert: #NEU entfernt, KI_FEHLER gesetzt.",
                                doc_id,
                                title,
                            )
                        except PaperlessApiError as quarantine_mark_exc:
                            skipped_with_neu_still_set += 1
                            LOGGER.warning(
                                "Quarantäne-Dokument %s (%s) konnte nicht auf KI_FEHLER/#NEU aktualisiert werden: %s",
                                doc_id,
                                title,
                                quarantine_mark_exc,
                            )
                retry_after_text = dt.datetime.fromtimestamp(
                    retry_after_ts, tz=dt.timezone.utc
                ).isoformat()
                LOGGER.info(
                    "Skip Dokument %s (%s): Fehler-Quarantäne bis %s (UTC)",
                    doc_id,
                    title,
                    retry_after_text,
                )
                skipped += 1
                _mark_completed(
                    document_id=int(doc_id) if doc_id is not None else None,
                    document_title=title,
                )
                continue
            if doc_key in failed_docs_until:
                failed_docs_until.pop(doc_key, None)

        # Defensive Prüfung bleibt aktiv, falls API-Filter je nach Version anders reagiert.
        if not effective_process_all_documents and only_tag_id is not None and only_tag_id not in doc_tags:
            skipped += 1
            _mark_completed(
                document_id=int(doc_id) if doc_id is not None else None,
                document_title=title,
            )
            continue

        if (
            not effective_process_all_documents
            and only_tag_id is None
            and not should_process_document(document)
        ):
            LOGGER.debug("Skip Dokument %s (%s): bereits klassifiziert", doc_id, title)
            skipped += 1
            _mark_completed(
                document_id=int(doc_id) if doc_id is not None else None,
                document_title=title,
            )
            continue

        # ---------- Precheck-Gates vor KI ----------

        if (
            config.already_classified_skip
            and not effective_process_all_documents
            and not backfill_existing_documents
        ):
            has_type = document.get("document_type") is not None
            has_tags = bool(document.get("tags"))
            # Regel für already_classified_skip:
            # Nur skippen, wenn KI-Tag vorhanden UND bereits eine KI-Notiz mit
            # Kurz-Zusammenfassung existiert.
            has_ki_summary_note = False
            if has_type and has_tags and has_ki_tag and doc_id is not None:
                try:
                    has_ki_summary_note = client.has_ki_summary_note(int(doc_id))
                except PaperlessApiError as notes_exc:
                    LOGGER.warning(
                        "Precheck already_classified_skip: Notizen für Dokument %s (%s) konnten nicht geprüft werden: %s",
                        doc_id,
                        title,
                        notes_exc,
                    )

            if has_type and has_tags and has_ki_tag and has_ki_summary_note:
                if not config.dry_run and doc_id is not None:
                    current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                    new_tags = set(current_tags)
                    if remove_neu_tag_id is not None:
                        new_tags.discard(int(remove_neu_tag_id))
                    if new_tags != current_tags:
                        try:
                            client._request(
                                "PATCH",
                                f"/api/documents/{int(doc_id)}/",
                                payload={"tags": sorted(new_tags)},
                                retries=1,
                            )
                        except PaperlessApiError as remove_neu_exc:
                            skipped_with_neu_still_set += 1
                            LOGGER.warning(
                                "Precheck already_classified_skip: #NEU konnte für Dokument %s (%s) nicht entfernt werden: %s",
                                doc_id,
                                title,
                                remove_neu_exc,
                            )
                LOGGER.info(
                    "Skip Dokument %s (%s): Precheck already_classified_skip (KI-Tag + Kurz-Zusammenfassung vorhanden)",
                    doc_id,
                    title,
                )
                skipped += 1
                _mark_completed(
                    document_id=int(doc_id) if doc_id is not None else None,
                    document_title=title,
                )
                continue

        names_lower = [name.lower() for name in collect_document_names(document)]
        matched_pattern = None
        if not backfill_existing_documents:
            for pattern in config.precheck_blocked_filename_patterns:
                pattern_lower = str(pattern).strip().lower()
                if not pattern_lower:
                    continue
                if any(pattern_lower in name for name in names_lower):
                    matched_pattern = pattern
                    break
        if matched_pattern:
            if not config.dry_run and doc_id is not None:
                current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                new_tags = set(current_tags)
                if remove_neu_tag_id is not None:
                    new_tags.discard(int(remove_neu_tag_id))
                if skip_precheck_tag_id is not None:
                    new_tags.add(int(skip_precheck_tag_id))
                if new_tags != current_tags:
                    try:
                        client._request(
                            "PATCH",
                            f"/api/documents/{int(doc_id)}/",
                            payload={"tags": sorted(new_tags)},
                            retries=1,
                        )
                    except PaperlessApiError as precheck_tag_exc:
                        LOGGER.error(
                            "KI_SKIP_PRECHECK-Tag konnte für Dokument %s (%s) nicht gesetzt werden: %s",
                            doc_id,
                            title,
                            precheck_tag_exc,
                        )
                try:
                    client.add_document_note(
                        int(doc_id),
                        build_precheck_skip_note_entry(
                            reason="mime_filename_gate",
                            details=f"Dateiname-Muster getroffen: {matched_pattern}",
                        ),
                    )
                except PaperlessApiError as precheck_note_exc:
                    LOGGER.error(
                        "Precheck-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                        doc_id,
                        title,
                        precheck_note_exc,
                    )
            LOGGER.info(
                "Skip Dokument %s (%s): Precheck mime_filename_gate (%s)",
                doc_id,
                title,
                matched_pattern,
            )
            skipped += 1
            _mark_completed(
                document_id=int(doc_id) if doc_id is not None else None,
                document_title=title,
            )
            continue

        doc_text = collect_document_text(document)
        word_count = len(re.findall(r"\b\w+\b", doc_text))
        alnum_ratio = calc_alnum_ratio(doc_text)
        precheck_reasons: List[str] = []
        if not backfill_existing_documents:
            if len(doc_text) < max(0, int(config.precheck_min_content_chars)):
                precheck_reasons.append(
                    f"content_len={len(doc_text)} < min_content_chars={int(config.precheck_min_content_chars)}"
                )
            if word_count < max(0, int(config.precheck_min_word_count)):
                precheck_reasons.append(
                    f"word_count={word_count} < min_word_count={int(config.precheck_min_word_count)}"
                )
            if alnum_ratio < max(0.0, float(config.precheck_min_alnum_ratio)):
                precheck_reasons.append(
                    f"alnum_ratio={alnum_ratio:.2f} < min_alnum_ratio={float(config.precheck_min_alnum_ratio):.2f}"
                )
        if precheck_reasons:
            if not config.dry_run and doc_id is not None:
                current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                new_tags = set(current_tags)
                if remove_neu_tag_id is not None:
                    new_tags.discard(int(remove_neu_tag_id))
                if skip_precheck_tag_id is not None:
                    new_tags.add(int(skip_precheck_tag_id))
                if new_tags != current_tags:
                    try:
                        client._request(
                            "PATCH",
                            f"/api/documents/{int(doc_id)}/",
                            payload={"tags": sorted(new_tags)},
                            retries=1,
                        )
                    except PaperlessApiError as precheck_tag_exc:
                        LOGGER.error(
                            "KI_SKIP_PRECHECK-Tag konnte für Dokument %s (%s) nicht gesetzt werden: %s",
                            doc_id,
                            title,
                            precheck_tag_exc,
                        )
                try:
                    client.add_document_note(
                        int(doc_id),
                        build_precheck_skip_note_entry(
                            reason="content_quality_gate",
                            details="; ".join(precheck_reasons),
                        ),
                    )
                except PaperlessApiError as precheck_note_exc:
                    LOGGER.error(
                        "Precheck-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                        doc_id,
                        title,
                        precheck_note_exc,
                    )
            LOGGER.info(
                "Skip Dokument %s (%s): Precheck content_quality_gate (%s)",
                doc_id,
                title,
                "; ".join(precheck_reasons),
            )
            skipped += 1
            _mark_completed(
                document_id=int(doc_id) if doc_id is not None else None,
                document_title=title,
            )
            continue

        if config.precheck_image_only_gate and not backfill_existing_documents:
            mime_type = str(document.get("mime_type") or document.get("media_type") or "").lower()
            has_image_or_pdf_type = ("pdf" in mime_type) or mime_type.startswith("image/")
            image_like_name = any(
                any(name.endswith(ext) for ext in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"))
                for name in names_lower
            )
            if (has_image_or_pdf_type or image_like_name) and len(doc_text.strip()) < 30:
                if not config.dry_run and doc_id is not None:
                    current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                    new_tags = set(current_tags)
                    if remove_neu_tag_id is not None:
                        new_tags.discard(int(remove_neu_tag_id))
                    if skip_precheck_tag_id is not None:
                        new_tags.add(int(skip_precheck_tag_id))
                    if new_tags != current_tags:
                        try:
                            client._request(
                                "PATCH",
                                f"/api/documents/{int(doc_id)}/",
                                payload={"tags": sorted(new_tags)},
                                retries=1,
                            )
                        except PaperlessApiError as precheck_tag_exc:
                            LOGGER.error(
                                "KI_SKIP_PRECHECK-Tag konnte für Dokument %s (%s) nicht gesetzt werden: %s",
                                doc_id,
                                title,
                                precheck_tag_exc,
                            )
                    try:
                        client.add_document_note(
                            int(doc_id),
                            build_precheck_skip_note_entry(
                                reason="image_only_gate",
                                details="Kein verwertbarer Text im OCR-Inhalt erkannt.",
                            ),
                        )
                    except PaperlessApiError as precheck_note_exc:
                        LOGGER.error(
                            "Precheck-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                            doc_id,
                            title,
                            precheck_note_exc,
                        )
                LOGGER.info(
                    "Skip Dokument %s (%s): Precheck image_only_gate (kein verwertbarer Text)",
                    doc_id,
                    title,
                )
                skipped += 1
                _mark_completed(
                    document_id=int(doc_id) if doc_id is not None else None,
                    document_title=title,
                )
                continue

        if config.precheck_duplicate_hash_gate and doc_id is not None and not backfill_existing_documents:
            checksum = str(document.get("checksum") or "").strip()
            if checksum:
                duplicate_doc = client.find_classified_duplicate(
                    current_document_id=int(doc_id),
                    checksum=checksum,
                )
                if duplicate_doc is not None:
                    duplicate_id = int(duplicate_doc.get("id"))
                    if config.precheck_duplicate_apply_metadata and not config.dry_run:
                        duplicate_patch: Dict[str, Any] = {}
                        for field in ("document_type", "correspondent", "storage_path", "created"):
                            value = duplicate_doc.get(field)
                            if value is not None:
                                duplicate_patch[field] = value
                        duplicate_tags = duplicate_doc.get("tags") or []
                        if duplicate_tags:
                            duplicate_patch["tags"] = [int(tag_id) for tag_id in duplicate_tags]
                        apply_forced_tag_rules(
                            patch_payload=duplicate_patch,
                            current_tag_ids=doc_tags,
                            ki_tag_id=ki_tag_id,
                            remove_neu_tag_id=remove_neu_tag_id,
                        )
                        if skip_precheck_tag_id is not None:
                            merged_tags = set(int(tag_id) for tag_id in duplicate_patch.get("tags", []))
                            merged_tags.add(int(skip_precheck_tag_id))
                            duplicate_patch["tags"] = sorted(merged_tags)
                        duplicate_patch = filter_unchanged_patch_fields(
                            document=document,
                            patch_payload=duplicate_patch,
                        )
                        if duplicate_patch:
                            client.update_document(int(doc_id), duplicate_patch)
                        try:
                            client.add_document_note(
                                int(doc_id),
                                build_precheck_skip_note_entry(
                                    reason="duplicate_hash_gate",
                                    details=(
                                        f"Dublette erkannt via checksum. Metadaten von Dokument "
                                        f"{duplicate_id} übernommen."
                                    ),
                                ),
                            )
                        except PaperlessApiError as precheck_note_exc:
                            LOGGER.error(
                                "Precheck-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                                doc_id,
                                title,
                                precheck_note_exc,
                            )
                        LOGGER.info(
                            "Aktualisiert Dokument %s (%s) via Precheck duplicate_hash_gate (Quelle=%s)",
                            doc_id,
                            title,
                            duplicate_id,
                        )
                        updated += 1
                        _mark_completed(
                            document_id=int(doc_id) if doc_id is not None else None,
                            document_title=title,
                        )
                        continue

                    if not config.dry_run and doc_id is not None:
                        current_tags = {int(tag_id) for tag_id in document.get("tags", [])}
                        new_tags = set(current_tags)
                        if remove_neu_tag_id is not None:
                            new_tags.discard(int(remove_neu_tag_id))
                        if skip_precheck_tag_id is not None:
                            new_tags.add(int(skip_precheck_tag_id))
                        if new_tags != current_tags:
                            try:
                                client._request(
                                    "PATCH",
                                    f"/api/documents/{int(doc_id)}/",
                                    payload={"tags": sorted(new_tags)},
                                    retries=1,
                                )
                            except PaperlessApiError as precheck_tag_exc:
                                LOGGER.error(
                                    "KI_SKIP_PRECHECK-Tag konnte für Dokument %s (%s) nicht gesetzt werden: %s",
                                    doc_id,
                                    title,
                                    precheck_tag_exc,
                                )
                        try:
                            client.add_document_note(
                                int(doc_id),
                                build_precheck_skip_note_entry(
                                    reason="duplicate_hash_gate",
                                    details=f"Dublette erkannt via checksum (Referenzdokument {duplicate_id}).",
                                ),
                            )
                        except PaperlessApiError as precheck_note_exc:
                            LOGGER.error(
                                "Precheck-Notiz konnte für Dokument %s (%s) nicht gespeichert werden: %s",
                                doc_id,
                                title,
                                precheck_note_exc,
                            )
                    LOGGER.info(
                        "Skip Dokument %s (%s): Precheck duplicate_hash_gate (Quelle=%s)",
                        doc_id,
                        title,
                        duplicate_id,
                    )
                    skipped += 1
                    _mark_completed(
                        document_id=int(doc_id) if doc_id is not None else None,
                        document_title=title,
                    )
                    continue

        if config.quarantine_failed_documents and doc_key is not None:
            # Bei neuem Versuch zunächst entsperren; bei erneutem Fehler wird der
            # Eintrag bei Fehlerbehandlung wieder gesetzt.
            failed_docs_until.pop(doc_key, None)

        if (
            has_ki_tag
            and not backfill_existing_documents
            and not config.reprocess_ki_tagged_documents
            and config.enable_tax_enrichment
            and config.tax_process_ki_tagged_documents
        ):
            if target_documents is not None and budget_used >= target_documents:
                break
            budget_used += 1
            try:
                _run_tax_only_for_document(document=document, doc_id=doc_id, title=title)
            except AiTemporaryPauseError as pause_exc:
                _pause_run(
                    pause_reason=pause_exc.pause_reason,
                    retry_after_seconds=pause_exc.retry_after_seconds,
                    current_document_id=int(doc_id) if doc_id is not None else None,
                    current_document_title=title,
                )
            _mark_completed(
                document_id=int(doc_id) if doc_id is not None else None,
                document_title=title,
            )
            continue

        # Budget zählt nur für Dokumente, die die Skip-Gates passiert haben.
        if target_documents is not None and budget_used >= target_documents:
            break
        budget_used += 1

        pending_ai_documents.append(
            PendingAiDocument(
                document=document,
                doc_id=int(doc_id) if doc_id is not None else None,
                doc_key=doc_key,
                title=title,
                doc_tags=set(doc_tags),
                enrichment_only=bool(backfill_existing_documents and has_ki_tag),
            )
        )
        if len(pending_ai_documents) >= parallel_ai_workers:
            _flush_pending_batch(list(pending_ai_documents))
            pending_ai_documents.clear()

    if pending_ai_documents:
        _flush_pending_batch(list(pending_ai_documents))
        pending_ai_documents.clear()

    if prefilt_ki_tagged > 0:
        LOGGER.info(
            "Vorfilter aktiv: %s Dokument(e) mit KI-Tag wurden vollständig ausgeschlossen "
            "(reprocess_ki_tagged_documents=false).",
            prefilt_ki_tagged,
        )
    if prefilt_secondbrain_ready > 0:
        LOGGER.info(
            "Backfill-Vorfilter aktiv: %s Dokument(e) mit bereits befüllten "
            "SecondBrain-Feldern wurden ohne neuen KI-Aufruf ausgeschlossen.",
            prefilt_secondbrain_ready,
        )
    if perf_ai_batches > 0:
        LOGGER.info(
            "Performance: KI-Batches=%s | KI-Dokumente=%s | KI-Zeit=%.2fs | "
            "Ø KI-Zeit/Dokument=%.3fs | Apply-Zeit=%.2fs",
            perf_ai_batches,
            perf_ai_docs,
            perf_ai_seconds,
            (perf_ai_seconds / perf_ai_docs) if perf_ai_docs else 0.0,
            perf_apply_seconds,
        )

    if config.quarantine_failed_documents:
        save_failed_documents(failed_docs_path, failed_docs_until)
        save_failed_patch_cache(failed_patch_cache_path, failed_patch_cache)
    if config.enable_tag_bypass_on_tags_500:
        save_tag_bypass_documents(tag_bypass_path, tag_bypass_docs)
    if tax_export_collector is not None:
        exported_paths = tax_export_collector.write_exports(Path(config.tax_export_dir))
        if exported_paths:
            LOGGER.info(
                "Tax Exporte geschrieben: %s",
                ", ".join(str(path) for path in exported_paths),
            )
        else:
            LOGGER.info(
                "Tax Enrichment aktiv, aber keine Exportdateien geschrieben "
                "(keine Dokumente fuer die konfigurierten Steuerjahre gefunden)."
            )
        if tax_enrichment_errors > 0:
            LOGGER.warning(
                "Tax Enrichment: %s Dokument(e) konnten nicht angereichert werden.",
                tax_enrichment_errors,
            )

    log_run_details(created_entities=created_entities, error_details=error_details)
    metrics_path = Path(config.metrics_file)
    existing_metrics = load_metrics(metrics_path)
    totals = existing_metrics.get("totals") or {}
    new_totals_prompt = int(totals.get("prompt_tokens", 0) or 0) + run_prompt_tokens
    new_totals_completion = int(totals.get("completion_tokens", 0) or 0) + run_completion_tokens
    new_totals_tokens = int(totals.get("total_tokens", 0) or 0) + run_total_tokens
    new_totals_cost = float(totals.get("cost_eur", 0.0) or 0.0) + run_cost_eur
    new_totals_bypass_skipped = int(totals.get("bypass_skipped", 0) or 0) + bypass_skipped
    new_totals_runs = int(totals.get("runs", 0) or 0) + 1

    metrics_payload = {
        "last_run": {
            "prompt_tokens": run_prompt_tokens,
            "completion_tokens": run_completion_tokens,
            "total_tokens": run_total_tokens,
            "cost_eur": round(run_cost_eur, 6),
            "bypass_skipped": bypass_skipped,
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "model": config.ai_model,
        },
        "totals": {
            "prompt_tokens": new_totals_prompt,
            "completion_tokens": new_totals_completion,
            "total_tokens": new_totals_tokens,
            "cost_eur": round(new_totals_cost, 6),
            "bypass_skipped": new_totals_bypass_skipped,
            "runs": new_totals_runs,
        },
    }
    save_metrics(metrics_path, metrics_payload)
    LOGGER.info(
        "Kosten/Token: Letzter Lauf=%s Tokens, %.6f EUR | Gesamt=%s Tokens, %.6f EUR",
        run_total_tokens,
        run_cost_eur,
        new_totals_tokens,
        new_totals_cost,
    )
    LOGGER.info(
        "Fertig. Gescannt=%s, Aktualisiert=%s, Übersprungen=%s, Fehler=%s, Bypass=%s, BypassSkip=%s",
        scanned,
        updated,
        skipped,
        failed,
        bypassed,
        bypass_skipped,
    )
    if skipped_with_neu_still_set > 0:
        LOGGER.warning(
            "Loop-Hinweis: Bei %s übersprungenen Dokument(en) konnte #NEU nicht entfernt werden "
            "(API-Fehler bei Tag-Update). Diese Dokumente triggern externe #NEU-Automatiken weiter.",
            skipped_with_neu_still_set,
        )
    emit_runtime_event(
        "progress",
        **_progress_payload(status="success"),
    )
    delete_runtime_file(run_state_path)
    delete_runtime_file(stop_request_path)


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
    parser.add_argument(
        "--backfill-existing-documents",
        action="store_true",
        help=(
            "Bestehende Paperless-Datenbank erneut für neue Zusatzfunktionen "
            "durchlaufen. Bereits KI-getaggte Dokumente werden dabei nur "
            "anreichernd aktualisiert."
        ),
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=None,
        help=(
            "Optionaler Override für max_documents aus der YAML (nur > 0 wirksam). "
            "Im Backfill-Modus eignet sich das zum Aufteilen in Chargen."
        ),
    )
    parser.add_argument(
        "--run-state-file",
        default=RUN_STATE_FILE_DEFAULT,
        help=(
            "Pfad zur Resume-State-Datei. Relative Pfade werden zum aktuellen "
            "Arbeitsverzeichnis aufgelöst."
        ),
    )
    parser.add_argument(
        "--stop-request-file",
        default=STOP_REQUEST_FILE_DEFAULT,
        help=(
            "Pfad zur Stop-Anfrage-Datei. Wenn die Datei existiert, pausiert der "
            "Lauf am nächsten sicheren Punkt."
        ),
    )
    parser.add_argument(
        "--resume-run",
        action="store_true",
        help="Setzt einen pausierten Lauf anhand der Run-State-Datei fort.",
    )
    parser.add_argument(
        "--request-stop",
        action="store_true",
        help="Legt nur eine Stop-Anfrage-Datei an und beendet sich sofort.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.request_stop:
        stop_path = resolve_runtime_path(args.stop_request_file, Path.cwd())
        request_manual_stop(stop_path)
        print(f"[STOP-REQUESTED] {stop_path}")
        return 0

    try:
        config = load_config(args.config, args.dry_run, args.max_documents)
    except ConfigError as exc:
        print(f"[CONFIG-ERROR] {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    LOGGER.info("Starte Paperless KI Sorter | dry_run=%s", config.dry_run)

    try:
        process_documents(
            config,
            process_all_documents=args.all_documents,
            backfill_existing_documents=args.backfill_existing_documents,
            document_limit_override=args.max_documents,
            run_state_file=args.run_state_file,
            stop_request_file=args.stop_request_file,
            resume_run=args.resume_run,
        )
    except RunPausedError as exc:
        LOGGER.warning(
            "Lauf kontrolliert pausiert | reason=%s | retry_after_seconds=%s",
            exc.pause_reason,
            exc.retry_after_seconds,
        )
        return RUN_PAUSE_EXIT_CODE
    except Exception as exc:  # Breiter Catch für sauberen Exit + logischen Fehlercode.
        LOGGER.exception("Unerwarteter Fehler im Hauptablauf: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
