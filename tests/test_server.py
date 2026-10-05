import json

import httpx2
import pytest
from fastapi.testclient import TestClient

import proxy.ollama_client as ollama_client
import settings
from proxy.server import app

client = TestClient(app)


class FakeOllama:
    """Stands in for Ollama: records request bodies and replies with NDJSON lines."""

    def __init__(self) -> None:
        self.lines: list[dict | str] = [{"done": True}]
        self.status = 200
        self.requests: list[dict] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        body = "".join((ln if isinstance(ln, str) else json.dumps(ln)) + "\n" for ln in self.lines)
        return httpx2.Response(self.status, text=body)


def _use_upstream(monkeypatch, handler) -> None:
    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(ollama_client, "client", httpx2.AsyncClient(base_url="http://ollama", transport=transport))


@pytest.fixture(autouse=True)
def ollama(monkeypatch) -> FakeOllama:
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", None)
    fake = FakeOllama()
    _use_upstream(monkeypatch, fake)
    return fake


def _fim(stream: bool = True, **body):
    return client.post("/v1/completions", json={"model": "m", "prompt": "p", "stream": stream, **body})


def _chat(stream: bool = False, **body):
    messages = [{"role": "user", "content": "hi"}]
    return client.post("/v1/chat/completions", json={"model": "m", "messages": messages, "stream": stream, **body})


