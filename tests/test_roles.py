"""Light refs, role resolution, and name matching."""

import pytest

from hue_agent_status.config import Config
from hue_agent_status.roles import (
    LightInfo,
    MatchError,
    effective_role_ids,
    format_light_ref,
    match_light,
    parse_light_ref,
)


class TestRefs:
    def test_bare_id_is_hue(self):
        assert parse_light_ref("abc-123") == ("hue", "abc-123")

    def test_prefixed_refs(self):
        assert parse_light_ref("hue:abc") == ("hue", "abc")
        assert parse_light_ref("wiz:aabbccddeeff") == ("wiz", "aabbccddeeff")

    def test_format_round_trip(self):
        assert parse_light_ref(format_light_ref("wiz", "aabbccddeeff")) == (
            "wiz",
            "aabbccddeeff",
        )


class TestEffectiveRoleIds:
    def test_empty_role_falls_back_to_defaults(self):
        cfg = Config()
        assert effective_role_ids(cfg, "thinking", ["a", "b"]) == ["a", "b"]

    def test_configured_role_filters_by_backend(self):
        cfg = Config()
        cfg.roles.waiting = ["hue:a", "wiz:aabbccddeeff", "b"]
        assert effective_role_ids(cfg, "waiting", ["z"], backend="hue") == ["a", "b"]
        assert effective_role_ids(cfg, "waiting", [], backend="wiz") == ["aabbccddeeff"]

    def test_duplicates_dropped(self):
        cfg = Config()
        cfg.roles.thinking = ["hue:a", "a"]
        assert effective_role_ids(cfg, "thinking", []) == ["a"]


def _inventory():
    return [
        LightInfo(ref="hue:1", backend="hue", id="1", name="Desk Lamp"),
        LightInfo(ref="hue:2", backend="hue", id="2", name="Bookshelf"),
        LightInfo(
            ref="wiz:aabbccddeeff", backend="wiz", id="aabbccddeeff", name="Desk Strip"
        ),
    ]


class TestMatchLight:
    def test_exact_name_case_insensitive(self):
        assert match_light("desk lamp", _inventory()).ref == "hue:1"

    def test_unique_substring(self):
        assert match_light("book", _inventory()).ref == "hue:2"

    def test_raw_ref_and_raw_id(self):
        assert match_light("wiz:aabbccddeeff", _inventory()).name == "Desk Strip"
        assert match_light("2", _inventory()).name == "Bookshelf"

    def test_ambiguous_lists_candidates(self):
        with pytest.raises(MatchError, match="Desk Lamp.*Desk Strip"):
            match_light("desk", _inventory())

    def test_duplicate_names_do_not_expose_stable_refs(self):
        inventory = [
            LightInfo(
                ref="hue:private-one", backend="hue", id="private-one", name="Desk"
            ),
            LightInfo(
                ref="wiz:private-two", backend="wiz", id="private-two", name="Desk"
            ),
        ]
        with pytest.raises(MatchError) as raised:
            match_light("Desk", inventory)
        assert "private-one" not in str(raised.value)
        assert "private-two" not in str(raised.value)
        assert "rename one" in str(raised.value)

    def test_no_match_lists_known(self):
        with pytest.raises(MatchError, match="Bookshelf"):
            match_light("garage", _inventory())
