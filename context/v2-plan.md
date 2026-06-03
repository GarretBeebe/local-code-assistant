# v2 Implementation Plan: Context Injection

## Summary

Add a background file watcher + tree-sitter parser that maintains a SQLite symbol index, and
inject relevant symbol signatures into the system prompt of every chat completion request.
Port infrastructure from `rag-system` where specified. Do not touch FIM (`/v1/completions`).

---

## What changes vs. the v2 spec definition

These are deviations or clarifications the spec doesn't address:

1. **Watcher runs as a FastAPI background thread, not a separate process.**
   The spec says "watchdog watcher populates SQLite symbol store" but doesn't say where it lives.
   rag-system runs the watcher as a standalone Docker service. Here the proxy is a single service,
   so the watcher should start from FastAPI's `lifespan` handler as a daemon thread. No new
   systemd unit or Docker service needed.

2. **`common/` directory is required** but missing from the spec's directory tree.
   `paths.py`, `sqlite_store.py`, `config.py`, and `types.py` are utilities shared between
   `indexer/` and `context/`. Port them from rag-system into `common/` here.

3. **Drop `index_state` (index versioning).** rag-system's watcher calls `bump_index_version()`
   to notify the API server when the Qdrant collection changes. There is no Qdrant here — clients
   read directly from SQLite at request time. Drop this dependency entirely.

4. **Add `cleanup_stale` as a module-local function in `indexer/watcher.py`.**
   rag-system has `ingest/cleanup_stale.py`. We need the same behavior (remove fingerprints for
   paths no longer under any watched root) but don't need a full `ingest/` package. Inline the
   ~15-line function directly in `indexer/watcher.py`.

5. **FIM requests do NOT get context injected.**
   The spec implies this but never states it. Injecting a symbol block into a FIM prompt corrupts
   the `<|fim_prefix|>…<|fim_suffix|>…<|fim_middle|>` structure. Context injection must only fire
   for `/v1/chat/completions`, never `/v1/completions`.

6. **`watcher_config.yaml` needs code-specific ignore patterns.**
   The rag-system YAML blocks `.env`, `.toml`, `.yaml`, `.json`, `.sh`, `.cfg`, `.ini` from indexing
   (those are documents, not code). The code-assistant watcher should NOT block `.py`, `.ts`, `.js`
   (obviously), but should block the same infra/secret patterns. A dedicated
   `config/watcher_config.yaml` is created rather than reusing the rag-system file. The
   `required_mounts` block is dropped (this runs natively on host, not in Docker with bind mounts).

7. **`allowed_extensions` is narrower than rag-system's.**
   rag-system indexes `.md`, `.txt`, and many binary-adjacent extensions. The code assistant only
   needs files tree-sitter can parse: `.py`, `.ts`, `.tsx`, `.js`, `.jsx`. No markdown or prose.

---

## Files to Create

### `common/` — ported from rag-system unchanged

| File | Source | Notes |
|---|---|---|
| `common/__init__.py` | new | empty |
| `common/paths.py` | `rag-system/common/paths.py` | exact copy |
| `common/sqlite_store.py` | `rag-system/common/sqlite_store.py` | exact copy |
| `common/config.py` | `rag-system/common/config.py` | exact copy |
| `common/types.py` | `rag-system/common/types.py` | copy but drop `RagMode`; keep `IndexDecision` |

### `indexer/` — ported from rag-system, callback swapped

| File | Source | Notes |
|---|---|---|
| `indexer/__init__.py` | new | empty |
| `indexer/fingerprint_store.py` | `rag-system/indexer/fingerprint_store.py` | adapt `DB_PATH` to use local `DATA_DIR` from `settings.py` |
| `indexer/watcher.py` | `rag-system/indexer/watcher.py` | see detailed notes below |

**`indexer/watcher.py` adaptations:**
- Remove all rag-system imports (`ingest.*`, `common.index_state`, `common.qdrant`)
- Replace `index_file(p)` / `remove_indexed_document(path)` calls with:
  - `index_file(p)` → `context.symbol_store.upsert_symbols(path, context.parser.extract_symbols(p))`
  - `remove_indexed_document(path)` → `context.symbol_store.delete_symbols(path)`
