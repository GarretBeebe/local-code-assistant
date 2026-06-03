from unittest.mock import patch

import pytest

import settings
from context import manager as ctx_manager
from context import rag_client
from proxy.schemas import ChatMessage, ChatRequest
from proxy.server import _to_ollama_chat


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


def test_retrieve_chunks_returns_data_on_success(rag_configured):
    fake_chunks = [{"text": "def foo(): pass", "filepath": "/f.py", "score": 0.9}]

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"chunks": fake_chunks}

    with patch.object(rag_client._session, "post", return_value=FakeResp()):
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


def test_build_query_truncates_long_messages():
    long = "x" * 400
    messages = [ChatMessage(role="user", content=long)]
    query = ctx_manager._build_query(messages)
    assert len(query) <= 300


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
