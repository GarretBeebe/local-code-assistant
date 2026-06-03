from unittest.mock import patch

import pytest

import settings
from context import manager as ctx_manager
from context import rag_client
from proxy.schemas import ChatMessage, ChatRequest
from proxy.server import _to_ollama_chat


def _fake_resp(chunks) -> object:
    class _Resp:
        def raise_for_status(self) -> None: pass
        def json(self) -> dict: return {"chunks": chunks}
    return _Resp()


# --- rag_client ---

def test_retrieve_chunks_disabled_when_no_base_url(monkeypatch):
    monkeypatch.setattr(settings, "RAG_BASE_URL", "")
    monkeypatch.setattr(settings, "RAG_INTERNAL_TOKEN", "tok")
    assert rag_client.retrieve_chunks("hello") == []


def test_retrieve_chunks_disabled_when_no_token(monkeypatch):
    monkeypatch.setattr(settings, "RAG_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(settings, "RAG_INTERNAL_TOKEN", None)
    assert rag_client.retrieve_chunks("hello") == []


@pytest.fixture
def rag_configured(monkeypatch):
    monkeypatch.setattr(settings, "RAG_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(settings, "RAG_INTERNAL_TOKEN", "tok")
    monkeypatch.setattr(settings, "RAG_CONTEXT_CHUNKS", 3)


def test_retrieve_chunks_returns_empty_on_exception(rag_configured):
    with patch.object(rag_client._session, "post", side_effect=ConnectionError("down")):
        assert rag_client.retrieve_chunks("hello") == []


def test_retrieve_chunks_returns_empty_when_chunks_not_a_list(rag_configured):
    with patch.object(rag_client._session, "post", return_value=_fake_resp("bad response")):
        assert rag_client.retrieve_chunks("foo") == []


def test_retrieve_chunks_returns_data_on_success(rag_configured):
    fake_chunks = [{"text": "def foo(): pass", "filepath": "/f.py", "score": 0.9}]

    with patch.object(rag_client._session, "post", return_value=_fake_resp(fake_chunks)):
        result = rag_client.retrieve_chunks("foo function")

    assert result == fake_chunks


# --- manager ---

def test_build_context_prefix_empty_when_no_chunks():
    with patch.object(rag_client, "retrieve_chunks", return_value=[]):
        result = ctx_manager.build_context_prefix([ChatMessage(role="user", content="hi")])
    assert result == ""


def test_build_context_prefix_formats_chunks():
    chunks = [{"text": "def foo(): pass", "filepath": "/watch/Code/foo.py", "score": 0.9}]
    with patch.object(rag_client, "retrieve_chunks", return_value=chunks):
        result = ctx_manager.build_context_prefix([ChatMessage(role="user", content="foo")])
    assert "Relevant code from your project:" in result
    assert "# /watch/Code/foo.py" in result
    assert "def foo(): pass" in result


def test_build_query_uses_last_user_and_assistant():
    messages = [
        ChatMessage(role="user", content="first question"),
        ChatMessage(role="assistant", content="first answer"),
        ChatMessage(role="user", content="follow up"),
    ]
    query = ctx_manager._build_query(messages)
    assert "follow up" in query
    assert "first answer" in query
    assert "first question" not in query


def test_build_query_truncates_long_user_message():
    messages = [ChatMessage(role="user", content="x" * 400)]
    query = ctx_manager._build_query(messages)
    assert len(query) == 300


def test_build_query_truncates_each_field_independently():
    # Each field is capped at 300 chars; combined max is 601 (300 + space + 300).
    messages = [
        ChatMessage(role="user", content="x" * 400),
        ChatMessage(role="assistant", content="y" * 400),
    ]
    query = ctx_manager._build_query(messages)
    assert query == "x" * 300 + " " + "y" * 300
    assert len(query) == 601


def test_build_context_prefix_skips_chunk_with_missing_text():
    chunks = [{"filepath": "/foo.py"}]  # no "text" key
    with patch.object(rag_client, "retrieve_chunks", return_value=chunks):
        result = ctx_manager.build_context_prefix([ChatMessage(role="user", content="hi")])
    assert result == ""


def test_build_context_prefix_skips_whitespace_only_text():
    chunks = [{"text": "   ", "filepath": "/foo.py"}]
    with patch.object(rag_client, "retrieve_chunks", return_value=chunks):
        result = ctx_manager.build_context_prefix([ChatMessage(role="user", content="hi")])
    assert result == ""


# --- server integration ---

def test_to_ollama_chat_prepends_system_message_when_prefix_nonempty():
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    payload = _to_ollama_chat(req, "some context")
    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "some context"
    assert msgs[1]["role"] == "user"


def test_to_ollama_chat_unchanged_when_prefix_empty():
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    payload = _to_ollama_chat(req, "")
    msgs = payload["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_to_ollama_chat_merges_prefix_into_existing_system_message():
    req = ChatRequest(model="m", messages=[
        ChatMessage(role="system", content="you are helpful"),
        ChatMessage(role="user", content="hi"),
    ])
    payload = _to_ollama_chat(req, "rag context")
    msgs = payload["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith("rag context")
    assert "you are helpful" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
