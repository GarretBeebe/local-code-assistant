import json
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
        raise OllamaError(502, "Cannot connect to upstream model server")
    except requests.Timeout:
        raise OllamaError(504, f"Ollama request timed out after {timeout}s")
    except requests.HTTPError as e:
        raise OllamaError(502, f"Ollama returned HTTP {e.response.status_code}")
    except json.JSONDecodeError:
        raise OllamaError(502, "malformed response from Ollama")


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


def _safe_lines(lines: Iterator[str]) -> Iterator[str]:
    try:
        yield from lines
    except requests.RequestException as e:
        raise OllamaError(502, f"Stream interrupted: {e}")


@contextmanager
def post_stream(path: str, payload: dict, timeout: float = settings.OLLAMA_TIMEOUT_SECONDS) -> Generator[Iterator[str], None, None]:
    with _handle_request_errors(timeout):
        resp = _session().post(_url(path), json=payload, stream=True, timeout=timeout)
    with resp:
        with _handle_request_errors(timeout):
            resp.raise_for_status()
        yield _safe_lines(resp.iter_lines(decode_unicode=True))
