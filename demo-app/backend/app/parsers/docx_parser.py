from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Union

log = logging.getLogger(__name__)

DocxSource = Union[bytes, io.BufferedReader, io.BytesIO, str]


@dataclass
class ParsedDocx:
    text: str
    paragraphs: list[str] = field(default_factory=list)
    headings: list[tuple[int, str]] = field(default_factory=list)


def parse_docx(source: DocxSource) -> ParsedDocx:
    """Extract paragraphs and headings from a DOCX file.

    Headings (Heading 1/2/3...) are preserved so the clause splitter can use
    them as strong section boundaries. Tables are flattened row-by-row into
    the paragraph stream so their text still gets analyzed.
    """
    try:
        from docx import Document  # python-docx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "python-docx is required to parse DOCX files. Install with: pip install python-docx"
        ) from exc

    handle = _to_file_like(source)
    doc = Document(handle)

    paragraphs: list[str] = []
    headings: list[tuple[int, str]] = []

    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style_name = (para.style.name or "") if para.style else ""
        level = _heading_level(style_name)
        if level is not None:
            headings.append((level, text))
            paragraphs.append(f"{'#' * min(level, 6)} {text}")
        else:
            paragraphs.append(text)

    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text)
            if row_text:
                paragraphs.append(row_text)

    return ParsedDocx(
        text="\n\n".join(paragraphs),
        paragraphs=paragraphs,
        headings=headings,
    )


def _heading_level(style_name: str) -> int | None:
    name = style_name.strip().lower()
    if name.startswith("heading"):
        tail = name.replace("heading", "").strip()
        if tail.isdigit():
            return int(tail)
        return 1
    if name == "title":
        return 1
    return None


def _to_file_like(source: DocxSource):
    if isinstance(source, bytes):
        return io.BytesIO(source)
    return source
