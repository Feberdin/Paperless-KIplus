# Paperless KIplus Home Assistant Integration

Die Integration verbindet Home Assistant mit deinem Paperless-ngx-Workflow und klassifiziert Dokumente per KI automatisiert.

## Was macht die Integration?

Die Integration startet den KI-Sorter direkt aus Home Assistant, schreibt Ergebnisse zurück nach Paperless-ngx und stellt Laufstatus, Kosten und Logs als Entitäten/Buttons bereit.

Zusätzlich kann die Integration optional ein steuerorientiertes `Tax Enrichment`
pro Dokument erzeugen. Diese Erweiterung richtet Dokumente auf private deutsche
Einkommensteuerfälle aus, bewertet Nachweisqualität vorsichtig und erzeugt
arbeitbare JSON-/CSV-Exporte für die manuelle Übernahme nach WISO Steuer.
Wenn du diese Funktion nicht möchtest, bleibt sie mit `enable_tax_enrichment: false`
komplett ausgeschaltet.

Neu dazugekommen:

- eigenständiger Docker-/Unraid-Worker mit Weboberfläche und JSON-API
- Remote-Ausführungsmodus in Home Assistant (`local` oder `remote_worker`)
- Konfig-Export aus Home Assistant direkt zum Worker
- optionaler eigener Tax-KI-Provider, z. B. lokales Ollama/vLLM für kleinere Aufgaben

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
- enable_custom_field_enrichment
- create_missing_custom_fields
- enable_secondbrain_custom_fields
- secondbrain_custom_fields_overwrite_existing
- secondbrain_custom_fields_attach_empty_when_unknown
- secondbrain_custom_fields_confidence_threshold
- secondbrain_custom_fields_log_missing_fields
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
- Optionales Custom-Field-Enrichment für strukturierte Vertrags- und Lohnfelder
- Token-/Kosten-Tracking (letzter Lauf + Gesamtwerte)
- Services:
  - `paperless_kiplus.run`
  - `paperless_kiplus.stop`
  - `paperless_kiplus.stop_now`
  - `paperless_kiplus.resume`
- Geräte-Buttons für:
  - Bestandsdaten neu anreichern
  - Lauf pausieren
  - Lauf sofort stoppen
  - Pausierten Lauf fortsetzen
  - Letztes Protokoll anzeigen
  - Letztes Protokoll exportieren
  - Statistiken zurücksetzen
  - Fehlgeschlagene Dokumente zurücksetzen
- Parallele KI-Verarbeitung (konfigurierbar)
- KI-Tag-Vorfilter: KI-getaggte Dokumente können standardmäßig komplett ausgeschlossen werden
- Echte Live-Fortschrittsanzeige:
  - Prozent-Fortschritt
  - aktueller Dokumenttitel
  - laufende Zähler für Gescannt / Aktualisiert / Übersprungen / Fehler
- Kontrolliertes Pausieren und Fortsetzen:
  - manuell per Button oder Service
  - automatisch bei Provider-Wartezeiten (`429`, `Retry-After`, `insufficient_quota`)
- Optionales Tax Enrichment für Einkommensteuer-Vorbereitung:
  - feste Steuer-Taxonomie
  - semantischer WISO-Mapping-Layer
  - Review-Flags und Confidence-Werte
  - formale Nachweisprüfung
  - Exporte als `tax_export.json` und `tax_review.csv`
  - steuerliche Ergebnis-Tags in Paperless
  - eigene UI-Optionen für Steuer-Kontext und Tax-only-Nachlauf

## Custom Field Enrichment

### Ziel

Zusätzlich zur normalen Klassifikation kann die Integration strukturierte
Paperless-Custom-Fields befüllen. Es gibt dafür jetzt zwei getrennte Wege:

- `enable_custom_field_enrichment` für den kleinen festen Standard-Katalog
  (Vertrag / Lohnabrechnung)
- `enable_secondbrain_custom_fields` für bereits in Paperless angelegte
  `sb_`-Felder, die von `SecondBrain` später strukturiert ausgelesen werden
- automatischer Tag `SB`, sobald ein Dokument als SecondBrain-vorbereitet gilt

### SecondBrain `sb_`-Felder

Die vollständige technische Consumer-Schnittstelle für `SecondBrain` steht in:

- [docs/secondbrain-interface.md](./docs/secondbrain-interface.md)

#### Aktivierung