- Remove `bump_index_version()` calls
- Add `cleanup_stale(accessible_roots)` as a module-local function (~15 lines: iterate
  `fingerprint_store.list_all_paths()`, delete entries whose paths aren't under any root)
- Remove the `main()` entrypoint; replace with `start_watcher(config: dict) -> None` that
  sets up and starts the `PollingObserver` in a daemon thread
- Keep: `validate_required_mounts` (with required_mounts section removed from config, this
  becomes a no-op but is harmless to keep), `WatchHandler`, `IndexWorker`, `initial_scan`,
  `sha256_file`, `_index_if_changed`, `_iter_watch_paths`, `_iter_schedulable_dirs`

### `context/` — new

| File | Notes |
|---|---|
| `context/__init__.py` | empty |
| `context/parser.py` | tree-sitter symbol extraction: class names, function/method signatures, top-level imports for `.py`, `.ts`, `.tsx`, `.js`, `.jsx` |
| `context/symbol_store.py` | SQLite CRUD: `init_db`, `upsert_symbols`, `delete_symbols`, `query_related` — uses `common.sqlite_store.SqliteStore` |
| `context/manager.py` | `build_context_prefix(messages) -> str`: looks at last user message for a file path hint, queries symbol_store for top-N related signatures, returns formatted prefix string |

**`context/symbol_store.py` schema:**
```sql
CREATE TABLE IF NOT EXISTS symbols (
    id        INTEGER PRIMARY KEY,
    filepath  TEXT NOT NULL,
    name      TEXT NOT NULL,
    signature TEXT NOT NULL,
    kind      TEXT NOT NULL,  -- 'class' | 'function' | 'method' | 'import'
    modified_at REAL NOT NULL
)
CREATE UNIQUE INDEX IF NOT EXISTS symbols_filepath_name ON symbols(filepath, name)
```

**`context/parser.py` approach:**
- Use `tree_sitter_languages.get_parser(lang)` for `python`, `typescript`, `javascript`
- Extract: top-level class names, function/method signatures (name + params, no body),
  top-level import statements
- Return `list[Symbol]` (a small dataclass: `filepath, name, signature, kind`)
- Keep it under ~60 lines; tree-sitter queries for Python and TS are the same shape

**`context/manager.py` approach:**
- At request time, scan `messages` for a file path in the most recent user content
  (pattern: anything ending in a known extension; look in the `content` string)
- Fall back to "recently modified" if no file path found
- Query `symbol_store.query_related(filepath, limit=MAX_CONTEXT_SYMS)` — returns symbols
  from that file + files that import it (or share a directory, as a simpler proxy)
- Format as the spec's system prompt prefix block and return as a string
- If symbol store is empty or unavailable, return `""` (graceful degradation — v1 behavior)

### `config/`

| File | Notes |
|---|---|
| `config/watcher_config.yaml` | Adapted from rag-system container YAML. Drop `required_mounts`. Use `~` paths (expand at runtime). Extensions: `.py`, `.ts`, `.tsx`, `.js`, `.jsx` only. Ignore patterns from rag-system minus the code extension blocks. |

---

## Files to Modify

### `settings.py`

Add:
```python
DATA_DIR                    = Path(os.environ.get("DATA_DIR", "data"))
SYMBOL_DB_PATH              = DATA_DIR / os.environ.get("SYMBOL_DB_NAME", "symbols.sqlite3")
FINGERPRINT_DB_PATH         = DATA_DIR / os.environ.get("FINGERPRINT_DB_NAME", "fingerprints.sqlite3")
CONFIG_PATH                 = Path(os.environ.get("CONFIG_PATH", "config/watcher_config.yaml"))
WATCHER_POLL_INTERVAL_SECONDS = _float("WATCHER_POLL_INTERVAL_SECONDS", "5.0")
ALLOWED_EXTENSIONS          = [".py", ".ts", ".tsx", ".js", ".jsx"]
MAX_CONTEXT_SYMS            = _int("MAX_CONTEXT_SYMS", "40")
```

### `proxy/server.py`

Two changes:

1. **Add FastAPI lifespan** to start the watcher on app startup:
```python
from contextlib import asynccontextmanager
from indexer import watcher
from common.config import load_yaml_config
import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_yaml_config(settings.CONFIG_PATH, allow_empty=True)
    if cfg:
        watcher.start_watcher(cfg)
    yield

app = FastAPI(lifespan=lifespan)
```

2. **Inject context in `_to_ollama_chat`**: call `context.manager.build_context_prefix(req.messages)`
   and, if non-empty, prepend it as a system message before forwarding to Ollama:
```python
from context import manager as ctx_manager

def _to_ollama_chat(req: ChatRequest) -> dict:
    messages = [m.model_dump() for m in req.messages]
    prefix = ctx_manager.build_context_prefix(req.messages)
    if prefix:
        messages = [{"role": "system", "content": prefix}] + messages
    payload: dict = {
        "model": req.model,
        "messages": messages,
        "stream": req.stream,
        "options": {"num_ctx": settings.CHAT_NUM_CTX},
    }
    ...
```

### `pyproject.toml`

Add to `dependencies`:
- `tree-sitter-languages`
- `watchdog`
- `pyyaml`

### `.env.example`

Add the new settings keys with comments.

---

## Directory Structure After v2

```
code-assistant/
├── common/
│   ├── __init__.py
│   ├── config.py          # YAML loader (rag-system port)
│   ├── paths.py           # path/extension filter utils (rag-system port)
│   ├── sqlite_store.py    # thread-local SQLite connection (rag-system port)
│   └── types.py           # IndexDecision enum (rag-system port, RagMode dropped)
├── context/
│   ├── __init__.py
│   ├── manager.py         # symbol lookup + context assembly
│   ├── parser.py          # tree-sitter extraction
│   └── symbol_store.py    # SQLite CRUD for symbols
├── indexer/
│   ├── __init__.py
│   ├── fingerprint_store.py  # SHA-256 dedup (rag-system port)
│   └── watcher.py            # PollingObserver + IndexWorker (rag-system port, callback swapped)
├── proxy/
│   ├── __init__.py
│   ├── fim.py
│   ├── formatting.py
│   ├── ollama_client.py
│   ├── schemas.py
│   └── server.py          # modified: lifespan + context injection
├── config/
│   └── watcher_config.yaml
├── context/               # spec/doc files (existing)
│   └── local-code-assistant.md
├── data/                  # runtime SQLite files (gitignored)
├── tests/
├── settings.py            # modified: new settings
├── pyproject.toml         # modified: new deps
└── .env.example           # modified: new keys
```

---

## Implementation Order

1. Port `common/` (paths, sqlite_store, config, types) — no logic, just copies
2. Create `context/symbol_store.py` — schema + CRUD, testable in isolation
3. Create `context/parser.py` — tree-sitter extraction, testable with fixture files
4. Port `indexer/fingerprint_store.py` — copy + adapt DATA_DIR
5. Port `indexer/watcher.py` — copy + swap callback + add cleanup_stale + add start_watcher
6. Create `context/manager.py` — depends on symbol_store
7. Update `settings.py`
8. Update `proxy/server.py` — lifespan + context injection
9. Create `config/watcher_config.yaml`
10. Update `pyproject.toml` and `.env.example`
11. Tests: symbol_store CRUD, parser extracts expected symbols, manager returns empty string when DB is empty (graceful degradation path)

---

## Open Questions

1. **Context lookup strategy in `manager.py`**: The spec says "current file's imports are resolved → related files identified". Resolving Python imports at query time is non-trivial. Simpler proxy: return symbols from (a) the current file if it's in the store, plus (b) all other files in the same directory, sorted by `modified_at` desc, limited to `MAX_CONTEXT_SYMS`. Good enough for v2; proper import resolution is v2.5+.

2. **File path extraction from messages**: Continue.dev doesn't always include a file path in the prompt. When it does, it's usually in the system context or in a code block header. How reliable is this? If unreliable, the fallback (most recently modified files) may be what fires most of the time.

3. **Watcher config path at runtime**: When running under systemd, the working directory may not be the project root. `CONFIG_PATH` should be an absolute path in the `.env` / service file. Worth noting in `.env.example`.
