import pytest

from aismm.store.local_store import LocalStore


@pytest.fixture()
def store(tmp_path):
    """A LocalStore backed by a throwaway SQLite file."""
    return LocalStore(db_url=f"sqlite:///{tmp_path/'test.sqlite'}")
