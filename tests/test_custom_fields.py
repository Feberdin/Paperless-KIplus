"""Tests for Paperless custom-field enrichment.

Purpose:
- Protect both custom-field paths against regressions:
  - legacy contract/payroll enrichment
  - new `sb_` SecondBrain field synchronization
- Verify normalization, select resolution, overwrite protection, and tax reuse.

How to run:
- `python3 -m unittest discover -s tests`
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

if "yaml" not in sys.modules:
    sys.modules["yaml"] = types.SimpleNamespace(safe_load=lambda *_args, **_kwargs: {})

from paperless_ai_sorter import (
    DEFAULT_CUSTOM_FIELD_DEFINITIONS,
    SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS,
    CustomFieldDefinition,
    PaperlessApiError,
    PaperlessClient,
    build_ai_note_entry,
    build_patch_payload,
    collect_populated_secondbrain_fields,
    build_secondbrain_custom_fields_payload,
    build_secondbrain_sync_report,
    build_secondbrain_suggestions,
    build_custom_field_extra_data,
    build_select_option_lookup,
    build_select_option_id,
    filter_unchanged_patch_fields,
    normalize_custom_field_value,
    normalize_monetary_value,
    normalize_prediction_custom_fields,
    normalize_secondbrain_prediction_fields,
    resolve_custom_field_value,
)


class _FakeClient:
    """Minimal fake for build_patch_payload tests."""

    def __init__(self) -> None:
        self.created_custom_fields: List[CustomFieldDefinition] = []

    def create_custom_field(self, definition: CustomFieldDefinition) -> Dict[str, Any]:
        self.created_custom_fields.append(definition)
        next_id = 100 + len(self.created_custom_fields)
        return {
            "id": next_id,
            "name": definition.paperless_name,
            "data_type": definition.data_type,
        }


class _PatchFallbackClient(PaperlessClient):
    """Fake Paperless client for Custom-Field write fallback tests."""

    def __init__(self, *, failing_field_ids: set[int] | None = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.failing_field_ids = failing_field_ids or set()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        payload: Dict[str, Any] | None = None,
        retries: int = 3,
    ) -> Dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "retries": retries,
            }
        )
        if method == "PATCH":
            raise PaperlessApiError("direct patch unsupported")

        parameters = (payload or {}).get("parameters") or {}
        add_fields = parameters.get("add_custom_fields")
        if isinstance(add_fields, dict):
            if len(add_fields) > 1:
                raise PaperlessApiError("bulk package rejected")
            for field_id in add_fields:
                if int(field_id) in self.failing_field_ids:
                    raise PaperlessApiError(f"field {field_id} rejected")
        return {}


class CustomFieldTests(unittest.TestCase):
    """Covers the productive Paperless custom-field flows."""

    def _secondbrain_field_map(self) -> Dict[str, Dict[str, Any]]:
        return {
            "sb_document_category": {
                "id": 201,
                "name": "sb_document_category",
                "data_type": "select",
                "extra_data": {
                    "select_options": [
                        {"id": 11, "label": "Rechnung"},
                        {"id": 12, "label": "Vertrag"},
                        {"id": 13, "label": "Steuer"},
                    ]
                },
                "select_options_by_label": build_select_option_lookup(
                    {
                        "select_options": [
                            {"id": 11, "label": "Rechnung"},
                            {"id": 12, "label": "Vertrag"},
                            {"id": 13, "label": "Steuer"},
                        ]
                    }
                ),
            },
            "sb_requires_action": {
                "id": 202,
                "name": "sb_requires_action",
                "data_type": "boolean",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_due_date": {
                "id": 203,
                "name": "sb_due_date",
                "data_type": "date",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_calendar_date": {
                "id": 214,
                "name": "sb_calendar_date",
                "data_type": "date",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_calendar_time": {
                "id": 215,
                "name": "sb_calendar_time",
                "data_type": "string",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_calendar_type": {
                "id": 216,
                "name": "sb_calendar_type",
                "data_type": "select",
                "extra_data": {
                    "select_options": [
                        {"id": 51, "label": "Termin"},
                        {"id": 52, "label": "Einladung"},
                        {"id": 53, "label": "Frist"},
                        {"id": 54, "label": "Gericht"},
                        {"id": 55, "label": "Zahlung"},
                        {"id": 56, "label": "Erinnerung"},
                        {"id": 57, "label": "Sonstiges"},
                    ]
                },
                "select_options_by_label": build_select_option_lookup(
                    {
                        "select_options": [
                            {"id": 51, "label": "Termin"},
                            {"id": 52, "label": "Einladung"},
                            {"id": 53, "label": "Frist"},
                            {"id": 54, "label": "Gericht"},
                            {"id": 55, "label": "Zahlung"},
                            {"id": 56, "label": "Erinnerung"},
                            {"id": 57, "label": "Sonstiges"},
                        ]
                    }
                ),
            },
            "sb_calendar_title": {
                "id": 217,
                "name": "sb_calendar_title",
                "data_type": "string",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_calendar_location": {
                "id": 219,
                "name": "sb_calendar_location",
                "data_type": "string",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_calendar_events": {
                "id": 218,
                "name": "sb_calendar_events",
                "data_type": "string",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_amount_total": {
                "id": 204,
                "name": "sb_amount_total",
                "data_type": "monetary",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_tax_year": {
                "id": 205,
                "name": "sb_tax_year",
                "data_type": "integer",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_confidence": {
                "id": 206,
                "name": "sb_confidence",
                "data_type": "select",
                "extra_data": {
                    "select_options": [
                        {"id": 31, "label": "KI sicher"},
                        {"id": 32, "label": "KI unsicher"},
                        {"id": 33, "label": "OCR unsicher"},
                        {"id": 34, "label": "Ungeprüft"},
                    ]
                },
                "select_options_by_label": build_select_option_lookup(
                    {
                        "select_options": [
                            {"id": 31, "label": "KI sicher"},
                            {"id": 32, "label": "KI unsicher"},
                            {"id": 33, "label": "OCR unsicher"},
                            {"id": 34, "label": "Ungeprüft"},
                        ]
                    }
                ),
            },
            "sb_source_quality": {
                "id": 207,
                "name": "sb_source_quality",
                "data_type": "select",
                "extra_data": {
                    "select_options": [
                        {"id": 41, "label": "Original-PDF"},
                        {"id": 42, "label": "Scan gut"},
                        {"id": 43, "label": "Scan schlecht"},
                        {"id": 44, "label": "Foto"},
                        {"id": 45, "label": "E-Mail"},
                        {"id": 46, "label": "Import"},
                    ]
                },
                "select_options_by_label": build_select_option_lookup(
                    {
                        "select_options": [
                            {"id": 41, "label": "Original-PDF"},
                            {"id": 42, "label": "Scan gut"},
                            {"id": 43, "label": "Scan schlecht"},
                            {"id": 44, "label": "Foto"},
                            {"id": 45, "label": "E-Mail"},
                            {"id": 46, "label": "Import"},
                        ]
                    }
                ),
            },
            "sb_provider_name": {
                "id": 208,
                "name": "sb_provider_name",
                "data_type": "string",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_document_date": {
                "id": 209,
                "name": "sb_document_date",
                "data_type": "date",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_period_start": {
                "id": 210,
                "name": "sb_period_start",
                "data_type": "date",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_period_end": {
                "id": 211,
                "name": "sb_period_end",
                "data_type": "date",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_export_to_secondbrain": {
                "id": 212,
                "name": "sb_export_to_secondbrain",
                "data_type": "boolean",
                "extra_data": {},
                "select_options_by_label": {},
            },
            "sb_ignore_by_secondbrain": {
                "id": 213,
                "name": "sb_ignore_by_secondbrain",
                "data_type": "boolean",
                "extra_data": {},
                "select_options_by_label": {},
            },
        }

    def test_normalize_prediction_custom_fields_filters_unknown_and_formats_values(self) -> None:
        prediction = {
            "custom_fields": {
                "contract_number": " V-12345 ",
                "monthly_cost": "49,9 €",
                "contract_start_date": "2026-04-01T00:00:00",
                "unknown_field": "should be ignored",
            }
        }

        normalized = normalize_prediction_custom_fields(
            prediction,
            DEFAULT_CUSTOM_FIELD_DEFINITIONS,
        )

        self.assertEqual(normalized["contract_number"], "V-12345")
        self.assertEqual(normalized["monthly_cost"], "EUR49.90")
        self.assertEqual(normalized["contract_start_date"], "2026-04-01")
        self.assertNotIn("unknown_field", normalized)

    def test_select_label_is_resolved_to_paperless_option_id(self) -> None:
        field = self._secondbrain_field_map()["sb_document_category"]
        resolved, reason = resolve_custom_field_value(field, "Rechnung")
        self.assertIsNone(reason)
        self.assertEqual(resolved, 11)

    def test_select_label_accepts_paperless_string_option_id(self) -> None:
        field = {
            "id": 999,
            "name": "sb_confidence",
            "data_type": "select",
            "select_options_by_label": {"ki sicher": "ki_sicher"},
        }
        resolved, reason = resolve_custom_field_value(field, "KI sicher")
        self.assertIsNone(reason)
        self.assertEqual(resolved, "ki_sicher")

    def test_select_option_extra_data_uses_stable_string_ids(self) -> None:
        definition = SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS["sb_calendar_type"]
        extra_data = build_custom_field_extra_data(definition)

        self.assertEqual(build_select_option_id("KI sicher"), "ki_sicher")
        self.assertIn(
            {"label": "Gericht", "id": "gericht"},
            extra_data["select_options"],
        )

    def test_custom_field_patch_falls_back_to_single_field_writes(self) -> None:
        client = _PatchFallbackClient()

        client.patch_document_custom_fields(123, {10: "alpha", 11: "beta"})

        bulk_payloads = [
            call["payload"]
            for call in client.calls
            if call["method"] == "POST" and call["path"] == "/api/documents/bulk_edit/"
        ]
        self.assertEqual(
            [payload["parameters"]["add_custom_fields"] for payload in bulk_payloads],
            [{10: "alpha", 11: "beta"}, {10: "alpha"}, {11: "beta"}],
        )

    def test_custom_field_patch_keeps_successful_single_fields(self) -> None:
        client = _PatchFallbackClient(failing_field_ids={11})

        client.patch_document_custom_fields(123, {10: "alpha", 11: "beta"})

        single_field_payloads = [
            call["payload"]["parameters"]["add_custom_fields"]
            for call in client.calls
            if call["method"] == "POST"
            and isinstance(call["payload"]["parameters"]["add_custom_fields"], dict)
            and len(call["payload"]["parameters"]["add_custom_fields"]) == 1
        ]
        self.assertEqual(single_field_payloads, [{10: "alpha"}, {11: "beta"}])

    def test_custom_field_patch_raises_when_all_single_fields_fail(self) -> None:
        client = _PatchFallbackClient(failing_field_ids={10, 11})

        with self.assertRaises(PaperlessApiError):
            client.patch_document_custom_fields(123, {10: "alpha", 11: "beta"})

    def test_invalid_select_label_is_reported(self) -> None:
        field = self._secondbrain_field_map()["sb_document_category"]
        resolved, reason = resolve_custom_field_value(field, "Nicht vorhanden")
        self.assertIsNone(resolved)
        self.assertIn("Select-Option nicht gefunden", reason or "")

    def test_date_normalization_uses_iso_format(self) -> None:
        definition = SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS["sb_due_date"]
        self.assertEqual(
            normalize_custom_field_value(definition, "2026-05-15T12:00:00"),
            "2026-05-15",
        )

    def test_monetary_normalization_accepts_german_format(self) -> None:
        self.assertEqual(
            normalize_monetary_value("1.234,56 €", output_format="decimal"),
            "1234.56",
        )
        self.assertEqual(
            normalize_monetary_value("1.234,56 €", output_format="paperless"),
            "EUR1234.56",
        )

    def test_boolean_normalization(self) -> None:
        definition = SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS["sb_requires_action"]
        self.assertIs(normalize_custom_field_value(definition, "ja"), True)
        self.assertIs(normalize_custom_field_value(definition, "false"), False)
        self.assertIsNone(normalize_custom_field_value(definition, "vielleicht"))

    def test_build_patch_payload_creates_missing_legacy_custom_fields(self) -> None:
        client = _FakeClient()
        created_entities: Dict[str, List[str]] = {}
        custom_field_id_to_definition: Dict[int, CustomFieldDefinition] = {}

        payload = build_patch_payload(
            client=client,
            document={},
            prediction={
                "custom_fields": {
                    "contract_number": "V-12345",
                    "monthly_cost": "49,90 €",
                }
            },
            tags_map={},
            doc_types_map={},
            correspondents_map={},
            storage_paths_map={},
            custom_fields_map={},
            custom_field_definitions=DEFAULT_CUSTOM_FIELD_DEFINITIONS,
            create_missing_entities=False,
            create_missing_custom_fields=True,
            include_standard_metadata=True,
            enable_secondbrain_custom_fields=False,
            secondbrain_overwrite_existing=False,
            secondbrain_attach_empty_when_unknown=False,
            secondbrain_confidence_threshold=0.70,
            secondbrain_log_missing_fields=True,
            created_entities=created_entities,
            custom_field_id_to_definition=custom_field_id_to_definition,
        )

        self.assertEqual(
            payload["custom_fields"],
            {
                101: "V-12345",
                102: "EUR49.90",
            },
        )
        self.assertEqual(
            created_entities["/api/custom_fields/"],
            ["Vertragsnummer", "Monatliche Aufwendungen"],
        )

    def test_secondbrain_prediction_normalization_keeps_confidence_and_reason(self) -> None:
        prediction = {
            "confidence": 0.81,
            "secondbrain_custom_fields": {
                "sb_document_category": {
                    "value": "Rechnung",
                    "confidence": 0.92,
                    "reason": "Rechnungsnummer und Betrag erkannt.",
                },
                "sb_amount_total": {
                    "value": "1.234,56 €",
                    "confidence": 0.88,
                    "reason": "Gesamtbetrag klar lesbar.",
                },
            },
        }

        normalized = normalize_secondbrain_prediction_fields(
            prediction,
            SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS,
        )

        self.assertEqual(normalized["sb_document_category"].value, "Rechnung")
        self.assertEqual(normalized["sb_document_category"].confidence, 0.92)
        self.assertEqual(normalized["sb_amount_total"].value, "1234.56")

    def test_missing_secondbrain_field_is_logged_in_report_and_skipped(self) -> None:
        report = {
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

        values, empty_ids, remove_ids = build_secondbrain_custom_fields_payload(
            document={"title": "Rechnung", "content": "Rechnung 123", "custom_fields": []},
            prediction={
                "confidence": 0.95,
                "secondbrain_custom_fields": {
                    "sb_document_category": {
                        "value": "Rechnung",
                        "confidence": 0.95,
                        "reason": "Klarer Rechnungsbezug.",
                    }
                },
            },
            tax_enrichment=None,
            custom_fields_map={},
            overwrite_existing=False,
            attach_empty_when_unknown=False,
            confidence_threshold=0.70,
            log_missing_fields=True,
            custom_field_id_to_definition={},
            sync_report=report,
        )

        self.assertEqual(values, {})
        self.assertEqual(empty_ids, [])
        self.assertEqual(remove_ids, [])
        self.assertIn("sb_document_category", report["missing_fields"])

    def test_existing_secondbrain_value_is_not_overwritten_by_default(self) -> None:
        report = {
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

        values, _, _ = build_secondbrain_custom_fields_payload(
            document={
                "title": "Rechnung",
                "content": "Rechnung 123",
                "custom_fields": {"sb_document_category": {"value": 12}},
            },
            prediction={
                "confidence": 0.95,
                "secondbrain_custom_fields": {
                    "sb_document_category": {
                        "value": "Rechnung",
                        "confidence": 0.95,
                        "reason": "Klarer Rechnungsbezug.",
                    }
                },
            },
            tax_enrichment=None,
            custom_fields_map=self._secondbrain_field_map(),
            overwrite_existing=False,
            attach_empty_when_unknown=False,
            confidence_threshold=0.70,
            log_missing_fields=True,
            custom_field_id_to_definition={},
            sync_report=report,
        )

        self.assertNotIn(201, values)
        self.assertIn("sb_document_category", report["preserved_existing"])

    def test_existing_secondbrain_value_is_overwritten_when_enabled(self) -> None:
        report = {
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

        values, _, _ = build_secondbrain_custom_fields_payload(
            document={
                "title": "Rechnung",
                "content": "Rechnung 123",
                "custom_fields": {"sb_document_category": {"value": 12}},
            },
            prediction={
                "confidence": 0.95,
                "secondbrain_custom_fields": {
                    "sb_document_category": {
                        "value": "Rechnung",
                        "confidence": 0.95,
                        "reason": "Klarer Rechnungsbezug.",
                    }
                },
            },
            tax_enrichment=None,
            custom_fields_map=self._secondbrain_field_map(),
            overwrite_existing=True,
            attach_empty_when_unknown=False,
            confidence_threshold=0.70,
            log_missing_fields=True,
            custom_field_id_to_definition={},
            sync_report=report,
        )

        self.assertEqual(values[201], 11)
        self.assertIn("sb_document_category", report["written"])

    def test_collect_populated_secondbrain_fields_detects_existing_values(self) -> None:
        populated = collect_populated_secondbrain_fields(
            {
                "title": "Rechnung",
                "custom_fields": {
                    "sb_document_category": {"value": 11},
                    "sb_amount_total": {"value": "EUR49.90"},
                    "other_field": {"value": "ignore"},
                },
            },
            self._secondbrain_field_map(),
        )

        self.assertIn("sb_document_category", populated)
        self.assertIn("sb_amount_total", populated)

    def test_collect_populated_secondbrain_fields_falls_back_to_raw_sb_keys(self) -> None:
        populated = collect_populated_secondbrain_fields(
            {
                "title": "Rechnung",
                "custom_fields": {
                    "sb_due_date": {"value": "2026-05-15"},
                    "foo": {"value": "bar"},
                },
            },
            {},
        )

        self.assertEqual(populated, ["sb_due_date"])

    def test_tax_enrichment_backfills_secondbrain_fields(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "Kita Rechnung",
                "content": "Monatlicher Beitrag Kita Musterstadt",
                "created": "2026-03-12",
            },
            prediction={
                "document_type": "Rechnung",
                "correspondent": "Kita Musterstadt",
                "summary": "Kinderbetreuungskosten erkannt.",
                "rationale": "Kita und Betreuungszeitraum im Dokument.",
                "confidence": 0.84,
            },
            tax_enrichment=SimpleNamespace(
                tax_year=2025,
                document_date="2026-03-12",
                service_period_from="2025-01-01",
                service_period_to="2025-01-31",
                issuer="Kita Musterstadt",
                total_amount=123.45,
                tax_category="kinderbetreuungskosten",
                flags=["needs_review"],
            ),
        )

        self.assertEqual(suggestions["sb_tax_year"].value, 2025)
        self.assertEqual(suggestions["sb_provider_name"].value, "Kita Musterstadt")
        self.assertEqual(suggestions["sb_amount_total"].value, "123.45")
        self.assertEqual(suggestions["sb_action_status"].value, "In Prüfung")

    def test_ai_calendar_fields_are_written_to_secondbrain_custom_fields(self) -> None:
        values, empty_ids, remove_ids = build_secondbrain_custom_fields_payload(
            document={"title": "Einladung Elternabend", "content": "Bitte kommen Sie zum Elternabend.", "custom_fields": []},
            prediction={
                "document_type": "Einladung",
                "correspondent": "Schule",
                "summary": "Einladung mit konkretem Termin.",
                "confidence": 0.93,
                "secondbrain_custom_fields": {
                    "sb_calendar_date": {
                        "value": "2026-09-12",
                        "confidence": 0.95,
                        "reason": "Termin der Einladung klar genannt.",
                    },
                    "sb_calendar_time": {
                        "value": "18:30",
                        "confidence": 0.90,
                        "reason": "Uhrzeit klar genannt.",
                    },
                    "sb_calendar_type": {
                        "value": "Einladung",
                        "confidence": 0.91,
                        "reason": "Dokument ist als Einladung formuliert.",
                    },
                    "sb_calendar_title": {
                        "value": "Elternabend Schule",
                        "confidence": 0.88,
                        "reason": "Kurzer Kalendertitel aus Dokumenttitel.",
                    },
                    "sb_calendar_location": {
                        "value": "Aula der Schule",
                        "confidence": 0.87,
                        "reason": "Ort klar genannt.",
                    },
                    "sb_calendar_events": {
                        "value": [
                            {
                                "date": "2026-09-12",
                                "time": "18:30",
                                "type": "Einladung",
                                "title": "Elternabend Schule",
                                "location": "Aula der Schule",
                                "reason": "Einladungstermin klar genannt.",
                            },
                            {
                                "date": "2026-09-19",
                                "type": "Frist",
                                "title": "Rückmeldung Elternabend",
                                "reason": "Rückmeldefrist klar genannt.",
                            },
                        ],
                        "confidence": 0.92,
                        "reason": "Alle kalenderrelevanten Ereignisse gesammelt.",
                    },
                },
            },
            tax_enrichment=None,
            custom_fields_map=self._secondbrain_field_map(),
            overwrite_existing=False,
            attach_empty_when_unknown=False,
            confidence_threshold=0.70,
            log_missing_fields=True,
            custom_field_id_to_definition={},
            sync_report=build_secondbrain_sync_report(),
        )

        self.assertEqual(values[214], "2026-09-12")
        self.assertEqual(values[215], "18:30")
        self.assertEqual(values[216], 52)
        self.assertEqual(values[217], "Elternabend Schule")
        self.assertEqual(values[219], "Aula der Schule")
        calendar_events = json.loads(values[218])
        self.assertEqual(len(calendar_events), 2)
        self.assertEqual(calendar_events[0]["date"], "2026-09-12")
        self.assertEqual(calendar_events[0]["time"], "18:30")
        self.assertEqual(calendar_events[0]["location"], "Aula der Schule")
        self.assertEqual(calendar_events[1]["date"], "2026-09-19")
        self.assertEqual(empty_ids, [])
        self.assertEqual(remove_ids, [])

    def test_rule_based_calendar_detection_finds_court_hearing(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "Ladung Amtsgericht",
                "content": (
                    "Sie werden geladen zum Termin zur mündlichen Verhandlung "
                    "am 15.08.2026 um 09:30 Uhr im Amtsgericht Musterstadt."
                ),
                "created": "2026-07-01",
            },
            prediction={
                "document_type": "Schreiben",
                "correspondent": "Amtsgericht Musterstadt",
                "summary": "Ladung zu einer Gerichtsverhandlung.",
                "rationale": "Gericht und Verhandlungstermin sind erkennbar.",
                "confidence": 0.88,
            },
            tax_enrichment=None,
        )

        self.assertEqual(suggestions["sb_calendar_date"].value, "2026-08-15")
        self.assertEqual(suggestions["sb_calendar_time"].value, "09:30")
        self.assertEqual(suggestions["sb_calendar_type"].value, "Gericht")
        self.assertTrue(suggestions["sb_requires_action"].value)
        self.assertEqual(suggestions["sb_action_status"].value, "Offen")

    def test_rule_based_calendar_detection_collects_general_events(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "Terminbestätigung Praxis",
                "content": (
                    "Ihr Arzttermin findet am 05.09.2026 um 10:15 Uhr statt. "
                    "Bitte senden Sie die Rückmeldung bis 01.09.2026."
                ),
                "created": "2026-08-20",
            },
            prediction={
                "document_type": "Terminbestätigung",
                "correspondent": "Praxis Musterstadt",
                "summary": "Terminbestätigung mit Rückmeldefrist.",
                "rationale": "Arzttermin und Rückmeldefrist sind im Text enthalten.",
                "confidence": 0.90,
            },
            tax_enrichment=None,
        )

        self.assertEqual(suggestions["sb_calendar_date"].value, "2026-09-05")
        self.assertEqual(suggestions["sb_calendar_time"].value, "10:15")
        self.assertEqual(suggestions["sb_calendar_type"].value, "Termin")
        calendar_events = json.loads(suggestions["sb_calendar_events"].value)
        self.assertEqual(
            [(event["date"], event.get("time"), event["type"]) for event in calendar_events],
            [
                ("2026-09-05", "10:15", "Termin"),
                ("2026-09-01", None, "Frist"),
            ],
        )

    def test_rule_based_calendar_detection_ignores_plain_document_date(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "Allgemeines Schreiben",
                "content": "Dieses Schreiben wurde am 15.08.2026 erstellt und enthält nur Informationen.",
                "created": "2026-08-15",
            },
            prediction={
                "document_type": "Schreiben",
                "correspondent": "Muster GmbH",
                "summary": "Informationsschreiben ohne Termin oder Frist.",
                "rationale": "Kein Kalenderbezug im Inhalt.",
                "confidence": 0.82,
            },
            tax_enrichment=None,
        )

        self.assertNotIn("sb_calendar_date", suggestions)
        self.assertEqual(suggestions["sb_document_date"].value, "2026-08-15")

    def test_rule_based_calendar_detection_prefers_compact_court_date_over_ai_document_date(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "2026_07_06_Landgericht Oldenburg_Rechtsanwalt_04092026_Akte",
                "content": (
                    "Oldenburg, den 06.07.2026. Termin zur mündlichen Verhandlung ist bestimmt. "
                    "Datum Uhrzeit Anschrift Freitag, 4. September 2026 09:00 "
                    "Richard-Wagner-Platz 1, Eingang 9 Oberlandesgericht."
                ),
                "created": "2026-07-06",
            },
            prediction={
                "document_type": "Schreiben",
                "correspondent": "Landgericht Oldenburg",
                "document_date": "2026-07-06",
                "summary": "Gerichtliches Schreiben mit Termin.",
                "rationale": "Das Dokument betrifft einen Gerichtstermin.",
                "confidence": 0.91,
                "secondbrain_custom_fields": {
                    "sb_calendar_date": {
                        "value": "2026-07-06",
                        "confidence": 0.90,
                        "reason": "Falsch aus dem Dokumentdatum übernommen.",
                    },
                    "sb_calendar_type": {
                        "value": "Termin",
                        "confidence": 0.90,
                        "reason": "Falsch aus dem Dokumentdatum übernommen.",
                    },
                },
            },
            tax_enrichment=None,
        )

        self.assertEqual(suggestions["sb_calendar_date"].value, "2026-09-04")
        self.assertEqual(suggestions["sb_calendar_time"].value, "09:00")
        self.assertEqual(suggestions["sb_calendar_date"].source, "rules")
        self.assertEqual(suggestions["sb_calendar_type"].value, "Gericht")
        self.assertIn("Richard-Wagner-Platz", suggestions["sb_calendar_location"].value)
        calendar_events = json.loads(suggestions["sb_calendar_events"].value)
        self.assertEqual([event["date"] for event in calendar_events], ["2026-09-04"])
        self.assertEqual(calendar_events[0]["time"], "09:00")
        self.assertIn("Richard-Wagner-Platz", calendar_events[0]["location"])

    def test_rule_based_calendar_detection_extracts_private_invitation_datetime(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "2026_07_17_Erkhard Laumann_Einladung_17072026",
                "content": (
                    "Einladung. Diesen besonderen Tag möchte ich mit Euch feiern. "
                    "Datum: 4. Oktober 2026 Zeit: 9.30 Uhr Ort: Hof Reck."
                ),
                "created": "2026-07-17",
            },
            prediction={
                "document_type": "Einladung",
                "correspondent": "Erkhard Laumann",
                "document_date": "2026-07-17",
                "summary": "Private Einladung zu einem Geburtstag.",
                "rationale": "Einladung mit Datum und Uhrzeit.",
                "confidence": 0.91,
                "secondbrain_custom_fields": {
                    "sb_calendar_date": {
                        "value": "2026-07-17",
                        "confidence": 0.90,
                        "reason": "Falsch aus dem Dokumentdatum übernommen.",
                    },
                    "sb_calendar_type": {
                        "value": "Gericht",
                        "confidence": 0.90,
                        "reason": "Falsch durch Teilstring 'ladung' in Einladung abgeleitet.",
                    },
                },
            },
            tax_enrichment=None,
        )

        self.assertEqual(suggestions["sb_calendar_date"].value, "2026-10-04")
        self.assertEqual(suggestions["sb_calendar_time"].value, "09:30")
        self.assertEqual(suggestions["sb_calendar_type"].value, "Einladung")
        self.assertIn("Hof Reck", suggestions["sb_calendar_location"].value)
        calendar_events = json.loads(suggestions["sb_calendar_events"].value)
        self.assertEqual(calendar_events[0]["date"], "2026-10-04")
        self.assertEqual(calendar_events[0]["time"], "09:30")
        self.assertIn("Hof Reck", calendar_events[0]["location"])

    def test_rule_based_calendar_detection_replaces_generic_ai_type_for_same_date(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "2026_07_06_Landgericht Oldenburg_Rechtsanwalt_04092026_Akte",
                "content": (
                    "Termin zur mündlichen Verhandlung. Datum Uhrzeit Anschrift "
                    "Freitag, 4. September 2026 09:00 Richard-Wagner-Platz 1, Eingang 9."
                ),
                "created": "2026-07-06",
            },
            prediction={
                "document_type": "Schreiben",
                "correspondent": "Landgericht Oldenburg",
                "document_date": "2026-07-06",
                "summary": "Gerichtliches Schreiben mit Termin.",
                "rationale": "Das Dokument betrifft einen Gerichtstermin.",
                "confidence": 0.91,
                "secondbrain_custom_fields": {
                    "sb_calendar_date": {
                        "value": "2026-09-04",
                        "confidence": 0.90,
                        "reason": "Datum korrekt, aber Details fehlen.",
                    },
                    "sb_calendar_type": {
                        "value": "Termin",
                        "confidence": 0.90,
                        "reason": "Zu generisch.",
                    },
                },
            },
            tax_enrichment=None,
        )

        self.assertEqual(suggestions["sb_calendar_date"].value, "2026-09-04")
        self.assertEqual(suggestions["sb_calendar_time"].value, "09:00")
        self.assertEqual(suggestions["sb_calendar_type"].value, "Gericht")
        self.assertIn("Richard-Wagner-Platz", suggestions["sb_calendar_location"].value)

    def test_rule_based_calendar_detection_ignores_court_invoice_date(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "2026_06_17_Landgericht Oldenburg_Rechtsanwalt_17062026_Kostenrechnung",
                "content": "Kostenrechnung vom 17.06.2026 mit Betrag und Gebühren.",
                "created": "2026-06-17",
            },
            prediction={
                "document_type": "Kostenrechnung",
                "correspondent": "Landgericht Oldenburg",
                "document_date": "2026-06-17",
                "summary": "Kostenrechnung zu Gebühren.",
                "rationale": "Es geht um einen Rechnungsbetrag.",
                "confidence": 0.90,
                "secondbrain_custom_fields": {
                    "sb_calendar_date": {
                        "value": "2026-06-17",
                        "confidence": 0.88,
                        "reason": "Falsch aus dem Rechnungsdatum übernommen.",
                    },
                    "sb_calendar_type": {
                        "value": "Gericht",
                        "confidence": 0.88,
                        "reason": "Falsch aus dem Absender abgeleitet.",
                    },
                },
            },
            tax_enrichment=None,
        )

        self.assertNotIn("sb_calendar_date", suggestions)
        self.assertNotIn("sb_calendar_events", suggestions)

    def test_billing_calendar_document_date_uses_due_date_when_available(self) -> None:
        suggestions = build_secondbrain_suggestions(
            document={
                "title": "2026_06_17_Landgericht Oldenburg_Rechtsanwalt_17062026_Kostenrechnung",
                "content": "Kostenrechnung vom 17.06.2026 mit Betrag und Gebühren.",
                "created": "2026-06-17",
            },
            prediction={
                "document_type": "Kostenrechnung",
                "correspondent": "Landgericht Oldenburg",
                "document_date": "2026-06-17",
                "summary": "Kostenrechnung zu Gebühren.",
                "rationale": "Rechnungsbetrag mit Fälligkeit.",
                "confidence": 0.90,
                "secondbrain_custom_fields": {
                    "sb_due_date": {
                        "value": "2026-07-01",
                        "confidence": 0.88,
                        "reason": "Fälligkeit aus der Kostenrechnung.",
                    },
                    "sb_calendar_date": {
                        "value": "2026-06-17",
                        "confidence": 0.88,
                        "reason": "Falsch aus dem Rechnungsdatum übernommen.",
                    },
                    "sb_calendar_type": {
                        "value": "Gericht",
                        "confidence": 0.88,
                        "reason": "Falsch aus dem Absender abgeleitet.",
                    },
                },
            },
            tax_enrichment=None,
        )

        self.assertEqual(suggestions["sb_calendar_date"].value, "2026-07-01")
        self.assertEqual(suggestions["sb_calendar_type"].value, "Zahlung")
        calendar_events = json.loads(suggestions["sb_calendar_events"].value)
        self.assertEqual(calendar_events[0]["date"], "2026-07-01")

    def test_build_patch_payload_includes_secondbrain_values(self) -> None:
        client = _FakeClient()
        custom_field_id_to_definition: Dict[int, CustomFieldDefinition] = {}
        secondbrain_report = {
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

        payload = build_patch_payload(
            client=client,
            document={"title": "2026-04 Rechnung", "content": "Rechnung 1234", "custom_fields": []},
            prediction={
                "document_type": "Rechnung",
                "correspondent": "Stromversorger",
                "storage_path": "Privat",
                "tags": [],
                "document_date": "2026-04-03",
                "summary": "Rechnung erkannt.",
                "confidence": 0.91,
                "rationale": "Rechnungsdaten klar erkennbar.",
                "secondbrain_custom_fields": {
                    "sb_document_category": {
                        "value": "Rechnung",
                        "confidence": 0.95,
                        "reason": "Rechnung erkannt.",
                    },
                    "sb_amount_total": {
                        "value": "1.234,56 €",
                        "confidence": 0.88,
                        "reason": "Gesamtbetrag erkannt.",
                    },
                },
            },
            tags_map={},
            doc_types_map={},
            correspondents_map={},
            storage_paths_map={},
            custom_fields_map=self._secondbrain_field_map(),
            custom_field_definitions=None,
            create_missing_entities=False,
            create_missing_custom_fields=False,
            include_standard_metadata=True,
            enable_secondbrain_custom_fields=True,
            secondbrain_overwrite_existing=False,
            secondbrain_attach_empty_when_unknown=False,
            secondbrain_confidence_threshold=0.70,
            secondbrain_log_missing_fields=True,
            tax_enrichment=None,
            created_entities={},
            custom_field_id_to_definition=custom_field_id_to_definition,
            secondbrain_sync_report=secondbrain_report,
        )

        self.assertEqual(payload["created"], "2026-04-03")
        self.assertEqual(payload["custom_fields"][201], 11)
        self.assertEqual(payload["custom_fields"][204], "EUR1234.56")
        self.assertIn(206, payload["custom_fields"])
        self.assertIn(207, payload["custom_fields"])

    def test_build_patch_payload_enrichment_only_keeps_secondbrain_fields_without_standard_metadata(self) -> None:
        client = _FakeClient()
        report = {
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

        payload = build_patch_payload(
            client=client,
            document={"title": "Bestehende Rechnung", "custom_fields": []},
            prediction={
                "document_type": "Rechnung",
                "correspondent": "Stromversorger",
                "storage_path": "Privat",
                "tags": ["Rechnung"],
                "document_date": "2026-04-03",
                "summary": "Rechnung erkannt.",
                "confidence": 0.91,
                "rationale": "Rechnungsdaten klar erkennbar.",
                "secondbrain_custom_fields": {
                    "sb_document_category": {
                        "value": "Rechnung",
                        "confidence": 0.95,
                        "reason": "Rechnung erkannt.",
                    },
                    "sb_amount_total": {
                        "value": "49,90 €",
                        "confidence": 0.88,
                        "reason": "Gesamtbetrag erkannt.",
                    },
                },
            },
            tags_map={"rechnung": 7},
            doc_types_map={"rechnung": 5},
            correspondents_map={"stromversorger": 6},
            storage_paths_map={"privat": 7},
            custom_fields_map=self._secondbrain_field_map(),
            custom_field_definitions=None,
            create_missing_entities=False,
            create_missing_custom_fields=False,
            include_standard_metadata=False,
            enable_secondbrain_custom_fields=True,
            secondbrain_overwrite_existing=False,
            secondbrain_attach_empty_when_unknown=False,
            secondbrain_confidence_threshold=0.70,
            secondbrain_log_missing_fields=True,
            created_entities={},
            custom_field_id_to_definition={},
            secondbrain_sync_report=report,
        )

        self.assertNotIn("document_type", payload)
        self.assertNotIn("correspondent", payload)
        self.assertNotIn("storage_path", payload)
        self.assertNotIn("created", payload)
        self.assertNotIn("tags", payload)
        self.assertEqual(payload["custom_fields"][201], 11)
        self.assertEqual(payload["custom_fields"][204], "EUR49.90")

    def test_build_patch_payload_adds_sb_tag_when_secondbrain_ready(self) -> None:
        client = _FakeClient()
        payload = build_patch_payload(
            client=client,
            document={
                "title": "Bestehende Rechnung",
                "tags": [7],
                "custom_fields": {"sb_document_category": {"value": 11}},
            },
            prediction={
                "document_type": "Rechnung",
                "correspondent": "Stromversorger",
                "storage_path": "Privat",
                "tags": [],
                "document_date": "2026-04-03",
                "summary": "Rechnung erkannt.",
                "confidence": 0.91,
                "rationale": "Rechnungsdaten klar erkennbar.",
                "secondbrain_custom_fields": {
                    "sb_document_category": {
                        "value": "Rechnung",
                        "confidence": 0.95,
                        "reason": "Rechnung erkannt.",
                    }
                },
            },
            tags_map={"sb": 999},
            doc_types_map={},
            correspondents_map={},
            storage_paths_map={},
            custom_fields_map=self._secondbrain_field_map(),
            custom_field_definitions=None,
            create_missing_entities=False,
            create_missing_custom_fields=False,
            include_standard_metadata=False,
            enable_secondbrain_custom_fields=True,
            secondbrain_overwrite_existing=False,
            secondbrain_attach_empty_when_unknown=False,
            secondbrain_confidence_threshold=0.70,
            secondbrain_log_missing_fields=True,
            created_entities={},
            custom_field_id_to_definition={},
            secondbrain_sync_report={
                "enabled": True,
                "prepared": {},
                "written": {},
                "cleared": [],
                "below_threshold": {},
                "preserved_existing": {"sb_document_category": 11},
                "missing_fields": [],
                "unresolved_selects": {},
                "invalid_values": {},
                "api_errors": [],
            },
            secondbrain_ready_tag_id=999,
        )

        self.assertEqual(payload["tags"], [7, 999])

    def test_filter_unchanged_patch_fields_removes_equal_custom_fields(self) -> None:
        custom_field_id_to_definition = {
            101: DEFAULT_CUSTOM_FIELD_DEFINITIONS["contract_number"],
            102: DEFAULT_CUSTOM_FIELD_DEFINITIONS["monthly_cost"],
        }
        filtered = filter_unchanged_patch_fields(
            document={
                "document_type": None,
                "custom_fields": {
                    "Vertragsnummer": {"value": "V-12345"},
                    "Monatliche Aufwendungen": {"value": "EUR49.90"},
                },
            },
            patch_payload={
                "document_type": 5,
                "custom_fields": {
                    101: "V-12345",
                    102: "EUR49.90",
                },
            },
            custom_field_id_to_definition=custom_field_id_to_definition,
        )

        self.assertEqual(filtered, {"document_type": 5})

    def test_build_ai_note_entry_lists_secondbrain_fields(self) -> None:
        note = build_ai_note_entry(
            prediction={
                "summary": "Rechnung erkannt.",
                "rationale": "Rechnungsnummer und Betrag klar im Dokument.",
            },
            patch_payload={
                "custom_fields": {
                    201: 11,
                    204: "EUR49.90",
                }
            },
            doc_type_id_to_label={},
            correspondent_id_to_label={},
            storage_path_id_to_label={},
            tag_id_to_label={},
            custom_field_id_to_definition={
                201: SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS["sb_document_category"],
                204: SECOND_BRAIN_CUSTOM_FIELD_DEFINITIONS["sb_amount_total"],
            },
            secondbrain_sync_report={
                "written": {
                    "sb_document_category": {"value": "Rechnung"},
                    "sb_amount_total": {"value": "EUR49.90"},
                    "sb_confidence": {"value": "KI sicher"},
                }
            },
            max_chars=800,
            include_summary=True,
            summary_max_chars=220,
        )

        self.assertIn("Kurz-Zusammenfassung: Rechnung erkannt.", note)
        self.assertIn("SecondBrain-Felder:", note)
        self.assertIn("- sb_document_category: Rechnung", note)
        self.assertIn("- sb_confidence: KI sicher", note)


if __name__ == "__main__":
    unittest.main()
