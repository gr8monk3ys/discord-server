"""The Monkey SMP season channels (layout.py) and how public_mode.py adds them: the channels
themselves, the founding roster's slow mode and no-threads permissions, and the Minecraft answer
in "What do you play?" that opens them, and the pinned roster, rules and FAQ posts. Fake guilds
only, no Discord."""

import asyncio
import re

import discord

import focus_mode
import layout
import public_mode
import setup_server
from names import slug
from test_games import old_server
from test_server_scripts import NS, _wave9_guild, _wave9_steps

ROSTER, FORUM, RULES, FAQ = "📝・founding-roster", "🚩・smp-forum", "📜・smp-rules", "❔・smp-faq"
SMP = "⛏️・monkey-smp"
OPENED_BY_MINECRAFT = [SMP, ROSTER, FORUM, RULES]
DISCORD_MAX_SLOWMODE = 21600
MAX_TAG_NAME = 20


def spec(name):
    return next(s for c in layout.CATEGORIES for s in c["channels"] if s["name"] == name)


def games_category():
    return next(c for c in layout.CATEGORIES if any(s["name"] == SMP for s in c["channels"]))


def minecraft_option():
    prompt = next(p for p in layout.ONBOARDING_PROMPTS if p["title"] == public_mode.GAMES_PROMPT)
    return next(o for o in prompt["options"] if o.get("role") == "Minecraft")


# ---------------------------------------------------------------- the layout
def test_the_season_channels_sit_right_after_monkey_smp_and_are_public():
    cat = games_category()
    names = [s["name"] for s in cat["channels"]]
    at = names.index(SMP)
    assert names[at + 1:at + 5] == [ROSTER, RULES, FAQ, FORUM]
    assert not cat.get("private_to")
    for name in (ROSTER, RULES, FAQ, FORUM):
        assert not spec(name).get("private_to"), name
    assert len({slug(n) for c in layout.CATEGORIES for n in (s["name"] for s in c["channels"])}) == \
        sum(len(c["channels"]) for c in layout.CATEGORIES)  # no slug is used twice


def test_founding_roster_everyone_posts_six_hour_slow_mode_no_threads():
    roster = spec(ROSTER)
    assert roster.get("type", "text") == "text"
    assert not roster.get("read_only")
    assert roster["slowmode"] == 6 * 3600 <= DISCORD_MAX_SLOWMODE
    assert roster["no_threads"] is True


def test_rules_and_faq_are_read_only_and_the_forum_is_a_forum():
    assert spec(RULES)["read_only"] and spec(FAQ)["read_only"]
    forum = spec(FORUM)
    assert forum["type"] == "forum" and not forum.get("read_only")
    tags = forum["tags"]
    assert "Faction" in {n for n, _ in tags} and "Looking for faction" in {n for n, _ in tags}
    assert all(0 < len(n) <= MAX_TAG_NAME for n, _ in tags)
    assert len(tags) <= public_mode.MAX_FORUM_TAGS
    for text in (spec(ROSTER)["topic"], spec(RULES)["topic"], spec(FAQ)["topic"], forum["topic"]):
        assert len(text) <= 1024  # Discord's limit for a channel topic


def test_minecraft_opens_the_smp_channels_and_hands_out_no_smp_ping():
    option = minecraft_option()
    assert option["channels"][0] == "⛏️・minecraft"
    assert option["channels"][1:] == OPENED_BY_MINECRAFT
    assert option["role"] == "Minecraft"
    for prompt in layout.ONBOARDING_PROMPTS:
        assert "SMP Ping" not in {o.get("role") for o in prompt["options"]}
    for row in public_mode.ONBOARDING_PROMPTS:
        assert "SMP Ping" not in {o.get("role") for o in row["options"]}
    group = next(g for g in layout.FOCUS_GROUPS if g["title"] == "Minecraft")
    assert set(OPENED_BY_MINECRAFT) <= set(group["channels"])
    assert FAQ in group["channels"]  # every public channel needs a home (focus_mode.classify)


def test_extra_answer_channels_are_public_layout_channels():
    rows = {r["name"]: r for r in focus_mode.classify()}
    for game, names in layout.GAME_EXTRA_CHANNELS.items():
        assert game in {role for _, _, role in layout.GAMES}, game
        for name in names:
            assert rows[name]["status"] != focus_mode.PRIVATE, name
            assert rows[name]["type"] in ("text", "forum"), name


