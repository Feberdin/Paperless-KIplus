# Paperless-KIplus 🤖📄

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-41BDF5.svg)](https://www.home-assistant.io/)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

KI-gestützte Klassifizierung für **Paperless-ngx** mit Fokus auf nachvollziehbare Änderungen, robuste Regeln und gute Debugbarkeit.

## 📚 Inhalt

- [✨ Features](#-features)
- [🏠 Home Assistant (HACS Integration)](#-home-assistant-hacs-integration)
- [⚙️ Konfiguration](#️-konfiguration)
- [🧩 Konfigurationsoptionen](#-konfigurationsoptionen)
- [▶️ Nutzung](#️-nutzung)
- [📝 KI-Notizen](#-ki-notizen)
- [🧯 Fehleranalyse](#-fehleranalyse)

## ✨ Features

- KI-Klassifizierung für:
  - `document_type`
  - `correspondent`
  - `storage_path`
  - `tags`
  - `document_date` (`created`)
- Optional strukturierte **Basis-Konfiguration** (`basis_config`) für Stammdaten & harte Regeln
- Optionaler Tag-Filter (`process_only_tag`, z. B. `#NEU`)
- Override per CLI: `--all-documents`
- Harte Tag-Regeln im Code:
  - `KI` hinzufügen
  - `#NEU` entfernen
- Optionale KI-Notizen in Paperless (`/api/documents/{id}/notes/`):
  - Kurz-Zusammenfassung (optional)
  - Begründung
  - Änderungsübersicht
- Umfangreiche Logs + Retry/Backoff
- `--dry-run` mit Feld-Diff-Tabelle (`Aktuell -> Neu`)
- Automatische Endzusammenfassung:
  - Neu angelegte Entitäten (Tags, Korrespondenten, Dokumenttypen, Speicherpfade)
  - Detaillierte Fehlerliste pro Dokument

## 🗂️ Projektstruktur

- `src/paperless_ai_sorter.py` - Hauptskript
- `config.example.yaml` - Beispielkonfiguration
- `requirements.txt` - Abhängigkeiten
- `custom_components/paperless_kiplus/` - Home Assistant HACS Integration
- `hacs.json` - HACS Metadaten

## ✅ Voraussetzungen

- Python 3.10+
- Laufende Paperless-ngx Instanz
- Paperless API Token
- KI-API-Key (OpenAI-kompatibel)

## 🚀 Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 🏠 Home Assistant (HACS Integration)

### 1) HACS installieren

1. In HACS auf `Integrations` gehen
2. `Custom repositories` öffnen
3. Dieses Repo hinzufügen:
   - URL: `https://github.com/Feberdin/Paperless-KIplus`
   - Kategorie: `Integration`
4. `Paperless KIplus Runner` installieren
5. Home Assistant neu starten

### 2) Integration in HA einrichten

In Home Assistant:

- `Einstellungen -> Geräte & Dienste -> Integration hinzufügen`
- Suche nach `Paperless KIplus Runner`
- Trage ein:
  - `Command` (z. B. `.venv/bin/python src/paperless_ai_sorter.py`)
  - `Working Directory` (Pfad zum Repo auf deinem HA-Host)
  - `Cooldown`

### 3) Trigger über Paperless Inbox Sensor

Beispiel-Automation (wenn Inbox > 0, dann Runner starten):

```yaml
alias: Paperless KI Runner bei Inbox
mode: single
trigger:
  - platform: numeric_state
    entity_id: sensor.paperless_dokumente_im_posteingang
    above: 0
    for: "00:02:00"
action:
  - service: paperless_kiplus.run
    data:
      force: false
      wait: false
```

Hinweis:

- Wenn du `process_only_tag: \"#NEU\"` nutzt, verarbeitet das Skript weiterhin nur diese Dokumente.
- Mit Service-Option `force: true` kannst du den Cooldown ignorieren.

## ⚙️ Konfiguration

1. Kopieren:

```bash
cp config.example.yaml config.yaml
```

2. Pflichtfelder setzen:

- `paperless_url`
- `paperless_token`
- `ai_api_key`
- `ai_model`

3. Erster Test:

- `dry_run: true`
- `max_documents: 5`

## 🧩 Konfigurationsoptionen

| Option | Typ | Default | Bedeutung |
|---|---|---:|---|
| `paperless_url` | string | - | Basis-URL von Paperless |
| `paperless_token` | string | - | Paperless API Token |
| `ai_api_key` | string | - | KI API Key |
| `ai_model` | string | - | KI Modellname |
| `ai_base_url` | string | `https://api.openai.com/v1` | OpenAI-kompatible API-Basis |
| `max_documents` | int | `25` | Maximale Anzahl Dokumente pro Lauf |
| `dry_run` | bool | `true/false` | Nur anzeigen statt schreiben |
| `create_missing_entities` | bool | `true` | Fehlende Tags/Typen/Korrespondenten/Speicherpfade anlegen |
| `confidence_threshold` | float | `0.70` | Mindest-Confidence |
| `request_timeout_seconds` | int | `30` | Request-Timeout |
| `log_level` | string | `INFO` | `DEBUG/INFO/WARNING/ERROR` |
| `enable_token_precheck` | bool | `false` | API-Token-Restbudget vorab prüfen |
| `min_remaining_tokens` | int | `1500` | Schwellwert für Token-Precheck |
| `custom_prompt_instructions` | multiline string | `""` | Freitext-Regeln für KI |
| `basis_config` | object | `{}` | Strukturierte Stammdaten/Regeln |
| `process_only_tag` | string | `""` | Nur Dokumente mit diesem Tag verarbeiten |
| `include_existing_entities_in_prompt` | bool | `true` | Vorhandene Werte als Kontext an KI geben |
| `enable_ai_notes` | bool | `true` | KI-Notizen in Paperless speichern |
| `ai_notes_max_chars` | int | `800` | Max. Länge Begründung in Notiz |
| `enable_ai_note_summary` | bool | `true` | Kurz-Zusammenfassung in Notiz |
| `ai_note_summary_max_chars` | int | `220` | Max. Länge Kurz-Zusammenfassung |

## 🧭 ChatGPT Prompt Für Eigene YAML-Konfig

Kopiere den folgenden Prompt in ChatGPT, um deine persönliche `config.yaml` strukturiert erstellen zu lassen:

```text
Du bist mein YAML-Konfigurations-Assistent für ein Paperless-KI-Projekt.

Ziel:
- Führe mich Schritt für Schritt durch alle relevanten Angaben.
- Stelle pro Schritt nur wenige, klare Fragen.
- Warte auf meine Antworten, bevor du zum nächsten Schritt gehst.
- Wenn etwas unklar ist, frage gezielt nach.
- Arbeite strukturiert und nummeriert.

Am Ende sollst du mir eine vollständige `config.yaml` ausgeben mit:
1) Basisfeldern (paperless_url, ai_model, etc.)
2) custom_prompt_instructions (kompakt, verständlich)
3) basis_config im folgenden Schema:
   - people (owner, household, contacts)
   - organizations
   - identifiers
   - classification_rules
   - guardrails
4) Notiz- und Summary-Optionen

Wichtige Anforderungen:
- Nutze korrekte YAML-Syntax.
- Nutze sinnvolle Defaults, wenn ich keine Werte habe.
- Trenne klar zwischen harten Regeln und optionalen Präferenzen.
- Erzeuge am Ende nur den YAML-Inhalt in einem Codeblock.

Frageblöcke, die du nacheinander abarbeiten sollst:
- Block A: Technische Basis (URLs, Modell, Dry-Run, Limits)
- Block B: Personen/Haushalt/Kontakte
- Block C: Firmen, Vereine, Aliase, Speicherpfade
- Block D: Identifikatoren (Zähler, Kunden-/Steuer-/Vertragsnummern)
- Block E: Dokumenttyp-Regeln (Rechnung, Rechtsfälle, Sonderfälle)
- Block F: Korrespondent-Regeln (Normalisierung, z. B. Hotel)
- Block G: Tag-Regeln (Jahres-Tag, KI/#NEU, Sparsamkeit)
- Block H: Guardrails (verbotene Zuordnungen, Fallbacks)
- Block I: KI-Notizen (ein/aus, Summary ein/aus, Längenlimits)

Starte jetzt mit Block A und stelle mir die ersten Fragen.
```

## 🧠 `basis_config` Standard (empfohlen)

Verwende dieses Schema für langlebige, gut wartbare Regeln:

- `people`
  - `owner`
  - `household`
  - `contacts`
- `organizations`
  - `employer_current`
  - `employer_former`
  - `clubs`
- `identifiers`
  - `meters`
  - (optional: `customer_numbers`, `tax_numbers`, `contract_numbers`)
- `classification_rules`
  - `document_type`
  - `correspondent`
  - `storage_path`
  - `tags`
  - `date`
- `guardrails`
  - `forbidden_path_assignments`

## ▶️ Nutzung

Standardlauf:

```bash
python src/paperless_ai_sorter.py
```

Dry-Run:

```bash
python src/paperless_ai_sorter.py --dry-run
```

Einmal alles durchsuchen (ignoriert Tag-Filter + Skip-Heuristik):

```bash
python src/paperless_ai_sorter.py --all-documents
```

## 🧪 Empfohlener Rollout

1. `dry_run: true`, `max_documents: 5`
2. Ergebnisse in Paperless prüfen
3. `dry_run: false`
4. `max_documents` schrittweise erhöhen

## 📝 KI-Notizen

Wenn `enable_ai_notes: true`, wird pro Änderung eine Notiz erzeugt mit:

- Zeitstempel
- optionaler Kurz-Zusammenfassung
- Begründung
- geänderten Feldern

## 🔒 Sicherheit

- `config.yaml` ist in `.gitignore`
- Keine Secrets committen
- Bei Unsicherheit vor erstem Echtlauf Backup/Snapshot von Paperless anlegen

## 🧯 Fehleranalyse

- `[CONFIG-ERROR]` => Konfigurationsproblem (Exit `2`)
- API-/KI-Fehler => pro Dokument geloggt
- Unerwarteter Fehler => Stacktrace + Exit `1`

## 📜 Lizenz

Noch keine Lizenz gesetzt. Optional: MIT.
