"""User data deletion: Meta's callback, TikTok's webhook, the manual path, the purge.

Meta: "Apps that access user data must provide a way for users to request that their
data be deleted", satisfied by a Data Deletion Request Callback or an instructions page.
Both exist now (aismm/data_deletion.py). Pinned here, without a network:

* a Meta ``signed_request`` is accepted only when its HMAC-SHA256 matches a configured
  Meta app's secret, and the answer is the JSON Meta specifies (``url`` +
  ``confirmation_code``);
* a TikTok webhook is accepted only with a valid, fresh ``TikTok-Signature``;
* the purge removes the account (tokens and meta), its runs and staged items, and its
  place in every instruction, and leaves every other account alone;
* an account connected before the Facebook user id was recorded is still matched,
  through ``/debug_token``;
* the platform user id is never stored, only a hash;
* the endpoints are public, a restart cannot strand a request, and the connect now
  records the id the callback will name.
"""
import base64
import dataclasses
import hashlib
import hmac
import json
import time

import pytest

from aismm import data_deletion as dd
from aismm.models import (
    Account, DeletionRequest, Instruction, PlatformApp, PlatformName, Run, RunStatus,
    StagedPost,
)

META_SECRET = "meta-app-secret"
TIKTOK_SECRET = "tiktok-client-secret"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signed_request(payload: dict, secret: str = META_SECRET) -> str:
    """Exactly how Meta builds one: sig = HMAC-SHA256(secret, payload segment)."""
    body = _b64(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{_b64(sig)}.{body}"


def tiktok_header(body: bytes, secret: str = TIKTOK_SECRET, stamp: int | None = None) -> str:
    stamp = int(time.time()) if stamp is None else stamp
    sig = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={stamp},s={sig}"


META_PAYLOAD = {"algorithm": "HMAC-SHA256", "expires": 0, "issued_at": 0, "user_id": "fb-123"}


# --- signatures ----------------------------------------------------------------------- #

def test_a_meta_signed_request_from_a_known_app_is_accepted():
    payload, app_id = dd.parse_meta_signed_request(
        signed_request(META_PAYLOAD), [("other-app", "nope"), ("meta-app", META_SECRET)])
    assert payload["user_id"] == "fb-123" and app_id == "meta-app"


@pytest.mark.parametrize("make", [
    lambda: signed_request(META_PAYLOAD, secret="someone-else"),            # wrong app
    lambda: signed_request({**META_PAYLOAD, "algorithm": "none"}),           # downgrade
    lambda: "not-a-signed-request",
    lambda: "",
    # a valid signature over one payload, attached to another
    lambda: signed_request(META_PAYLOAD).split(".")[0] + "."
    + _b64(json.dumps({**META_PAYLOAD, "user_id": "victim"}).encode()),
])
def test_anything_else_is_rejected(make):
    with pytest.raises(dd.InvalidSignature):
        dd.parse_meta_signed_request(make(), [("meta-app", META_SECRET)])


def test_tiktok_signature_rules():
    body = b'{"event":"authorization.removed","user_openid":"tt-1"}'
    secrets = [("tt-app", TIKTOK_SECRET)]
    assert dd.verify_tiktok_signature(tiktok_header(body), body, secrets) == "tt-app"
    for bad in (tiktok_header(body, secret="x"),                         # wrong app
                tiktok_header(body, stamp=int(time.time()) - 3600),      # replayed
                "", "t=abc,s="):
        with pytest.raises(dd.InvalidSignature):
            dd.verify_tiktok_signature(bad, body, secrets)
    with pytest.raises(dd.InvalidSignature):                             # body altered
        dd.verify_tiktok_signature(tiktok_header(body), body + b" ", secrets)


# --- the purge ------------------------------------------------------------------------- #

def _account(store, platform, external_id, *, fb_user=None, handle="h"):
    acct = Account(platform=platform, handle=handle, external_id=external_id)
    acct.set_meta({"provider_user_id": fb_user} if fb_user else {"page_id": "p"})
    return store.upsert_account(acct, access_token="tok", refresh_token="ref")


def _history(store, account, n_runs=2, n_staged=1):
    for _ in range(n_runs):
        store.add_run(Run(instruction_id="i", account_id=account.id, platform=account.platform,
                          status=RunStatus.published, caption="c"))
    for _ in range(n_staged):
        store.add_staged(StagedPost(instruction_id="i", account_id=account.id,
                                    platform=account.platform, caption="reply to a comment",
                                    target_excerpt="someone's comment"))


def test_a_meta_request_deletes_every_account_of_that_person_and_nothing_else(store):
    store.init()
    ig = _account(store, PlatformName.instagram, "ig-1", fb_user="fb-123")
    fb = _account(store, PlatformName.facebook, "page-1", fb_user="fb-123")
    other = _account(store, PlatformName.instagram, "ig-2", fb_user="fb-999")
    for a in (ig, fb, other):
        _history(store, a)
    instr = store.upsert_instruction(Instruction(name="I"))
    instr.set_account_ids([ig.id, other.id])
    store.upsert_instruction(instr)

    req = dd.submit(store, source="meta", user_id="fb-123", background=False)

    assert req.status == dd.COMPLETED
    assert (req.accounts_deleted, req.runs_deleted, req.staged_deleted) == (2, 4, 2)
    assert store.get_account(ig.id) is None and store.get_account(fb.id) is None
    assert store.get_account(other.id) is not None
    assert {r.account_id for r in store.list_runs(limit=100)} == {other.id}
    assert {s.account_id for s in store.list_staged(limit=100)} == {other.id}
    assert store.get_instruction(instr.id).account_ids == [other.id]


def test_the_user_id_is_never_stored(store):
    store.init()
    req = dd.submit(store, source="meta", user_id="fb-123", background=False)
    saved = store.get_deletion_request(req.id)
    assert "fb-123" not in json.dumps(saved.model_dump(), default=str)
    assert saved.user_ref == hashlib.sha256(b"meta:fb-123").hexdigest()


def test_an_account_connected_before_the_id_was_recorded_is_still_matched(store):
    store.init()
    legacy = _account(store, PlatformName.instagram, "ig-old")            # no fb user id
    unrelated = _account(store, PlatformName.facebook, "page-old")
    answers = {legacy.id: "fb-123", unrelated.id: "fb-555"}

    # The resolver stands in for Graph's /debug_token ("who granted this token?").
    req = store.upsert_deletion_request(DeletionRequest(source="meta",
                                                        user_ref=dd.user_ref("meta", "fb-123")))
    done = dd.process(store, req.id, resolve_meta_user=lambda a: answers.get(a.id, ""))
    assert done.status == dd.COMPLETED and done.accounts_deleted == 1
    assert store.get_account(legacy.id) is None
    # The unrelated account's id was learnt on the way and kept for next time.
    assert store.get_account(unrelated.id).meta["provider_user_id"] == "fb-555"


def test_nothing_found_is_a_completed_answer(store):
    store.init()
    _account(store, PlatformName.instagram, "ig-1", fb_user="fb-999")
    req = dd.submit(store, source="meta", user_id="fb-123", background=False)
    assert req.status == dd.NOTHING_FOUND and req.accounts_deleted == 0


def test_tiktok_matches_on_the_open_id(store):
    store.init()
    tt = _account(store, PlatformName.tiktok, "open-1")
    keep = _account(store, PlatformName.tiktok, "open-2")
    req = dd.submit(store, source="tiktok", user_id="open-1", background=False)
    assert req.status == dd.COMPLETED
    assert store.get_account(tt.id) is None and store.get_account(keep.id) is not None


def test_a_retried_callback_gets_the_same_code(store, monkeypatch):
    store.init()
    monkeypatch.setattr(dd, "_process_safely", lambda *_a: None)       # leave it "received"
    first = dd.submit(store, source="meta", user_id="fb-123")
    second = dd.submit(store, source="meta", user_id="fb-123")
    assert first.id == second.id


def test_a_restart_cannot_strand_a_request(store, monkeypatch):
    store.init()
    acct = _account(store, PlatformName.instagram, "ig-1", fb_user="fb-123")
    monkeypatch.setattr(dd, "_process_safely", lambda *_a: None)       # "process died"
    req = dd.submit(store, source="meta", user_id="fb-123")
    assert store.get_deletion_request(req.id).status == dd.RECEIVED
    assert dd.process_pending(store) == 1
    assert store.get_deletion_request(req.id).status == dd.COMPLETED
    assert store.get_account(acct.id) is None


def test_a_failure_is_recorded_and_retried(store, monkeypatch):
    store.init()
    acct = _account(store, PlatformName.instagram, "ig-1", fb_user="fb-123")
    real = store.delete_runs_for_account
    monkeypatch.setattr(store, "delete_runs_for_account",
                        lambda _id: (_ for _ in ()).throw(RuntimeError("storage down")))
    req = dd.submit(store, source="meta", user_id="fb-123", background=False)
    assert req.status == dd.FAILED and "retried" in req.detail
    monkeypatch.setattr(store, "delete_runs_for_account", real)
    dd.process_pending(store)
    assert store.get_deletion_request(req.id).status == dd.COMPLETED
    assert store.get_account(acct.id) is None


# --- both storage backends --------------------------------------------------------------- #

def _both_backends():
    from aismm.store.azure_store import AzureStore
    from aismm.store.local_store import LocalStore
    from tests.test_azure_store import FakeTableClient

    return [("local", LocalStore(db_url="sqlite:///:memory:")),
            ("azure", AzureStore(table_client=FakeTableClient()))]


@pytest.mark.parametrize("label,backend", _both_backends(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_the_store_methods_work_in_both_backends(label, backend):
    backend.init()
    a = _account(backend, PlatformName.instagram, "ig-1", fb_user="fb-1")
    b = _account(backend, PlatformName.instagram, "ig-2", fb_user="fb-2")
    _history(backend, a, n_runs=3, n_staged=2)
    _history(backend, b, n_runs=1, n_staged=1)
    assert backend.delete_runs_for_account(a.id) == 3
    assert backend.delete_staged_for_account(a.id) == 2
    assert [r.account_id for r in backend.list_runs(limit=50)] == [b.id]

    req = backend.upsert_deletion_request(DeletionRequest(source="meta", user_ref="x"))
    req.status, req.accounts_deleted = dd.COMPLETED, 1
    backend.upsert_deletion_request(req)
    got = backend.get_deletion_request(req.id)
    assert (got.status, got.accounts_deleted) == (dd.COMPLETED, 1)
    assert [r.id for r in backend.list_deletion_requests(status=dd.COMPLETED)] == [req.id]
    assert backend.list_deletion_requests(status=dd.RECEIVED) == []


# --- the connect records the id the callback names ------------------------------------- #

def test_with_user_id_never_blanks_a_stored_id():
    from aismm.platforms.instagram import with_user_id
    assert with_user_id({"a": 1}, "fb-1") == {"a": 1, "provider_user_id": "fb-1"}
    assert with_user_id({"a": 1}, "") == {"a": 1}       # a failed read must not erase it


def test_instagram_connect_records_the_facebook_user_id(monkeypatch):
    import asyncio

    import httpx

    from aismm.platforms import instagram

    def handler(request):
        if request.url.path.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": [{
                "id": "page-1", "name": "Page", "access_token": "page-tok",
                "instagram_business_account": {"id": "ig-1", "username": "kids"}}]})
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "fb-123"})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(instagram.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    from aismm.config import PlatformCreds
    identities = asyncio.run(instagram.Instagram(PlatformCreds()).fetch_identities("user-tok"))
    assert identities[0].meta["provider_user_id"] == "fb-123"


# --- the routes ------------------------------------------------------------------------ #

@pytest.fixture()
def client(store, monkeypatch, tmp_path):
    from aismm import assets as assets_module
    from aismm import config as config_module
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    (tmp_path / "assets").mkdir(exist_ok=True)
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(), data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    store.init()
    store.upsert_platform_app(PlatformApp(platform=PlatformName.instagram, client_id="meta-app"),
                              client_secret=META_SECRET)
    store.upsert_platform_app(PlatformApp(platform=PlatformName.tiktok, client_id="tt-app"),
                              client_secret=TIKTOK_SECRET)

    class _Inline:                         # run the background purge inline
        def __init__(self, target, args=(), **_kw):
            self._run = lambda: target(*args)

        def start(self):
            self._run()

    monkeypatch.setattr(dd.threading, "Thread", _Inline)
    application = app_module.create_app()
    application.secret_key = "test"
    return application.test_client()


def test_the_meta_callback_answers_as_meta_specifies(client, store):
    acct = _account(store, PlatformName.instagram, "ig-1", fb_user="fb-123")
    resp = client.post("/data-deletion/meta", data={"signed_request": signed_request(META_PAYLOAD)})
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"url", "confirmation_code"}
    assert body["url"].endswith(f"/data-deletion/status/{body['confirmation_code']}")
    assert store.get_account(acct.id) is None

    page = client.get(f"/data-deletion/status/{body['confirmation_code']}").get_data(as_text=True)
    assert "Done." in page and "1 connected account" in page


