"""Breathing-curve keyframe generation.

The Hue Bridge interpolates between commanded brightness targets over a
``dynamics.duration`` transition, so a slow, elegant breathe only needs a few
keyframes per cycle: we sample a raised-cosine (sine) curve and let the bridge
glide between samples. No rapid command stepping.
"""

from __future__ import annotations

import math

Keyframe = tuple[float, float]  # (brightness percent, duration seconds)


def breathing_keyframes(
    min_brightness: float,
    max_brightness: float,
    period_seconds: float,
    keyframes_per_half: int = 2,
    easing: str = "sine",
) -> list[Keyframe]:
    """One full inhale/exhale cycle, starting from (and returning to) minimum.

    Each keyframe is a brightness target plus the transition/hold duration to
    reach it. ``keyframes_per_half=1`` degenerates to the classic two-command
    pattern (fade to max over half the period, fade back to min).
    """
    if max_brightness < min_brightness:
        min_brightness, max_brightness = max_brightness, min_brightness
    steps = max(1, int(keyframes_per_half)) * 2
    duration = period_seconds / steps
    span = max_brightness - min_brightness
    frames: list[Keyframe] = []
    for i in range(1, steps + 1):
        phase = i / steps  # 0..1 over the full cycle
        if easing == "linear":
            level = 1.0 - abs(2.0 * phase - 1.0)  # triangle wave
        else:  # "sine" — raised cosine, gentle at both ends
            level = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
        frames.append((round(min_brightness + span * level, 2), duration))
    return frames
