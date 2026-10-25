"""Tests for setup_server, public_mode and snapshot_server against fake guilds (no Discord)."""

import asyncio
from types import SimpleNamespace

import discord

import layout
import public_mode
import setup_server
import snapshot_server
from names import slug


class NS(SimpleNamespace):
    __hash__ = object.__hash__


class FakeRole(NS):
    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    def is_default(self):
        return self.name == "@everyone"


def aiter_of(items):
    async def gen():
        for i in items:
            yield i
    return gen()


class FakeGuild:
    """Records create_* calls; returns simple namespaces for what it creates."""

    def __init__(self, roles=()):
        self.default_role = FakeRole(id=1, name="@everyone", position=0, managed=False)
        self.me = NS(id=2, name="Front Desk", top_role=FakeRole(id=3, name="Front Desk", position=50))
        self.roles = [self.default_role, self.me.top_role, *roles]
        self.categories, self.text_channels, self.voice_channels, self.forums = [], [], [], []
        self.stage_channels = []
        self.calls = []
        self._next = 100

    @property
    def channels(self):
        return [*self.categories, *self.text_channels, *self.voice_channels, *self.forums, *self.stage_channels]

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def _make(self, kind, name, **kw):
        self._next += 1
        self.calls.append((kind, name, kw))
        cat = kw.get("category")
        return NS(id=self._next, name=name, category_id=cat.id if cat else None,
                  overwrites=kw.get("overwrites", {}), **({"edit": _async_none} if kind == "forum" else {}))

    async def create_category(self, name, **kw):
        return self._make("category", name, **kw)

    async def create_text_channel(self, name, **kw):
        return self._make("text", name, **kw)

    async def create_voice_channel(self, name, **kw):
        return self._make("voice", name, **kw)

    async def create_forum(self, name, **kw):
        return self._make("forum", name, **kw)

    async def create_stage_channel(self, name, **kw):
        return self._make("stage", name, **kw)

    async def create_role(self, **kw):
        self.calls.append(("role", kw["name"], kw))
        return FakeRole(id=0, position=1, **kw)


async def _async_none(**_):
    return None


# ------------------------------------------------------------ finding 14


def test_uncached_member_overwrite_targets_are_fetched_by_name():
    # discord.py 2.7 builds uncached member targets as Object(type=User), never Member.
    alice = discord.Object(id=555, type=discord.User)
    role_target = discord.Object(id=777, type=discord.Role)
    fetched = []

    async def fetch_member(mid):
        fetched.append(mid)
        return NS(name="alice")

    guild = NS(
        channels=[NS(overwrites={alice: None, role_target: None})],
        fetch_member=fetch_member,
    )
    names = asyncio.run(snapshot_server.member_names_for_overwrites(guild))
    assert names == {555: "alice"}
    assert fetched == [555]


# ------------------------------------------------------------ finding 15


def test_make_posts_finds_pinned_post_after_joins_push_it_out_of_history(monkeypatch):
    guild = FakeGuild()
    me = guild.me
    post_specs = [s for s in setup_server.layout_channels() if s.get("post")]
    m = setup_server.Makeover(guild, apply=True)
    pinned = {}
    for spec in post_specs:
        ch = guild._make("text", spec["name"])
        guild.text_channels.append(ch)
        m.channels[slug(spec["name"])] = ch
        old = NS(author=me, embeds=[object()], pinned=True)
        pinned[ch.id] = old
        joins = [NS(author=NS(id=900 + i), embeds=[]) for i in range(60)]
        ch.pins = lambda limit=50, old=old: aiter_of([old])
        ch.history = lambda limit=50, joins=joins: aiter_of(joins[:limit])

        async def send(**_):
            raise AssertionError("posted a duplicate")
        ch.send = send

    refreshed = []

    async def refresh_post(key, msg, post):
        refreshed.append(msg)
    monkeypatch.setattr(m, "refresh_post", refresh_post)

    asyncio.run(m.make_posts())
    assert len(refreshed) == len(post_specs)
    assert all(msg in pinned.values() for msg in refreshed)


def test_find_post_skips_pin_notices_in_history():
    guild = FakeGuild()
    me = guild.me
    post = NS(author=me, embeds=[object()], pinned=False)
    notice = NS(author=me, embeds=[])  # "Front Desk pinned a message", newer than the post
    ch = NS(pins=lambda limit=50: aiter_of([]), history=lambda limit=50: aiter_of([notice, post]))
    assert asyncio.run(setup_server.Makeover(guild, apply=True).find_post(ch)) is post


# ------------------------------------------------------------ finding 16


def _hidden(overwrites, guild):
    ow = overwrites.get(guild.default_role)
    return ow is not None and ow.view_channel is False


