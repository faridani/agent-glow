"""Secret storage fallback file behavior (keyring disabled via env)."""

import os
import stat
import sys

from hue_agent_status import secret_store


def test_set_and_get_roundtrip():
    assert secret_store.get_secret("hue-app-key") is None
    backend = secret_store.set_secret("hue-app-key", "abc123")
    assert backend == "file"
    assert secret_store.get_secret("hue-app-key") == "abc123"
    assert secret_store.get_app_key() == "abc123"


def test_fallback_file_permissions():
    secret_store.set_secret("hue-app-key", "abc123")
    path = secret_store._fallback_path()
    assert path.exists()
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600


def test_delete_secret():
    secret_store.set_secret("hue-app-key", "abc123")
    secret_store.delete_secret("hue-app-key")
    assert secret_store.get_secret("hue-app-key") is None


def test_ensure_daemon_token_is_stable_and_random():
    token1 = secret_store.ensure_daemon_token()
    token2 = secret_store.ensure_daemon_token()
    assert token1 == token2
    assert len(token1) >= 32


def test_corrupt_fallback_file_is_survivable():
    path = secret_store._fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert secret_store.get_secret("hue-app-key") is None
    secret_store.set_secret("hue-app-key", "fresh")
    assert secret_store.get_secret("hue-app-key") == "fresh"
