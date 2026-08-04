"""Review helpers for Paperless entity duplicates and AI feedback.

Purpose:
- Find likely duplicate Paperless document types and correspondents by name.
- Persist human-reviewed alias/canonical decisions for later AI prompts.
- Keep merge/review decisions in a small JSON file that is safe to inspect.

Input / Output:
- Input: Paperless entity metadata (`id`, `name`, optional document count)
  and review actions from the worker UI.
- Output: duplicate candidates, stored review rules, and a compact prompt
  context that tells the classifier which existing entity to prefer.

Important invariants:
- This module never reads Paperless documents or secrets.
- It stores only entity IDs, entity names, timestamps, actions and optional
  user-provided context.
- Similarity suggestions are advisory. A merge must still be explicitly
  confirmed by the caller.

How to debug:
- Run `python3 -m unittest tests.test_entity_review`.
- Inspect the JSON rule file passed to `load_review_store`.
- Lower `min_similarity` to see more candidates, raise it to reduce noise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable


SUPPORTED_ENTITY_TYPES = {"document_type", "correspondent"}
STORE_VERSION = 1

_STOP_WORDS = {
    "am",
    "an",
    "bei",
    "das",
    "der",
    "die",
    "ein",
    "eine",
    "fuer",
    "fur",
    "im",
    "in",
    "und",
    "vom",
    "von",
    "zu",
    "zum",
    "zur",
}

_LEGAL_SUFFIXES = {
    "ag",
    "eg",
    "ev",
    "e.v",
    "gbr",
    "gmbh",
    "kg",
    "mbh",
    "ohg",
    "ug",
}

_TOKEN_ALIASES = {
    "abt": "abteilung",
    "bhv": "behoerde",
    "fa": "finanzamt",
    "finanzbehoerde": "finanzamt",
    "gerichtskasse": "gericht",
    "lra": "landratsamt",
    "ra": "rechtsanwalt",
    "rechtsanwaelte": "rechtsanwalt",
    "rechtsanwaltin": "rechtsanwalt",
    "str": "strasse",
}


@dataclass(frozen=True)
class EntityRecord:
    """Normalized Paperless entity metadata used by review matching."""

    entity_type: str
    entity_id: int
    name: str
    document_count: int = 0


@dataclass(frozen=True)
class EntityReviewCandidate:
    """One potential alias/canonical pair suggested for human review."""

    entity_type: str
    alias_id: int
    alias_name: str
    alias_document_count: int
    canonical_id: int
    canonical_name: str
    canonical_document_count: int
    similarity: float
    reason: str

    @property
    def pair_key(self) -> str:
        """Stable key independent of alias/canonical recommendation."""

        left, right = sorted((int(self.alias_id), int(self.canonical_id)))
        return f"{self.entity_type}:{left}:{right}"

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["pair_key"] = self.pair_key
        return payload


@dataclass(frozen=True)
class EntityReviewRule:
    """Human decision that should be reused by AI classification."""

    rule_id: str
    entity_type: str
    alias_id: int
    alias_name: str
    canonical_id: int
    canonical_name: str
    action: str
    context: str
    created_at: str
    updated_at: str

    @property
    def pair_key(self) -> str:
        left, right = sorted((int(self.alias_id), int(self.canonical_id)))
        return f"{self.entity_type}:{left}:{right}"

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["pair_key"] = self.pair_key
        return payload


def normalize_entity_name(value: str) -> str:
    """Return a comparison-safe entity name.

    Why this exists:
    - Paperless entities often differ only by punctuation, umlauts or legal
      suffixes.
    - The AI should learn from those cases without requiring exact spelling.

    Example:
    - Input: `Amtsgericht Osnabrück`
    - Output: `amtsgericht osnabruck`
    """

    raw = str(value or "").strip().casefold()
    raw = raw.replace("ß", "ss")
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = "".join(character for character in normalized if not unicodedata.combining(character))
    ascii_text = ascii_text.replace("&", " und ")
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def tokenize_entity_name(value: str) -> list[str]:
    """Tokenize an entity name for robust token-set matching."""

    normalized = normalize_entity_name(value)
    tokens: list[str] = []
    for token in normalized.split():
        mapped = _TOKEN_ALIASES.get(token, token)
        if mapped in _STOP_WORDS:
            continue
        tokens.append(mapped)
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return tokens


def _token_sort_key(value: str) -> str:
    return " ".join(sorted(tokenize_entity_name(value)))


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def entity_name_similarity(left: str, right: str) -> tuple[float, str]:
    """Score two entity labels and explain the strongest signal."""

    left_normalized = normalize_entity_name(left)
    right_normalized = normalize_entity_name(right)
    if not left_normalized or not right_normalized:
        return 0.0, "empty-name"
    if left_normalized == right_normalized:
        return 1.0, "normalized-exact"

    left_tokens = tokenize_entity_name(left)
    right_tokens = tokenize_entity_name(right)
    left_token_set = set(left_tokens)
    right_token_set = set(right_tokens)

    candidates: list[tuple[float, str]] = [
        (SequenceMatcher(None, left_normalized, right_normalized).ratio(), "name-similarity"),
        (SequenceMatcher(None, _token_sort_key(left), _token_sort_key(right)).ratio(), "token-order-insensitive"),
        (_jaccard(left_tokens, right_tokens), "token-overlap"),
    ]

    if left_token_set and right_token_set:
        smaller = min(len(left_token_set), len(right_token_set))
        larger = max(len(left_token_set), len(right_token_set))
        if smaller >= 2 and (
            left_token_set.issubset(right_token_set)
            or right_token_set.issubset(left_token_set)
        ):
            containment_score = 0.86 + min(0.1, smaller / max(larger, 1) * 0.1)
            candidates.append((containment_score, "token-contained"))

    score, reason = max(candidates, key=lambda item: item[0])
    return round(float(score), 4), reason


def _recommended_alias_and_canonical(left: EntityRecord, right: EntityRecord) -> tuple[EntityRecord, EntityRecord]:
    """Choose the likely duplicate and the likely canonical target."""

    left_score = (int(left.document_count), -len(left.name), -int(left.entity_id))
    right_score = (int(right.document_count), -len(right.name), -int(right.entity_id))
    if right_score > left_score:
        return left, right
    return right, left


def find_duplicate_candidates(
    records: Iterable[EntityRecord],
    *,
    min_similarity: float = 0.84,
    max_candidates: int = 250,
) -> list[EntityReviewCandidate]:
    """Build duplicate suggestions from Paperless entity names."""

    safe_min_similarity = max(0.0, min(1.0, float(min_similarity)))
    safe_max_candidates = max(1, int(max_candidates))
    items = [
        record
        for record in records
        if record.entity_type in SUPPORTED_ENTITY_TYPES and str(record.name or "").strip()
    ]
    candidates: list[EntityReviewCandidate] = []

    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            if left.entity_type != right.entity_type:
                continue
            similarity, reason = entity_name_similarity(left.name, right.name)
            if similarity < safe_min_similarity:
                continue
            alias, canonical = _recommended_alias_and_canonical(left, right)
            candidates.append(
                EntityReviewCandidate(
                    entity_type=left.entity_type,
                    alias_id=alias.entity_id,
                    alias_name=alias.name,
                    alias_document_count=alias.document_count,
                    canonical_id=canonical.entity_id,
                    canonical_name=canonical.name,
                    canonical_document_count=canonical.document_count,
                    similarity=similarity,
                    reason=reason,
                )
            )

    candidates.sort(
        key=lambda candidate: (
            -candidate.similarity,
            candidate.entity_type,
            candidate.canonical_name.casefold(),
            candidate.alias_name.casefold(),
        )
    )
    return candidates[:safe_max_candidates]


def build_rule_id(entity_type: str, alias_id: int, canonical_id: int, action: str) -> str:
    """Create a deterministic rule id without leaking any private content."""

    payload = f"{entity_type}:{int(alias_id)}:{int(canonical_id)}:{action}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def empty_review_store() -> dict[str, Any]:
    """Return the on-disk store shape used for first write and recovery."""

    return {
        "version": STORE_VERSION,
        "updated_at": None,
        "rules": [],
    }


def load_review_store(path: Path) -> dict[str, Any]:
    """Load review rules from disk and recover safely from missing files."""

    if not path.exists():
        return empty_review_store()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_review_store()
    if not isinstance(payload, dict):
        return empty_review_store()
    rules = payload.get("rules")
    if not isinstance(rules, list):
        rules = []
    return {
        "version": int(payload.get("version") or STORE_VERSION),
        "updated_at": payload.get("updated_at"),
        "rules": [rule for rule in rules if isinstance(rule, dict)],
    }


def save_review_store(path: Path, store: dict[str, Any]) -> None:
    """Persist the review rule store with stable formatting."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STORE_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "rules": list(store.get("rules") or []),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_review_rule(
    path: Path,
    *,
    entity_type: str,
    alias_id: int,
    alias_name: str,
    canonical_id: int,
    canonical_name: str,
    action: str,
    context: str = "",
) -> EntityReviewRule:
    """Insert or update one user decision for AI prompt reuse."""

    normalized_entity_type = str(entity_type or "").strip()
    normalized_action = str(action or "prefer").strip() or "prefer"
    if normalized_entity_type not in SUPPORTED_ENTITY_TYPES:
        raise ValueError(f"Nicht unterstützter Entity-Typ: {entity_type}")
    safe_alias_id = int(alias_id)
    safe_canonical_id = int(canonical_id)
    if safe_alias_id == safe_canonical_id:
        raise ValueError("Alias und Ziel dürfen nicht identisch sein.")

    now = datetime.now(UTC).isoformat()
    rule_id = build_rule_id(normalized_entity_type, safe_alias_id, safe_canonical_id, normalized_action)
    store = load_review_store(path)
    existing_rules = list(store.get("rules") or [])
    created_at = now
    kept_rules: list[dict[str, Any]] = []
    for raw_rule in existing_rules:
        if not isinstance(raw_rule, dict):
            continue
        if str(raw_rule.get("rule_id") or "") == rule_id:
            created_at = str(raw_rule.get("created_at") or now)
            continue
        kept_rules.append(raw_rule)

    rule = EntityReviewRule(
        rule_id=rule_id,
        entity_type=normalized_entity_type,
        alias_id=safe_alias_id,
        alias_name=str(alias_name or "").strip(),
        canonical_id=safe_canonical_id,
        canonical_name=str(canonical_name or "").strip(),
        action=normalized_action,
        context=str(context or "").strip()[:1200],
        created_at=created_at,
        updated_at=now,
    )
    kept_rules.append(rule.to_payload())
    kept_rules.sort(
        key=lambda item: (
            str(item.get("entity_type") or ""),
            str(item.get("canonical_name") or "").casefold(),
            str(item.get("alias_name") or "").casefold(),
            str(item.get("action") or ""),
        )
    )
    store["rules"] = kept_rules
    save_review_store(path, store)
    return rule


