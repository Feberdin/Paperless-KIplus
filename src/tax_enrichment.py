"""Tax enrichment for private German income tax preparation.

Purpose:
- Build a versioned tax object per document without disturbing the existing
  Paperless classification workflow.
- Keep tax logic explainable: taxonomy, WISO-oriented mapping, evidence checks,
  review flags, and exports are implemented as explicit Python rules.

Input / Output:
- Input: one Paperless document dict plus the existing standard AI
  classification result.
- Output: a `TaxEnrichment` object and year-based JSON/CSV exports.

Important invariants:
- Tax decisions are suggestions, never final legal rulings.
- WISO integration is semantic only; no proprietary WISO file format is used.
- Validation marks missing evidence and review needs instead of hard-rejecting
  tax relevance.

How to debug:
- Inspect `reasoning_summary`, `flags`, `missing_requirements`, and
  `recommended_follow_up` in the exported JSON.
- If AI extraction fails, the caller should log the exception and continue the
  normal document workflow unchanged.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


LOGGER = logging.getLogger("paperless_ai_sorter")

TAX_ENRICHMENT_SCHEMA_VERSION = "tax_enrichment.v1"

VALID_FORMALITY = {"valid", "incomplete", "invalid", "unknown"}
VALID_FLAGS = {
    "needs_review",
    "needs_payment_proof",
    "needs_person_assignment",
    "needs_year_assignment",
    "high_audit_relevance",
    "possible_finanzamt_query",
    "not_tax_relevant",
    "mixed_private_and_tax_relevant",
    "missing_labor_split",
    "cash_payment_not_eligible",
}
VALID_PAYMENT_METHODS = {
    "unknown",
    "bank_transfer",
    "sepa_direct_debit",
    "card",
    "cash",
    "paypal",
    "invoice_open",
    "other",
}
VALID_EVIDENCE_TYPES = {
    "rechnung",
    "vertrag",
    "zahlungsbeleg",
    "information",
    "belegsammlung",
    "unknown",
}
TAXONOMY: Dict[str, List[str]] = {
    "werbungskosten": ["arbeitsmittel", "homeoffice", "weiterbildung", "fahrtkosten"],
    "sonderausgaben": [],
    "aussergewoehnliche_belastungen": ["medikamente", "apotheke"],
    "kinderbetreuungskosten": ["kita", "tagesmutter", "babysitter"],
    "haushaltsnahe_dienstleistungen": ["reinigung", "gartenpflege", "winterdienst"],
    "handwerkerleistungen": ["handwerker_lohnkosten"],
    "unterhalt": ["unterhalt_ex_partner", "unterhalt_volljaehriges_kind"],
    "pflege": ["pflegedienst", "pflegeheim"],
    "kapitalvermoegen": [],
    "vermietung": [],
    "selbststaendigkeit": [],
    "nicht_steuerrelevant": [],
    "unklar": [],
}
DEFAULT_REQUIRED_SUPPORTING_DOCS = [
    "Rechnung oder gleichwertiger Beleg",
    "Zahlungsnachweis bei steuerlich sensiblen Leistungen",
]
WISO_MAPPING_RULES: Dict[tuple[str, Optional[str]], Dict[str, Any]] = {
    ("werbungskosten", "arbeitsmittel"): {
        "wiso_target_area": "Arbeitnehmer > Arbeitsmittel",
        "expected_user_input": ["Berufliche Nutzung plausibilisieren", "Kostenbetrag prüfen"],
        "required_supporting_docs": ["Rechnung", "Zahlungsnachweis bei Rueckfragen"],
        "likely_manual_review": False,
    },
    ("werbungskosten", "homeoffice"): {
        "wiso_target_area": "Arbeitnehmer > Homeoffice / Arbeitszimmer",
        "expected_user_input": ["Anzahl Homeoffice-Tage oder Zimmerbezug prüfen"],
        "required_supporting_docs": ["Beleg, falls konkrete Arbeitsmittel enthalten sind"],
        "likely_manual_review": True,
    },
    ("werbungskosten", "weiterbildung"): {
        "wiso_target_area": "Arbeitnehmer > Fortbildungskosten",
        "expected_user_input": ["Beruflichen Zusammenhang eintragen"],
        "required_supporting_docs": ["Rechnung", "Zahlungsnachweis"],
        "likely_manual_review": False,
    },
    ("werbungskosten", "fahrtkosten"): {
        "wiso_target_area": "Arbeitnehmer > Fahrtkosten",
        "expected_user_input": ["Anlass und Strecke prüfen"],
        "required_supporting_docs": ["Beleg oder nachvollziehbarer Nachweis"],
        "likely_manual_review": True,
    },
    ("kinderbetreuungskosten", "kita"): {
        "wiso_target_area": "Familie/Kinder > Kinderbetreuungskosten",
        "expected_user_input": ["Kind zuordnen", "Betreuungsmonate prüfen"],
        "required_supporting_docs": ["Rechnung/Beitragsbescheid", "Unbarer Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("kinderbetreuungskosten", "tagesmutter"): {
        "wiso_target_area": "Familie/Kinder > Kinderbetreuungskosten",
        "expected_user_input": ["Kind zuordnen", "Betreuungszeitraum prüfen"],
        "required_supporting_docs": ["Vertrag/Rechnung", "Unbarer Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("kinderbetreuungskosten", "babysitter"): {
        "wiso_target_area": "Familie/Kinder > Kinderbetreuungskosten",
        "expected_user_input": ["Kind zuordnen", "Betreuungszeitraum prüfen"],
        "required_supporting_docs": ["Rechnung/Vereinbarung", "Unbarer Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("haushaltsnahe_dienstleistungen", "reinigung"): {
        "wiso_target_area": "Steuerermaessigungen > Haushaltsnahe Dienstleistungen",
        "expected_user_input": ["Haushaltsbezug prüfen"],
        "required_supporting_docs": ["Rechnung", "Unbarer Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("haushaltsnahe_dienstleistungen", "gartenpflege"): {
        "wiso_target_area": "Steuerermaessigungen > Haushaltsnahe Dienstleistungen",
        "expected_user_input": ["Leistungsort und Haushalt prüfen"],
        "required_supporting_docs": ["Rechnung", "Unbarer Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("haushaltsnahe_dienstleistungen", "winterdienst"): {
        "wiso_target_area": "Steuerermaessigungen > Haushaltsnahe Dienstleistungen",
        "expected_user_input": ["Leistungsort und Haushalt prüfen"],
        "required_supporting_docs": ["Rechnung", "Unbarer Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("handwerkerleistungen", "handwerker_lohnkosten"): {
        "wiso_target_area": "Steuerermaessigungen > Handwerkerleistungen",
        "expected_user_input": ["Nur Arbeitskosten ansetzen", "Haushaltsbezug prüfen"],
        "required_supporting_docs": ["Rechnung", "Lohn-/Materialtrennung", "Unbarer Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("aussergewoehnliche_belastungen", "medikamente"): {
        "wiso_target_area": "Aussergewoehnliche Belastungen > Krankheitskosten",
        "expected_user_input": ["Medizinischen Bezug prüfen"],
        "required_supporting_docs": ["Apothekenbeleg/Rechnung"],
        "likely_manual_review": False,
    },
    ("aussergewoehnliche_belastungen", "apotheke"): {
        "wiso_target_area": "Aussergewoehnliche Belastungen > Krankheitskosten",
        "expected_user_input": ["Medizinischen Bezug prüfen"],
        "required_supporting_docs": ["Apothekenbeleg/Rechnung"],
        "likely_manual_review": False,
    },
    ("pflege", "pflegedienst"): {
        "wiso_target_area": "Aussergewoehnliche Belastungen > Pflegekosten",
        "expected_user_input": ["Betroffene Person zuordnen"],
        "required_supporting_docs": ["Rechnung", "Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("pflege", "pflegeheim"): {
        "wiso_target_area": "Aussergewoehnliche Belastungen > Pflegekosten",
        "expected_user_input": ["Betroffene Person zuordnen"],
        "required_supporting_docs": ["Rechnung/Heimabrechnung", "Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("unterhalt", "unterhalt_ex_partner"): {
        "wiso_target_area": "Aussergewoehnliche Belastungen > Unterhalt an beduerftige Personen",
        "expected_user_input": ["Empfaengerperson prüfen"],
        "required_supporting_docs": ["Vereinbarung/Beschluss", "Zahlungsnachweise"],
        "likely_manual_review": True,
    },
    ("unterhalt", "unterhalt_volljaehriges_kind"): {
        "wiso_target_area": "Aussergewoehnliche Belastungen > Unterhalt an beduerftige Personen",
        "expected_user_input": ["Kind zuordnen", "Anspruchsvoraussetzungen prüfen"],
        "required_supporting_docs": ["Vereinbarung/Beschluss", "Zahlungsnachweise"],
        "likely_manual_review": True,
    },
    ("kapitalvermoegen", None): {
        "wiso_target_area": "Kapitalertraege",
        "expected_user_input": ["Steuerbescheinigung und Freistellungsdaten prüfen"],
        "required_supporting_docs": ["Steuerbescheinigung", "Ertragsaufstellung"],
        "likely_manual_review": True,
    },
    ("vermietung", None): {
        "wiso_target_area": "Vermietung und Verpachtung",
        "expected_user_input": ["Objektbezug prüfen"],
        "required_supporting_docs": ["Rechnung", "ggf. Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("selbststaendigkeit", None): {
        "wiso_target_area": "Selbststaendigkeit / Betriebsausgaben",
        "expected_user_input": ["Betrieblichen Zusammenhang prüfen"],
        "required_supporting_docs": ["Rechnung", "ggf. Zahlungsnachweis"],
        "likely_manual_review": True,
    },
    ("sonderausgaben", None): {
        "wiso_target_area": "Sonderausgaben",
        "expected_user_input": ["Steuerliche Zuordnung prüfen"],
        "required_supporting_docs": ["Beleg/Rechnung"],
        "likely_manual_review": True,
    },
    ("nicht_steuerrelevant", None): {
        "wiso_target_area": "Kein WISO-Zielbereich",
        "expected_user_input": [],
        "required_supporting_docs": [],
        "likely_manual_review": False,
    },
    ("unklar", None): {
        "wiso_target_area": "Manuelle Zuordnung erforderlich",
        "expected_user_input": ["Kategorie manuell festlegen"],
        "required_supporting_docs": DEFAULT_REQUIRED_SUPPORTING_DOCS,
        "likely_manual_review": True,
    },
}


class TaxEnrichmentError(Exception):
    """Raised when tax enrichment extraction fails."""


def normalize_iso_date(value: Any) -> Optional[str]:
    """Normalizes dates to YYYY-MM-DD or returns None."""

    if value in (None, ""):
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if "T" in candidate:
        candidate = candidate.split("T", 1)[0]
    if " " in candidate:
        candidate = candidate.split(" ", 1)[0]
    try:
        return dt.date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def normalize_year(value: Any) -> Optional[int]:
    """Parses a four-digit tax year if possible."""

    if value in (None, ""):
        return None
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 1900 <= year <= 2100:
        return year
    return None


def normalize_amount(value: Any) -> Optional[float]:
    """Parses monetary values from simple strings or numbers."""

    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("EUR", "").replace("€", "").replace(" ", "")
    if text.count(",") == 1 and text.count(".") > 1:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    elif text.count(",") > 1 and text.count(".") == 0:
        text = text.replace(".", "").replace(",", "")
    else:
        text = text.replace(",", "")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def normalize_confidence(value: Any, default: float = 0.0) -> float:
    """Clamps confidence values into the 0-1 range."""

    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return max(0.0, min(1.0, confidence))


def normalize_bool(value: Any) -> Optional[bool]:
    """Parses tri-state booleans."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "ja", "on"}:
            return True
        if normalized in {"0", "false", "no", "nein", "off"}:
            return False
    return None


