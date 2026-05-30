import os

OLLAMA_BASE_URL        = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL             = os.environ.get("CHAT_MODEL", "qwen2.5-coder:14b")
FIM_MODEL              = os.environ.get("FIM_MODEL", "qwen2.5-coder:7b")
CHAT_NUM_CTX           = int(os.environ.get("CHAT_NUM_CTX", "8192"))
FIM_NUM_CTX            = int(os.environ.get("FIM_NUM_CTX", "4096"))
FIM_MAX_TOKENS         = int(os.environ.get("FIM_MAX_TOKENS", "128"))
PROXY_PORT             = int(os.environ.get("PROXY_PORT", "8080"))
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120.0"))
