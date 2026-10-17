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
import re
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
# Distinct from PDF_EMPTY_NOTICE: the page cap stopped the walk, so "no text
# here" is a statement about the pages that were CHECKED, not the document.
# Reporting it as an empty document would make the sidecar assert that a
# scanned appendix carries no text.
PDF_UNCHECKED_PAGES_NOTICE = (
    "No extractable text in the first {checked} of {total} pages "
    "(later pages were not checked)"
)

MAX_PDF_PREVIEW_PAGES = 500
MAX_PDF_PREVIEW_CHARS = 120_000
# Per-page line cap: page structure is preserved (see _normalize_page_text),
# which means a page of whitespace-only or one-character rows could otherwise
# spend the character budget on newlines alone.
MAX_PDF_PAGE_TEXT_LINES = 2_000
# Input ceiling. The upload route already caps at MAX_UPLOAD_BYTES; this is the
# defense-in-depth bound for extraction itself (a PDF's content streams can
# inflate far past its on-disk size).
MAX_PDF_INPUT_BYTES = 32_000_000


def _pdf_dependency_import_error() -> ImportError:
    return ImportError(PDF_DEPENDENCY_HINT)


def _pdf_not_decrypted_sentinel():
    """The value ``PdfReader.decrypt`` returns when no password matched.

    Read from pypdf so the comparison tracks the installed version rather than
    a hardcoded int; falls back to ``0`` (``PasswordType.NOT_DECRYPTED``) if a
    pypdf build does not export the enum.
    """
    try:
        from pypdf import PasswordType
    except ImportError:  # pragma: no cover - very old/odd pypdf build
        return 0
    return getattr(PasswordType, "NOT_DECRYPTED", 0)


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


def _normalize_page_text(page_text: str, max_lines: int | None = None) -> tuple[str, bool]:
    """Collapse PDF layout whitespace while KEEPING line structure.

    pypdf emits a line break wherever the PDF places one, so the line
    structure carries the document's lists, records, and code blocks. Joining
    every line into one string (``" ".join(page_text.split())``) destroyed
    exactly that: numbered lists ran together and code lost its breaks, so the
    agent received a wall of prose where the document had structure.

    Within a line, runs of spaces/tabs still collapse to one space — PDF
    positioning operators emit many — but newlines are real structure and are
    preserved. Blank-line runs collapse to a single separator so a page of
    mostly whitespace cannot pad the character budget.

    Returns ``(text, dropped_trailing_lines)``.
    """
    # Read the cap from the module at call time (not a default argument) so the
    # existing monkeypatch-the-cap test convention keeps working.
    if max_lines is None:
        max_lines = MAX_PDF_PAGE_TEXT_LINES
    lines: list[str] = []
    dropped = False
    for raw_line in page_text.splitlines():
        line = re.sub(r"[^\S\n]+", " ", raw_line).strip()
        if not line:
            # Keep at most one blank line, so paragraph gaps survive but a
            # whitespace-only page cannot spend the budget on empty rows.
            if lines and lines[-1] == "":
                continue
            lines.append("")
            continue
        if max_lines > 0 and len(lines) >= max_lines:
            dropped = True
            break
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines), dropped


def _extract_page_text(page, char_budget: int) -> tuple[str, bool]:
    """Extract one page's text, aborting once ``char_budget`` characters are seen.

    Returns ``(text, hit_budget)``. ``page.extract_text()`` returns the whole
    page, so calling it and clipping afterwards means a hostile page is parsed
    in full before the cap applies. pypdf's ``visitor_text`` hook is called
    incrementally per text-showing operator, so the walk stops as soon as the
    budget is spent and the remaining operators are never decoded.

    The per-operator granularity is pypdf's, not ours: a single operator
    carrying a megabyte of text is still handed over whole. That bounds work
    per operator rather than per character, which is strictly better than
    parsing the entire page and is what the installed pypdf exposes.
    """
    if char_budget <= 0:
        return "", True

    parts: list[str] = []
    seen = 0

    class _BudgetExhausted(Exception):
        pass

    def _visitor(text, *_args) -> None:
        nonlocal seen
        if not text:
            return
        parts.append(text)
        seen += len(text)
        if seen >= char_budget:
            raise _BudgetExhausted

    try:
        page.extract_text(visitor_text=_visitor)
        return "".join(parts), False
    except _BudgetExhausted:
        return "".join(parts), True
    except Exception:  # noqa: BLE001 - a single unreadable page must not
        # discard the pages that did extract.
        return "", False


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
                outcome = reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise ValueError("Encrypted PDFs cannot be read on this server") from exc
            # Trust decrypt()'s RETURN VALUE, not a second is_encrypted read:
            # pypdf documents that is_encrypted "will remain true even after
            # the decrypt() method is called", so re-checking it rejects a
            # document whose empty user password decrypted correctly. The
            # return is NOT_DECRYPTED exactly when no password matched.
            if outcome == _pdf_not_decrypted_sentinel():
                raise ValueError("Encrypted PDFs cannot be read on this server")
        # The page COUNT only — never `list(reader.pages)`. Materializing every
        # page object up front means a document with 100k pages pays to build
        # 100k page objects before the cap drops all but the first 500, so the
        # cap would not bound real work at all.
        total_pages = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _pdf_read_error() from exc

    builder = _TextBuilder()
    extracted_any = False
    for page_index in range(1, min(total_pages, MAX_PDF_PREVIEW_PAGES) + 1):
        if not builder.start_section() or not builder.append_text(f"Page {page_index}"):
            break
        try:
            page = reader.pages[page_index - 1]
        except Exception:  # noqa: BLE001 - unreadable page; keep the others.
            page_text, hit_budget = "", False
        else:
            # Reserve room for the label separator and the closing notice, so
            # the cap bounds the walk rather than being trimmed afterwards.
            budget = builder.remaining_chars - 1
            page_text, hit_budget = _extract_page_text(page, budget)
        normalized, dropped_lines = _normalize_page_text(page_text)
        if normalized:
            extracted_any = True
        else:
            # A per-page placeholder keeps the page numbering meaningful in a
            # mostly-text document that happens to contain an image-only page.
            normalized = "(no extractable text)"
        # ORDER MATTERS: append the bounded text BEFORE recording that a cap was
        # hit. _append() refuses every subsequent piece once `truncated` is set,
        # so setting the flag first made the append a no-op and discarded the
        # whole page — a long first page yielded "Page 1" plus the truncation
        # notice and no document text at all, which is the opposite of what the
        # cap is for.
        keep_going = builder.append_text(f"\n{normalized}")
        if hit_budget or dropped_lines:
            builder.truncated = True
        if not keep_going:
            break

    pages_unchecked = total_pages > MAX_PDF_PREVIEW_PAGES
    if pages_unchecked:
        builder.truncated = True
    # A scanned/image-only PDF has no text anywhere: report that once, rather
    # than repeating a placeholder for every page. When the page cap cut the
    # walk short, "empty" would be a claim about the whole document that we
    # cannot support — later pages may well carry the text — so say which
    # pages were actually checked instead.
    if not extracted_any:
        if pages_unchecked:
            notice = PDF_UNCHECKED_PAGES_NOTICE.format(
                checked=MAX_PDF_PREVIEW_PAGES, total=total_pages
            )
            return notice, True
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