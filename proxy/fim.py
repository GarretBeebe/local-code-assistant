import settings
from proxy.schemas import CompletionRequest


def to_ollama_generate(req: CompletionRequest) -> dict:
    options: dict = {
        "num_ctx": settings.FIM_NUM_CTX,
        "num_predict": req.max_tokens if req.max_tokens is not None else settings.FIM_MAX_TOKENS,
        "temperature": req.temperature if req.temperature is not None else settings.FIM_DEFAULT_TEMPERATURE,
    }
    if req.stop:
        options["stop"] = req.stop
    return {
        "model": req.model,
        "prompt": req.prompt,
        "stream": req.stream,
        "raw": True,  # skip Ollama's chat template so FIM tokens reach the model literally
        "options": options,
    }
