# OCR / extraction pipeline

Turns one electoral-roll PDF into reconciled, structured elector records.

```
rasterize/segment ─► [pluggable extractor] ─► assemble (dedupe + status) ─► reconcile (vs page-N summary)
```

The **extractor is swappable** — any engine that can turn an image + JSON schema +
instruction into JSON implements `ocr.extractors.base.Extractor`. Shipped:

| name        | engine                              | use |
|-------------|-------------------------------------|-----|
| `qwen2.5vl` | local Qwen2.5-VL via Ollama         | real runs (Mac/GPU) |
| `mock`      | returns empty schema-shaped data    | wiring tests, no model needed |

## Prerequisites
- **poppler** (`pdftoppm`, `pdfinfo`) — already on this machine.
- For `qwen2.5vl`: **Ollama** running + the vision model pulled:
  ```
  ollama pull qwen2.5vl:3b
  ```
  (Note: `qwen2.5:7b` is text-only and will NOT work — you need `qwen2.5vl`.)

No Python pip dependencies — stdlib only.

## Run
```
# wiring test (no model):
python3 -m ocr.cli /path/to/roll.pdf --extractor mock -v

# real extraction on your Mac:
python3 -m ocr.cli /path/to/roll.pdf --extractor qwen2.5vl --model qwen2.5vl:3b -v --out result.json
```
Exit code is 0 when the roll reconciles against its page-N summary, 1 otherwise.

## Layout
- `rasterize.py` — PDF → cover / voter half-page crops / summary PNGs
- `extractors/` — `base.py` (interface + registry), `ollama_qwen.py`, `mock.py`
- `schemas.py` — per-page JSON schemas + instructions (the domain knowledge)
- `assemble.py` — normalize, dedupe by serial, infer status, structural QA/confidence
- `reconcile.py` — counts/gender vs the summary oracle (mirrors `db/schema.sql`'s `v_roll_reconciliation`)
- `pipeline.py` — orchestrator (`run_roll`)
- `cli.py` — entry point

## Not yet wired (next)
- DB load into `db/schema.sql` (we proved this separately; add a loader step).
- Field-fidelity gate beyond structural checks (cross-read / gold-set CER) for native-language rolls.
- Indic specialist routing (`language_code` already drives extractor choice).
