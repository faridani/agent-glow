"""Secret storage: OS keychain via ``keyring`` with a strict-permission file fallback.

Two secrets are stored:

* ``hue-app-key`` — the application key issued by the Hue Bridge after the
  physical link button was pressed.
* ``daemon-token`` — a random bearer token required on every request to the
  local daemon.

Lookup order mirrors the write preference: OS keychain first, then the
fallback file. The daemon additionally accepts tokens from *both* backends
(``all_daemon_tokens``) so that a hook running in an environment without a
usable keychain (SSH session, cron) can never split-brain authentication.
"""

from __future__ import annotations

import json
import os
import secrets
import sys

from .config import config_dir, ensure_private_dir, ensure_private_file, open_private_fd

SERVICE_NAME = "hue-agent-status"

APP_KEY = "hue-app-key"
DAEMON_TOKEN = "daemon-token"

_warned_fallback = False

#: Set by the `hook` / `codex-notify` subcommands, which must print nothing.
SILENT = False


def _fallback_path():
    return config_dir() / "secrets.json"


def _read_fallback() -> dict[str, str]:
    path = _fallback_path()
    if not path.exists():
        return {}
    try:
        ensure_private_dir(path.parent)
        ensure_private_file(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _warn_fallback_once() -> None:
    global _warned_fallback
    if _warned_fallback or SILENT:
        return
    _warned_fallback = True
    extra = ""
    if sys.platform == "win32":
        extra = " (on Windows the file is protected only by your user profile ACLs)"
    print(
        f"warning: no usable OS keychain found; storing secrets in "
        f"{_fallback_path()} with owner-only permissions{extra}.",
        file=sys.stderr,
    )


def _write_fallback(data: dict[str, str]) -> None:
    path = _fallback_path()
    tmp = path.with_suffix(".json.tmp")
    fd = open_private_fd(tmp)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    ensure_private_file(path)


def _keyring():
    """Import keyring lazily; return the module or None if unusable."""
    if os.environ.get("HUE_AGENT_NO_KEYRING"):
        return None
    try:
        import keyring
        import keyring.backends.fail

        backend = keyring.get_keyring()
        if isinstance(backend, keyring.backends.fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def get_secret(name: str) -> str | None:
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE_NAME, name)
            if value:
                return value
        except Exception:
            pass
    return _read_fallback().get(name) or None


def set_secret(name: str, value: str) -> str:
    """Store a secret; returns the backend used ("keyring" or "file")."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE_NAME, name, value)
            # Drop any stale copy from the fallback file so the keychain
            # value is authoritative.
            data = _read_fallback()
            if name in data:
                del data[name]
                _write_fallback(data)
            return "keyring"
        except Exception:
            pass
    data = _read_fallback()
    data[name] = value
    _write_fallback(data)
    _warn_fallback_once()  # only when the secret actually lives in the file
    return "file"


def delete_secret(name: str) -> None:
    data = _read_fallback()
    if name in data:
        del data[name]
        _write_fallback(data)
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE_NAME, name)
        except Exception:
            pass


def get_app_key() -> str | None:
    return get_secret(APP_KEY)


def set_app_key(value: str) -> str:
    return set_secret(APP_KEY, value)


def get_daemon_token() -> str | None:
    return get_secret(DAEMON_TOKEN)


def ensure_daemon_token() -> str:
    token = get_daemon_token()
    if not token:
        token = secrets.token_urlsafe(32)
        set_secret(DAEMON_TOKEN, token)
    return token


def all_daemon_tokens() -> set[str]:
    """Every stored daemon token, from both backends.

    The daemon accepts any of these, so clients whose environment can only
    reach one backend (e.g. no keychain over SSH) still authenticate.
    """
    tokens: set[str] = set()
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE_NAME, DAEMON_TOKEN)
            if value:
                tokens.add(value)
        except Exception:
            pass
    file_value = _read_fallback().get(DAEMON_TOKEN)
    if file_value:
        tokens.add(file_value)
    if not tokens:
        tokens.add(ensure_daemon_token())
    return tokens
