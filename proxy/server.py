import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import settings
from proxy import fim, formatting, ollama_client
from proxy.ollama_client import OllamaError
from proxy.schemas import ChatRequest, CompletionRequest

# uvicorn configures only its own loggers; without a root handler our lines would be dropped.
logging.basicConfig(format="%(levelname)s:     %(message)s")
log = logging.getLogger("proxy")
log.setLevel(logging.INFO)

app = FastAPI()

_bearer = HTTPBearer(auto_error=False)

# A blank line, possibly indented or CRLF: the same test as Continue's noDoubleNewLine.
_BLANK_LINE = re.compile(r"\r?\n[ \t]*\r?\n")


async def _verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    if settings.PROXY_AUTH_TOKEN is None:
        return
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, which would be a 500.
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), settings.PROXY_AUTH_TOKEN.encode()
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.exception_handler(OllamaError)
def ollama_error_handler(request: Request, exc: OllamaError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": "upstream_error"}},
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(_verify_token)])
def list_models() -> dict:
    # Every request is routed to one of these two models, whatever model the client names.
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "local"}
            for m in dict.fromkeys([settings.CHAT_MODEL, settings.FIM_MODEL])
        ],
    }


def _to_ollama_chat(req: ChatRequest) -> dict:
    payload: dict = {
        "model": settings.CHAT_MODEL,
        "messages": [m.model_dump() for m in req.messages],
        "options": {"num_ctx": settings.CHAT_NUM_CTX},
    }
    if req.temperature is not None:
        payload["options"]["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["options"]["num_predict"] = req.max_tokens
    return payload


def _fim_stop(text: str) -> int:
    """Index of the first blank line after real content in text, or -1."""
    match = _BLANK_LINE.search(text, len(text) - len(text.lstrip()))
    return match.start() if match else -1


async def _fim_text(payload: dict, done: dict) -> AsyncIterator[str]:
    """Yield FIM text, stopping at the first blank line after real content.

    qwen2.5-coder doesn't reliably emit <|endoftext|> to end a FIM completion, so it runs on
    into prose. Continue makes the same cut client-side but keeps reading the stream in the
    background, so stopping here is what frees the model. done receives Ollama's final line.
    """
    emitted = ""
    async with ollama_client.stream("/api/generate", payload) as lines:
        async for data in lines:
            text = data.get("response", "")
            stop = _fim_stop(emitted + text)
            if stop != -1:
                if stop > len(emitted):
                    yield text[: stop - len(emitted)]
                return
            if text:
                yield text
            emitted += text
            if data.get("done"):
                done.update(data)  # keep reading to the end so the connection can be reused


async def _chat_text(payload: dict, done: dict) -> AsyncIterator[str]:
    """Yield chat content from Ollama; done receives Ollama's final line."""
    async with ollama_client.stream("/api/chat", payload) as lines:
        async for data in lines:
            content = data.get("message", {}).get("content", "")
            if content:
                yield content
            if data.get("done"):
                done.update(data)


async def _logged(route: str, texts: AsyncIterator[str], done: dict, **fields) -> AsyncIterator[str]:
    """Pass text through, logging one line per request when it ends, however it ends.

    Ollama's timings arrive only on its final line, which stopped and cancelled requests never
    see, so the proxy's own timings are the primary latency signal.
    """
    start = time.monotonic()
    first = None
    chunks = 0
    outcome = "cancelled"  # unless the stream finishes or fails below
    try:
        async for text in texts:
            if first is None:
                first = time.monotonic()
            chunks += 1
            yield text
        outcome = "done" if done else "stopped"
    except OllamaError:
        outcome = "error"
        raise
    finally:
        stats = {
            "outcome": outcome,
            "ttft_ms": round((first - start) * 1000) if first else None,
            "total_ms": round((time.monotonic() - start) * 1000),
            "chunks": chunks,
            **fields,
        }
        if done:
            stats.update(
                done_reason=done.get("done_reason"),
                load_ms=done.get("load_duration", 0) // 1_000_000,  # Ollama reports nanoseconds
                prompt_tokens=done.get("prompt_eval_count"),
                prefill_ms=done.get("prompt_eval_duration", 0) // 1_000_000,
                eval_tokens=done.get("eval_count"),
                eval_ms=done.get("eval_duration", 0) // 1_000_000,
            )
        log.info("%s %s", route, " ".join(f"{k}={v}" for k, v in stats.items()))


def _finish_reason(done: dict) -> str:
    return "length" if done.get("done_reason") == "length" else "stop"


async def _sse(chunks: AsyncIterator[dict]) -> AsyncIterator[str]:
    try:
        async for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"
    except OllamaError as e:
        error = {"error": {"message": e.message, "type": "server_error"}}
        yield f"data: {json.dumps(error)}\n\n"


@app.post("/v1/chat/completions", dependencies=[Depends(_verify_token)])
async def chat_completions(req: ChatRequest):
    done: dict = {}
    texts = _logged(
        "chat", _chat_text(_to_ollama_chat(req), done), done,
        messages=len(req.messages), max_tokens=req.max_tokens,
    )
    model = settings.CHAT_MODEL
    if req.stream:
        chat_id = f"chatcmpl-{uuid.uuid4().hex}"
        chunks = (formatting.format_chat_chunk(t, model, chat_id) async for t in texts)
        return StreamingResponse(_sse(chunks), media_type="text/event-stream")
    content = "".join([t async for t in texts])
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": _finish_reason(done),
        }],
    }


@app.post("/v1/completions", dependencies=[Depends(_verify_token)])
async def completions(req: CompletionRequest):
    done: dict = {}
    texts = _logged(
        "fim", _fim_text(fim.to_ollama_generate(req), done), done,
        prompt_chars=len(req.prompt), max_tokens=req.max_tokens, stops=len(req.stop or []),
    )
    model = settings.FIM_MODEL
    if req.stream:
        completion_id = f"cmpl-{uuid.uuid4().hex}"
        chunks = (formatting.format_completion(t, model, completion_id) async for t in texts)
        return StreamingResponse(_sse(chunks), media_type="text/event-stream")
    text = "".join([t async for t in texts])
    return formatting.format_completion(text, model, f"cmpl-{uuid.uuid4().hex}", _finish_reason(done))