def test_the_meta_callback_rejects_a_forged_request(client, store):
    acct = _account(store, PlatformName.instagram, "ig-1", fb_user="fb-123")
    resp = client.post("/data-deletion/meta",
                       data={"signed_request": signed_request(META_PAYLOAD, secret="forged")})
    assert resp.status_code == 400
    assert store.get_account(acct.id) is not None


def test_the_tiktok_webhook(client, store):
    acct = _account(store, PlatformName.tiktok, "open-1")
    body = json.dumps({"event": "authorization.removed", "user_openid": "open-1",
                       "client_key": "tt-app"}).encode()
    resp = client.post("/data-deletion/tiktok", data=body,
                       headers={"TikTok-Signature": tiktok_header(body),
                                "Content-Type": "application/json"})
    assert resp.status_code == 200 and resp.get_json()["confirmation_code"]
    assert store.get_account(acct.id) is None

    other = json.dumps({"event": "video.publish.complete"}).encode()
    resp = client.post("/data-deletion/tiktok", data=other,
                       headers={"TikTok-Signature": tiktok_header(other)})
    assert resp.status_code == 200 and resp.get_json()["ignored"] == "video.publish.complete"

    resp = client.post("/data-deletion/tiktok", data=body,
                       headers={"TikTok-Signature": tiktok_header(body, secret="forged")})
    assert resp.status_code == 401