def normalize_string_list(value: Any) -> List[str]:
    """Returns a clean list of unique non-empty strings."""

    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    normalized: List[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item).strip()
        if not text:
            continue
        if text not in seen:
            normalized.append(text)
            seen.add(text)
    return normalized


def normalize_tax_category(value: Any) -> str:
    """Normalizes the main tax category against the fixed taxonomy."""

    candidate = str(value or "").strip().lower()
    return candidate if candidate in TAXONOMY else "unklar"


def normalize_tax_subcategory(category: str, value: Any) -> Optional[str]:
    """Normalizes subcategory or drops invalid values."""

    if value in (None, ""):
        return None
    candidate = str(value).strip().lower()
    allowed = TAXONOMY.get(category, [])
    if candidate in allowed:
        return candidate
    return None


def normalize_flags(flags: Iterable[Any]) -> List[str]:
    """Keeps only supported review flags."""

    normalized = []
    seen: set[str] = set()
    for flag in flags:
        candidate = str(flag).strip()
        if candidate in VALID_FLAGS and candidate not in seen:
            normalized.append(candidate)
            seen.add(candidate)
    return normalized


def looks_like_cash_payment(text: str) -> bool:
    """Heuristic to detect cash payment hints in OCR text."""

    text_lower = text.lower()
    return any(
        pattern in text_lower
        for pattern in ("bar bezahlt", "barzahlung", "cash", "bar erhalten", "in bar")
    )


