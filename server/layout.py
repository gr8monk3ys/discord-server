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
# stays short. The list is what the server actually plays (the bot's weekly
# owner digest says when it has drifted), in the order it is shown.
GAMES = [
    # (emoji, channel name, role name)
    ("🌧️", "risk-of-rain-2", "Risk of Rain 2"),
    ("🪚", "gears-of-war", "Gears of War"),
    ("🦍", "overwatch", "Overwatch"),
    ("🦸", "marvel-rivals", "Marvel Rivals"),
    ("⛏️", "minecraft", "Minecraft"),
    ("🃏", "tabletop-simulator", "Tabletop Simulator"),
    ("🪖", "wardogs", "Wardogs"),
    ("🔺", "apex-legends", "Apex Legends"),
]

# Games the server used to list. Nothing of theirs is deleted: public_mode.py takes them
# out of the "What do you play?" question, the bot stops offering their roles, and their
# channels move into the staff-only GAME_ARCHIVE category. The roles stay, so a member who
# has one keeps it. To retire a game, move its line from GAMES to here; to bring one back,
# move its line from here to GAMES. Then run public_mode.py (dry run first).
RETIRED_GAMES = [
    ("🪂", "fortnite", "Fortnite"),
    ("🎯", "valorant", "Valorant"),
    ("💥", "call-of-duty", "Call of Duty"),
    ("🧙", "league-of-legends", "League of Legends"),
    ("💣", "counter-strike", "Counter-Strike 2"),
    ("🚗", "gta-online", "GTA Online"),
    ("🧱", "roblox", "Roblox"),
]

# Where retired game channels are kept: hidden from @everyone, visible to these roles. It is
# deliberately not in CATEGORIES, so nothing else creates, fills or rearranges it.
GAME_ARCHIVE = {"name": "99 · archive", "private_to": ["Keeper", "Moderator"]}

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
    {"name": "Season Champ", "hoist": True},  # top 3 of last month's season (Front Desk)
    {"name": "Birthday", "hoist": True},  # 24 h on your birthday (Front Desk)
    {"name": "Tournament Champ"},  # won the latest tournament (Front Desk)
    {"name": "Event Champ", "hoist": True},  # winner of the latest seasonal event (Front Desk)
    {"name": "Counting Champ"},  # top counter of the best counting run (Front Desk)
    {"name": "Hype", "hoist": True},  # bought in the shop, lasts 24 h (Front Desk)
    {"name": "Clip of the Week"},  # given by Front Desk to the weekly clip winner
    {"name": "Recruiter"},  # 3+ people you invited stayed
    {"name": "Bumper", "mentionable": True},  # opt-in: /bumpping on
    {"name": "Game Night", "mentionable": True},  # opt-in: pinged when a game night starts
    # Level rewards (Front Desk, from XP).
    {"name": "Mythic"}, {"name": "Legend"}, {"name": "Elite"}, {"name": "Veteran"}, {"name": "Regular"},
    # Self-assign: platform, region, play time (#roles panel and Onboarding).
    *({"name": n} for n in ("PC", "PlayStation", "Xbox", "Switch", "Mobile")),
    *({"name": n} for n in ("NA", "EU", "LATAM", "Asia", "OCE")),
    *({"name": n} for n in ("Early Bird", "Night Owl", "Weekend Warrior")),
    *({"name": role, "mentionable": True} for _, _, role in GAMES),
    # Retired game roles stay where they are (members keep them); they are just not offered.
    *({"name": role, "mentionable": True} for _, _, role in RETIRED_GAMES),
    {"name": "Welcome Crew", "mentionable": True},  # opt-in via /crew: pinged when someone new arrives
    # Cosmetic thank-you tier: no colour and no permissions, so it can't pass for staff. Given by
    # Ko-fi's Discord bot or Server Subscriptions (or /supporter grant).
    {"name": "Supporter", "hoist": True},
]

