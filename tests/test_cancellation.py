"""A client disconnect must cancel the upstream Ollama request, before and after the first token.

Continue aborts most autocomplete requests, and Ollama serves one request at a time, so an
abandoned generation delays the next completion. This runs the real app under uvicorn because
cancellation depends on the server: uvicorn advertises ASGI spec 2.3, so Starlette watches for
http.disconnect while streaming. TestClient can't disconnect mid-request.
"""
import asyncio
import threading
import time

import httpx2
import pytest
import uvicorn

import proxy.ollama_client as ollama_client
import settings
from proxy.server import app

FIRST_LINE = b'{"response": "x", "message": {"content": "x"}}\n'  # valid for generate and chat


class BlockingOllama:
    """Upstream that never finishes: optionally sends one line, then blocks until cancelled."""

    def __init__(self, send_first_line: bool) -> None:
        self.send_first_line = send_first_line
        self.entered = threading.Event()
        self.cancelled = threading.Event()

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.entered.set()
        if not self.send_first_line:
            await self._block()  # prefill: Ollama sends no response headers before the first token
        return httpx2.Response(200, content=self._body())

    async def _body(self):
        yield FIRST_LINE
        await self._block()

    async def _block(self) -> None:
        try:
            await asyncio.sleep(30)
        finally:
            self.cancelled.set()


@pytest.fixture(scope="module")
def base_url():
    config = uvicorn.Config(app, host="127.0.0.1", port=0, ws="none", log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert _eventually(lambda: server.started, timeout=5), "uvicorn did not start"
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.parametrize("phase", ["prefill", "decode"])
@pytest.mark.parametrize(
    "path, body",
    [
        ("/v1/completions", {"model": "m", "prompt": "p", "stream": True}),
        ("/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
    ],
    ids=["fim", "chat"],
)
def test_client_disconnect_cancels_upstream(base_url, monkeypatch, caplog, path, body, phase):
    upstream = BlockingOllama(send_first_line=phase == "decode")
    monkeypatch.setattr(settings, "PROXY_AUTH_TOKEN", None)
    transport = httpx2.MockTransport(upstream)
    monkeypatch.setattr(ollama_client, "client", httpx2.AsyncClient(base_url="http://ollama", transport=transport))

    with caplog.at_level("INFO", logger="proxy"):
        with httpx2.Client(timeout=5) as http, http.stream("POST", base_url + path, json=body) as resp:
            if phase == "decode":
                next(resp.iter_lines())  # the first token arrived
            else:
                assert upstream.entered.wait(timeout=2)
        # Leaving the block closes the connection, as Continue's abort does.

        assert upstream.cancelled.wait(timeout=1), "upstream request kept running after the client left"
        assert _eventually(lambda: any("outcome=cancelled" in r.getMessage() for r in caplog.records))


def _eventually(predicate, timeout: float = 1.0) -> bool:
    """Poll a condition that another thread makes true (server startup, the cancellation log)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True
