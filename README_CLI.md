# Paperless KIplus CLI

Diese Datei beschreibt den manuellen Betrieb per Python/CLI.

## Start

```bash
python src/paperless_ai_sorter.py
```

## Wichtige Flags

- `--dry-run`
- `--all-documents`
- `--backfill-existing-documents`
- `--force-secondbrain-backfill`
- `--added-today`
- `--added-on <YYYY-MM-DD>`
- `--created-on <YYYY-MM-DD>`
- `--max-documents <n>`
- `--config <pfad>`

## Beispiel

```bash
python src/paperless_ai_sorter.py --config config.yaml --dry-run --max-documents 10
```

Heute hinzugefügte Dokumente mit neuen SecondBrain-Regeln neu anreichern:

```bash
python src/paperless_ai_sorter.py \
  --config config.yaml \
  --backfill-existing-documents \
  --force-secondbrain-backfill \
  --added-today
```

## Hinweis

Für produktive Nutzung wird die Home-Assistant-Integration empfohlen:

- [README.md](README.md)
