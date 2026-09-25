from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Completion:
    text: str
    model: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class Provider(Protocol):
    """Anything that can answer a prompt. ``key`` identifies the model for caching."""

    @property
    def key(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str | None: ...

    def complete(self, system: str, user: str) -> Completion: ...
