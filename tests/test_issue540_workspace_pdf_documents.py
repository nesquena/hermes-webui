"""#540 Phase 3 (PDF slice): server-side PDF text extraction + upload sidecar.

The Office slice (#5142, v0.51.815) shipped .docx/.xlsx/.pptx preview + safe
docx edit, and #5859 shipped Office upload sidecars. PDF was left out of both:
a .pdf preview is a client-side iframe (#480) and a .pdf upload produces no
sidecar, so the agent cannot read the document's text at all.

These tests pin the missing server-side extraction and the .pdf sidecar branch
of the upload sidecar helper, reusing the existing safety posture of
api/office_documents.py: bounded output, fail-closed on unreadable/encrypted
input, optional dependency, and never a 500 escaping to the caller.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# pypdf is runtime-optional (commented in requirements.txt, installed by CI and
# requirements-dev.txt) — importorskip so a lean install skips this file cleanly
# instead of aborting collection for the whole suite.
pytest.importorskip("pypdf")

import api.pdf_documents as pdf_documents
from api.pdf_documents import (
    CLAIMED_PDF_EXTENSIONS,
    extract_pdf_text,
    is_claimed_pdf_path,
    preview_pdf_document,
)


def _pdf_bytes(*pages: str, encrypt_with: str | None = None) -> bytes:
    """Build a minimal, uncompressed PDF whose pages carry the given text."""
    pages = pages or ("",)

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_number = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    # Object numbering is deterministic: font=1, then per page a content stream
    # and a page object (2n, 2n+1), then /Pages, then /Catalog. Reserve the
    # /Pages number up front — page objects must /Parent-reference it.
    pages_obj_number = 1 + (2 * len(pages)) + 1
    page_numbers: list[int] = []
    for text in pages:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        content_number = add(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")
        page_numbers.append(
            add(
                b"<< /Type /Page /Parent "
                + str(pages_obj_number).encode("ascii")
                + b" 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 "
                + str(font_number).encode("ascii")
                + b" 0 R >> >> /Contents "
                + str(content_number).encode("ascii")
                + b" 0 R >>"
            )
        )

    kids = b" ".join(f"{number} 0 R".encode("ascii") for number in page_numbers)
    add(
        b"<< /Type /Pages /Kids ["
        + kids
        + b"] /Count "
        + str(len(page_numbers)).encode("ascii")
        + b" >>"
    )
    catalog_number = add(b"<< /Type /Catalog /Pages " + str(pages_obj_number).encode("ascii") + b" 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(index).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode("ascii") + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    trailer = b"trailer\n<< /Size " + str(len(objects) + 1).encode("ascii") + b" /Root " + str(catalog_number).encode("ascii") + b" 0 R"
    if encrypt_with is not None:
        # A structurally valid trailer naming an /Encrypt entry the reader will
        # refuse to decrypt without a password — the fail-closed path under test.
        trailer += b" /Encrypt 1 0 R"
    out += trailer + b" >>\nstartxref\n" + str(xref_offset).encode("ascii") + b"\n%%EOF\n"
    return bytes(out)


def test_pdf_registry_claims_pdf_only():
    assert CLAIMED_PDF_EXTENSIONS == {".pdf"}
    assert is_claimed_pdf_path("report.pdf")
    assert is_claimed_pdf_path("REPORT.PDF")
    assert not is_claimed_pdf_path("report.docx")
    assert not is_claimed_pdf_path("notes.txt")


def test_extract_pdf_text_returns_page_labelled_content():
    content, truncated = extract_pdf_text(_pdf_bytes("alpha page", "beta page"))

    assert "Page 1" in content
    assert "alpha page" in content
    assert "Page 2" in content
    assert "beta page" in content
    assert truncated is False


def test_extract_pdf_text_rejects_non_pdf_bytes():
    with pytest.raises(ValueError):
        extract_pdf_text(b"this is not a pdf at all")


def test_extract_pdf_text_is_bounded_by_page_cap(monkeypatch):
    monkeypatch.setattr(pdf_documents, "MAX_PDF_PREVIEW_PAGES", 2)

    content, truncated = extract_pdf_text(_pdf_bytes("one", "two", "three", "four"))

    assert truncated is True
    assert "Page 3" not in content
    assert "four" not in content
    assert pdf_documents.PDF_PREVIEW_TRUNCATED_NOTICE in content


def test_extract_pdf_text_is_bounded_by_char_cap(monkeypatch):
    monkeypatch.setattr(pdf_documents, "MAX_PDF_PREVIEW_CHARS", 40)

    content, truncated = extract_pdf_text(_pdf_bytes("x" * 400))

    assert truncated is True
    # The budget bounds the extracted content itself; the truncation notice (and
    # its separator) is appended afterwards, matching api/office_documents.py.
    assert len(content.split(pdf_documents.PDF_PREVIEW_TRUNCATED_NOTICE)[0].rstrip()) <= 40
    assert pdf_documents.PDF_PREVIEW_TRUNCATED_NOTICE in content


def test_extract_pdf_text_reports_blank_document_without_crashing():
    content, truncated = extract_pdf_text(_pdf_bytes(""))

    assert content == pdf_documents.PDF_EMPTY_NOTICE
    assert truncated is False


def test_extract_pdf_text_fails_closed_on_encrypted_pdf():
    with pytest.raises(ValueError) as excinfo:
        extract_pdf_text(_pdf_bytes("secret", encrypt_with="owner"))

    assert "PDF" in str(excinfo.value)


def test_extract_pdf_text_raises_import_error_with_install_hint_when_parser_missing(monkeypatch):
    def fail_load():
        raise pdf_documents._pdf_dependency_import_error()

    monkeypatch.setattr(pdf_documents, "_load_pdf_reader", fail_load)

    with pytest.raises(ImportError) as excinfo:
        extract_pdf_text(_pdf_bytes("alpha"))

    assert str(excinfo.value) == pdf_documents.PDF_DEPENDENCY_HINT
    assert "pypdf" in pdf_documents.PDF_DEPENDENCY_HINT


def test_preview_pdf_document_payload_shape():
    payload = preview_pdf_document("report.pdf", _pdf_bytes("alpha"))

    assert payload["path"] == "report.pdf"
    assert payload["size"] > 0
    assert payload["preview_kind"] == pdf_documents.PDF_PREVIEW_KIND
    assert payload["pdf_format"] == "pdf"
    assert payload["render_mode"] == pdf_documents.PDF_RENDER_MODE
    assert payload["lines"] == payload["content"].count("\n") + 1
    # PDFs are read-only by nature: never claim an edit surface.
    assert payload["editable"] is False
    assert payload["edit_blocked_reason"]


def test_preview_pdf_document_rejects_unsupported_extension():
    with pytest.raises(ValueError):
        preview_pdf_document("report.docx", b"whatever")


def test_upload_sidecar_writes_markdown_for_pdf_uploads(tmp_path):
    from api.upload import _write_office_upload_sidecar

    workspace = tmp_path / "ws"
    workspace.mkdir()
    dest = workspace / "report.pdf"
    dest.write_bytes(_pdf_bytes("alpha page"))

    sidecar = _write_office_upload_sidecar(workspace, dest, dest.read_bytes())

    assert sidecar is not None
    assert "error" not in sidecar
    assert sidecar["preview_kind"] == pdf_documents.PDF_PREVIEW_KIND
    assert sidecar["office_format"] == "pdf"
    sidecar_path = Path(sidecar["path"])
    assert sidecar_path.name == "report.pdf.md"
    body = sidecar_path.read_text(encoding="utf-8")
    assert "alpha page" in body
    assert "Page 1" in body


def test_upload_sidecar_returns_error_and_leaves_no_partial_file_for_corrupt_pdf(tmp_path):
    from api.upload import _write_office_upload_sidecar

    workspace = tmp_path / "ws"
    workspace.mkdir()
    dest = workspace / "broken.pdf"
    dest.write_bytes(b"%PDF-1.4\nnot really a pdf")

    sidecar = _write_office_upload_sidecar(workspace, dest, dest.read_bytes())

    assert sidecar is not None
    assert "error" in sidecar
    assert not (workspace / "broken.pdf.md").exists()


def test_upload_sidecar_ignores_non_office_and_non_pdf_extensions(tmp_path):
    from api.upload import _write_office_upload_sidecar

    workspace = tmp_path / "ws"
    workspace.mkdir()
    for name in ("notes.txt", "archive.zip", "legacy.doc"):
        dest = workspace / name
        dest.write_bytes(b"payload")

        assert _write_office_upload_sidecar(workspace, dest, b"payload") is None


def test_upload_sidecar_does_not_overwrite_an_existing_sidecar(tmp_path):
    from api.upload import _write_office_upload_sidecar

    workspace = tmp_path / "ws"
    workspace.mkdir()
    dest = workspace / "report.pdf"
    dest.write_bytes(_pdf_bytes("alpha"))
    existing = workspace / "report.pdf.md"
    existing.write_text("handwritten notes", encoding="utf-8")

    sidecar = _write_office_upload_sidecar(workspace, dest, dest.read_bytes())

    assert sidecar is not None and "error" in sidecar
    assert existing.read_text(encoding="utf-8") == "handwritten notes"


def test_pdf_client_iframe_preview_is_unchanged():
    workspace_js = (Path(__file__).resolve().parents[1] / "static" / "workspace.js").read_text(encoding="utf-8")

    # The server-side extractor is additive: the browser PDF preview keeps
    # rendering the real document in the iframe (visual fidelity), and the
    # sidecar gives the agent the text.
    assert "showPreview('pdf')" in workspace_js
    pdf_exts = re.search(r"const\s+PDF_EXTS\s*=\s*new Set\(\[(.*?)\]\);", workspace_js, re.S)
    assert pdf_exts is not None
    assert "'.pdf'" in pdf_exts.group(1)
    download_exts = re.search(r"const\s+DOWNLOAD_EXTS\s*=\s*new Set\(\[(.*?)\]\);", workspace_js, re.S)
    assert download_exts is not None
    assert "'.pdf'" not in download_exts.group(1)


def test_pdf_dependency_is_documented_as_optional():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    dev_requirements = (root / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "pypdf" in requirements
    assert "pypdf" in dev_requirements