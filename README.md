# Paperless KIplus Home Assistant Integration

Die Integration verbindet Home Assistant mit deinem Paperless-ngx-Workflow und klassifiziert Dokumente per KI automatisiert.

## Was macht die Integration?

Die Integration startet den KI-Sorter direkt aus Home Assistant, schreibt Ergebnisse zurück nach Paperless-ngx und stellt Laufstatus, Kosten und Logs als Entitäten/Buttons bereit.

### Bilder

#### Geräteansicht in Home Assistant
![Home Assistant Geräteansicht](./docs/images/ha-geraeteansicht.png)

#### Dokument mit KI-Notiz in Paperless-ngx
![Paperless Dokumentansicht mit KI-Notiz](./docs/images/paperless-ki-notiz.png)

#### Optionen in Home Assistant (Teil 1)
![Home Assistant Optionen Teil 1](./docs/images/ha-optionen-teil1.png)

#### Optionen in Home Assistant (Teil 2)
![Home Assistant Optionen Teil 2](./docs/images/ha-optionen-teil2.png)

## Wie installiere ich die Integration?

1. HACS öffnen -> `Integrationen` -> `Custom repositories`.
2. Repository hinzufügen:
   - URL: `https://github.com/Feberdin/Paperless-KIplus`
   - Kategorie: `Integration`
3. `Paperless KIplus Runner` installieren.
4. Home Assistant neu starten.
5. Unter `Einstellungen -> Geräte & Dienste` die Integration hinzufügen.
6. In den Optionen deine YAML-Konfiguration vollständig im YAML-Feld pflegen.
   Alternative mit ChatGPT:
   - Nutze den folgenden Prompt, um dir eine vollständige YAML erstellen zu lassen.
   - Ergebnis 1:1 in das YAML-Feld der Integration kopieren.

```text
Erstelle mir eine vollständige YAML-Konfiguration für die Home-Assistant Integration
"Paperless KIplus Runner" (Paperless-ngx KI-Sorter).

Ziel:
- Dokumente in Paperless-ngx per KI klassifizieren (Dokumenttyp, Korrespondent,
  Speicherpfad, Tags, Datum, Notiz).
- Sicherer Betrieb in Home Assistant mit Fokus auf stabile Automationen.

Wichtige Anforderungen:
1) Gib nur gültiges YAML aus (ohne Markdown, ohne Erklärtext).
2) Gib alle unten genannten Felder vollständig aus, auch wenn du Defaultwerte nutzt.
3) Setze process_only_tag auf "#NEU".
4) Setze dry_run auf false.
5) Setze reprocess_ki_tagged_documents auf false.
6) Konfiguriere bereits klassifizierte Dokumente so, dass sie zuverlässig übersprungen werden.
7) Aktiviere Quarantäne- und Duplicate-Prechecks.
8) Aktiviere parallele KI-Verarbeitung moderat (3 bis 5 Jobs).
9) Nutze sinnvolle produktive Defaultwerte.

Pflicht-Platzhalter:
- paperless_url: <PAPERLESS_URL>
- paperless_token: <PAPERLESS_TOKEN>
- ai_api_key: <AI_API_KEY>
- ai_model: <AI_MODEL>
- ai_base_url: <AI_BASE_URL>

Die YAML muss diese Felder enthalten:
- paperless_url
- paperless_token
- ai_api_key
- ai_model
- ai_base_url
- max_documents
- dry_run
- create_missing_entities
- confidence_threshold
- request_timeout_seconds
- log_level
- enable_token_precheck
- min_remaining_tokens
- custom_prompt_instructions
- basis_config
- process_only_tag
- include_existing_entities_in_prompt
- enable_ai_notes
- ai_notes_max_chars
- enable_ai_note_summary
- ai_note_summary_max_chars
- metrics_file
- input_cost_per_1k_tokens_eur
- output_cost_per_1k_tokens_eur
- quarantine_failed_documents
- failed_document_cooldown_hours
- failed_documents_file
- failed_tags_only_cooldown_hours
- failed_patch_cache_file
- enable_tag_bypass_on_tags_500
- tag_bypass_file
- already_classified_skip
- already_classified_require_ki_tag
- precheck_min_content_chars
- precheck_min_word_count
- precheck_min_alnum_ratio
- precheck_blocked_filename_patterns
- precheck_image_only_gate
- precheck_duplicate_hash_gate
- precheck_duplicate_apply_metadata
- reprocess_ki_tagged_documents
- enable_parallel_ai
- max_parallel_ai_jobs

Die `basis_config` muss mindestens diese Struktur enthalten (Feldnamen exakt so verwenden):

basis_config:
  people:
    owner:
      full_name: "Max Mustermann"
      aliases: []
      address:
        street: "Musterstraße 1"
        postal_code: "12345"
        city: "Musterstadt"
      contact:
        mobile: "0123456789"
      tax:
        tax_number: ""
    household:
      children: []
      relatives: []
    contacts: []
  organizations:
    employer_current:
      name: ""
      preferred_storage_path: ""
    employer_former:
      name: ""
      locations: []
      preferred_storage_path: ""
      only_if_clear_business_context: true
    clubs: []
  identifiers:
    meters: []
  classification_rules:
    document_type:
      invoice_addressed_to_owner: "Rechnung"
      legal_documents_force_type:
        type: "Rechtsanwalt"
        trigger_terms: ["Rechtsanwalt", "Gericht", "Klage", "Beschluss", "Einspruch", "Aktenzeichen"]
    correspondent:
      normalize:
        - if_contains_any: ["Hotel", "Pension", "Unterkunft", "Übernachtung"]
          set_to: "Hotel"
    storage_path:
      mappings: []
      default: "Privat"
    tags:
      add_year_tag_for_invoices: true
      add_customer_number_tag_for_contracts: true
      legal_case_tag_prefers_case_reference: true
      keep_sparse: true
    date:
      prefer_document_date_over_upload_date: true
  guardrails:
    forbidden_path_assignments: []

Rahmendaten:
- Paperless URL: <PAPERLESS_URL>
- Paperless Token: <PAPERLESS_TOKEN>
- AI API Key: <AI_API_KEY>
- AI Modell: <AI_MODEL>
- AI Base URL (optional): <AI_BASE_URL>

Erzeuge jetzt die vollständige YAML.
```

