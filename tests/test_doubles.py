"""The test doubles themselves.

Worth testing: every other test depends on them being deterministic and on their
answers staying in a shape the normaliser accepts. A silent change here would
quietly weaken the whole suite.
"""

from __future__ import annotations

import pytest

from tests.doubles import LexicalEngine, ScriptedEngine

CHOICE = {
    "department": {
        "type": "choice",
        "instructions": "Which department?",
        "criteria": {
            "billing": "invoices, refunds, duplicate charges",
            "technical": "bugs, outages, system errors",
            "sales": "pricing, new contracts",
        },
    }
}


def test_lexical_engine_is_deterministic():
    state = {"body": "We need a refund for a duplicate invoice charge."}
    first = LexicalEngine().predict(state, CHOICE)
    second = LexicalEngine().predict(state, CHOICE)
    assert first.answers == second.answers


def test_lexical_engine_prefers_the_matching_option():
    result = LexicalEngine().predict(
        {"text": "Our refund is missing and we were double billed."}, CHOICE
    )
    answer = result.answers["department"]
    assert answer["choice"] == "billing"
    assert sum(answer["probabilities"].values()) == pytest.approx(1.0, abs=1e-6)


def test_lexical_engine_is_uniform_without_evidence():
    result = LexicalEngine().predict({"text": "zzz qqq"}, CHOICE)
    assert result.answers["department"]["confidence"] == pytest.approx(1 / 3, abs=0.01)


def test_lexical_engine_noul_matches_the_real_answer_shape():
    result = LexicalEngine().predict(
        {"text": "we will cancel"}, {"r": {"type": "noul", "instructions": "Threat?"}}
    )
    answer = result.answers["r"]
    assert answer["type"] == "noul"
    assert 0.0 <= answer["noul"] <= 1.0
    assert "action" in answer


def test_lexical_engine_score_emits_a_legend():
    result = LexicalEngine().predict(
        {"text": "production is down"},
        {"u": {"type": "score", "instructions": "Urgency?", "criteria": ["none", "soon", "blocking"]}},
    )
    answer = result.answers["u"]
    assert answer["type"] == "score"
    assert set(answer["legend"]) == {"0", "1", "2"}
    assert 0.0 <= answer["score"] <= 2.0


def test_scripted_engine_returns_exactly_what_it_was_told():
    scripted = ScriptedEngine(
        answers={"department": {"type": "choice", "choice": "sales",
                                "probabilities": {"billing": 0.1, "sales": 0.9},
                                "confidence": 0.9, "action": {"act_probability": 0.9}}}
    )
    result = scripted.predict({"text": "anything"}, CHOICE)
    assert result.answers["department"]["choice"] == "sales"
    assert scripted.calls[0]["questions"] is CHOICE


def test_scripted_engine_records_its_calls():
    scripted = ScriptedEngine()
    scripted.predict({"text": "a"}, CHOICE, model="multilingual")
    assert scripted.calls[-1]["model"] == "multilingual"


def test_scripted_engine_can_raise():
    scripted = ScriptedEngine(raises=RuntimeError("engine exploded"))
    with pytest.raises(RuntimeError, match="engine exploded"):
        scripted.predict({"text": "a"}, CHOICE)


def test_scripted_engine_falls_back_to_a_neutral_answer():
    """The default must be uniform, so a test cannot accidentally read signal into it."""
    result = ScriptedEngine().predict({"text": "a"}, CHOICE)
    answer = result.answers["department"]
    assert answer["confidence"] == 0.0
    assert sum(answer["probabilities"].values()) == pytest.approx(1.0, abs=1e-6)
