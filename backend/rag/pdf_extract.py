from __future__ import annotations

from io import BytesIO

from backend.core.logging import get_logger

logger = get_logger(__name__)


# Soft-import so the rest of the rag package keeps loading even if the
# binary wheel for pypdf isn't installed in some environment (e.g.
# integration tests that don't need PDF).
try:
    from pypdf import PdfReader
    _HAS_PYPDF = True
except ImportError:  # pragma: no cover — runtime degraded mode
    PdfReader = None  # type: ignore[assignment]
    _HAS_PYPDF = False


MAX_PAGES = 200


class PdfUnsupportedError(RuntimeError):
    """Raised when pypdf isn't available or the PDF is unparseable."""


def extract_text(blob: bytes) -> str:
    """Best-effort plain-text extraction from a PDF byte blob.

    Returns the concatenated text of all pages separated by blank lines.
    Page count is capped at MAX_PAGES so a malicious/oversized PDF can't
    explode our chunker. We log and skip pages that fail individually
    rather than aborting the whole extract.
    """
    if not _HAS_PYPDF:
        raise PdfUnsupportedError("pypdf is not installed in this image")
    if not blob:
        return ""

    try:
        reader = PdfReader(BytesIO(blob))
    except Exception as exc:  # noqa: BLE001 — bubble as a clean error
        raise PdfUnsupportedError(f"could not parse PDF: {exc}") from exc

    return _extract_from_reader(reader)


def _extract_from_reader(reader) -> str:
    pages: list[str] = []
    for i, page in enumerate(reader.pages[:MAX_PAGES]):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("rag.pdf.page_extract_failed", page=i, error=str(exc))
            continue
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)
