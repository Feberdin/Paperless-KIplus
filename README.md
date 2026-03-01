# Paperless KIplus Home Assistant Integration

Home-Assistant-Integration für die KI-gestützte Sortierung von Paperless-ngx Dokumenten.

## Fokus dieser README

Diese Datei beschreibt **nur** die Nutzung in Home Assistant (HACS).

- CLI / manuelles Python-Starten: siehe [README_CLI.md](README_CLI.md)

## Installation (HACS)

1. HACS -> `Integrationen` -> `Custom repositories`
2. Repository hinzufügen:
   - URL: `https://github.com/Feberdin/Paperless-KIplus`
   - Kategorie: `Integration`
3. `Paperless KIplus Runner` installieren
4. Home Assistant neu starten
5. Integration hinzufügen: `Einstellungen -> Geräte & Dienste -> Integration hinzufügen`

## Optionen in der Integration

In den Optionen sind nur die fachlich relevanten Felder sichtbar.
Technische Pfade/Befehle sind fest implementiert, um Fehlkonfigurationen zu vermeiden.

### Dry-Run

Wenn **Dry-Run aktiv** ist:

- Es werden **keine Änderungen** in Paperless gespeichert.
- Die KI analysiert Dokumente und erzeugt nur Vorschläge.
- Du siehst in den Logs, was geändert würde (z. B. Typ, Korrespondent, Speicherpfad, Tags, Datum, Notiz).

Verwendung:

- Zum sicheren Testen neuer YAML-Regeln.
- Nach Regeländerungen immer erst 1-2 Dry-Run-Läufe durchführen.

### Alle Dokumente

Wenn **Alle Dokumente aktiv** ist:

- Der Lauf verarbeitet den gesamten Bestand (begrenzt durch `Max. Dokumente`).
- Der übliche YAML-Filter (z. B. `process_only_tag: "#NEU"`) wird für diesen Lauf ignoriert.

Wenn **Alle Dokumente aus** ist:

- Es gelten die Filter/Regeln aus deiner YAML (empfohlen für den Alltag).

### Input-/Output-Kosten

Standardwerte:

- `Input-Kosten pro 1.000 Tokens (EUR)`: `0.0004`
- `Output-Kosten pro 1.000 Tokens (EUR)`: `0.0016`

Quelle der Preisbasis:

- OpenAI Preisseite (GPT-4.1 mini): [https://platform.openai.com/docs/pricing](https://platform.openai.com/docs/pricing)
- Umrechnung aus den dort genannten Preisen pro 1M Tokens auf 1.000 Tokens.

Hinweis:

- Falls du ein anderes Modell/anderen Anbieter nutzt, bitte die beiden Werte anpassen.

### YAML-Konfiguration (immer in HA)

Die YAML wird **immer in Home Assistant** gepflegt.

- Den kompletten YAML-Text im Feld `YAML-Konfiguration (kompletter Inhalt)` einfügen.
- Keine externe YAML-Datei verwenden.

Hilfelink zur YAML-Erstellung mit ChatGPT:

- [ChatGPT Prompt für eigene YAML-Konfig](https://github.com/Feberdin/Paperless-KIplus?tab=readme-ov-file#-chatgpt-prompt-f%C3%BCr-eigene-yaml-konfig)

## Entitäten im Gerät

Alle Entitäten sind einem gemeinsamen Gerät zugeordnet:

- **Paperless KIplus Runner**

Dadurch siehst du die Werte direkt in der Geräte-/Integrationsansicht.

Wichtige Entitäten:

- Letzter Lauf Tokens
- Letzter Lauf Kosten
- Gesamt Tokens
- Gesamtkosten
- Letzte Zusammenfassung (G/A/U/F)
- Letztes Protokoll

## Buttons

### Paperless KIplus Statistiken zurücksetzen

- Setzt Token-/Kostenstatistiken auf 0 (letzter Lauf + Gesamt)
- Schreibt die Werte auch in die Metrik-Datei zurück

### Paperless KIplus Letztes Protokoll exportieren

- Exportiert das letzte Protokoll nach:
  - `/config/www/paperless_kiplus_last_log.txt`
- Download in HA/Browser über:
  - `/local/paperless_kiplus_last_log.txt`

Damit können Nutzer das Log einfach teilen.

## Service

Service: `paperless_kiplus.run`

Optionale Lauf-Overrides:

- `force`
- `wait`
- `dry_run`
- `all_documents`
- `max_documents`

## Icon-Hinweis

Die Integration liefert Branding-Dateien unter `custom_components/paperless_kiplus/brand/`.
Ab Home Assistant 2026.3 können Custom Integrations dieses lokale Branding direkt nutzen.
Wenn trotzdem \"icon not available\" erscheint, bitte HA-Core/HACS aktualisieren und neu starten.

## Support

Bei Fehlern bitte mitsenden:

1. Exportiertes Log (`/local/paperless_kiplus_last_log.txt`)
2. `Fertig. Gescannt=..., Aktualisiert=..., Übersprungen=..., Fehler=...`
3. `Kosten/Token`-Zeile
