"""Local model adapter for Ollama (self-hosted, free, one request at a time).

API fields (`/api/chat`: `format` JSON schema, `stream`, `think`, `options.num_ctx/num_predict/
temperature/seed`, `keep_alive`, and the nanosecond timing/token counters in the response) were
checked against docs.ollama.com on 2026-09-30. Only the local server is contacted.

`think` defaults to False: measured on 2026-09-30, qwen3.5:4b with `think` unset spent the whole
600-token budget on hidden reasoning (no JSON, 54 s), while `think=False` answered in 3 s.

Guards against the failure that matters at a 4,096-token context: Ollama would silently drop
prompt text that does not fit, so the adapter refuses to send a prompt whose estimated size
plus `num_predict` exceeds `num_ctx`, and reports a full or truncated response as an error.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from nemo.providers.base import LLMResult, ProviderError

CHARS_PER_TOKEN = 3.0  # conservative ASSUMPTION for preflight; measured ratios are reported by the check script
_MARGIN_TOKENS = 64


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class OllamaLLM:
    name = "ollama"
    is_paid = False

    def __init__(self, model: str = "qwen3.5:4b", *, base_url: str = "http://127.0.0.1:11434",
                 num_ctx: int = 4096, num_predict: int = 600, temperature: float = 0.0, seed: int = 0,
                 think: bool | None = False, keep_alive: str = "10m", timeout: float = 300.0,
                 opener: Callable[..., Any] | None = None):
        self.model, self.base_url = model, base_url.rstrip("/")
        self.num_ctx, self.num_predict = num_ctx, num_predict
        self.temperature, self.seed, self.think = temperature, seed, think
        self.keep_alive, self.timeout = keep_alive, timeout
        self._open = opener or urllib.request.urlopen
        self._lock = threading.Lock()  # one request at a time, even across threads

    def max_cost_usd(self, system: str, prompt: str) -> float:
        return 0.0

    @staticmethod
    def _system_with_schema(system: str, schema: dict[str, Any]) -> str:
        # Ollama's docs advise also giving the schema in the prompt to ground the output.
        return f"{system}\n\nRespond with one JSON object matching this JSON schema:\n{json.dumps(schema)}"

    def available_prompt_chars(self, system: str, schema: dict[str, Any]) -> int:
        """Characters left for the user prompt after the system text, schema, output and margin."""
        used = estimate_tokens(self._system_with_schema(system, schema))
        return max(0, int((self.num_ctx - self.num_predict - _MARGIN_TOKENS - used) * CHARS_PER_TOKEN))

    def request_body(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "format": schema, "keep_alive": self.keep_alive,
            "messages": [{"role": "system", "content": self._system_with_schema(system, schema)},
                         {"role": "user", "content": prompt}],
            "options": {"num_ctx": self.num_ctx, "num_predict": self.num_predict,
                        "temperature": self.temperature, "seed": self.seed}}
        if self.think is not None:
            body["think"] = self.think
        return body

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str(json.loads(exc.read().decode() or "{}").get("error", ""))[:200]
            except Exception:  # noqa: BLE001 - detail is best effort
                pass
            kind = "model_not_found" if exc.code == 404 else f"http_{exc.code}"
            raise ProviderError(kind, detail or f"HTTP {exc.code}") from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("timeout", f"no response within {self.timeout:.0f}s") from None
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "connection_error"
            raise ProviderError(kind, f"cannot reach Ollama at {self.base_url}: {exc.reason}") from None
        except json.JSONDecodeError:
            raise ProviderError("bad_response", "Ollama response was not JSON") from None

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
        est = estimate_tokens(self._system_with_schema(system, schema)) + estimate_tokens(prompt)
        if est + self.num_predict + _MARGIN_TOKENS > self.num_ctx:
            raise ProviderError("context_overflow", f"~{est} prompt tokens + {self.num_predict} output "
                                f"exceeds num_ctx {self.num_ctx}; shorten the input", charged=False)
        with self._lock:
            raw = self._post("/api/chat", self.request_body(system, prompt, schema))
        if raw.get("error"):
            raise ProviderError("server_error", str(raw["error"])[:200])
        msg = raw.get("message") or {}
        pt, ct = int(raw.get("prompt_eval_count") or 0), int(raw.get("eval_count") or 0)
        ms = lambda k: round((raw.get(k) or 0) / 1e6, 1)  # noqa: E731 - ns -> ms
        eval_ms = ms("eval_duration")
        meta = {"total_ms": ms("total_duration"), "load_ms": ms("load_duration"),
                "prompt_eval_ms": ms("prompt_eval_duration"), "eval_ms": eval_ms,
                "prompt_tokens": pt, "completion_tokens": ct,
                "gen_tokens_per_s": round(ct / (eval_ms / 1000), 1) if eval_ms else None,
                "done_reason": raw.get("done_reason"), "est_prompt_tokens": est,
                "thinking_chars": len(msg.get("thinking") or "")}
        if raw.get("done_reason") == "length":
            raise ProviderError("truncated_output", f"hit num_predict={self.num_predict} before finishing JSON")
        if pt + ct >= self.num_ctx - 1:
            raise ProviderError("context_full", f"{pt}+{ct} tokens reached num_ctx {self.num_ctx}; input may have "
                                "been truncated by the server")
        content = msg.get("content") or ""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            raise ProviderError("bad_output", f"not JSON: {content[:200]!r}") from None
        if not isinstance(data, dict):
            raise ProviderError("bad_output", "JSON output is not an object")
        return LLMResult(data=data, prompt_tokens=pt, completion_tokens=ct, cost_usd=0.0, meta=meta)

    # --- diagnostics used by the check script (not by the pipeline) ---
    def loaded_models(self) -> list[dict[str, Any]]:
        req = urllib.request.Request(self.base_url + "/api/ps")
        with self._open(req, timeout=10) as resp:
            return json.load(resp).get("models", [])

    def installed_models(self) -> list[str]:
        req = urllib.request.Request(self.base_url + "/api/tags")
        with self._open(req, timeout=10) as resp:
            return [m.get("name", "") for m in json.load(resp).get("models", [])]
