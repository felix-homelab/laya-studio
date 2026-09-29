"""Provider contract for the hybrid generative fallback.

Laya handles the cheap, high-volume, well-calibrated majority of decisions. A
generative model is only consulted on the *middle band* (by default 60–90%
confidence), where spending tokens actually buys accuracy. This module defines
what a provider must look like; concrete providers live alongside it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


class LLMUnavailable(RuntimeError):
    """No usable provider is configured, or the provider rejected the call."""


@dataclass(slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(abc.ABC):
    """Minimal synchronous chat interface."""

    name: str = "unknown"

    @abc.abstractmethod
    def available(self) -> bool:
        """True when credentials/base URL are present."""

    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Run one completion and return the assistant text.

        Raises :class:`LLMUnavailable` when the provider is not configured, the
        call fails, or the response cannot be read. Implementations translate
        their own transport and parsing errors into it rather than letting them
        escape; callers treat it as "could not verify" and fail closed.
        """

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "available": self.available()}
