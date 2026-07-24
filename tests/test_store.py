from aismm.models import Account, Instruction, PlatformName, PublishMode


def test_account_token_roundtrip(store):
    acct = Account(platform=PlatformName.twitter, handle="@bot")
    saved = store.upsert_account(acct, access_token="secret-access", refresh_token="secret-refresh")
    # Ciphertext is not the plaintext…
    assert saved.access_token_enc and saved.access_token_enc != "secret-access"
    # …but decrypts back.
    access, refresh = store.get_tokens(saved.id)
    assert (access, refresh) == ("secret-access", "secret-refresh")


def test_instruction_account_ids(store):
    instr = Instruction(name="Test", publish_mode=PublishMode.approval)
    instr.set_account_ids(["a1", "a2"])
    saved = store.upsert_instruction(instr)
    assert store.get_instruction(saved.id).account_ids == ["a1", "a2"]


def test_enabled_only_filter(store):
    store.upsert_instruction(Instruction(name="on", enabled=True))
    store.upsert_instruction(Instruction(name="off", enabled=False))
    names = {i.name for i in store.list_instructions(enabled_only=True)}
    assert names == {"on"}


def test_single_flight_lock(store):
    assert store.acquire_lock("k", ttl_seconds=3600) is True
    assert store.acquire_lock("k", ttl_seconds=3600) is False  # held
    store.release_lock("k")
    assert store.acquire_lock("k", ttl_seconds=3600) is True    # reacquire after release
