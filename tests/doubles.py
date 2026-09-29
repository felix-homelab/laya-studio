"""Test doubles. **Not shipped, and not reachable from configuration.**

Laya is the only engine in the application. The suite still needs something
deterministic and fast to run against — spinning up three checkpoints for every
assertion would make the tests pointless — so the substitution happens through
``app.inference.set_engine``, which exists for exactly this and is called from
nowhere in ``app/``.

Two doubles, for two different jobs:

:class:`ScriptedEngine`
    Returns *exactly* what a test told it to. Use this whenever the test cares
    about a value: it removes any possibility of asserting the double's behaviour
    instead of the code under test.

:class:`LexicalEngine`
    A deterministic keyword scorer, for the tests that need plausible answers for
    *arbitrary* questions — bulk uploads, A/B comparisons, calibration runs —
    where scripting every question would be noise.

``tests/test_real_laya.py`` covers the adapter against the actual runtime.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.inference.base import (
    DecisionEngine,
    DecisionResult,
    content_tokens,
    normalize_result,
    tokens_match,
)
from app.inference.routing import routing_decision

GAIN = 4.0
NOUL_GAIN = 6.0
NOUL_PRIOR = 0.35


def _overlap(surface_tokens: set[str], state_tokens: set[str]) -> int:
    return sum(
        1
        for token in surface_tokens
        if any(tokens_match(token, other) for other in state_tokens)
    )


def _surface_score(state_tokens: set[str], state_lower: str, surface: str) -> float:
    surface_tokens = set(content_tokens(surface))
    if not surface_tokens:
        return 0.0
    coverage = _overlap(surface_tokens, state_tokens) / len(surface_tokens)
    phrase = surface.replace("_", " ").replace("-", " ").strip().lower()
    bonus = 0.6 if len(phrase) > 3 and phrase in state_lower else 0.0
    return coverage + bonus


def _softmax(logits: list[float]) -> list[float]:
    import math

    if not logits:  # a question with no options: callers fall back to neutral values
        return []
    peak = max(logits)
    exponents = [math.exp(value - peak) for value in logits]
    total = sum(exponents)
    return [value / total for value in exponents]


class ScriptedEngine(DecisionEngine):
    """Returns precisely what the test specified.

    ``answers`` is keyed by question name. Anything not listed falls back to
    ``default``, or to a neutral answer if that is ``None``.
    """

    name = "scripted"

    def __init__(
        self,
        *,
        answers: dict[str, dict[str, Any]] | None = None,
        default: dict[str, Any] | None = None,
        checkpoint: str = "english",
        raises: Exception | None = None,
    ) -> None:
        self._answers = answers or {}
        self._default = default
        self._checkpoint = checkpoint
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def predict(self, state, questions, model=None) -> DecisionResult:
        started = perf_counter()
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self._raises is not None:
            raise self._raises

        answers: dict[str, dict[str, Any]] = {}
        for name, definition in questions.items():
            if name in self._answers:
                answers[name] = dict(self._answers[name])
            elif self._default is not None:
                answers[name] = dict(self._default)
            else:
                answers[name] = _neutral_answer(definition)

        routing = routing_decision(state, model or self._checkpoint)
        return normalize_result(
            {"answers": answers, "routing": routing},
            engine_name=self.name,
            latency_ms=(perf_counter() - started) * 1000.0,
            default_model=self._checkpoint,
        )

    def route(self, state, questions=None) -> dict[str, Any]:
        return routing_decision(state)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": True, "device": "cpu", "preloaded": True}


def _neutral_answer(definition: dict[str, Any]) -> dict[str, Any]:
    """A maximally uninformative answer of the right shape."""
    primitive = definition.get("type", "choice")
    if primitive == "choice":
        criteria = definition.get("criteria") or {}
        labels = list(criteria) or ["a", "b"]
        share = 1.0 / len(labels)
        return {
            "type": "choice",
            "choice": labels[0],
            "probabilities": {label: share for label in labels},
            "confidence": 0.0,
            "action": {"act_probability": 0.5},
        }
    if primitive == "score":
        criteria = definition.get("criteria") or ["low", "high"]
        share = 1.0 / len(criteria)
        return {
            "type": "score",
            "score": (len(criteria) - 1) / 2,
            "legend": {str(i): str(level) for i, level in enumerate(criteria)},
            "probabilities": {str(i): share for i in range(len(criteria))},
            "confidence": 0.0,
            "action": {"act_probability": 0.5},
        }
    return {"type": "noul", "noul": 0.5, "confidence": 0.5, "action": {"act_probability": 0.5}}


class LexicalEngine(DecisionEngine):
    """Deterministic keyword scorer for tests that need answers for any question.

    Deliberately simple and *not* a model: it scores an option by how much of its
    wording appears in the state. It produces uniform probabilities when nothing
    matches, which is what makes it useful — a test can rely on "no evidence" and
    "strong evidence" producing different bands.
    """

    name = "lexical"

    def predict(self, state, questions, model=None) -> DecisionResult:
        started = perf_counter()
        from app.inference.base import flatten_state

        text = flatten_state(state)
        state_tokens = set(content_tokens(text))
        state_lower = text.lower()

        answers: dict[str, dict[str, Any]] = {}
        for name, definition in questions.items():
            primitive = definition.get("type", "choice")
            if primitive == "choice":
                criteria = definition.get("criteria") or {}
                labels = [str(label) for label in criteria]
                scores = [
                    _surface_score(state_tokens, state_lower, f"{label} {criteria[label]}")
                    for label in labels
                ]
                probabilities = _softmax([score * GAIN for score in scores])
                best = max(range(len(labels)), key=lambda i: (scores[i], -i)) if labels else 0
                answers[name] = {
                    "type": "choice",
                    "choice": labels[best] if labels else None,
                    "probabilities": {
                        labels[i]: round(probabilities[i], 6) for i in range(len(labels))
                    },
                    "confidence": round(probabilities[best], 6) if labels else 0.0,
                    "action": {"act_probability": 1.0},
                }
            elif primitive == "score":
                criteria = definition.get("criteria") or []
                levels = [str(level) for level in criteria]
                scores = [_surface_score(state_tokens, state_lower, level) for level in levels]
                probabilities = _softmax([score * GAIN for score in scores])
                answers[name] = {
                    "type": "score",
                    "score": round(
                        sum(i * p for i, p in enumerate(probabilities)), 4
                    ),
                    "legend": {str(i): level for i, level in enumerate(levels)},
                    "probabilities": {
                        str(i): round(probabilities[i], 6) for i in range(len(levels))
                    },
                    "confidence": round(max(probabilities), 6) if probabilities else 0.0,
                    "action": {"act_probability": 1.0},
                }
            else:
                criteria = definition.get("criteria")
                positives: list[str] = []
                negatives: list[str] = []
                if isinstance(criteria, dict) and criteria:
                    for label, description in criteria.items():
                        surface = f"{label} {description}"
                        if str(label).strip().lower() in ("no", "false", "0", "negative"):
                            negatives.append(surface)
                        else:
                            positives.append(surface)
                elif definition.get("instructions"):
                    positives.append(str(definition["instructions"]))

                pos = max(
                    (_surface_score(state_tokens, state_lower, s) for s in positives), default=0.0
                )
                neg = max(
                    (_surface_score(state_tokens, state_lower, s) for s in negatives), default=0.0
                )
                import math

                log_odds = NOUL_GAIN * (pos - neg) + math.log(NOUL_PRIOR / (1 - NOUL_PRIOR))
                probability = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, log_odds))))
                answers[name] = {
                    "type": "noul",
                    "noul": round(probability, 6),
                    "confidence": round(max(probability, 1 - probability), 6),
                    "action": {"act_probability": 1.0},
                }

        routing = routing_decision(state, model)
        return normalize_result(
            {"answers": answers, "routing": routing},
            engine_name=self.name,
            latency_ms=(perf_counter() - started) * 1000.0,
            default_model=str(routing.get("model", "english")),
        )

    def route(self, state, questions=None) -> dict[str, Any]:
        return routing_decision(state)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": True, "device": "cpu", "preloaded": True}