```yaml
enable_secondbrain_custom_fields: true
secondbrain_custom_fields_overwrite_existing: false
secondbrain_custom_fields_attach_empty_when_unknown: false
secondbrain_custom_fields_confidence_threshold: 0.70
secondbrain_custom_fields_log_missing_fields: true
```

Alternativ gruppiert:

```yaml
secondbrain_custom_fields:
  enabled: true
  overwrite_existing: false
  attach_empty_when_unknown: false
  confidence_threshold: 0.70
  log_missing_fields: true
```

#### Voraussetzungen

- Paperless-ngx mit Custom-Field-Support
- die `sb_`-Felder müssen bereits in Paperless existieren
- ein API-Benutzer mit Rechten auf `CustomField` und `Document`

Wichtig:

- Fehlende `sb_`-Felder brechen den Lauf nicht ab. Sie werden geloggt und
  übersprungen.
- Select-Felder werden nicht über hart codierte IDs beschrieben. Die
  sichtbaren Labels aus der KI-Ausgabe werden zur Laufzeit gegen die in
  Paperless hinterlegten Select-Optionen aufgelöst.
- Bestehende Werte werden standardmäßig nicht überschrieben.
- Im Dry-Run wird nur angezeigt, was geschrieben würde.
- Dokumente mit sinnvoll befüllten `sb_`-Feldern gelten als vorbereitet und
  bekommen zusätzlich den Tag `SB`.

#### Unterstützte `sb_`-Felder

Klassifizierung:

- `sb_document_category`
- `sb_life_area`

Referenzen:

- `sb_case_reference`
- `sb_contract_number`
- `sb_customer_number`
- `sb_invoice_number`
- `sb_policy_number`
- `sb_meter_number`
- `sb_provider_name`
- `sb_person_involved`
- `sb_object_reference`
- `sb_bank_account_hint`

Beträge:

- `sb_amount_total`
- `sb_amount_net`
- `sb_amount_tax`

Datumsfelder:

- `sb_due_date`
- `sb_document_date`
- `sb_period_start`
- `sb_period_end`
- `sb_effective_from`
- `sb_effective_until`

Aufgaben / Status:

- `sb_requires_action`
- `sb_action_status`
- `sb_action_owner`
- `sb_next_action`

Recht / Finanzen / Steuer:

- `sb_legal_relevance`
- `sb_financial_relevance`
- `sb_tax_year`
- `sb_tax_type`

Energie / Fahrzeug:

- `sb_energy_type`
- `sb_vehicle`

Qualität / SecondBrain-Steuerung:

- `sb_confidence`
- `sb_source_quality`
- `sb_sensitive`
- `sb_export_to_secondbrain`
- `sb_ignore_by_secondbrain`

Verknüpfungen:

- `sb_related_documents`
- `sb_external_url`

#### Verhalten

- Die KI kann optional ein strukturiertes Objekt `secondbrain_custom_fields`
  liefern. Jeder Feldvorschlag besteht aus `value`, `confidence` und `reason`.
- Werte unterhalb `secondbrain_custom_fields_confidence_threshold` werden nicht
  nach Paperless geschrieben.
- Datumswerte werden auf `YYYY-MM-DD` normalisiert.
- Monetäre Werte werden intern tolerant gelesen und für Paperless auf das
  dokumentierte Format `EUR12.34` gebracht.
- Wenn `enable_tax_enrichment` aktiv ist, werden vorhandene Steuerdaten wie
  `tax_year`, `document_date`, `service_period_from`, `service_period_to`,
  `issuer` und `total_amount` als `sb_`-Fallbacks wiederverwendet.
- In der KI-Notiz erscheint ein eigener Abschnitt `SecondBrain-Felder`, sobald
  tatsächlich `sb_`-Werte erkannt oder gesetzt wurden.

#### Bestandsdaten-Backfill

Für bereits eingelesene Paperless-Datenbanken gibt es einen eigenen
Backfill-Durchlauf. Er ist für genau den Fall gedacht, dass neue Funktionen wie
Tax Enrichment, `sb_`-Custom-Fields oder weitere Zusatzfelder nachträglich auf
alte Dokumente angewendet werden sollen.

Wichtig dabei:

- Der Backfill ignoriert den normalen `#NEU`-Tag-Filter.
- Bereits KI-getaggte Dokumente werden noch einmal analysiert, aber nur
  anreichernd aktualisiert.
