# Geschützte Exporte, Version 1.4.22

## Änderung

Lokaler Runner und Remote-Runner schreiben Protokolle und effektive
Worker-Konfigurationen in eine konfigurierte HA-Medienquelle. Öffentliche
`/config/www`-Dateien und `/local`-Links werden für diese Exporte nicht mehr
erzeugt. Medienquellen innerhalb von `www` und andere Dateinamen werden abgelehnt.
Historische Exporte werden nicht automatisch gelöscht.

Dateien: `private_exports.py` (Zielauflösung), `runner.py` und `remote_runner.py`
(vier Exportpfade), `button.py` (Beschreibung), `tests/test_private_exports.py`.

## Rot → Grün

Befehl für beide Phasen:

```sh
python3 -m unittest discover -s tests -p test_private_exports.py -v
```

Vor der Korrektur scheiterten beide Protokollexporter fachlich, weil sie
`/local/paperless_kiplus_last_log.txt` zurückgaben. Ein zweiter Test scheiterte
für beide Konfigurationsexporter, weil keine Datei im geschützten Medienpfad
entstand. Nach der Korrektur sind beide Verhaltenstests einschließlich lokaler
und entfernter Variante grün. Zwei zusätzliche Tests decken fehlende/falsche
Medienkonfiguration, Pfadmanipulation und die Standardquelle `local` ab.

Die Tests führen die unveränderten tatsächlichen Exportmethoden isoliert aus.
Nur Home Assistant, Remote-Abruf und der alte absolute Dateisystemzugriff sind
ersetzt. Es werden ausschließlich synthetische Daten und temporäre Verzeichnisse
verwendet. Auf macOS werden temporäre Pfade wegen `/var` → `/private/var`
vor dem Vergleich normalisiert.

## Weitere Prüfungen

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q custom_components/paperless_kiplus tests/test_private_exports.py
git diff --check
docker run --rm -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest
```

Die vollständige Suite enthält 98 Tests. Hassfest meldet keine ungültige
Integration; die bestehende CONFIG_SCHEMA-Warnung ist davon unabhängig.
Ein eigenständiger Formatter, Linter, Typechecker oder Build ist in diesem
Python-Integrationsprojekt nicht eingerichtet. GitHub prüft zusätzlich HACS,
Versionssprung und die Regressionstests.

## Betrieb und Grenzen

Vor dem Update eine funktionierende Medienquelle einrichten. Bei HTTP 401
Authentifizierung des Download-Clients prüfen; im normalen Browser wird der
HA-Zugriffstoken nicht automatisch an direkte Medien-URLs angehängt.
Protokollinhalte bleiben privat und dürfen nicht in Fehlerausgaben übernommen
werden. Bereits veröffentlichte Dateien separat sichern und entfernen; eventuell
darin enthaltene Geheimnisse gegebenenfalls rotieren.

Die Tests ersetzen keine Prüfung der installierten HA-Medienroute. Nach dem
Deployment anonymen Zugriff (401/403) und authentifizierten Zugriff (200) prüfen.
