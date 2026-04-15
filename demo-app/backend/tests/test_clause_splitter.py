from app.parsers.clause_splitter import split_into_clauses


def test_empty_input_returns_empty():
    assert split_into_clauses("") == []
    assert split_into_clauses("   \n\n  ") == []


def test_numbered_headings_split():
    text = """PREAMBLE PARAGRAPH WITH SOME WORDS HERE.

1. First Section
This is the body of the first section. It contains enough text to survive the
minimum-length filter.

2. Second Section
Body of the second section, also long enough to be kept.

3. Third Section
Body of the third, with substantive content."""
    clauses = split_into_clauses(text)
    ids = [c.section_id for c in clauses]
    assert "1" in ids and "2" in ids and "3" in ids
    sec_1 = next(c for c in clauses if c.section_id == "1")
    assert sec_1.title == "First Section"
    assert "first section" in sec_1.text.lower()


def test_section_headings_split():
    text = """ARTICLE I - PARTIES
This clause names the parties and their addresses for legal notice purposes.

ARTICLE II - TERM
The term of this agreement is twelve months from the effective date noted above."""
    clauses = split_into_clauses(text)
    ids = [c.section_id for c in clauses]
    assert any("article" in i for i in ids)


def test_fallback_paragraph_split():
    text = """This is paragraph one with some content that is long enough to count.

This is paragraph two which is clearly separate and has its own meaning."""
    clauses = split_into_clauses(text)
    assert len(clauses) == 2
    assert clauses[0].section_id == "1"
    assert clauses[1].section_id == "2"


def test_drops_tiny_fragments():
    text = """1. Real Section
A meaningful body with enough characters to survive filtering.

2. X
Also a body long enough."""
    clauses = split_into_clauses(text)
    # section 1 should appear; section 2 has a very short body but still > min.
    assert any(c.section_id == "1" for c in clauses)
