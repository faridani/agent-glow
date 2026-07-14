"""Owner-only permissions for config and runtime state artifacts."""

import os
import stat

import pytest

from hue_agent_status import autostart, secret_store
from hue_agent_status.backends import wiz
from hue_agent_status.backends.base import atomic_write_json, load_snapshot_data
from hue_agent_status.config import (
    Config,
    config_dir,
    config_path,
    daemon_log_path,
    load_config,
    pidfile_path,
    save_config,
    snapshot_path,
    state_dir,
    wiz_ip_cache_path,
    wiz_snapshot_path,
    write_private_text,
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_runtime_artifacts_ignore_permissive_umask():
    previous_umask = os.umask(0)
    try:
        save_config(Config())
        atomic_write_json(snapshot_path(), {"lights": {}})
        atomic_write_json(wiz_snapshot_path(), {"lights": {}})
        atomic_write_json(wiz_ip_cache_path(), {"aabbccddeeff": "192.0.2.1"})
        write_private_text(pidfile_path(), "123")
        secret_store.set_secret("hue-app-key", "test-value")
    finally:
        os.umask(previous_umask)

    assert _mode(config_dir()) == 0o700
    assert _mode(state_dir()) == 0o700
    for path in (
        config_path(),
        snapshot_path(),
        wiz_snapshot_path(),
        wiz_ip_cache_path(),
        pidfile_path(),
        secret_store._fallback_path(),
    ):
        assert _mode(path) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_writes_repair_existing_permissive_modes():
    config_dir().mkdir(parents=True)
    config_path().write_text("old", encoding="utf-8")
    os.chmod(config_dir(), 0o755)
    os.chmod(config_path(), 0o644)

    state_dir().mkdir(parents=True)
    snapshot_path().write_text("old", encoding="utf-8")
    os.chmod(state_dir(), 0o755)
    os.chmod(snapshot_path(), 0o644)

    save_config(Config())
    atomic_write_json(snapshot_path(), {"lights": {}})

    assert _mode(config_dir()) == 0o700
    assert _mode(config_path()) == 0o600
    assert _mode(state_dir()) == 0o700
    assert _mode(snapshot_path()) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_reads_repair_legacy_permissive_modes():
    config_dir().mkdir(parents=True)
    config_path().write_text("", encoding="utf-8")
    secret_store._fallback_path().write_text("{}", encoding="utf-8")
    state_dir().mkdir(parents=True)
    snapshot_path().write_text('{"lights": {}, "controlled": []}', encoding="utf-8")
    wiz_ip_cache_path().write_text("{}", encoding="utf-8")
    pidfile_path().write_text("123", encoding="utf-8")
    daemon_log_path().write_text("private log", encoding="utf-8")
    stamp = state_dir() / "autostart.stamp"
    stamp.write_text("", encoding="utf-8")

    for directory in (config_dir(), state_dir()):
        os.chmod(directory, 0o755)
    private_files = (
        config_path(),
        secret_store._fallback_path(),
        snapshot_path(),
        wiz_ip_cache_path(),
        pidfile_path(),
        daemon_log_path(),
        stamp,
    )
    for path in private_files:
        os.chmod(path, 0o644)

    load_config()
    secret_store._read_fallback()
    load_snapshot_data(snapshot_path(), lambda item: item)
    wiz._load_ip_cache()

    assert _mode(config_dir()) == 0o700
    assert _mode(state_dir()) == 0o700
    for path in private_files:
        assert _mode(path) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_generated_autostart_files_are_owner_only(tmp_path):
    path = tmp_path / "generated-service"
    autostart._write_private_bytes(path, b"private launcher path\n")
    assert _mode(path) == 0o600

    os.chmod(path, 0o644)
    autostart._write_private_bytes(path, b"updated\n")
    assert _mode(path) == 0o600
