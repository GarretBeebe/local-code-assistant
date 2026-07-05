import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import proxy.ollama_client as ollama_client
import settings
from context import manager as ctx_manager
from proxy.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", None)
    monkeypatch.setattr(settings, "ALLOWED_MODELS", None)


def _stream(*lines):
    @contextmanager
    def _mock(path, payload):
        yield iter(lines)
    return _mock


# --- health ---

def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- /v1/models ---

def test_list_models():
    mock_tags = {"models": [{"name": "qwen2.5-coder:7b"}, {"name": "qwen2.5-coder:14b"}]}
    with patch.object(ollama_client, "get_json", return_value=mock_tags):
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    ids = [m["id"] for m in data["data"]]
    assert "qwen2.5-coder:7b" in ids
    assert "qwen2.5-coder:14b" in ids


def test_list_models_empty():
    with patch.object(ollama_client, "get_json", return_value={"models": []}):
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


# --- /v1/chat/completions ---

def test_chat_completions_non_streaming():
    mock_response = {"message": {"role": "assistant", "content": "hello"}}
    with patch.object(ctx_manager, "build_context_prefix", return_value=""):
        with patch.object(ollama_client, "post_json", return_value=mock_response):
            resp = client.post("/v1/chat/completions", json={
                "model": "qwen2.5-coder:14b",
                "messages": [{"role": "user", "content": "hi"}],
            })
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["object"] == "chat.completion"


