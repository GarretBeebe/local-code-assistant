import os


def _int(key: str, default: str) -> int:
    val = os.environ.get(key, default)
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"Invalid value for {key}: {val!r} (expected integer)")


def _float(key: str, default: str) -> float:
    val = os.environ.get(key, default)
    try:
        return float(val)
    except ValueError:
        raise ValueError(f"Invalid value for {key}: {val!r} (expected float)")


OLLAMA_BASE_URL         = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_NUM_CTX            = _int("CHAT_NUM_CTX", "8192")
FIM_NUM_CTX             = _int("FIM_NUM_CTX", "4096")
FIM_MAX_TOKENS          = _int("FIM_MAX_TOKENS", "128")
FIM_DEFAULT_TEMPERATURE = _float("FIM_DEFAULT_TEMPERATURE", "0.1")
PROXY_PORT              = _int("PROXY_PORT", "8080")
OLLAMA_TIMEOUT_SECONDS  = _float("OLLAMA_TIMEOUT_SECONDS", "120.0")
PROXY_AUTH_TOKEN: str | None = os.environ.get("PROXY_AUTH_TOKEN") or None

_raw_models = os.environ.get("ALLOWED_MODELS", "")
_parsed_models = frozenset(m.strip() for m in _raw_models.split(",") if m.strip()) if _raw_models else None
if _parsed_models is not None and not _parsed_models:
    raise ValueError("ALLOWED_MODELS is set but contains no valid model names")
ALLOWED_MODELS: frozenset[str] | None = _parsed_models
