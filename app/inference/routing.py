"""Unicode script detection powering language-aware checkpoint routing.

Laya's own router dispatches on Unicode script *before* the forward pass, which
matters because an English checkpoint fails loudly-but-confidently on non-Latin
input (the model card reports Khmer at 0.000 accuracy with 0.952 confidence).
Confidence gating alone therefore gives no warning — the script check does.

We reimplement the detection half here so that routing is inspectable without a
forward pass, and so the UI can label a state's script.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: ``(script, inclusive codepoint ranges)`` in rough order of prevalence.
SCRIPT_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("latin", ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F), (0x1E00, 0x1EFF))),
    ("cyrillic", ((0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF))),
    ("greek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("hebrew", ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))),
    ("devanagari", ((0x0900, 0x097F), (0xA8E0, 0xA8FF))),
    ("bengali", ((0x0980, 0x09FF),)),
    ("gurmukhi", ((0x0A00, 0x0A7F),)),
    ("gujarati", ((0x0A80, 0x0AFF),)),
    ("tamil", ((0x0B80, 0x0BFF),)),
    ("telugu", ((0x0C00, 0x0C7F),)),
    ("kannada", ((0x0C80, 0x0CFF),)),
    ("malayalam", ((0x0D00, 0x0D7F),)),
    ("sinhala", ((0x0D80, 0x0DFF),)),
    ("thai", ((0x0E00, 0x0E7F),)),
    ("lao", ((0x0E80, 0x0EFF),)),
    ("tibetan", ((0x0F00, 0x0FFF),)),
    ("myanmar", ((0x1000, 0x109F),)),
    ("georgian", ((0x10A0, 0x10FF),)),
    ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("ethiopic", ((0x1200, 0x137F),)),
    ("cherokee", ((0x13A0, 0x13FF),)),
    ("khmer", ((0x1780, 0x17FF),)),
    ("mongolian", ((0x1800, 0x18AF),)),
    ("hiragana", ((0x3040, 0x309F),)),
    ("katakana", ((0x30A0, 0x30FF),)),
    ("han", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))),
    ("armenian", ((0x0530, 0x058F),)),
)

#: Scripts that share the multilingual checkpoint's remit.
CJK_SCRIPTS = frozenset({"han", "hiragana", "katakana", "hangul"})

_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ScriptProfile:
    """Dominant script of a piece of text plus how dominant it is."""

    script: str
    fraction: float
    letter_count: int
    counts: dict[str, int]

    @property
    def is_latin(self) -> bool:
        return self.script == "latin"


def _script_of(char: str) -> str | None:
    codepoint = ord(char)
    for script, ranges in SCRIPT_RANGES:
        for low, high in ranges:
            if low <= codepoint <= high:
                return script
    return None


def profile_text(text: str) -> ScriptProfile:
    """Count letters per script and report the dominant one."""
    counts: dict[str, int] = {}
    total = 0
    for char in text:
        if not _LETTER_RE.match(char):
            continue
        total += 1
        script = _script_of(char)
        if script is None:
            continue
        counts[script] = counts.get(script, 0) + 1

    if total == 0:
        return ScriptProfile(script="unknown", fraction=0.0, letter_count=0, counts={})

    # Fold the CJK scripts into Han's tally for dominance purposes: a Japanese
    # sentence is "mostly CJK" even though the kana runs are separate blocks.
    grouped = dict(counts)
    grouped_cjk = sum(counts.get(name, 0) for name in CJK_SCRIPTS)
    for name in CJK_SCRIPTS:
        grouped.pop(name, None)
    if grouped_cjk:
        grouped["cjk"] = grouped_cjk

    dominant = max(grouped, key=grouped.__getitem__)
    return ScriptProfile(
        script=dominant,
        fraction=grouped[dominant] / total,
        letter_count=total,
        counts=counts,
    )


def detect_dominant_script(text: str) -> str:
    """Convenience wrapper returning just the script name."""
    return profile_text(text).script


def routing_decision(state: dict[str, Any] | str, force_model: str | None = None) -> dict[str, Any]:
    """Choose a checkpoint for ``state`` and explain the choice.

    Mirrors the fields Laya returns under ``result["routing"]`` so the UI can
    render either engine identically.
    """
    if force_model and force_model != "auto":
        return {
            "model": force_model,
            "repo": f"convaiinnovations/laya/{force_model}",
            "reason": "checkpoint explicitly requested; router bypassed",
            "script": None,
            "script_fraction": None,
            "forced": True,
        }

    if isinstance(state, str):
        text = state
    else:
        text = " ".join(str(value) for value in state.values())

    profile = profile_text(text)

    if profile.script == "unknown":
        return {
            "model": "english",
            "repo": "convaiinnovations/laya",
            "reason": "no letters detected; defaulting to the English checkpoint",
            "script": "unknown",
            "script_fraction": 0.0,
            "forced": False,
        }

    if profile.script == "latin" and profile.fraction >= 0.9:
        return {
            "model": "english",
            "repo": "convaiinnovations/laya",
            "reason": f"latin script ({profile.fraction:.0%} of letters)",
            "script": "latin",
            "script_fraction": round(profile.fraction, 4),
            "forced": False,
        }

    if profile.script == "latin":
        # Latin-dominant but mixed: the multilingual checkpoint is the safe pick
        # because it still reads Latin script.
        return {
            "model": "multilingual",
            "repo": "convaiinnovations/laya/multilingual",
            "reason": (
                f"mixed script ({profile.fraction:.0%} latin); "
                "multilingual checkpoint reads both scripts"
            ),
            "script": profile.script,
            "script_fraction": round(profile.fraction, 4),
            "forced": False,
        }

    return {
        "model": "multilingual",
        "repo": "convaiinnovations/laya/multilingual",
        "reason": (
            f"non-Latin script ({profile.script}, {profile.fraction:.0%} of letters); "
            "the English checkpoint cannot read it"
        ),
        "script": profile.script,
        "script_fraction": round(profile.fraction, 4),
        "forced": False,
    }
