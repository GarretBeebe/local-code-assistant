# v4 Implementation Plan: RAG Context Injection

## Summary

Use rag-system's existing code index to inject relevant code chunks into every chat completion
system prompt. rag-system already watches and indexes `/watch/Code` continuously via Qdrant.
This feature adds:

1. A retrieval-only HTTP endpoint to rag-system (`POST /v1/retrieve`)
2. A lightweight client in the code assistant that calls it
3. The injection hook in `proxy/server.py` (same location as v2 was going to use)

No new indexing infrastructure. No watcher. No tree-sitter. No SQLite. The code assistant
becomes a client of a service that already exists.

---

## Changes to rag-system

### New endpoint: `POST /v1/retrieve`

**Location:** `web/api_server.py`

**Request:**
```json
{ "query": "validate user token", "limit": 3 }
```

**Response:**
```json
{
  "chunks": [
    { "text": "def validate_token(token: str) -> User | None:\n    ...", "filepath": "/watch/Code/proxy/auth.py", "score": 0.91 },
    { "text": "class User(BaseModel):\n    id: str\n    ...", "filepath": "/watch/Code/proxy/models.py", "score": 0.87 }
  ]
}
```

**Implementation:**
```python
@app.post("/v1/retrieve")
def retrieve(req: RetrieveRequest) -> dict:
    _check_internal_token(request)
    chunks = retrieve_best(req.query, final_k=req.limit)
    return {
        "chunks": [
            {"text": c.payload.get("text", ""), "filepath": c.payload.get("filepath", ""), "score": c.rerank_score or c.score}
            for c in chunks
        ]
    }
```

**Auth:** Bearer token checked in the handler via a new `_check_internal_token(request)` helper.
Uses a new setting `RAG_INTERNAL_TOKEN`. If unset, the endpoint is disabled (returns 503).
This is a machine-to-machine credential, not user auth — no session, no cookie.

**Middleware bypass:** Add `/v1/retrieve` to the security_middleware bypass list (same pattern
as `/auth/login`) so the global auth check doesn't 401 the request before the handler runs.
The handler performs its own auth check immediately.

### New schema: `RetrieveRequest`

**Location:** `web/schemas.py`

```python
class RetrieveRequest(BaseModel):
    query: str
    limit: int = 3
```

Field limits: `query` max 500 chars (enforce at schema level), `limit` clamped to 1–10.

### New setting: `RAG_INTERNAL_TOKEN`

**Location:** `rag-system/settings.py`

```python
RAG_INTERNAL_TOKEN: str | None = os.environ.get("RAG_INTERNAL_TOKEN") or None
```

Add to `.env.example` with a comment: "Shared secret for server-to-server /v1/retrieve calls.
Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""

### Bug fix: `ALLOWED_EXTENSIONS` missing TS/JS

**Location:** `rag-system/settings.py`

The watcher config indexes `.ts`, `.tsx`, `.js`, `.jsx` but `ALLOWED_EXTENSIONS` in settings
only contains `.md`, `.txt`, `.py`, `.json`, `.yaml`, `.yml`, `.toml`. This means the
`_extract_filename` heuristic in `retrieval.py` won't boost results for TypeScript/JS files
mentioned by name in queries.

Fix: add `.ts`, `.tsx`, `.js`, `.jsx` to `ALLOWED_EXTENSIONS`. This unblocks filename-boosted
retrieval for code files in those languages.

---

## Changes to code assistant

### New file: `context/__init__.py`
Empty.

### New file: `context/rag_client.py`

Thin HTTP client wrapping the `/v1/retrieve` endpoint:

```python
import settings
import requests

_session = requests.Session()

def retrieve_chunks(query: str, limit: int | None = None) -> list[dict]:
    """Return chunks from rag-system, or [] if unavailable or not configured."""
    if not settings.RAG_BASE_URL or not settings.RAG_INTERNAL_TOKEN:
        return []
    try:
        resp = _session.post(
            f"{settings.RAG_BASE_URL}/v1/retrieve",
            json={"query": query, "limit": limit or settings.RAG_CONTEXT_CHUNKS},
            headers={"Authorization": f"Bearer {settings.RAG_INTERNAL_TOKEN}"},
            timeout=settings.RAG_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json().get("chunks", [])
    except Exception:
        return []
```

No logging of the exception — we don't want noise when rag-system is down. The proxy
degrades silently to v1 behavior (no context), which is always correct.

### New file: `context/manager.py`

Assembles the system prompt prefix from retrieved chunks:

```python
from context import rag_client

def build_context_prefix(query: str) -> str:
    chunks = rag_client.retrieve_chunks(query)
    if not chunks:
        return ""
    lines = ["Relevant code from your project:\n"]
    for chunk in chunks:
        if chunk.get("filepath"):
            lines.append(f"# {chunk['filepath']}")
        lines.append(chunk["text"].strip())
        lines.append("")
    return "\n".join(lines)
```

