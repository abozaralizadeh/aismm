"""Instruction attachments, and the prompt shown on a run's detail page.

Two things a run needs to be debuggable and steerable: the exact prompt it was
given (stored on the Run, since the instruction may have changed since), and the
files a human attached to the instruction — read as text by the model, or handed
to the image/video generators as references.
"""
import asyncio
import io

import pytest

from aismm import attachments
from aismm.agent.prompts import build_kickoff
from aismm.dashboard import app as app_module
from aismm.models import (
    Account, AttachmentPurpose, Instruction, InstructionFile, PlatformName, Run,
)
from aismm.platforms.base import Capabilities
from aismm.tools.memory_tool import perform_read_attachment

CAPS = Capabilities(supports_text=True, supports_image=True, supports_video=True,
                    needs_public_media_url=False, default_orientation="portrait",
                    caption_limit=2200)


def text_pdf(body: str = "Tone: warm, clinical, never alarmist.") -> bytes:
    """A minimal PDF with a real text layer (Pillow's PDF export rasterizes)."""
    stream = f"BT /F1 14 Tf 40 700 Td ({body}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources "
        b"<< /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n").encode()
    return bytes(out)


def png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (14, 124, 123)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture()
def dash(store, monkeypatch, tmp_path):
    import dataclasses

    from aismm import assets, config as config_module

    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    application = app_module.create_app()
    application.secret_key = "test"
    return application


@pytest.fixture()
def instruction(store):
    return store.upsert_instruction(Instruction(name="Comicbook", brief="One panel a day."))


# --- text extraction ---------------------------------------------------------------- #

def test_a_pdf_with_a_text_layer_is_extracted():
    text, note = attachments.extract_text(text_pdf(), "application/pdf", "voice.pdf")
    assert "never alarmist" in text
    assert note == ""


