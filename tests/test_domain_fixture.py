"""Structural checks for the human-authored domain evaluation corpus.

These checks make the fixture consumable by a future controlled-response
runner. They do not evaluate model behavior or enforce the domain policy.
"""

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "domain_evaluation.json"


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict), "fixture root must be an object"
    return value


def test_domain_fixture_has_supported_top_level_shape(corpus: dict[str, Any]) -> None:
    assert corpus.get("schema_version") == 1
    assert _nonblank(corpus.get("title"))
    assert _nonblank(corpus.get("purpose"))
    assert isinstance(corpus.get("evaluation_notes"), list)
    assert corpus["evaluation_notes"]
    assert all(_nonblank(note) for note in corpus["evaluation_notes"])
    assert isinstance(corpus.get("current_gateway_capabilities"), dict)
    assert isinstance(corpus.get("cases"), list) and corpus["cases"]


def test_domain_fixture_cases_are_well_formed(corpus: dict[str, Any]) -> None:
    seen_ids: set[str] = set()
    for case in corpus["cases"]:
        assert isinstance(case, dict)
        case_id = case.get("id")
        assert _nonblank(case_id), "each case needs a nonblank id"
        assert case_id not in seen_ids, f"duplicate case id: {case_id}"
        seen_ids.add(case_id)

        assert _nonblank(case.get("category")), f"{case_id}: category is required"
        assert _nonblank(case.get("language")), f"{case_id}: language is required"
        turns = case.get("turns")
        assert isinstance(turns, list) and turns, f"{case_id}: turns must be nonempty"
        for turn in turns:
            assert isinstance(turn, dict), f"{case_id}: each turn must be an object"
            assert turn.get("role") in {"user", "assistant"}, (
                f"{case_id}: turn role must be user or assistant"
            )
            assert _nonblank(turn.get("text")), f"{case_id}: turn text must be nonblank"

        expected = case.get("expected_behavior")
        assert isinstance(expected, list) and expected, (
            f"{case_id}: expected_behavior must be a nonempty list"
        )
        assert all(_nonblank(item) for item in expected), (
            f"{case_id}: expected_behavior items must be nonblank strings"
        )

        forbidden = case.get("forbidden_behavior", [])
        assert isinstance(forbidden, list), f"{case_id}: forbidden_behavior must be a list"
        assert all(_nonblank(item) for item in forbidden), (
            f"{case_id}: forbidden_behavior items must be nonblank strings"
        )
        if "memory_expectation" in case:
            assert _nonblank(case["memory_expectation"]), (
                f"{case_id}: memory_expectation must be nonblank"
            )
        if "setup" in case:
            assert isinstance(case["setup"], dict), f"{case_id}: setup must be an object"
