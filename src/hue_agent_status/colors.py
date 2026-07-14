"""Color parsing for the waiting look: names, ``#rrggbb`` hex, or CIE ``"x,y"``.

Every parse yields both representations a backend may need: CIE xy for Hue
and 8-bit RGB for WiZ. Conversions use the standard sRGB/D65 matrices.
"""

from __future__ import annotations

from dataclasses import dataclass

#: D65 white point, used when a conversion degenerates.
_WHITE_XY = (0.3127, 0.3290)


@dataclass(frozen=True)
class ParsedColor:
    xy: tuple[float, float]
    rgb: tuple[int, int, int]


def rgb_to_xy(r: int, g: int, b: int) -> tuple[float, float]:
    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    rl, gl, bl = linear(r), linear(g), linear(b)
    x = 0.4124 * rl + 0.3576 * gl + 0.1805 * bl
    y = 0.2126 * rl + 0.7152 * gl + 0.0722 * bl
    z = 0.0193 * rl + 0.1192 * gl + 0.9505 * bl
    total = x + y + z
    if total <= 0:
        return _WHITE_XY
    return (round(x / total, 4), round(y / total, 4))


def xy_to_rgb(x: float, y: float) -> tuple[int, int, int]:
    if y <= 0:
        return (255, 255, 255)
    big_y = 1.0
    big_x = x * big_y / y
    big_z = (1 - x - y) * big_y / y
    rl = 3.2406 * big_x - 1.5372 * big_y - 0.4986 * big_z
    gl = -0.9689 * big_x + 1.8758 * big_y + 0.0415 * big_z
    bl = 0.0557 * big_x - 0.2040 * big_y + 1.0570 * big_z

    def gamma(c: float) -> float:
        c = max(0.0, c)
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055

    channels = [gamma(c) for c in (rl, gl, bl)]
    peak = max(channels)
    if peak > 1.0:
        channels = [c / peak for c in channels]
    return tuple(round(c * 255) for c in channels)  # type: ignore[return-value]


_NAMED_RGB: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "orange": (255, 120, 0),
    "yellow": (255, 220, 0),
    "green": (0, 255, 0),
    "cyan": (0, 255, 255),
    "blue": (0, 0, 255),
    "purple": (128, 0, 255),
    "magenta": (255, 0, 255),
    "pink": (255, 105, 180),
    "white": (255, 255, 255),
}

NAMED_COLORS: dict[str, ParsedColor] = {
    name: ParsedColor(xy=rgb_to_xy(*rgb), rgb=rgb) for name, rgb in _NAMED_RGB.items()
}
# The historical hue-agent red, deeper than the sRGB primary and inside all
# Hue gamuts — keep it so existing installs look identical.
NAMED_COLORS["red"] = ParsedColor(xy=(0.675, 0.322), rgb=(255, 0, 0))


def parse_color(value: str) -> ParsedColor:
    """Accepts a color name, ``#rrggbb``, or an ``"x,y"`` chromaticity pair."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty color value")
    text = value.strip().lower()
    if text in NAMED_COLORS:
        return NAMED_COLORS[text]
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) != 6:
            raise ValueError(f"hex color must be #rrggbb, got {value!r}")
        try:
            rgb = tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            raise ValueError(f"invalid hex color {value!r}") from None
        return ParsedColor(xy=rgb_to_xy(*rgb), rgb=rgb)  # type: ignore[arg-type]
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        try:
            x, y = (
                (float(parts[0]), float(parts[1])) if len(parts) == 2 else (None, None)
            )
        except ValueError:
            x = y = None
        if x is None or y is None or not (0 <= x <= 1 and 0 < y <= 1):
            raise ValueError(f"xy color must be like '0.675,0.322', got {value!r}")
        return ParsedColor(xy=(x, y), rgb=xy_to_rgb(x, y))
    known = ", ".join(sorted(NAMED_COLORS))
    raise ValueError(f"unknown color {value!r}; use one of {known}, #rrggbb, or 'x,y'")
