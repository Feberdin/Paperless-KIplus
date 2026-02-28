# Paperless-KIplus

KI-gestützte Klassifizierung für **Paperless-ngx**.

Dieses Projekt lädt Dokumente aus Paperless, lässt sie über ein LLM klassifizieren und schreibt strukturierte Metadaten (Dokumenttyp, Korrespondent, Ablagepfad, Tags) zurück.

## Features

- Automatische Dokument-Klassifizierung über ein OpenAI-kompatibles API
- Rückschreiben von:
  - `document_type`
  - `correspondent`
  - `storage_path`
  - `tags`
- Konfigurierbarer Confidence-Threshold
- `--dry-run` für sichere Testläufe
- Umfangreiche Logs und klare Fehlermeldungen
- Optionales Auto-Anlegen fehlender Entitäten in Paperless

## Projektstruktur

- `src/paperless_ai_sorter.py`: Hauptskript mit API-Client, KI-Klassifizierung und Update-Logik
- `config.example.yaml`: Beispiel-Konfiguration
- `requirements.txt`: Python-Abhängigkeiten

## Voraussetzungen

- Python 3.10+
- Laufende Paperless-ngx Instanz
- Paperless API Token
- API-Key für ein OpenAI-kompatibles Modell

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Konfiguration

1. Beispiel kopieren:

```bash
cp config.example.yaml config.yaml
```

2. `config.yaml` anpassen:

- `paperless_url`
- `paperless_token`
- `ai_api_key`
- `ai_model`

Für den ersten Lauf:

- `dry_run: true`
- `log_level: "DEBUG"` (optional für detaillierte Analyse)

## Nutzung

Standardlauf (nimmt `config.yaml`):

```bash
python src/paperless_ai_sorter.py
```

Explizit mit Datei + Dry Run:

```bash
python src/paperless_ai_sorter.py --config config.yaml --dry-run
```

## Wie die Verarbeitung funktioniert

1. Metadaten-Mappings werden aus Paperless geladen (Tags, Typen, Korrespondenten, Ablagepfade)
2. Dokumente werden seitenweise abgefragt
3. Pro Dokument erstellt die KI eine strukturierte JSON-Antwort
4. Die Antwort wird validiert (Pflichtfelder + Datentypen + Confidence)
5. Bei ausreichender Confidence werden IDs aufgelöst und als PATCH zurückgeschrieben

## Fehlerbehandlung und Debugging

Das Skript ist auf transparente Fehleranalyse ausgelegt:

- Konfigurationsfehler führen zu `[CONFIG-ERROR]` und Exit-Code `2`
- API- und KI-Fehler werden pro Dokument geloggt, der Lauf geht weiter
- Unerwartete globale Fehler werden mit Stacktrace ausgegeben und enden mit Exit-Code `1`
- HTTP-Requests haben Retry + Backoff bei transienten Fehlern

Empfohlener Debug-Workflow:

1. Mit `dry_run: true` starten
2. `log_level: "DEBUG"` setzen
3. Eine kleine `max_documents` Zahl verwenden (z. B. `5`)
4. Logs prüfen, dann `dry_run: false`

## Sicherheit

- `config.yaml` ist in `.gitignore`, damit Tokens nicht committed werden
- Keine Secrets in der README oder im Quellcode hinterlegen

## Nächste sinnvolle Ausbaustufen

- Filter auf `inbox`/unklassifizierte Dokumente direkt in der API-Abfrage
- Wiederholte Fehlklassifizierung über Feedback-Loop verbessern
- Prompt-Versionierung und A/B-Vergleich
- Optionaler Modus für reines Tagging ohne Typ/Korrespondent

## Lizenz

Aktuell keine Lizenz hinterlegt. Wenn gewünscht, kann eine `MIT`-Lizenz ergänzt werden.
