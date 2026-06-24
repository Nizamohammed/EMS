"""Extractor interface + registry.

The whole point of the pipeline being model-agnostic lives here: every engine
(local VLM via Ollama, a hosted API, a templated OCR stack, ...) implements the
same one-method contract, so swapping engines never touches the pipeline.
"""
from __future__ import annotations
from abc import ABC, abstractmethod

_REGISTRY: dict[str, type] = {}


def register(name: str):
    def deco(cls):
        _REGISTRY[name] = cls
        cls.name = name
        return cls
    return deco


def get_extractor(name: str, **kwargs) -> "Extractor":
    if name not in _REGISTRY:
        raise ValueError(f"unknown extractor {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def available() -> list[str]:
    return sorted(_REGISTRY)


class Extractor(ABC):
    """Vision model that returns structured JSON for one image.

    Contract: given an image path, a JSON schema describing the desired output,
    and a natural-language instruction, return parsed JSON (dict or list) that
    conforms to the schema. Determinism (temperature 0) is expected.
    """
    name = "base"

    @abstractmethod
    def extract(self, image_path: str, json_schema: dict, instruction: str):
        ...
