import json
import time
import uuid
from typing import Iterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

import settings
from proxy import fim, ollama_client
from proxy.schemas import ChatRequest, CompletionRequest

app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/v1/models")
def list_models():
    data = ollama_client.get_json("/api/tags")
    models = [
        {"id": m["name"], "object": "model", "created": 0, "owned_by": "local"}
        for m in data.get("models", [])
    ]
    return {"object": "list", "data": models}


def _to_ollama_chat(req: ChatRequest) -> dict:
    payload: dict = {
        "model": req.model,
        "messages": [m.model_dump() for m in req.messages],
        "stream": req.stream,
        "options": {"num_ctx": settings.CHAT_NUM_CTX},
    }
    if req.temperature is not None:
        payload["options"]["temperature"] = req.temperature
    return payload


def _format_chat_chunk(line: dict, model: str, chat_id: str) -> dict | None:
    if line.get("done"):
        return None
    content = line.get("message", {}).get("content", "")
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def _stream_chat(payload: dict, model: str) -> Iterator[str]:
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    with ollama_client.post_stream("/api/chat", payload) as lines:
        for line in lines:
            if not line:
                continue
            chunk = _format_chat_chunk(json.loads(line), model, chat_id)
            if chunk:
                yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


def _stream_completion(payload: dict, model: str) -> Iterator[str]:
    """Stream FIM completion, stopping at double-newline.

    qwen2.5-coder Q4 doesn't reliably emit <|endoftext|> to self-terminate
    FIM completions. Without intervention the model runs into prose.
    Stopping at the first \\n\\n after real content captures the intended
    completion without the runon.
    """
    completion_id = f"cmpl-{uuid.uuid4().hex}"
    has_content = False
    tail = ""  # rolling 2-char window to detect \\n\\n split across chunks
    with ollama_client.post_stream("/api/generate", payload) as lines:
        for line in lines:
            if not line:
                continue
            data = json.loads(line)
            if data.get("done"):
                break
            text = data.get("response", "")
            if not text:
                continue
            if text.strip():
                has_content = True
            if has_content:
                combined = tail + text
                stop = combined.find("\n\n")
                if stop != -1:
                    before = combined[len(tail):stop]
                    if before:
                        chunk = fim.format_completion_chunk(
                            {"response": before}, model, completion_id
                        )
                        if chunk:
                            yield f"data: {json.dumps(chunk)}\n\n"
                    break
                tail = combined[-2:]
            chunk = fim.format_completion_chunk(data, model, completion_id)
            if chunk:
                yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    payload = _to_ollama_chat(req)
    if req.stream:
        return StreamingResponse(_stream_chat(payload, req.model), media_type="text/event-stream")
    data = ollama_client.post_json("/api/chat", payload)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{"index": 0, "message": data["message"], "finish_reason": "stop"}],
    }


@app.post("/v1/completions")
def completions(req: CompletionRequest):
    payload = fim.to_ollama_generate(req)
    if req.stream:
        return StreamingResponse(
            _stream_completion(payload, req.model), media_type="text/event-stream"
        )
    data = ollama_client.post_json("/api/generate", payload)
    return fim.format_completion_response(data, req.model)
