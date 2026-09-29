"""Anthropic Messages API provider."""

from __future__ import annotations

import logging
from time import perf_counter

import httpx

from .base import LLMProvider, LLMResponse, LLMUnavailable

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def available(self) -> bool:
        return bool(self._api_key)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMUnavailable("LAYA_STUDIO_ANTHROPIC_API_KEY is not set.")

        payload: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        started = perf_counter()
        try:
            response = httpx.post(
                f"{self._base_url}/messages",
                json=payload,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            # Error bodies can echo the request, prompt included, and this message
            # is logged and persisted with the run — so the body stays at debug level.
            logger.debug("Anthropic error body: %s", exc.response.text[:500])
            raise LLMUnavailable(f"Anthropic returned HTTP {exc.response.status_code}.") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Anthropic request failed: {exc}") from exc
        except ValueError as exc:
            raise LLMUnavailable(f"Anthropic returned malformed JSON: {exc}") from exc

        latency_ms = (perf_counter() - started) * 1000.0

        # Errors name the shape, not the body: the body carries model output
        # derived from the state, and these messages are logged and persisted.
        blocks = data.get("content") if isinstance(data, dict) else None
        if not isinstance(blocks, list):
            raise LLMUnavailable(f"Unexpected Anthropic response shape: {_shape(data)}")

        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            piece = block.get("text", "")
            if not isinstance(piece, str):
                raise LLMUnavailable(
                    f"Anthropic returned a text block whose text is {type(piece).__name__}."
                )
            parts.append(piece)
        text = "".join(parts)

        usage = data.get("usage")
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(data.get("model") or self._model),
            latency_ms=latency_ms,
            usage=dict(usage) if isinstance(usage, dict) else {},
            raw={"id": data.get("id"), "stop_reason": data.get("stop_reason")},
        )

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "available": self.available(),
            "model": self._model,
            "base_url": self._base_url,
        }


def _shape(data: object) -> str:
    """Describe a payload by its type and top-level keys, never its contents."""
    if isinstance(data, dict):
        return f"object with keys {sorted(str(key) for key in data)}"
    return type(data).__name__
