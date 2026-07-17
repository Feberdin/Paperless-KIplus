# SecondBrain Schnittstellenbeschreibung

## Ziel

Diese Datei beschreibt die stabile Übergabeschnittstelle zwischen
`Paperless-KIplus` und einem nachgelagerten Consumer wie
[`SecondBrain`](https://github.com/Feberdin/SecondBrain).

Der Fokus liegt auf drei Datenquellen:

1. Paperless-Dokumente inklusive gesetzter `sb_`-Custom-Fields
2. Paperless-Standardmetadaten wie Tags, Titel und Dokumentdatum
3. optionale Steuerexporte aus `tax_exports/<jahr>/tax_export.json`

Wichtig:

- Die `sb_`-Custom-Fields sind die primäre strukturierte Übergabeschicht.
- KI-Notizen sind für Menschen gedacht und nur eine ergänzende Fallback-Quelle.
- Die Steuerexporte sind eine zusätzliche Jahres-Sicht und kein Ersatz für den
  dokumentbezogenen Zugriff über Paperless.

## Scope und Stabilitätsversprechen

`Paperless-KIplus` befüllt vorhandene Paperless-Custom-Fields mit Prefix
`sb_`, wenn `enable_secondbrain_custom_fields: true` aktiv ist.

Dabei gilt:

- Feldnamen werden über den Paperless-Feldnamen aufgelöst, nicht über feste IDs.
- Select-Felder werden intern per Options-ID gespeichert.
- `SecondBrain` sollte deshalb Select-Werte immer über die Felddefinitionen aus
  `/api/custom_fields/` in Labels zurückauflösen.
- Bereits vorhandene Werte werden standardmäßig nicht blind überschrieben.
- Fehlende oder ungültige Werte werden weggelassen, nicht künstlich erfunden.

## Empfohlene Abrufstrategie für SecondBrain

### Primäre Datenquelle: Paperless REST API

Empfohlene Abrufreihenfolge:

1. `GET /api/custom_fields/`
2. `GET /api/documents/` mit Pagination
3. optional `GET /api/documents/<id>/notes/`, falls Notizen als Fallback
   eingelesen werden sollen

### Warum diese Reihenfolge?

- Die Felddefinitionen liefern die stabile Übersetzung von Feldname -> Feld-ID
  sowie bei Select-Feldern `Label -> Option-ID`.
- Dokumente enthalten anschließend die tatsächlichen Werte.
- Erst mit beiden Datenquellen zusammen kann `SecondBrain` die gespeicherten
  Werte robust in sein internes Modell überführen.

## Paperless-Endpunkte

### 1. Custom Fields laden

Endpoint:

```text
GET /api/custom_fields/
```

Relevante Felder pro Custom Field:

- `id`
- `name`
- `data_type`
- `extra_data`

Für Select-Felder ist besonders wichtig:

- `extra_data.select_options[]`
  - `id`
  - `label`

`SecondBrain` sollte daraus folgende Maps aufbauen:

- `custom_field_by_name[name] -> definition`
- `custom_field_by_id[id] -> definition`
- `select_label_by_option_id[field_name][option_id] -> label`
- `select_option_id_by_label[field_name][label_lower] -> option_id`

### 2. Dokumente laden

Endpoint:

```text
GET /api/documents/
```

Empfohlene Felder, die `SecondBrain` berücksichtigen sollte:

- `id`
- `title`
- `created`
- `document_type`
- `correspondent`
- `storage_path`
- `tags`
- `checksum`
- `content`
- `custom_fields`

## Tolerante Auswertung von `custom_fields`

Paperless-ngx kann `custom_fields` je nach Version oder Endpunkt leicht
unterschiedlich zurückgeben. `SecondBrain` sollte deshalb bewusst tolerant
implementiert werden.

### Variante A: Dictionary

Beispiel:

```json
{
  "custom_fields": {
    "sb_document_category": {
      "value": 11
    },
    "sb_amount_total": {
      "value": "EUR123.45"
    }
  }
}
```

### Variante B: Liste

Beispiel:

```json
{
  "custom_fields": [
    {
      "name": "sb_document_category",
      "value": 11
    },
    {
      "name": "sb_amount_total",
      "value_monetary": "EUR123.45"
    }
  ]
}
```

### Empfohlene robuste Leselogik

`SecondBrain` sollte intern auf ein einheitliches Format normalisieren:

```json
{
  "sb_document_category": 11,
  "sb_amount_total": "EUR123.45"
}
```

## Unterstützte `sb_`-Felder

### Klassifizierung

- `sb_document_category` (`select`)
- `sb_life_area` (`select`)

### Referenzen

- `sb_case_reference` (`string`)
- `sb_contract_number` (`string`)
- `sb_customer_number` (`string`)
- `sb_invoice_number` (`string`)
- `sb_policy_number` (`string`)
- `sb_meter_number` (`string`)
- `sb_provider_name` (`string`)
- `sb_person_involved` (`string`)
- `sb_object_reference` (`string`)
- `sb_bank_account_hint` (`string`)

### Beträge

- `sb_amount_total` (`monetary`)
- `sb_amount_net` (`monetary`)
- `sb_amount_tax` (`monetary`)

### Datumsfelder

- `sb_due_date` (`date`)
- `sb_calendar_date` (`date`)
- `sb_calendar_time` (`string`)
- `sb_calendar_type` (`select`)
- `sb_calendar_title` (`string`)
- `sb_calendar_events` (`string`, kompakter JSON-Array-String)
- `sb_document_date` (`date`)
- `sb_period_start` (`date`)
- `sb_period_end` (`date`)
- `sb_effective_from` (`date`)
- `sb_effective_until` (`date`)

### Aufgaben / Status

- `sb_requires_action` (`boolean`)
- `sb_action_status` (`select`)
- `sb_action_owner` (`select`)
- `sb_next_action` (`string`)

### Recht / Finanzen / Steuer

- `sb_legal_relevance` (`select`)
- `sb_financial_relevance` (`select`)
- `sb_tax_year` (`integer`)
- `sb_tax_type` (`select`)

### Energie / Fahrzeug

- `sb_energy_type` (`select`)
- `sb_vehicle` (`select`)

### Qualität / SecondBrain-Steuerung

- `sb_confidence` (`select`)
- `sb_source_quality` (`select`)
- `sb_sensitive` (`boolean`)
- `sb_export_to_secondbrain` (`boolean`)
- `sb_ignore_by_secondbrain` (`boolean`)

### Verknüpfungen

- `sb_related_documents` (`documentlink`)
- `sb_external_url` (`url`)

## Datentypvertrag

### `string`

- UTF-8-Text
- leer oder nicht gesetzt bedeutet: kein verlässlicher Wert vorhanden

### `select`

In Paperless wird der Wert als Options-ID gespeichert.

Beispiel:

```json
{
  "sb_document_category": 11
}
```

`SecondBrain` sollte diesen Wert mit den zuvor geladenen
`extra_data.select_options` wieder auf das sichtbare Label zurückführen.

Beispiel intern:

```json
{
  "field": "sb_document_category",
  "raw_value": 11,
  "label": "Rechnung"
}
```

### `date`

Immer als ISO-Datum:

```text
YYYY-MM-DD
```

Beispiel:

```json
{
  "sb_due_date": "2026-05-15",
  "sb_calendar_date": "2026-08-15",
  "sb_calendar_time": "09:30",
  "sb_calendar_events": "[{\"date\":\"2026-08-15\",\"time\":\"09:30\",\"type\":\"Gericht\",\"title\":\"Gericht: Ladung Amtsgericht\"}]"
}
```

### `monetary`

`Paperless-KIplus` schreibt Geldwerte im Paperless-kompatiblen Format:

```text
EUR1234.56
```

Beispiele:

- `EUR49.90`
- `EUR1234.56`

Empfohlene Normalisierung in `SecondBrain`:

```json
{
  "currency": "EUR",
  "amount": 1234.56
}
```

### `boolean`

Echte Booleans:

- `true`
- `false`

### `integer`

Ganzzahl, zum Beispiel:

```json
{
  "sb_tax_year": 2025
}
```

### `url`

Absolute URL mit `http://` oder `https://`

### `documentlink`

Liste von Dokument-IDs.

Beispiel:

```json
{
  "sb_related_documents": [123, 456]
}
```

## Bedeutungsregeln für SecondBrain

### Primäre Steuerungsfelder

Diese Felder sind für die Consumer-Logik besonders wichtig:

- `sb_export_to_secondbrain`
- `sb_ignore_by_secondbrain`
- `sb_confidence`
- `sb_source_quality`
- `sb_requires_action`
- `sb_action_status`
- `sb_calendar_date`
- `sb_calendar_type`
- `sb_calendar_events`

Empfohlene Interpretation:

- `sb_export_to_secondbrain = true`
  - Dokument darf aktiv nach `SecondBrain` übernommen werden
- `sb_ignore_by_secondbrain = true`
  - Dokument sollte in `SecondBrain` standardmäßig nicht importiert werden
- `sb_confidence`
  - Vertrauensindikator für die Strukturqualität
- `sb_source_quality`
  - Qualität und Herkunft der Quelle
- `sb_requires_action = true`
  - Dokument erzeugt To-do, Review oder Fristbezug
- `sb_calendar_date`
  - explizites Datum, das `SecondBrain` als Kalender- oder Reminder-Kandidat
    übernehmen kann
- `sb_calendar_time`
  - optionale Uhrzeit zu `sb_calendar_date`; fehlt, wenn im Dokument keine
    eindeutige Uhrzeit erkennbar war
- `sb_calendar_type`
  - fachliche Art des Kalendereintrags, z. B. Gericht, Einladung oder Frist
- `sb_calendar_title`
  - kurzer Anzeigename für den möglichen Kalendereintrag
- `sb_calendar_events`
  - JSON-Liste aller erkannten Kalenderkandidaten; `SecondBrain` sollte daraus
    mehrere Kalender- oder Reminder-Einträge erzeugen können

### Wichtiger fachlicher Hinweis

Das Fehlen eines Feldes bedeutet nicht automatisch `false` oder `nicht relevant`.
Gerade bei KI-basierten Zusatzfeldern ist die Abwesenheit häufig ein Signal für:

- zu geringe Sicherheit
- nicht eindeutig im Dokument enthalten
- bewusst nicht überschrieben

`SecondBrain` sollte deshalb zwischen

- `explizit false`
- `explizit leer`
- `nicht vorhanden`

unterscheiden.

## Selektierte Labelwerte

Die sichtbaren Labels werden in `Paperless-KIplus` verwendet, aber in Paperless
als Options-ID gespeichert. `SecondBrain` sollte sich deshalb nicht auf feste
IDs verlassen.

### `sb_document_category`

- Rechnung
- Vertrag
- Bescheid
- Steuer
- Versicherung
- Recht
- Bank
- Gehalt
- Energie
- Fahrzeug
- Gesundheit
- Immobilie
- Garantie
- Kommunikation
- Sonstiges

### `sb_calendar_type`

- Termin
- Einladung
- Frist
- Gericht
- Zahlung
- Erinnerung
- Sonstiges

### `sb_life_area`

- Privat
- Arbeit
- Haus
- Auto
- Finanzen
- Steuer
- Recht
- Gesundheit
- Versicherung
- Energie
- Familie
- Technik

### `sb_action_status`

- Offen
- In Prüfung
- Wartet auf Rückmeldung
- Erledigt
- Bezahlt
- Widersprochen
- Weitergeleitet
- Archiviert

### `sb_action_owner`

- Ich
- Steuerberater
- Anwalt
- Arbeitgeber
- Versicherung
- Behörde
- Bank
- Sonstige

### `sb_legal_relevance`

- Keine
- Niedrig
- Mittel
- Hoch
- Fristkritisch

### `sb_financial_relevance`

- Keine
- Einnahme
- Ausgabe
- Erstattung
- Nachzahlung
- Forderung

### `sb_tax_type`

- Einkommensteuer
- Gewerbesteuer
- Umsatzsteuer
- Lohnsteuer
- Grundsteuer
- Kapitalertragsteuer
- Kfz-Steuer
- Sonstige Steuer

### `sb_energy_type`

- Strom
- Gas
- Wasser
- PV
- Einspeisung
- Wallbox
- Sonstige

### `sb_vehicle`

- Tesla Model 3
- Anderes Fahrzeug
- Nicht fahrzeugbezogen

### `sb_confidence`

- Manuell geprüft
- KI sicher
- KI unsicher
- OCR unsicher
- Ungeprüft

### `sb_source_quality`

- Original-PDF
- Scan gut
- Scan schlecht
- Foto
- E-Mail
- Import

## Zusammenspiel mit Tax Enrichment

Wenn `enable_tax_enrichment: true` aktiv ist, nutzt `Paperless-KIplus`
vorhandene Steuerdaten zusätzlich als Quelle für `sb_`-Felder.

Wichtige Ableitungen:

- `tax_year -> sb_tax_year`
- `document_date -> sb_document_date`
- `service_period_from -> sb_period_start`
- `service_period_to -> sb_period_end`
- `issuer -> sb_provider_name`
- `total_amount -> sb_amount_total`

Das bedeutet für `SecondBrain`:

- `sb_`-Felder bleiben die primäre Dokument-Schnittstelle
- die jährlichen Steuerexporte sind eine ergänzende Aggregationssicht

## Steuerexporte

Optional erzeugt `Paperless-KIplus` zusätzlich:

- `tax_exports/<jahr>/tax_export.json`
- `tax_exports/<jahr>/tax_review.csv`

Diese Dateien sind nützlich für:

- Jahresübersichten
- Review-Queues
- Steuerfall-spezifische Dashboards

Sie sind aber nicht die primäre Dokumentquelle für `SecondBrain`.

### `tax_export.json`

Enthält mindestens:

- `taxpayer`
- `tax_year`
- `documents`
- `category_totals`
- `review_items`
- `missing_evidence`
- `notes_for_wiso`

### `tax_review.csv`

Eine Zeile pro Dokument mit unter anderem:

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

## KI-Notizen als Fallback

Wenn `enable_ai_notes` aktiv ist, ergänzt `Paperless-KIplus` die Notiz mit
einem Abschnitt:

```text
SecondBrain-Felder:
- sb_document_category: ...
- sb_life_area: ...
- sb_case_reference: ...
- sb_amount_total: ...
- sb_due_date: ...
- sb_calendar_date: ...
- sb_calendar_type: ...
- sb_calendar_events: ...
- sb_requires_action: ...
- sb_action_status: ...
- sb_confidence: ...
```

Das ist hilfreich für Menschen und kann in `SecondBrain` als Fallback dienen,
wenn strukturierte `custom_fields` fehlen.

Empfehlung:

- primär `custom_fields` lesen
- Notizen nur als Backup oder Debug-Sicht nutzen

## Backfill-Verhalten

Für bestehende Paperless-Datenbanken gibt es einen Backfill-Modus.

Dabei gilt:

- bereits KI-getaggte Dokumente werden erneut analysiert
- Standard-Metadaten dieser Dokumente bleiben unverändert
- neue `sb_`-Felder und Steueranreicherungen können trotzdem gesetzt werden

Das ist für `SecondBrain` wichtig, weil auch Altbestand nachträglich eine
vollständige `sb_`-Belegung bekommen kann, ohne dass klassische Paperless-
Metadaten neu gemischt werden.

## Empfohlene Consumer-Implementierung

### Minimal robuster Ablauf

1. Lade alle Custom-Field-Definitionen aus Paperless
2. Baue Maps für Feldnamen, Feld-IDs und Select-Labels
3. Lade Dokumentseiten aus `/api/documents/`
4. Normalisiere `custom_fields` tolerant auf ein einheitliches internes Format
5. Wandle Select-IDs in sichtbare Labels um
6. Parse `monetary` nach `{currency, amount}`
7. Nutze `sb_export_to_secondbrain` und `sb_ignore_by_secondbrain` als primäre Importsteuerung
8. Ziehe optional Steuerexporte als Jahresaggregat hinzu

### Empfohlene interne Normalform

```json
{
  "document_id": 5220,
  "title": "2026-04-04_patrick.preuss_Re_...",
  "created": "2026-04-04",
  "sb": {
    "document_category": {
      "raw": 11,
      "label": "Rechnung"
    },
    "life_area": {
      "raw": 22,
      "label": "Finanzen"
    },
    "amount_total": {
      "raw": "EUR123.45",
      "currency": "EUR",
      "amount": 123.45
    },
    "due_date": "2026-05-15",
    "calendar": {
      "date": "2026-08-15",
      "time": "09:30",
      "type": {
        "raw": 54,
        "label": "Gericht"
      },
      "title": "Gericht: Ladung Amtsgericht",
      "events": [
        {
          "date": "2026-08-15",
          "time": "09:30",
          "type": "Gericht",
          "title": "Gericht: Ladung Amtsgericht"
        },
        {
          "date": "2026-09-01",
          "type": "Frist",
          "title": "Frist: Rückmeldung"
        }
      ]
    },
    "requires_action": true,
    "action_status": {
      "raw": 31,
      "label": "Offen"
    },
    "export_to_secondbrain": true,
    "ignore_by_secondbrain": false
  }
}
```

## Fehler- und Sonderfälle

`SecondBrain` sollte folgende Fälle bewusst behandeln:

- Feld existiert in Paperless nicht
- Select-Option-ID ist vorhanden, aber nicht mehr auflösbar
- Dokument hat gar keine `custom_fields`
- `custom_fields` kommen als Liste statt Dictionary
- `monetary` hat unerwartetes Format
- Werte fehlen, weil die Confidence zu niedrig war
- Altbestand wurde noch nicht durch den Backfill geschickt

## Debugging-Hinweise

Wenn ein erwartetes `sb_`-Feld nicht in `SecondBrain` auftaucht, prüfe in
dieser Reihenfolge:

1. Existiert das Feld in Paperless wirklich?
2. Ist `enable_secondbrain_custom_fields: true` aktiv?
3. Wurde das Dokument bereits klassifiziert oder durch den Backfill geschickt?
4. Hat `Paperless-KIplus` den Wert wegen zu geringer Confidence weggelassen?
5. War bereits ein Wert gesetzt und `overwrite_existing=false`?
6. Lässt sich die Select-Option-ID sauber in ein Label zurückübersetzen?

## Nicht-Ziele

Diese Schnittstelle garantiert nicht:

- dass jedes Dokument jedes `sb_`-Feld erhält
- dass KI-Werte fachlich immer korrekt sind
- dass KI-Notizen maschinenstabil bleiben
- dass Select-Option-IDs über Umgebungen hinweg identisch sind

Deshalb sollte `SecondBrain` immer:

- feldnamenbasiert arbeiten
- Selects dynamisch auflösen
- fehlende Werte tolerant behandeln
- KI-Notizen nur ergänzend nutzen