def test_chat_completions_streaming():
    lines = [
        json.dumps({"message": {"content": "hel"}, "done": False}),
        json.dumps({"message": {"content": "lo"}, "done": False}),
        json.dumps({"done": True}),
    ]
    with patch.object(ctx_manager, "build_context_prefix", return_value=""):
        with patch.object(ollama_client, "post_stream", _stream(*lines)):
            resp = client.post("/v1/chat/completions", json={
                "model": "qwen2.5-coder:14b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            })
    assert resp.status_code == 200
    assert "hel" in resp.text
    assert "lo" in resp.text
    assert "[DONE]" in resp.text


def test_chat_completions_injects_rag_prefix():
    mock_response = {"message": {"role": "assistant", "content": "hello"}}
    with patch.object(ctx_manager, "build_context_prefix", return_value="rag context"):
        with patch.object(ollama_client, "post_json", return_value=mock_response) as mock_post:
            resp = client.post("/v1/chat/completions", json={
                "model": "qwen2.5-coder:14b",
                "messages": [{"role": "user", "content": "hi"}],
            })
    assert resp.status_code == 200
    call_payload = mock_post.call_args[0][1]
    assert call_payload["messages"][0] == {"role": "system", "content": "rag context"}
    assert call_payload["messages"][1]["role"] == "user"


def test_chat_completions_no_prefix_when_rag_empty():
    mock_response = {"message": {"role": "assistant", "content": "hello"}}
    with patch.object(ctx_manager, "build_context_prefix", return_value=""):
        with patch.object(ollama_client, "post_json", return_value=mock_response) as mock_post:
            resp = client.post("/v1/chat/completions", json={
                "model": "qwen2.5-coder:14b",
                "messages": [{"role": "user", "content": "hi"}],
            })
    assert resp.status_code == 200
    call_payload = mock_post.call_args[0][1]
    assert call_payload["messages"][0]["role"] == "user"
    assert len(call_payload["messages"]) == 1


def test_chat_completions_merges_rag_prefix_with_existing_system_message():
    mock_response = {"message": {"role": "assistant", "content": "hello"}}
    with patch.object(ctx_manager, "build_context_prefix", return_value="rag context"):
        with patch.object(ollama_client, "post_json", return_value=mock_response) as mock_post:
            resp = client.post("/v1/chat/completions", json={
                "model": "qwen2.5-coder:14b",
                "messages": [
                    {"role": "system", "content": "you are helpful"},
                    {"role": "user", "content": "hi"},
                ],
            })
    assert resp.status_code == 200
    call_payload = mock_post.call_args[0][1]
    assert len(call_payload["messages"]) == 2
    assert call_payload["messages"][0]["role"] == "system"
    assert call_payload["messages"][0]["content"].startswith("rag context")
    assert "you are helpful" in call_payload["messages"][0]["content"]
    assert call_payload["messages"][1]["role"] == "user"


# --- /v1/completions ---

def test_completions_non_streaming():
    with patch.object(ollama_client, "post_json", return_value={"response": "result"}):
        resp = client.post("/v1/completions", json={"model": "qwen2.5-coder:7b", "prompt": "p"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["text"] == "result"
    assert body["object"] == "text_completion"


def test_completions_streaming_basic():
    lines = [
        json.dumps({"response": "def foo():", "done": False}),
        json.dumps({"response": "\n    pass", "done": False}),
        json.dumps({"done": True}),
    ]
    with patch.object(ollama_client, "post_stream", _stream(*lines)):
        resp = client.post("/v1/completions", json={
            "model": "qwen2.5-coder:7b", "prompt": "p", "stream": True,
        })
    assert resp.status_code == 200
    assert "def foo():" in resp.text
    assert "[DONE]" in resp.text


def test_completions_stream_truncates_at_double_newline():
    """Content after \\n\\n must not appear in the response."""
    lines = [
        json.dumps({"response": "def foo():", "done": False}),
        json.dumps({"response": "\n    pass", "done": False}),
        json.dumps({"response": "\n\n", "done": False}),
        json.dumps({"response": "RUNON", "done": False}),
    ]
    with patch.object(ollama_client, "post_stream", _stream(*lines)):
        resp = client.post("/v1/completions", json={
            "model": "qwen2.5-coder:7b", "prompt": "p", "stream": True,
        })
    assert resp.status_code == 200
    assert "RUNON" not in resp.text
    assert "[DONE]" in resp.text


def test_completions_stream_truncates_double_newline_split_across_chunks():
    """\\n\\n spanning two chunks must still stop the stream."""
    lines = [
        json.dumps({"response": "foo\n", "done": False}),
        json.dumps({"response": "\nRUNON", "done": False}),
    ]
    with patch.object(ollama_client, "post_stream", _stream(*lines)):
        resp = client.post("/v1/completions", json={
            "model": "qwen2.5-coder:7b", "prompt": "p", "stream": True,
        })
    assert resp.status_code == 200
    assert "RUNON" not in resp.text
    assert "[DONE]" in resp.text


def test_completions_stream_double_newline_within_single_chunk():
    """\\n\\n within one chunk: text before it is kept, text after is dropped."""
    lines = [
        json.dumps({"response": "kept\n\ndropped", "done": False}),
    ]
    with patch.object(ollama_client, "post_stream", _stream(*lines)):
        resp = client.post("/v1/completions", json={
            "model": "qwen2.5-coder:7b", "prompt": "p", "stream": True,
        })
    assert resp.status_code == 200
    assert "kept" in resp.text
    assert "dropped" not in resp.text


def test_completions_stream_leading_blank_lines_not_truncated():
    """\\n\\n before real content in a single chunk must not truncate the output."""
    lines = [
        json.dumps({"response": "\n\ncode", "done": False}),
        json.dumps({"done": True}),
    ]
    with patch.object(ollama_client, "post_stream", _stream(*lines)):
        resp = client.post("/v1/completions", json={
            "model": "qwen2.5-coder:7b", "prompt": "p", "stream": True,
        })
    assert resp.status_code == 200
    assert "code" in resp.text
    assert "[DONE]" in resp.text


def test_completions_stream_blank_only_chunk_before_content():
    """A blank-only chunk followed by content must not trigger truncation."""
    lines = [
        json.dumps({"response": "\n\n", "done": False}),
        json.dumps({"response": "code", "done": False}),
        json.dumps({"done": True}),
    ]
    with patch.object(ollama_client, "post_stream", _stream(*lines)):
        resp = client.post("/v1/completions", json={
            "model": "qwen2.5-coder:7b", "prompt": "p", "stream": True,
        })
    assert resp.status_code == 200
    assert "code" in resp.text
    assert "[DONE]" in resp.text


def test_completions_non_streaming_leading_blank_lines_not_truncated():
    """Non-streaming: \\n\\n before real content must not truncate the response."""
    with patch.object(ollama_client, "post_json", return_value={"response": "\n\ncode"}):
        resp = client.post("/v1/completions", json={"model": "qwen2.5-coder:7b", "prompt": "p"})
    assert resp.status_code == 200
    assert "code" in resp.json()["choices"][0]["text"]


# --- auth ---

def test_auth_required(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_auth_valid_token(monkeypatch):
    mock_tags = {"models": []}
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    with patch.object(ollama_client, "get_json", return_value=mock_tags):
        resp = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


def test_auth_invalid_token(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    resp = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


# --- model allowlist ---

def test_configured_model_not_in_allowlist_raises():
    from settings import _check_configured_models
    import pytest
    with pytest.raises(ValueError, match="not in ALLOWED_MODELS"):
        _check_configured_models(frozenset({"other-model"}), "qwen2.5-coder:7b")


def test_fim_request_succeeds():
    with patch.object(ollama_client, "post_json", return_value={"response": "ok"}):
        resp = client.post("/v1/completions", json={"model": "qwen2.5-coder:7b", "prompt": "p"})
    assert resp.status_code == 200


def test_list_models_filtered_by_allowlist(monkeypatch):
    mock_tags = {"models": [{"name": "allowed"}, {"name": "blocked"}]}
    monkeypatch.setattr(settings, "ALLOWED_MODELS", frozenset({"allowed"}))
    with patch.object(ollama_client, "get_json", return_value=mock_tags):
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert ids == ["allowed"]
