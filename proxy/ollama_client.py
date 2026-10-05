import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2

import settings

# Set the timeout explicitly: httpx2's 5 s default is shorter than a cold model load.
client = httpx2.AsyncClient(
    base_url=settings.OLLAMA_BASE_URL,
    timeout=httpx2.Timeout(settings.OLLAMA_TIMEOUT_SECONDS, connect=5.0),
)


class OllamaError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


async def _parse_lines(resp: httpx2.Response) -> AsyncIterator[dict]:
    async for line in resp.aiter_lines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            raise OllamaError(502, "malformed response from Ollama") from None
        if "error" in data:  # Ollama reports failures after the 200 as an NDJSON line
            raise OllamaError(502, f"Ollama error: {data['error']}")
        yield data


@asynccontextmanager
async def stream(path: str, payload: dict) -> AsyncIterator[AsyncIterator[dict]]:
    """Stream Ollama's NDJSON lines as dicts, the final done line included.

    Leaving the block, whether returning early or cancelled because the client disconnected,
    closes the upstream connection, which is what makes Ollama stop generating.
    """
    try:
        async with client.stream("POST", path, json={**payload, "stream": True}) as resp:
            if resp.is_error:
                await resp.aread()
                raise OllamaError(
                    502, f"Ollama returned HTTP {resp.status_code}: {resp.text.strip()[:200]}"
                )
            yield _parse_lines(resp)
    except httpx2.TimeoutException:
        raise OllamaError(
            504, f"Ollama request timed out after {settings.OLLAMA_TIMEOUT_SECONDS}s"
        ) from None
    except httpx2.HTTPError as e:
        raise OllamaError(502, f"Upstream model server error: {type(e).__name__}") from None