def extract_household_context(basis_config: Dict[str, Any]) -> Dict[str, Any]:
    """Extracts taxpayer and family context for prompts and exports."""

    owner = (((basis_config.get("people") or {}).get("owner")) or {})
    household = (((basis_config.get("people") or {}).get("household")) or {})
    return {
        "taxpayer_name": str(owner.get("full_name") or "").strip(),
        "children": household.get("children") or [],
        "relatives": household.get("relatives") or [],
    }


def resolve_wiso_mapping(category: str, subcategory: Optional[str]) -> Dict[str, Any]:
    """Resolves the semantic WISO target area and review expectations."""

    rule = WISO_MAPPING_RULES.get((category, subcategory))
    if rule is None:
        rule = WISO_MAPPING_RULES.get((category, None), {})
    if not rule:
        rule = WISO_MAPPING_RULES[("unklar", None)]
    return {
        "wiso_target_area": str(rule.get("wiso_target_area") or "Manuelle Zuordnung erforderlich"),
        "expected_user_input": normalize_string_list(rule.get("expected_user_input")),
        "required_supporting_docs": normalize_string_list(rule.get("required_supporting_docs")),
        "likely_manual_review": bool(rule.get("likely_manual_review", True)),
    }


def build_tax_tag_labels(enrichment: "TaxEnrichment") -> List[str]:
    """Returns the Paperless tags that should reflect the tax result.

    Why this exists:
    - The sorting pipeline should not duplicate tag naming logic in multiple
      places.
    - Tests can validate the tax-tag contract independently from Paperless I/O.
    """

    if enrichment.tax_category == "nicht_steuerrelevant":
        return ["KI nicht Steuerrelevant"]
    if enrichment.tax_category not in {"unklar", "nicht_steuerrelevant"} and enrichment.tax_year is not None:
        return [f"KI Steuerrelevant {enrichment.tax_year}"]
    return []