def test_setup_creates_private_to_spaces_hidden_from_everyone():
    keeper = FakeRole(id=10, name="Keeper", position=5, managed=False)
    squad = FakeRole(id=11, name="Squad", position=4, managed=False)
    mod = FakeRole(id=12, name="Moderator", position=6, managed=False)
    guild = FakeGuild(roles=[keeper, squad, mod])
    m = setup_server.Makeover(guild, apply=True)
    m.roles = {"Keeper": keeper, "Squad": squad}
    asyncio.run(m.make_channels())

    created = {name: kw for _, name, kw in guild.calls}
    private_names = set()
    for cat_spec in layout.CATEGORIES:
        if cat_spec.get("private_to"):
            private_names.add(cat_spec["name"])
        for spec in cat_spec["channels"]:
            if spec.get("private_to") or cat_spec.get("private_to"):
                private_names.add(spec["name"])
    assert {"06 · squad", "🔒・squad-chat", "🔒 Squad Only", "📋・mod-log"} <= private_names
    for name in private_names:
        ow = created[name].get("overwrites", {})
        assert _hidden(ow, guild), name
        assert ow[guild.me].view_channel is True
    assert created["06 · squad"]["overwrites"][squad].view_channel is True
    assert created["📋・mod-log"]["overwrites"][mod].view_channel is True
    assert not _hidden(created["💬・general"].get("overwrites", {}), guild)
    assert not _hidden(created["02 · the lobby"].get("overwrites", {}), guild)


# ------------------------------------------------------------ finding 17


def test_public_mode_creates_bot_roles_with_layout_hoist():
    guild = FakeGuild()
    guild.me.guild_permissions = NS(**{p: True for p in public_mode.NEEDED})
    asyncio.run(public_mode.PublicMode(guild, apply=True).roles())
    made = {name: kw for kind, name, kw in guild.calls if kind == "role"}
    assert made["Season Champ"]["hoist"] is True
    assert made["Hype"]["hoist"] is True
    assert made["Recruiter"]["hoist"] is False


def test_public_mode_hoists_existing_unhoisted_role():
    edits = []

    async def edit(**kw):
        edits.append(kw)

    hype = FakeRole(id=20, name="Hype", position=4, hoist=False, edit=edit)
    guild = FakeGuild(roles=[
        hype,
        *(FakeRole(id=21 + i, name=n, position=3, hoist=n in ("Season Champ", "Birthday", "Event Champ", "Supporter"))
          for i, n in enumerate(public_mode.BOT_ROLES) if n != "Hype"),
    ])
    asyncio.run(public_mode.PublicMode(guild, apply=True).roles())
    assert edits == [{"hoist": True, "reason": "Public server"}]
    assert not [c for c in guild.calls if c[0] == "role"]


# ------------------------------------------------------------ onboarding prompts (self-assign roles)


def _onboarding_guild(prompts=(), role_names=None):
    names = role_names if role_names is not None else [
        o["role"] for p in public_mode.ONBOARDING_PROMPTS for o in p["options"]]
    guild = FakeGuild(roles=[FakeRole(id=200 + i, name=n, position=5) for i, n in enumerate(names)])
    ob = NS(prompts=list(prompts))
    edits = []

    async def onboarding():
        return ob

    async def edit_onboarding(**kw):
        edits.append(kw)
    guild.onboarding = onboarding
    guild.edit_onboarding = edit_onboarding
    return guild, edits


def _existing_prompt(title):
    return discord.OnboardingPrompt(type=discord.OnboardingPromptType.multiple_choice, title=title,
                                    options=[discord.OnboardingPromptOption(title="x", roles=[discord.Object(9)])])


def test_onboarding_prompts_added_after_existing_ones():
    old = [_existing_prompt("What do you play?"), _existing_prompt("Want squad pings?")]
    guild, edits = _onboarding_guild(old)
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    [kw] = edits
    assert set(kw) == {"prompts"}  # default channels and the enabled flag are left alone
    prompts = kw["prompts"]
    assert prompts[:2] == old  # never removed or reordered
    new = {p.title: p for p in prompts[2:]}
    assert list(new) == ["What do you play on?", "Where are you?", "Want pings?"]
    assert new["What do you play on?"].single_select is False
    assert new["Where are you?"].single_select is True
    assert all(p.required is False for p in new.values())
    by_name = {r.name: r.id for r in guild.roles}
    assert [o.role_ids for o in new["What do you play on?"].options] == [
        {by_name[n]} for n in ("PC", "PlayStation", "Xbox", "Switch", "Mobile")]
    assert [o.role_ids for o in new["Want pings?"].options] == [{by_name["Game Night"]}]


