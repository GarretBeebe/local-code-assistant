import time
import uuid

import settings
from proxy.schemas import CompletionRequest


def to_ollama_generate(req: CompletionRequest) -> dict:
    return {
        "model": req.model,
        "prompt": req.prompt,
        "stream": req.stream,
        "options": {
            "num_ctx": settings.FIM_NUM_CTX,
            "num_predict": req.max_tokens if req.max_tokens is not None else settings.FIM_MAX_TOKENS,
            "temperature": req.temperature if req.temperature is not None else 0.1,
        },
    }


def format_completion_chunk(line: dict, model: str, completion_id: str) -> dict | None:
    if line.get("done"):
        return None
    return {
        "id": completion_id,
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"text": line.get("response", ""), "index": 0, "finish_reason": None}],
    }


def format_completion_response(data: dict, model: str) -> dict:
    return {
        "id": f"cmpl-{uuid.uuid4().hex}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"text": data.get("response", ""), "index": 0, "finish_reason": "stop"}],
    }
