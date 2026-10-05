import os


def _env_typed(key: str, default: str, cast: type, label: str):
    val = os.environ.get(key, default)
    try:
        return cast(val)
    except ValueError:
        raise ValueError(f"Invalid value for {key}: {val!r} (expected {label})")


def _int(key: str, default: str) -> int:
    return _env_typed(key, default, int, "integer")


def _float(key: str, default: str) -> float:
    return _env_typed(key, default, float, "float")


OLLAMA_BASE_URL         = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL              = os.environ.get("CHAT_MODEL", "qwen2.5-coder:14b")
FIM_MODEL               = os.environ.get("FIM_MODEL",  "qwen2.5-coder:7b")
CHAT_NUM_CTX            = _int("CHAT_NUM_CTX", "8192")
FIM_NUM_CTX             = _int("FIM_NUM_CTX", "4096")
FIM_MAX_TOKENS          = _int("FIM_MAX_TOKENS", "128")
FIM_DEFAULT_TEMPERATURE = _float("FIM_DEFAULT_TEMPERATURE", "0.1")
FIM_KEEP_ALIVE          = os.environ.get("FIM_KEEP_ALIVE", "2h")  # Ollama duration; "-1m" = forever
OLLAMA_TIMEOUT_SECONDS  = _float("OLLAMA_TIMEOUT_SECONDS", "120.0")
PROXY_AUTH_TOKEN: str | None = os.environ.get("PROXY_AUTH_TOKEN") or None