def test_onboarding_prompts_dry_run_changes_nothing(capsys):
    guild, edits = _onboarding_guild()
    asyncio.run(public_mode.PublicMode(guild, apply=False).onboarding_prompts())
    assert edits == []
    assert "Where are you?" in capsys.readouterr().out


def test_onboarding_prompts_rerun_is_a_noop():
    have = [_existing_prompt(p["title"]) for p in public_mode.ONBOARDING_PROMPTS]
    guild, edits = _onboarding_guild(have)
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    assert edits == []


def test_onboarding_prompts_skip_missing_roles():
    guild, edits = _onboarding_guild(role_names=["PC", "Xbox"])
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    [kw] = edits
    [p] = kw["prompts"]  # region and ping roles don't exist yet: those prompts wait for a re-run
    assert p.title == "What do you play on?"
    assert [o.title for o in p.options] == ["PC", "Xbox"]


def test_onboarding_prompts_respect_discord_limit():
    full = [_existing_prompt(f"q{i}") for i in range(public_mode.MAX_PROMPTS - 1)]
    guild, edits = _onboarding_guild(full)
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    assert edits == []


def test_onboarding_prompts_only_platform_is_pre_join():
    guild, edits = _onboarding_guild([_existing_prompt("What do you play?")])
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    new = {p.title: p for p in edits[0]["prompts"][1:]}
    assert new["What do you play on?"].in_onboarding is True
    assert new["Where are you?"].in_onboarding is False
    assert new["Want pings?"].in_onboarding is False


def test_onboarding_prompts_fall_back_to_post_join_when_discord_caps_questions():
    guild, edits = _onboarding_guild([_existing_prompt("What do you play?")])

    class Resp:
        status, reason = 400, "Bad Request"

    async def edit_onboarding(**kw):
        edits.append(kw)
        if len(edits) == 1:
            raise discord.HTTPException(Resp(), {"code": 50035, "message": "Invalid Form Body",
                                                 "errors": {"prompts": {"_errors": [{"code": "x",
                                                 "message": "Too many questions in onboarding."}]}}})
    guild.edit_onboarding = edit_onboarding
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    assert len(edits) == 2
    assert edits[1]["prompts"][0].title == "What do you play?"
    assert all(p.in_onboarding is False for p in edits[1]["prompts"][1:])


# ------------------------------------------------------------ AutoMod gaps


def _rule(name, trigger):
    async def refuse(**_):
        raise AssertionError(f"{name} was edited or deleted")
    return NS(name=name, trigger=trigger, edit=refuse, delete=refuse)


def _live_rules():
    """What the server has today (server/snapshot/automod.json): no invite-link rule."""
    T = discord.AutoModRuleTriggerType
    return [
        _rule("Block Words in Member Profile Names", discord.AutoModTrigger(type=T.member_profile)),
        _rule("Front Desk: mention raids", discord.AutoModTrigger(mention_limit=6, mention_raid_protection=True)),
        _rule("Front Desk: slurs", discord.AutoModTrigger(
            presets=discord.AutoModPresets(slurs=True, sexual_content=True))),
        _rule("Front Desk: spam", discord.AutoModTrigger(type=T.spam)),
    ]


def _automod_guild(rules, invites=(), with_channels=True):
    keeper = FakeRole(id=10, name="Keeper", position=60)
    mod = FakeRole(id=12, name="Moderator", position=55)
    guild = FakeGuild(roles=[keeper, mod])
    if with_channels:
        guild.categories.append(NS(id=40, name=public_mode.AUTOMOD_EXEMPT_CATEGORY))
        guild.text_channels.append(NS(id=41, name=layout.AUTOMOD_ALERTS))
        guild.text_channels.append(NS(id=42, name="📋・mod-log"))  # not where the alerts go
    created = []

    async def fetch_automod_rules():
        return list(rules)

    async def create_automod_rule(**kw):
        created.append(kw)

    async def invites_():
        return list(invites)
    guild.fetch_automod_rules = fetch_automod_rules
    guild.create_automod_rule = create_automod_rule
    guild.invites = invites_
    guild.vanity_url_code = None
    return guild, created


