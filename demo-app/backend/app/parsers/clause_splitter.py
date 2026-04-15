from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.schemas import Clause

# Matches common clause-heading patterns at the start of a line:
#   "1. ", "1.2 ", "2.3.4 ", "Section 7:", "ARTICLE II -"
#   Markdown headings "# Title" / "## Title"
_NUMBERED_HEADING_RE = re.compile(
    r"""^(?P<num>\d+(?:\.\d+)*)(?:\.|\)|:)?\s+(?P<title>[A-Z][^\n]{1,120})$""",
    re.MULTILINE,
)
_SECTION_HEADING_RE = re.compile(
    r"""^(?P<kind>Section|Article|Clause|SECTION|ARTICLE|CLAUSE)\s+
        (?P<num>[0-9IVXLC]+(?:\.\d+)*)
        \s*[:\.\-]?\s*(?P<title>[^\n]{0,120})$""",
    re.MULTILINE | re.VERBOSE,
)
_MD_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>[^\n]+)$", re.MULTILINE)
_ALL_CAPS_HEADING_RE = re.compile(
    r"^(?P<title>[A-Z][A-Z0-9 &/\-\.,']{4,80})$",
    re.MULTILINE,
)


@dataclass
class Heading:
    start: int
    end: int
    section_id: str
    title: str


def _collect_headings(text: str) -> list[Heading]:
    found: list[Heading] = []

    for m in _SECTION_HEADING_RE.finditer(text):
        found.append(
            Heading(
                start=m.start(),
                end=m.end(),
                section_id=f"{m.group('kind').lower()}-{m.group('num')}",
                title=(m.group("title") or "").strip(),
            )
        )
    for m in _NUMBERED_HEADING_RE.finditer(text):
        # Skip if already consumed by a Section/Article match at the same offset.
        if any(h.start == m.start() for h in found):
            continue
        found.append(
            Heading(
                start=m.start(),
                end=m.end(),
                section_id=m.group("num"),
                title=m.group("title").strip(),
            )
        )
    for m in _MD_HEADING_RE.finditer(text):
        found.append(
            Heading(
                start=m.start(),
                end=m.end(),
                section_id=f"h-{len(m.group('hashes'))}-{m.start()}",
                title=m.group("title").strip(),
            )
        )
    for m in _ALL_CAPS_HEADING_RE.finditer(text):
        # Only treat as heading if it's on its own line (prev/next blank)
        if _is_standalone_line(text, m.start(), m.end()):
            if any(h.start == m.start() for h in found):
                continue
            found.append(
                Heading(
                    start=m.start(),
                    end=m.end(),
                    section_id=f"cap-{m.start()}",
                    title=m.group("title").strip(),
                )
            )

    found.sort(key=lambda h: h.start)
    # Dedupe headings that start at the same offset (keep the first).
    deduped: list[Heading] = []
    last_start = -1
    for h in found:
        if h.start == last_start:
            continue
        deduped.append(h)
        last_start = h.start
    return deduped


def _is_standalone_line(text: str, start: int, end: int) -> bool:
    prev_nl = text.rfind("\n", 0, start)
    next_nl = text.find("\n", end)
    before_blank = (prev_nl == -1) or (text[prev_nl - 1 : prev_nl] in ("\n", ""))
    after_blank = (next_nl == -1) or (text[next_nl + 1 : next_nl + 2] in ("\n", ""))
    return before_blank and after_blank


def _fallback_paragraph_split(text: str) -> list[Clause]:
    """When no headings are detected, fall back to paragraph-level clauses."""
    clauses: list[Clause] = []
    cursor = 0
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    for i, para in enumerate(paragraphs, start=1):
        idx = text.find(para, cursor)
        if idx == -1:
            idx = cursor
        end = idx + len(para)
        cursor = end
        title = _derive_title(para)
        clauses.append(
            Clause(
                section_id=str(i),
                title=title,
                text=para.strip(),
                start_char=idx,
                end_char=end,
            )
        )
    return clauses


def _derive_title(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    first_line = first_line.strip()
    if len(first_line) <= 80:
        return first_line
    return first_line[:77].rstrip() + "..."


MIN_CLAUSE_CHARS = 20


def split_into_clauses(text: str) -> list[Clause]:
    """Split raw contract text into clause objects.

    Strategy:
      1. Find all probable headings (numbered, "Section X", markdown, ALL CAPS).
      2. The span between heading N and heading N+1 is a clause.
      3. Anything before the first heading becomes clause 'preamble'.
      4. If no headings are found, fall back to paragraph-split.
    Tiny fragments (< MIN_CLAUSE_CHARS after stripping) are dropped so the
    analyzer doesn't waste tokens on orphaned whitespace.
    """
    if not text or not text.strip():
        return []

    headings = _collect_headings(text)
    if not headings:
        return _fallback_paragraph_split(text)

    clauses: list[Clause] = []
    first_start = headings[0].start
    if first_start > 0:
        preamble = text[:first_start].strip()
        if len(preamble) >= MIN_CLAUSE_CHARS:
            clauses.append(
                Clause(
                    section_id="preamble",
                    title="Preamble",
                    text=preamble,
                    start_char=0,
                    end_char=first_start,
                )
            )

    for i, heading in enumerate(headings):
        start = heading.end
        end = headings[i + 1].start if i + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        if len(body) < MIN_CLAUSE_CHARS:
            continue
        clauses.append(
            Clause(
                section_id=heading.section_id,
                title=heading.title or _derive_title(body),
                text=body,
                start_char=start,
                end_char=end,
            )
        )

    # Ensure section_ids are unique; if duplicates, append an index.
    seen: dict[str, int] = {}
    for c in clauses:
        count = seen.get(c.section_id, 0)
        if count:
            c.section_id = f"{c.section_id}-{count}"
        seen[c.section_id.split("-")[0] if "-" in c.section_id else c.section_id] = count + 1

    return clauses
