"""OpenAI-compatible chat completions provider.

Works against ``api.openai.com`` and any compatible gateway (Azure OpenAI,
vLLM, Ollama, LiteLLM, …) by overriding ``LAYA_STUDIO_OPENAI_BASE_URL``.
"""

from __future__ import annotations

import logging
from time import perf_counter

import httpx

from .base import LLMProvider, LLMResponse, LLMUnavailable

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    name = "openai"

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
            raise LLMUnavailable("LAYA_STUDIO_OPENAI_API_KEY is not set.")

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        started = perf_counter()
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise LLMUnavailable(f"OpenAI returned {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"OpenAI request failed: {exc}") from exc
        except ValueError as exc:
            raise LLMUnavailable(f"OpenAI returned malformed JSON: {exc}") from exc

        latency_ms = (perf_counter() - started) * 1000.0

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"Unexpected OpenAI response shape: {data}") from exc

        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(data.get("model", self._model)),
            latency_ms=latency_ms,
            usage=dict(data.get("usage", {}) or {}),
            raw={"id": data.get("id"), "finish_reason": _finish_reason(data)},
        )

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "available": self.available(),
            "model": self._model,
            "base_url": self._base_url,
        }


def _finish_reason(data: dict) -> str | None:
    try:
        return data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):  # pragma: no cover - defensive
        return None