- Standard-Metadaten wie Dokumenttyp, Korrespondent, Speicherpfad, Tags und
  Dokumentdatum werden bei bereits KI-getaggten Dokumenten dabei nicht
  überschrieben.
- Wenn du bestehende KI-Dokumente absichtlich komplett neu klassifizieren
  möchtest, nutze weiterhin den normalen Reprocess-Weg über
  `reprocess_ki_tagged_documents: true` und nicht den Backfill-Modus.
- Die kostenoptimierenden Standard-Prechecks werden im Backfill bewusst
  ausgesetzt, damit die Bestandsdaten wirklich vollständig erneut geprüft
  werden können.

CLI-Beispiel für den kompletten Backfill:

```bash
python3 /config/custom_components/paperless_kiplus/paperless_ai_sorter.py \
  --config config.yaml \
  --backfill-existing-documents
```

Wenn du den Gesamtdurchlauf in Chargen aufteilen möchtest:

```bash
python3 /config/custom_components/paperless_kiplus/paperless_ai_sorter.py \
  --config config.yaml \
  --backfill-existing-documents \
  --max-documents 200
```

In Home Assistant kannst du den Backfill auf zwei Wegen starten:

- Button `Paperless KIplus Bestandsdaten neu anreichern`
- Service `paperless_kiplus.run` mit `backfill_existing_documents: true`

Beispiel-Service-Call:

```yaml
service: paperless_kiplus.run
data:
  backfill_existing_documents: true
```

Im Backfill gilt zusätzlich:

- Bereits befüllte SecondBrain-Dokumente werden vor dem KI-Aufruf erkannt.
- Wenn schon sinnvolle `sb_`-Felder vorhanden sind, wird das Dokument ohne neue
  KI-Tokens übersprungen.
- Solche Dokumente bekommen, falls noch nicht vorhanden, automatisch den Tag
  `SB`.

#### Live-Fortschritt, Pause und Resume

Während eines Laufs schreibt der Sorter jetzt maschinenlesbare Runtime-Events.
Dadurch kann Home Assistant echten Fortschritt anzeigen, statt nur auf das
Laufende zu warten.

Sichtbar sind unter anderem:

- `Paperless KIplus Fortschritt` als Prozent-Sensor
- `progress_current_document_title` im Statussensor
- `progress_last_event_at`, damit man sofort sieht, wann der letzte echte Fortschritt ankam
- eigener Sensor `Paperless KIplus Aktuelles Dokument`
- eigener Sensor `Paperless KIplus Letztes fertiges Dokument`
- `progress_scanned`, `progress_updated`, `progress_skipped`, `progress_failed`
- `resume_available`, `pause_reason` und `auto_resume_at`

Zusätzlich gibt es jetzt klickbare Hilfs-Buttons:

- `Paperless KIplus Aktuelles Dokument öffnen`
- `Paperless KIplus Letztes fertiges Dokument öffnen`
- `Paperless KIplus Letztes Protokoll herunterladen`
- `Paperless KIplus Lauf neu starten`

Die Dokument-Buttons erzeugen in Home Assistant eine anklickbare
Benachrichtigung mit direktem Paperless-Link zum jeweiligen Dokument. Der
Log-Download-Button exportiert das letzte Protokoll nach `/config/www` und
zeigt direkt einen anklickbaren Download-Link an.

#### Frischer Neustart statt Resume

Wenn du bewusst **nicht** an einem pausierten Stand weiterlaufen willst, sondern
mit neuer Konfiguration komplett frisch neu beginnen möchtest, nutze jetzt:

- Button: `Paperless KIplus Lauf neu starten`
- Service: `paperless_kiplus.restart`

Der Neustart:

- stoppt einen laufenden Prozess bei Bedarf sofort,
- verwirft den alten Resume-Stand,
- startet den Lauf frisch von vorne,
- übernimmt ohne explizite Angabe den zuletzt bekannten Modus, z. B.
  `Bestandsdaten-Backfill`.

#### Fertiges Dashboard zum Einfügen

Es gibt jetzt eine große Lovelace-YAML-Vorlage unter:

- [dashboards/paperless_kiplus_dashboard.yaml](/Users/joachim.stiegler/Paperless-KIplus/dashboards/paperless_kiplus_dashboard.yaml)

Damit bekommst du auf einen Blick:

- Status
- Fortschritt
- aktuelles Dokument
- letztes fertiges Dokument
- Neustart / Pause / Stop / Resume
- Log-Download und Support-Hilfen

