"""Inference layer: normalisation, routing and schema validation.

Nothing here touches an engine — these are the pure functions both the real
runtime and the test doubles flow through. The doubles themselves are covered by
test_doubles.py.
"""

from __future__ import annotations

import pytest

from app.inference import (
    build_question_results,
    confidence_band,
    extract_probabilities,
    flatten_state,
    normalize_result,
    validate_questions,
)
from app.inference.base import extract_score, has_errors
from app.inference.routing import detect_dominant_script, routing_decision

# --- flatten_state -----------------------------------------------------------


def test_flatten_state_prefers_free_text_fields_first():
    text = flatten_state({"id": 7, "body": "the body", "subject": "the subject"})
    # subject/body are the informative fields, so they must lead.
    assert text.index("the subject") < text.index("the body") < text.index("7")


def test_flatten_state_handles_nested_and_missing_values():
    text = flatten_state({"tags": ["a", "b"], "meta": {"k": "v"}, "empty": None})
    assert "a, b" in text
    assert "k=v" in text
    assert "empty" not in text


def test_flatten_state_accepts_plain_string():
    assert flatten_state("hello") == "hello"


# --- validation --------------------------------------------------------------


def test_validate_accepts_a_well_formed_bundle():
    findings = validate_questions(
        {
            "department": {
                "type": "choice",
                "instructions": "Which department?",
                "criteria": {"a": "first", "b": "second"},
            }
        }
    )
    assert findings == []


def test_validate_rejects_empty_bundle_and_bad_type():
    assert has_errors(validate_questions({}))
    findings = validate_questions({"q": {"type": "nonsense", "criteria": {}}})
    assert has_errors(findings)
    assert "type must be" in findings[0]["message"]


def test_validate_requires_distinct_choice_options():
    findings = validate_questions({"q": {"type": "choice", "criteria": {"only": "one"}}})
    assert has_errors(findings)


def test_validate_warns_once_a_choice_lands_in_the_uncalibrated_bucket():
    """The shipped `choice:11+` temperature is out of range and gets clamped.

    So at 11 options the reported confidence stops being calibrated, and a
    confidence threshold over it stops being meaningful. Ten is fine.
    """
    ten = validate_questions(
        {"q": {"type": "choice", "instructions": "pick",
               "criteria": {f"label_{i}": f"desc {i}" for i in range(10)}}}
    )
    assert not any("temperature bucket" in item["message"] for item in ten)

    eleven = validate_questions(
        {"q": {"type": "choice", "instructions": "pick",
               "criteria": {f"label_{i}": f"desc {i}" for i in range(11)}}}
    )
    assert not has_errors(eleven)
    assert any("choice:11+" in item["message"] for item in eleven)


def test_validate_warns_when_noul_criteria_share_vocabulary():
    """A binary guardrail whose yes/no surfaces overlap is the classic authoring bug."""
    findings = validate_questions(
        {
            "risky": {
                "type": "noul",
                "instructions": "Threat?",
                "criteria": {
                    "yes": "threatens to cancel the account",
                    "no": "does not cancel the account",
                },
            }
        }
    )
    assert any("share distinctive wording" in item["message"] for item in findings)


def test_validate_accepts_noul_criteria_with_disjoint_vocabulary():
    findings = validate_questions(
        {
            "risky": {
                "type": "noul",
                "instructions": "Threat?",
                "criteria": {
                    "yes": "threatens to cancel the account",
                    "no": "ordinary question with nothing unusual",
                },
            }
        }
    )
    assert not any("share distinctive wording" in item["message"] for item in findings)


# --- routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("plain english text", "latin"),
        ("Москва и область", "cyrillic"),
        ("שלום עולם", "hebrew"),
        ("नमस्ते दुनिया", "devanagari"),
        ("สวัสดีชาวโลก", "thai"),
        # Syriac has no SCRIPT_RANGES entry: letters, but no recognised script.
        ("ܫܠܡܐ ܥܠܡܐ", "other"),
        ("", "unknown"),
    ],
)
def test_detect_dominant_script(text, expected):
    assert detect_dominant_script(text) == expected


