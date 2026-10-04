"""The server layout. Edit this file, then run setup_server.py.

Existing channels/roles are matched by their plain name (emoji and
separators ignored), so an old "#general" becomes "💬・general" instead
of getting a duplicate. Anything not listed here is left alone.
"""

# Field Notebook palette (lscaturchio.xyz DESIGN.md)
FOREST = 0x42A979  # forest-ink, night variant: the one pen
MOSS = 0x397F5E
SAND = 0xE2DBD5
MUTED = 0xABB0BA
SLATE = 0x606976

# ---------------------------------------------------------------- games
# One ping role + one channel per game. Members pick their games in
# Onboarding and only see the channels for games they chose, so the sidebar
# stays short. Top friend-group games of 2026, plus the group's own picks.
GAMES = [
    # (emoji, channel name, role name)
    ("🪖", "wardogs", "Wardogs"),
    ("⛏️", "minecraft", "Minecraft"),
    ("🪂", "fortnite", "Fortnite"),
    ("🎯", "valorant", "Valorant"),
    ("💥", "call-of-duty", "Call of Duty"),
    ("🧙", "league-of-legends", "League of Legends"),
    ("💣", "counter-strike", "Counter-Strike 2"),
    ("🔺", "apex-legends", "Apex Legends"),
    ("🚗", "gta-online", "GTA Online"),
    ("🧱", "roblox", "Roblox"),
]

# Any role, category or channel can list old names under "was": the existing
# one is renamed and restyled in place, so members, messages and permissions
# carry over instead of ending up with a duplicate.

# ---------------------------------------------------------------- roles
# Top to bottom. Only Keeper and Squad get a name colour (the One Pen Rule:
# colour marks what matters, it isn't wallpaper). Game roles are colourless
# ping roles.
ROLES = [
    {"name": "Keeper", "color": FOREST, "hoist": True, "admin": True, "was": ["Admin"]},
    {"name": "Moderator", "keep": True},
    # Hosted bots: their roles must sit above the roles they hand out
    # (Captcha.bot gives @Verified).
    {"name": "Captcha.bot", "keep": True},
    {"name": "MEE6", "keep": True},
    {"name": "GiveawayBot", "keep": True},
    {"name": "Squad", "color": MOSS, "hoist": True, "was": ["Member"]},
    {"name": "Verified"},  # given by Captcha.bot after the captcha; unlocks media
    {"name": "Guest", "color": MUTED},
    {"name": "Bots", "color": SLATE, "hoist": True, "was": ["Bot"]},
    {"name": "LFG", "mentionable": True},
    # Public-server roles, handed out by Front Desk.
    {"name": "Clip of the Week"},  # given by Front Desk to the weekly clip winner
    {"name": "Recruiter"},  # 3+ people you invited stayed
    {"name": "Bumper", "mentionable": True},  # opt-in: /bumpping on
    *({"name": role, "mentionable": True} for _, _, role in GAMES),
]

