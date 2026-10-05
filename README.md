# Local Code Assistant

A FastAPI proxy between [Continue.dev](https://continue.dev) and [Ollama](https://ollama.com). It
translates OpenAI-format requests and applies server-side generation policy for local code models.
Tab completion and chat run on local models: zero API costs, and no code leaves the machine.

**Hardware target:** GMKtec K16 (Ryzen 7 7735HS, 32GB RAM, Radeon 680M iGPU via Ollama's Vulkan backend)

**Models (defaults):**
- `qwen2.5-coder:7b` — FIM / tab autocomplete
- `qwen2.5-coder:14b` — chat, edit, apply

**Measured on the target (Ollama 0.35, Vulkan):**
- **7B throughput:** ~158 tok/s prefill, ~9 tok/s decode.
- **Cold load:** ~26 s for a 7B model.
- **Autocomplete prefill:** Continue's default 1024-token prompt costs ~6 s before the first token.

These numbers drive most of the tuning advice below; see
[`context/v5-audit-fixes.md`](context/v5-audit-fixes.md).

## Architecture

```
Continue.dev (VS Code / JetBrains)
        │  OpenAI-format requests
        ▼
┌───────────────────────┐
│  Code Assistant Proxy │  FastAPI, port 8080
│  /v1/chat/completions ├──► Ollama /api/chat      (CHAT_MODEL)
│  /v1/completions      ├──► Ollama /api/generate  (FIM_MODEL, raw FIM prompt)
│  /v1/models           │    static: the two configured models
└───────────────────────┘
```

What the proxy adds on top of Ollama:
- **Model routing:** chat goes to `CHAT_MODEL` and completions go to `FIM_MODEL`, whatever model
  the client names.
- **Cancellation:** when the client disconnects, the proxy cancels the upstream request, even
  mid-prefill. Continue aborts most autocomplete requests, and Ollama serves one request at a
  time, so an abandoned generation would otherwise delay the next completion.
- **FIM policy:**
  - Completions stop at the first blank line after real content, because `qwen2.5-coder`
    doesn't reliably self-terminate FIM.
  - `FIM_MAX_TOKENS` is a ceiling on `max_tokens`.
  - `FIM_KEEP_ALIVE` keeps the FIM model loaded past Ollama's 5-minute idle unload.
- **Bearer auth** (`PROXY_AUTH_TOKEN`) on all `/v1/*` routes.
- **One log line per completion**, including cancelled ones:
  - outcome (`done` / `stopped` / `cancelled` / `error`);
  - time to first token and total time;
  - request shape;
  - Ollama's load/prefill/eval timings when generation finishes.

  Prompt text is never logged.

## Build Milestones

- **v1 — Transparent proxy ✅:** Continue.dev → Ollama format translation, streaming and
  non-streaming.
- **v2 — Symbol indexer ⛔ superseded:** by v4.
- **v3 — Dual-model routing ✅:** FIM → 7B, chat → 14B, plus FIM generation options.
- **v4 — RAG context injection 🗑️ removed in v5:** never enabled in production. On this hardware
  it would have added an estimated 10–20 s of chat prefill per turn. The code is in git
  (`3920a24`).
- **v5 — Audit fixes ✅:**
  - upstream cancellation on client disconnect;
  - FIM keep-alive and `max_tokens` ceiling;
  - a corrected blank-line stop (CRLF, indented blank lines);
  - surfaced Ollama errors and request logging;
  - a reproducible Docker build from `uv.lock`.

## Quickstart (Docker)

```bash
cp .env.example .env        # set PROXY_AUTH_TOKEN if exposing outside localhost
docker compose up -d --build
curl localhost:8080/healthz  # {"status":"ok"}
```

Ollama stays on the host. The compose file reaches it via `host.docker.internal:11434`, which
works on Windows and macOS natively, and on Linux via the `extra_hosts: host-gateway` entry. The
container restarts automatically (`restart: unless-stopped`). The image installs exactly what
`uv.lock` pins, and `.env` is supplied at runtime, never baked into the image.

Pull the models if you haven't already:

```bash
ollama pull qwen2.5-coder:14b   # chat
ollama pull qwen2.5-coder:7b    # FIM autocomplete
```

All settings are documented in [`.env.example`](.env.example).

## Testing

```bash
uv sync --extra dev
uv run pytest
```

Tests run without Ollama: the upstream is an in-memory `httpx2.MockTransport`.
`tests/test_cancellation.py` starts the real app under uvicorn on an ephemeral port, to prove that
a client disconnect cancels the upstream request before and after the first token.

## Deployment

**Docker (Windows / cross-platform):** single-service compose stack (see Quickstart).

**Linux (systemd):** run uvicorn directly on the host for zero-hop Ollama access:
```bash
uv sync
.venv/bin/uvicorn proxy.server:app --port 8080
```

## Continue.dev Config

Continue's `config.yaml` (schema v1) assigns models with `roles`. A `tabAutocompleteModel` block
is ignored in this schema.

```yaml
name: Local Code Assistant
version: 1.0.0
schema: v1
models:
  - name: Local (Chat)
    provider: openai
    model: qwen2.5-coder:14b
    apiBase: http://<proxy-host>:8080/v1
    apiKey: <PROXY_AUTH_TOKEN>      # any string if auth is disabled
    roles: [chat, edit, apply]
    defaultCompletionOptions:
      contextLength: 8192   # match CHAT_NUM_CTX; Continue otherwise assumes 32768 and Ollama silently truncates
      maxTokens: 2048
  - name: Local (Autocomplete)
    provider: openai
    model: qwen2.5-coder:7b         # Continue picks its FIM prompt template from this name
    apiBase: http://<proxy-host>:8080/v1
    apiKey: <PROXY_AUTH_TOKEN>
    roles: [autocomplete]
    autocompleteOptions:
      maxPromptTokens: 512  # default 1024; prefill cost scales with prompt size
      modelTimeout: 2000    # default 150 ms; see below
```

**Why `modelTimeout` matters on local hardware.** Two Continue behaviors depend on it:
- Once the first non-blank line has arrived after `modelTimeout` has passed, Continue shows only
  what it has.
- It aborts a request that is still streaming after 2.5 × `modelTimeout`.

The 150 ms default therefore means single-line suggestions here. Raise it if you want multi-line
completions, and lower it if you'd rather have faster single lines.

**Supported Continue modes:**
- **Chat, Edit, and Apply** work.
- **Agent mode** gets no tools: the proxy doesn't forward `tools`, and it rejects `role: "tool"`
  messages.
- **Image inputs** are rejected.

## Tuning Experiments (no code changes)

- **FIM model.** `qwen2.5-coder:7b` is the *Instruct* finetune. Qwen trains its **base** models
  for FIM.
  - To try one, run `ollama pull qwen2.5-coder:3b-base` (or `1.5b-base`) and set `FIM_MODEL`.
  - Compare `ttft_ms` and `total_ms` in the proxy log. A 3B model prefills about 2.5× faster here.
- **Chat model.** The 14B decodes at an estimated ~4–5 tok/s on this iGPU. `qwen2.5-coder:7b` is
  about twice as fast for chat.
- **Ollama.** `OLLAMA_FLASH_ATTENTION=1` with `OLLAMA_KV_CACHE_TYPE=q8_0` may help prefill and
  memory. It's unverified on Vulkan, so benchmark it.

See [`context/local-code-assistant.md`](context/local-code-assistant.md) for the original design
notes and [`context/v5-audit-fixes.md`](context/v5-audit-fixes.md) for the audit behind the
current design.