def test_routing_sends_non_latin_to_the_multilingual_checkpoint():
    decision = routing_decision({"text": "नमस्ते दुनिया"})
    assert decision["model"] == "multilingual"
    assert "non-Latin" in decision["reason"]


def test_routing_keeps_latin_on_the_english_checkpoint():
    assert routing_decision("hello there")["model"] == "english"


def test_routing_honours_an_explicit_override():
    decision = routing_decision("hello", force_model="typed-decisions")
    assert decision["model"] == "typed-decisions"
    assert decision["forced"] is True


# --- normalisation -----------------------------------------------------------


def test_extract_probabilities_probes_alternate_field_names():
    assert extract_probabilities({"probabilities": {"a": 0.7, "b": 0.3}}) == {"a": 0.7, "b": 0.3}
    assert extract_probabilities({"distribution": {"a": 0.5}}) == {"a": 0.5}
    assert extract_probabilities({"options": [{"label": "a", "probability": 0.9}]}) == {"a": 0.9}
    assert extract_probabilities({"nothing": 1}) == {}


def test_extract_probabilities_reads_a_list_of_scalars():
    assert extract_probabilities({"scores": [0.25, 0.75]}) == {"0": 0.25, "1": 0.75}


def test_normalize_result_accepts_a_bare_answer_map():
    result = normalize_result(
        {"department": {"choice": "billing", "confidence": 0.9}},
        engine_name="test",
        latency_ms=1.0,
    )
    assert result.answers["department"]["choice"] == "billing"
    assert result.model == "unknown"


def test_normalize_result_ignores_top_level_metadata_in_a_bare_answer_map():
    result = normalize_result(
        {"department": {"choice": "billing"}, "usage": {"input_tokens": 12}},
        engine_name="test",
        latency_ms=1.0,
    )
    assert list(result.answers) == ["department"]


def test_extract_score_orders_named_levels_by_rubric_not_alphabet():
    # Listed in rubric order; alphabetically this would be High, Low, Medium.
    answer = {"probabilities": {"Low": 0.1, "Medium": 0.2, "High": 0.7}}
    assert extract_score(answer) == pytest.approx(1.6)


def test_normalize_result_rejects_non_dicts():
    with pytest.raises(TypeError):
        normalize_result("nope", engine_name="test", latency_ms=0.0)  # type: ignore[arg-type]


def test_build_question_results_ranks_and_flags_the_ladder():
    results = build_question_results(
        {"q": {"choice": "b", "confidence": 0.95, "probabilities": {"a": 0.05, "b": 0.95}}},
        {"q": {"type": "choice", "instructions": "pick", "criteria": {"a": "x", "b": "y"}}},
        auto_threshold=0.90,
        verify_threshold=0.60,
    )
    entry = results[0]
    assert entry["choice"] == "b"
    top = entry["breakdown"][0]
    assert top["label"] == "b"
    assert top["raw_label"] == "b"
    assert top["probability"] == 0.95
    assert top["rank"] == 1 and top["is_top"] is True
    assert [a["label"] for a in entry["alternatives"]] == ["a"]
    assert entry["band"] == "act"


def test_build_question_results_recovers_score_from_a_distribution():
    results = build_question_results(
        {"sev": {"probabilities": {"0": 0.25, "1": 0.25, "2": 0.5}}},
        {"sev": {"type": "score", "criteria": ["low", "mid", "high"]}},
    )
    entry = results[0]
    # 0*0.25 + 1*0.25 + 2*0.5 == 1.25
    assert entry["score"] == pytest.approx(1.25)
    assert entry["max_score"] == 2.0
    # `level` is the most likely level; `score` is the expected one. They differ.
    assert entry["level"] == "2"


@pytest.mark.parametrize(
    "confidence,expected",
    [(0.97, "act"), (0.75, "verify"), (0.3, "escalate"), (None, "unknown")],
)
def test_confidence_band(confidence, expected):
    assert confidence_band(confidence, 0.90, 0.60) == expected