def test_automod_gaps_adds_only_the_invite_rule_to_the_live_set():
    invites = [NS(code="ourcode", max_age=0), NS(code="temp", max_age=3600)]
    guild, created = _automod_guild(_live_rules(), invites)
    asyncio.run(public_mode.PublicMode(guild, apply=True).automod_gaps())
    [kw] = created
    assert kw["name"] == public_mode.AUTOMOD_INVITES
    assert kw["enabled"] is True
    assert kw["event_type"] == discord.AutoModRuleEventType.message_send
    t = kw["trigger"]
    assert t.type == discord.AutoModRuleTriggerType.keyword
    assert t.regex_patterns == public_mode.INVITE_PATTERNS
    assert t.allow_list == ["discord.gg/ourcode"]  # our permanent invite is fine; temp ones expire
    assert [r.name for r in kw["exempt_roles"]] == ["Keeper", "Moderator"]
    assert [c.id for c in kw["exempt_channels"]] == [40]  # the staff category
    types = {a.type for a in kw["actions"]}
    assert types == {discord.AutoModRuleActionType.block_message, discord.AutoModRuleActionType.send_alert_message}
    alert = next(a for a in kw["actions"] if a.type == discord.AutoModRuleActionType.send_alert_message)
    assert alert.channel_id == 41  # layout.AUTOMOD_ALERTS, the channel polish_server.py's rules alert in
    assert not hasattr(public_mode, "AUTOMOD_ALERTS")  # one constant, in layout.py


def test_automod_invite_patterns_match_invites_not_other_links():
    import re
    pats = [re.compile(p, re.I) for p in public_mode.INVITE_PATTERNS]
    hit = ["join discord.gg/abc123", "https://discord.com/invite/Xyz", "discordapp.com/invite/a-b", "dsc.gg/foo",
           "DISCORD.GG/ABC"]
    miss = ["https://discord.com/channels/1/2", "discord gg", "https://youtube.com/invite", "discord.com/store"]
    assert all(any(p.search(s) for p in pats) for s in hit)
    assert not any(p.search(s) for p in pats for s in miss)
    assert all(len(p) <= 260 for p in public_mode.INVITE_PATTERNS)


def test_automod_gaps_creates_every_missing_builtin_rule():
    guild, created = _automod_guild([])
    asyncio.run(public_mode.PublicMode(guild, apply=True).automod_gaps())
    T = discord.AutoModRuleTriggerType
    by_type = {kw["trigger"].type: kw for kw in created}
    assert set(by_type) == {T.spam, T.mention_spam, T.keyword_preset, T.keyword}
    preset = by_type[T.keyword_preset]["trigger"].presets
    assert preset.slurs and preset.sexual_content and not preset.profanity  # trash talk is fine
    mention = by_type[T.mention_spam]
    assert mention["trigger"].mention_limit == layout.MENTION_LIMIT
    assert mention["trigger"].mention_raid_protection is True
    assert any(a.type == discord.AutoModRuleActionType.timeout for a in mention["actions"])
    assert all(kw["exempt_roles"] and kw["exempt_channels"] for kw in created)


def test_automod_gaps_dry_run_changes_nothing(capsys):
    guild, created = _automod_guild(_live_rules())
    asyncio.run(public_mode.PublicMode(guild, apply=False).automod_gaps())
    assert created == []
    out = capsys.readouterr().out
    assert public_mode.AUTOMOD_INVITES in out
    assert "ok      spam filter" in out


def test_automod_gaps_rerun_is_a_noop_and_never_edits():
    T = discord.AutoModRuleTriggerType
    rules = [*_live_rules(), _rule("hand-made", discord.AutoModTrigger(type=T.keyword, keyword_filter=["*discord.gg/*"]))]
    guild, created = _automod_guild(rules)
    asyncio.run(public_mode.PublicMode(guild, apply=True).automod_gaps())
    assert created == []


def test_automod_gaps_reports_but_keeps_a_weaker_preset_rule(capsys):
    T = discord.AutoModRuleTriggerType
    rules = [_rule("spam", discord.AutoModTrigger(type=T.spam)),
             _rule("mentions", discord.AutoModTrigger(mention_limit=10)),
             _rule("words", discord.AutoModTrigger(presets=discord.AutoModPresets(slurs=True))),
             _rule("invites", discord.AutoModTrigger(regex_patterns=[r"discord\.gg/\w+"]))]
    guild, created = _automod_guild(rules)
    asyncio.run(public_mode.PublicMode(guild, apply=True).automod_gaps())
    assert created == []  # one preset rule per server; this step never edits it
    assert "lacks the sexual content preset" in capsys.readouterr().out


def test_automod_gaps_respects_keyword_rule_limit(capsys):
    T = discord.AutoModRuleTriggerType
    rules = [*_live_rules(), *(_rule(f"words {i}", discord.AutoModTrigger(type=T.keyword, keyword_filter=[f"w{i}"]))
                               for i in range(public_mode.MAX_KEYWORD_RULES))]
    guild, created = _automod_guild(rules)
    asyncio.run(public_mode.PublicMode(guild, apply=True).automod_gaps())
    assert created == []
    assert "keyword rules" in capsys.readouterr().out


def test_automod_gaps_skips_without_the_mod_log():
    guild, created = _automod_guild([], with_channels=False)
    asyncio.run(public_mode.PublicMode(guild, apply=True).automod_gaps())
    assert created == []


