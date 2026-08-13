# Changelog

## Unreleased

### Added

- Persistente, idempotente HTTP-202-Hintergrundjobs für Sorter, Restart,
  Entity-Scan und Entity-Merge.
- Authentifizierter Jobstatus mit sicheren Request-IDs, exaktem Fortschritt
  und Reload-Recovery in beiden Weboberflächen.
- Restart-, Parallelitäts-, Redaction-, API- und Docker-CI-Tests.

### Changed

- Home Assistant pollt Remote-Jobs bis zum terminalen Status.
- Browser-Token werden nur noch sitzungsbezogen gespeichert.
- Lange Live-Review-Abfragen verwenden den Job-Endpunkt.

### Security

- API-, Log- und Konfigurationsantworten sind nicht cachebar.
- Jobfehler und Worker-Logs maskieren Zugangsdaten und Providerdetails.
- Unterbrochene Schreibjobs werden nicht automatisch wiederholt.
- Python-Basisimage und Runtime-Abhängigkeiten sind reproduzierbar gepinnt.
- Produktion baut commitgebunden im Broker und benötigt keinen privaten
  Registry-Pull.
