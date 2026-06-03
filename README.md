# Local Code Assistant

A FastAPI proxy that sits between [Continue.dev](https://continue.dev) and [Ollama](https://ollama.com), injecting relevant code context before forwarding requests to a local code-specialized model. Tab completion and inline chat powered by local models — zero API costs, zero code leaving the machine.

**Hardware target:** GMKtec K16 (Ryzen 7 7735HS, 32GB RAM)

**Models:**
- `qwen2.5-coder:7b` Q4 — FIM / tab autocomplete (~35 tok/s)
- `qwen2.5-coder:14b` Q4 — chat, `/edit` commands (~18 tok/s)

## Architecture

```
Continue.dev (VS Code / JetBrains)
        │  OpenAI-format requests
        ▼
┌──────────────────────┐
│  Code Assistant      │  FastAPI, port 8080
│  Proxy               │
│  /v1/chat/completions├──► Ollama /api/chat
│  /v1/completions     ├──► Ollama /api/generate
│  /v1/models          ├──► Ollama /api/tags
└──────────────────────┘
        │
        ▼
   Ollama (host :11434)
```

## Build Milestones

**v1 — Transparent proxy ✅ complete**
- FastAPI proxy forwards Continue.dev → Ollama unchanged
- `/v1/chat/completions` (streaming + non-streaming)
- `/v1/completions` (FIM passthrough with qwen2.5-coder token formatting)
- `/v1/models` passthrough
- Continue.dev connected, autocomplete + chat working end-to-end

**v2 — Context injection 🔲**
- `tree-sitter-languages` parses Python, TypeScript, JavaScript
- watchdog watcher populates SQLite symbol store
- Context manager injects top-N relevant signatures into system prompt
- Success metric: fewer "undefined symbol" mistakes in chat completions

**v3 — Dual-model routing 🔲**
- `/v1/completions` (FIM) explicitly routed to `qwen2.5-coder:7b`
- `/v1/chat/completions` explicitly routed to `qwen2.5-coder:14b`
- FIM-specific generation options: `temperature=0.1`, `num_predict=128`, `num_ctx=4096`
- Success metric: autocomplete p50 latency < 1.5s

**v4 — RAG bridge 🔲**
- For chat requests, query Qdrant (`localhost:6333`) for relevant document chunks
- Append top-2 chunks to system prompt alongside symbol context
- Designed for co-location with [`rag-system`](https://github.com/GarretBeebe/rag-system)

## Quickstart (Docker)

```bash
cp .env.example .env        # see .env.example — set PROXY_AUTH_TOKEN if exposing outside localhost
docker compose up --build
curl localhost:8080/healthz  # {"status":"ok"}
```

Ollama stays on the host. The compose file reaches it via `host.docker.internal:11434` —
this works on Windows and macOS natively, and on Linux via the `extra_hosts: host-gateway` entry.

Pull the models if you haven't already:

```bash
ollama pull qwen2.5-coder:14b   # chat / edit
ollama pull qwen2.5-coder:7b    # FIM autocomplete
```

## Testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tests run without Ollama — all external calls are mocked.

## Deployment

**Docker (Windows / cross-platform):** Single-service compose stack. Ollama stays on the host; reached via `host.docker.internal:11434`.

**Linux (systemd):** Run uvicorn directly on the host for zero-hop Ollama access:
```bash
pip install -e .
uvicorn proxy.server:app --port 8080
```

See [`context/local-code-assistant.md`](context/local-code-assistant.md) for full architecture notes and [`context/v1-transparent-proxy.md`](context/v1-transparent-proxy.md) for the v1 implementation spec.

## Continue.dev Config

Set `apiKey` to your `PROXY_AUTH_TOKEN` value (or any string if auth is disabled).

```json
{
  "models": [
    {
      "title": "Local (Chat)",
      "provider": "openai",
      "model": "qwen2.5-coder:14b",
      "apiBase": "http://localhost:8080/v1",
      "apiKey": "<PROXY_AUTH_TOKEN>"
    }
  ],
  "tabAutocompleteModel": {
    "title": "Local (FIM)",
    "provider": "openai",
    "model": "qwen2.5-coder:7b",
    "apiBase": "http://localhost:8080/v1",
    "apiKey": "<PROXY_AUTH_TOKEN>"
  }
}
```

## Related Projects

- [`rag-system`](https://github.com/GarretBeebe/rag-system) — document RAG pipeline, shares infrastructure patterns (PollingObserver watcher, Ollama client, SQLite fingerprint store)
