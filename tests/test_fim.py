import settings
from proxy.fim import to_ollama_generate
from proxy.schemas import CompletionRequest


def test_to_ollama_generate_defaults():
    req = CompletionRequest(model="m", prompt="p")
    payload = to_ollama_generate(req)
    assert payload["model"] == "m"
    assert payload["prompt"] == "p"
    assert payload["raw"] is True
    assert payload["options"]["temperature"] == settings.FIM_DEFAULT_TEMPERATURE
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


def test_to_ollama_generate_max_tokens_override():
    req = CompletionRequest(model="m", prompt="p", max_tokens=64)
    payload = to_ollama_generate(req)
    assert payload["options"]["num_predict"] == 64
