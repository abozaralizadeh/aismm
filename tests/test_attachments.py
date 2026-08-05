"""Instruction attachments, and the prompt shown on a run's detail page.

Two things a run needs to be debuggable and steerable: the exact prompt it was
given (stored on the Run, since the instruction may have changed since), and the
files a human attached to the instruction — a 'context' PDF/image is sent to the
model directly (native file input), a 'reference' image is handed to the
image/video generators, and anything else falls back to our own extracted text.
"""
import asyncio
import io

import pytest

from aismm import assets, attachments
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


# --- native file input: PDFs/images go to the model directly, not just as text ------- #

def _stored_file(dash, instruction, data, content_type, purpose=AttachmentPurpose.context,
                 filename="voice.pdf", note=""):
    """A file with real bytes on disk, like one that went through the upload route."""
    path = assets.save_bytes(data, filename.rsplit(".", 1)[-1])
    return InstructionFile(instruction_id=instruction.id, filename=filename,
                           content_type=content_type, purpose=purpose, asset_path=path,
                           size_bytes=len(data), note=note)


def test_goes_natively_accepts_pdf_and_common_images_not_plain_text(instruction):
    pdf = InstructionFile(instruction_id=instruction.id, filename="a.pdf",
                          content_type="application/pdf")
    png = InstructionFile(instruction_id=instruction.id, filename="b.png",
                          content_type="image/png")
    txt = InstructionFile(instruction_id=instruction.id, filename="c.txt",
                          content_type="text/plain")
    assert attachments.goes_natively(pdf)
    assert attachments.goes_natively(png)
    assert not attachments.goes_natively(txt)


def test_a_context_pdf_becomes_an_input_file_part(dash, instruction):
    data = text_pdf()
    f = _stored_file(dash, instruction, data, "application/pdf")
    parts, attached, fell_back = attachments.build_content_parts([f])
    assert attached == ["voice.pdf"] and fell_back == []
    assert parts[0]["type"] == "input_file"
    assert parts[0]["filename"] == "voice.pdf"
    assert parts[0]["file_data"].startswith("data:application/pdf;base64,")


def test_a_context_image_becomes_an_input_image_part(dash, instruction):
    data = png_bytes()
    f = _stored_file(dash, instruction, data, "image/png", filename="diagram.png")
    parts, attached, fell_back = attachments.build_content_parts([f])
    assert attached == ["diagram.png"]
    assert parts[0]["type"] == "input_image"
    assert parts[0]["image_url"].startswith("data:image/png;base64,")


def test_a_reference_image_is_never_sent_to_the_text_model(dash, instruction):
    """Reference images stay dedicated to generate_image/video — only context goes natively."""
    f = _stored_file(dash, instruction, png_bytes(), "image/png",
                     purpose=AttachmentPurpose.reference, filename="palette.png")
    parts, attached, fell_back = attachments.build_content_parts([f])
    assert parts == [] and attached == [] and fell_back == []


def test_a_missing_asset_falls_back_instead_of_raising(instruction):
    f = InstructionFile(instruction_id=instruction.id, filename="gone.pdf",
                        content_type="application/pdf", asset_path="/no/such/file.pdf")
    parts, attached, fell_back = attachments.build_content_parts([f])
    assert parts == [] and fell_back == ["gone.pdf"]