# ---------------------------------------------------------------- public_mode: new channels
def test_dry_run_lists_the_season_channels_and_changes_nothing(capsys):
    guild = _wave9_guild()
    asyncio.run(_wave9_steps(public_mode.PublicMode(guild, apply=False)))
    assert guild.calls == []
    out = capsys.readouterr().out
    cat = games_category()["name"]
    assert f"create  #{ROSTER} in {cat} (no threads, slow mode 21600 s)" in out
    assert f"create  #{RULES} in {cat} (read-only)" in out
    assert f"create  #{FAQ} in {cat} (read-only)" in out
    assert f"create  #{FORUM} (forum) in {cat} with {len(spec(FORUM)['tags'])} tags" in out


def test_apply_creates_the_roster_with_slow_mode_and_no_threads_but_posting_on():
    guild = _wave9_guild()
    asyncio.run(_wave9_steps(public_mode.PublicMode(guild, apply=True)))
    made = {name: (kind, kw) for kind, name, kw in guild.calls if kind != "role"}
    kind, kw = made[ROSTER]
    assert kind == "text" and kw["slowmode_delay"] == 21600 and kw["topic"] == spec(ROSTER)["topic"]
    everyone = kw["overwrites"][guild.default_role]
    assert everyone.create_public_threads is False and everyone.create_private_threads is False
    assert everyone.send_messages is None and everyone.view_channel is None  # posting and reading stay on
    for name in (RULES, FAQ):
        kind, kw = made[name]
        assert kind == "text" and kw["overwrites"][guild.default_role].send_messages is False, name
    kind, kw = made[FORUM]
    assert kind == "forum" and [t.name for t in kw["available_tags"]] == [n for n, _ in spec(FORUM)["tags"]]


def test_no_threads_keeps_the_categorys_own_everyone_overwrite():
    guild = NS(default_role=object())
    keep = discord.PermissionOverwrite(attach_files=False)
    other = object()
    cat = NS(overwrites={guild.default_role: keep, other: discord.PermissionOverwrite(send_messages=True)})
    ow = setup_server.no_thread_overwrites(guild, cat)
    assert ow[guild.default_role].attach_files is False and ow[guild.default_role].create_public_threads is False
    assert ow[other].send_messages is True
    assert keep.create_public_threads is None  # the category's own overwrite is not changed


def test_a_hand_made_roster_is_never_edited_only_reported(capsys):
    guild = _wave9_guild()
    edits = []

    async def edit(**kw):
        edits.append(kw)
    cat = next(c for c in guild.categories if c.name == games_category()["name"])
    guild.text_channels.append(NS(id=600, name="founding-roster", category_id=cat.id, slowmode_delay=0, edit=edit,
                                  overwrites_for=lambda target: discord.PermissionOverwrite()))
    asyncio.run(public_mode.PublicMode(guild, apply=True).new_channels())
    out = capsys.readouterr().out
    assert edits == [] and not [c for c in guild.calls if c[1] == ROSTER]
    assert "! note    #founding-roster exists but slow mode is 0 s, the layout says 21600 s; " \
           "@everyone may start threads" in out
    good = discord.PermissionOverwrite(create_public_threads=False, create_private_threads=False)
    guild.text_channels[-1].slowmode_delay = 21600
    guild.text_channels[-1].overwrites_for = lambda target: good
    asyncio.run(public_mode.PublicMode(guild, apply=True).new_channels())
    assert "ok      #founding-roster (slow mode, no threads)" in capsys.readouterr().out


def test_setup_server_first_build_makes_the_roster_without_threads():
    guild = _wave9_guild()
    m = setup_server.Makeover(guild, apply=True)
    m.roles = {}
    asyncio.run(m.make_channel(spec(ROSTER), None))
    ((kind, name, kw),) = guild.calls
    assert (kind, name, kw["slowmode_delay"]) == ("text", ROSTER, 21600)
    assert kw["overwrites"][guild.default_role].create_public_threads is False


# ---------------------------------------------------------------- public_mode: the Minecraft answer
def channel_ids(guild, names):
    by = {slug(c.name): c.id for c in guild.channels}
    return {by[slug(n)] for n in names}


def minecraft_answer(guild):
    prompt = next(p for p in guild.ob.prompts if p.title == public_mode.GAMES_PROMPT)
    return next(o for o in prompt.options if o.title == "Minecraft")