Neuer Service zum sicheren Pausieren:

```yaml
service: paperless_kiplus.stop
data: {}
```

Neuer Service für einen echten Sofort-Stopp:

```yaml
service: paperless_kiplus.stop_now
data: {}
```

Neuer Service zum Fortsetzen eines pausierten Laufs:

```yaml
service: paperless_kiplus.resume
data:
  wait: false
```

Wichtige Regeln:

- `stop` ist bewusst ein kontrollierter Stop nach aktuellem Dokument oder
  aktuellem KI-Batch. Der Prozess wird nicht blind hart beendet.
- `stop_now` beendet den laufenden Prozess sofort. Wenn bereits ein
  Fortschrittszustand geschrieben wurde, kann der Lauf später trotzdem wieder
  aufgenommen werden.
- Ein pausierter Lauf speichert seinen Zustand in einer Resume-Datei und setzt
  später genau dort fort.
- Bei kurzen Rate-Limits mit kleinem `Retry-After` wartet der Lauf direkt im
  selben Prozess.
- Bei längeren Provider-Wartezeiten oder `insufficient_quota` pausiert der Lauf
  kontrolliert und plant die Wiederaufnahme, statt Dokumente unnötig in die
  Fehlerquarantäne zu schicken.
- Für manuelle CLI-Läufe gibt es zusätzlich:
  - `--resume-run`
  - `--request-stop`
  - `--run-state-file`
  - `--stop-request-file`

#### Beispiel

Beispiel einer KI-Antwort mit zusätzlichen SecondBrain-Feldern:

```json
{
  "document_type": "Rechnung",
  "correspondent": "Muster GmbH",
  "storage_path": "Privat",
  "tags": ["Finanzen"],
  "document_date": "2026-05-01",
  "summary": "Rechnung über Speichererweiterung.",
  "confidence": 0.91,
  "rationale": "Rechnungsnummer, Betrag und Zahlungsziel klar erkannt.",
  "secondbrain_custom_fields": {
    "sb_document_category": {
      "value": "Rechnung",
      "confidence": 0.95,
      "reason": "Rechnungsnummer und Gesamtbetrag klar erkennbar."
    },
    "sb_amount_total": {
      "value": "123.45",
      "confidence": 0.88,
      "reason": "Gesamtbetrag inkl. MwSt. erkannt."
    },
    "sb_due_date": {
      "value": "2026-05-15",
      "confidence": 0.84,
      "reason": "Zahlungsziel im Dokument erkannt."
    }
  }
}
```

Nach der Auflösung gegen Paperless können daraus zum Beispiel diese Werte
entstehen:

- `sb_document_category` -> Select-Option-ID aus Paperless
- `sb_amount_total` -> `EUR123.45`
- `sb_due_date` -> `2026-05-15`

### Standard-Katalog (Vertrag / Lohnabrechnung)

Zusätzlich gibt es weiterhin den kleineren festen Katalog:

```yaml
enable_custom_field_enrichment: true
create_missing_custom_fields: true
```

Verträge:

- `Vertragsnummer`
- `Kundennummer`
- `Vertragsbeginn`
- `Vertragsende`
- `Kündigen bis`
- `Monatliche Aufwendungen`

Lohnabrechnungen:

- `Brutto`
- `Netto`
- `Boni`
- `Sonstige Bezüge`
- `Steuern/Sozialabgaben`
- `Sonstige Abzüge`
- `Abgaben gesamt`

### Grenzen

- Die Extraktion bleibt KI-gestützt und ist daher ein Vorschlagssystem.
- Das Feature ist bewusst auf einen festen Feldkatalog begrenzt, damit keine
  unkontrollierte Feldflut in Paperless entsteht.
- Wenn `SecondBrain` diese Felder als First-Class-Datenmodell nutzen soll,
  muss dessen Importpfad die Paperless-Custom-Fields ebenfalls aktiv auslesen.

## Tax Enrichment

### Ziel

Die Tax-Enrichment-Funktion ergänzt die normale Dokumentklassifikation um eine
steuerorientierte Sicht pro Dokument. Sie ist als Vorschlagssystem gebaut und
trifft keine endgültigen Rechtsentscheidungen.

### Architekturüberblick

