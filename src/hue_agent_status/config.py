"""Configuration model, TOML persistence, and cross-platform paths."""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import platformdirs
import tomli_w

from .colors import parse_color
from .roles import ROLE_NAMES, VALID_BACKENDS, parse_light_ref

APP_NAME = "hue-agent-status"

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

VALID_TARGET_MODES = ("lights", "room", "zone", "grouped_light")
VALID_RESTORE_MODES = ("smart", "always", "never")
VALID_EASINGS = ("sine", "linear")


def config_dir() -> Path:
    override = os.environ.get("HUE_AGENT_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_config_dir(APP_NAME))


def state_dir() -> Path:
    override = os.environ.get("HUE_AGENT_STATE_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_state_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / "config.toml"


def snapshot_path() -> Path:
    return state_dir() / "snapshot.json"


def wiz_snapshot_path() -> Path:
    return state_dir() / "snapshot-wiz.json"


def wiz_ip_cache_path() -> Path:
    return state_dir() / "wiz-ip-cache.json"


def pidfile_path() -> Path:
    return state_dir() / "daemon.pid"


def daemon_log_path() -> Path:
    return state_dir() / "daemon.log"


def ensure_private_dir(path: Path) -> None:
    """Create a runtime directory and restrict it to the current user."""
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    try:
        os.chmod(path, PRIVATE_DIR_MODE)
    except OSError:
        pass


def ensure_private_file(path: Path) -> None:
    """Repair a runtime file's mode where POSIX permissions are available."""
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        pass


def open_private_fd(path: Path, *, binary: bool = False, append: bool = False) -> int:
    """Open an owner-only runtime file without relying on the process umask."""
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    if binary:
        flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
    except (AttributeError, OSError):
        pass
    return fd


def write_private_text(path: Path, text: str) -> None:
    fd = open_private_fd(path)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    ensure_private_file(path)


def repair_private_runtime_permissions() -> None:
    """Upgrade owner-only modes for files created by older releases."""
    for directory in (config_dir(), state_dir()):
        if directory.exists():
            ensure_private_dir(directory)
    for path in (
        config_path(),
        config_dir() / "secrets.json",
        snapshot_path(),
        wiz_snapshot_path(),
        wiz_ip_cache_path(),
        pidfile_path(),
        daemon_log_path(),
        state_dir() / "autostart.stamp",
    ):
        if path.exists():
            ensure_private_file(path)


@dataclass
class BridgeConfig:
    host: str = ""
    bridge_id: str = ""
    api_version: int = 2


@dataclass
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    active_ttl_seconds: int = 1800
    waiting_ttl_seconds: int = 14400
    # How long "the turn is over, your move" (Stop / agent-turn-complete)
    # keeps the lights red before restoring. Hard-blocked waiting states
    # (permission prompts, questions) use waiting_ttl_seconds instead.
    turn_end_waiting_seconds: int = 300
    idle_grace_seconds: float = 2.0


@dataclass
class TargetConfig:
    mode: str = "lights"
    ids: list[str] = field(default_factory=list)


@dataclass
class AnimationConfig:
    breath_period_seconds: float = 6.0
    breath_min_brightness: float = 25.0
    breath_max_brightness: float = 65.0
    breath_keyframes_per_half: int = 2
    easing: str = "sine"
    # "auto": keep each lamp's current color if it was on; soft warm white if it was off.
    # "warm" / "cool": force a color temperature. "preserve": never touch color.
    breath_color: str = "auto"
    wait_brightness: float = 85.0
    wait_color: str = "red"
    wait_pulse_fallback: bool = True
    restore: str = "smart"
    idle_restore_transition_ms: int = 1500
    wait_transition_ms: int = 800


@dataclass
class WizBulbConfig:
    mac: str = ""  # stable identity (12 hex chars; separators tolerated)
    ip: str = ""  # hint only — the runtime IP cache wins after discovery
    name: str = ""


@dataclass
class WizConfig:
    broadcast: str = "255.255.255.255"
    bulbs: list[WizBulbConfig] = field(default_factory=list)


@dataclass
class RolesConfig:
    """Per-state light lists (light refs like "hue:<uuid>" / "wiz:<mac>").

    An empty list means "all configured lights", the pre-roles behavior.
    """

    thinking: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)


@dataclass
class PrivacyConfig:
    debug_log_payloads: bool = False


@dataclass
class Config:
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    animation: AnimationConfig = field(default_factory=AnimationConfig)
    wiz: WizConfig = field(default_factory=WizConfig)
    roles: RolesConfig = field(default_factory=RolesConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)


class ConfigError(Exception):
    """Invalid configuration value or file."""


def _section_from_dict(cls: type, data: Any) -> Any:
    if not isinstance(data, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    try:
        return cls(**kwargs)
    except TypeError as err:
        raise ConfigError(f"invalid [{cls.__name__}] section: {err}") from err


def _wiz_from_dict(data: Any) -> WizConfig:
    # [[wiz.bulbs]] is a list of tables, which the flat helper can't build.
    if not isinstance(data, dict):
        return WizConfig()
    bulbs = [
        _section_from_dict(WizBulbConfig, item)
        for item in data.get("bulbs") or []
        if isinstance(item, dict)
    ]
    wiz = _section_from_dict(WizConfig, {k: v for k, v in data.items() if k != "bulbs"})
    wiz.bulbs = bulbs
    return wiz


def config_from_dict(data: dict[str, Any]) -> Config:
    return Config(
        bridge=_section_from_dict(BridgeConfig, data.get("bridge")),
        daemon=_section_from_dict(DaemonConfig, data.get("daemon")),
        target=_section_from_dict(TargetConfig, data.get("target")),
        animation=_section_from_dict(AnimationConfig, data.get("animation")),
        wiz=_wiz_from_dict(data.get("wiz")),
        roles=_section_from_dict(RolesConfig, data.get("roles")),
        privacy=_section_from_dict(PrivacyConfig, data.get("privacy")),
    )


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return dataclasses.asdict(cfg)


def load_config(path: Path | None = None) -> Config:
    if path is None:
        repair_private_runtime_permissions()
        path = config_path()
    if not path.exists():
        return Config()
    try:
        ensure_private_dir(path.parent)
        ensure_private_file(path)
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    return config_from_dict(data)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    validate_config(cfg)
    path = path or config_path()
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(".toml.tmp")
    fd = open_private_fd(tmp, binary=True)
    with os.fdopen(fd, "wb") as fh:
        tomli_w.dump(config_to_dict(cfg), fh)
    tmp.replace(path)
    ensure_private_file(path)
    return path


def is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_config(cfg: Config) -> None:
    if cfg.target.mode not in VALID_TARGET_MODES:
        raise ConfigError(
            f"target.mode must be one of {VALID_TARGET_MODES}, got {cfg.target.mode!r}"
        )
    if cfg.animation.restore not in VALID_RESTORE_MODES:
        raise ConfigError(
            f"animation.restore must be one of {VALID_RESTORE_MODES}, got {cfg.animation.restore!r}"
        )
    if cfg.animation.easing not in VALID_EASINGS:
        raise ConfigError(
            f"animation.easing must be one of {VALID_EASINGS}, got {cfg.animation.easing!r}"
        )
    if not is_loopback_host(cfg.daemon.host):
        raise ConfigError(
            f"daemon.host must be a loopback address (got {cfg.daemon.host!r}); "
            "hue-agent-status never exposes a network server"
        )
    if not (0 < cfg.daemon.port < 65536):
        raise ConfigError(f"daemon.port must be 1-65535, got {cfg.daemon.port}")
    anim = cfg.animation
    if not (0 <= anim.breath_min_brightness <= anim.breath_max_brightness <= 100):
        raise ConfigError("animation brightness must satisfy 0 <= min <= max <= 100")
    if anim.breath_period_seconds < 1.0:
        raise ConfigError("animation.breath_period_seconds must be >= 1.0")
    if anim.breath_keyframes_per_half < 1:
        raise ConfigError("animation.breath_keyframes_per_half must be >= 1")
    try:
        parse_color(anim.wait_color)
    except ValueError as err:
        raise ConfigError(f"animation.wait_color: {err}") from None
    seen_macs: set[str] = set()
    for index, bulb in enumerate(cfg.wiz.bulbs):
        mac = re.sub(r"[\s:.\-]", "", bulb.mac or "").lower()
        if not re.fullmatch(r"[0-9a-f]{12}", mac):
            raise ConfigError(f"wiz.bulbs[{index}].mac must be 12 hex digits")
        if mac in seen_macs:
            raise ConfigError(f"duplicate wiz bulb mac in wiz.bulbs[{index}]")
        seen_macs.add(mac)
        if bulb.ip:
            try:
                ipaddress.ip_address(bulb.ip)
            except ValueError:
                raise ConfigError(f"wiz.bulbs[{index}].ip is invalid") from None
    for role in ROLE_NAMES:
        entries = getattr(cfg.roles, role)
        if not isinstance(entries, list):
            raise ConfigError(f"roles.{role} must be a list of strings")
        for index, ref in enumerate(entries):
            if not isinstance(ref, str) or not ref.strip():
                raise ConfigError(f"roles.{role} entries must be non-empty strings")
            backend, light_id = parse_light_ref(ref)
            if backend not in VALID_BACKENDS:
                raise ConfigError(
                    f"roles.{role}[{index}]: unknown backend; "
                    f"valid backends: {', '.join(VALID_BACKENDS)}"
                )
            if not light_id:
                raise ConfigError(f"roles.{role}[{index}]: missing light id")


def _coerce(value: str, target_type: type) -> Any:
    if target_type is bool:
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"expected a boolean, got {value!r}")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is list:
        value = value.strip()
        if value.startswith("["):
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ConfigError(f"expected a list, got {value!r}")
            return [str(item) for item in parsed]
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def set_config_value(cfg: Config, dotted_key: str, raw_value: str) -> Config:
    """Set ``section.field`` on a Config from a string, with type coercion."""
    if dotted_key == "wiz.bulbs":
        raise ConfigError(
            "use `hue-agent wiz add` / `hue-agent wiz remove` to manage WiZ bulbs"
        )
    parts = dotted_key.split(".")
    if len(parts) != 2:
        raise ConfigError(f"key must look like 'section.field', got {dotted_key!r}")
    section_name, field_name = parts
    section = getattr(cfg, section_name, None)
    if section is None or not dataclasses.is_dataclass(section):
        raise ConfigError(f"unknown config section {section_name!r}")
    matching = [f for f in fields(section) if f.name == field_name]
    if not matching:
        known = ", ".join(f.name for f in fields(section))
        raise ConfigError(f"unknown key {dotted_key!r}; known: {known}")
    current = getattr(section, field_name)
    target_type = type(current) if current is not None else str
    setattr(section, field_name, _coerce(raw_value, target_type))
    validate_config(cfg)
    return cfg