# ---------------------------------------------------------------- channels
# Categories are numbered like a catalogue; Discord uppercases them, which
# gives you the site's wall-label look for free.
# "read_only": everyone can read and react, only Keepers (and the bot) post.
CATEGORIES = [
    {
        "name": "01 · front desk",
        "was": ["Welcome"],
        "channels": [
            {"name": "📌・rules", "topic": "Read these once. They're short.", "read_only": True, "post": "rules"},
            {"name": "📣・announcements", "topic": "Game nights, updates, server news.", "read_only": True},
            {"name": "👋・welcome", "topic": "New faces land here.", "read_only": True, "system": True, "post": "welcome"},
        ],
    },
    {
        "name": "02 · the lobby",
        "channels": [
            {"name": "💬・general", "topic": "Anything goes. Mostly."},
            {"name": "🤣・memes", "topic": "Post it here, not in #general."},
            {"name": "📸・clips", "topic": "Highlights, fails, and receipts.", "slowmode": 10},
            {"name": "⭐・hall-of-fame", "topic": "3 ⭐ on any message and it lands here.", "read_only": True},
        ],
    },
    {
        "name": "03 · games",
        "channels": [
            {"name": "🕹️・gaming", "topic": "General gaming chat.", "was": ["gaming"]},
            {
                # A forum: one post per session, tagged by game, so squads
                # don't get buried in chat.
                "name": "🎮・lfg",
                "type": "forum",
                "topic": "\n".join(
                    [
                        "One post per session. Title it with the game and a time, like \"Wardogs, 9pm\".",
                        "Tag the game (and Ranked or Casual). Ping @LFG if you need people right now.",
                        "Close or delete the post once the squad is full.",
                    ]
                ),
                "tags": [*((role, emoji) for emoji, _, role in GAMES), ("Ranked", "🏆"), ("Casual", "🛋️")],
            },
            *(
                {"name": f"{emoji}・{channel}", "topic": f"{role} talk, squads, and strats. Ping @{role}."}
                for emoji, channel, role in GAMES
            ),
        ],
    },
    {
        "name": "04 · off topic",
        "channels": [
            {"name": "🎨・art", "topic": "Things you made."},
            {"name": "💻・code", "topic": "Projects, snippets, and bugs."},
            {"name": "👗・fashion", "topic": "Fits and finds."},
            {"name": "♟️・chess", "topic": "Games, puzzles, and challenges."},
            {"name": "🤖・bot-commands", "topic": "Music (/play), bump reminders, and other bot spam."},
        ],
    },
    {
        "name": "05 · voice",
        "was": ["Voice Channels"],
        "channels": [
            {"name": "🔊 Lobby", "type": "voice", "was": ["General"]},
            {"name": "➕ New Squad", "type": "voice"},  # join to get your own channel
            {"name": "🎮 Squad", "type": "voice", "user_limit": 5, "was": ["🎮 Squad I", "General 2"]},
            {"name": "💤 AFK", "type": "voice", "afk": True},
        ],
    },
    {
        # Staff-only: channels keep their existing permissions.
        "name": "00 · staff",
        "was": ["Admin"],
        "channels": [
            {"name": "🛡️・mod", "was": ["mod"]},
            {"name": "🔒・admin", "was": ["admin"]},
            # Front Desk logs joins, leaves, bans, AutoMod hits and /report here.
            {"name": "📋・mod-log", "private_to": ["Keeper", "Moderator"]},
        ],
    },
    {
        # The friend group's own space now that the server is public.
        "name": "06 · squad",
        "private_to": ["Squad", "Keeper"],
        "channels": [
            {"name": "🔒・squad-chat", "topic": "Just us."},
            {"name": "🔒 Squad Only", "type": "voice"},
        ],
    },
]

AFK_TIMEOUT_SECONDS = 900  # 1, 5, 15, 30 or 60 minutes are the allowed values

# Optional: path to a square PNG/JPG to use as the server icon (relative to
# this folder). assets/make_icon.py draws the Field Notebook one.
ICON_PATH = "../assets/server-icon.png"

# ---------------------------------------------------------------- posts
# Embeds the bot posts once (skipped if the bot already posted there).
# {#general} becomes a clickable channel link and {@Squad} a role mention.
POSTS = {
    "rules": {
        "title": "House rules",
        "description": "\n".join(
            [
                "`01`  18+ only. If you're under 18, this isn't the server for you.",
                "`02`  Be decent. Trash-talk the play, not the person. No racism, sexism or slurs.",
                "`03`  No cheats, no exploits, no selling accounts.",
                "`04`  No spam or self-promo, and no DM advertising to members.",
                "`05`  Clips in {#clips}, memes in {#memes}, music in {#bot-commands}.",
                "`06`  Spoilers go under ||spoiler tags||. No NSFW anywhere.",
                "`07`  Want a squad? Post in {#lfg} and ping {@LFG}, not @everyone.",
                "`08`  Something wrong? Right-click the message → Apps → Report message, or use `/report`.",
                "`09`  Mods have the final word.",
            ]
        ),
        "footer": "FRONT DESK · 01 · UPDATED 2026-10-03",
    },
    "welcome": {
        "title": "Welcome in",
        "description": "\n".join(
            [
                "Chill 18+ gamers. Read {#rules}, then say hi in {#general}.",
                "",
                "**Roles**",
                "{@Keeper}  runs the place",
                "{@Squad}  the regulars",
                "`GAME ROLES`  pick yours under **Channels & Roles** at the top of the channel list",
            ]
        ),
        "footer": "FRONT DESK · 03",
    },
}