def test_the_minecraft_answer_gains_the_smp_channels_and_nothing_else_changes():
    guild = old_server()
    before = {o.title: (set(o.channel_ids), set(o.role_ids))
              for o in next(p for p in guild.ob.prompts if p.title == public_mode.GAMES_PROMPT).options}
    asyncio.run(public_mode.PublicMode(guild, apply=True).game_answer_channels())
    (edit,) = guild.onboarding_edits
    assert set(edit) == {"prompts"}  # default channels and the enabled flag are not sent
    answer = minecraft_answer(guild)
    want = before["Minecraft"][0] | channel_ids(guild, OPENED_BY_MINECRAFT)
    assert set(answer.channel_ids) == want and set(answer.role_ids) == before["Minecraft"][1]
    for o in next(p for p in guild.ob.prompts if p.title == public_mode.GAMES_PROMPT).options:
        if o.title != "Minecraft":
            assert (set(o.channel_ids), set(o.role_ids)) == before[o.title], o.title
    # Running it again changes nothing.
    asyncio.run(public_mode.PublicMode(guild, apply=True).game_answer_channels())
    assert len(guild.onboarding_edits) == 1


def test_the_answer_step_dry_run_lists_channels_made_later_in_the_same_run(capsys):
    guild = old_server()
    guild.text_channels = [c for c in guild.text_channels if slug(c.name) != slug(ROSTER)]
    asyncio.run(public_mode.PublicMode(guild, apply=False).game_answer_channels())
    assert guild.onboarding_edits == []
    out = capsys.readouterr().out
    assert f"Minecraft also opens {', '.join(OPENED_BY_MINECRAFT)} (roles unchanged)" in out
    # Applied while the roster doesn't exist: the others are added, the roster waits for a re-run.
    asyncio.run(public_mode.PublicMode(guild, apply=True).game_answer_channels())
    out = capsys.readouterr().out
    assert f"! skip    Minecraft -> {ROSTER}: no such channel yet" in out
    assert channel_ids(guild, [SMP, FORUM, RULES]) <= set(minecraft_answer(guild).channel_ids)


def test_a_full_run_makes_the_channels_and_opens_them_from_the_minecraft_answer():
    guild = old_server()
    smp = {slug(n) for n in (ROSTER, RULES, FAQ, FORUM)}
    guild.text_channels = [c for c in guild.text_channels if slug(c.name) not in smp]
    guild.forums = [f for f in guild.forums if slug(f.name) not in smp]

    async def steps(pm):
        await pm.roles()
        await pm.new_channels()
        await pm.game_options()
        await pm.game_answer_channels()
    asyncio.run(steps(public_mode.PublicMode(guild, apply=True)))
    made = {name for kind, name, _ in guild.calls if kind in ("text", "forum")}
    assert {ROSTER, RULES, FAQ, FORUM} <= made
    assert channel_ids(guild, OPENED_BY_MINECRAFT) <= set(minecraft_answer(guild).channel_ids)
    assert channel_ids(guild, [FAQ]).isdisjoint(minecraft_answer(guild).channel_ids)


def test_polish_server_first_build_gives_the_minecraft_answer_every_channel():
    import polish_server
    guild = old_server()  # every layout channel exists; the game channels are the old ones
    for emoji, channel, _ in layout.GAMES:
        if not any(slug(c.name) == slug(channel) for c in guild.text_channels):
            guild.add_text(f"{emoji}・{channel}", None)
    for role in {o["role"] for p in layout.ONBOARDING_PROMPTS for o in p["options"] if o.get("role")}:
        if not any(r.name == role for r in guild.roles):
            guild.add_role(role)
    asyncio.run(polish_server.Polish(guild, apply=True).onboarding())
    (edit,) = guild.onboarding_edits
    answer = minecraft_answer(guild)
    assert set(answer.channel_ids) == channel_ids(guild, minecraft_option()["channels"])
    assert edit["enabled"] is True


# ---------------------------------------------------------------- the pinned posts
SEASON_POSTS = {ROSTER: "founding-roster", RULES: "smp-rules", FAQ: "smp-faq"}
LONGEST_MENTION = len("<#12345678901234567890>")  # a channel or role mention once expand() fills it in
DATE_OR_TIME = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*day\b|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}(:\d{2})?\s?(AM|PM|am|pm)\b|\b(Pacific|Eastern|PT|ET|UTC)\b|<t:\d+",
)


def placeholders(text):
    return re.findall(r"\{#([^}]+)\}", text), re.findall(r"\{@([^}]+)\}", text)


def test_each_season_channel_carries_its_post():
    for name, key in SEASON_POSTS.items():
        assert spec(name).get("post") == key, name
    used = [s["post"] for c in layout.CATEGORIES for s in c["channels"] if s.get("post")]
    assert len(used) == len(set(used)) and set(used) <= set(layout.POSTS)


