"""Integration tests against the **real** Laya runtime.

Deselected by default because they download ~400 MB of weights and need torch:

    pytest --real-laya tests/test_real_laya.py

Everything asserted here was written by running it against ``laya 0.3.5``. The
rest of the suite runs against a double (``tests/doubles.py``); this file is what
keeps :class:`~app.inference.laya_engine.LayaEngine` honest.

Measured on an Apple M-series laptop (MPS), laya 0.3.5:

* one question: ~34 ms
* 32 questions: ~534 ms  (~17 ms/question)
* checkpoint temperatures: ``choice:11+`` ships at 0.1006, below Laya's clamp
  floor of 0.5, so Laya warns and clamps it
"""

from __future__ import annotations

import pytest

from app.inference.base import QuestionTooLarge, normalize_result
from app.inference.laya_engine import ALL_CHECKPOINTS, LayaEngine

pytestmark = pytest.mark.real_laya

QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which department should handle this request?",
        "criteria": {
            "billing": "invoices, payments, refunds",
            "technical": "bugs, outages, system errors",
            "sales": "pricing, new contracts",
            "other": "everything else",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this?",
        "criteria": ["no time pressure", "soon", "blocking or a deadline"],
    },
    "churn": {
        "type": "noul",
        "instructions": "Does the sender threaten to cancel?",
    },
}

BILLING = {
    "subject": "Duplicate charge on invoice #4411",
    "body": "We were billed twice for March. Please refund the duplicate or we will cancel.",
}


@pytest.fixture(scope="module")
def engine() -> LayaEngine:
    # A single instance for the module: loading a checkpoint costs seconds.
    return LayaEngine(preload=False, device="auto")


def test_router_predict_round_trips_through_the_normaliser(engine):
    result = engine.predict(BILLING, QUESTIONS)

    assert set(result.answers) == set(QUESTIONS)
    assert result.engine_name == "laya"

    for name, answer in result.answers.items():
        assert answer["type"] == QUESTIONS[name]["type"]

    department = result.answers["department"]
    assert department["choice"] in QUESTIONS["department"]["criteria"]
    assert 0.0 <= department["confidence"] <= 1.0
    assert sum(department["probabilities"].values()) == pytest.approx(1.0, abs=1e-3)


def test_the_checkpoint_is_named_in_provenance_not_the_agent_class(engine):
    """Laya's top-level `model` key is "laya-rl-agent"; the checkpoint is in routing."""
    result = engine.predict(BILLING, QUESTIONS)
    assert result.model in ALL_CHECKPOINTS
    assert result.routing.get("model") in ALL_CHECKPOINTS
    assert result.routing.get("repo")


def test_routing_sends_non_latin_text_to_the_multilingual_checkpoint(engine):
    decision = engine.route({"body": "同じ月に二回請求されています。返金してください。"})
    assert decision["model"] == "multilingual"

    result = engine.predict({"body": "同じ月に二回請求されています。返金してください。"}, QUESTIONS)
    assert result.routing["model"] == "multilingual"
    assert result.model == "multilingual"


def test_latin_text_stays_on_the_english_checkpoint(engine):
    assert engine.route(BILLING)["model"] == "english"


def test_score_answers_carry_a_legend_keyed_by_index(engine):
    """Laya keys score probabilities by index and ships a separate legend."""
    answer = engine.predict(BILLING, QUESTIONS).answers["urgency"]
    assert answer["type"] == "score"
    assert set(answer["legend"]) == {"0", "1", "2"}
    assert set(answer["probabilities"]) == {"0", "1", "2"}
    assert 0.0 <= answer["score"] <= 2.0


def test_confidence_is_entropy_based_not_the_top_probability(engine):
    """A real and easily-missed property: `confidence` != max(probabilities).

    Laya derives confidence as ``1 - H(p)/log(k)``. It is the value the ladder
    gates on, so conflating it with the top probability would silently mis-set
    every threshold.
    """
    department = engine.predict(BILLING, QUESTIONS).answers["department"]
    top_probability = max(department["probabilities"].values())
    assert department["confidence"] != pytest.approx(top_probability, abs=1e-6)


def test_an_uninformative_state_produces_a_low_confidence(engine):
    """The property the review queue depends on."""
    confident = engine.predict(BILLING, QUESTIONS).answers["department"]["confidence"]
    unsure = engine.predict({"body": "zzz qqq wwww"}, QUESTIONS).answers["department"]["confidence"]
    assert unsure < confident
    # A near-uniform distribution over 4 options has near-zero entropy confidence.
    assert unsure < 0.2


