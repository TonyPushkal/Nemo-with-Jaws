import io
import json
import threading
import time
import urllib.error

import pytest

from nemo.providers.base import LLMProvider, ProviderError
from nemo.providers.ollama import OllamaLLM, estimate_tokens

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def reply(content='{"a": "x"}', **over):
    base = {"message": {"role": "assistant", "content": content}, "done_reason": "stop",
            "total_duration": 2_000_000_000, "load_duration": 100_000_000, "prompt_eval_count": 120,
            "prompt_eval_duration": 500_000_000, "eval_count": 30, "eval_duration": 1_500_000_000}
    base.update(over)
    return base


class Opener:
    def __init__(self, payload=None, exc=None, delay=0.0):
        self.payload, self.exc, self.delay, self.requests = payload, exc, delay, []
        self.active = self.max_active = 0
        self._l = threading.Lock()

    def __call__(self, req, timeout=None):
        with self._l:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.requests.append((req, timeout))
            time.sleep(self.delay)
            if self.exc:
                raise self.exc
            return io.BytesIO(json.dumps(self.payload).encode())
        finally:
            with self._l:
                self.active -= 1


def test_request_shape_local_only_and_defaults():
    op = Opener(reply())
    llm = OllamaLLM(opener=op)
    llm.complete_json("sys", "user text", SCHEMA)
    req, _ = op.requests[0]
    body = json.loads(req.data)
    assert req.full_url == "http://127.0.0.1:11434/api/chat"
    assert body["stream"] is False and body["format"] == SCHEMA and body["think"] is False
    assert body["options"] == {"num_ctx": 4096, "num_predict": 600, "temperature": 0.0, "seed": 0}
    assert body["messages"][0]["role"] == "system" and json.dumps(SCHEMA) in body["messages"][0]["content"]
    assert body["model"] == "qwen3.5:4b"


def test_result_and_metrics_mapping():
    res = OllamaLLM(opener=Opener(reply())).complete_json("s", "p", SCHEMA)
    assert res.data == {"a": "x"} and res.cost_usd == 0.0
    assert res.meta["prompt_tokens"] == 120 and res.meta["completion_tokens"] == 30
    assert res.meta["total_ms"] == 2000.0 and res.meta["load_ms"] == 100.0 and res.meta["gen_tokens_per_s"] == 20.0


def test_is_free_and_satisfies_llm_interface():
    llm = OllamaLLM(opener=Opener(reply()))
    assert isinstance(llm, LLMProvider) and llm.is_paid is False and llm.max_cost_usd("s", "p") == 0.0


def test_oversized_prompt_refused_before_any_request():
    op = Opener(reply())
    with pytest.raises(ProviderError) as e:
        OllamaLLM(opener=op).complete_json("s", "x" * 12_000, SCHEMA)
    assert e.value.kind == "context_overflow" and op.requests == []


def test_available_prompt_chars_fits_the_context():
    llm = OllamaLLM(opener=Opener(reply()))
    room = llm.available_prompt_chars("sys", SCHEMA)
    op = Opener(reply())
    llm._open = op
    llm.complete_json("sys", "y" * room, SCHEMA)          # exactly the advertised room must be accepted
    assert len(op.requests) == 1
    with pytest.raises(ProviderError):
        llm.complete_json("sys", "y" * (room + 400), SCHEMA)
    assert estimate_tokens("abc") == 1


def test_truncated_and_full_context_and_bad_output_are_errors():
    with pytest.raises(ProviderError) as e:
        OllamaLLM(opener=Opener(reply(done_reason="length"))).complete_json("s", "p", SCHEMA)
    assert e.value.kind == "truncated_output"
    with pytest.raises(ProviderError) as e:
        OllamaLLM(opener=Opener(reply(prompt_eval_count=4090, eval_count=6))).complete_json("s", "p", SCHEMA)
    assert e.value.kind == "context_full"
    for bad in ("not json", "[1, 2]", ""):
        with pytest.raises(ProviderError) as e:
            OllamaLLM(opener=Opener(reply(content=bad))).complete_json("s", "p", SCHEMA)
        assert e.value.kind == "bad_output"


def test_server_and_connection_errors_are_typed():
    err404 = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error": "model \\"x\\" not found"}'))
    with pytest.raises(ProviderError) as e:
        OllamaLLM(opener=Opener(exc=err404)).complete_json("s", "p", SCHEMA)
    assert e.value.kind == "model_not_found" and "not found" in str(e.value)
    refused = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
    with pytest.raises(ProviderError) as e:
        OllamaLLM(opener=Opener(exc=refused)).complete_json("s", "p", SCHEMA)
    assert e.value.kind == "connection_error"
    with pytest.raises(ProviderError) as e:
        OllamaLLM(opener=Opener(exc=TimeoutError())).complete_json("s", "p", SCHEMA)
    assert e.value.kind == "timeout"
    with pytest.raises(ProviderError) as e:
        OllamaLLM(opener=Opener(reply(error="boom"))).complete_json("s", "p", SCHEMA)
    assert e.value.kind == "server_error"


def test_only_one_request_at_a_time_across_threads():
    op = Opener(reply(), delay=0.05)
    llm = OllamaLLM(opener=op)
    ts = [threading.Thread(target=llm.complete_json, args=("s", "p", SCHEMA)) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(op.requests) == 4 and op.max_active == 1
