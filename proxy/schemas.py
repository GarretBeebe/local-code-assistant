from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


class CompletionRequest(BaseModel):
    model: str
    prompt: str
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    stop: list[str] | None = None