def test_answers_include_layas_own_act_probability(engine):
    answer = engine.predict(BILLING, QUESTIONS).answers["department"]
    assert "action" in answer
    act = answer["action"]["act_probability"]
    assert 0.0 <= act <= 1.0


def test_the_engine_reports_the_checkpoints_temperature_table(engine):
    """The checkpoint's own calibration, which the UI surfaces."""
    engine.predict(BILLING, QUESTIONS)  # ensure english is loaded
    configuration = engine.checkpoint_configuration()
    assert "english" in configuration

    english = configuration["english"]
    assert english["max_len"] == 512
    assert english["head_max_len"] == 192
    # Per (question type, option-count bucket), which is why the linter warns at 11.
    assert "choice:11+" in english["temperature_by_options"]


def test_eleven_options_land_in_the_uncalibrated_bucket(engine):
    """`choice:11+` ships a temperature Laya clamps, so confidence there is uncalibrated.

    The linter warns about this; here we confirm the bucket is real.
    """
    questions = {
        "pick": {
            "type": "choice",
            "instructions": "Pick one",
            "criteria": {f"option_{i}": f"description number {i}" for i in range(11)},
        }
    }
    answer = engine.predict({"body": "we were billed twice"}, questions).answers["pick"]
    assert answer["choice"] in questions["pick"]["criteria"]
    assert 0.0 <= answer["confidence"] <= 1.0


def test_options_that_cannot_fit_raise_a_typed_error(engine):
    """A head budget larger than the sequence limit drops trailing options.

    Laya detects that and raises rather than scoring a truncated option set; the
    adapter converts it into :class:`QuestionTooLarge` so callers get the remedy.
    """
    engine._router.load("english")
    agent = engine._router._agents["english"]
    original_max = agent.cfg["max_len"]
    original_head = agent.cfg["head_max_len"]
    try:
        # max_len (512) < head_max_len (700): the sequence truncates mid-options.
        agent.cfg["head_max_len"] = 700
        oversized = {
            "q": {
                "type": "choice",
                "instructions": "Pick",
                "criteria": {
                    f"option_{i}": "a fairly long description of this choice category " * 2
                    for i in range(40)
                },
            }
        }
        with pytest.raises(QuestionTooLarge) as excinfo:
            engine.predict({"body": "we were billed twice"}, oversized)
        assert "head_max_len" in str(excinfo.value)
        assert "sequence limit" in excinfo.value.remedy
    finally:
        agent.cfg["max_len"] = original_max
        agent.cfg["head_max_len"] = original_head


def test_the_transport_shape_is_stable(engine):
    """Guard against a Laya release that renames a key we depend on."""
    raw = engine._router.predict(BILLING, QUESTIONS)
    assert isinstance(raw, dict)
    assert set(raw) >= {"answers", "routing", "usage"}
    assert raw["usage"]["output_tokens"] == 0  # non-autoregressive: nothing generated
    assert isinstance(raw["routing"], dict)
    assert {"model", "reason"} <= set(raw["routing"])

    normalised = normalize_result(raw, engine_name="laya", latency_ms=1.0)
    assert normalised.model in ALL_CHECKPOINTS


def test_batching_amortises_the_per_call_overhead(engine):
    """Questions share one forward pass, so the marginal cost is lower than the first.

    Measured on MPS: ~34 ms for one question vs ~17 ms/question at 32. The win is
    real but sublinear — a schema with four questions is not four times the cost of
    one, and equally is not free.
    """
    import time

    def median_ms(n_questions: int, reps: int = 3) -> float:
        questions = {
            f"q{i}": {"type": "noul", "instructions": f"Is claim {i} true of this text?"}
            for i in range(n_questions)
        }
        engine.predict(BILLING, questions)  # warm
        samples = []
        for _ in range(reps):
            started = time.perf_counter()
            engine.predict(BILLING, questions)
            samples.append((time.perf_counter() - started) * 1000)
        samples.sort()
        return samples[len(samples) // 2]

    one = median_ms(1)
    sixteen = median_ms(16)
    assert sixteen < one * 16  # strictly better than linear in the call overhead
    assert sixteen > one * 4  # but nowhere near free
