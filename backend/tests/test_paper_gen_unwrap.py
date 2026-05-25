"""Unit tests for the LLM-shape tolerant unwrap.

The small generation model (qwen2.5:3b) emits valid JSON when forced
into json_mode but the *shape* drifts: contract key, alternates, single
object, bare list, embedded list under a different key. The unwrap must
accept all of those because the paper-gen flow's silent zero-question
failure mode was a wrap-key mismatch.
"""
from __future__ import annotations

from backend.services.paper_generator import _items_from_parsed


def _q(text: str) -> dict:
    return {"question_text": text,
            "difficulty": "medium", "answer_key": "..."}


def test_bare_list_passes_through() -> None:
    items = [_q("a"), _q("b")]
    assert _items_from_parsed(items) == items


def test_contract_key_questions() -> None:
    assert _items_from_parsed({"questions": [_q("a")]}) == [_q("a")]


def test_alternate_keys() -> None:
    for key in ("items", "list", "data", "results"):
        assert _items_from_parsed({key: [_q("x")]}) == [_q("x")], (
            f"unwrap failed for key {key!r}")


def test_single_object_wrapped() -> None:
    one = _q("just one")
    assert _items_from_parsed(one) == [one]


def test_unknown_key_with_question_text_inside() -> None:
    parsed = {"my_random_field": [_q("z")]}
    assert _items_from_parsed(parsed) == [_q("z")]


def test_empty_object_returns_empty_list() -> None:
    # The original bug: model returns {} → silent zero questions. The
    # unwrap returns [] cleanly so the warning path fires.
    assert _items_from_parsed({}) == []


def test_garbage_types_return_empty_list() -> None:
    assert _items_from_parsed(None) == []
    assert _items_from_parsed(42) == []
    assert _items_from_parsed("string") == []


def test_questions_key_with_non_list_value_skipped() -> None:
    # If the model writes {"questions": "stub"}, do NOT crash on iteration.
    assert _items_from_parsed({"questions": "stub"}) == []
