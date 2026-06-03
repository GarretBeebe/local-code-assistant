from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class _BaseRequest(BaseModel):
    model: str = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, gt=0)


class ChatRequest(_BaseRequest):
    messages: list[ChatMessage] = Field(min_length=1)


class CompletionRequest(_BaseRequest):
    prompt: str = Field(min_length=1)
    stop: list[str] | None = Field(None, max_length=4)
