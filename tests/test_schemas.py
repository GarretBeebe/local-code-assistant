import pytest
from pydantic import ValidationError

from proxy.schemas import ChatMessage, ChatRequest, CompletionRequest


def test_chat_request_defaults():
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    assert req.stream is False
    assert req.temperature is None
    assert req.max_tokens is None


def test_completion_request_defaults():
    req = CompletionRequest(model="m", prompt="p")
    assert req.stream is False
    assert req.stop is None
    assert req.temperature is None


def test_chat_message_dump():
    msg = ChatMessage(role="user", content="hello")
    assert msg.model_dump() == {"role": "user", "content": "hello"}


# --- validation ---

def test_empty_model_rejected():
    with pytest.raises(ValidationError):
        CompletionRequest(model="", prompt="p")


def test_empty_prompt_rejected():
    with pytest.raises(ValidationError):
        CompletionRequest(model="m", prompt="")


def test_negative_max_tokens_rejected():
    with pytest.raises(ValidationError):
        CompletionRequest(model="m", prompt="p", max_tokens=-1)


def test_zero_max_tokens_rejected():
    with pytest.raises(ValidationError):
        CompletionRequest(model="m", prompt="p", max_tokens=0)


def test_temperature_too_high_rejected():
    with pytest.raises(ValidationError):
        CompletionRequest(model="m", prompt="p", temperature=3.0)


def test_temperature_negative_rejected():
    with pytest.raises(ValidationError):
        CompletionRequest(model="m", prompt="p", temperature=-0.1)


def test_invalid_role_rejected():
    with pytest.raises(ValidationError):
        ChatMessage(role="bot", content="hello")


def test_empty_content_rejected():
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content="")


def test_empty_messages_list_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(model="m", messages=[])


def test_stop_twelve_accepted():
    req = CompletionRequest(model="m", prompt="p", stop=[str(i) for i in range(12)])
    assert len(req.stop) == 12


def test_stop_too_many_rejected():
    with pytest.raises(ValidationError):
        CompletionRequest(model="m", prompt="p", stop=[str(i) for i in range(21)])
