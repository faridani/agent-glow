import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep every test away from the user's real config/state/keychain."""
    monkeypatch.setenv("HUE_AGENT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HUE_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HUE_AGENT_CLAUDE_SETTINGS", str(tmp_path / "claude" / "settings.json"))
    monkeypatch.setenv("HUE_AGENT_CODEX_DIR", str(tmp_path / "codex"))
    monkeypatch.setenv("HUE_AGENT_NO_KEYRING", "1")
    return tmp_path