# ---------------------------------------------------------------- polish
# Used by polish_server.py: Community, Onboarding, AutoMod, and trimming.

# Empty channels to remove (skipped if anyone has posted in them).
# Small servers feel emptier with more channels: aim for 5-10 text, 2-3 voice.
# Forums are never trimmed, so the old text #🎮・lfg can go once the forum exists.
TRIM = ["🔗・links", "🎮 Squad II", "🎧 Chill", "🎮・lfg"]

COMMUNITY = {
    "rules_channel": "📌・rules",
    "updates_channel": "🛡️・mod",  # where Discord sends admin-only notices
}

# Onboarding: what new members see before they land. Channels listed here are
# the defaults everyone gets; Discord needs at least 7, 5 of them postable.
ONBOARDING_DEFAULT_CHANNELS = [
    "📌・rules", "📣・announcements", "👋・welcome", "💬・general",
    "🤣・memes", "📸・clips", "⭐・hall-of-fame", "🕹️・gaming", "🎮・lfg", "🎨・art", "💻・code",
    "👗・fashion", "♟️・chess", "🤖・bot-commands", "🔊 Lobby", "➕ New Squad", "🎮 Squad", "💤 AFK",
]
# Anything public that's neither a default channel nor an Onboarding option is
# hidden from members who went through Onboarding, so keep this list complete.
ONBOARDING_PROMPTS = [
    {
        "title": "What do you play?",
        "multi": True,
        "required": True,
        "options": [
            *(
                {"title": role, "emoji": emoji, "role": role, "channel": f"{emoji}・{channel}",
                 "description": f"Get @{role} pings and the #{channel} channel."}
                for emoji, channel, role in GAMES
            ),
            {"title": "Just here to hang", "emoji": "🛋️", "role": "Guest",
             "description": "No game pings. You can pick some later."},
        ],
    },
    {
        "title": "Want squad pings?",
        "multi": False,
        "required": False,
        "options": [
            {"title": "Ping me for squads", "emoji": "🎮", "role": "LFG",
             "description": "Get @LFG pings when someone needs people for a game."},
        ],
    },
]

# Welcome Screen: the card new members see, with up to 5 channels.
WELCOME_SCREEN = {
    "description": "Chill 18+ gamers. No tryhards, no toxicity. Pick your games and find a squad tonight.",
    "channels": [
        ("📌・rules", "📌", "Short and worth reading"),
        ("💬・general", "💬", "Say hi"),
        ("🎮・lfg", "🎮", "Find a squad for tonight"),
        ("📸・clips", "📸", "Post your best plays"),
        ("🕹️・gaming", "🕹️", "Talk games"),
    ],
}

# The server is public now: public_mode.py keeps one permanent invite for
# listings, so polish_server.py must not revoke it.
REVOKE_PERMANENT_INVITES = False

# ---------------------------------------------------------------- public
# Used by public_mode.py. The server description shows in invites and
# Discovery; keep it under 120 characters.
PUBLIC = {
    "description": "Chill 18+ gaming community. No tryhards, no toxicity: squad up for Valorant, CoD, Fortnite, Minecraft and more.",
    "invite_channel": "👋・welcome",
}

# AutoMod: alerts go to this channel; these roles are never filtered.
AUTOMOD_ALERTS = "🛡️・mod"
AUTOMOD_EXEMPT_ROLES = ["Keeper", "Moderator"]
MENTION_LIMIT = 6

# New-member media lock: @everyone can't post images, files or link embeds
# until they pass the captcha (Captcha.bot gives @Verified). These roles
# get media back.
MEDIA_ROLES = ["Keeper", "Moderator", "Squad", "Verified"]

# Every bot member (except Front Desk itself) gets this role, so bots are
# grouped together in the member list.
BOT_ROLE = "Bots"
