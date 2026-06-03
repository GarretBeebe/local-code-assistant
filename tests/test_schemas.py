from proxy.schemas import ChatMessage, ChatRequest, CompletionRequest


def test_chat_request_defaults():
    req = ChatRequest(model="m", messages=[])
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
