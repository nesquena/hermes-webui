"""Server-side PDF text extraction for workspace uploads and previews.

Scope (#540): the Office slice shipped .docx/.xlsx/.pptx preview and upload
sidecars, but PDF was left out — a .pdf preview is a client-side iframe (#480)
and a .pdf upload produced no sidecar, so the agent could not read a PDF's text
at all. This module adds the missing server-side extraction. It is deliberately
additive: the browser keeps rendering the real document in the iframe for visual
fidelity, and this text feeds the agent-facing sidecar.

Design mirrors api/office_documents.py:
  * pypdf is a runtime-OPTIONAL dependency; absence raises ImportError carrying an
    install hint, which the routes surface as 503 rather than a 500 traceback.
  * Output is hard-bounded (page cap + char budget + input-size cap) so a
    hostile or merely huge PDF cannot exhaust CPU, memory, or the sidecar file.
  * Unreadable, corrupt, and encrypted input fails CLOSED with ValueError
    (rendered as a clean 4xx / sidecar error), never an unhandled traceback.
  * A PDF is read-only by nature: nothing here returns an edit surface.
"""

from __future__ import annotations

import io
from pathlib import Path

CLAIMED_PDF_EXTENSIONS = frozenset({".pdf"})
CLAIMED_PDF_FORMATS = frozenset({"pdf"})
PDF_PREVIEW_KIND = "pdf"
PDF_RENDER_MODE = "code"

PDF_DEPENDENCY_HINT = (
    "PDF text extraction is not available on this server. Install pypdf to "
    "enable it: pip install pypdf>=5.0"
)
PDF_PREVIEW_TRUNCATED_NOTICE = "[Preview truncated: PDF content exceeds safe limits]"
PDF_EMPTY_NOTICE = "Empty document (no extractable text)"

MAX_PDF_PREVIEW_PAGES = 500
MAX_PDF_PREVIEW_CHARS = 120_000
# Input ceiling. The upload route already caps at MAX_UPLOAD_BYTES; this is the
# defense-in-depth bound for extraction itself (a PDF's content streams can
# inflate far past its on-disk size).
MAX_PDF_INPUT_BYTES = 32_000_000


def _pdf_dependency_import_error() -> ImportError:
    return ImportError(PDF_DEPENDENCY_HINT)


def _pdf_read_error() -> ValueError:
    return ValueError("Unable to read PDF text")


def _load_pdf_reader():
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on local install shape
        raise _pdf_dependency_import_error() from exc
    return PdfReader


def is_claimed_pdf_path(path: str | Path) -> bool:
    return Path(str(path)).suffix.lower() in CLAIMED_PDF_EXTENSIONS


def _pdf_format_for_path(path: str | Path) -> str:
    return Path(str(path)).suffix.lower().lstrip(".")


class _TextBuilder:
    """Bounded text accumulator shared by the page and char budgets."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._started = False
        self.truncated = False

    @property
    def remaining_chars(self) -> int:
        return max(MAX_PDF_PREVIEW_CHARS - self._length, 0)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def has_content(self) -> bool:
        return bool(self._parts)

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def _append(self, piece: str) -> bool:
        if self.truncated:
            return False
        if not piece:
            return True
        remaining = self.remaining_chars
        if remaining <= 0:
            self.truncated = True
            return False
        if len(piece) > remaining:
            piece = piece[:remaining].rstrip()
            self.truncated = True
        if piece:
            self._parts.append(piece)
            self._length += len(piece)
        return not self.truncated

    def start_section(self) -> bool:
        if self._started:
            return self._append("\n\n")
        self._started = True
        return True

    def append_text(self, text: str) -> bool:
        if not self._started:
            self._started = True
        return self._append(text)

    def finish(self) -> tuple[str, bool]:
        text = self.text.strip()
        if len(text) > MAX_PDF_PREVIEW_CHARS:
            text = text[:MAX_PDF_PREVIEW_CHARS].rstrip()
            self.truncated = True
        if self.truncated:
            notice = PDF_PREVIEW_TRUNCATED_NOTICE
            text = f"{text}\n\n{notice}" if text else notice
        return text, self.truncated


def extract_pdf_text(raw: bytes) -> tuple[str, bool]:
    """Extract page-labelled plain text from a PDF.

    Returns ``(content, truncated)``. Raises ImportError (install hint) when
    pypdf is absent, and ValueError when the PDF cannot be read — including the
    encrypted case, which is a deliberate fail-closed rather than a silent
    empty result.
    """
    if len(raw) > MAX_PDF_INPUT_BYTES:
        raise ValueError("PDF exceeds the safe extraction size limit")

    # ImportError propagates on purpose: the routes map it to a 503 install hint.
    reader_factory = _load_pdf_reader()
    try:
        reader = reader_factory(io.BytesIO(raw), strict=False)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a clean 4xx
        raise _pdf_read_error() from exc

    try:
        if getattr(reader, "is_encrypted", False):
            # Empty-password decryption first (some producers "encrypt" with
            # nothing); anything still locked is reported, not faked empty.
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise ValueError("Encrypted PDFs cannot be read on this server") from exc
            if getattr(reader, "is_encrypted", False):
                raise ValueError("Encrypted PDFs cannot be read on this server")
        pages = list(reader.pages)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _pdf_read_error() from exc

    builder = _TextBuilder()
    extracted_any = False
    for page_index, page in enumerate(pages[:MAX_PDF_PREVIEW_PAGES], start=1):
        if not builder.start_section() or not builder.append_text(f"Page {page_index}"):
            break
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a single unreadable page must not
            # discard the pages that did extract.
            page_text = ""
        normalized = " ".join(page_text.split())
        if normalized:
            extracted_any = True
        else:
            # A per-page placeholder keeps the page numbering meaningful in a
            # mostly-text document that happens to contain an image-only page.
            normalized = "(no extractable text)"
        if not builder.append_text(f"\n{normalized}"):
            break

    if len(pages) > MAX_PDF_PREVIEW_PAGES:
        builder.truncated = True
    # A scanned/image-only PDF has no text anywhere: report that once, rather
    # than repeating a placeholder for every page.
    if not extracted_any:
        return PDF_EMPTY_NOTICE, builder.truncated
    return builder.finish()


def preview_pdf_document(path: str | Path, raw: bytes) -> dict:
    """Build the preview payload for a .pdf, mirroring the Office payload shape."""
    pdf_format = _pdf_format_for_path(path)
    if pdf_format not in CLAIMED_PDF_FORMATS:
        raise ValueError(f"Unsupported PDF path: {path}")

    content, truncated = extract_pdf_text(raw)
    payload = {
        "path": str(path),
        "content": content,
        "size": len(raw),
        "lines": content.count("\n") + 1,
        "preview_kind": PDF_PREVIEW_KIND,
        "pdf_format": pdf_format,
        "render_mode": PDF_RENDER_MODE,
        "editable": False,
        "edit_blocked_reason": "PDF is a read-only format in this slice",
    }
    if truncated:
        payload["truncated"] = True
    return payload