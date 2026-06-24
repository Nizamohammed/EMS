"""Local vision-LLM extractor via Ollama (default: qwen2.5vl:3b).

Talks to the Ollama HTTP API with stdlib only (no pip deps), so it runs on a
Mac with `ollama pull qwen2.5vl:3b` and nothing else. Ollama's structured-output
`format` field is given the JSON schema to constrain the response.
"""
from __future__ import annotations
import base64
import json
import urllib.request
import urllib.error

from .base import Extractor, register


@register("qwen2.5vl")
class OllamaQwenExtractor(Extractor):
    def __init__(self, model: str = "qwen2.5vl:3b",
                 host: str = "http://localhost:11434",
                 temperature: float = 0.0, timeout: int = 240, retries: int = 1):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.retries = retries

    def extract(self, image_path: str, json_schema: dict, instruction: str):
        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "user", "content": instruction, "images": [b64]}],
            "format": json_schema,
            "options": {"temperature": self.temperature},
        }
        data = json.dumps(payload).encode("utf-8")
        last = None
        for _ in range(self.retries + 1):
            req = urllib.request.Request(
                self.host + "/api/chat", data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read())
                content = body.get("message", {}).get("content", "")
                return json.loads(content)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                last = e
                continue
        raise RuntimeError(
            f"Ollama call failed after {self.retries + 1} attempt(s) at {self.host} "
            f"(model {self.model}): {last}"
        ) from last