def _sse_text(resp) -> str:
    """Concatenate the text of every SSE completion event (FIM text or chat delta)."""
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: {"):
            event = json.loads(line[len("data: "):])
            if "choices" in event:
                choice = event["choices"][0]
                out.append(choice.get("text") or choice.get("delta", {}).get("content", ""))
    return "".join(out)


# --- health, models, auth ---

def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_models_returns_configured_models_without_upstream_call(ollama):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert [m["id"] for m in data["data"]] == [settings.CHAT_MODEL, settings.FIM_MODEL]
    assert ollama.requests == []


def test_auth_required(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_auth_valid_token(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    resp = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


def test_auth_invalid_token(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    resp = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


# --- upstream requests ---

def test_upstream_is_always_streamed(ollama):
    _fim(stream=False)
    _chat(stream=False)
    assert [r["stream"] for r in ollama.requests] == [True, True]


def test_chat_pins_model_and_forwards_messages_and_options(ollama):
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    _chat(messages=messages, temperature=0.3, max_tokens=50)
    sent = ollama.requests[0]
    assert sent["model"] == settings.CHAT_MODEL
    assert sent["messages"] == messages
    assert sent["options"] == {"num_ctx": settings.CHAT_NUM_CTX, "temperature": 0.3, "num_predict": 50}


# --- /v1/chat/completions ---

def test_chat_non_streaming_joins_content(ollama):
    ollama.lines = [
        {"message": {"content": "hel"}},
        {"message": {"content": "lo"}},
        {"message": {"content": ""}, "done": True, "done_reason": "stop"},
    ]
    body = _chat().json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert body["choices"][0]["finish_reason"] == "stop"


def test_chat_non_streaming_reports_length_finish(ollama):
    ollama.lines = [{"message": {"content": "cut"}}, {"done": True, "done_reason": "length"}]
    assert _chat().json()["choices"][0]["finish_reason"] == "length"


def test_chat_streaming(ollama):
    ollama.lines = [{"message": {"content": "hel"}}, {"message": {"content": "lo"}}, {"done": True}]
    resp = _chat(stream=True)
    assert resp.status_code == 200
    assert _sse_text(resp) == "hello"
    assert resp.text.endswith("data: [DONE]\n\n")


# --- /v1/completions ---

def test_completions_non_streaming(ollama):
    ollama.lines = [{"response": "result"}, {"response": "", "done": True, "done_reason": "stop"}]
    body = _fim(stream=False).json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "result"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_completions_streaming(ollama):
    ollama.lines = [{"response": "def foo():"}, {"response": "\n    pass"}, {"done": True}]
    resp = _fim(stream=True)
    assert resp.status_code == 200
    assert _sse_text(resp) == "def foo():\n    pass"
    assert resp.text.endswith("data: [DONE]\n\n")


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "non-stream"])
@pytest.mark.parametrize(
    "tokens, expected",
    [
        (["def foo():", "\n    pass", "\n\n", "RUNON"], "def foo():\n    pass"),
        (["foo\n", "\nRUNON"], "foo\n"),
        (["kept\n\ndropped"], "kept"),
        (["\n\ncode"], "\n\ncode"),
        (["\n\n", "code"], "\n\ncode"),
        (["foo", " \n", "\n", "RUNON"], "foo \n"),
        (["foo\r\n", "\r\n", "RUNON"], "foo\r\n"),
        (["foo\n", "    \n", "RUNON"], "foo\n"),
    ],
    ids=[
        "blank-line-chunk", "split-across-chunks", "within-one-chunk", "leading-blanks-kept",
        "blank-only-first-chunk", "trailing-space-split", "crlf", "indented-blank-line",
    ],
)
def test_fim_stops_at_first_blank_line_after_content(ollama, tokens, expected, stream):
    ollama.lines = [{"response": t} for t in tokens]
    resp = _fim(stream=stream)
    text = _sse_text(resp) if stream else resp.json()["choices"][0]["text"]
    assert text == expected


# --- upstream errors ---

def test_streaming_error_line_becomes_sse_error(ollama):
    ollama.lines = [{"response": "partial"}, {"error": "model runner crashed"}]
    resp = _fim(stream=True)
    assert resp.status_code == 200
    assert "Ollama error: model runner crashed" in resp.text
    assert "[DONE]" not in resp.text


def test_non_streaming_error_line_returns_502(ollama):
    ollama.lines = [{"error": "model runner crashed"}]
    resp = _chat()
    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == "Ollama error: model runner crashed"


def test_malformed_line_returns_502(ollama):
    ollama.lines = ["not json"]
    resp = _fim(stream=False)
    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == "malformed response from Ollama"


def test_upstream_http_error_carries_ollama_message(ollama):
    ollama.status = 404
    ollama.lines = [{"error": 'model "x" not found, try pulling it first'}]
    resp = _chat()
    assert resp.status_code == 502
    assert 'model \\"x\\" not found' in resp.json()["error"]["message"]


@pytest.mark.parametrize(
    "exc, status",
    [(httpx2.ConnectError("refused"), 502), (httpx2.ReadTimeout("slow"), 504)],
    ids=["connect-error", "timeout"],
)
def test_transport_failures_map_to_gateway_errors(monkeypatch, exc, status):
    def fail(request):
        raise exc

    _use_upstream(monkeypatch, fail)
    assert _fim(stream=False).status_code == status


# --- request log ---

def _log_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "proxy"]


def test_log_line_for_stopped_fim_has_request_shape_but_not_prompt(ollama, caplog):
    ollama.lines = [{"response": "code\n\nrunon"}]
    with caplog.at_level("INFO", logger="proxy"):
        _fim(prompt="SECRET", max_tokens=4096, stop=["<|endoftext|>"])
    [line] = _log_lines(caplog)
    assert line.startswith("fim outcome=stopped ttft_ms=")
    assert "chunks=1 prompt_chars=6 max_tokens=4096 stops=1" in line
    assert "SECRET" not in line


def test_log_line_includes_ollama_timings_when_generation_finishes(ollama, caplog):
    ollama.lines = [
        {"message": {"content": "hi"}},
        {
            "done": True, "done_reason": "stop", "load_duration": 0,
            "prompt_eval_count": 12, "prompt_eval_duration": 80_000_000,
            "eval_count": 3, "eval_duration": 30_000_000,
        },
    ]
    with caplog.at_level("INFO", logger="proxy"):
        _chat()
    [line] = _log_lines(caplog)
    assert line.startswith("chat outcome=done ")
    assert "done_reason=stop load_ms=0 prompt_tokens=12 prefill_ms=80 eval_tokens=3 eval_ms=30" in line


def test_log_line_records_upstream_errors(ollama, caplog):
    ollama.lines = [{"error": "model runner crashed"}]
    with caplog.at_level("INFO", logger="proxy"):
        _chat()
    [line] = _log_lines(caplog)
    assert line.startswith("chat outcome=error ")