## Welche Features hat die Integration?

- Native Home-Assistant Integration mit Config Flow und Options-UI
- KI-gestützte Dokumentklassifizierung für:
  - Dokumenttyp
  - Korrespondent
  - Speicherpfad
  - Tags
  - Dokumentdatum
- Optionales Auto-Anlegen fehlender Entitäten (Korrespondent, Dokumenttyp, Tags)
- Dry-Run Modus ohne Schreibzugriffe in Paperless
- Vollscan-Modus (`Alle Dokumente`) für Bestandsläufe
- Precheck-/Skip-Logik zur Token-Einsparung
- Doppelte Dokumente per Checksum erkennen (optional Metadatenübernahme)
- Fehler-Quarantäne und Tag-Bypass für robuste Dauerläufe
- KI-Notizen inkl. Begründung/Kurz-Zusammenfassung
- Token-/Kosten-Tracking (letzter Lauf + Gesamtwerte)
- Service `paperless_kiplus.run` mit Overrides (`force`, `wait`, `dry_run`, `all_documents`, `max_documents`)
- Geräte-Buttons für:
  - Letztes Protokoll anzeigen
  - Letztes Protokoll exportieren
  - Statistiken zurücksetzen
  - Fehlgeschlagene Dokumente zurücksetzen
- Parallele KI-Verarbeitung (konfigurierbar)
- KI-Tag-Vorfilter: KI-getaggte Dokumente können standardmäßig komplett ausgeschlossen werden

## Versionsverlauf (antichronologisch)

- `v1.0.0` (2026-03-08)
  - Erstes stabiles Release für HACS.

- `v0.1.49` (2026-03-08)
  - KI-Tag-Vorfilter vor der Abarbeitung ergänzt.
  - Performance-Metriken im Log ergänzt (KI-Batches/Zeiten).

- `v0.1.48` (2026-03-06)
  - `max_documents` zählt übersprungene Dokumente nicht mehr als Verarbeitungsbudget.

- `v0.1.47` (2026-03-06)
  - Option `reprocess_ki_tagged_documents` eingeführt (Default AUS).

- `v0.1.46` (2026-03-03)
  - `already_classified_skip` im All-Documents-Verhalten nachgeschärft.

- `v0.1.45` (2026-03-03)
  - Tag-Sanitizer, KI-Retry-Backoff und robustere PATCH-Fallbacks.

- `v0.1.44` (2026-03-03)
  - Parallele KI-Verarbeitung mit konfigurierbarer Worker-Anzahl.

- Ältere Releases
  - Weitere Tags vorhanden: `v0.1.43` bis `v0.1.2`.
