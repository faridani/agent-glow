"""WiZ local UDP protocol (JSON datagrams on port 38899).

Deliberately minimal — the whole protocol surface this project needs is four
messages (``setPilot``, ``getPilot``, ``getSystemConfig``, and the
``registration`` discovery broadcast), so we own the ~200 lines instead of
depending on a library. Builders/parsers are pure functions; ``WizTransport``
is the only piece that touches sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

WIZ_PORT = 38899
DEFAULT_TIMEOUT_SECONDS = 1.0
DEFAULT_RETRIES = 3
#: WiZ firmware rejects dimming below 10%.
MIN_DIMMING = 10
#: Kelvin range most WiZ tunable-white bulbs accept.
WARMEST_KELVIN = 2200
COOL_KELVIN = 4300

_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


class WizTimeoutError(Exception):
    """Bulb did not answer within the retry budget (UDP is lossy)."""


class WizProtocolError(Exception):
    """Bulb answered with an error payload."""


def normalize_mac(value: str) -> str:
    """Lowercase 12-hex form; tolerates ``:``, ``-``, ``.`` separators."""
    mac = re.sub(r"[\s:.\-]", "", value or "").lower()
    if not _MAC_RE.match(mac):
        raise ValueError(f"invalid WiZ mac {value!r} (need 12 hex digits)")
    return mac


def clamp_dimming(percent: float) -> int:
    return max(MIN_DIMMING, min(100, round(percent)))


@dataclass(frozen=True)
class WizCapabilities:
    supports_color: bool
    supports_ct: bool


def parse_capabilities(module_name: str) -> WizCapabilities:
    """Infer capabilities from moduleName (e.g. ``ESP01_SHRGB1C_31``).

    RGB → full color, TW → tunable white, DW (or anything unknown) →
    dimmable-only, the safe floor.
    """
    name = (module_name or "").upper()
    if "RGB" in name:
        return WizCapabilities(supports_color=True, supports_ct=True)
    if "TW" in name:
        return WizCapabilities(supports_color=False, supports_ct=True)
    return WizCapabilities(supports_color=False, supports_ct=False)


# -- message builders -----------------------------------------------------------


def build_get_pilot() -> dict:
    return {"method": "getPilot", "params": {}}


def build_get_system_config() -> dict:
    return {"method": "getSystemConfig", "params": {}}


def build_registration() -> dict:
    # register=false: just ask who's there, don't subscribe to push updates.
    return {
        "method": "registration",
        "params": {
            "phoneMac": "AAAAAAAAAAAA",
            "register": False,
            "phoneIp": "192.0.2.1",
            "id": 1,
        },
    }


def build_set_pilot(
    state: bool | None = None,
    dimming: float | None = None,
    temp_k: int | None = None,
    rgb: tuple[int, int, int] | None = None,
    cold_white: int | None = None,
    warm_white: int | None = None,
    scene_id: int | None = None,
    speed: int | None = None,
) -> dict:
    """``setPilot`` params; never mixes ``temp`` with ``r/g/b``."""
    params: dict = {}
    if state is not None:
        params["state"] = bool(state)
    if dimming is not None:
        params["dimming"] = clamp_dimming(dimming)
    if scene_id:
        params["sceneId"] = int(scene_id)
        if speed is not None:
            params["speed"] = int(speed)
    elif rgb is not None:
        params["r"], params["g"], params["b"] = (max(0, min(255, int(c))) for c in rgb)
        if cold_white is not None:
            params["c"] = max(0, min(255, int(cold_white)))
        if warm_white is not None:
            params["w"] = max(0, min(255, int(warm_white)))
    elif temp_k is not None:
        params["temp"] = int(temp_k)
    return {"method": "setPilot", "params": params}


# -- transport --------------------------------------------------------------------


class _WizDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.pending: dict[tuple[str, str], asyncio.Future] = {}
        self.discovered: list[tuple[str, str]] | None = None  # (mac, ip)

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            message = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        ip = addr[0]
        method = str(message.get("method", ""))
        if method == "registration" and self.discovered is not None:
            mac = (message.get("result") or {}).get("mac", "")
            with contextlib.suppress(ValueError):
                self.discovered.append((normalize_mac(str(mac)), ip))
        future = self.pending.pop((ip, method), None)
        if future is not None and not future.done():
            future.set_result(message)

    def error_received(self, exc) -> None:
        LOGGER.debug("wiz udp error: %s", exc)


class WizTransport:
    """One shared UDP endpoint for all bulbs; request/response with retries."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _WizDatagramProtocol | None = None
        self._last_sent: dict[str, float] = {}
        self._spacing_lock = asyncio.Lock()

    async def _ensure(self) -> _WizDatagramProtocol:
        if self._transport is None or self._transport.is_closing():
            loop = asyncio.get_running_loop()
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                _WizDatagramProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True
            )
        return self._protocol

    async def _space_commands(self, ip: str, interval: float = 0.1) -> None:
        # WiZ bulbs are UDP-direct (no hub bottleneck) but still appreciate
        # a little spacing between commands to the same bulb.
        async with self._spacing_lock:
            now = time.monotonic()
            wait_until = self._last_sent.get(ip, 0.0) + interval
            if wait_until > now:
                await asyncio.sleep(wait_until - now)
            self._last_sent[ip] = time.monotonic()

    async def send_command(
        self,
        ip: str,
        message: dict,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
    ) -> dict:
        """Send one message and await the bulb's reply; returns ``result``."""
        protocol = await self._ensure()
        await self._space_commands(ip)
        method = str(message.get("method", ""))
        data = json.dumps(message).encode("utf-8")
        loop = asyncio.get_running_loop()
        for _attempt in range(max(1, retries)):
            future: asyncio.Future = loop.create_future()
            protocol.pending[(ip, method)] = future
            self._transport.sendto(data, (ip, WIZ_PORT))
            try:
                response = await asyncio.wait_for(future, timeout)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            finally:
                # Cancellation bypasses the timeout handler. Always discard
                # the waiter so a cancelled daemon task cannot leak entries.
                key = (ip, method)
                if protocol.pending.get(key) is future:
                    protocol.pending.pop(key, None)
            result = response.get("result")
            if isinstance(result, dict):
                return result
            raise WizProtocolError(f"{method} rejected: {response.get('error')}")
        raise WizTimeoutError(f"no reply from {ip} after {retries} attempt(s)")

    async def discover(
        self, broadcast: str = "255.255.255.255", wait: float = 2.0
    ) -> list[tuple[str, str]]:
        """Broadcast a registration ping; returns unique (mac, ip) pairs."""
        protocol = await self._ensure()
        protocol.discovered = []
        data = json.dumps(build_registration()).encode("utf-8")
        try:
            deadline = asyncio.get_running_loop().time() + max(0.5, wait)
            while asyncio.get_running_loop().time() < deadline:
                self._transport.sendto(data, (broadcast, WIZ_PORT))
                await asyncio.sleep(min(0.7, wait))
            found: dict[str, str] = {}
            for mac, ip in protocol.discovered:
                found[mac] = ip
            return sorted(found.items())
        finally:
            protocol.discovered = None

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            self._protocol = None
