"""The inference contract, and the normalisation every answer flows through.

One engine implements :class:`DecisionEngine` — :class:`LayaEngine`, wrapping
``laya.Router``. The protocol exists so the test suite can substitute a double
(``tests/doubles.py``, installed via ``app.inference.set_engine``) without the
application knowing, and so a future backend has a shape to implement.

The contract mirrors Laya's own answer payload::

    result["answers"][question] -> {"choice"|"score"|"noul": ..., "confidence": ...}
    result["routing"]           -> {"model": "english"|"multilingual"|..., "reason": ...}

Everything downstream (visualizer, bulk metrics, workflows, calibration) reads
only the normalised :class:`DecisionResult`, never engine internals.
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import PRIMITIVES

#: Laya fits a separate temperature per ``(question type, option-count bucket)``,
#: using the buckets ``2``, ``3-5``, ``6-10`` and ``11+``. The shipped English
#: checkpoint's ``choice:11+`` temperature is 0.1006 — below Laya's own clamp floor
#: of 0.5 — so the runtime clamps it and warns:
#:
#:     "this checkpoint ships temperatures outside [0.5, 5] which would distort
#:      confidence; clamping choice:11+=0.1006. Treat confidence from the affected
#:      buckets as uncalibrated."
#:
#: Which means: an 11-option ``choice`` question produces a confidence **nobody
#: has calibrated**, and the confidence ladder gates on exactly that number. The
#: linter warns at 11 rather than at some hand-waved accuracy cliff.
UNCALIBRATED_CHOICE_OPTIONS = 11

#: Legacy alias kept because the name reads better at call sites that only care
#: about size.
MAX_CHOICE_OPTIONS = UNCALIBRATED_CHOICE_OPTIONS

#: Laya's own published position on its base checkpoints. Surfaced in the UI so
#: nobody reads a first-run confidence as an accuracy claim.
BASE_CHECKPOINT_NOTE = (
    "Laya is a fast base to specialise, not a zero-shot decision engine. "
    "Fine-tune and calibrate per domain before trusting the numbers."
)

# ---------------------------------------------------------------------------
# Shared text normalisation
#
# Used by the schema linter, to catch `noul` criteria that share vocabulary. Kept
# here so "what counts as the same word" has exactly one definition.
# ---------------------------------------------------------------------------

WORD_RE = re.compile(r"\w+", re.UNICODE)

STOPWORDS = frozenset(
    """
    the a an of to in for on at is are was were be been being this that these
    those and or not with you your we our it its if then than as by from will
    would can could should shall may might must do does did have has had me my
    i he she they them their his her our us about into over under again more
    most some such only own same so too very just please
    """.split()
)

#: Two tokens count as the same word when one is a prefix of the other and both
#: are at least this long. A deliberately crude stand-in for a stemmer:
#: billed/billing/bill, charges/charge, refunds/refund.
MIN_PREFIX_MATCH = 4


def content_tokens(text: str) -> list[str]:
    """Lowercase content words with stopwords and pure digits removed."""
    tokens: list[str] = []
    for match in WORD_RE.finditer(text):
        token = match.group(0).lower()
        if len(token) <= 2 or token.isdigit() or token in STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < MIN_PREFIX_MATCH:
        return False
    return left.startswith(right) or right.startswith(left)


class EngineUnavailable(RuntimeError):
    """Raised when the Laya runtime cannot be constructed.

    Permanent for this process: something is missing or broken, and retrying will
    not help.
    """


class EngineNotReady(RuntimeError):
    """Raised while the engine is still being constructed.

    Temporary. The checkpoints may be downloading, which takes minutes on a cold
    cache. Carries ``retry_after`` so an HTTP caller can be told when to come back.
    """

    def __init__(self, message: str, *, retry_after: int = 5) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class QuestionTooLarge(ValueError):
    """A question's options do not fit Laya's per-question token budget.

    Laya renders every option into a fixed budget and truncates the *whole*
    sequence to ``max_len``. When the options cannot fit, it raises rather than
    silently dropping them — which is the right call, but its message is terse.
    This type carries the remedy.
    """

    @property
    def remedy(self) -> str:
        return (
            "Reduce the number of options, shorten their descriptions, or shortlist "
            "them. Raising LAYA_STUDIO_LAYA_HEAD_MAX_LEN only helps if "
            "LAYA_STUDIO_LAYA_MAX_LEN is raised with it — the budget must fit inside "
            "the sequence limit."
        )


@dataclass(slots=True)
class DecisionResult:
    """Normalised result of one Laya forward pass."""

    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    engine_name: str = "unknown"
    model: str = "unknown"

    @property
    def question_results(self) -> list[dict[str, Any]]:
        return build_question_results(self.answers)


class DecisionEngine(abc.ABC):
    """The inference contract.

    One implementation ships — :class:`~app.inference.laya_engine.LayaEngine`.
    The protocol exists so the test suite can substitute a double without the
    application knowing, and so a future backend has a shape to implement.
    """

    #: Short identifier persisted on every evaluation (``laya``).
    name: str = "unknown"

    @abc.abstractmethod
    def predict(
        self,
        state: dict[str, Any] | str,
        questions: dict[str, dict[str, Any]],
        model: str | None = None,
    ) -> DecisionResult:
        """Score ``questions`` against ``state`` in a single forward pass."""

    def route(
        self,
        state: dict[str, Any] | str,
        questions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Explain which checkpoint *would* serve this state, without predicting."""
        return {"model": "unknown", "reason": "routing introspection unavailable"}

    def describe(self) -> dict[str, Any]:
        """Health/diagnostic payload for ``/api/health``."""
        return {"name": self.name, "available": True}


