# v5: Audit Fixes — Responsiveness, Accuracy, Simplification

**Status:** Implemented and deployed (2026-10-05). See Implementation Notes, Tuning Results, and Deployment at the end.

## Summary

A principal-engineer audit of the proxy, checked against the live system rather than the design
docs. Evidence came from four places: the deployed container and image, Ollama's own logs and
config, Continue.dev 2.0's shipped client code, and a disconnect experiment against the real proxy.
The experiment ran under the dev venv and again inside the production image.

The assistant has been effectively unusable on the target hardware, for three reasons:

1. **The proxy never cancels abandoned generations.** Continue aborts most autocomplete requests on
   slow hardware, but Ollama keeps generating until `\n\n` or `num_predict`. With
   `OLLAMA_NUM_PARALLEL=1`, every leaked request blocks the next completion.
2. **The FIM model is the wrong variant and the wrong size.**
   - `qwen2.5-coder:7b` is the **Instruct** finetune (`general.finetune: Instruct`), not the base
     model Qwen trains for FIM. The `\n\n` truncation patches the symptom: run-on output.
     *Correction after benchmarking:* base models run on at about the same rate, so the variant was
     not the cause of the run-on (see Tuning Results). The size finding stands.
   - Measured on this iGPU: ~158 tok/s prefill and ~8.9 tok/s decode, not the README's
     "~35 tok/s". Continue's default 1024-token autocomplete prompt costs ~6 s of prefill.
   - A cold load costs 26 s after Ollama's 5-minute idle unload.
3. **The service is down.** The container exited on host restart and has no restart policy. Prod
   logs show a 70-second smoke test on deploy day (9 FIM requests), then zero real requests in
   3 months, against ~9,075 internet scanner probes.

v5 fixes the code-side causes, deletes the never-enabled RAG integration, and documents the
model and client changes, which need no code.

---

## Evidence

| Finding | How it was verified |
|---|---|
| Client aborts never cancel upstream: 60/60 tokens generated in every case (FIM and chat, abort during prefill and after the 1st chunk). The proxy's own `\n\n` stop does close upstream (29 ms), so detection works | Fake-Ollama + real proxy under uvicorn; same result inside the prod image (starlette 1.3.1, uvicorn 0.50.0) |
| Cause: Starlette wraps the sync generator in `iterate_in_threadpool`. On disconnect the async side is cancelled, but the sync generator is never closed, so `with resp:` in `post_stream` never exits | `starlette/responses.py` + experiment |
| An async `httpx2` prototype cancels upstream in 28 ms mid-stream and **5–7 ms during prefill**. A sync `finally: gen.close()` wrapper fixes mid-stream only: it waits out the whole prefill | Both prototyped in the harness |
| uvicorn (0.48 dev, 0.50 prod) advertises ASGI `spec_version` 2.3, which is what makes Starlette run `listen_for_disconnect` | uvicorn source, dev and prod |
| Ollama 0.35.1 config: `NUM_PARALLEL=1`, `KEEP_ALIVE=5m`, flash attention off, Vulkan on the Radeon iGPU | `%LOCALAPPDATA%\Ollama\server.log` |
| Throughput: qwen2.5 7B (same arch/quant as the FIM model) 158 tok/s prefill, 8.9 tok/s decode (n≈100). 3B ~407 tok/s prefill (n=2). Cold load: 7B 26.2 s, 3B 10.9 s | llama-server timing lines mapped to models via manifests |
| Continue 2.0 defaults: `maxPromptTokens 1024`, `debounceDelay 350`, `modelTimeout 150` | `continue-2.0.0/out/extension.js` |
| How those defaults behave on slow hardware: `stopAfterMaxProcessingTime(375 ms)` aborts after ~10 chunks; `showWhateverWeHaveAtXMs(150)` shows only the first line; `noDoubleNewLine` runs client-side, but `ListenableGenerator` keeps reading the stream in the background | `continue-2.0.0/out/extension.js` |
| Continue also defaults `DEFAULT_CONTEXT_LENGTH=32768` and `DEFAULT_MAX_TOKENS=4096`, while the proxy runs `CHAT_NUM_CTX=8192`, so Ollama silently truncates large chats | `continue-2.0.0/out/extension.js` |
| Current FIM stop misses three cases: a trailing-space split (`"foo"`, `" \n"`, `"\n"`), CRLF `\r\n\r\n`, and whitespace-only blank lines | Ran `_find_fim_truncation` |
| Prod deps drift from `uv.lock`: fastapi 0.139 / starlette 1.3.1 / uvicorn 0.50 / py3.11, vs locked 0.136.3 / 1.2.1 / 0.48.0 and py3.12 in dev | `docker run --rm` on the prod image |
| **The prod image contains `/app/.env` (auth token)**, plus the host `.venv` (79 MB), `.git`, and `.claude/`. There is no `.dockerignore` | `docker run --rm` on the prod image |
| FastAPI reads and parses the full request body *before* the auth dependency runs | `fastapi/routing.py` (body read precedes `solve_dependencies`) |
| Continue 2.0's serializer can send `role: "tool"` and image arrays, which the schema rejects with 422. `tools`, `stop`, and `top_p` are silently dropped, so Agent-mode tools can't work | `toChatMessage` / `toChatBody` |

