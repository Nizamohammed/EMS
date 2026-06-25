"""EMS OCR/extraction pipeline.

PDF roll -> rasterize/segment -> pluggable VLM extractor -> assemble
(dedupe + status inference) -> reconcile against the page-N summary oracle.

The extractor is swappable (Ollama/Qwen-VL today; any image->JSON model later)
via ocr.extractors. See README.md.
"""
