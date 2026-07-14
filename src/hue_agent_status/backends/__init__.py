"""Light backends (Hue, WiZ) and the composite controller that drives them."""

from __future__ import annotations

from ..config import Config
from .base import BackendUnavailableError
from .composite import CompositeController, build_backends

__all__ = ["BackendUnavailableError", "CompositeController", "build_controller"]


def build_controller(config: Config, app_key: str | None = None) -> CompositeController:
    """Create one controller spanning every backend the config enables.

    A config with only ``[bridge]`` + ``[target]`` yields exactly one Hue
    backend — the pre-WiZ behavior, unchanged.
    """
    return CompositeController(build_backends(config, app_key=app_key))