def test_public_mode_run_includes_automod_gaps(monkeypatch):
    guild = FakeGuild()
    guild.name = "test"
    guild.me.guild_permissions = NS(**{p: True for p in public_mode.NEEDED})
    ran = []
    # The games steps: additive ones first, and retire_games (the only step that hides
    # anything) after the Onboarding question has been brought up to date.
    steps = ("roles", "renamed_channels", "new_channels", "private", "description", "posts", "invite", "onboarding",
             "onboarding_prompts", "game_tags", "restore_games", "game_options", "retire_games", "automod_gaps")
    pm = public_mode.PublicMode(guild, apply=False)
    for name in steps:
        async def step(name=name):
            ran.append(name)
        monkeypatch.setattr(pm, name, step)
    assert asyncio.run(pm.run()) is True
    assert ran == list(steps)


def _renamable(guild, name):
    edits = []

    async def edit(**kw):
        edits.append(kw)
    ch = NS(id=800 + len(guild.text_channels), name=name, category_id=None, overwrites={}, edit=edit)
    guild.text_channels.append(ch)
    return edits


def _was_spec():
    return _spec("⛏️・monkey-smp")  # its old name slugs differently, so it really is a rename


def test_renamed_channels_gives_an_old_name_its_layout_name():
    guild = FakeGuild()
    spec = _was_spec()
    edits = _renamable(guild, spec["was"][0])
    asyncio.run(public_mode.PublicMode(guild, apply=True).renamed_channels())
    assert edits == [{"name": spec["name"], "reason": "Public server: renamed"}]


def test_renamed_channels_dry_run_and_current_name_change_nothing(capsys):
    guild = FakeGuild()
    spec = _was_spec()
    old = _renamable(guild, spec["was"][0])
    asyncio.run(public_mode.PublicMode(guild, apply=False).renamed_channels())
    assert old == [] and f"-> #{spec['name']}" in capsys.readouterr().out
    current = _renamable(guild, spec["name"])  # the layout name exists: the old one is left alone
    asyncio.run(public_mode.PublicMode(guild, apply=True).renamed_channels())
    assert old == [] and current == []


def test_monkey_smp_channel_keeps_its_old_name_for_the_rename():
    spec = _spec("⛏️・monkey-smp")
    assert spec["was"] == ["⛏️・realms"]


def _read_only_channel(guild, name, allowed):
    sets = []

    async def set_permissions(target, overwrite=None, reason=None):
        sets.append((target, overwrite))
    ch = NS(id=900 + len(guild.text_channels), name=name, category_id=None, overwrites={},
            permissions_for=lambda me: discord.Permissions(**{p: True for p in allowed}),
            overwrites_for=lambda target: discord.PermissionOverwrite(add_reactions=True),
            set_permissions=set_permissions)
    guild.text_channels.append(ch)
    return ch, sets


def test_bot_posting_adds_overwrite_only_where_the_bot_cannot_post():
    guild = FakeGuild()
    _, blocked = _read_only_channel(guild, "📣・announcements", ["view_channel"])
    _, fine = _read_only_channel(guild, "🏆・tournaments", ["send_messages", "embed_links", "attach_files"])
    _, not_read_only = _read_only_channel(guild, "💬・general", ["view_channel"])
    asyncio.run(public_mode.PublicMode(guild, apply=True).bot_posting())
    [(target, ow)] = blocked
    assert target is guild.me
    assert ow.send_messages is True and ow.embed_links is True and ow.attach_files is True
    assert ow.add_reactions is True  # what was already there is kept
    assert fine == [] and not_read_only == []


def test_bot_posting_dry_run_changes_nothing():
    guild = FakeGuild()
    _, sets = _read_only_channel(guild, "👋・welcome", ["view_channel"])
    asyncio.run(public_mode.PublicMode(guild, apply=False).bot_posting())
    assert sets == []


def test_bot_posting_lets_the_bot_open_ticket_threads_in_help():
    guild = FakeGuild()
    _, sets = _read_only_channel(guild, "🆘・help", ["send_messages", "embed_links", "attach_files"])
    asyncio.run(public_mode.PublicMode(guild, apply=True).bot_posting())
    [(target, ow)] = sets
    assert target is guild.me
    assert ow.create_private_threads is True and ow.send_messages_in_threads is True and ow.manage_threads is True


# ------------------------------------------------------------ forums and the stage


def _spec(name):
    return next(s for s in setup_server.layout_channels() if slug(s["name"]) == slug(name))


def _with_categories(guild):
    for cat_spec in layout.CATEGORIES:
        guild.categories.append(NS(id=len(guild.categories) + 10, name=cat_spec["name"], overwrites={}))
    return guild


