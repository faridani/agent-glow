"""Configuration model, TOML persistence, and cross-platform paths."""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import platformdirs
import tomli_w

APP_NAME = "hue-agent-status"

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


def pidfile_path() -> Path:
    return state_dir() / "daemon.pid"


def daemon_log_path() -> Path:
    return state_dir() / "daemon.log"


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
class PrivacyConfig:
    debug_log_payloads: bool = False


@dataclass
class Config:
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    animation: AnimationConfig = field(default_factory=AnimationConfig)
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


def config_from_dict(data: dict[str, Any]) -> Config:
    return Config(
        bridge=_section_from_dict(BridgeConfig, data.get("bridge")),
        daemon=_section_from_dict(DaemonConfig, data.get("daemon")),
        target=_section_from_dict(TargetConfig, data.get("target")),
        animation=_section_from_dict(AnimationConfig, data.get("animation")),
        privacy=_section_from_dict(PrivacyConfig, data.get("privacy")),
    )


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return dataclasses.asdict(cfg)


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    return config_from_dict(data)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    validate_config(cfg)
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".toml.tmp")
    with open(tmp, "wb") as fh:
        tomli_w.dump(config_to_dict(cfg), fh)
    tmp.replace(path)
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
