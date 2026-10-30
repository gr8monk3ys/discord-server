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
    # A retired game, and the only one whose role and channel slugs differ.
    cs = next(g for g in config.RETIRED_GAMES if g.channel == "counter-strike")
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


def test_old_names_come_from_layout():
    assert config.MONKEY_CHANNEL_WAS == ("⛏️・realms",)
    assert config.layout_old_names("01 · front desk") == ("Welcome",)  # categories too
    assert config.layout_old_names(config.GENERAL_CHANNEL) == ()
    assert config.layout_old_names("not in the layout") == ()


def test_match_by_names_takes_the_first_name_that_matches():
    both = named("⛏️・realms", "⛏️・monkey-smp")
    assert config.match_by_names(both, ("⛏️・monkey-smp", "⛏️・realms")).name == "⛏️・monkey-smp"
    assert config.match_by_names(named("⛏️・realms"), ("⛏️・monkey-smp", "⛏️・realms")).name == "⛏️・realms"
    assert config.match_by_names(named("general"), ("⛏️・monkey-smp", "⛏️・realms")) is None
    assert config.match_by_names(both, ()) is None


def test_unknown_game_key():
    assert config.game_by_key("tetris") is None


def test_retired_games_are_known_but_not_listed():
    assert [g.role for g in config.RETIRED_GAMES] == [role for _, _, role in config.layout.RETIRED_GAMES]
    listed, retired = {g.key for g in config.GAMES}, {g.key for g in config.RETIRED_GAMES}
    assert listed and retired and not listed & retired
    for g in config.RETIRED_GAMES:
        assert config.game_by_key(g.key) is None  # nothing a member can start resolves it
        assert config.known_game_by_key(g.key) is g  # old rows still show its emoji and name
    for g in config.GAMES:
        assert config.known_game_by_key(g.key) is g
    assert config.known_game_by_key("tetris") is None
    assert config.known_game_by_key("valorant").channel_name == "🎯・valorant"


def test_monkey_settings_fall_back_to_their_old_realms_names():
    assert config.MONKEY_ENV_WAS["MONKEY_ADDRESS"] == "REALMS_ADDRESS"
    old = {"REALMS_ADDRESS": "127.0.0.1:25565", "REALMS_LOG_PATH": "/srv/old.log"}
    assert config.monkey_environ(old)["MONKEY_ADDRESS"] == "127.0.0.1:25565"
    both = {**old, "MONKEY_LOG_PATH": "/srv/new.log", "MONKEY_RCON_PORT": ""}
    got = config.monkey_environ(both)
    assert got["MONKEY_LOG_PATH"] == "/srv/new.log"  # the new name wins
    assert got["MONKEY_RCON_PORT"] == "" and got["MONKEY_RCON_PASSWORD"] == ""
    assert set(got) == set(config.MONKEY_ENV)