def test_layout_forums_and_stage_fit_discords_limits():
    forums = [s for s in setup_server.layout_channels() if s.get("type") == "forum"]
    assert {"guides", "setups"} <= {slug(s["name"]) for s in forums}
    for spec in forums:
        tags = spec.get("tags", [])
        assert len(tags) <= 20, spec["name"]  # Discord: 20 tags per forum
        assert len({n for n, _ in tags}) == len(tags)
        assert all(0 < len(n) <= 20 for n, _ in tags), spec["name"]
        assert 0 < len(spec["topic"]) <= 4096
        setup_server.forum_options(spec)  # every layout/sort name is one the scripts know
    guides = _spec("guides")
    assert {role for _, _, role in layout.GAMES} <= {n for n, _ in guides["tags"]}  # one tag per game
    assert _spec("setups")["layout"] == "gallery"
    assert _spec("stage")["type"] == "stage"
    # Public channels must be reachable through Onboarding: a default or an opt-in answer
    # (tests/test_focus_mode.py checks every one). The Stage is the exception: Discord may
    # refuse one as a default (see layout.py).
    reachable = {slug(n) for n in (*layout.ONBOARDING_DEFAULT_CHANNELS,
                                   *(n for g in layout.FOCUS_GROUPS for n in g["channels"]))}
    assert {"guides", "setups"} <= reachable and "stage" not in reachable
    assert {"guides", "setups"} <= {slug(n) for n in layout.ONBOARDING_EVERYTHING}


def test_forum_options_only_pass_what_the_spec_sets():
    assert setup_server.forum_options({"name": "x"}) == {"default_layout": discord.ForumLayoutType.list_view}
    assert setup_server.forum_options(_spec("setups")) == {
        "default_layout": discord.ForumLayoutType.gallery_view,
        "default_sort_order": discord.ForumOrderType.creation_date,
        "default_reaction_emoji": "🔥",
        "slowmode_delay": 600,
        "default_thread_slowmode_delay": 5,
    }


def test_public_mode_creates_new_forums_and_the_stage_once(capsys):
    guild = _with_categories(FakeGuild())
    asyncio.run(public_mode.PublicMode(guild, apply=True).new_channels())
    made = {slug(name): (kind, kw) for kind, name, kw in guild.calls}
    kind, kw = made["guides"]
    assert kind == "forum" and kw["category"].name == "03 · games"
    assert {t.name for t in kw["available_tags"]} >= {"Minecraft", "Beginner", "Question"}
    assert kw["default_layout"] is discord.ForumLayoutType.list_view
    assert kw["default_sort_order"] is discord.ForumOrderType.latest_activity
    assert kw["default_reaction_emoji"] == "👍" and kw["slowmode_delay"] == 600
    assert "overwrites" not in kw  # same permissions as the other public channels in the category
    kind, kw = made["setups"]
    assert kind == "forum" and kw["default_layout"] is discord.ForumLayoutType.gallery_view
    kind, kw = made["stage"]
    assert kind == "stage" and kw["category"].name == "05 · voice"
    assert made["general"][0] == "text" and made["lobby"][0] == "voice"  # the stage isn't mistaken for either
    assert "can't schedule Stage events without Manage Channels, Mute Members, Move Members" in capsys.readouterr().out

    # Second run with everything in place: nothing is created, moved or edited.
    for kind, name, kw in guild.calls:
        pool = {"forum": guild.forums, "stage": guild.stage_channels, "voice": guild.voice_channels,
                "text": guild.text_channels}[kind]
        pool.append(NS(id=1000 + len(pool), name=name, overwrites={},
                       permissions_for=lambda me: discord.Permissions.all()))
    guild.calls.clear()
    asyncio.run(public_mode.PublicMode(guild, apply=True).new_channels())
    assert guild.calls == []
    assert "Stage events" not in capsys.readouterr().out


def test_public_mode_forums_and_stage_dry_run_and_missing_category(capsys):
    guild = _with_categories(FakeGuild())
    asyncio.run(public_mode.PublicMode(guild, apply=False).new_channels())
    assert guild.calls == []
    out = capsys.readouterr().out
    assert "#📚・guides (forum) in 03 · games with 12 tags (layout, sort, reaction, post_slowmode, slowmode)" in out
    assert "🎙・stage (stage) in 05 · voice" in out

    bare = FakeGuild()  # no categories at all: skipped with a reason, never created loose
    asyncio.run(public_mode.PublicMode(bare, apply=True).new_channels())
    assert bare.calls == []
    assert "🎙・stage (stage): category 05 · voice doesn't exist" in capsys.readouterr().out


