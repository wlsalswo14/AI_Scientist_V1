from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from ..llm import ModelProvider
from ..prompts import load_prompt

T = TypeVar("T", bound=BaseModel)


class StructuredAgent(Generic[T]):
    role: str
    prompt_name: str
    output_schema: type[T]
    tools: list[dict[str, Any]] = []

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def run(self, payload: dict[str, Any], *, session_label: str) -> T:
        return await self.provider.generate(
            self.output_schema,
            instructions=load_prompt(self.prompt_name),
            payload=payload,
            tools=list(self.tools),
            session_label=session_label,
        )

