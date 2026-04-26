# Migration von Home Assistant zum Remote-Worker

## Ziel

Home Assistant bleibt deine Steuerzentrale und Visualisierung. Die eigentliche Verarbeitung zieht auf einen Docker-Worker um.

## Empfohlene Reihenfolge

1. Worker auf Unraid oder Docker starten.
2. Die Worker-Weboberflaeche lokal testen.
3. In Home Assistant `execution_mode` auf `remote_worker` umstellen.
4. `remote_worker_url` und optional `remote_worker_token` eintragen.
5. Einmal `Worker-Konfiguration exportieren` ausfuehren.
6. Danach Testlauf ueber Home Assistant starten.

## Was Home Assistant dann noch macht

- Buttons und Services bereitstellen
- Fortschritt, Status, Kosten und Logs anzeigen
- Konfiguration zentral im YAML-Feld halten
- auf Wunsch die Konfiguration direkt zum Worker synchronisieren

## Was der Worker uebernimmt

- eigentliche Dokumentverarbeitung
- Pause/Resume/Restart
- Logspeicherung
- Laufzustand und Backfill-Status

## Typische Stolperstellen

### Aenderungen in Home Assistant greifen nicht sofort
- Nutze den Button `Worker-Konfiguration exportieren`.
- Oder aktiviere `remote_worker_sync_config`, damit vor jedem Lauf automatisch synchronisiert wird.

### Home Assistant zeigt nur Fehler vom Worker
- Das ist Absicht. Die Ursache liegt dann meist im Worker-Log oder in `config_validation_message`.

### Neue Konfiguration wurde exportiert, aber der alte Lauf macht weiter
- Nutze `Paperless KIplus Lauf neu starten` statt `Resume`.
- `Resume` setzt bewusst den pausierten Zustand fort.