# =============================================================================
# Normalisation helpers — tolerant of Laya's evolving result schema
# =============================================================================


def flatten_state(state: dict[str, Any] | str) -> str:
    """Render a state as searchable text.

    Laya accepts a plain string or an arbitrary dict (email, ticket, JSON). Both
    collapse to text here so the review queue and analytics can display and
    search them uniformly.
    """
    if isinstance(state, str):
        return state
    if not isinstance(state, dict):
        return str(state)

    parts: list[str] = []
    # Prefer the conventional free-text fields first so the most informative
    # content leads the rendering.
    preferred = ("subject", "title", "summary", "body", "text", "message", "content")
    seen: set[str] = set()
    for key in preferred:
        if key in state:
            value = state[key]
            seen.add(key)
            if value is None or value == "":
                continue
            parts.append(f"{key}: {_stringify(value)}")
    for key, value in state.items():
        if key in seen or value is None or value == "":
            continue
        parts.append(f"{key}: {_stringify(value)}")
    return "\n".join(parts)


def _stringify(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}={_stringify(v)}" for k, v in value.items())
    return str(value)


def extract_probabilities(answer: dict[str, Any]) -> dict[str, float]:
    """Pull the per-option distribution out of a raw answer.

    Laya names this field differently across versions and primitives, so probe
    the known spellings rather than assuming one.
    """
    for key in ("probabilities", "probability", "distribution", "options", "scores"):
        candidate = answer.get(key)
        if isinstance(candidate, dict) and candidate:
            out: dict[str, float] = {}
            for label, value in candidate.items():
                try:
                    out[str(label)] = float(value)
                except (TypeError, ValueError):
                    continue
            if out:
                return out
        if isinstance(candidate, list) and candidate:
            out = {}
            for index, item in enumerate(candidate):
                if isinstance(item, dict):
                    label = item.get("label") or item.get("choice") or item.get("level")
                    value = item.get("probability", item.get("p", item.get("score")))
                    if label is None or value is None:
                        continue
                    try:
                        out[str(label)] = float(value)
                    except (TypeError, ValueError):
                        continue
                else:
                    try:
                        out[str(index)] = float(item)
                    except (TypeError, ValueError):
                        continue
            if out:
                return out
    return {}


def extract_choice(answer: dict[str, Any]) -> str | None:
    value = answer.get("choice")
    if value is None:
        value = answer.get("top_choice") or answer.get("label")
    if value is None:
        probabilities = extract_probabilities(answer)
        if probabilities:
            return max(probabilities, key=probabilities.__getitem__)
        return None
    return str(value)