- Die bestehende Paperless-Klassifikation bleibt unverändert und läuft weiter wie bisher.
- Optional wird danach ein separates, versioniertes `tax_enrichment` pro Dokument erzeugt.
- Das Steuerobjekt nutzt:
  - eine feste interne Taxonomie
  - einen semantischen WISO-Zielbereich
  - eine formale Nachweis-/Validierungslogik
  - Review-Flags für menschliche Nacharbeit
- Es wird bewusst kein proprietäres WISO-Dateiformat erzeugt.

### Datenmodell

Das Steuerobjekt enthält unter anderem:

- `tax_year`
- `document_date`
- `service_period_from`
- `service_period_to`
- `document_type`
- `issuer`
- `recipient`
- `total_amount`
- `currency`
- `payment_method`
- `payment_verified`
- `evidence_type`
- `tax_category`
- `tax_subcategory`
- `deduction_domain`
- `wiso_target_area`
- `classification_confidence`
- `eligibility_confidence`
- `reasoning_summary`
- `flags`
- optional zusätzlich `person_reference`, `child_reference`, `household_reference`, `extracted_evidence`, `missing_requirements`, `recommended_follow_up`, `formal_validity`

### Steuerkategorien

Hauptkategorien:

- `werbungskosten`
- `sonderausgaben`
- `aussergewoehnliche_belastungen`
- `kinderbetreuungskosten`
- `haushaltsnahe_dienstleistungen`
- `handwerkerleistungen`
- `unterhalt`
- `pflege`
- `kapitalvermoegen`
- `vermietung`
- `selbststaendigkeit`
- `nicht_steuerrelevant`
- `unklar`

Beispiel-Unterkategorien:

- `arbeitsmittel`
- `homeoffice`
- `weiterbildung`
- `fahrtkosten`
- `kita`
- `tagesmutter`
- `babysitter`
- `reinigung`
- `gartenpflege`
- `winterdienst`
- `handwerker_lohnkosten`
- `medikamente`
- `apotheke`
- `pflegedienst`
- `pflegeheim`

### Review-Flags

Mindestens diese Review-Flags werden unterstützt:

- `needs_review`
- `needs_payment_proof`
- `needs_person_assignment`
- `needs_year_assignment`
- `high_audit_relevance`
- `possible_finanzamt_query`
- `not_tax_relevant`
- `mixed_private_and_tax_relevant`
- `missing_labor_split`
- `cash_payment_not_eligible`

### Exportformate

Bei aktivierter Funktion werden pro Steuerjahr Dateien erzeugt:

- `tax_exports/<jahr>/tax_export.json`
- `tax_exports/<jahr>/tax_review.csv`

`tax_export.json` enthält:

- `taxpayer`
- `tax_year`
- `documents`
- `category_totals`
- `review_items`
- `missing_evidence`
- `notes_for_wiso`

`tax_review.csv` enthält pro Dokument mindestens:

- `document_id`
- `title`
- `document_date`
- `issuer`
- `total_amount`
- `tax_year`
- `tax_category`
- `tax_subcategory`
- `wiso_target_area`
- `formal_validity`
- `classification_confidence`
- `eligibility_confidence`
- `flags`
- `reasoning_summary`

### Grenzen der Automatisierung

- Die Steueranalyse ist ein Vorschlagssystem, keine Rechtsberatung.
- WISO wird nur semantisch vorbereitet, nicht über ein proprietäres Dateiformat angesteuert.
- Fehlende Zahlungsnachweise, fehlende Personenzuordnung oder unklare Jahre werden bewusst als Review-Fall markiert.
- Bei haushaltsnahen Dienstleistungen und Handwerkerleistungen werden Barzahlung und fehlende Lohn-/Materialtrennung explizit markiert.

### Beispielkonfiguration

Zusätzliche Konfigurationsfelder:

```yaml
enable_tax_enrichment: true
tax_export_dir: "tax_exports"
tax_export_years:
  - 2025
tax_process_ki_tagged_documents: false
tax_ai_api_key: dummy
tax_ai_model: qwen2.5:7b
tax_ai_base_url: http://ollama:11434/v1
tax_personal_context: |
  Steuerpflichtiger: Max Mustermann
  Familienstand:
  Kinder:
  Betreuungsmodell:
  Sonstige steuerlich wichtige Hinweise:
```

### Steuer-Tags in Paperless

Wenn Tax Enrichment aktiv ist, werden steuerliche Ergebnis-Tags best effort nach
Paperless gespiegelt:

