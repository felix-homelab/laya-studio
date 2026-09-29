"""Provider selection and the secondary-verification prompt.

This is the "smart hybrid fallback" half of the hybrid pipeline: when Laya lands
in the middle confidence band, we ask a generative model to adjudicate the
*same* question over the *same* state. We ask only for the label (and a one-line
rationale), never for free-form reasoning about the workflow, because the whole
point of the hybrid is to spend as few tokens as possible.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from ..config import Settings, get_settings
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider, LLMUnavailable
from .openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)

VERIFY_SYSTEM_PROMPT = (
    "You are a precise classification judge. You decide one question at a time "
    "and answer with a single JSON object. Never add commentary outside the JSON."
)


@dataclass(slots=True)
class VerificationVerdict:
    """Outcome of one secondary-verification call."""

    #: "ok" | "skipped" | "failed"
    status: str = "skipped"
    choice: str | None = None
    agrees: bool | None = None
    rationale: str = ""
    provider: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "choice": self.choice,
            "agrees": self.agrees,
            "rationale": self.rationale,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 3),
            "error": self.error,
            "usage": self.usage,
        }


def build_provider(settings: Settings | None = None) -> LLMProvider | None:
    """Instantiate the configured provider, or ``None`` when disabled."""
    settings = settings or get_settings()

    if settings.llm_provider == "openai":
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout,
        )
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            base_url=settings.anthropic_base_url,
            timeout=settings.llm_timeout,
        )
    return None


# (signature, provider) as one tuple, so a concurrent rebuild can never pair a
# provider with another configuration's signature.
_cached: tuple[tuple, LLMProvider | None] | None = None


def get_provider(settings: Settings | None = None) -> LLMProvider | None:
    """Return the cached provider for the current configuration."""
    global _cached

    settings = settings or get_settings()
    # Everything build_provider() reads, so a rotated key or a changed timeout
    # rebuilds the provider instead of reusing one holding the old value.
    signature = (
        settings.llm_provider,
        settings.openai_model,
        settings.openai_base_url,
        settings.anthropic_model,
        settings.anthropic_base_url,
        settings.openai_api_key,
        settings.anthropic_api_key,
        settings.llm_timeout,
    )
    cached = _cached
    if cached is not None and cached[0] == signature:
        return cached[1]

    provider = build_provider(settings)
    _cached = (signature, provider)
    return provider


def reset_provider_cache() -> None:
    global _cached
    _cached = None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first balanced ``{...}`` object out of a model response.

    Models wrap JSON in prose or fences even when told not to, so scan for the
    first balanced object rather than trusting the whole response to parse.
    """
    if not text:
        return None

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        # Resume *after* the malformed object, not inside it: an
                        # inner object is a fragment, not the verdict.
                        start = index
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def build_verification_prompt(
    *,
    question: str,
    instructions: str,
    state_text: str,
    candidates: list[str],
    candidate_choice: str | None,
    candidate_confidence: float | None,
) -> str:
    """Compose the adjudication prompt for one question."""
    option_lines = "\n".join(f"- {option}" for option in candidates) or "- (no options recorded)"
    confidence_text = (
        f"{candidate_confidence:.1%}"
        if isinstance(candidate_confidence, (int, float))
        and not isinstance(candidate_confidence, bool)
        else "unknown"
    )

    return (
        f"Question: {question}\n"
        f"Instructions: {instructions or 'Choose the single best option.'}\n\n"
        f"Allowed options:\n{option_lines}\n\n"
        f"State to classify:\n---\n{state_text}\n---\n\n"
        f"A fast decision model answered {candidate_choice!r} with {confidence_text} "
        f"confidence. That is in the uncertain band, so it needs adjudication.\n\n"
        "Decide the single best option yourself. Reply with ONLY a JSON object:\n"
        '{"choice": "<one of the allowed options>", "agrees": <true|false>, '
        '"rationale": "<one short sentence>"}'
    )


def verify_decision(
    *,
    question: str,
    instructions: str,
    state_text: str,
    candidates: list[str],
    candidate_choice: str | None,
    candidate_confidence: float | None,
    settings: Settings | None = None,
) -> VerificationVerdict:
    """Ask the generative fallback to adjudicate an uncertain Laya decision.

    Never raises: a missing provider, a network failure or unparseable output all
    degrade to a ``status`` other than ``"ok"`` so the workflow can fall through
    to human review instead of erroring.
    """
    settings = settings or get_settings()
    provider = get_provider(settings)

    if provider is None:
        return VerificationVerdict(
            status="skipped",
            error=f"No LLM provider configured (LAYA_STUDIO_LLM_PROVIDER={settings.llm_provider}).",
        )

    if not provider.available():
        return VerificationVerdict(
            status="skipped",
            provider=provider.name,
            error=f"{provider.name} selected but no API key is set.",
        )

    prompt = build_verification_prompt(
        question=question,
        instructions=instructions,
        state_text=state_text,
        candidates=candidates,
        candidate_choice=candidate_choice,
        candidate_confidence=candidate_confidence,
    )

    started = perf_counter()
    try:
        response = provider.complete(
            prompt,
            system=VERIFY_SYSTEM_PROMPT,
            max_tokens=settings.llm_max_tokens,
            temperature=0.0,
        )
    except LLMUnavailable as exc:
        logger.warning("LLM verification failed: %s", exc)
        return VerificationVerdict(
            status="failed",
            provider=provider.name,
            latency_ms=(perf_counter() - started) * 1000.0,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - verification must never break a run
        logger.exception("Unexpected LLM verification error")
        return VerificationVerdict(
            status="failed",
            provider=provider.name,
            latency_ms=(perf_counter() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )

    payload = extract_json_object(response.text)
    if payload is None:
        return VerificationVerdict(
            status="failed",
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=response.usage,
            error="Model response did not contain a JSON object.",
        )

    raw_choice = payload.get("choice")
    choice = str(raw_choice).strip() if raw_choice is not None else None

    # Only accept the verdict if it names one of the options we offered.
    matched: str | None = None
    if choice:
        for option in candidates:
            if option.strip().lower() == choice.lower():
                matched = option
                break

    rationale = str(payload.get("rationale", ""))[:500]

    # A label we did not offer is absence of evidence, and fails closed like any
    # other unusable reply (ADR 0003) — it must never reach auto-execution.
    if matched is None:
        return VerificationVerdict(
            status="failed",
            rationale=rationale,
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=response.usage,
            error="Model chose a label outside the offered options.",
        )

    # Agreement is a fact about the two labels, so compute it whenever both are
    # known; the model's own ``agrees`` field is only a fallback. Otherwise a reply
    # of {"choice": "B", "agrees": true} against a candidate "A" would auto-execute
    # B while claiming to confirm A.
    if candidate_choice is not None:
        agrees = matched.lower() == candidate_choice.strip().lower()
    else:
        agrees = payload.get("agrees")
        if not isinstance(agrees, bool):
            agrees = False

    return VerificationVerdict(
        status="ok",
        choice=matched,
        agrees=agrees,
        rationale=rationale,
        provider=response.provider,
        model=response.model,
        latency_ms=response.latency_ms,
        usage=response.usage,
    )
