# Cloudflare-sichere Langläufer

## Zweck

Dieses Dokument beschreibt den persistenten Job-Vertrag des Standalone-Workers,
die betroffenen Endpunkte und die sichere Migration. Der HTTP-Request nimmt nur
Arbeit an; Paperless-Zugriffe und Sorter-Prozesse laufen danach unabhängig vom
Cloudflare-Request weiter.

Wichtige Invarianten:

- Die HTTP-Annahme liefert schnell `202 Accepted`.
- Job- und Request-ID bleiben über Seiten-Reloads und Worker-Restarts erhalten.
- Es gibt keine erfundenen Prozentwerte oder Restzeiten.
- Schreibende Jobs laufen nie parallel auf derselben Ressource.
- Unterbrochene Schreibjobs werden nicht automatisch wiederholt.
- Parameter, Dokument-IDs, Rohfehler und Secrets erscheinen nicht im Jobstatus.
- `/api/status` und Heimdall melden Image-Version und vollständigen Build-Commit.

Debugging erfolgt über `request_id`, den authentifizierten Jobstatus und die
redigierten Worker-Logs. `PAPERLESS_KIPLUS_LOG_LEVEL=DEBUG` aktiviert zusätzliche
Diagnosemeldungen.

## Endpunkt-Inventar

| Endpunkt | Bewertung | Verhalten |
|---|---|---|
| `POST /api/run` | potenziell lang | `202`, persistenter `sorter_run` |
| `POST /api/resume` | potenziell lang | `202`, persistenter `sorter_resume` |
| `POST /api/restart` | potenziell lang | `202`, ersetzt einen aktiven Sorter kontrolliert |
| `POST /api/review/entities/jobs` | potenziell lang, nur lesend | `202`, persistenter `review_scan` |
| `POST /api/review/merge` | potenziell lang, schreibend | `202`, persistenter `review_merge` |
| `GET /api/jobs/<job_id>` | kurz | authentifizierter persistenter Status |
| `GET /api/review/entities` | früher blockierend | jetzt `405` mit Verweis auf den Job-Endpunkt |
| `GET /api/status`, `/api/logs`, `/api/config/*`, `/api/review/rules` | kurz | direkte lokale Antwort |
| `POST /api/stop`, `/api/stop_now` | kurz | setzt Stop-Signal bzw. terminiert den Prozess |
| `POST /api/config/import`, `/api/review/rules`, Reset-Endpunkte | kurz | validierter lokaler Dateizugriff |
| `/`, `/review`, `/api/heimdall/v1` | kurz | UI bzw. redigierter Health-Status |

Der JSON-Body ist auf 1 MiB begrenzt. API-, Log- und Config-Antworten senden
`Cache-Control: no-store`, sodass Cloudflare und Browser private Daten nicht als
Cacheobjekt behandeln.

## HTTP-Vertrag

Ein Client kann optional einen maximal 200 Zeichen langen `Idempotency-Key`
senden. Dieselbe Operation mit demselben Schlüssel erhält erneut denselben Job.

```http
POST /api/run
Authorization: Bearer <WORKER_TOKEN>
Idempotency-Key: <EINMALIGER_CLIENTSCHLUESSEL>
Content-Type: application/json

{"dry_run":true,"max_documents":10}
```

```json
{
  "ok": true,
  "status": "queued",
  "job_id": "job_<zufaellige_id>",
  "request_id": "req_<zufaellige_id>",
  "status_url": "/api/jobs/job_<zufaellige_id>",
  "deduplicated": false
}
```

Der Status liefert `queued`, `running`, `succeeded`, `failed`, `interrupted`
oder `cancelled`. Unbekannte Werte bleiben explizit `null`:

```json
{
  "status": "running",
  "phase": "discovering_documents",
  "progress_percent": null,
  "estimated_seconds_remaining": null,
  "attempt_count": 1,
  "max_attempts": 1
}
```

