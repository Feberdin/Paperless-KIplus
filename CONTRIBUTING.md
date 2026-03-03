# Contributing

Vielen Dank für dein Interesse an Beiträgen zu **Paperless KIplus**.

## Voraussetzungen

- Home Assistant Testumgebung
- Zugriff auf eine Paperless-ngx Instanz (für Integrations-Tests)
- Python 3

## Entwicklungsprinzipien

- Kleine, nachvollziehbare Änderungen pro Pull Request
- Klare Commit-Messages (Deutsch oder Englisch, technisch präzise)
- Robuste Fehlerbehandlung mit verwertbaren Logs
- Rückwärtskompatibilität für bestehende Nutzerkonfigurationen

## Lokale Checks

Vor einem PR bitte mindestens:

1. Syntax prüfen:
   - `python3 -m py_compile custom_components/paperless_kiplus/*.py src/paperless_ai_sorter.py`
2. Integration laden und einen Testlauf in Home Assistant durchführen
3. Prüfen, dass README/Docs bei neuen Features aktualisiert sind

## Pull-Request Ablauf

1. Fork/Branch erstellen
2. Änderung umsetzen
3. Tests/Checks durchführen
4. Pull Request mit klarer Beschreibung einreichen

Bitte im PR enthalten:

- Was wurde geändert?
- Warum ist die Änderung nötig?
- Welche Risiken/Nebenwirkungen gibt es?
- Wie wurde getestet?

## Versionierung und Releases

- Bei Änderungen, die Nutzer betreffen, wird die Version in
  `custom_components/paperless_kiplus/manifest.json` erhöht.
- Releases werden über Git-Tags veröffentlicht.
