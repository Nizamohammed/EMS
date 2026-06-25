"""Remote vision-LLM extractor via a vLLM OpenAI-compatible server.

For the GPU path: serve the model with
    vllm serve Qwen/Qwen3-VL-8B-Instruct --port 8000
then point this extractor at it. Structured output is constrained with
`response_format` (json_schema). stdlib-only HTTP, same Extractor contract as
the Ollama extractor, so swapping engines never touches the pipeline.
"""
from __future__ import annotations
import base64
import json
import urllib.request
import urllib.error

from .base import Extractor, register


@register("vllm")
class VllmExtractor(Extractor):
    def __init__(self, model: str = "Qwen/Qwen3-VL-8B-Instruct",
                 host: str = "http://localhost:8000", api_key: str = "EMPTY",
                 temperature: float = 0.0, timeout: int = 240, retries: int = 1):
        self.model = model
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.retries = retries

    def extract(self, image_path: str, json_schema: dict, instruction: str):
        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "extraction", "schema": json_schema}},
        }
        data = json.dumps(payload).encode("utf-8")
        last = None
        for _ in range(self.retries + 1):
            req = urllib.request.Request(
                self.host + "/v1/chat/completions", data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read())
                content = body["choices"][0]["message"]["content"]
                return json.loads(content)
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
                last = e
                continue
        raise RuntimeError(
            f"vLLM call failed after {self.retries + 1} attempt(s) at {self.host} "
            f"(model {self.model}): {last}"
        ) from last
