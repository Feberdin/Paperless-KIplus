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

Vor einem PR bitte aus dem Repository-Root ausführen:

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest -q
docker compose -f docker/docker-compose.unraid-broker.yml config --quiet
docker build -f docker/Dockerfile -t paperless-kiplus-worker:test .
```

Bei Änderungen an Hintergrundjobs zusätzlich mindestens einen Happy Path,
einen ungültigen Input, Restart-Recovery und fünf wiederholte parallele
Admission-Läufe testen. Echte Paperless- oder API-Token dürfen nie in Fixtures,
Logs oder Fehlermeldungen erscheinen.

Für gezieltes Debugging:

```bash
PAPERLESS_KIPLUS_LOG_LEVEL=DEBUG python3 src/worker_api.py --data-dir ./worker-data
python3 -m unittest tests.test_background_jobs -v
```

Anschließend die Integration in Home Assistant laden und einen kleinen Dry-Run
gegen eine Testinstanz durchführen. README und Betriebsdoku müssen das neue
Verhalten erklären.

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
