# Code Review — 2025-06-02

Multi-agent review of the security hardening and FIM truncation fixes landed in commits
`e3f563f`–`4388cb8`. 9 finder angles × 8 candidates, 1-vote verification, 1 gap sweep.

---

## Confirmed Bugs

### 1. `ALLOWED_MODELS=","` silently blocks all requests
**File:** `settings.py:30`

A comma-only or whitespace-only value for `ALLOWED_MODELS` is truthy, so the frozenset
branch runs, but after strip+filter all entries are removed — producing `frozenset()`.
`_check_model` then rejects every model with HTTP 400. `/healthz` still returns 200, so
monitoring misses the outage. `ALLOWED_MODELS=""` (unset equivalent) behaves oppositely:
it is falsy, so `ALLOWED_MODELS=None` and all models are allowed.

**Fix:** After parsing, raise at startup if the result is a non-None empty frozenset.

```python
_parsed = frozenset(m.strip() for m in _raw_models.split(",") if m.strip()) if _raw_models else None
if _parsed is not None and not _parsed:
    raise ValueError("ALLOWED_MODELS is set but contains no valid model names")
ALLOWED_MODELS: frozenset[str] | None = _parsed
```

---

### 2. `PROXY_AUTH_TOKEN=""` permanently locks the proxy
**File:** `settings.py:27`

An empty-string token is not `None`, so `_verify_token` enforces auth. No client can
supply a token that `secrets.compare_digest` matches against `""` (HTTPBearer rejects
empty/absent bearer tokens before they reach the comparison). Every `/v1/*` request returns
401 with no startup warning.

**Fix:** Treat empty string the same as unset:

```python
PROXY_AUTH_TOKEN: str | None = os.environ.get("PROXY_AUTH_TOKEN") or None
```

---

### 3. Non-streaming chat: `data['message']` hard key lookup raises KeyError
**File:** `proxy/server.py:180`

`chat_completions` accesses `data["message"]` directly. If Ollama returns a 200 with an
unexpected body (no `message` key), the handler raises `KeyError` → unhandled HTTP 500.
The analogous completions path uses `data.get("response", "")` defensively.

**Fix:**
```python
"choices": [{"index": 0, "message": data.get("message", {}), "finish_reason": "stop"}],
```

---

### 4. `stop` strings have no individual `max_length`
**File:** `proxy/schemas.py:24`

The list is capped at 20 items but each string is unbounded. A request with
`stop=["A" * 100_000] * 20` passes Pydantic validation and forwards a ~2 MB stop array
to Ollama on every request, bypassing the intent of the size limits added in this diff.

**Fix:**
```python
stop: list[str] | None = Field(None, max_length=20, json_schema_extra={"items": {"maxLength": 200}})
```
Or as a Pydantic v2 annotated type:
```python
stop: list[Annotated[str, Field(max_length=200)]] | None = Field(None, max_length=20)
```

---

### 5. Zero test coverage for auth and model-allowlist controls
**File:** `tests/test_server.py:1`

`_verify_token` and `_check_model` have no tests. A future refactor could break auth
silently. The HTTP 400 vs 403 issue in `_check_model` exists precisely because this path
is untested.

**Fix:** Add tests that patch `settings.PROXY_AUTH_TOKEN` / `settings.ALLOWED_MODELS`
directly (not via `os.environ` — settings are frozen at import time):

```python
def test_auth_required(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    resp = client.get("/v1/models")
    assert resp.status_code == 401

def test_auth_valid_token(monkeypatch):
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", "secret")
    resp = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
```

---

### 6. Real env vars in shell break entire test suite
**File:** `tests/test_server.py:10`

`TestClient(app)` and `settings` are module-level. If `PROXY_AUTH_TOKEN` or
`ALLOWED_MODELS` are set in the developer's shell, all `/v1/*` tests return 401/400 with
no diagnostic. A fixture that resets `settings.PROXY_AUTH_TOKEN = None` and
`settings.ALLOWED_MODELS = None` before each test is needed.

