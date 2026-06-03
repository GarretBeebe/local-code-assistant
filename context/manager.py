from context import rag_client
from proxy.schemas import ChatMessage

_QUERY_TRUNCATE = 300


def _build_query(messages: list[ChatMessage]) -> str:
    last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    last_assistant = next((m.content for m in reversed(messages) if m.role == "assistant"), "")
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
        if chunk.get("filepath"):
            lines.append(f"# {chunk['filepath']}")
        lines.append(chunk["text"].strip())
        lines.append("")
    return "\n".join(lines)