- `KI Steuerrelevant <Jahr>` bei steuerlich relevantem Dokument mit erkanntem Steuerjahr
- `KI nicht Steuerrelevant` bei klar nicht steuerrelevanten Dokumenten

So kannst du spaeter direkt nach Steuerjahr oder Nicht-Relevanz filtern.

### Alte KI-Dokumente einmal steuerlich nachziehen

Wenn du bereits viele Dokumente mit KI-Tag hast und diese nicht neu klassifizieren,
aber einmal steuerlich prüfen lassen willst, nutze:

```yaml
enable_tax_enrichment: true
tax_process_ki_tagged_documents: true
reprocess_ki_tagged_documents: false
```

Dann werden bestehende KI-Dokumente nur fuer Tax Enrichment erneut betrachtet,
ohne die normale Dokument-Klassifikation noch einmal durchzuschicken.

### Prompt fuer deinen privaten Steuerkontext

Du kannst dir einen guten Freitext fuer `tax_personal_context` mit ChatGPT erzeugen
lassen und dann in Home Assistant direkt in dein YAML-Feld einfügen.

```text
Erstelle mir einen kompakten, gut strukturierten Freitext fuer die YAML-Einstellung
"tax_personal_context" meiner Home-Assistant Integration "Paperless KIplus Runner".

Ziel:
- Die Information soll einer Steuer-KI helfen, private deutsche Dokumente fuer die
  Einkommensteuer sinnvoller zu bewerten.
- Es geht nur um Kontext, nicht um eine Steuererklaerung.
- Gib nur klaren, kopierbaren Text aus, kein Markdown, keine Erklaerungen.

Bitte frage bzw. strukturiere die Antwort nach diesen Punkten:
- Steuerpflichtige Hauptperson mit Name
- Ehe-/Partnerschaftsstatus
- Zusammenveranlagung oder Trennung, falls bekannt
- Kinder mit Name, Geburtsjahr/Alter und Wohn-/Betreuungssituation
- Bei getrennten Eltern: Verteilung der Kinderbetreuung und wer welche Kosten traegt
- Weitere haushaltszugehoerige oder unterstuetzte Personen
- Pflegefaelle, Unterhalt, Behinderung, Krankheitskosten oder andere besondere Belastungen
- Berufliche Situation, soweit fuer Werbungskosten wichtig
- Vermietung, Selbststaendigkeit, Kapitalertraege oder sonstige steuerlich relevante Lebensbereiche
- Sonstige Hinweise, wonach eine Steuer-KI besonders schauen soll

Anforderungen:
- Formuliere neutral, knapp und sachlich.
- Verwende Abschnitte mit klaren Ueberschriften.
- Erfinde nichts und lasse unbekannte Punkte als "unbekannt" stehen.
- Optimiere den Text fuer spaeteres maschinelles Mitlesen.
```

## Standalone Worker für Docker / Unraid

Wenn du die Rechenlast von Home Assistant auf deinen Server ziehen willst,
gibt es jetzt einen voll lauffähigen Worker mit eigener Weboberfläche.

Was der Worker mitbringt:

- vollständige Ausführung ohne Home Assistant
- eingebaute Weboberfläche unter `/`
- JSON-API für Run / Stop / Resume / Restart / Backfill
- Log-Download, Status und Konfigurationsverwaltung
- persistente Dateien für Config, Metriken und Resume-State unter `/data`

Dokumentation:

- [Docker- und Unraid-Betrieb](./docs/docker-unraid.md)
- [Migration von Home Assistant zum Remote-Worker](./docs/migration-ha-to-worker.md)
- [Lokale LLMs für kleinere Aufgaben](./docs/local-llm-routing.md)

### Schnellstart Standalone

```bash
mkdir -p worker-data/config
cp config.example.yaml worker-data/config/config.yaml
docker compose -f docker/docker-compose.example.yml up -d
```

Danach:

- Weboberfläche: `http://<server>:8787/`
- Status-API: `http://<server>:8787/api/status`

### Robuste Unraid-Installation

Für Unraid gibt es jetzt zwei klare Wege:

1. Von macOS/Linux per SSH auf einen entfernten Unraid-Server deployen
2. Direkt auf dem Unraid-Server installieren

#### Empfohlen: Remote-Deploy von macOS/Linux nach Unraid

