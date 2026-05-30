import settings
from proxy.fim import format_completion_chunk, format_completion_response, to_ollama_generate
from proxy.schemas import CompletionRequest


def test_to_ollama_generate_defaults():
    req = CompletionRequest(model="m", prompt="p")
    payload = to_ollama_generate(req)
    assert payload["model"] == "m"
    assert payload["prompt"] == "p"
    assert payload["raw"] is True
    assert payload["options"]["temperature"] == 0.1
    assert payload["options"]["num_predict"] == settings.FIM_MAX_TOKENS
    assert "stop" not in payload["options"]


def test_to_ollama_generate_with_temperature():
    req = CompletionRequest(model="m", prompt="p", temperature=0.5)
    payload = to_ollama_generate(req)
    assert payload["options"]["temperature"] == 0.5


def test_to_ollama_generate_with_stop():
    req = CompletionRequest(model="m", prompt="p", stop=["<|end|>", "\n"])
    payload = to_ollama_generate(req)
    assert payload["options"]["stop"] == ["<|end|>", "\n"]


def test_to_ollama_generate_stream_flag():
    req = CompletionRequest(model="m", prompt="p", stream=True)
    payload = to_ollama_generate(req)
    assert payload["stream"] is True


def test_format_completion_chunk_with_content():
    chunk = format_completion_chunk({"response": "hello", "done": False}, "m", "id1")
    assert chunk is not None
    assert chunk["choices"][0]["text"] == "hello"
    assert chunk["object"] == "text_completion"
    assert chunk["id"] == "id1"


def test_format_completion_chunk_done_returns_none():
    chunk = format_completion_chunk({"done": True}, "m", "id1")
    assert chunk is None


def test_format_completion_chunk_missing_response():
    chunk = format_completion_chunk({"done": False}, "m", "id1")
    assert chunk is not None
    assert chunk["choices"][0]["text"] == ""


def test_format_completion_response():
    resp = format_completion_response({"response": "world"}, "m")
    assert resp["choices"][0]["text"] == "world"
    assert resp["object"] == "text_completion"
    assert resp["choices"][0]["finish_reason"] == "stop"


def test_format_completion_response_model_echo():
    resp = format_completion_response({"response": "x"}, "qwen2.5-coder:7b")
    assert resp["model"] == "qwen2.5-coder:7b"
