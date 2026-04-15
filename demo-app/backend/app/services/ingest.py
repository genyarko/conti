from __future__ import annotations

import logging
from pathlib import PurePath
from typing import Optional

from app.models.schemas import ParsedContract
from app.parsers import parse_docx, parse_pdf, split_into_clauses

log = logging.getLogger(__name__)


def _infer_doc_type(filename: Optional[str], content: bytes) -> str:
    if filename:
        suffix = PurePath(filename).suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix in (".docx", ".doc"):
            return "docx"
        if suffix in (".txt", ".md"):
            return "txt"
    if content.startswith(b"%PDF"):
        return "pdf"
    if content[:4] == b"PK\x03\x04":
        # .docx is a zip container; safe bet when paired with no suffix.
        return "docx"
    return "txt"


def ingest_bytes(
    content: bytes,
    *,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
) -> ParsedContract:
    doc_type = _infer_doc_type(filename, content)
    if doc_type == "pdf":
        parsed = parse_pdf(content)
        text = parsed.text
        metadata = {"num_pages": parsed.num_pages}
    elif doc_type == "docx":
        parsed = parse_docx(content)
        text = parsed.text
        metadata = {"num_headings": len(parsed.headings)}
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
        metadata = {}

    clauses = split_into_clauses(text)
    log.info(
        "ingested contract filename=%s doc_type=%s chars=%d clauses=%d",
        filename,
        doc_type,
        len(text),
        len(clauses),
    )
    return ParsedContract(
        filename=filename or "",
        doc_type=doc_type,
        raw_text=text,
        clauses=clauses,
        metadata=metadata,
    )


def ingest_text(text: str, *, filename: Optional[str] = None) -> ParsedContract:
    return ingest_bytes(text.encode("utf-8"), filename=filename or "raw.txt")
