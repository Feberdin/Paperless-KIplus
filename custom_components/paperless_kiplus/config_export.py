"""Helpers for exporting the effective managed YAML configuration.

Purpose:
- Build the exact runtime YAML that Paperless KIplus should execute with.
- Reuse the same normalization for local Home Assistant runs and remote worker
  exports so both execution modes stay in sync.

Input / Output:
- Input: Raw managed YAML from Home Assistant plus UI-level override values.
- Output: A normalized YAML string or config mapping that can be written to
  disk or sent to a remote worker.

Important invariants:
- The managed YAML remains the source of truth for all detailed sorter options.
- Home Assistant option fields that intentionally override parts of the YAML
  must always win over the embedded YAML values.
- The helper must stay pure so it is easy to test and debug.

How to debug:
- Use `build_effective_managed_config_payload()` and inspect the returned dict.
- If local and remote behavior differ, compare the exported YAML text directly.
"""

from __future__ import annotations

from typing import Any

import yaml


def _parse_bool(value: Any, default: bool = False) -> bool:
    """Convert loosely typed Home Assistant option values to bool."""

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


def _parse_float(value: Any, default: float) -> float:
    """Convert Home Assistant option values robustly to float."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int(value: Any, default: int) -> int:
    """Convert Home Assistant option values robustly to int."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_effective_managed_config_payload(
    raw_yaml: str,
    *,
    input_cost_per_1k_tokens_eur: float,
    output_cost_per_1k_tokens_eur: float,
    already_classified_skip: bool,
    already_classified_require_ki_tag: bool,
    precheck_min_content_chars: int,
    precheck_min_word_count: int,
    precheck_min_alnum_ratio: float,
    precheck_blocked_filename_patterns: str,
    precheck_image_only_gate: bool,
    precheck_duplicate_hash_gate: bool,
    precheck_duplicate_apply_metadata: bool,
    reprocess_ki_tagged_documents: bool,
    enable_parallel_ai: bool,
    max_parallel_ai_jobs: int,
    enable_tax_enrichment: bool,
    tax_process_ki_tagged_documents: bool,
    tax_personal_context: str,
) -> dict[str, Any]:
    """Build the exact runtime config mapping from managed YAML and UI overrides.

    Why this exists:
    - The Home Assistant UI already exposes a few important safety toggles that
      intentionally override the pasted YAML.
    - A remote worker must receive the same effective config as the local
      process runner, otherwise behavior drifts silently.
    """

    parsed = yaml.safe_load(raw_yaml) or {}
    if not isinstance(parsed, dict):
        raise ValueError("managed_config_yaml muss ein YAML-Objekt sein.")

    parsed["input_cost_per_1k_tokens_eur"] = _parse_float(
        input_cost_per_1k_tokens_eur,
        0.0004,
    )
    parsed["output_cost_per_1k_tokens_eur"] = _parse_float(
        output_cost_per_1k_tokens_eur,
        0.0016,
    )
    parsed["already_classified_skip"] = _parse_bool(already_classified_skip, True)
    parsed["already_classified_require_ki_tag"] = _parse_bool(
        already_classified_require_ki_tag,
        True,
    )
    parsed["precheck_min_content_chars"] = _parse_int(precheck_min_content_chars, 120)
    parsed["precheck_min_word_count"] = _parse_int(precheck_min_word_count, 20)
    parsed["precheck_min_alnum_ratio"] = _parse_float(precheck_min_alnum_ratio, 0.40)
    parsed["precheck_blocked_filename_patterns"] = [
        part.strip()
        for part in str(precheck_blocked_filename_patterns).split(",")
        if part.strip()
    ]
    parsed["precheck_image_only_gate"] = _parse_bool(precheck_image_only_gate, True)
    parsed["precheck_duplicate_hash_gate"] = _parse_bool(precheck_duplicate_hash_gate, True)
    parsed["precheck_duplicate_apply_metadata"] = _parse_bool(
        precheck_duplicate_apply_metadata,
        True,
    )
    parsed["reprocess_ki_tagged_documents"] = _parse_bool(
        reprocess_ki_tagged_documents,
        False,
    )
    parsed["enable_parallel_ai"] = _parse_bool(enable_parallel_ai, False)
    parsed["max_parallel_ai_jobs"] = max(1, _parse_int(max_parallel_ai_jobs, 5))
    parsed["enable_tax_enrichment"] = _parse_bool(enable_tax_enrichment, False)
    parsed["tax_process_ki_tagged_documents"] = _parse_bool(
        tax_process_ki_tagged_documents,
        False,
    )
    parsed["tax_personal_context"] = str(tax_personal_context or "")

    return parsed


def build_effective_managed_config_yaml(raw_yaml: str, **kwargs: Any) -> str:
    """Return the effective managed config as human-readable YAML."""

    payload = build_effective_managed_config_payload(raw_yaml, **kwargs)
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
