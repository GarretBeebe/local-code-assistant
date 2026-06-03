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
└──────────┬───────────┘
           │  POST /v1/retrieve (optional, RAG_BASE_URL)
           ▼
   rag-system (port 8000)
   Qdrant code index
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

**v2 — Symbol indexer ⛔ superseded**
- Superseded by v4. `rag-system` already continuously indexes `/watch/Code` via Qdrant, making a parallel watcher/tree-sitter/SQLite stack redundant.

**v3 — Dual-model routing ✅ complete**
- `/v1/completions` (FIM) explicitly routed to `qwen2.5-coder:7b`
- `/v1/chat/completions` explicitly routed to `qwen2.5-coder:14b`
- FIM-specific generation options: `temperature=0.1`, `num_predict=128`, `num_ctx=4096`

**v4 — RAG context injection ✅ complete**
- Chat completions query `rag-system`'s `/v1/retrieve` endpoint and inject the top-N code chunks as a system prompt prefix
- Query is built from the last user message + last assistant message (truncated to 300 chars each) so follow-up questions carry context
- Opt-in: disabled by default; enable by setting `RAG_BASE_URL` and `RAG_INTERNAL_TOKEN`
- FIM/autocomplete requests are unaffected; latency impact is capped by `RAG_TIMEOUT_SECONDS` (default 1.5s)

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

## RAG Context Injection (v4)

When co-located with [`rag-system`](https://github.com/GarretBeebe/rag-system), the proxy can retrieve relevant code chunks from its Qdrant index and inject them as a system prompt prefix on every chat completion.

**Enable it** by adding to `.env`:

```
RAG_BASE_URL=http://rag-api:8000          # rag-system container name (see networking step below)
RAG_INTERNAL_TOKEN=<shared secret>        # must match RAG_INTERNAL_TOKEN in rag-system's .env
RAG_CONTEXT_CHUNKS=3                      # chunks to inject (default: 3)
RAG_TIMEOUT_SECONDS=1.5                   # max wait before degrading gracefully (default: 1.5)
```

Generate the shared secret:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**Docker networking (manual step):** the two stacks run on separate Docker networks by default. After both are up, connect them once so the proxy can reach `rag-api` by container name:

```bash
docker network create rag-bridge
docker network connect rag-bridge rag-api
docker network connect rag-bridge local-code-assistant-proxy-1
```

This step is not required on every restart — the containers rejoin the network automatically as long as `rag-bridge` exists. To disconnect, run `docker network disconnect rag-bridge <container>`.

Leave `RAG_BASE_URL` empty (the default) to disable the feature entirely — the proxy behaves identically to v1/v3 with no dependency on rag-system.

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

- [`rag-system`](https://github.com/GarretBeebe/rag-system) — document and code RAG pipeline; v4 context injection queries its `/v1/retrieve` endpoint to inject relevant code chunks into chat completions