def test_an_oversized_file_falls_back_to_text(dash, instruction, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_INLINE_BYTES", 10)
    f = _stored_file(dash, instruction, text_pdf(), "application/pdf")
    parts, attached, fell_back = attachments.build_content_parts([f])
    assert parts == [] and fell_back == ["voice.pdf"]


def test_the_request_budget_is_shared_across_files(dash, instruction, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_INLINE_BYTES", 10_000)
    monkeypatch.setattr(attachments, "MAX_INLINE_TOTAL_BYTES", 12_000)
    files = [_stored_file(dash, instruction, b"x" * 8_000, "application/pdf",
                          filename=f"f{i}.pdf")
            for i in range(3)]
    parts, attached, fell_back = attachments.build_content_parts(files)
    assert len(attached) == 1 and len(fell_back) == 2


def test_a_context_pdf_shows_as_attached_directly_in_the_kickoff(dash, instruction):
    f = _stored_file(dash, instruction, text_pdf(), "application/pdf")
    kickoff = build_kickoff(account=Account(platform=PlatformName.instagram),
                            instruction=instruction, platform_caps=CAPS, files=[f])
    assert "voice.pdf (context" in kickoff
    assert "attached directly to this message" in kickoff
    assert "Tone: warm, clinical." not in kickoff  # not dumped as text — it's a real file part


def test_build_agent_input_is_plain_text_with_no_attachable_files(instruction):
    agent_input, attached, fell_back = attachments.build_agent_input("BRIEF:\nhello", [])
    assert agent_input == "BRIEF:\nhello"
    assert attached == [] and fell_back == []


def test_build_agent_input_wraps_the_kickoff_with_native_parts(dash, instruction):
    f = _stored_file(dash, instruction, text_pdf(), "application/pdf")
    agent_input, attached, fell_back = attachments.build_agent_input("BRIEF:\nhello", [f])
    assert attached == ["voice.pdf"]
    assert isinstance(agent_input, list)
    content = agent_input[0]["content"]
    assert content[0] == {"type": "input_text", "text": "BRIEF:\nhello"}
    assert content[1]["type"] == "input_file"


@pytest.mark.parametrize("message,expected", [
    ("Unsupported parameter: 'input_file' is not supported with this model", True),
    ("invalid_type: image_url must be a string", True),
    ("Rate limit reached for requests", False),
    ("500 internal server error", False),
])
def test_looks_like_unsupported_file_input(message, expected):
    assert attachments.looks_like_unsupported_file_input(message) is expected


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


# --- attaching while CREATING an instruction ----------------------------------------- #
# Reported: the upload section only appeared once the instruction existed, so the
# first thing you wanted to give it had to wait for a second visit. An attachment
# needs an instruction id and there is none until the row is saved — so the file
# rides along with the save and is attached immediately afterwards.

def test_the_new_instruction_form_offers_a_file(dash):
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert 'enctype="multipart/form-data"' in page
    assert 'type="file"' in page
    assert 'name="purpose"' in page


def test_a_file_chosen_while_creating_is_attached(dash, store):
    dash.test_client().post(
        "/instructions",
        data={"name": "Comicbook", "publish_mode": "dry_run", "media_pref": "auto",
              "purpose": "context", "note": "brand voice",
              "file": (io.BytesIO(text_pdf()), "voice.pdf")},
        content_type="multipart/form-data", follow_redirects=True)

    instruction = store.list_instructions()[0]
    files = store.list_instruction_files(instruction.id)
    assert [f.filename for f in files] == ["voice.pdf"]
    assert files[0].purpose is AttachmentPurpose.context
    assert files[0].note == "brand voice"
    assert "never alarmist" in files[0].text        # same extraction as the edit page


def test_the_purpose_is_honoured_on_creation(dash, store):
    dash.test_client().post(
        "/instructions",
        data={"name": "Comicbook", "publish_mode": "dry_run", "media_pref": "auto",
              "purpose": "reference",
              "file": (io.BytesIO(png_bytes()), "sheet.png")},
        content_type="multipart/form-data", follow_redirects=True)
    files = store.list_instruction_files(store.list_instructions()[0].id)
    assert files[0].purpose is AttachmentPurpose.reference


def test_creating_WITHOUT_a_file_still_works(dash, store):
    """The field is optional; an empty file part must not fail the save."""
    response = dash.test_client().post(
        "/instructions",
        data={"name": "No file", "publish_mode": "dry_run", "media_pref": "auto"},
        content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    instruction = store.list_instructions()[0]
    assert instruction.name == "No file"
    assert store.list_instruction_files(instruction.id) == []


def test_attaching_on_creation_lands_on_the_edit_page(dash, store):
    """So you can see what was attached and add more, rather than being sent to
    the list with no confirmation of the file."""
    response = dash.test_client().post(
        "/instructions",
        data={"name": "Comicbook", "publish_mode": "dry_run", "media_pref": "auto",
              "file": (io.BytesIO(text_pdf()), "voice.pdf")},
        content_type="multipart/form-data", follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "voice.pdf" in page
    assert f'value="{store.list_instructions()[0].id}"' in page


def test_an_oversized_file_is_refused_but_the_instruction_is_saved(dash, store):
    """Losing the whole instruction over an attachment would be worse."""
    huge = io.BytesIO(b"x" * (app_module.MAX_UPLOAD_BYTES + 1))
    response = dash.test_client().post(
        "/instructions",
        data={"name": "Comicbook", "publish_mode": "dry_run", "media_pref": "auto",
              "file": (huge, "huge.pdf")},
        content_type="multipart/form-data", follow_redirects=True)
    assert "the limit is" in response.get_data(as_text=True)
    instruction = store.list_instructions()[0]
    assert instruction.name == "Comicbook"
    assert store.list_instruction_files(instruction.id) == []