def extract_confidence(answer: dict[str, Any]) -> float | None:
    for key in ("confidence", "probability", "p"):
        value = answer.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _clamp01(float(value))
    probabilities = extract_probabilities(answer)
    if probabilities:
        return _clamp01(max(probabilities.values()))
    return None


def extract_noul(answer: dict[str, Any]) -> float | None:
    value = answer.get("noul")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _clamp01(float(value))
    return None


def extract_score(answer: dict[str, Any]) -> float | None:
    """The expected level on the ordinal rubric.

    Laya returns this directly (``sum(index * p)`` over the temperature-scaled
    distribution), so it is preferred. The recomputation below only runs when a
    payload omits it.
    """
    value = answer.get("score")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    probabilities = extract_probabilities(answer)
    if probabilities:
        # Recover the expected level from the distribution when ``score`` is
        # absent but the per-level probabilities are present.
        total = sum(probabilities.values())
        if total <= 0:
            return None
        # Levels may be non-numeric labels; fall back to ordinal position.
        numeric: dict[str, float] = {}
        for index, label in enumerate(sorted(probabilities)):
            try:
                numeric[label] = float(label)
            except ValueError:
                numeric[label] = float(index)
        return sum(numeric[label] * probability for label, probability in probabilities.items()) / total
    return None


def extract_act_probability(answer: dict[str, Any]) -> float | None:
    """Laya's own trained act-vs-escalate recommendation.

    The checkpoint carries a decision head that emits ``action.act_probability``
    alongside the answer — a learned second opinion on whether this is a case to
    act on automatically or to escalate. Laya Studio's confidence ladder is driven
    by ``confidence`` (as the brief specifies), but surfacing the model's own
    recommendation next to it is more useful than hiding it.
    """
    action = answer.get("action")
    if isinstance(action, dict):
        value = action.get("act_probability")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _clamp01(float(value))
    return None


def extract_legend(answer: dict[str, Any]) -> dict[str, str]:
    """The index -> level mapping Laya returns for ``score`` questions.

    ``score`` answers key ``probabilities`` by index ("0", "1", ...) and carry a
    separate ``legend``. Without it a breakdown renders as bare numbers instead of
    the rubric the author actually wrote.
    """
    legend = answer.get("legend")
    if isinstance(legend, dict):
        return {str(key): str(value) for key, value in legend.items()}
    return {}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def confidence_band(
    confidence: float | None,
    auto_threshold: float = 0.90,
    verify_threshold: float = 0.60,
) -> str:
    """Map a confidence onto the act-vs-escalate ladder."""
    if confidence is None:
        return "unknown"
    if confidence >= auto_threshold:
        return "act"
    if confidence >= verify_threshold:
        return "verify"
    return "escalate"


def build_question_results(
    answers: dict[str, Any],
    questions: dict[str, dict[str, Any]] | None = None,
    auto_threshold: float = 0.90,
    verify_threshold: float = 0.60,
) -> list[dict[str, Any]]:
    """Shape raw Laya answers into the payload the probability visualizer renders.

    Laya marks every answer with its own ``type``, which is authoritative: the
    model knows which head produced it. The question definition is used only for
    ``instructions`` and to cross-check the rubric.
    """
    questions = questions or {}
    results: list[dict[str, Any]] = []

    for name, answer in answers.items():
        if not isinstance(answer, dict):
            continue
        definition = questions.get(name, {})
        primitive = str(answer.get("type") or definition.get("type") or _infer_primitive(answer))
        confidence = extract_confidence(answer)
        probabilities = extract_probabilities(answer)
        legend = extract_legend(answer)

        ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
        breakdown = [
            {
                "label": _display_label(label, legend),
                "raw_label": label,
                "probability": round(probability, 6),
                "rank": rank + 1,
                "is_top": rank == 0,
            }
            for rank, (label, probability) in enumerate(ranked)
        ]

        # Fields are gated on the primitive rather than populated opportunistically.
        # Each head returns only its own output, so a `score` question has no
        # `choice` and a `choice` question has no `score` — filling them in from
        # the distribution would invent an answer that the model never gave.
        is_choice = primitive == "choice"
        is_score = primitive == "score"
        is_noul = primitive == "noul"

        results.append(
            {
                "question": name,
                "type": primitive,
                "instructions": str(definition.get("instructions", "")),
                "choice": extract_choice(answer) if is_choice else None,
                "level": _top_level(breakdown) if is_score else None,
                "confidence": None if confidence is None else round(confidence, 6),
                "noul": extract_noul(answer) if is_noul else None,
                "score": extract_score(answer) if is_score else None,
                "max_score": _max_score(legend, probabilities) if is_score else None,
                "act_probability": extract_act_probability(answer),
                "rubric": _rubric(primitive, definition, legend),
                "breakdown": breakdown if (is_choice or is_score) else [],
                "alternatives": (
                    [item for item in breakdown if not item["is_top"]]
                    if (is_choice or is_score)
                    else []
                ),
                "band": confidence_band(confidence, auto_threshold, verify_threshold),
            }
        )
    return results


