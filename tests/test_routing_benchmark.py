"""Routing benchmark: terse developer instructions against the intent they mean (issue #18).

It runs the deterministic router with no model, so it is fast and the same everywhere.
"""

import pytest

from tests.routing_cases import (
    CASES,
    EDITING,
    KNOWN_GAPS,
    READ_ONLY,
    intent_of,
    score,
)


@pytest.mark.parametrize(("text", "expected"), CASES)
async def test_instruction_routes_to_its_intent(text: str, expected: object) -> None:
    assert await intent_of(text) is expected


@pytest.mark.parametrize(
    ("text", "expected", "why"),
    [pytest.param(*gap, marks=pytest.mark.xfail(strict=True, reason=gap[2])) for gap in KNOWN_GAPS],
)
async def test_known_gap_still_fails(text: str, expected: object, why: str) -> None:
    assert await intent_of(text) is expected


async def test_a_read_only_request_never_reaches_the_editing_path() -> None:
    """The failure that matters: asking to be told something must not run the editing loop."""
    wrong = [
        text
        for text, expected in [*CASES, *((t, i) for t, i, _ in KNOWN_GAPS)]
        if expected in READ_ONLY and await intent_of(text) in EDITING
    ]
    assert wrong == []


async def test_every_miss_is_a_listed_known_gap() -> None:
    right, total, misses = await score()
    listed = {text for text, _, _ in KNOWN_GAPS}
    assert {text for text, _, _ in misses} == listed
    assert right == total - len(KNOWN_GAPS)
