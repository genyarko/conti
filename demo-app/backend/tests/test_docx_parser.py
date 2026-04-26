"""Inline-heading detection in the docx parser.

Real-world contracts (corporate templates, legal forms) frequently use
Title-Case sentence-style section labels like "Termination. This Contract
may be terminated..." instead of Word's Heading styles or numbered headings.
Without detecting these, the entire body collapses into a single mega-clause
and clause-level analysis becomes impossible.
"""
from __future__ import annotations

from app.parsers.docx_parser import _looks_like_title_case_heading, _split_inline_heading


def test_standalone_heading_paragraph():
    heading, body = _split_inline_heading("Termination.")
    assert heading == "Termination"
    assert body is None


def test_inline_heading_with_body():
    heading, body = _split_inline_heading(
        "Indemnification.   Contractor will indemnify Mercy Corps for all claims."
    )
    assert heading == "Indemnification"
    assert body is not None
    assert body.startswith("Contractor will indemnify")


def test_multi_word_title_case_heading():
    heading, body = _split_inline_heading(
        "Work Product and Intellectual Property Rights. The parties agree as follows."
    )
    assert heading == "Work Product and Intellectual Property Rights"
    assert body is not None


def test_lowercase_connectors_allowed():
    assert _looks_like_title_case_heading("Delivery of Services")
    assert _looks_like_title_case_heading("Taxes, Duties and Expenses")
    assert _looks_like_title_case_heading("Access to Books and Records")


def test_prose_paragraph_is_not_a_heading():
    # First period appears late in a long sentence — too long to be a heading.
    heading, _ = _split_inline_heading(
        "Contractor will perform the Services in accordance with the Statement "
        "of Work attached hereto and made a part hereof."
    )
    assert heading is None


def test_lowercase_word_disqualifies():
    # "may", "be", "under" are lowercase non-connectors → prose, not heading.
    heading, _ = _split_inline_heading(
        "This Contract may be terminated under the following circumstances."
    )
    assert heading is None


def test_short_one_word_is_not_a_heading():
    # Avoid "Mr.", "Dr.", "Inc." being lifted as section headings.
    assert not _looks_like_title_case_heading("Mr")
    assert not _looks_like_title_case_heading("Dr")


def test_substantial_one_word_is_a_heading():
    assert _looks_like_title_case_heading("Termination")
    assert _looks_like_title_case_heading("Indemnification")
    assert _looks_like_title_case_heading("Confidentiality")
    assert _looks_like_title_case_heading("Miscellaneous")


def test_too_long_phrase_is_not_a_heading():
    # 80+ char phrase or 10+ words is treated as prose.
    long_phrase = " ".join(["Word"] * 12)
    assert not _looks_like_title_case_heading(long_phrase)


def test_paragraph_starting_lowercase_rejected():
    heading, _ = _split_inline_heading("by either Party for its convenience with notice.")
    assert heading is None
