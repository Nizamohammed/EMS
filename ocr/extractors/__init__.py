"""Pluggable extractor registry.

An Extractor turns (image, json_schema, instruction) into structured JSON.
Concrete implementations register themselves; importing this package imports them.

Final model set: `rapidocr` (English workhorse; DELETED cards -> Donut+Pix2Struct
+Qwen2.5-VL-3B combine) and `surya` (Indic).
"""
from .base import Extractor, register, get_extractor, available

# import implementations so they self-register
from . import rapidocr  # noqa: E402,F401
from . import surya     # noqa: E402,F401

__all__ = ["Extractor", "register", "get_extractor", "available"]
