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

| Milestone | Description | Status |
|---|---|---|
| v1 | Transparent proxy — forward Continue.dev → Ollama, prove the pipeline | 🔲 |
| v2 | Context injection — tree-sitter symbol extraction, watchdog file watcher, SQLite store | 🔲 |
| v3 | Dual-model routing — FIM → 7b, chat → 14b with tuned generation options | 🔲 |
| v4 | RAG bridge — optional Qdrant integration for document chunk retrieval | 🔲 |

## Deployment

**Linux (systemd):** Run uvicorn directly on the host. Ollama at `localhost:11434` — zero hops.

**Windows / cross-platform (Docker):** Single-service compose stack. Ollama stays on the host; reached via `host.docker.internal:11434`.

See [`context/local-code-assistant.md`](context/local-code-assistant.md) for full architecture notes and [`context/v1-transparent-proxy.md`](context/v1-transparent-proxy.md) for the v1 implementation spec.

## Continue.dev Config

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
  }
}
```

## Related Projects

- [`rag-system`](https://github.com/GarretBeebe/rag-system) — document RAG pipeline, shares infrastructure patterns (PollingObserver watcher, Ollama client, SQLite fingerprint store)