---

## Plausible / Should Fix

### 7. `_check_model` returns HTTP 400 instead of 403
**File:** `proxy/server.py:34`

A syntactically valid request blocked by policy is an authorization failure (403 Forbidden),
not a malformed request (400 Bad Request). The existing auth guard correctly uses 401.

**Fix:** `raise HTTPException(status_code=403, detail=f"Model not allowed: {model}")`

---

### 8. Streaming response connection not closed on `raise_for_status()` error
**File:** `proxy/ollama_client.py:67`

In `post_stream`, if `resp.raise_for_status()` raises `HTTPError` (converted to
`OllamaError`), execution never reaches `with resp:` so `resp.__exit__` is never called.
Cleanup depends on CPython GC reference counting. Under sustained error load this can
exhaust the connection pool before GC collects.

**Fix:** Enter the response context manager inside `_handle_request_errors`:

```python
with _handle_request_errors(timeout):
    resp = _session().post(...)
    resp.raise_for_status()
    resp.__enter__()
try:
    yield _safe_lines(resp.iter_lines(decode_unicode=True))
finally:
    resp.__exit__(None, None, None)
```
Or use a `try/finally` around the yield.

---

### 9. `GET /v1/models` reveals all installed models even when `ALLOWED_MODELS` restricts completions
**File:** `proxy/server.py:54`

An authenticated client can enumerate every model installed in Ollama via `/v1/models`,
including ones blocked by `ALLOWED_MODELS`. This leaks inventory information.

**Fix:** Filter the model list by `ALLOWED_MODELS` when it is set:

```python
models = [
    {"id": m["name"], ...}
    for m in data.get("models", [])
    if settings.ALLOWED_MODELS is None or m["name"] in settings.ALLOWED_MODELS
]
```

---

### 10. `post_stream` type annotation is wrong
**File:** `proxy/ollama_client.py:65`

`post_stream` is annotated `-> Iterator[str]` but is a `@contextmanager` returning
`ContextManager[Iterator[str]]`. A caller who reads the annotation and skips the `with`
block gets a `GeneratorContextManager`, and `for line in ...` raises `TypeError`.

**Fix:** `-> Generator[Iterator[str], None, None]` or add a comment clarifying usage.

---

### 11. `_check_model` called imperatively rather than as a FastAPI dependency
**File:** `proxy/server.py:34`

`_verify_token` is wired via `dependencies=[]` in the decorator (impossible to forget).
`_check_model` is called manually in the handler body. A future route author following
the decorator pattern will silently omit the allowlist check.

**Fix:** Convert to a body-aware dependency, or at minimum add a docstring warning.

---

### 12. `c.strip()` allocates per character in hot path
**File:** `proxy/server.py:89`

```python
content_start = next((i for i, c in enumerate(combined) if c.strip()), None)
```

`c.strip()` on a single character allocates a new string object. `not c.isspace()` is
the correct idiomatic test and avoids the allocations.

**Fix:** `next((i for i, c in enumerate(combined) if not c.isspace()), None)`

---

## Refuted (investigated, not bugs)

- **`combined[len(tail):stop]` empty slice** — Tail bytes are already yielded in the
  previous `yield text` call; the empty slice correctly means "nothing additional to emit
  from this chunk before the stop." Not a data loss bug.
- **`_truncate_fim_text("\n\n")` behavioral change** — The new behavior (return `"\n\n"`
  unchanged instead of `""`) is more correct. A response of only whitespace has no real
  content, so there is nothing to truncate.
- **urllib3 `ProtocolError` escaping `_safe_lines`** — In requests ≥ 2.x, urllib3
  exceptions during streaming are wrapped in `ChunkedEncodingError` (a
  `RequestException` subclass) before reaching `_safe_lines`. No escape path.
