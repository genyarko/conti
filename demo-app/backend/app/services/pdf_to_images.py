from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageImage:
    """One PDF page rendered to PNG bytes for multimodal LLM input."""

    page_number: int  # 1-indexed
    png_bytes: bytes
    width: int
    height: int


def _import_pdfium():
    """Lazy import so a missing pypdfium2 install only blocks multimodal mode."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "pypdfium2 is not installed. Run `pip install pypdfium2` to "
            "enable multimodal contract ingestion."
        ) from exc
    return pdfium


def render_pdf_pages(
    content: bytes,
    *,
    max_pages: int = 25,
    dpi: int = 144,
) -> list[PageImage]:
    """Render up to `max_pages` pages of a PDF to PNG bytes at `dpi`.

    Designed for sending pages to Gemini multimodally. The 144 DPI default
    keeps payloads well below Gemini's per-image budget while preserving
    enough resolution for OCR-quality text recognition. Pages beyond
    `max_pages` are silently dropped — the caller decides how to message that.
    """
    if not content:
        return []
    if max_pages <= 0:
        return []

    pdfium = _import_pdfium()
    # pdfium converts DPI → scale via a 72 DPI baseline. Floor to 1.0 so we
    # never ship pages smaller than the source, even if a caller passes
    # absurdly low DPI by mistake.
    scale = max(1.0, dpi / 72.0)

    images: list[PageImage] = []
    pdf = pdfium.PdfDocument(content)
    try:
        page_count = min(len(pdf), max_pages)
        for i in range(page_count):
            page = pdf[i]
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
                buf = io.BytesIO()
                pil_image.save(buf, format="PNG", optimize=True)
                images.append(
                    PageImage(
                        page_number=i + 1,
                        png_bytes=buf.getvalue(),
                        width=pil_image.width,
                        height=pil_image.height,
                    )
                )
            finally:
                page.close()
    finally:
        pdf.close()

    log.info(
        "rendered %d/%d PDF page(s) to PNG (dpi=%d, scale=%.2f)",
        len(images),
        len(images) if not images else page_count,
        dpi,
        scale,
    )
    return images


def to_image_parts(images: list[PageImage]) -> list[tuple[bytes, str]]:
    """Shape the renderer output for `GeminiClient(image_parts=...)`."""
    return [(img.png_bytes, "image/png") for img in images]
