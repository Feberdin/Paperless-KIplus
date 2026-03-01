# Paperless KIplus CLI

Diese Datei beschreibt den manuellen Betrieb per Python/CLI.

## Start

```bash
python src/paperless_ai_sorter.py
```

## Wichtige Flags

- `--dry-run`
- `--all-documents`
- `--max-documents <n>`
- `--config <pfad>`

## Beispiel

```bash
python src/paperless_ai_sorter.py --config config.yaml --dry-run --max-documents 10
```

## Hinweis

Für produktive Nutzung wird die Home-Assistant-Integration empfohlen:

- [README.md](README.md)
