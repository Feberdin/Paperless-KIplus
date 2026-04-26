# Docker- und Unraid-Betrieb

## Zweck

Diese Betriebsart verschiebt die eigentliche Rechenlast von Home Assistant auf einen eigenstaendigen Docker-Worker. Der Worker ist komplett alleine lauffaehig: Weboberflaeche, API, Log-Export, Pause/Resume und Backfill laufen ohne Home Assistant.

## Architektur

- `src/worker_api.py`: eingebauter Webserver mit HTML-UI und JSON-API
- `src/paperless_ai_sorter.py`: produktive Kernlogik
- `/data/config/config.yaml`: Worker-Konfiguration
- `/data/state/`: Resume-State, Metriken, Stop-Dateien
- `/data/logs/worker.log`: kombinierter Worker-Log
- `/data/exports/`: Platz fuer weitere Export-Artefakte

## Schnellstart mit Docker Compose

```bash
mkdir -p worker-data/config
cp config.example.yaml worker-data/config/config.yaml
docker compose -f docker/docker-compose.example.yml up -d
```

Danach:
- Web UI: `http://<server>:8787/`
- API-Status: `http://<server>:8787/api/status`

## Schnellstart auf Unraid

### Empfohlener Weg: Installationsskript

Das robusteste Setup fuer Unraid ist jetzt das neue Installationsskript:

```bash
bash docker/install-unraid-worker.sh \
  --paperless-url http://192.168.178.20:8000 \
  --paperless-token PAPERLESS_TOKEN \
  --ai-api-key OPENAI_KEY \
  --ai-model gpt-4.1-mini
```

Das Skript:

- erkennt Docker Compose
- legt das Appdata-Verzeichnis an
- erzeugt oder uebernimmt `config.yaml`
- sichert bestehende Dateien vor dem Ueberschreiben
- schreibt einen Compose-Stack fuer GHCR
- startet oder aktualisiert den Container
- prueft `/api/status` per Health-Check

Typische Zusatzoptionen:

```bash
--data-dir /mnt/user/appdata/paperless-kiplus-worker
--worker-token MEIN_API_TOKEN
--enable-tax-enrichment true
--tax-ai-api-key dummy
--tax-ai-model qwen2.5:7b
--tax-ai-base-url http://192.168.178.30:11434/v1
```

### Alternativ: Unraid-Template

1. `docker/unraid-template.xml` als eigenes Template importieren.
2. Ein persistentes Appdata-Verzeichnis fuer `/data` angeben.
3. Container starten.
4. Entweder in der Weboberflaeche die YAML einfuegen oder `config.yaml` unter `/data/config/config.yaml` ablegen.
5. Anschliessend ueber die Weboberflaeche `Run`, `Resume`, `Restart` oder `Backfill` starten.

## Welche Datei ist die produktive Konfiguration?

Im Standalone-Worker ist immer diese Datei massgeblich:

```text
/data/config/config.yaml
```

Wenn du Home Assistant als Steuerzentrale nutzt, kann die Integration diese Datei per API automatisch ueberschreiben.

## Optionale lokale LLMs

Der Worker selbst braucht keine spezielle LLM-Infrastruktur. Er spricht weiterhin OpenAI-kompatible APIs an.

Pragmatische Wege:
- Hauptklassifikation in der Cloud belassen: `ai_base_url` auf OpenAI, `ai_model` auf dein Cloud-Modell
- Kleinere Steuer-Aufgaben lokal ausfuehren:
  - `tax_ai_base_url: http://ollama:11434/v1`
  - `tax_ai_model: qwen2.5:7b`
  - `tax_ai_api_key: dummy`

Wichtig:
- Dein lokaler Endpoint muss OpenAI-kompatibel sein.
- Nicht jedes lokale Modell ist fuer OCR-lastige oder juristische Dokumente gleich gut geeignet.
- Tax Enrichment ist ein guter Startpunkt fuer lokale, kleinere Modelle.

## Wichtige Endpunkte

- `GET /` -> Weboberflaeche
- `GET /api/status` -> aktueller Status fuer UI und Home Assistant
- `GET /api/logs` -> kurzer JSON-Logausschnitt
- `GET /api/logs/download` -> kompletter Log als Text
- `GET /api/config/export` -> aktuelle Worker-Konfiguration als JSON-Payload
- `GET /api/config/download` -> aktuelle Worker-YAML als Download
- `POST /api/config/import` -> neue YAML speichern
- `POST /api/run` -> neuen Lauf starten
- `POST /api/resume` -> pausierten Lauf fortsetzen
- `POST /api/restart` -> frischen Neustart machen
- `POST /api/stop` -> sicher pausieren
- `POST /api/stop_now` -> sofort stoppen

## Debugging

### Container laeuft nicht an
- `docker logs paperless-kiplus-worker`
- Pruefe, ob `/data/config/config.yaml` gueltiges YAML ist.
- Pruefe, ob `paperless_url`, `paperless_token`, `ai_api_key` und `ai_model` gesetzt sind.

### Weboberflaeche ist da, aber Start scheitert
- `GET /api/status` oeffnen und auf `config_validation_message` achten.
- `worker.log` unter `/data/logs/worker.log` pruefen.
- Pruefen, ob Paperless vom Container aus erreichbar ist.

### Resume funktioniert nicht
- Existiert `/data/state/run_state.json`?
- Wurde der Lauf mit `stop` pausiert oder durch Provider-Wartezeit angehalten?
- Bei `stop_now` ist Resume nur ab dem letzten gespeicherten Fortschritt moeglich.