def test_the_pages_are_public_and_work(client):
    from aismm.dashboard import sso

    for endpoint in ("data_deletion_page", "data_deletion_status", "meta_data_deletion",
                     "tiktok_data_deletion"):
        assert endpoint in sso.PUBLIC_ENDPOINTS
    page = client.get("/data-deletion").get_data(as_text=True)
    for platform in ("Instagram", "Facebook", "TikTok", "X", "YouTube", "LinkedIn", "Reddit"):
        assert platform in page
    assert client.get("/data-deletion/status/no-such-code").status_code == 404
    assert client.get("/data-deletion/status?code=no-such-code").status_code == 404
    assert "Data deletion" in client.get("/legal/privacy").get_data(as_text=True)


def test_the_operator_can_delete_an_accounts_data(client, store):
    acct = _account(store, PlatformName.twitter, "x-1", handle="someone")
    _history(store, acct)
    resp = client.post(f"/accounts/{acct.id}/delete-data", follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "Deleted all data of someone" in page and "Confirmation code" in page
    assert store.get_account(acct.id) is None
    assert store.list_runs(limit=10) == []


def test_the_apps_page_shows_the_url_to_register(client):
    page = client.get("/apps/instagram").get_data(as_text=True)
    assert "/data-deletion/meta" in page
    assert "/data-deletion/tiktok" in client.get("/apps/tiktok").get_data(as_text=True)


def test_the_callbacks_work_with_sign_in_enabled(store, monkeypatch, tmp_path):
    """Production runs with SSO ON, and Meta/TikTok call with no session at all."""
    from aismm import assets as assets_module
    from aismm import config as config_module
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    (tmp_path / "assets").mkdir(exist_ok=True)
    auth = AuthSettings(issuer="https://login.example.com", client_id="c", client_secret="s",
                        allowed_emails=["me@example.com"])
    patched = dataclasses.replace(config_module.settings, auth=auth, data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    store.init()
    store.upsert_platform_app(PlatformApp(platform=PlatformName.facebook, client_id="meta-app"),
                              client_secret=META_SECRET)
    monkeypatch.setattr(dd, "_process_safely", lambda *_a: None)
    application = app_module.create_app()
    application.secret_key = "test"
    web = application.test_client()

    assert web.get("/accounts").status_code in (302, 401, 403)     # the app IS guarded
    resp = web.post("/data-deletion/meta",
                    data={"signed_request": signed_request(META_PAYLOAD)})
    assert resp.status_code == 200 and "confirmation_code" in resp.get_json()
    code = resp.get_json()["confirmation_code"]
    assert web.get(f"/data-deletion/status/{code}").status_code == 200
    assert web.get("/data-deletion").status_code == 200