### Modified: `proxy/server.py`

Two changes:

1. Extract the user's query from the messages for the retrieval call:
```python
from context import manager as ctx_manager

def _to_ollama_chat(req: ChatRequest) -> dict:
    messages = [m.model_dump() for m in req.messages]
    user_text = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )
    prefix = ctx_manager.build_context_prefix(user_text)
    if prefix:
        messages = [{"role": "system", "content": prefix}] + messages
    payload: dict = {
        "model": settings.CHAT_MODEL,
        "messages": messages,
        ...
    }
```

Note: `user_text` is passed to `retrieve_chunks` as the semantic query. This is more
meaningful than passing the full messages array — the retrieval model needs a short, dense
query, not a conversation thread.

2. No lifespan changes needed — there's no background thread to start.

### Modified: `settings.py`

Add:
```python
RAG_BASE_URL         = os.environ.get("RAG_BASE_URL", "")           # empty = disabled
RAG_INTERNAL_TOKEN   = os.environ.get("RAG_INTERNAL_TOKEN") or None
RAG_CONTEXT_CHUNKS   = _int("RAG_CONTEXT_CHUNKS", "3")
RAG_TIMEOUT_SECONDS  = _float("RAG_TIMEOUT_SECONDS", "1.5")
```

`RAG_BASE_URL` defaults to empty string, which disables the feature. Operators must
explicitly configure it to enable context injection.

### Modified: `pyproject.toml`

No new dependencies. `requests` is already a dependency. `qdrant-client` is NOT needed —
the code assistant talks to rag-system's HTTP API, not Qdrant directly.

### Modified: `.env.example`

Add:
```
# RAG context injection (v4) — leave blank to disable
RAG_BASE_URL=http://localhost:8000
RAG_INTERNAL_TOKEN=
RAG_CONTEXT_CHUNKS=3
RAG_TIMEOUT_SECONDS=1.5
```

---

## Directory structure after v4

```
code-assistant/
├── context/
│   ├── __init__.py
│   ├── manager.py       # assembles system prompt prefix
│   └── rag_client.py    # HTTP client for rag-system /v1/retrieve
├── proxy/
│   └── server.py        # modified: context injection in _to_ollama_chat
├── settings.py          # modified: RAG_* settings
└── .env.example         # modified: RAG_* keys
```

No `common/`, no `indexer/`, no `config/`, no `data/`. The original v2 directory tree is
gone entirely.

---

## Implementation order

**In rag-system:**
1. Add `RAG_INTERNAL_TOKEN` to `settings.py`
2. Add `RetrieveRequest` to `web/schemas.py` with field limits
3. Add `/v1/retrieve` handler to `web/api_server.py`
4. Add middleware bypass for `/v1/retrieve`
5. Fix `ALLOWED_EXTENSIONS` to include TS/JS
6. Tests: endpoint returns chunks, 503 when token unset, 401 on wrong token, degrades if Qdrant unavailable

**In code assistant:**
7. Add `RAG_*` settings to `settings.py`
8. Create `context/rag_client.py` — with a test that it returns `[]` when `RAG_BASE_URL` is unset
9. Create `context/manager.py`
10. Update `proxy/server.py` — inject prefix in `_to_ollama_chat`
11. Update `.env.example`
12. Tests: `_to_ollama_chat` includes system message when manager returns non-empty, passes through unchanged when manager returns empty

---

## Latency impact

The retrieval call adds ~200-500ms to each chat completion (embed + Qdrant + rerank). The
`RAG_TIMEOUT_SECONDS` default of 1.5s caps the worst case. FIM requests are unaffected.
Chat completions are not latency-sensitive at the same level as FIM — a 500ms overhead on a
request that then waits 2-5s for generation is acceptable.

If rag-system is under load and hits its concurrency limit, `/v1/retrieve` will queue behind
other requests. Consider whether the internal token endpoint should bypass the semaphore in
rag-system — it probably should, since it's a lightweight retrieval-only call with no
generation step.

---

## Open questions

1. **Should `/v1/retrieve` bypass rag-system's `RAG_CONCURRENCY_LIMIT` semaphore?**
   Retrieval without generation is much cheaper than a full RAG request. Sharing the
   semaphore could cause context injection to time out during heavy rag-system load. Bypassing
   it (or having a separate limit) is probably correct.

2. **How much context is useful?** `RAG_CONTEXT_CHUNKS=3` is a guess. Each chunk is up to
   `MAX_CHUNK_CHARS=2000` characters. 3 chunks = up to 6000 characters of injected context.
   That's substantial — may need to tune down if it crowds out the user's actual message in
   the context window.

3. **Should the query include conversation history?** Using only the last user message is
   simple. Using the last N messages (concatenated) would give better retrieval for follow-up
   questions but increases query length and noise. Start with last message only.
