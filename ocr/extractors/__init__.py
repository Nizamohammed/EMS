"""Pluggable extractor registry.

An Extractor turns (image, json_schema, instruction) into structured JSON.
Concrete implementations register themselves; importing this package imports them.
"""
from .base import Extractor, register, get_extractor, available

# import implementations so they self-register
from . import ollama_qwen  # noqa: E402,F401
from . import vllm_qwen    # noqa: E402,F401
from . import mock         # noqa: E402,F401

__all__ = ["Extractor", "register", "get_extractor", "available"]
