import settings
from context import rag_client
from proxy.schemas import ChatMessage

_QUERY_TRUNCATE = 300


def _build_query(messages: list[ChatMessage]) -> str:
    last_user = last_assistant = ""
    for m in reversed(messages):
        if m.role == "user" and not last_user:
            last_user = m.content
        elif m.role == "assistant" and not last_assistant:
            last_assistant = m.content
        if last_user and last_assistant:
            break
    query = last_user[:_QUERY_TRUNCATE]
    if last_assistant:
        query += " " + last_assistant[:_QUERY_TRUNCATE]
    return query.strip()


def build_context_prefix(messages: list[ChatMessage]) -> str:
    query = _build_query(messages)
    if not query:
        return ""
    chunks = rag_client.retrieve_chunks(query)
    if not chunks:
        return ""
    lines = ["Relevant code from your project:\n"]
    for chunk in chunks:
        text = chunk.get("text", "").strip()
        if not text:
            continue
        filepath = chunk.get("filepath", "")
        if filepath and settings.RAG_FILEPATH_STRIP_PREFIX:
            filepath = filepath.removeprefix(settings.RAG_FILEPATH_STRIP_PREFIX)
        if filepath:
            lines.append(f"# {filepath}")
        lines.append(text)
        lines.append("")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