```bash
bash docker/deploy-to-unraid.sh \
  --unraid-host 192.168.178.30 \
  --paperless-url http://192.168.178.20:8000 \
  --paperless-token PAPERLESS_TOKEN \
  --ai-api-key OPENAI_KEY \
  --ai-model gpt-4.1-mini
```

Das Remote-Skript:

- verbindet sich per SSH mit Unraid
- kopiert den eigentlichen Host-Installer auf den Server
- überträgt optional eine lokale `config.yaml`
- führt die Installation direkt auf Unraid aus

#### Direkte Ausführung auf dem Unraid-Server

Wenn du bereits per Shell auf Unraid bist, kannst du dort das Host-Skript verwenden:

```bash
bash /pfad/zum/repo/docker/install-unraid-worker.sh \
  --paperless-url http://192.168.178.20:8000 \
  --paperless-token PAPERLESS_TOKEN \
  --ai-api-key OPENAI_KEY \
  --ai-model gpt-4.1-mini
```

Das Host-Skript:

- legt die Appdata-Verzeichnisse an
- sichert bestehende Dateien
- erzeugt eine startfähige `config.yaml`
- schreibt einen Compose-Stack mit GHCR-Image
- startet oder aktualisiert den Worker
- prüft die API per Health-Check

### Remote-Steuerung aus Home Assistant

In den Integrationsoptionen kannst du jetzt wählen:

- `execution_mode: local`
- `execution_mode: remote_worker`

Für den Remote-Modus brauchst du typischerweise:

- `remote_worker_url`, zum Beispiel `http://unraid-server:8787`
- optional `remote_worker_token`
- optional `remote_worker_sync_config: true`

Zusätzliche HA-Hilfen:

- Button `Paperless KIplus Worker-Konfiguration exportieren`
- Button `Paperless KIplus Worker-Weboberfläche öffnen`
- Service `paperless_kiplus.export_config`

### Optionale lokale LLMs für kleinere Aufgaben

Tax Enrichment kann jetzt auf einen eigenen OpenAI-kompatiblen Endpoint zeigen.
Damit kannst du die Hauptklassifikation weiter in der Cloud lassen und die
kleinere steuerliche Extraktion lokal ausführen.

Beispiel:

```yaml
ai_api_key: <OPENAI_KEY>
ai_model: gpt-4.1-mini
ai_base_url: https://api.openai.com/v1

enable_tax_enrichment: true
tax_ai_api_key: dummy
tax_ai_model: qwen2.5:7b
tax_ai_base_url: http://ollama:11434/v1
```

## Versionsverlauf (antichronologisch)

- `v1.4.3` (2026-04-27)
  - Neues Remote-Deploy-Skript `docker/deploy-to-unraid.sh` ergänzt, damit die Installation von macOS/Linux per SSH sauber auf einem entfernten Unraid-Server ausgeführt werden kann.
  - Das Host-Installationsskript `docker/install-unraid-worker.sh` blockiert jetzt bewusst direkte Ausführung auf macOS und verweist stattdessen auf den Remote-Deploy-Weg.

- `v1.4.2` (2026-04-26)
  - Eigenständigen Docker-/Unraid-Worker mit Weboberfläche und JSON-API ergänzt.
  - Remote-Ausführungsmodus in Home Assistant ergänzt, inklusive `execution_mode`, `remote_worker_url` und automatischem Config-Sync.
  - Neuer Config-Export-Service und neue Buttons für Worker-UI sowie Worker-Konfiguration ergänzt.
  - Optionalen eigenen Tax-KI-Provider (`tax_ai_*`) ergänzt, damit kleinere Steuer-Aufgaben auf lokalen OpenAI-kompatiblen LLMs laufen können.
  - Dockerfile, Compose-Beispiel, Unraid-Template, GHCR-Build-Workflow und Betriebsdoku ergänzt.
  - Neues robustes Unraid-Installationsskript ergänzt, das Appdata, Compose, Konfiguration, Backups und Health-Check automatisch vorbereitet.

- `v1.3.5` (2026-04-26)
  - Neuer Service und Button `Lauf neu starten`: verwirft bewusst den alten Resume-Stand und startet frisch von vorne.
  - Neustart übernimmt standardmäßig den zuletzt bekannten Modus, zum Beispiel Backfill.
  - Großes Lovelace-Dashboard als direkt einfügbare YAML-Vorlage ergänzt.

