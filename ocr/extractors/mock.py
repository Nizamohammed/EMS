"""Deterministic no-model extractor for wiring tests / CI.

Returns minimal schema-shaped data so the pipeline runs end-to-end without a GPU
or Ollama. It does NOT produce real extractions — use it to verify plumbing only.
"""
from __future__ import annotations
from .base import Extractor, register


def _empty_for(schema: dict):
    t = schema.get("type")
    if t == "array":
        return []
    if t == "object":
        out = {}
        for key, sub in schema.get("properties", {}).items():
            st = sub.get("type")
            out[key] = ([] if st == "array" else {} if st == "object"
                        else 0 if st == "integer" else False if st == "boolean" else "")
        return out
    return {}


@register("mock")
class MockExtractor(Extractor):
    def __init__(self, **_):
        pass

    def extract(self, image_path: str, json_schema: dict, instruction: str):
        return _empty_for(json_schema)
