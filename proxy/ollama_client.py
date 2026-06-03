import threading
from collections.abc import Iterator
from contextlib import contextmanager

import requests

import settings

_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def _url(path: str) -> str:
    return f"{settings.OLLAMA_BASE_URL}{path}"


def get_json(path: str, timeout: float = settings.OLLAMA_TIMEOUT_SECONDS) -> dict:
    resp = _session().get(_url(path), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def post_json(path: str, payload: dict, timeout: float = settings.OLLAMA_TIMEOUT_SECONDS) -> dict:
    resp = _session().post(_url(path), json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


@contextmanager
def post_stream(path: str, payload: dict, timeout: float = settings.OLLAMA_TIMEOUT_SECONDS) -> Iterator[str]:
    with _session().post(_url(path), json=payload, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        yield resp.iter_lines(decode_unicode=True)