def _display_label(label: str, legend: dict[str, str]) -> str:
    """Score distributions are keyed by index; show the rubric text instead."""
    return legend.get(label, label)


def _top_level(breakdown: list[dict[str, Any]]) -> str | None:
    """The single most likely rubric level.

    Distinct from ``score``, which is the *expected* level over the distribution.
    Both are useful and they can disagree: on a 0-2 rubric a ``score`` of 1.25
    with the mass on level 2 means the most likely answer is the top level, even
    though the expectation sits in the middle.
    """
    return breakdown[0]["label"] if breakdown else None


def _rubric(
    primitive: str,
    definition: dict[str, Any],
    legend: dict[str, str],
) -> list[str]:
    if primitive != "score":
        criteria = definition.get("criteria")
        if isinstance(criteria, dict):
            return [str(label) for label in criteria]
        if isinstance(criteria, list):
            return [str(level) for level in criteria]
        return []
    # The legend is what Laya actually rendered, so it wins over the stored
    # definition when the two disagree.
    if legend:
        return [legend[key] for key in sorted(legend, key=_as_index)]
    criteria = definition.get("criteria")
    return [str(level) for level in criteria] if isinstance(criteria, list) else []


def _as_index(key: str) -> int:
    try:
        return int(key)
    except (TypeError, ValueError):
        return 0


def _infer_primitive(answer: dict[str, Any]) -> str:
    if "choice" in answer:
        return "choice"
    if "score" in answer:
        return "score"
    if "noul" in answer:
        return "noul"
    return "choice"


def _max_score(legend: dict[str, str], probabilities: dict[str, float]) -> float | None:
    """Highest index on an ordinal rubric."""
    keys = list(legend) or list(probabilities)
    if not keys:
        return None
    indices = [_as_index(key) for key in keys]
    try:
        return float(max(indices))
    except ValueError:  # pragma: no cover - _as_index never raises
        return None


def normalize_result(
    raw: dict[str, Any],
    *,
    engine_name: str,
    latency_ms: float,
    default_model: str = "unknown",
) -> DecisionResult:
    """Wrap a raw engine payload into a :class:`DecisionResult`."""
    if not isinstance(raw, dict):
        raise TypeError(f"engine returned {type(raw).__name__}, expected dict")

    answers = raw.get("answers")
    if not isinstance(answers, dict):
        # Some surfaces return the answer map at the top level.
        answers = {
            key: value
            for key, value in raw.items()
            if key not in ("routing", "model", "latency_ms") and isinstance(value, dict)
        }

    routing = raw.get("routing")
    if not isinstance(routing, dict):
        routing = {}
    # `routing["model"]` names the checkpoint (english / multilingual); the
    # top-level `model` key is Laya's internal agent class name, so it is the
    # fallback, not the first choice.
    model = str(routing.get("model") or raw.get("model") or default_model)

    return DecisionResult(
        answers=answers,
        routing=routing,
        latency_ms=latency_ms,
        engine_name=engine_name,
        model=model,
    )


