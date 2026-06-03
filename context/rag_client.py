from typing import TypedDict

import requests

import settings

_session = requests.Session()


class Chunk(TypedDict):
    text: str
    filepath: str


def retrieve_chunks(query: str) -> list[Chunk]:
    """Return chunks from rag-system, or [] if unavailable or not configured."""
    if not settings.RAG_BASE_URL or not settings.RAG_INTERNAL_TOKEN:
        return []
    try:
        resp = _session.post(
            f"{settings.RAG_BASE_URL}/v1/retrieve",
            json={"query": query, "limit": settings.RAG_CONTEXT_CHUNKS},
            headers={"Authorization": f"Bearer {settings.RAG_INTERNAL_TOKEN}"},
            timeout=settings.RAG_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json().get("chunks", [])
    except Exception:
        return []