- `v1.3.4` (2026-04-26)
  - Neue Sensoren für `Aktuelles Dokument` und `Letztes fertiges Dokument` ergänzt.
  - Neue Buttons ergänzt, die anklickbare Paperless-Links für aktuelles und zuletzt abgeschlossenes Dokument bereitstellen.
  - Log-Download-Button verbessert: Export erzeugt jetzt direkt eine anklickbare Home-Assistant-Benachrichtigung mit Download-Link.
  - Statussensor enthält jetzt zusätzlich URLs für aktuelles und letztes fertiges Dokument.

- `v1.3.3` (2026-04-26)
  - Live-Fortschritts-Events deutlich verschlankt, damit Home Assistant nicht mehr komplette OCR-Inhalte als Fortschritt mitschleppen muss.
  - Resume-State bleibt weiterhin vollständig auf Disk erhalten, während der Live-Status nur noch schlanke Pending-Metadaten überträgt.
  - Neuer Zeitstempel `progress_last_event_at` ergänzt, damit hängende oder stille Läufe sofort erkennbar sind.
  - Sofort-Stopp bevorzugt jetzt die vollständige Run-State-Datei für Resume, statt einen eventuell abgespeckten Live-Status zu konservieren.

- `v1.3.2` (2026-04-26)
  - Backfill prüft jetzt vor dem KI-Aufruf, ob ein Dokument bereits sinnvolle `sb_`-Felder besitzt.
  - Bereits vorbereitete SecondBrain-Dokumente werden im Backfill ohne neue KI-Tokens übersprungen.
  - Dokumente mit vorhandenen oder frisch gesetzten SecondBrain-Feldern bekommen automatisch den Tag `SB`.

- `v1.3.1` (2026-04-26)
  - Echten Sofort-Stopp per Home-Assistant-Service `paperless_kiplus.stop_now` ergänzt.
  - Neuen Geräte-Button `Paperless KIplus Lauf sofort stoppen` ergänzt.
  - Runner bewahrt beim Sofort-Stopp den letzten bekannten Fortschrittszustand, damit ein späteres Resume möglich bleibt.
  - Statussensor zeigt jetzt zusätzlich `force_stop_requested`.

- `v1.3.0` (2026-04-26)
  - Echte Live-Fortschrittsanzeige mit Prozent, aktuellem Dokument und laufenden Zählern ergänzt.
  - Kontrolliertes Pausieren und Fortsetzen für manuelle Läufe eingebaut.
  - Resume-State-Datei eingeführt, damit pausierte Läufe später exakt weiterlaufen können.
  - Neue Home-Assistant-Services `paperless_kiplus.stop` und `paperless_kiplus.resume` ergänzt.
  - Neue Geräte-Buttons für Pause und Fortsetzen ergänzt.
  - Provider-429/Quota-Fälle werden jetzt als Pause statt als Dokumentfehler behandelt.
  - Automatische Wiederaufnahme nach `Retry-After` bzw. Provider-Backoff ergänzt.

- `v1.2.0` (2026-04-26)
  - SecondBrain-Custom-Field-Sync für bestehende `sb_`-Felder in Paperless ergänzt.
  - Strukturierte KI-Ausgabe für `sb_`-Felder mit Confidence und Begründung hinzugefügt.
  - Bestehende Paperless-Custom-Fields werden dynamisch per Name und Select-Option-ID aufgelöst.
  - Neuer Bestandsdaten-Backfill für bereits eingelesene Paperless-Datenbanken ergänzt.
  - KI-getaggte Dokumente können im Backfill gezielt nur für neue Zusatzfunktionen erneut angereichert werden.
  - Home-Assistant-Service und Geräte-Button für den Backfill ergänzt.

- `v1.1.1` (2026-03-29)
  - UI-Optionen für Steuerfunktion ergänzt.
  - Privater Steuerkontext direkt in Home Assistant pflegbar.
  - Bereits KI-getaggte Dokumente können einmalig nur steuerlich nachgeprüft werden.
  - Steuer-Tags `KI Steuerrelevant <Jahr>` und `KI nicht Steuerrelevant` ergänzt.

- `v1.1.0` (2026-03-28)
  - Erste produktiv nutzbare Tax-Enrichment-Erweiterung hinzugefügt.
  - Feste Steuer-Taxonomie, WISO-Mapping-Layer, Review-Flags und Nachweisprüfung ergänzt.
  - Export pro Steuerjahr als `tax_export.json` und `tax_review.csv` eingeführt.

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