@dataclass
class TaxEnrichment:
    """Versioned tax view for one Paperless document."""

    schema_version: str
    document_id: Optional[int]
    title: str
    tax_year: Optional[int]
    document_date: Optional[str]
    service_period_from: Optional[str]
    service_period_to: Optional[str]
    document_type: Optional[str]
    issuer: Optional[str]
    recipient: Optional[str]
    total_amount: Optional[float]
    currency: str
    payment_method: Optional[str]
    payment_verified: Optional[bool]
    evidence_type: str
    tax_category: str
    tax_subcategory: Optional[str]
    deduction_domain: Optional[str]
    wiso_target_area: str
    classification_confidence: float
    eligibility_confidence: float
    reasoning_summary: str
    flags: List[str] = field(default_factory=list)
    person_reference: Optional[str] = None
    child_reference: Optional[str] = None
    household_reference: Optional[str] = None
    extracted_evidence: Dict[str, Any] = field(default_factory=dict)
    missing_requirements: List[str] = field(default_factory=list)
    recommended_follow_up: List[str] = field(default_factory=list)
    formal_validity: str = "unknown"
    expected_user_input: List[str] = field(default_factory=list)
    required_supporting_docs: List[str] = field(default_factory=list)
    likely_manual_review: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the dataclass for JSON export."""

        return asdict(self)


class TaxEnrichmentProcessor:
    """Applies taxonomy normalization, evidence checks, and review flags."""

    def __init__(self, basis_config: Optional[Dict[str, Any]] = None) -> None:
        self.basis_config = basis_config or {}
        self.context = extract_household_context(self.basis_config)

    def build_tax_enrichment(
        self,
        *,
        document: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> TaxEnrichment:
        """Converts AI payload into a normalized, validated tax object."""

        title = str(document.get("title") or "<ohne Titel>")
        content = str(document.get("content") or "")
        document_date = normalize_iso_date(payload.get("document_date")) or normalize_iso_date(document.get("created"))
        service_period_from = normalize_iso_date(payload.get("service_period_from"))
        service_period_to = normalize_iso_date(payload.get("service_period_to"))
        tax_year = self._derive_tax_year(
            explicit_year=payload.get("tax_year"),
            document_date=document_date,
            service_period_from=service_period_from,
            service_period_to=service_period_to,
        )
        category = normalize_tax_category(payload.get("tax_category"))
        subcategory = normalize_tax_subcategory(category, payload.get("tax_subcategory"))
        payment_method = self._normalize_payment_method(payload.get("payment_method"), content)
        payment_verified = normalize_bool(payload.get("payment_verified"))
        evidence_type = self._normalize_evidence_type(payload.get("evidence_type"), payload.get("document_type"), title)
        extracted_evidence = self._normalize_extracted_evidence(
            payload.get("extracted_evidence"),
            evidence_type=evidence_type,
            payment_method=payment_method,
            payment_verified=payment_verified,
            document_text=content,
        )

        mapping = resolve_wiso_mapping(category, subcategory)
        enrichment = TaxEnrichment(
            schema_version=TAX_ENRICHMENT_SCHEMA_VERSION,
            document_id=self._coerce_int(document.get("id")),
            title=title,
            tax_year=tax_year,
            document_date=document_date,
            service_period_from=service_period_from,
            service_period_to=service_period_to,
            document_type=self._normalize_optional_text(payload.get("document_type")),
            issuer=self._normalize_optional_text(payload.get("issuer")),
            recipient=self._normalize_optional_text(payload.get("recipient")),
            total_amount=normalize_amount(payload.get("total_amount")),
            currency=self._normalize_currency(payload.get("currency")),
            payment_method=payment_method,
            payment_verified=payment_verified,
            evidence_type=evidence_type,
            tax_category=category,
            tax_subcategory=subcategory,
            deduction_domain=self._normalize_optional_text(payload.get("deduction_domain")),
            wiso_target_area=mapping["wiso_target_area"],
            classification_confidence=normalize_confidence(payload.get("classification_confidence"), 0.0),
            eligibility_confidence=normalize_confidence(payload.get("eligibility_confidence"), 0.0),
            reasoning_summary=self._build_reasoning_summary(payload.get("reasoning_summary"), category, subcategory, mapping["wiso_target_area"]),
            flags=normalize_flags(normalize_string_list(payload.get("flags"))),
            person_reference=self._normalize_optional_text(payload.get("person_reference")),
            child_reference=self._normalize_optional_text(payload.get("child_reference")),
            household_reference=self._normalize_optional_text(payload.get("household_reference")),
            extracted_evidence=extracted_evidence,
            expected_user_input=mapping["expected_user_input"],
            required_supporting_docs=mapping["required_supporting_docs"],
            likely_manual_review=bool(mapping["likely_manual_review"]),
        )
        self._apply_validation_rules(enrichment)
        return enrichment

    def _derive_tax_year(
        self,
        *,
        explicit_year: Any,
        document_date: Optional[str],
        service_period_from: Optional[str],
        service_period_to: Optional[str],
    ) -> Optional[int]:
        year = normalize_year(explicit_year)
        if year is not None:
            return year
        for candidate in (service_period_to, service_period_from, document_date):
            normalized = normalize_iso_date(candidate)
            if normalized is not None:
                return dt.date.fromisoformat(normalized).year
        return None

    def _normalize_payment_method(self, value: Any, document_text: str) -> Optional[str]:
        candidate = str(value or "").strip().lower()
        aliases = {
            "ueberweisung": "bank_transfer",
            "banktransfer": "bank_transfer",
            "bank_transfer": "bank_transfer",
            "lastschrift": "sepa_direct_debit",
            "sepa": "sepa_direct_debit",
            "karte": "card",
            "credit_card": "card",
            "debit_card": "card",
            "paypal": "paypal",
            "bar": "cash",
            "cash": "cash",
            "rechnung": "invoice_open",
        }
        if candidate in aliases:
            return aliases[candidate]
        if candidate in VALID_PAYMENT_METHODS:
            return candidate
        if looks_like_cash_payment(document_text):
            return "cash"
        return None

    def _normalize_evidence_type(self, value: Any, document_type: Any, title: str) -> str:
        candidates = [str(value or "").strip().lower(), str(document_type or "").strip().lower(), title.lower()]
        for candidate in candidates:
            if any(token in candidate for token in ("rechnung", "invoice")):
                return "rechnung"
            if any(token in candidate for token in ("vertrag", "agreement")):
                return "vertrag"
            if any(token in candidate for token in ("zahlung", "kontoauszug", "ueberweisung", "transaktion")):
                return "zahlungsbeleg"
            if any(token in candidate for token in ("info", "mitteilung", "hinweis")):
                return "information"
        return "unknown"

    def _normalize_extracted_evidence(
        self,
        raw: Any,
        *,
        evidence_type: str,
        payment_method: Optional[str],
        payment_verified: Optional[bool],
        document_text: str,
    ) -> Dict[str, Any]:
        extracted = raw if isinstance(raw, dict) else {}
        normalized = {
            "invoice_present": normalize_bool(extracted.get("invoice_present")),
            "issuer_identified": normalize_bool(extracted.get("issuer_identified")),
            "recipient_identified": normalize_bool(extracted.get("recipient_identified")),
            "service_description_present": normalize_bool(extracted.get("service_description_present")),
            "service_period_present": normalize_bool(extracted.get("service_period_present")),
            "amount_present": normalize_bool(extracted.get("amount_present")),
            "payment_method_identified": normalize_bool(extracted.get("payment_method_identified")),
            "unbare_payment_evidence": normalize_bool(extracted.get("unbare_payment_evidence")),
            "labor_material_split": normalize_bool(extracted.get("labor_material_split")),
        }
        if normalized["invoice_present"] is None and evidence_type == "rechnung":
            normalized["invoice_present"] = True
        if normalized["payment_method_identified"] is None and payment_method is not None:
            normalized["payment_method_identified"] = True
        if normalized["unbare_payment_evidence"] is None and payment_verified is True and payment_method not in {None, "cash"}:
            normalized["unbare_payment_evidence"] = True
        if normalized["unbare_payment_evidence"] is None and payment_method == "cash":
            normalized["unbare_payment_evidence"] = False
        if normalized["labor_material_split"] is None:
            normalized["labor_material_split"] = "material" in document_text.lower() and "lohn" in document_text.lower()
        return normalized

    def _apply_validation_rules(self, enrichment: TaxEnrichment) -> None:
        missing: List[str] = []
        follow_up: List[str] = []
        flags = set(enrichment.flags)
        evidence = enrichment.extracted_evidence
        sensitive_household = enrichment.tax_category in {"haushaltsnahe_dienstleistungen", "handwerkerleistungen"}

        if enrichment.tax_category == "nicht_steuerrelevant":
            flags.add("not_tax_relevant")

        if enrichment.tax_year is None:
            flags.add("needs_year_assignment")
            missing.append("Steuerjahr unklar")
            follow_up.append("Steuerjahr manuell festlegen")

        if enrichment.tax_category == "kinderbetreuungskosten" and not enrichment.child_reference:
            flags.add("needs_person_assignment")
            missing.append("Kind nicht zugeordnet")
            follow_up.append("Passendes Kind in der Steuerakte zuordnen")

        if enrichment.tax_category in {"pflege", "unterhalt"} and not enrichment.person_reference:
            flags.add("needs_person_assignment")
            missing.append("Personenbezug fehlt")
            follow_up.append("Betroffene Person oder empfangende Person zuordnen")

        if enrichment.tax_category not in {"nicht_steuerrelevant", "unklar"}:
            if enrichment.evidence_type not in {"rechnung", "vertrag", "zahlungsbeleg"}:
                missing.append("Belegart ist fuer steuerliche Wuerdigung zu unklar")
            if evidence.get("invoice_present") is False and enrichment.evidence_type == "information":
                missing.append("Keine Rechnung erkannt")
            if evidence.get("issuer_identified") is False or not enrichment.issuer:
                missing.append("Leistungserbringer nicht klar erkennbar")
            if evidence.get("recipient_identified") is False and not enrichment.recipient:
                missing.append("Rechnungsempfaenger nicht klar erkennbar")
            if evidence.get("service_description_present") is False:
                missing.append("Leistungsbeschreibung fehlt")
            if evidence.get("service_period_present") is False and enrichment.tax_category in {
                "kinderbetreuungskosten",
                "haushaltsnahe_dienstleistungen",
                "handwerkerleistungen",
            }:
                missing.append("Leistungszeitraum fehlt")
            if evidence.get("amount_present") is False or enrichment.total_amount is None:
                missing.append("Betrag fehlt")
            if evidence.get("payment_method_identified") is False and sensitive_household:
                missing.append("Zahlungsart nicht erkennbar")

        if sensitive_household:
            flags.add("high_audit_relevance")
            if enrichment.payment_method == "cash":
                flags.add("cash_payment_not_eligible")
                flags.add("possible_finanzamt_query")
                missing.append("Unbare Zahlung nicht nachgewiesen")
                follow_up.append("Unbaren Zahlungsnachweis beifuegen oder Fall manuell pruefen")
            elif enrichment.payment_verified is not True:
                flags.add("needs_payment_proof")
                missing.append("Zahlungsnachweis fehlt")
                follow_up.append("Kontoauszug oder anderen unbaren Zahlungsnachweis beifuegen")

        if enrichment.tax_category == "handwerkerleistungen":
            if evidence.get("labor_material_split") is not True:
                flags.add("missing_labor_split")
                flags.add("possible_finanzamt_query")
                missing.append("Lohn-/Materialtrennung fehlt")
                follow_up.append("Handwerkerrechnung mit getrennter Ausweisung von Lohn und Material pruefen")

        if enrichment.tax_category == "kinderbetreuungskosten" and enrichment.payment_verified is not True:
            flags.add("needs_payment_proof")
            missing.append("Unbarer Zahlungsnachweis fuer Kinderbetreuung fehlt")
            follow_up.append("Kontoauszug oder Lastschriftbeleg fuer Kinderbetreuung ergänzen")

        if enrichment.tax_category in {"pflege", "unterhalt", "kapitalvermoegen", "vermietung"}:
            flags.add("possible_finanzamt_query")
            flags.add("high_audit_relevance")

        if enrichment.tax_category == "unklar" or enrichment.likely_manual_review:
            flags.add("needs_review")

        if enrichment.classification_confidence < 0.70 or enrichment.eligibility_confidence < 0.70:
            flags.add("needs_review")

        if enrichment.tax_category == "nicht_steuerrelevant":
            enrichment.formal_validity = "unknown"
        elif missing:
            enrichment.formal_validity = "invalid" if len(missing) >= 4 else "incomplete"
        else:
            enrichment.formal_validity = "valid"

        enrichment.flags = normalize_flags(flags)
        enrichment.missing_requirements = normalize_string_list(missing)
        enrichment.recommended_follow_up = normalize_string_list(
            follow_up + self._derive_generic_follow_up(enrichment)
        )

    def _derive_generic_follow_up(self, enrichment: TaxEnrichment) -> List[str]:
        follow_up: List[str] = []
        if enrichment.tax_category == "unklar":
            follow_up.append("Kategorie in WISO manuell zuordnen")
        if enrichment.tax_category == "nicht_steuerrelevant":
            follow_up.append("Nur archivieren, keine Uebernahme nach WISO erforderlich")
        if enrichment.tax_year is not None and enrichment.document_date is not None:
            doc_year = dt.date.fromisoformat(enrichment.document_date).year
            if doc_year != enrichment.tax_year:
                follow_up.append("Steuerjahr gegen Dokumentdatum und Leistungszeitraum pruefen")
        return follow_up

    @staticmethod
    def _normalize_optional_text(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_currency(value: Any) -> str:
        candidate = str(value or "EUR").strip().upper()
        return candidate if candidate else "EUR"

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_reasoning_summary(value: Any, category: str, subcategory: Optional[str], target_area: str) -> str:
        text = str(value or "").strip()
        if text:
            return text
        if subcategory:
            return (
                f"Als {category}/{subcategory} eingeordnet. "
                f"Vorgeschlagener WISO-Zielbereich: {target_area}."
            )
        return f"Als {category} eingeordnet. Vorgeschlagener WISO-Zielbereich: {target_area}."


class TaxEnrichmentAiExtractor:
    """LLM-based extractor for tax-relevant document evidence."""

    def __init__(
        self,
        *,
        ai_model: str,
        ai_api_key: str,
        ai_base_url: str,
        request_timeout_seconds: int,
        basis_config: Optional[Dict[str, Any]] = None,
        personal_context: str = "",
    ) -> None:
        self.ai_model = ai_model
        self.ai_base_url = ai_base_url.rstrip("/")
        self.request_timeout_seconds = request_timeout_seconds
        self.basis_config = basis_config or {}
        self.personal_context = str(personal_context or "").strip()
        self.context = extract_household_context(self.basis_config)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {ai_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "paperless-kiplus-tax/1.0",
            }
        )

    def extract(
        self,
        *,
        document: Dict[str, Any],
        classification_prediction: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Calls an OpenAI-compatible endpoint for tax enrichment extraction."""

        prompt = (
            "Du extrahierst steuerrelevante Hinweise fuer private deutsche Einkommensteuerfaelle. "
            "Antworte ausschliesslich als JSON. Keine Rechtsentscheidung, nur vorsichtige Vorschlaege "
            "mit Begruendung und Confidence. Nutze nur diese tax_category-Werte: "
            + ", ".join(TAXONOMY.keys())
            + ". Nutze nur bekannte tax_subcategory-Werte aus dieser Taxonomie: "
            + json.dumps(TAXONOMY, ensure_ascii=False)
            + ". Verwende fuer evidence_type nur: "
            + ", ".join(sorted(VALID_EVIDENCE_TYPES))
            + ". Verwende fuer payment_method nur: "
            + ", ".join(sorted(VALID_PAYMENT_METHODS))
            + ". Ausgabe-Felder: tax_year, document_date, service_period_from, service_period_to, "
            "document_type, issuer, recipient, total_amount, currency, payment_method, payment_verified, "
            "evidence_type, tax_category, tax_subcategory, deduction_domain, person_reference, "
            "child_reference, household_reference, extracted_evidence, classification_confidence, "
            "eligibility_confidence, reasoning_summary, flags. "
            "extracted_evidence muss ein Objekt mit invoice_present, issuer_identified, "
            "recipient_identified, service_description_present, service_period_present, amount_present, "
            "payment_method_identified, unbare_payment_evidence, labor_material_split sein."
        )
        if self.context["taxpayer_name"]:
            prompt += (
                "\nSteuerpflichtige Hauptperson: "
                + self.context["taxpayer_name"]
            )
        if self.context["children"]:
            prompt += "\nBekannte Kinder/Hinweise: " + json.dumps(self.context["children"], ensure_ascii=False)
        if self.personal_context:
            prompt += "\nZusätzlicher privater Steuerkontext (hoch priorisiert):\n" + self.personal_context

        content_preview = str(document.get("content") or "")[:8000]
        user_payload = {
            "title": document.get("title"),
            "created": document.get("created"),
            "standard_classification": classification_prediction,
            "content_preview": content_preview,
        }
        req_body = {
            "model": self.ai_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
        }

        try:
            response = self.session.post(
                f"{self.ai_base_url}/chat/completions",
                data=json.dumps(req_body),
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise TaxEnrichmentError("Tax-KI lieferte kein Objekt.")
            return parsed
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise TaxEnrichmentError(f"Tax-KI-Antwort ungueltig oder Request fehlgeschlagen: {exc}") from exc


class TaxEnrichmentService:
    """Coordinates AI extraction and rule-based post-processing."""

    def __init__(
        self,
        *,
        ai_model: str,
        ai_api_key: str,
        ai_base_url: str,
        request_timeout_seconds: int,
        basis_config: Optional[Dict[str, Any]] = None,
        personal_context: str = "",
    ) -> None:
        self.extractor = TaxEnrichmentAiExtractor(
            ai_model=ai_model,
            ai_api_key=ai_api_key,
            ai_base_url=ai_base_url,
            request_timeout_seconds=request_timeout_seconds,
            basis_config=basis_config,
            personal_context=personal_context,
        )
        self.processor = TaxEnrichmentProcessor(basis_config=basis_config)

    def enrich(
        self,
        *,
        document: Dict[str, Any],
        classification_prediction: Dict[str, Any],
    ) -> TaxEnrichment:
        payload = self.extractor.extract(
            document=document,
            classification_prediction=classification_prediction,
        )
        return self.processor.build_tax_enrichment(document=document, payload=payload)


class TaxExportCollector:
    """Collects tax enrichment records and writes JSON/CSV exports by year."""

    CSV_COLUMNS = [
        "document_id",
        "title",
        "document_date",
        "issuer",
        "total_amount",
        "tax_year",
        "tax_category",
        "tax_subcategory",
        "wiso_target_area",
        "formal_validity",
        "classification_confidence",
        "eligibility_confidence",
        "flags",
        "reasoning_summary",
    ]

    def __init__(self, *, basis_config: Optional[Dict[str, Any]] = None, export_years: Optional[List[int]] = None) -> None:
        self.basis_config = basis_config or {}
        self.context = extract_household_context(self.basis_config)
        self.export_years = sorted({year for year in (export_years or []) if isinstance(year, int)})
        self.documents: List[TaxEnrichment] = []

    def add(self, enrichment: TaxEnrichment) -> None:
        self.documents.append(enrichment)

    def write_exports(self, export_dir: Path) -> List[Path]:
        """Writes per-year JSON and CSV exports and returns created file paths."""

        export_dir.mkdir(parents=True, exist_ok=True)
        created: List[Path] = []
        docs_by_year = self._documents_by_year()
        for tax_year, docs in docs_by_year.items():
            year_dir = export_dir / str(tax_year)
            year_dir.mkdir(parents=True, exist_ok=True)
            json_path = year_dir / "tax_export.json"
            csv_path = year_dir / "tax_review.csv"
            payload = self._build_json_export(tax_year, docs)
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._write_csv(csv_path, docs)
            created.extend([json_path, csv_path])
        return created

    def _documents_by_year(self) -> Dict[int, List[TaxEnrichment]]:
        grouped: Dict[int, List[TaxEnrichment]] = {}
        for document in self.documents:
            if document.tax_year is None:
                continue
            if self.export_years and document.tax_year not in self.export_years:
                continue
            grouped.setdefault(document.tax_year, []).append(document)
        return dict(sorted(grouped.items()))

    def _build_json_export(self, tax_year: int, documents: List[TaxEnrichment]) -> Dict[str, Any]:
        category_totals: Dict[tuple[str, Optional[str]], Dict[str, Any]] = {}
        review_items: List[Dict[str, Any]] = []
        missing_evidence: List[Dict[str, Any]] = []
        notes_for_wiso: List[str] = []

        for document in documents:
            if document.total_amount is not None and document.tax_category not in {"nicht_steuerrelevant", "unklar"}:
                key = (document.tax_category, document.tax_subcategory)
                entry = category_totals.setdefault(
                    key,
                    {
                        "tax_category": document.tax_category,
                        "tax_subcategory": document.tax_subcategory,
                        "total_amount": 0.0,
                        "document_count": 0,
                        "currency": document.currency,
                    },
                )
                entry["total_amount"] = round(float(entry["total_amount"]) + float(document.total_amount), 2)
                entry["document_count"] += 1

            if document.flags or document.formal_validity != "valid":
                review_items.append(
                    {
                        "document_id": document.document_id,
                        "title": document.title,
                        "wiso_target_area": document.wiso_target_area,
                        "formal_validity": document.formal_validity,
                        "flags": document.flags,
                        "reasoning_summary": document.reasoning_summary,
                    }
                )
            if document.missing_requirements:
                missing_evidence.append(
                    {
                        "document_id": document.document_id,
                        "title": document.title,
                        "missing_requirements": document.missing_requirements,
                        "recommended_follow_up": document.recommended_follow_up,
                    }
                )
            if document.flags:
                notes_for_wiso.append(
                    f"Dokument {document.document_id or '-'} ({document.title}): "
                    f"{', '.join(document.flags)} | Ziel: {document.wiso_target_area}"
                )

        return {
            "taxpayer": {
                "name": self.context.get("taxpayer_name") or "",
            },
            "tax_year": tax_year,
            "documents": [document.to_dict() for document in documents],
            "category_totals": list(category_totals.values()),
            "review_items": review_items,
            "missing_evidence": missing_evidence,
            "notes_for_wiso": notes_for_wiso,
        }

    def _write_csv(self, csv_path: Path, documents: List[TaxEnrichment]) -> None:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.CSV_COLUMNS)
            writer.writeheader()
            for document in documents:
                writer.writerow(
                    {
                        "document_id": document.document_id,
                        "title": document.title,
                        "document_date": document.document_date or "",
                        "issuer": document.issuer or "",
                        "total_amount": "" if document.total_amount is None else f"{document.total_amount:.2f}",
                        "tax_year": document.tax_year or "",
                        "tax_category": document.tax_category,
                        "tax_subcategory": document.tax_subcategory or "",
                        "wiso_target_area": document.wiso_target_area,
                        "formal_validity": document.formal_validity,
                        "classification_confidence": f"{document.classification_confidence:.2f}",
                        "eligibility_confidence": f"{document.eligibility_confidence:.2f}",
                        "flags": ", ".join(document.flags),
                        "reasoning_summary": document.reasoning_summary,
                    }
                )