def test_a_scanned_pdf_says_why_it_is_empty():
    """Pillow-exported PDFs rasterize their text — a real scan behaves the same."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(buffer, "PDF")
    text, note = attachments.extract_text(buffer.getvalue(), "application/pdf", "scan.pdf")
    assert text == ""
    assert "no extractable text" in note


@pytest.mark.parametrize("filename,content_type", [
    ("notes.txt", "text/plain"), ("readme.md", "text/markdown"),
    ("prices.csv", "text/csv"), ("data.json", "application/json"),
])
def test_plain_files_are_read_directly(filename, content_type):
    text, note = attachments.extract_text(b"warm and clinical", content_type, filename)
    assert text == "warm and clinical"


def test_an_image_extracts_no_text_but_says_what_it_is_for():
    text, note = attachments.extract_text(png_bytes(), "image/png", "palette.png")
    assert text == ""
    assert "reference" in note


def test_extraction_is_bounded():
    huge = b"x" * (attachments.MAX_TEXT_CHARS * 2)
    text, note = attachments.extract_text(huge, "text/plain", "big.txt")
    assert len(text) == attachments.MAX_TEXT_CHARS
    assert note == "truncated"


def test_an_unknown_type_is_reported_not_crashed():
    text, note = attachments.extract_text(b"\x00\x01", "application/zip", "a.zip")
    assert text == "" and "no text extractor" in note


def test_a_corrupt_pdf_does_not_raise():
    text, note = attachments.extract_text(b"%PDF-1.4 garbage", "application/pdf", "bad.pdf")
    assert text == "" and note


# --- storage ------------------------------------------------------------------------ #

def test_attachment_round_trip(store, instruction):
    record = store.add_instruction_file(InstructionFile(
        instruction_id=instruction.id, filename="voice.pdf", content_type="application/pdf",
        text="warm and clinical", size_bytes=653))
    listed = store.list_instruction_files(instruction.id)
    assert [f.filename for f in listed] == ["voice.pdf"]
    assert store.get_instruction_file(record.id).text == "warm and clinical"


def test_attachments_are_scoped_to_their_instruction(store, instruction):
    other = store.upsert_instruction(Instruction(name="Other"))
    store.add_instruction_file(InstructionFile(instruction_id=instruction.id, filename="a.txt"))
    store.add_instruction_file(InstructionFile(instruction_id=other.id, filename="b.txt"))
    assert len(store.list_instruction_files(instruction.id)) == 1


def test_deleting_an_instruction_removes_its_files(store, instruction):
    store.add_instruction_file(InstructionFile(instruction_id=instruction.id, filename="a.txt"))
    store.delete_instruction(instruction.id)
    assert store.list_instruction_files(instruction.id) == []


def test_delete_one_attachment(store, instruction):
    record = store.add_instruction_file(
        InstructionFile(instruction_id=instruction.id, filename="a.txt"))
    store.delete_instruction_file(record.id)
    assert store.get_instruction_file(record.id) is None


# --- the prompt sees them ------------------------------------------------------------ #

def _files(instruction):
    return [
        InstructionFile(instruction_id=instruction.id, filename="voice.pdf",
                        content_type="application/pdf", purpose=AttachmentPurpose.context,
                        text="Tone: warm, clinical. " * 200, note="brand voice"),
        InstructionFile(instruction_id=instruction.id, filename="palette.png",
                        content_type="image/png", purpose=AttachmentPurpose.reference,
                        asset_path="/assets/palette.png", note="use this teal"),
    ]


def test_context_text_is_excerpted_into_the_kickoff(instruction):
    kickoff = build_kickoff(account=Account(platform=PlatformName.instagram),
                            instruction=instruction, platform_caps=CAPS,
                            files=_files(instruction))
    assert "FILES ATTACHED TO THIS INSTRUCTION" in kickoff
    assert "voice.pdf (context" in kickoff
    assert "Tone: warm, clinical." in kickoff
    assert "read_attachment" in kickoff          # how to get the rest


def test_a_long_attachment_is_not_dumped_whole_into_the_prompt(instruction):
    files = _files(instruction)
    kickoff = build_kickoff(account=Account(platform=PlatformName.instagram),
                            instruction=instruction, platform_caps=CAPS, files=files)
    assert len(files[0].text) > attachments.KICKOFF_EXCERPT_CHARS
    assert files[0].text not in kickoff          # excerpt only
    assert "more characters" in kickoff


def test_a_reference_image_exposes_its_asset_path(instruction):
    kickoff = build_kickoff(account=Account(platform=PlatformName.instagram),
                            instruction=instruction, platform_caps=CAPS,
                            files=_files(instruction))
    assert "reference image, asset_path: /assets/palette.png" in kickoff


def test_the_human_note_reaches_the_prompt(instruction):
    kickoff = build_kickoff(account=Account(platform=PlatformName.instagram),
                            instruction=instruction, platform_caps=CAPS,
                            files=_files(instruction))
    assert "use this teal" in kickoff


def test_no_attachments_adds_no_section(instruction):
    kickoff = build_kickoff(account=Account(platform=PlatformName.instagram),
                            instruction=instruction, platform_caps=CAPS, files=[])
    assert "FILES ATTACHED" not in kickoff


def test_read_attachment_returns_the_full_text(store, instruction):
    store.add_instruction_file(InstructionFile(
        instruction_id=instruction.id, filename="voice.pdf", text="x" * 5000))
    result = asyncio.run(perform_read_attachment(
        {"store": store, "instruction": instruction}, "voice.pdf"))
    assert result["chars"] == 5000


def test_read_attachment_is_case_insensitive(store, instruction):
    store.add_instruction_file(InstructionFile(
        instruction_id=instruction.id, filename="Voice.PDF", text="hello"))
    result = asyncio.run(perform_read_attachment(
        {"store": store, "instruction": instruction}, "voice.pdf"))
    assert result["text"] == "hello"


def test_read_attachment_names_what_is_available_when_wrong(store, instruction):
    store.add_instruction_file(InstructionFile(
        instruction_id=instruction.id, filename="voice.pdf", text="x"))
    result = asyncio.run(perform_read_attachment(
        {"store": store, "instruction": instruction}, "nope.txt"))
    assert result["error"] == "not_found" and "voice.pdf" in result["message"]


def test_read_attachment_on_an_image_points_at_the_reference_path(store, instruction):
    store.add_instruction_file(InstructionFile(
        instruction_id=instruction.id, filename="p.png", content_type="image/png",
        purpose=AttachmentPurpose.reference, asset_path="/assets/p.png"))
    result = asyncio.run(perform_read_attachment(
        {"store": store, "instruction": instruction}, "p.png"))
    assert "/assets/p.png" in result["message"]


# --- upload + delete through the dashboard ------------------------------------------- #

def test_uploading_a_pdf_extracts_and_stores_it(dash, store, instruction):
    response = dash.test_client().post(
        f"/instructions/{instruction.id}/files",
        data={"file": (io.BytesIO(text_pdf()), "voice.pdf"), "purpose": "context",
              "note": "brand voice"},
        content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200

    files = store.list_instruction_files(instruction.id)
    assert len(files) == 1
    assert files[0].filename == "voice.pdf"
    assert "never alarmist" in files[0].text
    assert files[0].purpose is AttachmentPurpose.context
    assert files[0].size_bytes > 0


def test_uploading_a_reference_image(dash, store, instruction):
    dash.test_client().post(
        f"/instructions/{instruction.id}/files",
        data={"file": (io.BytesIO(png_bytes()), "palette.png"), "purpose": "reference"},
        content_type="multipart/form-data")
    stored = store.list_instruction_files(instruction.id)[0]
    assert stored.purpose is AttachmentPurpose.reference
    assert stored.is_image and stored.asset_path


def test_the_upload_filename_is_sanitised(dash, store, instruction):
    dash.test_client().post(
        f"/instructions/{instruction.id}/files",
        data={"file": (io.BytesIO(b"hi"), "../../etc/passwd.txt"), "purpose": "context"},
        content_type="multipart/form-data")
    assert "/" not in store.list_instruction_files(instruction.id)[0].filename


def test_an_oversized_upload_is_refused(dash, store, instruction, monkeypatch):
    big = io.BytesIO(b"x" * (26 * 1024 * 1024))
    dash.test_client().post(
        f"/instructions/{instruction.id}/files",
        data={"file": (big, "huge.txt"), "purpose": "context"},
        content_type="multipart/form-data")
    assert store.list_instruction_files(instruction.id) == []


def test_uploading_to_an_unknown_instruction_is_404(dash):
    response = dash.test_client().post(
        "/instructions/nope/files",
        data={"file": (io.BytesIO(b"hi"), "a.txt")}, content_type="multipart/form-data")
    assert response.status_code == 404


def test_removing_an_attachment_from_the_form(dash, store, instruction):
    record = store.add_instruction_file(
        InstructionFile(instruction_id=instruction.id, filename="a.txt"))
    dash.test_client().post(f"/files/{record.id}/delete")
    assert store.get_instruction_file(record.id) is None


def test_the_edit_page_lists_attachments(dash, store, instruction):
    store.add_instruction_file(InstructionFile(
        instruction_id=instruction.id, filename="voice.pdf", text="abc",
        content_type="application/pdf", asset_path="/a/voice.pdf"))
    page = dash.test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    assert "voice.pdf" in page
    assert 'enctype="multipart/form-data"' in page


# --- the run detail shows the prompt ------------------------------------------------- #

def test_the_run_stores_the_prompt_it_was_given(store, instruction):
    run = store.add_run(Run(instruction_id=instruction.id, account_id="a",
                            prompt="BRIEF:\nOne panel a day."))
    assert "One panel a day." in store.get_run(run.id).prompt


def test_the_detail_page_shows_every_prompt_part(dash, store, instruction):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id="1"), access_token="t")
    store.set_memory(instruction.id, "CURRENT POSITION: Panel 4")
    store.set_note(instruction.id, "Prefer recent panels.")
    store.add_instruction_file(InstructionFile(
        instruction_id=instruction.id, filename="voice.pdf", text="abc"))
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            prompt="KICKOFF TEXT HERE"))

    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert "KICKOFF TEXT HERE" in page
    assert "CURRENT POSITION: Panel 4" in page
    assert "Prefer recent panels." in page
    assert "One panel a day." in page               # the brief
    assert "voice.pdf" in page                      # attachments
    assert "<details" in page                       # expandable, not a wall of text


def test_an_older_run_without_a_prompt_says_so(dash, store, instruction):
    run = store.add_run(Run(instruction_id=instruction.id, account_id="a"))
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert "predates prompt capture" in page
