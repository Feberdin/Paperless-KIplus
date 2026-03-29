"""Tests for the tax enrichment module.

Purpose:
- Protect the fixed taxonomy, WISO mapping layer, evidence validation, and
  exports against regressions.

How to run:
- `python3 -m unittest discover -s tests`
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


import sys


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tax_enrichment import (
    TaxEnrichmentProcessor,
    TaxExportCollector,
    build_tax_tag_labels,
    resolve_wiso_mapping,
)


class TaxEnrichmentTests(unittest.TestCase):
    """Covers productive tax enrichment building blocks."""

    def setUp(self) -> None:
        self.processor = TaxEnrichmentProcessor(
            basis_config={
                "people": {
                    "owner": {"full_name": "Max Mustermann"},
                    "household": {"children": [{"name": "Erika Mustermann"}]},
                }
            }
        )

    def test_mapping_layer_for_household_service(self) -> None:
        mapping = resolve_wiso_mapping("haushaltsnahe_dienstleistungen", "reinigung")
        self.assertEqual(
            mapping["wiso_target_area"],
            "Steuerermaessigungen > Haushaltsnahe Dienstleistungen",
        )
        self.assertTrue(mapping["likely_manual_review"])
        self.assertIn("Rechnung", mapping["required_supporting_docs"])

    def test_handwerker_without_labor_split_gets_review_flags(self) -> None:
        enrichment = self.processor.build_tax_enrichment(
            document={
                "id": 1001,
                "title": "Handwerkerrechnung Bad",
                "created": "2025-05-10",
                "content": "Rechnung fuer Reparatur im Bad, ueberwiesen am 12.05.2025.",
            },
            payload={
                "tax_year": 2025,
                "document_date": "2025-05-10",
                "document_type": "Rechnung",
                "issuer": "Malerbetrieb GmbH",
                "recipient": "Max Mustermann",
                "total_amount": "450,00",
                "currency": "EUR",
                "payment_method": "bank_transfer",
                "payment_verified": True,
                "evidence_type": "rechnung",
                "tax_category": "handwerkerleistungen",
                "tax_subcategory": "handwerker_lohnkosten",
                "classification_confidence": 0.91,
                "eligibility_confidence": 0.82,
                "reasoning_summary": "Handwerkerrechnung erkannt.",
                "extracted_evidence": {
                    "invoice_present": True,
                    "issuer_identified": True,
                    "recipient_identified": True,
                    "service_description_present": True,
                    "service_period_present": True,
                    "amount_present": True,
                    "payment_method_identified": True,
                    "unbare_payment_evidence": True,
                    "labor_material_split": False,
                },
            },
        )
        self.assertEqual(enrichment.formal_validity, "incomplete")
        self.assertIn("missing_labor_split", enrichment.flags)
        self.assertIn("Lohn-/Materialtrennung fehlt", enrichment.missing_requirements)

    def test_kita_without_payment_proof_needs_follow_up(self) -> None:
        enrichment = self.processor.build_tax_enrichment(
            document={
                "id": 1002,
                "title": "Kita-Beitrag April 2025",
                "created": "2025-04-02",
                "content": "Monatsbeitrag Kinderbetreuung fuer Erika Mustermann.",
            },
            payload={
                "tax_year": 2025,
                "document_date": "2025-04-01",
                "service_period_from": "2025-04-01",
                "service_period_to": "2025-04-30",
                "document_type": "Rechnung",
                "issuer": "Kita Sonnenschein",
                "recipient": "Max Mustermann",
                "total_amount": "320.00",
                "currency": "EUR",
                "payment_method": "sepa_direct_debit",
                "payment_verified": False,
                "evidence_type": "rechnung",
                "tax_category": "kinderbetreuungskosten",
                "tax_subcategory": "kita",
                "child_reference": "Erika Mustermann",
                "classification_confidence": 0.93,
                "eligibility_confidence": 0.71,
                "reasoning_summary": "Kinderbetreuungskosten erkannt.",
                "extracted_evidence": {
                    "invoice_present": True,
                    "issuer_identified": True,
                    "recipient_identified": True,
                    "service_description_present": True,
                    "service_period_present": True,
                    "amount_present": True,
                    "payment_method_identified": True,
                    "unbare_payment_evidence": False,
                    "labor_material_split": None,
                },
            },
        )
        self.assertIn("needs_payment_proof", enrichment.flags)
        self.assertIn("Unbarer Zahlungsnachweis fuer Kinderbetreuung fehlt", enrichment.missing_requirements)
        self.assertIn(
            "Kontoauszug oder Lastschriftbeleg fuer Kinderbetreuung ergänzen",
            enrichment.recommended_follow_up,
        )

    def test_apotheke_receipt_maps_to_medical_category(self) -> None:
        enrichment = self.processor.build_tax_enrichment(
            document={
                "id": 1003,
                "title": "Apothekenbeleg",
                "created": "2025-01-12",
                "content": "Apotheke Musterstadt Medikamentenbeleg.",
            },
            payload={
                "tax_year": 2025,
                "document_date": "2025-01-12",
                "document_type": "Beleg",
                "issuer": "Apotheke Musterstadt",
                "recipient": "Max Mustermann",
                "total_amount": "18,99",
                "currency": "EUR",
                "payment_method": "card",
                "payment_verified": True,
                "evidence_type": "rechnung",
                "tax_category": "aussergewoehnliche_belastungen",
                "tax_subcategory": "apotheke",
                "classification_confidence": 0.85,
                "eligibility_confidence": 0.68,
                "reasoning_summary": "Apothekenbeleg erkannt.",
                "extracted_evidence": {
                    "invoice_present": True,
                    "issuer_identified": True,
                    "recipient_identified": True,
                    "service_description_present": True,
                    "service_period_present": True,
                    "amount_present": True,
                    "payment_method_identified": True,
                    "unbare_payment_evidence": True,
                    "labor_material_split": None,
                },
            },
        )
        self.assertEqual(
            enrichment.wiso_target_area,
            "Aussergewoehnliche Belastungen > Krankheitskosten",
        )
        self.assertIn("needs_review", enrichment.flags)

    def test_not_tax_relevant_document_gets_flag(self) -> None:
        enrichment = self.processor.build_tax_enrichment(
            document={
                "id": 1004,
                "title": "Streaming-Abo Info",
                "created": "2025-02-10",
                "content": "Information zu deinem Unterhaltungspaket.",
            },
            payload={
                "tax_year": 2025,
                "document_date": "2025-02-10",
                "document_type": "Information",
                "issuer": "Streaming GmbH",
                "recipient": "Max Mustermann",
                "currency": "EUR",
                "payment_method": "card",
                "payment_verified": True,
                "evidence_type": "information",
                "tax_category": "nicht_steuerrelevant",
                "classification_confidence": 0.96,
                "eligibility_confidence": 0.10,
                "reasoning_summary": "Privates Unterhaltungsdokument.",
                "extracted_evidence": {},
            },
        )
        self.assertEqual(enrichment.formal_validity, "unknown")
        self.assertIn("not_tax_relevant", enrichment.flags)
        self.assertEqual(build_tax_tag_labels(enrichment), ["KI nicht Steuerrelevant"])

    def test_relevant_document_gets_year_tag_label(self) -> None:
        enrichment = self.processor.build_tax_enrichment(
            document={
                "id": 1006,
                "title": "Kinderbetreuung Mai 2025",
                "created": "2025-05-02",
                "content": "Kita-Rechnung",
            },
            payload={
                "tax_year": 2025,
                "document_date": "2025-05-01",
                "service_period_from": "2025-05-01",
                "service_period_to": "2025-05-31",
                "document_type": "Rechnung",
                "issuer": "Kita Sonnenschein",
                "recipient": "Max Mustermann",
                "total_amount": "310.00",
                "currency": "EUR",
                "payment_method": "sepa_direct_debit",
                "payment_verified": True,
                "evidence_type": "rechnung",
                "tax_category": "kinderbetreuungskosten",
                "tax_subcategory": "kita",
                "child_reference": "Erika Mustermann",
                "classification_confidence": 0.91,
                "eligibility_confidence": 0.83,
                "reasoning_summary": "Kita-Rechnung erkannt.",
                "extracted_evidence": {
                    "invoice_present": True,
                    "issuer_identified": True,
                    "recipient_identified": True,
                    "service_description_present": True,
                    "service_period_present": True,
                    "amount_present": True,
                    "payment_method_identified": True,
                    "unbare_payment_evidence": True,
                    "labor_material_split": None,
                },
            },
        )
        self.assertEqual(build_tax_tag_labels(enrichment), ["KI Steuerrelevant 2025"])

    def test_export_json_and_csv(self) -> None:
        collector = TaxExportCollector(
            basis_config={"people": {"owner": {"full_name": "Max Mustermann"}}},
            export_years=[2025],
        )
        collector.add(
            self.processor.build_tax_enrichment(
                document={
                    "id": 1005,
                    "title": "Arbeitsmittel Maus",
                    "created": "2025-03-05",
                    "content": "Rechnung Computermaus",
                },
                payload={
                    "tax_year": 2025,
                    "document_date": "2025-03-05",
                    "document_type": "Rechnung",
                    "issuer": "Tech Shop",
                    "recipient": "Max Mustermann",
                    "total_amount": "49.90",
                    "currency": "EUR",
                    "payment_method": "card",
                    "payment_verified": True,
                    "evidence_type": "rechnung",
                    "tax_category": "werbungskosten",
                    "tax_subcategory": "arbeitsmittel",
                    "classification_confidence": 0.88,
                    "eligibility_confidence": 0.84,
                    "reasoning_summary": "Arbeitsmittel erkannt.",
                    "extracted_evidence": {
                        "invoice_present": True,
                        "issuer_identified": True,
                        "recipient_identified": True,
                        "service_description_present": True,
                        "service_period_present": True,
                        "amount_present": True,
                        "payment_method_identified": True,
                        "unbare_payment_evidence": True,
                        "labor_material_split": None,
                    },
                },
            )
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            created = collector.write_exports(Path(tmp_dir))
            self.assertEqual(len(created), 2)
            json_path = Path(tmp_dir) / "2025" / "tax_export.json"
            csv_path = Path(tmp_dir) / "2025" / "tax_review.csv"
            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["tax_year"], 2025)
            self.assertEqual(payload["taxpayer"]["name"], "Max Mustermann")
            self.assertEqual(len(payload["documents"]), 1)
            self.assertEqual(payload["category_totals"][0]["tax_category"], "werbungskosten")

            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertIn("document_id,title,document_date", csv_text)
            self.assertIn("Arbeitsmittel Maus", csv_text)


if __name__ == "__main__":
    unittest.main()
