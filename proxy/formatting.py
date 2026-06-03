import time
import uuid


def format_chat_chunk(line: dict, model: str, chat_id: str) -> dict | None:
    if line.get("done"):
        return None
    content = line.get("message", {}).get("content", "")
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def _completion_dict(text: str, model: str, completion_id: str, finish_reason: str | None) -> dict:
    return {
        "id": completion_id,
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"text": text, "index": 0, "finish_reason": finish_reason}],
    }


def format_completion_chunk(text: str, model: str, completion_id: str) -> dict:
    return _completion_dict(text, model, completion_id, None)


def format_completion_response(text: str, model: str) -> dict:
    return _completion_dict(text, model, f"cmpl-{uuid.uuid4().hex}", "stop")
