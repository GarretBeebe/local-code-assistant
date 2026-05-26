# Local Code Assistant Server

## Concept

A FastAPI proxy that sits between Continue.dev (VS Code/JetBrains editor plugin) and Ollama,
injecting relevant code context before forwarding requests to the model. You get tab completion
and inline chat powered by a local code-specialized model — zero API costs, zero code leaving
the machine.

**Hardware target**: GMKtec K16 (Ryzen 7 7735HS, 16 threads, 32GB LPDDR5)
**Model targets**:
- `qwen2.5-coder:7b` Q4 (~4.5GB, ~35 tok/s) — FIM / tab autocomplete
- `qwen2.5-coder:14b` Q4 (~9GB, ~18 tok/s) — chat, `/edit` commands

Both models fit in RAM simultaneously alongside the rag-system's `qwen2.5:14b` if needed,
though in practice only one heavyweight model runs at a time.

---

## Architecture

```
Editor (VS Code / JetBrains)
  Continue.dev plugin
         │
         │  OpenAI-format requests
         ▼
┌─────────────────────────────────┐
│   Code Assistant Proxy          │  FastAPI, port 8080
│                                 │
│  POST /v1/chat/completions  ────┼──► inject symbol context ──► Ollama /api/chat
│  POST /v1/completions (FIM) ────┼──► reformat FIM tokens   ──► Ollama /api/generate
│  GET  /v1/models            ────┼──────────────────────────► Ollama /api/tags
│                                 │
│  ┌──────────────────────────┐  │
│  │     Context Manager      │  │
│  │  - open-file symbol cache│  │
│  │  - tree-sitter parsing   │  │
│  │  - recency-ranked index  │  │
│  └──────────────────────────┘  │
│                                 │
│  ┌──────────────────────────┐  │
│  │     File Watcher         │  │
│  │  watchdog, YAML config   │  │
│  └──────────────────────────┘  │
└─────────────────┬───────────────┘
                  │
                  ▼
           Ollama (host :11434)
```

**Deployment model**: Two supported paths:

- **Systemd** (Linux primary target — GMKtec K16): uvicorn runs directly on the host,
  Ollama at `localhost:11434` — zero hops, lowest latency.
- **Docker** (Windows / cross-platform): single-service compose stack. Ollama stays on the
  host; reached via `host.docker.internal:11434` (`extra_hosts: host.docker.internal:host-gateway`
  makes this work on Linux Docker too). On Linux you can also use `network_mode: host` +
  `OLLAMA_BASE_URL=http://localhost:11434` to skip NAT entirely.

The earlier concern about file watching in Docker (inotify unreliability on Windows NTFS
bind mounts) is resolved: the watcher will use `PollingObserver` from the `watchdog` library,
which works on all platforms regardless of how directories are mounted. This is the same
approach used by the `rag-system` project.

---

## The Non-Obvious Part: Context Injection

Continue.dev by default sends just the text around your cursor. The proxy intercepts each
request, enriches it with a compact symbol index of your project, and forwards the augmented
prompt to Ollama.

**What gets injected** (system prompt prefix):

```
You are a code assistant. Relevant symbols from this project:

# src/auth/middleware.py
class AuthMiddleware:
def validate_token(token: str) -> User | None:
def require_role(role: str) -> Callable:

# src/models/user.py
class User(BaseModel):
class UserRole(Enum):
```

**What does NOT get injected**: full function bodies. Signatures only. This keeps the injected
context small enough to leave the model's working context window for the actual task.

**How it's built**:
1. `watchdog` observer watches your configured project directories
2. On each changed file, `tree-sitter` parses it and extracts: class names, function/method
   signatures, top-level imports
3. Extracted symbols are stored in a SQLite table (path, symbol_name, signature, modified_at)
4. At request time: current file's imports are resolved → related files identified →
   top-N most recently modified related files selected → their signatures injected

---

## FIM Translation

Continue.dev sends OpenAI `/v1/completions` requests in this shape:

```json
{
  "model": "qwen2.5-coder:7b",
  "prompt": "<|fim_prefix|>def calculate_total(\n    items: list\n<|fim_suffix|>\n    return total<|fim_middle|>",
  "max_tokens": 64,
  "stream": true
}
```

The proxy translates to Ollama's `/api/generate`:

```json
{
  "model": "qwen2.5-coder:7b",
  "prompt": "<|fim_prefix|>def calculate_total(\n    items: list\n<|fim_suffix|>\n    return total<|fim_middle|>",
  "stream": true,
  "options": { "num_ctx": 4096, "num_predict": 64, "temperature": 0.1 }
}
```

FIM tokens pass through unchanged — `qwen2.5-coder` understands them natively. The proxy's
job is format translation and setting appropriate generation options for autocomplete
(low temperature, capped token count, smaller context window than chat).

---

## Tech Stack

Follows rag-system conventions throughout.

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Consistent with rag-system |
| Web framework | FastAPI | Same pattern, async |
| Ollama client | `requests.Session` direct HTTP | Same as rag-system `ollama_client.py` |
| Code parsing | `tree-sitter-languages` | Pre-built grammars, no compile step |
| Symbol store | SQLite (same `sqlite_store.py` pattern) | No extra services |
| File watching | `watchdog` + YAML config | Same as rag-system `indexer/watcher.py` |
| Config | `settings.py` with `os.environ.get()` | Same pattern, no pydantic-settings |
| Editor client | Continue.dev | OpenAI-compat, works with any local model |
| Service (Linux) | systemd unit | Always-on, auto-restart, zero Ollama hop |
| Service (cross-platform) | Docker + docker-compose | Windows / macOS support, PollingObserver for file watching |

