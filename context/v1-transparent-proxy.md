# v1: Transparent Proxy — Implementation Spec

## Goal

Build the smallest thing that proves the pipeline: Continue.dev sends OpenAI-format requests,
the proxy forwards them to Ollama unchanged (format-translated), and autocomplete + chat work
end-to-end. No context injection in v1 — that is v2.

---

## File Structure

```
/                              ← repo root
├── proxy/
│   ├── __init__.py
│   ├── server.py              ← FastAPI app + routes
│   ├── ollama_client.py       ← thread-local requests.Session
│   ├── fim.py                 ← FIM format translation
│   └── schemas.py             ← Pydantic OpenAI-compat models
├── settings.py
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── code-assistant.service
└── .env.example
```

---

## Deployment Model

Two equally supported paths — choose based on target machine:

**Systemd (Linux primary target — GMKtec K16)**
- Uvicorn runs directly on the host
- Ollama at `localhost:11434` — zero hops
- `code-assistant.service` unit, `Restart=on-failure`

**Docker (Windows / cross-platform)**
- Single-service compose stack
- Ollama stays on the host; reached via `host.docker.internal:11434`
- `extra_hosts: host.docker.internal:host-gateway` makes this work on Linux Docker too
- On Linux you can also use `network_mode: host` + `OLLAMA_BASE_URL=http://localhost:11434` to
  skip NAT entirely (~2-3ms savings, matters for autocomplete)
- File watching in v2 will use `PollingObserver` (rag-system pattern) — works on all platforms
  including Windows NTFS bind mounts

---

## Routes

| Method | Path | Upstream | Purpose |
|---|---|---|---|
| `GET` | `/healthz` | — | Liveness check |
| `GET` | `/v1/models` | `GET /api/tags` | List available models |
| `POST` | `/v1/chat/completions` | `POST /api/chat` | Chat (streaming + non-streaming) |
| `POST` | `/v1/completions` | `POST /api/generate` | FIM autocomplete (streaming + non-streaming) |

---

## Format Translation

### Chat completions (`/v1/chat/completions` → `/api/chat`)

**Request:**
```json
// OpenAI in
{ "model": "qwen2.5-coder:14b", "messages": [...], "stream": true, "temperature": 0.7 }

// Ollama out
{ "model": "qwen2.5-coder:14b", "messages": [...], "stream": true,
  "options": { "num_ctx": 8192, "temperature": 0.7 } }
```

**Streaming response** (Ollama NDJSON → OpenAI SSE):
```
// Ollama line
{"model":"...","message":{"role":"assistant","content":"Hello"},"done":false}

// SSE chunk emitted
data: {"id":"chatcmpl-<uuid>","object":"chat.completion.chunk","created":<ts>,
       "model":"...","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

// Terminal line
data: [DONE]
```

**Non-streaming**: Ollama returns one JSON object with `message.content`; reshape to OpenAI
`ChatCompletion` with `choices[0].message`.

### FIM completions (`/v1/completions` → `/api/generate`)

FIM tokens (`<|fim_prefix|>`, `<|fim_suffix|>`, `<|fim_middle|>`) pass through unchanged —
`qwen2.5-coder` understands them natively. The proxy's job is format + options translation:

```json
// OpenAI in
{ "model": "qwen2.5-coder:7b", "prompt": "<|fim_prefix|>....<|fim_middle|>",
  "stream": true, "max_tokens": 64 }

// Ollama out
{ "model": "qwen2.5-coder:7b",
  "prompt": "<|fim_prefix|>....<|fim_middle|>",
  "stream": true,
  "options": { "num_ctx": 4096, "num_predict": 64, "temperature": 0.1 } }
```

**Streaming response** (Ollama NDJSON → OpenAI SSE):
```
// Ollama line
{"model":"...","response":"def ","done":false}

// SSE chunk emitted
data: {"id":"cmpl-<uuid>","object":"text_completion","created":<ts>,
       "model":"...","choices":[{"text":"def ","index":0,"finish_reason":null}]}
```

### Models list (`/v1/models` → `/api/tags`)

```json
// Ollama /api/tags response
{"models": [{"name": "qwen2.5-coder:7b", ...}]}

// OpenAI /v1/models response
{"object": "list", "data": [{"id": "qwen2.5-coder:7b", "object": "model",
                              "created": 0, "owned_by": "local"}]}
```

---

## Streaming Implementation

`StreamingResponse` wraps a sync generator — FastAPI runs sync iterators in a thread pool
via `iterate_in_threadpool` automatically. No manual thread management needed.

```python
def _stream_chat(payload: dict, model: str) -> Iterator[str]:
    with requests.post(..., stream=True) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            chunk = _format_chat_chunk(json.loads(line), model)
            if chunk:
                yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    payload = _to_ollama_chat(req)
    if req.stream:
        return StreamingResponse(_stream_chat(payload, req.model),
                                 media_type="text/event-stream")
    ...
```

---

## `proxy/ollama_client.py`

Adapted from `rag-system/api/ollama_client.py`. Keeps:
- Thread-local `requests.Session` (connection reuse across requests in same thread)
- `_url(path)` helper
- `post_json(path, payload, timeout)` → parsed dict, raises on non-2xx
- `post_stream(path, payload, timeout)` → context manager yielding `iter_lines`

Drops for v1: `BoundedSemaphore`, retry logic, cancellation tokens. This is a single-user local
tool; concurrency control is not needed yet.

---

## `settings.py`

```python
OLLAMA_BASE_URL        = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL             = os.environ.get("CHAT_MODEL", "qwen2.5-coder:14b")
FIM_MODEL              = os.environ.get("FIM_MODEL", "qwen2.5-coder:7b")
CHAT_NUM_CTX           = int(os.environ.get("CHAT_NUM_CTX", "8192"))
FIM_NUM_CTX            = int(os.environ.get("FIM_NUM_CTX", "4096"))
FIM_MAX_TOKENS         = int(os.environ.get("FIM_MAX_TOKENS", "128"))
PROXY_PORT             = int(os.environ.get("PROXY_PORT", "8080"))
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120.0"))
```

No validation block — the set is small enough that bad values will fail loudly on first use.

---

## `pyproject.toml` Dependencies

Runtime: `fastapi`, `uvicorn[standard]`, `requests`, `pyyaml` (placeholder for v2 watcher config)
Dev: `pytest`, `ruff`
Ruff: line-length=100, same profile as rag-system

---

## Verification Steps

1. `pip install -e .` — no import errors
2. `uvicorn proxy.server:app --port 8080` — server starts, logs port
3. `curl localhost:8080/healthz` → `{"status":"ok"}`
4. `curl localhost:8080/v1/models` → JSON array of Ollama models
5. `curl -X POST localhost:8080/v1/chat/completions -d '{"model":"qwen2.5-coder:14b","messages":[{"role":"user","content":"say hi"}],"stream":false}'` → coherent response
6. Same with `"stream":true` → SSE chunks visible in curl output
7. `curl -X POST localhost:8080/v1/completions` with FIM prompt → completion tokens stream back
8. Configure Continue.dev to point at `http://localhost:8080/v1` → autocomplete and chat work in editor
9. `docker compose up --build` → same curls pass via container

---

## What This Does NOT Include (v2+)

- Symbol extraction (tree-sitter)
- File watcher (watchdog + PollingObserver, adapted from rag-system)
- SQLite symbol store
- Context injection into system prompt
- Dual-model routing (chat → 14b, FIM → 7b) — routing is implicit in v1 via Continue.dev config
- RAG bridge to Qdrant
