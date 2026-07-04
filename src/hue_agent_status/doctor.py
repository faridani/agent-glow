"""`hue-agent doctor`: verify the whole chain from Python to lights to hooks."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from .config import Config, config_path

OK = "ok"
WARN = "warn"
FAIL = "fail"


def _symbols() -> dict[str, str]:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓✗".encode(encoding)
        return {OK: "✓", WARN: "!", FAIL: "✗"}
    except (UnicodeEncodeError, LookupError):
        return {OK: "OK", WARN: "!!", FAIL: "XX"}


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


def _check_python() -> CheckResult:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info >= (3, 11):
        return CheckResult("python", OK, f"Python {version}")
    return CheckResult("python", FAIL, f"Python {version}; 3.11+ required")


def _check_config(config: Config) -> CheckResult:
    path = config_path()
    if not path.exists():
        return CheckResult("config", WARN, f"{path} missing — run `hue-agent setup`")
    if not config.bridge.host:
        return CheckResult("config", WARN, "no bridge configured — run `hue-agent setup`")
    return CheckResult("config", OK, str(path))


def _check_keyring() -> CheckResult:
    from . import secret_store

    kr = secret_store._keyring()
    if kr is not None:
        try:
            name = type(kr.get_keyring()).__name__
        except Exception:
            name = "unknown"
        return CheckResult("keyring", OK, f"OS keychain available ({name})")
    if secret_store._fallback_path().exists():
        return CheckResult(
            "keyring", WARN, f"no keychain; using {secret_store._fallback_path()}"
        )
    return CheckResult("keyring", WARN, "no OS keychain backend available")


def _check_app_key() -> CheckResult:
    from . import secret_store

    if secret_store.get_app_key():
        return CheckResult("app-key", OK, "Hue app key stored")
    return CheckResult("app-key", FAIL, "no Hue app key — run `hue-agent setup`")


async def _bridge_checks(config: Config) -> list[CheckResult]:
    """Connectivity, key validity, and target existence in one connection."""
    from aiohue.errors import Unauthorized

    from . import secret_store
    from .hue import HueController

    if not config.bridge.host:
        return [CheckResult("bridge", FAIL, "no bridge host configured")]
    if not secret_store.get_app_key():
        return [CheckResult("bridge", FAIL, "no app key; cannot test bridge")]
    controller = HueController(config)
    try:
        async with asyncio.timeout(12):
            await controller.connect()
    except Exception as err:
        cause = err.__cause__ or err
        if isinstance(cause, Unauthorized) or "unauthorized" in str(err).lower():
            return [
                CheckResult("bridge", OK, f"reachable at {config.bridge.host}"),
                CheckResult("app-key-valid", FAIL, "bridge rejected the app key — re-run setup"),
            ]
        return [CheckResult("bridge", FAIL, f"unreachable at {config.bridge.host}: {err}")]
    try:
        results = [
            CheckResult("bridge", OK, f"reachable at {config.bridge.host}"),
            CheckResult("app-key-valid", OK, "app key accepted"),
        ]
        summary = controller.target_summary()
        wanted = len(config.target.ids)
        if summary["lights"] or summary["grouped_light"]:
            results.append(
                CheckResult(
                    "targets",
                    OK,
                    f"{config.target.mode}: {len(summary['lights'])} light(s) resolved",
                )
            )
        elif wanted:
            results.append(
                CheckResult("targets", FAIL, "configured targets not found on bridge")
            )
        else:
            results.append(CheckResult("targets", WARN, "no target lights selected"))
        return results
    finally:
        await controller.close()


def _check_daemon(config: Config) -> CheckResult:
    from . import secret_store
    from .client import get_health

    token = secret_store.get_daemon_token()
    if not token:
        return CheckResult("daemon", WARN, "no daemon token yet (created on first run)")
    health = get_health(config, token, timeout=1.5)
    if health:
        return CheckResult(
            "daemon",
            OK,
            f"running (pid {health.get('pid')}, state: {health.get('aggregate')})",
        )
    return CheckResult("daemon", WARN, "not running (hooks auto-start it on demand)")


def _check_claude_hooks() -> CheckResult:
    from . import hooks_claude

    if hooks_claude.is_installed():
        return CheckResult("claude-hooks", OK, str(hooks_claude.claude_settings_path()))
    return CheckResult(
        "claude-hooks", WARN, "not installed — run `hue-agent install-hooks --claude`"
    )


def _check_codex_hooks() -> CheckResult:
    from . import hooks_codex

    if hooks_codex.hooks_installed():
        return CheckResult("codex-hooks", OK, str(hooks_codex.codex_hooks_path()))
    return CheckResult(
        "codex-hooks", WARN, "not installed — run `hue-agent install-hooks --codex`"
    )


def _check_codex_notify() -> CheckResult:
    from . import hooks_codex

    if hooks_codex.notify_installed():
        return CheckResult("codex-notify", OK, str(hooks_codex.codex_config_path()))
    return CheckResult(
        "codex-notify", WARN, "not configured — run `hue-agent install-hooks --codex`"
    )


def run_doctor(config: Config, config_error: str | None = None) -> int:
    config_check = (
        CheckResult("config", FAIL, config_error) if config_error else _check_config(config)
    )
    results: list[CheckResult] = [
        _check_python(),
        config_check,
        _check_keyring(),
        _check_app_key(),
    ]
    try:
        results.extend(asyncio.run(_bridge_checks(config)))
    except Exception as err:
        results.append(CheckResult("bridge", FAIL, f"check failed: {err}"))
    results.append(_check_daemon(config))
    results.append(_check_claude_hooks())
    results.append(_check_codex_hooks())
    results.append(_check_codex_notify())

    symbols = _symbols()
    width = max(len(r.name) for r in results)
    for r in results:
        print(f" {symbols[r.status]} {r.name.ljust(width)}  {r.detail}")
    failures = sum(1 for r in results if r.status == FAIL)
    warnings = sum(1 for r in results if r.status == WARN)
    print(f"\n{len(results)} checks: {failures} failed, {warnings} warning(s)")
    return 1 if failures else 0
