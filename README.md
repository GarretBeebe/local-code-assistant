# Local Code Assistant

A FastAPI proxy between [Continue.dev](https://continue.dev) and [Ollama](https://ollama.com). It
translates OpenAI-format requests and applies server-side generation policy for local code models.
Tab completion and chat run on local models: zero API costs, and no code leaves the machine.

**Hardware target:** GMKtec K16 (Ryzen 7 7735HS, 32GB RAM, Radeon 680M iGPU via Ollama's Vulkan backend)

**Models (defaults):**
- `qwen2.5-coder:1.5b-base` — FIM / tab autocomplete
- `qwen2.5-coder:14b` — chat, edit, apply

**FIM benchmark on the target (Ollama 0.35, Vulkan).** Medians over 12 cursor positions in this
repo, using Continue's prompt format:

| FIM model | Prompt | First token | First line | Prefill / decode | First-line similarity | Cold load |
|---|---|---|---|---|---|---|
| `qwen2.5-coder:7b` (instruct) | ~790 tok | 4.1 s | 4.8 s | 206 / 10 tok/s | 0.67 | 31 s |
| `qwen2.5-coder:3b-base` | ~790 tok | 2.2 s | 2.6 s | 405 / 20 tok/s | 0.60 | 3 s |
| `qwen2.5-coder:1.5b-base` | ~790 tok | 1.2 s | 1.35 s | 781 / 36 tok/s | 0.59 | 2 s |
| `qwen2.5-coder:1.5b-base` | ~430 tok | 0.7 s | 0.96 s | 781 / 37 tok/s | 0.60 | — |

- **Accuracy:** the differences are within noise at this sample size. 7B's edge comes from a
  single case.
- **Termination:** every model, base or instruct, runs on past the intended completion about half
  the time. The proxy's blank-line stop is therefore needed regardless of model.
- **Choice:** 1.5B is the only model that meets a sub-1.5 s first-line target.

Full method and results are in [`context/v5-audit-fixes.md`](context/v5-audit-fixes.md).

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
ollama pull qwen2.5-coder:14b        # chat
ollama pull qwen2.5-coder:1.5b-base  # FIM autocomplete
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
    model: qwen2.5-coder:1.5b-base  # the proxy serves FIM_MODEL; Continue picks its Qwen FIM template from this name
    apiBase: http://<proxy-host>:8080/v1
    apiKey: <PROXY_AUTH_TOKEN>
    roles: [autocomplete]
    autocompleteOptions:
      maxPromptTokens: 512  # default 1024; measured ~45% lower time to first token, no accuracy loss
      modelTimeout: 1500    # default 150 ms; see below
```

**Why `modelTimeout` matters on local hardware.** Two Continue behaviors depend on it:
- Once the first non-blank line has arrived after `modelTimeout` has passed, Continue shows only
  what it has.
- It aborts a request that is still streaming after 2.5 × `modelTimeout`.

With the 150 ms default, every suggestion is a single line, shown after about 1 s with the 1.5B
model. With `1500`, multi-line suggestions get time to finish: the median completion is done by
about 1.3 s. Use 150 if you'd rather have the fastest single lines.

**Supported Continue modes:**
- **Chat, Edit, and Apply** work.
- **Agent mode** gets no tools: the proxy doesn't forward `tools`, and it rejects `role: "tool"`
  messages.
- **Image inputs** are rejected.

## Tuning (no code changes)

- **FIM model.** If 1.5B suggestions feel weak, set `FIM_MODEL=qwen2.5-coder:3b-base`. It
  roughly doubles latency, and the benchmark showed no accuracy gain at its sample size. Compare
  `ttft_ms` and `total_ms` in the proxy log.
- **Chat model.** The 14B decodes at an estimated ~4–5 tok/s on this iGPU. `qwen2.5-coder:7b` is
  about twice as fast for chat.
- **Ollama: disable llama-server's host-RAM prompt cache (applied on the target).** Ollama runs
  each model in a llama-server process, which keeps an 8 GiB prompt cache in system RAM by
  default. Before each request with a new prompt, it copies the current cache state there.
  - *The problem:* on the Vulkan iGPU that copy takes 17 ms while the GPU is busy but 1–4 s after
    it has idled. So the first suggestion after any pause waited seconds before inference started.
  - *The fix:* set the Windows user environment variable `LLAMA_ARG_CACHE_RAM=0` (for example with
    `setx LLAMA_ARG_CACHE_RAM 0`). Then restart Ollama: Quit from the tray, then start it from the
    Start menu. Ollama doesn't pass `--cache-ram` itself, so llama-server picks the variable up, and
    its log reports "prompt cache is disabled".
  - *Result:* time to first token after 3 minutes idle dropped from 2,263 ms to 187 ms. During
    continuous typing it is about 60 ms.
  - *Trade-off:* older prompts are no longer restored from RAM, for every model this Ollama serves.
    The live cache still covers consecutive edits in the same file.
- **Ollama: flash attention.** `OLLAMA_FLASH_ATTENTION=1` with `OLLAMA_KV_CACHE_TYPE=q8_0` may
  help prefill and memory. It's unverified on Vulkan, so benchmark it.

See [`context/local-code-assistant.md`](context/local-code-assistant.md) for the original design
notes and [`context/v5-audit-fixes.md`](context/v5-audit-fixes.md) for the audit behind the
current design.