def test_season_posts_fit_discords_embed_limits_once_mentions_are_filled_in():
    for key in SEASON_POSTS.values():
        post = layout.POSTS[key]
        chans, roles = placeholders(post["description"])
        longest = len(re.sub(r"\{[#@][^}]+\}", "x" * LONGEST_MENTION, post["description"]))
        assert longest <= 4096, key  # an embed description
        assert len(post["title"]) <= 256 and len(post["footer"]) <= 2048, key
        assert len(post["title"]) + longest + len(post["footer"]) <= 6000, key  # one message's embeds


def test_season_posts_link_only_layout_channels_and_roles():
    channels = {slug(s["name"]) for c in layout.CATEGORIES for s in c["channels"]}
    roles = {r["name"] for r in layout.ROLES}
    for key in SEASON_POSTS.values():
        chans, used_roles = placeholders(layout.POSTS[key]["description"])
        assert chans, key
        for name in chans:
            assert slug(name) in channels, f"{key}: {{#{name}}}"
        assert set(used_roles) <= roles, key


def test_season_posts_carry_no_dates_or_times():
    # The season date is announced elsewhere; a date in a pinned post goes stale.
    for key in SEASON_POSTS.values():
        post = layout.POSTS[key]
        for part in (post["title"], post["description"], post["footer"]):
            found = DATE_OR_TIME.search(part)
            assert not found, f"{key}: {found.group(0)!r}"


def test_season_posts_send_questions_to_the_forum_or_a_ticket():
    for key in SEASON_POSTS.values():
        text = layout.POSTS[key]["description"]
        assert "{#smp-forum}" in text and "{#help}" in text, key
    rules = layout.POSTS["smp-rules"]["description"]
    assert "https://monkeysmp.lscaturchio.xyz/#rules" in rules
    cited = re.findall(r"\(([^()]*\d[^()]*)\)$", rules, re.M)
    numbers = [int(n) for c in cited for n in re.findall(r"(?<![B\d])(\d+)", c)]
    bans = [int(n) for c in cited for n in re.findall(r"B(\d+)", c)]
    assert numbers and max(numbers) <= 46 and bans and max(bans) <= 19  # the rulebook's 46 rules, B1-B19
    roster = layout.POSTS["founding-roster"]["description"]
    for want in ("Minecraft Java name", "faction", "First come, first listed", "isn't a pass"):
        assert want in roster, want


def _post_guild():
    """Every layout channel, the ones that carry a post empty; send() records the embed and returns a message."""
    from test_server_scripts import FakeGuild, aiter_of
    guild = FakeGuild()
    sent = {}
    for s in setup_server.layout_channels():
        ch = guild._make("text", s["name"])
        ch.mention = f"<#{ch.id}>"
        guild.text_channels.append(ch)
        if not s.get("post"):
            continue

        async def send(embed, ch=ch):
            async def pin(msg=None):
                msg_ref.pinned = True
            msg_ref = NS(author=guild.me, embeds=[embed], pinned=False, pin=pin)
            msg_ref.edit = _no_edit
            sent.setdefault(ch.name, []).append(msg_ref)
            return msg_ref
        ch.send = send
        ch.pins = lambda limit=None, ch=ch: aiter_of([m for m in sent.get(ch.name, []) if m.pinned])
        ch.history = lambda limit=50, ch=ch: aiter_of(sent.get(ch.name, []))
    guild.calls.clear()
    return guild, sent


async def _no_edit(**_):
    raise AssertionError("edited a post that had not changed")


def test_public_mode_posts_and_pins_each_season_post_once(capsys):
    guild, sent = _post_guild()
    asyncio.run(public_mode.PublicMode(guild, apply=True).posts())
    out = capsys.readouterr().out
    for name, key in SEASON_POSTS.items():
        (msg,) = sent[name]
        assert msg.pinned and msg.embeds[0].title == layout.POSTS[key]["title"], name
        assert "{#" not in msg.embeds[0].description and "<#" in msg.embeds[0].description, name
        assert f"post    {key} -> #{name} (pinned)" in out
    # A second run finds the pinned posts and leaves them alone.
    asyncio.run(public_mode.PublicMode(guild, apply=True).posts())
    out = capsys.readouterr().out
    for name, key in SEASON_POSTS.items():
        assert len(sent[name]) == 1, name
        assert f"ok      {key} (posted and pinned)" in out
