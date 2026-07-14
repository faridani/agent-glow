"""Color parsing: names, hex, xy pairs, and conversions."""

import pytest

from hue_agent_status.colors import (
    NAMED_COLORS,
    parse_color,
    rgb_to_xy,
    xy_to_rgb,
)


class TestParseColor:
    def test_red_keeps_the_historical_hue_xy(self):
        parsed = parse_color("red")
        assert parsed.xy == (0.675, 0.322)
        assert parsed.rgb == (255, 0, 0)

    def test_names_are_case_insensitive(self):
        assert parse_color("Purple") == parse_color("purple")

    def test_hex_round_trip(self):
        parsed = parse_color("#ff0080")
        assert parsed.rgb == (255, 0, 128)
        assert 0 < parsed.xy[0] < 1 and 0 < parsed.xy[1] < 1

    def test_xy_pair(self):
        parsed = parse_color("0.2, 0.3")
        assert parsed.xy == (0.2, 0.3)
        assert all(0 <= c <= 255 for c in parsed.rgb)

    @pytest.mark.parametrize(
        "bad", ["", "notacolor", "#ff00", "#gggggg", "1.5,0.3", "0.3", "0.2,0"]
    )
    def test_rejects_garbage(self, bad):
        with pytest.raises(ValueError):
            parse_color(bad)

    def test_error_lists_known_names(self):
        with pytest.raises(ValueError, match="purple"):
            parse_color("crimsonish")


class TestConversions:
    def test_white_rgb_lands_near_d65(self):
        x, y = rgb_to_xy(255, 255, 255)
        assert abs(x - 0.3127) < 0.01 and abs(y - 0.3290) < 0.01

    def test_xy_to_rgb_saturates_primaries(self):
        r, g, b = xy_to_rgb(0.675, 0.322)  # deep red
        assert r == 255 and g < 80 and b < 80

    def test_black_rgb_degenerates_to_white_point(self):
        assert rgb_to_xy(0, 0, 0) == (0.3127, 0.3290)

    def test_all_named_colors_have_valid_xy(self):
        for name, parsed in NAMED_COLORS.items():
            x, y = parsed.xy
            assert 0 < x < 1 and 0 < y < 1, name
