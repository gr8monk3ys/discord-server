from types import SimpleNamespace

import config


def named(*names):
    return [SimpleNamespace(name=n) for n in names]


def test_every_layout_game_is_registered():
    assert [g.role for g in config.GAMES] == [role for _, _, role in config.layout.GAMES]


def test_game_keys_are_unique_and_round_trip():
    keys = [g.key for g in config.GAMES]
    assert len(keys) == len(set(keys))
    for g in config.GAMES:
        assert config.game_by_key(g.key) is g


def test_counter_strike_matches_role_tag_and_channel_separately():
    cs = next(g for g in config.GAMES if g.channel == "counter-strike")
    roles = named("Counter-Strike 2", "Valorant")
    channels = named("💣・counter-strike", "🎯・valorant")
    assert config.match_by_name(roles, cs.role).name == "Counter-Strike 2"
    assert config.match_by_name(channels, cs.channel_name).name == "💣・counter-strike"
    # The bug the registry avoids: deriving the role from the channel slug.
    assert config.match_by_name(roles, cs.channel) is None


def test_match_ignores_emoji_and_separators():
    assert config.match_by_name(named("🎮・lfg", "💬・general"), config.LFG_FORUM).name == "🎮・lfg"
    assert config.match_by_name(named("🎮 Squad", "🔊 Lobby"), "🔊 Lobby").name == "🔊 Lobby"
    assert config.match_by_name(named("general"), "missing") is None


def test_unknown_game_key():
    assert config.game_by_key("tetris") is None