def validate_questions(questions: Any) -> list[dict[str, str]]:
    """Lint a Laya question bundle.

    Returns a list of ``{"severity", "question", "message"}`` findings. Errors
    block evaluation; warnings surface the documented Laya caveats (high
    cardinality, missing instructions) without blocking.
    """
    findings: list[dict[str, str]] = []

    if not isinstance(questions, dict) or not questions:
        findings.append(
            {
                "severity": "error",
                "question": "",
                "message": "At least one question is required.",
            }
        )
        return findings

    for name, definition in questions.items():
        if not isinstance(definition, dict):
            findings.append(
                {
                    "severity": "error",
                    "question": str(name),
                    "message": "Question definition must be an object.",
                }
            )
            continue

        primitive = definition.get("type")
        if primitive not in PRIMITIVES:
            findings.append(
                {
                    "severity": "error",
                    "question": str(name),
                    "message": f"type must be one of {', '.join(PRIMITIVES)} (got {primitive!r}).",
                }
            )
            continue

        if len(str(name)) > 120:
            findings.append(
                {
                    "severity": "warning",
                    "question": str(name),
                    "message": "Question names longer than 120 characters are truncated in the UI.",
                }
            )

        if not str(definition.get("instructions", "")).strip():
            findings.append(
                {
                    "severity": "warning",
                    "question": str(name),
                    "message": "No instructions set; the model has less to condition on.",
                }
            )

        criteria = definition.get("criteria")
        if primitive == "choice":
            if not isinstance(criteria, dict) or len(criteria) < 2:
                findings.append(
                    {
                        "severity": "error",
                        "question": str(name),
                        "message": "choice requires a criteria object with at least two labels.",
                    }
                )
            else:
                if any(not str(label).strip() for label in criteria):
                    findings.append(
                        {
                            "severity": "error",
                            "question": str(name),
                            "message": "choice labels must be non-empty.",
                        }
                    )
                if len(criteria) >= UNCALIBRATED_CHOICE_OPTIONS:
                    findings.append(
                        {
                            "severity": "warning",
                            "question": str(name),
                            "message": (
                                f"{len(criteria)} options lands in Laya's `choice:11+` "
                                "temperature bucket, whose shipped value is out of range and "
                                "gets clamped — so this question's confidence is uncalibrated. "
                                "Any confidence threshold you apply to it is a guess. "
                                "Shortlist the options below 11, or accept that the reported "
                                "confidence is not a probability."
                            ),
                        }
                    )
        elif primitive == "score":
            if not isinstance(criteria, list) or len(criteria) < 2:
                findings.append(
                    {
                        "severity": "error",
                        "question": str(name),
                        "message": "score requires an ordered criteria list of at least two levels.",
                    }
                )
            elif len(criteria) >= UNCALIBRATED_CHOICE_OPTIONS:
                findings.append(
                    {
                        "severity": "warning",
                        "question": str(name),
                        "message": (
                            f"{len(criteria)} rubric levels lands in Laya's `score:11+` bucket. "
                            "Ordinal levels are truncated to a fixed token budget, so adjacent "
                            "levels stop being distinguishable."
                        ),
                    }
                )
        elif primitive == "noul" and isinstance(criteria, dict):
            overlap = _criteria_vocabulary_overlap(criteria)
            if overlap:
                findings.append(
                    {
                        "severity": "warning",
                        "question": str(name),
                        "message": (
                            "The yes/no criteria share distinctive wording ("
                            + ", ".join(sorted(overlap)[:6])
                            + "). A one-forward-pass decision head separates these "
                            "surfaces weakly; describe the negative case with different "
                            "vocabulary."
                        ),
                    }
                )

    return findings


def _criteria_vocabulary_overlap(criteria: dict) -> set[str]:
    """Tokens shared between the affirmative and negative criteria of a ``noul``.

    Shared vocabulary is the single most common authoring mistake for a binary
    guardrail: "threatens to cancel" / "does not threaten to cancel" gives the
    decision head almost nothing to separate on.
    """
    positives: set[str] = set()
    negatives: set[str] = set()

    for label, description in criteria.items():
        surface = f"{label} {description}"
        key = str(label).strip().lower()
        if key in ("no", "false", "0", "negative", "absent", "n", "none"):
            negatives.update(content_tokens(surface))
        else:
            positives.update(content_tokens(surface))

    if not positives or not negatives:
        return set()

    shared: set[str] = set()
    for token in positives:
        if any(tokens_match(token, other) for other in negatives):
            shared.add(token)
    return shared


def has_errors(findings: list[dict[str, str]]) -> bool:
    return any(finding["severity"] == "error" for finding in findings)
