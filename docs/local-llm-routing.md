# Lokale LLMs fuer kleinere Aufgaben

## Was heute schon geht

Paperless KIplus kann bereits OpenAI-kompatible Endpunkte verwenden. Damit lassen sich lokale Server auf Unraid direkt einbinden.

## Sinnvolle erste Aufteilung

- `ai_*` bleibt auf einem starken Cloud-Modell fuer die Hauptklassifikation
- `tax_ai_*` zeigt auf ein lokales kleineres Modell fuer Steuer-Extraktionen

## Beispiel

```yaml
ai_api_key: <OPENAI_KEY>
ai_model: gpt-4.1-mini
ai_base_url: https://api.openai.com/v1

enable_tax_enrichment: true
tax_ai_api_key: dummy
tax_ai_model: qwen2.5:7b
tax_ai_base_url: http://ollama:11434/v1
```

## Warum gerade Tax Enrichment?

- Die Aufgabe ist enger begrenzt als die komplette Dokumentklassifikation.
- Du kannst schrittweise Qualitaet und Kosten vergleichen.
- Ein lokales Modell spart dabei Cloud-Tokens, ohne deinen Hauptpfad sofort umzustellen.

## Debug-Hinweise

- Im Log steht jetzt explizit, welches Modell und welche Base-URL fuer Tax Enrichment aktiv sind.
- Wenn lokale Aufrufe scheitern, pruefe zuerst:
  - Endpoint erreichbar?
  - Modell wirklich vorhanden?
  - OpenAI-kompatibler `/v1/chat/completions` Pfad vorhanden?