def test_public_mode_reports_a_stage_the_bot_cannot_schedule_on(capsys):
    guild = _with_categories(FakeGuild())
    guild.stage_channels.append(NS(id=77, name="🎙・stage", overwrites={}, permissions_for=lambda me: discord.Permissions(
        manage_channels=True, move_members=True)))
    asyncio.run(public_mode.PublicMode(guild, apply=True).new_stage(_spec("stage"), guild.categories[0], "05 · voice"))
    assert guild.calls == []
    out = capsys.readouterr().out
    assert "without Mute Members (" in out and "Manage Channels" not in out


def test_setup_creates_the_stage_as_a_stage_and_forums_with_their_settings():
    guild = FakeGuild()
    m = setup_server.Makeover(guild, apply=True)
    asyncio.run(m.make_channels())
    made = {slug(name): (kind, kw) for kind, name, kw in guild.calls}
    assert made["stage"][0] == "stage"
    assert made["setups"][1]["default_layout"] is discord.ForumLayoutType.gallery_view
    assert made["lfg"][1]["default_layout"] is discord.ForumLayoutType.list_view  # unchanged for the old forums
    # Re-running finds the stage instead of making a text channel with its name.
    guild.stage_channels.append(NS(id=5, name="🎙・stage", category_id=None))
    guild.calls.clear()
    asyncio.run(setup_server.Makeover(guild, apply=True).make_stage(_spec("stage"), None))
    assert guild.calls == []


# ------------------------------------------------------------ wave 9: everything new, on a server that has none of it

WAVE9_ROLES = ("House Fern", "House Kestrel", "House Otter", "Welcome Crew", "Supporter")
WAVE9_PUBLIC = {"📚・guides": "forum", "🖥・setups": "forum", "🎙・stage": "stage", "⛏️・monkey-smp": "text"}
WAVE9_PRIVATE = {"💜・supporters": {"Supporter", "Keeper", "Moderator"}, "🔒・appeals": {"Keeper", "Moderator"}}


def _wave9_guild():
    """A server from before wave 9: staff roles above Front Desk, every category, no new channel."""
    keeper = FakeRole(id=10, name="Keeper", position=60, managed=False)
    mod = FakeRole(id=12, name="Moderator", position=55, managed=False)
    guild = FakeGuild(roles=[keeper, mod, FakeRole(id=11, name="Squad", position=4, managed=False)])
    guild.name = "test"
    guild.me.guild_permissions = discord.Permissions.all()
    for cat_spec in layout.CATEGORIES:
        want = setup_server.private_overwrites(guild, cat_spec.get("private_to", ()))
        guild.categories.append(NS(  # private categories are already private, as on the live server
            id=len(guild.categories) + 10, name=cat_spec["name"], overwrites={},
            permissions_for=lambda me: discord.Permissions.all(),
            overwrites_for=lambda target, want=want: want.get(target, discord.PermissionOverwrite())))
    return guild


async def _wave9_steps(pm):
    await pm.roles()
    await pm.new_channels()
    await pm.bot_posting()
    await pm.private()


def test_public_mode_dry_run_lists_everything_wave9_adds_and_changes_nothing(capsys):
    guild = _wave9_guild()
    asyncio.run(_wave9_steps(public_mode.PublicMode(guild, apply=False)))
    assert guild.calls == []
    out = capsys.readouterr().out
    assert set(layout.HOUSE_ROLES.values()) == set(WAVE9_ROLES[:3])
    for name in WAVE9_ROLES:
        assert f"  create  @{name}\n" in out, name
    assert "create  #📚・guides (forum) in 03 · games" in out
    assert "create  #🖥・setups (forum) in " in out
    assert "create  🎙・stage (stage) in 05 · voice" in out
    assert "create  #⛏️・monkey-smp in 03 · games" in out
    assert "create  #💜・supporters (private)" in out
    assert "create  #🔒・appeals (private)" in out


