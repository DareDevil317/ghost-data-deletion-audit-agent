"""Minimal Gemini REST client (standard library only) with token accounting.

The key is read from the GEMINI_API_KEY environment variable (or a local, uncommitted
.env file).  Every call's token usage is appended to usage_log so the usage report can
be generated from the real run.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.5-flash-lite"

# Paid-tier list prices (USD per 1M tokens) used only to *estimate* cost in the report.
# The free tier itself costs $0.  Update if Google changes pricing.
PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
}


def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class LLMUnavailable(Exception):
    pass


class GeminiClient:
    def __init__(self, model: str | None = None, usage_log: list | None = None,
                 min_interval_s: float = 4.5):
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.usage_log = usage_log if usage_log is not None else []
        self.min_interval_s = min_interval_s   # stay under free-tier requests-per-minute
        self._last_call = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def generate_json(self, prompt: str, purpose: str, image_bytes: bytes | None = None,
                      image_mime: str = "image/png", max_retries: int = 4) -> dict | list:
        """Send one prompt (optionally with an image) and parse a JSON reply."""
        if not self.available:
            raise LLMUnavailable("GEMINI_API_KEY not set")
        parts = [{"text": prompt}]
        if image_bytes is not None:
            parts.append({"inline_data": {"mime_type": image_mime,
                                          "data": base64.b64encode(image_bytes).decode("ascii")}})
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        data = json.dumps(body).encode("utf-8")
        url = API_URL.format(model=self.model)
        delay = 5.0
        for attempt in range(max_retries + 1):
            wait = self.min_interval_s - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": self.api_key})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMUnavailable(f"HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMUnavailable(str(e)) from None
        usage = payload.get("usageMetadata", {}) or {}
        self.usage_log.append({
            "provider": "Google Gemini API", "model": self.model, "purpose": purpose,
            "input_tokens": int(usage.get("promptTokenCount", 0) or 0),
            "output_tokens": int(usage.get("candidatesTokenCount", 0) or 0)
                             + int(usage.get("thoughtsTokenCount", 0) or 0),
        })
        try:
            text = "".join(p.get("text", "") for p in payload["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError):
            raise LLMUnavailable(f"unexpected response: {str(payload)[:300]}") from None
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("\n") + 1:] if "\n" in text else text
        return json.loads(text)
