from typing import Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class CompletionRequest(BaseModel):
    model: str
    prompt: str
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stop: Optional[list[str]] = None
    suffix: Optional[str] = None
