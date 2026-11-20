"""Reply parsing and citation resolution.

Two real incidents are pinned here.

**Raw JSON reached the customer.** Against the live deployment the service
returned TWO complete JSON objects in a single message despite `strict: true`
on the response_format schema. `json.loads` raised, the prose fallback released
the literal `{"answer": ...}` text, and the customer-visible reply was raw JSON
-- twice over. The parser's job is therefore narrower than "parse the model's
JSON": it must never hand JSON plumbing to a customer, whatever the provider
sends.

**A well-grounded answer was withheld because a hash was mis-typed.** The model
used to cite chunk keys verbatim. On a live turn it returned
`...#02:d27aad6be70e3` for an accepted `...#02:d27aad6be70e` -- the right
chunk, one character longer. The citation guard refused the answer, correctly
under that contract, and the customer got "I couldn't confirm the policy
sources" for a question the evidence answered at 0.692 similarity. Citations
are now short ordinal refs resolved server-side, so there is no hash to
mis-type; the tests below cover the resolution and keep the guard's teeth.
"""

from __future__ import annotations

import json

from support.agent.runner import _parse_reply, _resolve_refs
from support.contracts.enums import GroundingDecision
from support.retrieval.search import RetrievalResult, RetrievedChunk

CHUNK_A = "refund-policy-premium@2.0#00:f8fe0c1cf4f2"
CHUNK_B = "refund-policy-premium@2.0#02:d27aad6be70e"


def envelope(answer: str, refs: list[int]) -> str:
    return json.dumps({"answer": answer, "cited_refs": refs})


def evidence(*keys: str) -> RetrievalResult:
    chunks = tuple(
        RetrievedChunk(
            chunk_key=key,
            doc_key="refund-policy-premium",
            document_title="Refund Policy",
            document_version="2.0",
            heading_path="Per-request refund cap",
            text="Premium refunds are capped at $500 per request.",
            similarity=0.6,
        )
        for key in keys
    )
    return RetrievalResult(
        decision=GroundingDecision.GROUNDED,
        threshold=0.38,
        top_similarity=0.6,
        accepted=chunks,
        considered=chunks,
    )


# ------------------------------------------------------------- envelopes --


def test_well_formed_envelope() -> None:
    text, refs = _parse_reply(envelope("Your refund is approved.", [1]))
    assert text == "Your refund is approved."
    assert refs == [1]


def test_two_concatenated_objects_takes_the_first() -> None:
    """The observed provider failure. Neither object may leak as text."""
    raw = envelope("First reply.", [1]) + envelope("Second reply.", [2])

    text, refs = _parse_reply(raw)

    assert text == "First reply."
    assert refs == [1]
    assert "cited_refs" not in text
    assert "{" not in text


def test_two_objects_separated_by_whitespace() -> None:
    raw = envelope("First reply.", [1]) + "\n\n" + envelope("Second.", [])
    text, _ = _parse_reply(raw)
    assert text == "First reply."


def test_trailing_prose_after_the_object_is_discarded() -> None:
    raw = envelope("The policy allows it.", [2]) + "\nHope that helps!"
    text, refs = _parse_reply(raw)
    assert text == "The policy allows it."
    assert refs == [2]


def test_plain_prose_passes_through_uncited() -> None:
    """A provider that ignores response_format must still produce a reply.

    It gets no citations, which is the safe direction: the answer is released
    without claiming a source rather than with one nobody checked.
    """
    raw = "You can get a refund within thirty days."
    text, refs = _parse_reply(raw)
    assert text == raw
    assert refs == []


def test_malformed_envelope_recovers_the_answer_field() -> None:
    """Truncated JSON still yields prose, not a brace salad."""
    raw = '{"answer": "Refunds take five days.", "cited_refs": [1'
    text, _ = _parse_reply(raw)
    assert text == "Refunds take five days."


def test_malformed_envelope_recovers_refs_when_the_array_closed() -> None:
    raw = '{"answer": "Refunds take five days.", "cited_refs": [1, 2], "extra'
    text, refs = _parse_reply(raw)
    assert text == "Refunds take five days."
    assert refs == [1, 2]


def test_malformed_envelope_recovers_escaped_quotes() -> None:
    raw = '{"answer": "We call this a \\"duplicate charge\\".", "cited_refs": []'
    text, _ = _parse_reply(raw)
    assert text == 'We call this a "duplicate charge".'


def test_unrecoverable_json_is_withheld_rather_than_shown() -> None:
    """Better an empty reply than JSON internals in the transcript."""
    raw = '{"unexpected": [1, 2, 3'
    text, refs = _parse_reply(raw)
    assert text == ""
    assert refs == []


def test_empty_answer_field_falls_back_rather_than_releasing_nothing() -> None:
    raw = '{"answer": "", "cited_refs": [1]}'
    text, refs = _parse_reply(raw)
    assert text == ""
    assert refs == [1]


def test_json_array_reply_is_not_released_as_text() -> None:
    text, _ = _parse_reply('[{"answer": "nope"}]')
    assert "{" not in text


def test_empty_and_blank_input() -> None:
    assert _parse_reply("") == ("", [])
    assert _parse_reply("   \n ") == ("", [])


def test_refs_of_the_wrong_json_type_are_dropped_not_guessed() -> None:
    """A stringly-typed ref is read; a word is not invented into a number."""
    raw = json.dumps({"answer": "Fine.", "cited_refs": ["2", 1.0, True, "two", None, "[3]"]})
    _, refs = _parse_reply(raw)
    assert refs == [2, 1, 3]


# ------------------------------------------------------------ resolution --


def test_refs_resolve_in_evidence_order() -> None:
    """The order the model was shown is the order it cites against."""
    assert _resolve_refs([1, 2], evidence(CHUNK_A, CHUNK_B)) == [CHUNK_A, CHUNK_B]
    assert _resolve_refs([2], evidence(CHUNK_A, CHUNK_B)) == [CHUNK_B]


def test_a_ref_outside_the_range_is_refused_not_dropped() -> None:
    """The whole point of the guard.

    Dropping an unknown ref would release a policy claim whose stated source
    does not exist. It is resolved to something that cannot be a chunk key, so
    the downstream validation refuses the answer exactly as it would for a
    fabricated id.
    """
    resolved = _resolve_refs([1, 9], evidence(CHUNK_A, CHUNK_B))

    assert resolved[0] == CHUNK_A
    assert resolved[1] != CHUNK_B
    assert "9" in resolved[1]
    assert resolved[1] not in {CHUNK_A, CHUNK_B}


def test_zero_and_negative_refs_are_refused() -> None:
    """1-based, so 0 is not "the first chunk" and -1 is not "the last"."""
    for ref in (0, -1):
        resolved = _resolve_refs([ref], evidence(CHUNK_A, CHUNK_B))
        assert resolved[0] not in {CHUNK_A, CHUNK_B}


def test_no_evidence_means_no_citations() -> None:
    assert _resolve_refs([1], None) == []


def test_resolution_is_not_fooled_by_a_near_miss_key() -> None:
    """The failure that motivated refs cannot recur: there is no key to copy.

    A model that writes the mis-typed key as a *ref* produces no digits at all,
    so it never reaches resolution; this pins the complementary half -- that a
    near-miss key is not quietly matched to the chunk it resembles.
    """
    near_miss = CHUNK_B + "3"
    _, refs = _parse_reply(json.dumps({"answer": "Fine.", "cited_refs": [near_miss]}))
    assert refs == []
