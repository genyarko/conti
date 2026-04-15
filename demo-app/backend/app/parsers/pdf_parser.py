from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Union

log = logging.getLogger(__name__)

PdfSource = Union[bytes, io.BufferedReader, io.BytesIO, str]


@dataclass
class ParsedPdf:
    text: str
    num_pages: int
    page_texts: list[str]


def parse_pdf(source: PdfSource) -> ParsedPdf:
    """Extract text from a PDF.

    Accepts raw bytes, a path, or a file-like object. Uses pdfplumber so we can
    later tap into layout/table structure if the analyzer needs it. Pages are
    joined with a form-feed character so the clause splitter can still recover
    page boundaries when needed.
    """
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover — surfaced to caller
        raise RuntimeError(
            "pdfplumber is required to parse PDFs. Install with: pip install pdfplumber"
        ) from exc

    handle = _to_file_like(source)
    page_texts: list[str] = []
    with pdfplumber.open(handle) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            page_texts.append(page_text.strip())

    joined = "\n\n".join(pt for pt in page_texts if pt)
    return ParsedPdf(text=joined, num_pages=len(page_texts), page_texts=page_texts)


def _to_file_like(source: PdfSource):
    if isinstance(source, bytes):
        return io.BytesIO(source)
    return source