---

## Decisions

- **Delete the RAG integration (v4).**
  - It has never been enabled: `RAG_BASE_URL` was never set in prod, and the container isn't on
    `rag-bridge`.
  - It targets rag-system, not graph-rag.
  - On this iGPU, ~1.5k injected tokens would add an estimated 10–20 s of 14B prefill per chat
    turn.
  - Front-loading per-turn chunks into the system prompt also invalidates Ollama's KV cache for
    the whole conversation.
  - The code can be recovered from git (`3920a24` and follow-ups). rag-system's `/v1/retrieve`
    endpoint is left untouched.
- **Async upstream I/O (`httpx2`) rather than a sync close-wrapper.** Most autocomplete aborts
  happen during prefill, and only async cancellation interrupts it. `httpx2` is already
  Starlette's test-client dependency, so `requests` can be dropped.
- **Keep server-side FIM truncation, but fix it.** Continue reads the stream in the background for
  generator reuse, so the server-side stop is what bounds Ollama's work.
- **FIM model swap: documented, not automated.** Ollama on the host is left untouched; see
  "Outside this repo".

---

## Implementation Steps

Each step ends with `pytest` and `ruff check` green. No commits unless requested.

### Step 1 — Ops and build hygiene

- `docker-compose.yml`: `restart: unless-stopped` (matches `graph-rag-api`).
- New `.dockerignore`: `.env*`, `.venv`, `.git`, `.claude`, `tests`, `.pytest_cache`,
  `.ruff_cache`, `__pycache__`. This keeps the token out of the image and stops `COPY . .`
  overwriting the image's venv with the host's.
- `Dockerfile`: install from the lockfile so prod runs what the tests ran:

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd -r -s /bin/false appuser
USER appuser
CMD exec uvicorn proxy.server:app --host 0.0.0.0 --port ${PROXY_PORT:-8080}
```

The hatchling stub hack goes away. `settings` and `proxy` keep importing from the working
directory, exactly as today.

### Step 2 — Delete RAG and vestigial config

- Delete the RAG modules and their tests: `context/__init__.py`, `context/manager.py`,
  `context/rag_client.py`, `tests/test_context.py`. `context/` remains the docs folder, which also
  ends the code/docs mixing.
- `proxy/server.py`: drop the context-manager import and the system-prompt merge in
  `_to_ollama_chat`.
- `settings.py`: drop `RAG_*`.
- `settings.py`: drop `ALLOWED_MODELS` + `_check_configured_models`. v3 kept them so that
  misconfigured models "fail fast", but they compare two env vars set in the same `.env`. The real
  failure, a model that isn't installed, is surfaced by passing Ollama's error text through
  (Step 3).
- `settings.py`: drop `PROXY_PORT`. Python never reads it; compose and the Dockerfile read the env
  var directly.
- `GET /v1/models` returns exactly `[CHAT_MODEL, FIM_MODEL]`. Those are the only models the proxy
  serves, and it no longer calls Ollama, so `get_json` goes too.
- Tests: remove the RAG and allowlist tests, and the duplicate `test_fim_request_succeeds`. Add one
  test for the static model list.

### Step 3 — Async upstream and FIM generation policy

`pyproject.toml`: move `httpx2` to runtime dependencies, drop `requests`, then run `uv lock`.

`proxy/ollama_client.py`:
- Module-level client. Its timeout must be explicit: httpx2's 5 s default would turn every
  26 s cold load into a 504.
- One streaming entry point. It is a context manager, so an early `return` in the caller closes
  the socket deterministically.
- The thread-local session, `_safe_lines`, `post_json`, and `get_json` are removed.

```python
client = httpx2.AsyncClient(
    base_url=settings.OLLAMA_BASE_URL,
    timeout=httpx2.Timeout(settings.OLLAMA_TIMEOUT_SECONDS, connect=5.0),
)