def review_rules_from_store(store: dict[str, Any]) -> list[EntityReviewRule]:
    """Convert raw JSON rule dictionaries into typed rules."""

    typed_rules: list[EntityReviewRule] = []
    for raw_rule in store.get("rules") or []:
        if not isinstance(raw_rule, dict):
            continue
        try:
            typed_rules.append(
                EntityReviewRule(
                    rule_id=str(raw_rule.get("rule_id") or ""),
                    entity_type=str(raw_rule.get("entity_type") or ""),
                    alias_id=int(raw_rule.get("alias_id")),
                    alias_name=str(raw_rule.get("alias_name") or ""),
                    canonical_id=int(raw_rule.get("canonical_id")),
                    canonical_name=str(raw_rule.get("canonical_name") or ""),
                    action=str(raw_rule.get("action") or "prefer"),
                    context=str(raw_rule.get("context") or ""),
                    created_at=str(raw_rule.get("created_at") or ""),
                    updated_at=str(raw_rule.get("updated_at") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    return typed_rules


def filter_candidates_by_rules(
    candidates: Iterable[EntityReviewCandidate],
    rules: Iterable[EntityReviewRule],
) -> list[EntityReviewCandidate]:
    """Hide pairs that the user already classified as ignored."""

    ignored_pairs = {rule.pair_key for rule in rules if rule.action == "ignore"}
    return [candidate for candidate in candidates if candidate.pair_key not in ignored_pairs]


def build_ai_prompt_context(rules: Iterable[EntityReviewRule], *, max_rules: int = 200) -> str:
    """Render human review rules as compact high-priority AI instructions."""

    usable_rules = [
        rule
        for rule in rules
        if rule.action in {"prefer", "merge"} and rule.alias_name and rule.canonical_name
    ][: max(1, int(max_rules))]
    if not usable_rules:
        return ""

    lines = [
        "Nutze die folgenden geprüften Paperless-Zuordnungsregeln. "
        "Wenn Alias und Ziel sinngleich passen, bevorzuge immer den Zielwert "
        "und lege keinen neuen Dokumenttyp/Korrespondenten dafür an.",
    ]
    for rule in usable_rules:
        label = "Dokumenttyp" if rule.entity_type == "document_type" else "Korrespondent"
        context = f" Kontext: {rule.context}" if rule.context else ""
        lines.append(
            f"- {label}: `{rule.alias_name}` ist sinngleich zu `{rule.canonical_name}`; "
            f"verwende `{rule.canonical_name}`.{context}"
        )
    return "\n".join(lines)
