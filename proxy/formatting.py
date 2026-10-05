import time


def format_chat_chunk(content: str, model: str, chat_id: str) -> dict:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def format_completion(text: str, model: str, completion_id: str, finish_reason: str | None = None) -> dict:
    return {
        "id": completion_id,
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"text": text, "index": 0, "finish_reason": finish_reason}],
    }
