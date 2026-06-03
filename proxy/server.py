import json
import time
import uuid
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import settings
from proxy import fim, formatting, ollama_client
from proxy.ollama_client import OllamaError
from proxy.schemas import ChatRequest, CompletionRequest

app = FastAPI()

def _sse_error(message: str) -> str:
    return f'data: {json.dumps({"error": {"message": message, "type": "server_error"}})}\n\n'


@app.exception_handler(OllamaError)
def ollama_error_handler(request: Request, exc: OllamaError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": "upstream_error"}},
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/models")
def list_models() -> dict:
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
    if req.max_tokens is not None:
        payload["options"]["num_predict"] = req.max_tokens
    return payload


def _parse_stream_line(line: str) -> dict:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        raise ValueError("malformed response from Ollama")


def _find_fim_truncation(tail: str, text: str) -> tuple[str | None, str]:
    """Detect \\n\\n across chunk boundaries. Returns (text_before_stop, new_tail).
    text_before_stop is None when no stop marker is found."""
    combined = tail + text
    stop = combined.find("\n\n")
    if stop != -1:
        return combined[len(tail):stop], ""
    return None, combined[-2:]


def _truncate_fim_text(text: str) -> str:
    content_start = next((i for i, c in enumerate(text) if c.strip()), None)
    if content_start is None:
        return text
    stop = text.find("\n\n", content_start)
    return text[:stop] if stop != -1 else text


def _iter_completion_text(payload: dict) -> Iterator[str]:
    """Yield raw FIM text chunks from Ollama, stopping at \\n\\n after real content."""
    has_content = False
    tail = ""
    with ollama_client.post_stream("/api/generate", payload) as lines:
        for line in lines:
            if not line:
                continue
            data = _parse_stream_line(line)
            if data.get("done"):
                break
            text = data.get("response", "")
            if not text:
                continue
            if text.strip():
                has_content = True
            if has_content:
                before_stop, tail = _find_fim_truncation(tail, text)
                if before_stop is not None:
                    if before_stop:
                        yield before_stop
                    return
            yield text


def _wrap_sse_stream(inner: Iterator[str]) -> Iterator[str]:
    try:
        yield from inner
    except OllamaError as e:
        yield _sse_error(e.message)
        return
    except ValueError as e:
        yield _sse_error(str(e))
        return
    yield "data: [DONE]\n\n"


def _chat_chunks(payload: dict, model: str, chat_id: str) -> Iterator[str]:
    with ollama_client.post_stream("/api/chat", payload) as lines:
        for line in lines:
            if not line:
                continue
            data = _parse_stream_line(line)
            chunk = formatting.format_chat_chunk(data, model, chat_id)
            if chunk:
                yield f"data: {json.dumps(chunk)}\n\n"


def _stream_chat(payload: dict, model: str) -> Iterator[str]:
    return _wrap_sse_stream(_chat_chunks(payload, model, f"chatcmpl-{uuid.uuid4().hex}"))


def _completion_chunks(payload: dict, model: str, completion_id: str) -> Iterator[str]:
    for text in _iter_completion_text(payload):
        chunk = formatting.format_completion_chunk(text, model, completion_id)
        yield f"data: {json.dumps(chunk)}\n\n"


def _stream_completion(payload: dict, model: str) -> Iterator[str]:
    """Stream FIM completion, stopping at double-newline.

    qwen2.5-coder Q4 doesn't reliably emit <|endoftext|> to self-terminate
    FIM completions. Without intervention the model runs into prose.
    Stopping at the first \\n\\n after real content captures the intended
    completion without the runon.
    """
    return _wrap_sse_stream(_completion_chunks(payload, model, f"cmpl-{uuid.uuid4().hex}"))


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
    return formatting.format_completion_response(
        _truncate_fim_text(data.get("response", "")), req.model
    )
