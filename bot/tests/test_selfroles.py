"""Pure self-assign role rules: panel sections, toggles, select handling, safety checks."""

import config
from logic import selfroles as S


def test_sections_come_from_config_in_panel_order():
    secs = S.sections()
    assert [s.key for s in secs] == ["platform", "region", "playtime", "pings", "games"]
    by = {s.key: s for s in secs}
    assert by["platform"].names == tuple(config.PLATFORM_ROLES)
    assert by["region"].names == tuple(config.REGION_ROLES)
    assert by["playtime"].names == tuple(config.PLAYTIME_ROLES)
    assert by["pings"].names == (config.GAMENIGHT_ROLE, config.LFG_ROLE, config.BUMPER_ROLE)
    assert by["games"].names == tuple(g.role for g in config.GAMES)
    assert by["region"].single and not by["platform"].single and not by["pings"].single
    assert by["games"].select and not by["platform"].select


def test_button_sections_fit_a_row_and_games_fit_a_select():
    for s in S.sections():
        if s.select:
            assert len(s.names) <= S.SELECT_MAX
        else:
            assert len(s.names) <= S.ROW_MAX


def test_games_are_capped_at_select_limit():
    many = [f"Game {i}" for i in range(40)]
    secs = S.sections(games=many)
    games = next(s for s in secs if s.key == "games")
    assert len(games.names) == S.SELECT_MAX
    assert games.names[0] == "Game 0"


def test_section_and_name_lookup_only_by_index():
    assert S.section("region").title == "Region"
    assert S.section("nope") is None
    assert S.name_at("platform", 0) == config.PLATFORM_ROLES[0]
    assert S.name_at("platform", 99) is None
    assert S.name_at("platform", -1) is None
    assert S.name_at("games", 0) is None  # games use the select, not buttons
    assert S.name_at("nope", 0) is None


def test_toggle_multi_adds_then_removes():
    plat = S.section("platform")
    assert S.toggle(plat, "PC", have=set()) == S.Change(add=("PC",), remove=())
    assert S.toggle(plat, "PC", have={"PC", "Xbox"}) == S.Change(add=(), remove=("PC",))
    assert S.toggle(plat, "Xbox", have={"PC"}) == S.Change(add=("Xbox",), remove=())


def test_toggle_single_choice_swaps_region():
    reg = S.section("region")
    assert S.toggle(reg, "EU", have={"NA", "PC"}) == S.Change(add=("EU",), remove=("NA",))
    assert S.toggle(reg, "EU", have={"EU"}) == S.Change(add=(), remove=("EU",))
    # Holding two regions (set by hand) collapses to the one picked.
    assert S.toggle(reg, "OCE", have={"NA", "EU"}) == S.Change(add=("OCE",), remove=("NA", "EU"))


def test_toggle_single_only_removes_available_others():
    reg = S.section("region")
    ch = S.toggle(reg, "EU", have={"NA", "Asia"}, available={"EU", "NA"})
    assert ch == S.Change(add=("EU",), remove=("NA",))


def test_toggle_unknown_name_is_noop():
    assert S.toggle(S.section("platform"), "Keeper", have=set()) == S.Change((), ())


def test_select_toggles_each_picked_game_and_ignores_unknown():
    games = S.section("games")
    a, b, c = games.names[:3]
    ch = S.select(games, [a, b, "Keeper", a], have={b, c})
    assert ch == S.Change(add=(a,), remove=(b,))


def test_select_by_index_values():
    games = S.section("games")
    assert S.select_values(games, ["0", "2", "x", "999", "-1", "2"]) == [games.names[0], games.names[2]]


def test_can_assign():
    assert S.can_assign(position=3, bot_top=10, managed=False, is_default=False, dangerous=False)
    assert not S.can_assign(position=10, bot_top=10, managed=False, is_default=False, dangerous=False)
    assert not S.can_assign(position=12, bot_top=10, managed=False, is_default=False, dangerous=False)
    assert not S.can_assign(position=3, bot_top=10, managed=True, is_default=False, dangerous=False)
    assert not S.can_assign(position=0, bot_top=10, managed=False, is_default=True, dangerous=False)
    assert not S.can_assign(position=3, bot_top=10, managed=False, is_default=False, dangerous=True)


def test_dangerous_permission_names_cover_staff_powers():
    for p in ("administrator", "manage_roles", "manage_guild", "ban_members", "kick_members",
              "mention_everyone", "manage_messages", "moderate_members"):
        assert p in S.DANGEROUS


def test_describe_change():
    assert S.describe(S.Change(("PC",), ())) == "Added PC."
    assert S.describe(S.Change(("EU",), ("NA",))) == "Added EU. Removed NA."
    assert S.describe(S.Change((), ("PC", "Xbox"))) == "Removed PC, Xbox."
    assert S.describe(S.Change((), ())) == "Nothing changed."


def test_panel_key_round_trip():
    assert S.parse_panel_ref(S.panel_ref(12, 34)) == (12, 34)
    assert S.parse_panel_ref(None) is None
    assert S.parse_panel_ref("garbage") is None
    assert S.parse_panel_ref("1:x") is None


def test_has_dangerous_permissions():
    import discord
    from types import SimpleNamespace
    assert S.has_dangerous_permissions(SimpleNamespace(permissions=discord.Permissions(mention_everyone=True)))
    assert S.has_dangerous_permissions(SimpleNamespace(permissions=discord.Permissions(manage_messages=True)))
    assert not S.has_dangerous_permissions(SimpleNamespace(permissions=discord.Permissions(send_messages=True)))
    assert not S.has_dangerous_permissions(SimpleNamespace())
