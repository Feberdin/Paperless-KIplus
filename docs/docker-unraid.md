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

## Produktion auf Unraid

In der Feberdin-Umgebung wird ausschließlich die GitOps-Quelle
`docker/docker-compose.unraid-broker.yml` über den Unraid Deployment Broker
bereitgestellt. Direkte SSH-, Shell-, Docker-CLI- und HTTP-Deployments sind
nicht Teil dieses Betriebswegs.

Voraussetzungen:

- Das Repository ist im Broker registriert.
- Die Stack-Quelle zeigt auf einen vollständigen Commit-SHA.
- `PAPERLESS_KIPLUS_TOKEN` ist im Broker-Secret-Store vorhanden.
- Das bestehende Appdata-Verzeichnis `/mnt/user/appdata/paperless-kiplus`
  bleibt erhalten.

Sicherer Ablauf:

1. `stack_source_status`
2. `stack_validate`
3. `deploy_plan`
4. `approval_request`, falls erforderlich
5. `deploy_apply`
6. `deployment_status`, `docker_list` und `logs_tail`

Das Compose referenziert das Secret ausschließlich als
`secret://PAPERLESS_KIPLUS_TOKEN`; der Broker injiziert es erst beim Apply. Das
Worker-Image wird lokal aus dem brokergebundenen Git-Checkout gebaut. Der
Dockerfile pinnt Basisimage und Python-Abhängigkeiten; die Compose-Build-Args
halten den erfolgreich geprüften App-Commit und die App-Version fest. Damit
benötigt die Produktion keinen privaten Registry-Pull.

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
- `GET /review` -> Review-Seite fuer doppelte Dokumenttypen/Korrespondenten
- `GET /api/status` -> aktueller Status fuer UI und Home Assistant
- `GET /api/logs` -> kurzer JSON-Logausschnitt
- `GET /api/logs/download` -> kompletter Log als Text
- `GET /api/config/export` -> aktuelle Worker-Konfiguration als JSON-Payload
- `GET /api/config/download` -> aktuelle Worker-YAML als Download
- `GET /api/jobs/<job_id>` -> persistenter Jobstatus
- `POST /api/review/entities/jobs` -> asynchroner Dopplungsscan (`202`)
- `GET /api/review/entities` -> `405`, veralteter blockierender Zugriff
- `GET /api/review/rules` -> gespeicherte KI-Regeln fuer Entity-Zuordnungen
- `POST /api/review/rules` -> Alias-/Ziel-Regel oder "kein Duplikat" speichern
- `POST /api/review/merge` -> Merge asynchron planen oder anwenden (`202`)
- `POST /api/config/import` -> neue YAML speichern
- `POST /api/run` -> neuen Lauf als Job starten (`202`)
- `POST /api/resume` -> pausierten Lauf als Job fortsetzen (`202`)
- `POST /api/restart` -> kontrollierten Neustart als Job starten (`202`)
- `POST /api/stop` -> sicher pausieren
- `POST /api/stop_now` -> sofort stoppen

Die Review-Regeln liegen im Worker standardmaessig unter
`/data/state/entity_review_rules.json`. Der Worker uebergibt diese Datei beim
Start automatisch an den Sorter. Dadurch bevorzugt die KI beim naechsten Lauf
die geprueften Zielwerte und legt sinngleiche Dokumenttypen oder
Korrespondenten nicht erneut an.

## Debugging

### Container laeuft nicht an
- In Produktion `deployment_status`, `docker_list` und `logs_tail` im Broker
  prüfen. Lokal darf `docker compose logs paperless-kiplus-worker` verwendet
  werden.
- Pruefe, ob `/data/config/config.yaml` gueltiges YAML ist.
- Pruefe, ob `paperless_url`, `paperless_token`, `ai_api_key` und `ai_model` gesetzt sind.
- Der produktive Broker-Stack startet den Worker bewusst ohne Root-Rechte als
  Unraid-Benutzer `99:100`. Meldet der Start fehlende Schreibrechte fuer
  `/data`, muss das bestehende Appdata-Verzeichnis diesem Benutzer bzw. der
  Gruppe gehoeren; die Anwendung darf dafuer nicht als Root gestartet werden.

### Weboberflaeche ist da, aber Start scheitert
- `GET /api/status` oeffnen und auf `config_validation_message` achten.
- `worker.log` unter `/data/logs/worker.log` pruefen.
- Pruefen, ob Paperless vom Container aus erreichbar ist.

### Resume funktioniert nicht
- Existiert `/data/state/run_state.json`?
- Wurde der Lauf mit `stop` pausiert oder durch Provider-Wartezeit angehalten?
- Bei `stop_now` ist Resume nur ab dem letzten gespeicherten Fortschritt moeglich.

### Job bleibt nach einem Worker-Restart stehen

- `GET /api/jobs/<job_id>` mit dem Worker-Token prüfen.
- Read-only-Scans werden einmal sicher fortgesetzt.
- Schreibjobs erhalten absichtlich `interrupted`; Paperless-Zustand prüfen und
  erst danach kontrolliert erneut auslösen.
- Mit `request_id` in den redigierten Broker-Logs suchen.

Details zu Idempotenz, Parallelität, Aufbewahrung und Rollback stehen unter
[Cloudflare-sichere Langläufer](./cloudflare-long-running-jobs.md).
