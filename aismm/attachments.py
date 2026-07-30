"""Turning an uploaded file into something a text model can read.

An instruction can carry files — a brand guide PDF, a price list, a source
document, a reference photo. The textual model cannot open a file, so text is
extracted **once at upload** and stored on the row: every run then gets it for
free, with no per-run parsing and no dependency on the file still being readable.

Supported for text extraction: PDF (via ``pypdf``), and anything plain
(``.txt``, ``.md``, ``.csv``, ``.json``, ``.html``…). Images extract no text —
they are useful as ``reference`` attachments for the image/video generators
instead. Office formats (docx/xlsx) are deliberately out: they would pull in
another dependency for a case that "export to PDF" already covers.

Extraction is best-effort and never fatal: a file that cannot be parsed is still
stored, still downloadable, and simply has no text.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("aismm.attachments")

# Enough for a real brand guide, bounded so one upload cannot dominate a prompt.
MAX_TEXT_CHARS = 20_000
# What we put in the kickoff; the agent calls read_attachment for the rest.
KICKOFF_EXCERPT_CHARS = 1_500

TEXTUAL_TYPES = {
    "text/plain", "text/markdown", "text/csv", "text/html", "application/json",
    "application/xml", "text/xml", "text/x-python", "application/x-yaml", "text/yaml",
}
TEXTUAL_SUFFIXES = {"txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml",
                    "html", "htm", "log", "srt", "vtt"}


def is_pdf(content_type: str, filename: str) -> bool:
    return content_type == "application/pdf" or filename.lower().endswith(".pdf")


def is_textual(content_type: str, filename: str) -> bool:
    if content_type in TEXTUAL_TYPES or content_type.startswith("text/"):
        return True
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return suffix in TEXTUAL_SUFFIXES


def extract_text(data: bytes, content_type: str = "", filename: str = "") -> tuple[str, str]:
    """Return ``(text, note)``. ``note`` explains an empty result."""
    if is_pdf(content_type, filename):
        return _extract_pdf(data)
    if is_textual(content_type, filename):
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception as exc:  # noqa: BLE001
            return "", f"could not decode: {exc}"
        return text[:MAX_TEXT_CHARS], ("truncated" if len(text) > MAX_TEXT_CHARS else "")
    if content_type.startswith("image/"):
        return "", "image — use it as a reference attachment, not as text"
    if content_type.startswith("video/"):
        return "", "video — no text extracted"
    return "", f"no text extractor for {content_type or 'this file type'}"


def _extract_pdf(data: bytes) -> tuple[str, str]:
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except ImportError:
        return "", ("pypdf is not installed — run pip install -r requirements.txt to read "
                    "PDF attachments")
    try:
        reader = PdfReader(BytesIO(data))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
            if sum(len(p) for p in pages) > MAX_TEXT_CHARS:
                break
        text = "\n\n".join(pages).strip()
    except Exception as exc:  # noqa: BLE001 - a broken PDF must not fail the upload
        logger.warning("PDF text extraction failed: %s", exc)
        return "", f"could not read this PDF: {exc}"
    if not text:
        return "", "this PDF has no extractable text (it may be a scan — OCR is not done here)"
    return text[:MAX_TEXT_CHARS], ("truncated" if len(text) > MAX_TEXT_CHARS else "")


def describe(files: list) -> str:
    """A compact listing of an instruction's attachments for the kickoff prompt."""
    if not files:
        return ""
    lines = []
    for item in files:
        bits = [f"- {item.filename} ({item.purpose.value}"]
        if item.content_type:
            bits.append(f", {item.content_type}")
        bits.append(")")
        line = "".join(bits)
        if item.note:
            line += f" — {item.note}"
        if item.purpose.value == "reference" and item.is_image:
            line += f"\n  reference image, asset_path: {item.asset_path}"
        elif item.text:
            excerpt = item.text[:KICKOFF_EXCERPT_CHARS].strip()
            more = (f"\n  …{len(item.text) - len(excerpt)} more characters — call "
                    f"read_attachment(\"{item.filename}\") for the rest"
                    if len(item.text) > len(excerpt) else "")
            line += f"\n  {excerpt}{more}"
        lines.append(line)
    return "\n".join(lines)
