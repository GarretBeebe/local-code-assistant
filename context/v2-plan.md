# v2 Implementation Plan: Context Injection

## Status: Superseded

**The original v2 plan is superseded.** rag-system is already indexing the code folder
(`/watch/Code`) continuously. Building a parallel watcher, fingerprint store, tree-sitter
parser, and SQLite symbol store in the code assistant would duplicate infrastructure that
already exists and is already running.

The injection mechanism originally planned for v2 (query local SQLite → format → inject into
system prompt) is functionally identical to what v4 was always going to do, just pointing at a
different data source. Since the data source (rag-system's Qdrant) already has the code
indexed, v2 collapses into v4.

**Skip v2. Implement v4 directly.** See `context/v4-plan.md`.

---

## What the original plan was (preserved for reference)

The original v2 plan proposed:
- A background file watcher (ported from `rag-system/indexer/watcher.py`) running as a FastAPI
  lifespan thread
- A tree-sitter parser extracting class names and function signatures from `.py`, `.ts`, `.js`
- A SQLite symbol store tracking extracted symbols per file
- A context manager injecting top-N symbol signatures into every chat completion system prompt

That plan was correct given the assumption that the code assistant would need its own indexer.
The assumption is false — rag-system handles indexing already.

**What v2 adds that v4 doesn't replace:** tree-sitter symbol signatures are structured
(just names and type signatures, no bodies), while rag-system's chunks are raw text. The
structured format is marginally cleaner for injection but not worth the infrastructure cost
given the alternative already exists.

---

## Spec deviations that remain valid for v4

These points from the original v2 plan carry forward unchanged:

- **FIM requests must NOT get context injected.** Injecting into FIM corrupts the
  `<|fim_prefix|>…<|fim_suffix|>…<|fim_middle|>` structure. Chat only.
- **Graceful degradation is required.** If rag-system is unavailable, the proxy must
  proceed without context rather than failing the request.
- **Context injection is a system prompt prefix.** The format is a `{"role": "system", ...}`
  message prepended to the messages array before forwarding to Ollama.
