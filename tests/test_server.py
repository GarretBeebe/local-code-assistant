import json
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

import proxy.ollama_client as ollama_client
from proxy.server import app

client = TestClient(app)


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
