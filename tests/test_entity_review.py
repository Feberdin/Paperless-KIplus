"""Tests for Paperless entity duplicate review helpers.

Purpose:
- Protect similarity matching for document types and correspondents.
- Verify that user review decisions are written as reusable AI prompt rules.
- Keep the duplicate-review logic testable without a live Paperless server.

How to run:
- `python3 -m unittest tests.test_entity_review`
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from entity_review import (  # noqa: E402
    EntityRecord,
    build_ai_prompt_context,
    filter_candidates_by_rules,
    find_duplicate_candidates,
    load_review_store,
    review_rules_from_store,
    upsert_review_rule,
)


class EntityReviewTests(unittest.TestCase):
    """Covers local duplicate detection and AI feedback persistence."""

    def test_find_duplicate_candidates_handles_umlaut_variants(self) -> None:
        records = [
            EntityRecord("correspondent", 10, "Amtsgericht Osnabrück", 42),
            EntityRecord("correspondent", 11, "Amtsgericht Osnabrueck", 2),
            EntityRecord("correspondent", 12, "Stadtwerke Musterstadt", 17),
        ]

        candidates = find_duplicate_candidates(records, min_similarity=0.80)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].canonical_id, 10)
        self.assertEqual(candidates[0].alias_id, 11)
        self.assertGreaterEqual(candidates[0].similarity, 0.80)

    def test_review_rule_is_persisted_and_rendered_for_ai_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            rules_path = Path(tmp_dir) / "rules.json"

            rule = upsert_review_rule(
                rules_path,
                entity_type="document_type",
                alias_id=50,
                alias_name="Rechtsanwalt",
                canonical_id=7,
                canonical_name="Recht",
                action="prefer",
                context="Gerichtliche Schreiben sollen nicht als neuer Typ Rechtsanwalt angelegt werden.",
            )
            rules = review_rules_from_store(load_review_store(rules_path))
            prompt = build_ai_prompt_context(rules)

            self.assertEqual(rule.alias_name, "Rechtsanwalt")
            self.assertIn("Rechtsanwalt", prompt)
            self.assertIn("Recht", prompt)
            self.assertIn("lege keinen neuen Dokumenttyp", prompt)

    def test_ignore_rule_hides_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            rules_path = Path(tmp_dir) / "rules.json"
            candidate = find_duplicate_candidates(
                [
                    EntityRecord("document_type", 1, "Bescheid", 10),
                    EntityRecord("document_type", 2, "Bescheide", 1),
                ],
                min_similarity=0.70,
            )[0]
            upsert_review_rule(
                rules_path,
                entity_type="document_type",
                alias_id=candidate.alias_id,
                alias_name=candidate.alias_name,
                canonical_id=candidate.canonical_id,
                canonical_name=candidate.canonical_name,
                action="ignore",
            )

            rules = review_rules_from_store(load_review_store(rules_path))
            visible_candidates = filter_candidates_by_rules([candidate], rules)

            self.assertEqual(visible_candidates, [])


if __name__ == "__main__":
    unittest.main()