Terminale Fehler enthalten nur Fehlercode, sichere Handlungsanweisung und
`request_id`. Rohantworten externer APIs werden weder persistiert noch an die UI
zurückgegeben.

## Persistenz, Parallelität und Restart

Die additive SQLite-Datenbank liegt unter
`/data/state/background_jobs.sqlite3`. Transaktionen und ein partieller Unique
Index vergeben pro Ressourcenklasse genau einen aktiven Besitzer:

- `paperless_write`: Sorter-Läufe und echte Entity-Merges
- `review_scan`: nur lesende Entity-Scans

Ein Restart darf ausschließlich aktive Sorter-Jobs ersetzen. Ein gleichzeitig
laufender Merge führt weiterhin zu `409 Conflict`. Der Executor ist auf drei
Threads begrenzt; Jobausführungen haben genau einen Versuch. Bestehende,
ebenfalls begrenzte Paperless-HTTP-Retries bleiben davon unberührt.

Beim Worker-Start gilt:

- `review_scan` wird mit den minimal persistierten Parametern sicher neu geplant.
- Sorter- und Merge-Jobs werden `interrupted` und niemals automatisch erneut
  ausgeführt.
- Ein unterbrochener Merge kann bereits einzelne Dokumente aktualisiert haben.
  Vor einem manuellen Retry muss der Paperless-Zustand geprüft werden.
- Der Sorter behält zusätzlich seinen bestehenden Resume-State unter `/data/state`.

Terminale Jobs werden nach 24 Stunden gelöscht; zusätzlich bleiben höchstens
200 terminale Datensätze erhalten.

## Browser und Home Assistant

Beide eingebetteten UIs speichern nur Job-, Request- und Status-URL im
`localStorage`. Das Bearer-Token liegt ausschließlich im `sessionStorage` und
wird nach dem Schließen des Tabs verworfen. Beim Reload werden aktive Jobs mit
exponentiellem Backoff von 750 ms bis maximal 10 s weiter beobachtet.

Die Home-Assistant-Remote-Steuerung akzeptiert sowohl alte Sofortantworten als
auch den neuen 202-Vertrag. Neue Aktionen senden einen Idempotency-Key und
pollen die konkrete `status_url`, bis der Job terminal ist.

## Cloudflare-Betrieb

Ein separater Cloudflare Worker oder eine Durable Object Instanz ist nicht
erforderlich: Cloudflare sieht nur kurze Admission- und Status-Requests. Für
den Proxy müssen lediglich dieselben Pfade zum Origin durchgereicht und
Caching für API-Antworten deaktiviert bleiben; der Origin setzt dafür bereits
`no-store`.

## Migration und Rollback

Migration:

1. Neues Image mit unverändertem `/data`-Mount deployen.
2. Worker-Health und `GET /api/status` prüfen.
3. Einen kleinen Dry-Run starten und die gelieferte `status_url` bis
   `succeeded` pollen.
4. Browser neu laden und prüfen, dass der Job weiterhin angezeigt wird.

Die SQLite-Tabelle wird beim Start additiv angelegt. Bestehende Config-,
Metrik-, Log- und Resume-Dateien werden nicht verschoben.

Rollback:

1. Im Broker den vorherigen Git-Commit und dessen Image-Digest planen.
2. Plan prüfen und anwenden.
3. Die neue SQLite-Datei darf liegen bleiben; ältere Worker-Versionen ignorieren
   sie. Für einen späteren Roll-forward bleibt dadurch die Diagnose erhalten.

## Lokale Qualitätssicherung

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest -q
docker compose -f docker/docker-compose.unraid-broker.yml config --quiet
docker build -f docker/Dockerfile -t paperless-kiplus-worker:test .
```

Negativfälle und fünfmal wiederholte Parallelitätsläufe sind Teil der
automatisierten Tests. Die produktive Bereitstellung erfolgt ausschließlich
über den Unraid Deployment Broker.
