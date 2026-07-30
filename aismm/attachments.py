"""Getting an uploaded file in front of the model.

**Files go to the model as files.** The Responses API takes a PDF as an
``input_file`` part and an image as ``input_image``, and for a PDF the service
puts *both the extracted text and a rendered image of every page* into the
model's context — so tables, diagrams, layout and screenshots all survive.
Extracting text ourselves would throw all of that away.

Extraction is kept for two narrower jobs:

* **plain text formats** (``.txt``, ``.md``, ``.csv``, ``.json``) — inlining the
  text is lossless and far cheaper than base64-encoding the file;
* **a fallback** when native file input is refused. Support depends on the
  deployment and api-version, and a model without vision cannot take a PDF at
  all, so a 400 must not mean the attachment silently disappears.

Historical note (the reason this module exists at all):

an instruction can carry files — a brand guide PDF, a price list, a reference
photo — and text was originally extracted at upload because "the textual model
cannot open a file". That is no longer true, so the file itself is sent.

Extraction stays best-effort and never fatal: a file that cannot be parsed is
still stored, still sent natively, and simply has no fallback text.
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
    _, attached_natively, fell_back = build_content_parts(files)
    attached_set, fell_back_set = set(attached_natively), set(fell_back)
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
        elif item.filename in attached_set:
            line += "\n  attached directly to this message — you can already read/see it"
        elif item.text:
            excerpt = item.text[:KICKOFF_EXCERPT_CHARS].strip()
            more = (f"\n  …{len(item.text) - len(excerpt)} more characters — call "
                    f"read_attachment(\"{item.filename}\") for the rest"
                    if len(item.text) > len(excerpt) else "")
            line += f"\n  {excerpt}{more}"
        elif item.filename in fell_back_set:
            line += "\n  too large to attach directly — call read_attachment for its extracted text"
        lines.append(line)
    return "\n".join(lines)


# --- native file input ------------------------------------------------------- #
# Base64 inflates by 4/3 and Azure caps a request at 50MB, so inlining is
# budgeted. A file that doesn't fit falls back to its extracted text, and the
# prompt says which — a silently dropped attachment is much worse than a noted one.
MAX_INLINE_BYTES = 12 * 1024 * 1024          # per file, before encoding
MAX_INLINE_TOTAL_BYTES = 28 * 1024 * 1024    # across one request

NATIVE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def goes_natively(file) -> bool:
    """Should this file be sent to the model as a file rather than as text?

    PDFs and images yes — the model reads layout and pixels we cannot. Plain text
    no: inlining it is lossless and much cheaper than base64.
    """
    if is_textual(file.content_type, file.filename) and not is_pdf(file.content_type,
                                                                   file.filename):
        return False
    return is_pdf(file.content_type, file.filename) or file.content_type in NATIVE_IMAGE_TYPES


def build_content_parts(files: list) -> tuple[list[dict], list[str], list[str]]:
    """Turn attachments into Responses API content parts.

    Returns ``(parts, attached_natively, fell_back)`` — the second and third are
    filenames, so the text half of the prompt can say what the model can actually
    see and what it must read as text instead.
    """
    from .assets import read_bytes

    parts: list[dict] = []
    attached: list[str] = []
    fell_back: list[str] = []
    budget = MAX_INLINE_TOTAL_BYTES

    for item in files or []:
        if getattr(item.purpose, "value", item.purpose) != "context":
            continue  # reference images stay dedicated to generate_image/video, not the text model
        if not goes_natively(item):
            continue
        if item.size_bytes and item.size_bytes > MAX_INLINE_BYTES:
            logger.info("%s is %d bytes — too big to inline; using extracted text",
                        item.filename, item.size_bytes)
            fell_back.append(item.filename)
            continue
        try:
            data = read_bytes(item.asset_path)
        except Exception as exc:  # noqa: BLE001 - a missing file must not kill the run
            logger.warning("Could not read attachment %s: %s", item.filename, exc)
            fell_back.append(item.filename)
            continue
        if len(data) > budget:
            logger.info("Request budget exhausted before %s; using extracted text",
                        item.filename)
            fell_back.append(item.filename)
            continue
        budget -= len(data)

        import base64

        encoded = base64.b64encode(data).decode()
        if is_pdf(item.content_type, item.filename):
            parts.append({"type": "input_file", "filename": item.filename,
                          "file_data": f"data:application/pdf;base64,{encoded}"})
        else:
            parts.append({"type": "input_image",
                          "image_url": f"data:{item.content_type};base64,{encoded}",
                          "detail": "auto"})
        attached.append(item.filename)

    return parts, attached, fell_back


def looks_like_unsupported_file_input(error: str) -> bool:
    """Does this model error mean the deployment won't take file/image parts?

    Support varies by deployment and api-version, and a model without vision
    refuses outright. When that happens we resend with text only rather than
    losing the attachment.
    """
    low = (error or "").lower()
    markers = ("input_file", "input_image", "unsupported", "not supported", "invalid_type",
               "unknown parameter", "image_url", "file_data", "invalid value")
    return any(marker in low for marker in markers)


def build_agent_input(kickoff_text: str,
                       files: list | None = None) -> tuple[object, list[str], list[str]]:
    """The first turn to hand ``Runner.run`` — plain text, or text + native file parts.

    Returns ``(agent_input, attached_natively, fell_back)``: the last two are
    filenames, for the caller to log what actually reached the model versus what
    fell back to extracted text (too large, unreadable, or no attachments at all).
    """
    parts, attached, fell_back = build_content_parts(files or [])
    if not parts:
        return kickoff_text, attached, fell_back
    message = [{"role": "user", "content": [{"type": "input_text", "text": kickoff_text}, *parts]}]
    return message, attached, fell_back