def test_public_mode_apply_creates_wave9_roles_at_the_bottom_and_private_channels_private():
    guild = _wave9_guild()
    assert not hasattr(guild, "edit_role_positions")  # the script has no way to move a role
    asyncio.run(_wave9_steps(public_mode.PublicMode(guild, apply=True)))
    roles = {name: kw for kind, name, kw in guild.calls if kind == "role"}
    assert set(WAVE9_ROLES) <= set(roles)
    for name, kw in roles.items():
        # no position: Discord creates the role at the bottom, under Front Desk, and moves nothing
        assert set(kw) == {"name", "colour", "hoist", "mentionable", "reason"}, name
        assert kw["colour"].value == next(r.get("color", 0) for r in layout.ROLES if r.get("name") == name)
    made = {name: (kind, kw) for kind, name, kw in guild.calls if kind != "role"}
    for name, kind in WAVE9_PUBLIC.items():
        assert made[name][0] == kind, name
        assert "overwrites" not in made[name][1], name  # public: the category's own permissions
    by_name = {r.name: r for r in guild.roles}
    for name, who in WAVE9_PRIVATE.items():
        kind, kw = made[name]
        ow = kw["overwrites"]
        assert kind == "text" and _hidden(ow, guild) and ow[guild.me].view_channel is True, name
        seen = {target.name for target, o in ow.items() if o.view_channel and target is not guild.me}
        assert seen == who, name  # exactly the roles layout.py names, nobody else
        assert all(ow[by_name[r]].view_channel for r in who & set(by_name)), name
    # Supporter was created a moment ago in the same run and is not in guild.roles yet:
    # the lounge still lets it in, instead of waiting for a second run.
    assert "Supporter" not in by_name
    supporter = next(t for t in made["💜・supporters"][1]["overwrites"] if getattr(t, "name", "") == "Supporter")
    assert supporter.position < guild.me.top_role.position
    assert not any(kind == "category" for kind, _, _ in guild.calls)  # nothing existing was recreated


def test_bot_posting_ignores_forums_and_the_stage():
    """A forum or Stage has no text-channel permissions to fix and is never looked up as one."""
    guild = _wave9_guild()

    def explode(*_):
        raise AssertionError("bot_posting touched a forum or the stage")
    for name, kind in WAVE9_PUBLIC.items():
        pool = {"forum": guild.forums, "stage": guild.stage_channels, "text": guild.text_channels}[kind]
        pool.append(NS(id=700 + len(pool), name=name, overwrites={}, permissions_for=explode,
                       overwrites_for=explode, set_permissions=explode))
    _, sets = _read_only_channel(guild, "📣・announcements", ["view_channel"])
    asyncio.run(public_mode.PublicMode(guild, apply=True).bot_posting())
    assert len(sets) == 1
    read_only = {slug(s["name"]) for s in setup_server.layout_channels() if s.get("read_only")}
    assert not read_only & {slug(n) for n in (*WAVE9_PUBLIC, *WAVE9_PRIVATE)}


def test_onboarding_defaults_are_ordinary_public_channels_only():
    specs = {}
    for cat_spec in layout.CATEGORIES:
        for spec in cat_spec["channels"]:
            specs[slug(spec["name"])] = (spec.get("type", "text"),
                                         bool(spec.get("private_to") or cat_spec.get("private_to")))
    # The core, and the full list `focus_mode.py --revert` restores.
    for name in (*layout.ONBOARDING_DEFAULT_CHANNELS, *layout.ONBOARDING_EVERYTHING):
        kind, private = specs[slug(name)]  # KeyError: a default that is not in the layout
        assert kind in ("text", "forum", "voice") and not private, name
    everything = {slug(n) for n in layout.ONBOARDING_EVERYTHING}
    assert {slug(n) for n in layout.ONBOARDING_DEFAULT_CHANNELS} <= everything
    assert {slug(n) for n in ("📚・guides", "🖥・setups", "⛏️・monkey-smp")} <= everything
    assert not everything & {slug(n) for n in ("🎙・stage", "💜・supporters", "🔒・appeals", "📋・mod-log")}


def test_onboarding_step_adds_forums_but_never_the_stage_or_a_private_channel(capsys, monkeypatch):
    # public_mode only ever adds what layout.py calls a default; with the full list as the
    # defaults (the state before focus_mode.py, and after --revert) it adds the wave 9 channels.
    monkeypatch.setattr(layout, "ONBOARDING_DEFAULT_CHANNELS", layout.ONBOARDING_EVERYTHING)
    guild = _wave9_guild()
    existing = NS(id=500, name="💬・general")
    guild.text_channels += [existing, NS(id=501, name="⛏️・monkey-smp"), NS(id=502, name="💜・supporters"),
                            NS(id=503, name="🔒・appeals")]
    guild.forums += [NS(id=510, name="📚・guides"), NS(id=511, name="🖥・setups")]
    guild.stage_channels.append(NS(id=520, name="🎙・stage"))
    ob = NS(default_channels=[existing], prompts=[])
    edits = []

    async def onboarding():
        return ob

    async def edit_onboarding(**kw):
        edits.append(kw)
    guild.onboarding, guild.edit_onboarding = onboarding, edit_onboarding
    asyncio.run(public_mode.PublicMode(guild, apply=False).onboarding())
    assert edits == []
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding())
    (edit,) = edits
    names = [c.name for c in edit["default_channels"]]
    assert names == ["💬・general", "📚・guides", "🖥・setups", "⛏️・monkey-smp"]  # the existing ones first, kept
    assert "🎙・stage" not in capsys.readouterr().out
