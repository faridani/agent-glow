"""Breathing keyframe generation."""

import pytest

from hue_agent_status.animation import breathing_keyframes


def test_cycle_returns_to_minimum():
    frames = breathing_keyframes(25, 65, 6.0)
    assert frames[-1][0] == 25.0


def test_cycle_reaches_maximum():
    frames = breathing_keyframes(25, 65, 6.0)
    assert max(b for b, _ in frames) == 65.0


def test_durations_sum_to_period():
    frames = breathing_keyframes(25, 65, 6.0, keyframes_per_half=3)
    assert sum(d for _, d in frames) == pytest.approx(6.0)


def test_single_keyframe_per_half_degenerates_to_two_commands():
    frames = breathing_keyframes(25, 65, 6.0, keyframes_per_half=1)
    assert frames == [(65.0, 3.0), (25.0, 3.0)]


def test_brightness_stays_in_band():
    frames = breathing_keyframes(25, 65, 6.0, keyframes_per_half=4)
    for brightness, _ in frames:
        assert 25.0 <= brightness <= 65.0


def test_sine_easing_midpoints_follow_raised_cosine():
    frames = breathing_keyframes(0, 100, 4.0, keyframes_per_half=2, easing="sine")
    # phases: 1/4, 1/2, 3/4, 1 -> 50, 100, 50, 0
    assert [b for b, _ in frames] == [50.0, 100.0, 50.0, 0.0]


def test_linear_easing_is_triangle():
    frames = breathing_keyframes(0, 100, 4.0, keyframes_per_half=2, easing="linear")
    assert [b for b, _ in frames] == [50.0, 100.0, 50.0, 0.0]


def test_swapped_min_max_normalized():
    frames = breathing_keyframes(65, 25, 6.0)
    assert min(b for b, _ in frames) == 25.0
    assert max(b for b, _ in frames) == 65.0