# ---------------------------------------------------------------- channels
# Categories are numbered like a catalogue; Discord uppercases them, which
# gives you the site's wall-label look for free.
# "read_only": everyone can read and react, only Keepers (and the bot) post.
# Forums ("type": "forum") may also set "layout" (list | gallery), "sort" (latest_activity |
# creation_date), "reaction" (the default reaction emoji), "post_slowmode" (seconds between
# new posts per member) and "slowmode" (seconds between messages inside a post).
# "type": "stage" is a Stage channel (needs Community).
CATEGORIES = [
    {
        "name": "01 · front desk",
        "was": ["Welcome"],
        "channels": [
            {"name": "📌・rules", "topic": "Read these once. They're short.", "read_only": True, "post": "rules"},
            {"name": "📣・announcements", "topic": "Game nights, updates, server news.", "read_only": True},
            {"name": "👋・welcome", "topic": "New faces land here.", "read_only": True, "system": True, "post": "welcome"},
            {"name": "🆘・help", "topic": "Need a mod? Press the button to open a private ticket.", "read_only": True},
            {"name": "🎭・roles", "topic": "Pick your platform, region and pings with the buttons.", "read_only": True},
            {
                "name": "💡・suggestions",
                "type": "forum",
                "topic": "One idea per post. Vote with 👍 / 👎. Mods tag it when it's decided.",
                "tags": [("Idea", "💡"), ("Accepted", "✅"), ("Denied", "❌"), ("Done", "🎉")],
            },
        ],
    },
    {
        "name": "02 · the lobby",
        "channels": [
            {"name": "💬・general", "topic": "Anything goes. Mostly."},
            {"name": "🤣・memes", "topic": "Post it here, not in #general."},
            {"name": "📸・clips", "topic": "Highlights, fails, and receipts.", "slowmode": 10},
            {"name": "⭐・hall-of-fame", "topic": "3 ⭐ on any message and it lands here.", "read_only": True},
            {"name": "🔢・counting", "topic": "Count up together. Same person can't go twice; a wrong number resets."},
            {"name": "🎲・games", "topic": "Daily word game, weekly challenges, /slots, /trivia and friends."},
            {"name": "💜・supporters", "topic": "A quiet corner for supporters. Thank you for keeping the lights on.",
             "private_to": ["Supporter", "Keeper", "Moderator"]},
        ],
    },
    {
        "name": "03 · games",
        "channels": [
            {"name": "🕹️・gaming", "topic": "General gaming chat.", "was": ["gaming"]},
            {"name": "🏆・tournaments", "topic": "Sign up, see brackets, report results. Join with the buttons.",
             "read_only": True},
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
            {
                # A forum stays readable when things are quiet: a guide from last month is
                # still the first thing you see, where a chat channel would just look dead.
                "name": "📚・guides",
                "type": "forum",
                "topic": "\n".join(
                    [
                        "Tips, builds, settings and how-tos that are worth finding again next month.",
                        "One topic per post. Put the game and the point in the title, like "
                        "\"Overwatch: crosshair settings that work\".",
                        "Tag the game. Write it so someone new can follow it: steps, numbers, a screenshot if it helps.",
                        "Your own words, please: no pasted articles, no link-only posts.",
                        "A real guide that stays up for a day earns a few coins (a couple of posts a week at most).",
                    ]
                ),
                "tags": [*((role, emoji) for emoji, _, role in GAMES),
                         ("Beginner", "🌱"), ("Advanced", "🧠"), ("Settings", "⚙️"), ("Question", "❓")],
                "layout": "list", "sort": "latest_activity", "reaction": "👍",
                "post_slowmode": 600, "slowmode": 5,
            },
            # Front Desk's bridge to our Minecraft server: pinned status panel, chat both ways.
            {"name": "⛏️・realms", "topic": "The Realms, our Minecraft server. Status is pinned; chat here reaches "
                                           "the game, and linked players' chat comes back. /realms link gets "
                                           "you on the whitelist.",
             "slowmode": 2},
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
            {"name": "📺・creators", "topic": "Members' streams and uploads. Link yours with /creator.", "read_only": True},
            {"name": "🤝・partners", "topic": "Servers we partner with. Apply with /partner apply.", "read_only": True},
            {
                # Image-first: the gallery layout shows each post's first picture.
                "name": "🖥・setups",
                "type": "forum",
                "topic": "\n".join(
                    [
                        "Battlestations, PC builds, console corners, desks and in-game bases. Lead with a picture.",
                        "One setup per post. Say what we're looking at: parts, the game, how long the base took.",
                        "Tag the type. Your own photos and screenshots only, and nothing that shows an address "
                        "or a face you'd rather keep private.",
                        "A setup with a picture that stays up for a day earns a few coins "
                        "(a couple of posts a week at most).",
                    ]
                ),
                "tags": [("Battlestation", "🖥️"), ("PC Build", "🔧"), ("Console", "🎮"), ("Desk", "🪑"),
                         ("In-game Base", "🏰"), ("Work in Progress", "🚧")],
                "layout": "gallery", "sort": "creation_date", "reaction": "🔥",
                "post_slowmode": 600, "slowmode": 5,
            },
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
            # Watch parties and the monthly Town Hall (Front Desk schedules both as Stage events).
            {"name": "🎙・stage", "type": "stage"},
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
            # Ban appeals from the website arrive here through a webhook; Front Desk turns each
            # into a review card (bot/cogs/appeals.py).
            {"name": "🔒・appeals", "private_to": ["Keeper", "Moderator"],
             "topic": "Ban appeals from the website. Front Desk posts a card for each one."},
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
                "This is a small co-op and PvE server. People are mostly in voice in the evenings, "
                "US Pacific time. Read {#rules}, then say hi in {#general}.",
                "",
                "**Start here**",
                "`VOICE`  If someone is in the Lobby, join them. That is how most games here start.",
                "`GAMES`  Pick yours under **Channels & Roles** at the top of the channel list.",
                "`HOUSE`  Front Desk sorts you into Fern, Kestrel or Otter. `/house me` shows yours.",
                "`MINECRAFT`  The Realms is the owner's modded server. After two weeks here, "
                "`/realms link` puts you on its whitelist.",
                "",
                "**Roles**",
                "{@Keeper}  runs the place",
                "{@Squad}  the regulars",
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

# Onboarding: what new members see before they land. Channels listed here are the core: the
# defaults every newcomer gets. Discord needs at least 7, and 5 of them must let @everyone
# send messages. Everything else public is opt-in (FOCUS_GROUPS below), so a newcomer lands
# on a short list of rooms with something in them. focus_mode.py applies this and prints
# the whole plan first; `focus_mode.py --revert` puts ONBOARDING_EVERYTHING back.
# #gaming is core because game nights, watch parties and the weekly free games are posted
# there, and it is a plain text channel that certainly counts towards Discord's five.
ONBOARDING_DEFAULT_CHANNELS = [
    "📌・rules", "📣・announcements", "💬・general", "🕹️・gaming", "🎮・lfg", "📸・clips",
    "🔊 Lobby", "➕ New Squad",
]
# The default list before the core was cut down, word for word: what --revert restores.
ONBOARDING_EVERYTHING = [
    "📌・rules", "📣・announcements", "👋・welcome", "💬・general",
    "🆘・help", "💡・suggestions", "🤣・memes", "📸・clips", "⭐・hall-of-fame", "🔢・counting", "🎲・games", "🕹️・gaming", "🎮・lfg", "🎨・art", "💻・code",
    "👗・fashion", "♟️・chess", "🤖・bot-commands", "📺・creators", "🏆・tournaments", "🎭・roles", "🤝・partners",
    "🔊 Lobby", "➕ New Squad", "🎮 Squad", "💤 AFK",
    "📚・guides", "🖥・setups",
    "⛏️・realms",
]
# Opt-in groups: one question under Channels & Roles (asked after joining, so it does not
# count against Discord's cap on pre-join questions). Every public channel that is not a
# game channel is in the core, in exactly one group, or in FOCUS_UNLISTED; a test checks it.
# Nobody loses access: these channels stay visible to @everyone, and members who are already
# here keep the channel list they have. Only what a newcomer starts with changes.
FOCUS_PROMPT = "What else do you want to see?"
FOCUS_GROUPS = [
    {"title": "Events and tournaments", "emoji": "🏆",
     "description": "Brackets, sign-ups and results.",
     "channels": ["🏆・tournaments"]},
    {"title": "Coins, levels and games", "emoji": "🎲",
     "description": "Daily word game, counting, coin games and bot commands.",
     "channels": ["🎲・games", "🔢・counting", "🤖・bot-commands"]},
    {"title": "Creators and highlights", "emoji": "📺",
     "description": "Members' streams and uploads, starred messages, partner servers.",
     "channels": ["📺・creators", "⭐・hall-of-fame", "🤝・partners"]},
    {"title": "Forums", "emoji": "📚",
     "description": "Guides, setups and suggestions for the server.",
     "channels": ["📚・guides", "🖥・setups", "💡・suggestions"]},
    {"title": "Off topic", "emoji": "🤣",
     "description": "Memes, art, code, fashion and chess.",
     "channels": ["🤣・memes", "🎨・art", "💻・code", "👗・fashion", "♟️・chess"]},
    {"title": "Minecraft", "emoji": "⛏️",
     "description": "The Realms: server status and chat with whoever is in game.",
     "channels": ["⛏️・realms"]},
    {"title": "Help desk, roles and arrivals", "emoji": "🆘",
     "description": "Open a ticket with the mods, the roles panel, who just joined.",
     "channels": ["👋・welcome", "🆘・help", "🎭・roles"]},
    {"title": "More voice rooms", "emoji": "🔊",
     "description": "The five-seat squad room and the AFK room.",
     "channels": ["🎮 Squad", "💤 AFK"]},
]
# Event-driven: opt-in most of the time, and a default channel for newcomers while the thing
# is live. Front Desk (bot/cogs/focus.py) adds the channel to the defaults when it starts and
# takes it out again afterwards. These are the only channels that module may touch.
FOCUS_EVENT_CHANNELS = {
    "🏆・tournaments": "a tournament is open or running",
    "⛏️・realms": "the Minecraft server is online",
}
# Public, but never a default and never an answer. Discord may refuse a Stage as a default
# channel, and one refused channel fails the whole Onboarding edit; a Stage shows itself to
# everyone while an event is live anyway.
FOCUS_UNLISTED = {"🎙・stage": "Discord shows a live Stage to everyone by itself"}
# If Discord says the core does not have five channels @everyone can post in (it may not
# count a forum or a voice channel), focus_mode.py adds these one at a time until it does.
FOCUS_RESERVE = ["🤣・memes", "🤖・bot-commands"]
# Private ("private_to") channels never go in any of these lists.
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
    "description": "A small co-op and PvE server. Pick your games, then join the Lobby voice room when someone "
                   "is in it. Evenings, Pacific time.",
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
    "description": "A small, friendly co-op and PvE server. Risk of Rain 2, Gears of War, Overwatch, Minecraft. "
                   "Evenings, Pacific time.",
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

# ---------------------------------------------------------------- houses
# Three teams, named like specimens pressed into the notebook. Front Desk sorts every member
# into one and hands out its role; the colours are quiet inks so the One Pen stays the loudest.
HOUSES = [
    # (key stored in the database, name, emoji, colour)
    ("fern", "Fern", "🌿", 0x7FA36B),
    ("kestrel", "Kestrel", "🪶", 0xC48A4A),
    ("otter", "Otter", "🦦", 0x6F93A8),
]
HOUSE_ROLES = {key: f"House {name}" for key, name, _, _ in HOUSES}
# Appended, so they are created at the bottom of the stack (below Front Desk).
ROLES += [{"name": HOUSE_ROLES[key], "color": color} for key, _, _, color in HOUSES]