---

## Directory Structure

```
code-assistant/
├── proxy/
│   ├── server.py          # FastAPI app, route handlers
│   ├── ollama_client.py   # requests.Session wrapper (same pattern as rag-system)
│   ├── fim.py             # FIM request translation logic
│   └── schemas.py         # Pydantic OpenAI-compat request/response models
├── context/
│   ├── manager.py         # Symbol lookup + context assembly at request time
│   ├── parser.py          # tree-sitter symbol extraction
│   └── symbol_store.py    # SQLite CRUD for symbol index
├── indexer/
│   ├── watcher.py         # watchdog observer (mirrors rag-system pattern)
│   └── fingerprint_store.py  # SHA-256 dedup (can share rag-system's if co-located)
├── config/
│   └── watcher_config.yaml   # Watch paths, ignore patterns, same YAML schema
├── settings.py            # All config via os.environ.get()
├── pyproject.toml
├── code-assistant.service # systemd unit file
├── docker-compose.yml     # Alternative deployment
└── .env.example
```

---

## Continue.dev Config

`~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "Local (Chat)",
      "provider": "openai",
      "model": "qwen2.5-coder:14b",
      "apiBase": "http://localhost:8080/v1",
      "apiKey": "local"
    }
  ],
  "tabAutocompleteModel": {
    "title": "Local (FIM)",
    "provider": "openai",
    "model": "qwen2.5-coder:7b",
    "apiBase": "http://localhost:8080/v1",
    "apiKey": "local"
  },
  "contextProviders": []
}
```

The proxy handles context injection server-side. Continue.dev's built-in context providers
are disabled to avoid doubling up.

---

## Key Settings

```python
# settings.py
OLLAMA_BASE_URL   = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL        = os.environ.get("CHAT_MODEL", "qwen2.5-coder:14b")
FIM_MODEL         = os.environ.get("FIM_MODEL", "qwen2.5-coder:7b")
CHAT_NUM_CTX      = int(os.environ.get("CHAT_NUM_CTX", "8192"))
FIM_NUM_CTX       = int(os.environ.get("FIM_NUM_CTX", "4096"))
FIM_MAX_TOKENS    = int(os.environ.get("FIM_MAX_TOKENS", "128"))
PROXY_PORT        = int(os.environ.get("PROXY_PORT", "8080"))
SYMBOL_DB_PATH    = os.environ.get("SYMBOL_DB_PATH", "data/symbols.sqlite3")
MAX_CONTEXT_SYMS  = int(os.environ.get("MAX_CONTEXT_SYMS", "40"))
```

---

## Build Milestones

**v1 — Transparent proxy (prove the pipeline)**
- FastAPI proxy forwards Continue.dev → Ollama unchanged
- `/v1/chat/completions` (streaming + non-streaming)
- `/v1/completions` (FIM passthrough)
- `/v1/models` passthrough
- Continue.dev connected, autocomplete + chat working end-to-end
- systemd service running

**v2 — Context injection**
- `tree-sitter-languages` parses Python, TypeScript, JavaScript (the common cases)
- watchdog watcher populates SQLite symbol store
- Context manager injects top-N relevant signatures into system prompt
- Measure: does the model make fewer "undefined symbol" mistakes?
- **Watcher implementation**: port directly from `rag-system/indexer/watcher.py`. Reuse:
  `PollingObserver` setup, `validate_required_mounts`, `WatchHandler` skeleton,
  `fingerprint_store.py` (SHA-256 dedup), `common/paths.py` (extension + ignore filtering).
  Only the ingest callback changes: tree-sitter symbol extraction + SQLite instead of
  vector embedding + Qdrant. Container YAML path pattern (`/watch/Code` mapped via volume mount)
  ports over directly from `rag-system/config/watcher_config.container.yaml`.

**v3 — Dual-model routing**
- `/v1/completions` (FIM) routes to `qwen2.5-coder:7b`
- `/v1/chat/completions` routes to `qwen2.5-coder:14b`
- FIM-specific options: `temperature=0.1`, `num_predict=128`, `num_ctx=4096`
- Measure autocomplete latency — target p50 < 1.5s

**v4 — RAG bridge (optional)**
- For chat requests, query the rag-system's Qdrant (`localhost:6333`) for relevant
  document chunks (e.g., README, architecture docs, API references)
- Append top-2 chunks to system prompt alongside symbols
- Only worthwhile if co-located with rag-system on the same machine (it is)

---

## Latency Budget

Autocomplete (FIM, 7b model):
- Tokenization + context lookup: ~5ms
- Ollama generation (128 tokens, 35 tok/s): ~3.5s worst case, ~0.3s for 10-token completions
- **Practical feel**: good for line completions; multi-line completions will have noticeable lag

Chat (14b model, /edit):
- Context lookup + assembly: ~10ms
- Ollama generation: ~18 tok/s — acceptable for chat, not for autocomplete
- This is why dual-model routing (v3) matters

---

## Compared to Existing Agents

| | Life Ops Agent | RAG System | Code Assistant |
|---|---|---|---|
| Privacy value | High (email) | High (docs) | Medium (code) |
| Daily usefulness | Medium | Medium | High (every coding session) |
| Local model fit | Good (summarization) | Good (Q&A) | Good (code models are strong at 7-14B) |
| Complexity | Low | High | Medium |
| Shares infrastructure | Gmail/Calendar APIs | Qdrant + Ollama | Ollama (+ optionally Qdrant in v4) |

The code assistant is the only one of the three with a **synchronous latency requirement** —
autocomplete must respond in under ~2s or it feels broken. That constraint drives most of the
design decisions above (7b FIM model, capped token counts, systemd over Docker).