@asynccontextmanager
async def stream(path: str, payload: dict) -> AsyncIterator[AsyncIterator[dict]]:
    """Stream NDJSON lines from Ollama (always stream=True), done line included."""
    try:
        async with client.stream("POST", path, json={**payload, "stream": True}) as resp:
            if resp.is_error:
                await resp.aread()
                raise OllamaError(502, f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
            yield _lines(resp)   # raises OllamaError on {"error": ...} lines and malformed JSON
    except httpx2.TimeoutException:
        raise OllamaError(504, f"Ollama request timed out after {settings.OLLAMA_TIMEOUT_SECONDS}s")
    except httpx2.HTTPError as e:
        raise OllamaError(502, f"Upstream model server error: {type(e).__name__}")
```

`proxy/server.py`:
- **Async route handlers.** Async `_fim_text` / `_chat_text` generators are built on
  `async with ollama_client.stream(...)`.
- **One upstream path.** Non-streaming requests also stream from Ollama and join the text, so
  parsing, stopping, error mapping, and logging live in one place.
- **SSE errors.** The SSE wrapper catches only `OllamaError`, never `CancelledError`. Streaming
  errors stay "200 + SSE error event"; non-streaming errors keep the JSON exception handler.
- **Ollama errors become visible.** A mid-stream `{"error": ...}` line is now an error event
  instead of a silent empty answer.
- **`finish_reason`.** Non-streaming responses take it from Ollama's `done_reason` when present.

**FIM stop helper.** It replaces `_find_fim_truncation`, `_truncate_fim_text`, and
`_parse_stream_line`. It mirrors Continue's own `noDoubleNewLine` (`line.trim() === ""`).
Prototyped: it passes all six existing truncation behaviors and fixes the three misses.

```python
_BLANK_LINE = re.compile(r"\r?\n[ \t]*\r?\n")

def _fim_stop(text: str) -> int:
    """Index of the first blank line after real content, or -1."""
    m = _BLANK_LINE.search(text, len(text) - len(text.lstrip()))
    return m.start() if m else -1
```

The streaming loop applies it to the accumulated text: output is capped at `FIM_MAX_TOKENS`, so
re-scanning is trivial. It yields only the part of the current chunk before the stop.

`proxy/fim.py`:
- **Top-level `"keep_alive": settings.FIM_KEEP_ALIVE`.** This is a new setting, default `"2h"`. It
  avoids the 26 s cold load after 5 idle minutes. The value is finite because the iGPU shares
  system RAM.
- **`num_predict` capped at the ceiling.** Set it to
  `min(req.max_tokens or FIM_MAX_TOKENS, FIM_MAX_TOKENS)`. Continue's generic default is 4096
  tokens; at 9 tok/s, an uncapped run-on holds the only Ollama slot for minutes.

Tests:
- **Test seam.** An autouse fixture sets
  `ollama_client.client = httpx2.AsyncClient(transport=MockTransport(fake))`. The fake records
  request JSON and returns NDJSON. Existing tests port with 1–2 line changes, and real network calls
  become impossible.
- **New tests** cover:
  - the three truncation edge cases;
  - the `max_tokens` ceiling;
  - `keep_alive`;
  - the forced `stream: true`;
  - `{"error"}` and malformed lines;
  - an HTTP error status carrying Ollama's text;
  - the non-streaming join.
- **Cancellation regression test.** It runs the real app under uvicorn in a thread, with a
  MockTransport handler that blocks. A client disconnects before the first token and again
  mid-stream. Assert the upstream request is cancelled in under 1 s in both cases. This guards the
  ASGI 2.3 dependency below.

### Step 4 — Observability

- `logging.basicConfig(level=INFO)`. uvicorn only configures its own loggers, so app logs are
  otherwise dropped.
- One line per completion request, written from a `finally` so cancellations are recorded too.
  Fields:
  - route and outcome (`done` / `stopped` / `cancelled` / `error`);
  - `ttft_ms`, `total_ms`, chunk count;
  - `prompt_chars`, requested `max_tokens`, stop-list length;
  - Ollama's `load_duration`, `prompt_eval_*`, and `eval_*` when the done line arrives. Stopped and
    cancelled requests never receive them, so the proxy's own timings are the primary signal.
- Prompt text is never logged.
- The requested `max_tokens` and stop count settle what Continue actually sends; its 4096 default
  is unverified for autocomplete.

### Step 5 — Docs

- **README**:
  - Architecture without RAG, and the measured latency numbers.
  - Continue config as v1 YAML (`roles`, chat `contextLength: 8192`, a realistic `maxTokens`,
    autocomplete tuning notes).
  - Supported Continue modes: Chat works; Agent-mode tools and image input do not.
- **`.env.example`**: remove `RAG_*` and `ALLOWED_MODELS`; add `FIM_KEEP_ALIVE`.
- **`context/v4-plan.md`**: add a "Removed in v5" status line, as `v2-plan.md` has.
- **Project memory notes**: update roadmap, v4 decisions, and FIM raw mode.

### Step 6 — Validate, then deploy on request

- Run the Code Simplifier and Build Validator agents in parallel.
- Deploy only on explicit go-ahead. First put both clones on the same commit, then run the
  existing compose command against the WSL-native clone.

---

## Outside This Repo (documented; operator's call)

- **FIM model A/B, the biggest single lever.**
  - `ollama pull qwen2.5-coder:3b-base` and `qwen2.5-coder:1.5b-base`, then set `FIM_MODEL`.
  - Compare them using the Step 4 log line.
  - Expect roughly 2.5× faster prefill for 3B. Base models should self-terminate FIM; verify. If
    they do, the `\n\n` stop becomes a safety net rather than the main mechanism.
- **Continue autocomplete options.**
  - `modelTimeout: 150` makes completions first-line-only on this hardware; raise it for
    multi-line.
  - `maxPromptTokens: 512` roughly halves prefill.
- **Chat model.** 14B is estimated at ~4–5 tok/s decode here (extrapolated from the measured 7B);
  consider `qwen2.5-coder:7b` for chat.
- **Ollama tuning.** `OLLAMA_FLASH_ATTENTION=1` plus `OLLAMA_KV_CACHE_TYPE=q8_0` is worth
  benchmarking; it's unverified on Vulkan.
- **Caddy.**
  - Forward only `/v1/*` and `/healthz`; 99.9% of proxied traffic is scanner noise.
  - Add `request_body max_size`, because FastAPI parses the body before auth runs.
- **Cleanup.**
  - Remove the now-ignored `ALLOWED_MODELS` and `RAG_*` from both clones' `.env`.
  - Prune old image layers.
  - Rotate `PROXY_AUTH_TOKEN` if the old image was ever pushed or shared.
- **Strategic.** With RAG gone, the proxy's value is FIM policy (stop, ceiling, keep-alive),
  auth, and model pinning. After a few weeks of logs, check whether it still earns its network
  hop, versus Continue's native `ollama` provider behind Caddy auth.

---

## Files

| Action | Files |
|---|---|
| Modify | `proxy/ollama_client.py`, `proxy/server.py`, `proxy/fim.py`, `settings.py`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `docker-compose.yml`, `README.md`, `.env.example`, `tests/test_server.py`, `tests/test_fim.py`, `context/v4-plan.md` |
| New | `.dockerignore` |
| Delete | `context/__init__.py`, `context/manager.py`, `context/rag_client.py`, `tests/test_context.py` |

---

## Verification

**Tests and the harness**
- `pytest` passes, including the cancellation test, and `ruff check` is clean.
- Re-run the disconnect harness against the new code. Every abort scenario must cancel upstream
  within ~50 ms, including during prefill, and the `\n\n` control must still stop.

**After deploy**
- `docker inspect` shows `RestartPolicy=unless-stopped`.
- The image has no `.env` or `.venv`, and its package versions match `uv.lock`.
- Live check: start a streaming FIM `curl` and press Ctrl-C mid-prefill.
  - With `OLLAMA_DEBUG=1`, Ollama's log should show the request cancelled. This also confirms that
    Docker Desktop's `host.docker.internal` forwarding propagates the close.
  - The proxy's log line should show `outcome=cancelled`.

---

## Risks

- **Prefill cancellation depends on uvicorn advertising ASGI 2.3.** At 2.4, Starlette detects
  disconnects only when `send` fails, which is after prefill. This is guarded by `uv.lock` pinning
  and the regression test.
- **Real Ollama probably checks for cancellation only between prefill batches** (512 tokens is
  about 3 s here). That's still far better than today; confirm live.
- **Non-streaming requests are not cancelled on disconnect.** Starlette doesn't watch for
  disconnects outside `StreamingResponse`. Continue always streams, so only curl and tests use this
  path.
- **Never add `BaseHTTPMiddleware`.** It routes `receive`/`send` through its own task group.
  Re-run the cancellation test if any middleware is added.
- **Look up `ollama_client.client` at call time.** A `from … import client` would keep the original
  object and bypass the test fixture.

---

## Implementation Notes

**Deviations from the plan above**
- **Dockerfile:** uses `uv sync --locked` rather than `--frozen`. It installs the same thing, but
  the build fails if `pyproject.toml` and `uv.lock` disagree. `--no-dev` was dropped: `dev` is an
  optional extra rather than a dependency group, so it was never installed and the flag did
  nothing.
- **Smaller code adjustments:**
  - `proxy/formatting.py` now builds chat chunks from text rather than from raw Ollama lines.
  - `format_completion_response` takes a `finish_reason`.
  - `_verify_token` is `async`, which avoids a threadpool hop per request.
- **Request log:** implemented as a `_logged` wrapper around the text generators, so streaming and
  non-streaming requests share it. Ollama's `done_reason` and timings reach it, and the
  non-streaming `finish_reason`, through a `done` dict that the generators fill in.

**Verification results**
- **Test suite:** 60 tests pass, and `ruff check` is clean. Before v5 there were 57 tests: the RAG
  tests went, and truncation, error, payload, logging, and cancellation tests were added.
- **Cancellation regression test:** `tests/test_cancellation.py` runs the real app under uvicorn
  and checks FIM and chat, before and after the first token. It was mutation-checked: with
  upstream reads moved to a detached task (reintroducing the leak), all four cases fail.
- **End-to-end disconnect harness** (fake Ollama over real HTTP, proxy as a uvicorn subprocess):

  | Scenario | Before v5 | After v5 |
  |---|---|---|
  | FIM abort after 1st chunk | 60/60 tokens generated | cancelled in 29 ms, 1 token |
  | FIM abort during prefill | 60/60 tokens generated | cancelled in 5 ms, during prefill |
  | Chat abort after 1st chunk | 60/60 tokens generated | cancelled in 29 ms, 1 token |
  | Chat abort during prefill | 60/60 tokens generated | cancelled in 7 ms, during prefill |
  | Control: `\n\n` stop | closes upstream in 29 ms | closes upstream in 26 ms |

- **Image:** a test build has no `.env`, `.git`, `.claude`, or tests. Its venv is its own, and it
  runs exactly the `uv.lock` versions (fastapi 0.136.3, starlette 1.2.1, uvicorn 0.48.0) on
  Python 3.12.

**Changes from review** (Code Simplifier and Build Validator agents, run in parallel; the
simplifier was report-only)
- **Upstream connections are now reused.** `_fim_text` and `_chat_text` no longer `return` on
  Ollama's done line. Reading to the end of the body lets the pool reuse the connection: 6 requests
  now share 1 connection, where each previously opened its own. The early `return` on a blank-line
  stop stays, because that one must close the socket to stop generation.
- **Simplifications:**
  - Removed the explicit `aclose()` calls (`ollama_client.stream`, `aclosing` in `_logged`). They
    were no-ops on every reachable path: `client.stream()`'s exit already closes the socket.
  - Inlined the single-caller `_sse_error`.
  - Collapsed the three completion formatters into `format_completion`.
- **Test robustness:** the cancellation test's server startup now has a 5 s deadline instead of an
  unbounded wait.
- **Final state:**
  - 60 tests pass three times under `-X dev`, asyncio debug mode, and ResourceWarning-as-error.
  - The cancellation test passed 40/40 in a 10× loop.
  - The image rebuilds and matches `uv.lock`.

**Known limitation (not fixed)**
- **Disconnect while blocked in `send()`:** if a client disconnects while Starlette is waiting in
  `send()`, the body generator chain isn't closed until garbage collection, and the upstream stays
  open until then. This requires more than 64 KiB buffered for a client that has stopped reading.
  FIM responses are under 10 KB, and Continue reads continuously, so this isn't reachable in
  practice. The fix would wrap `StreamingResponse.stream_response` in
  `aclosing(self.body_iterator)`, which couples the proxy to Starlette internals.
- **Unexpected upstream shapes** (well-formed JSON that isn't an object) surface as 500s. Real
  Ollama doesn't send them.

---

## Tuning Results (2026-10-05)

**Method**
- **Cases:** 12 cursor positions sampled from this repo's Python files, half at line start and half
  mid-line. The ground truth is the real rest of the line.
- **Prompt:** Continue's Qwen FIM format,
  `<|fim_prefix|>{prefix}<|fim_suffix|>{suffix}<|fim_middle|>`, with its 9 template stop tokens.
- **Payload:** the proxy's exact payload (raw, `num_ctx` 4096, temperature 0.1, `num_predict` 64).
- **Prompt sizes:** about 790 and about 430 real tokens. Prefix and suffix are pruned by whole
  lines, as Continue does.
- **Setup:** run against Ollama directly from a throwaway container. Pulling
  `qwen2.5-coder:3b-base` and `qwen2.5-coder:1.5b-base` for this was approved by the user.

**Results** (medians):

| Model | Prompt | First token | First line | Ollama freed | Prefill / decode | First-line exact / similarity | Self-terminated |
|---|---|---|---|---|---|---|---|
| 7b (instruct) | ~790 | 4131 ms | 4817 ms | 6985 ms | 206 / 10.1 tok/s | 2/12 · 0.67 | 6/12 |
| 7b (instruct) | ~430 | 2333 ms | 3126 ms | 8616 ms | 209 / 10.0 tok/s | 1/12 · 0.60 | 4/12 |
| 3b-base | ~790 | 2212 ms | 2579 ms | 3405 ms | 405 / 19.6 tok/s | 2/12 · 0.60 | 5/12 |
| 3b-base | ~430 | 1309 ms | 1833 ms | 2508 ms | 404 / 19.7 tok/s | 1/12 · 0.56 | 4/12 |
| 1.5b-base | ~790 | 1214 ms | 1352 ms | 2031 ms | 781 / 36.1 tok/s | 1/12 · 0.59 | 6/12 |
| 1.5b-base | ~430 | 722 ms | 963 ms | 1257 ms | 781 / 36.6 tok/s | 1/12 · 0.60 | 7/12 |

Cold loads were 31.2 s for 7b, 2.9 s for 3b, and 2.1 s for 1.5b.

**Conclusions**
- **Latency decides the model.** 1.5b-base shows its first line about 3.5× sooner than 7b and is
  the only model under the 1.5 s first-line target.
- **Accuracy doesn't separate them** at n=12. 7b's similarity edge comes from a single case
  (tests/test_server.py:262); without it, the paired difference against 1.5b is about 0.
- **Self-termination is about the same for base and instruct**, roughly half the time. The audit's
  claim that the Instruct variant caused the run-on is therefore withdrawn. The proxy's blank-line
  stop stays necessary for every model.
- **A ~430-token prompt nearly halves time to first token** with no measurable accuracy loss.

**Actions taken**
- The default `FIM_MODEL` is now `qwen2.5-coder:1.5b-base`; `3b-base` stays installed as a
  one-line step up.
- The README now recommends Continue `maxPromptTokens: 512` and `modelTimeout: 1500`.
- The Continue client config lives on whichever machine runs the editor. It is not on the proxy
  host, whose `~/.continue/config.yaml` has no models.

---

## Deployment (2026-10-05)

**Release**
- Commits `09de3a2` (v5), `e646abc` (non-ASCII token → 401), and `845c907` (FIM model).
- Deployed with the compose command.
- `/home/garret/Code` is a symlink to `/mnt/c/Users/Garret/Code`, so there is a single checkout and
  no clone sync is needed.

**Live checks**
- **Container:** running with `restart=unless-stopped`.
- **Image:** runs as `appuser`, and has no `.env` and no host `.venv`.
- **Auth:** `/healthz` returns 200. `/v1/models` returns 401 without a token, with a wrong token,
  and with a non-ASCII token (previously a 500). With a valid token it lists `qwen2.5-coder:14b`
  and `qwen2.5-coder:1.5b-base`.
- **FIM through the real path** (proxy → `host.docker.internal` → Ollama on Windows):
  - Cold request: a 1.9 s load and 304 ms of prefill for 231 tokens.
  - Warm request: Ollama's prompt cache cut prefill to 32 ms, so the first token arrived in 48 ms.
- **Cancellation against real Ollama:** a 2,495-token request aborted after 1 s gave
  `outcome=cancelled total_ms=1013`, and Ollama's own request log shows it ending at **1.0 s**,
  mid-prefill. The same request run to completion held Ollama's only slot for **5.5 s** (2.0 s
  prefill, then 128 tokens).
- **Cleanup:** the pre-v5 image (`4c3be59ea723`, which contained `.env`) and the `:v5-test` image
  are gone.

**Follow-ups outside this repo**
- **Caddy hardening on the Pi: done and verified on 2026-10-05.**
  - Only `/v1/*` and `/healthz` are forwarded. Probes get a 404 from Caddy and never reach the
    proxy.
  - `request_body max_size 4MB`: a 5 MB POST gets a 413 from Caddy.
  - SSE streams incrementally through Caddy.
  - A client abort through Caddy cancelled Ollama at 1.44 s, which confirms `flush_interval -1` is
    not set.
  - A 70-request burst got all 200s.
- **`.env` cleanup:** the ignored `ALLOWED_MODELS` / `RAG_*` keys were removed, and the container
  was recreated without them.
- **Still open:** applying the README's Continue config on the editor machine.

---

## Ollama Host Tuning: First Request After Idle (2026-10-05)

**Symptom**
- The proxy log showed `ttft_ms` far above Ollama's `prefill_ms` on the first request after a
  pause. One example was 2,263 ms against 38 ms, with the model already loaded (`load_ms=10`).
- Ollama's own request log matched, for instance 3.14 s against llama-server's 0.33 s.

**Diagnosis**
- `OLLAMA_DEBUG=1` showed that Ollama's scheduler handed the request over in 2 ms.
- The delay was inside llama-server: `prompt cache update took 2118.02 ms` while saving a
  0.493 MiB slot state. llama-server keeps a host-RAM prompt cache (`--cache-ram`, default
  8192 MiB), and it copies the current KV state there before switching to a new prompt.
- Pairing every save in that day's logs with the idle time before it:

| Idle before request | n | Median save | Max |
|---|---|---|---|
| < 5 s | 111 | 17 ms | 389 ms |
| 5–60 s | 2 | 1,091 ms | 1,091 ms |
| 1–10 min | 3 | 2,118 ms | 2,798 ms |

- The device-to-host copy is slow once the Vulkan iGPU has idled.

**Change**
- Ollama starts llama-server without `--cache-ram`. llama-server reads the option from
  `LLAMA_ARG_CACHE_RAM` (per `llama-server.exe --help`).
- Setting the Windows user environment variable `LLAMA_ARG_CACHE_RAM=0` and restarting Ollama
  makes llama-server log "prompt cache is disabled".

**Result: two separate effects.** Each row is one FIM request through the proxy, with the cache
off, after an idle gap. No other Ollama or proxy traffic hit any of these windows:

| Idle before request | Time to first token | Prefill |
|---|---|---|
| 3 min | 187 ms | 155 ms |
| 5 min | 217 ms | 188 ms |
| 5 min | 226 ms | 196 ms |
| ~19 min | 2,288 ms | 2,173 ms |
| ~52 min | 2,288 ms | 2,161 ms |

- **Short pauses (seconds up to at least 5 minutes): fixed.** With the cache on, the save alone
  cost 1.1 s after 5–60 s idle and about 2.1 s after 1–10 minutes. With it off, the first request
  takes about 0.2 s. Keep the setting.
- **Long breaks (about 20 minutes or more): not fixed.** The iGPU drops into a deep power state,
  and the first request still pays about 2.2 s once, now in prefill. Switching the Windows power
  mode from "Balanced" to "Best performance" might help; that is untested.
- During sustained requests, time to first token is about 60 ms. The first one or two requests
  after a pause add 150–400 ms while the iGPU clocks ramp up.
- *Correction:* the first version of this section (commit `7886213`) concluded, from a single
  3-minute sample, that the setting removed the idle delay entirely and that "the cost did not move
  into prefill". The repeated measurements above show that only holds for short pauses.

**Trade-offs and notes**
- llama-server no longer restores older prompts from RAM, for every model this Ollama serves. That
  includes graph-rag's.
- FIM keeps its main reuse, because the live slot cache covers consecutive edits in the same file.
- `OLLAMA_DEBUG` was removed again after the diagnosis.
