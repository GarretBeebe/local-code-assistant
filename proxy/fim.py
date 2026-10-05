import settings
from proxy.schemas import CompletionRequest


def to_ollama_generate(req: CompletionRequest) -> dict:
    options: dict = {
        "num_ctx": settings.FIM_NUM_CTX,
        # A ceiling, not a default: Continue's generic max_tokens default is 4096, and at
        # single-digit tok/s an uncapped run-on would hold Ollama's only slot for minutes.
        "num_predict": min(req.max_tokens or settings.FIM_MAX_TOKENS, settings.FIM_MAX_TOKENS),
        "temperature": req.temperature if req.temperature is not None else settings.FIM_DEFAULT_TEMPERATURE,
    }
    if req.stop:
        options["stop"] = req.stop
    return {
        "model": settings.FIM_MODEL,
        "prompt": req.prompt,
        "raw": True,  # skip Ollama's chat template so FIM tokens reach the model literally
        "keep_alive": settings.FIM_KEEP_ALIVE,  # outlast Ollama's 5 min idle unload (cold load ~26 s)
        "options": options,
    }
