import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager

import requests

import settings

_local = threading.local()


class OllamaError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


@contextmanager
def _handle_request_errors(timeout: float) -> Generator[None, None, None]:
    try:
        yield
    except requests.ConnectionError:
        raise OllamaError(502, f"Cannot connect to Ollama at {settings.OLLAMA_BASE_URL}")
    except requests.Timeout:
        raise OllamaError(504, f"Ollama request timed out after {timeout}s")
    except requests.HTTPError as e:
        raise OllamaError(502, f"Ollama returned HTTP {e.response.status_code}")


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def _url(path: str) -> str:
    return f"{settings.OLLAMA_BASE_URL}{path}"


def get_json(path: str, timeout: float = settings.OLLAMA_TIMEOUT_SECONDS) -> dict:
    with _handle_request_errors(timeout):
        resp = _session().get(_url(path), timeout=timeout)
        resp.raise_for_status()
        return resp.json()


def post_json(path: str, payload: dict, timeout: float = settings.OLLAMA_TIMEOUT_SECONDS) -> dict:
    with _handle_request_errors(timeout):
        resp = _session().post(_url(path), json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()


@contextmanager
def post_stream(path: str, payload: dict, timeout: float = settings.OLLAMA_TIMEOUT_SECONDS) -> Iterator[str]:
    with _handle_request_errors(timeout):
        resp = _session().post(_url(path), json=payload, stream=True, timeout=timeout)
        resp.raise_for_status()
    with resp:
        yield resp.iter_lines(decode_unicode=True)
