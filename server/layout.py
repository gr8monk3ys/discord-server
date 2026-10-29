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
    {"name": "SMP Ping", "mentionable": True},  # opt-in: pinged when 3+ are on Monkey SMP
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
    {"name": "Welcomer", "mentionable": True},  # opt-in (#roles panel): pinged when someone new joins voice
    # Cosmetic thank-you tier: no colour and no permissions, so it can't pass for staff. Given by
    # Ko-fi's Discord bot or Server Subscriptions (or /supporter grant).
    {"name": "Supporter", "hoist": True},
]

# ---------------------------------------------------------------- channels
# Categories are numbered like a catalogue; Discord uppercases them, which
# gives you the site's wall-label look for free.
# "read_only": everyone can read and react, only Keepers (and the bot) post.
# "no_threads": everyone can still post, but @everyone can't start threads (public or private).
# "slowmode" on a text channel: seconds between one member's messages (Discord allows up to 21600).
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
            # Front Desk's weekly field notes (bot/cogs/fieldnotes.py); replies go in each post's thread.
            {"name": "📓・field-notes", "topic": "What Lorenzo built, heard and watched this week.", "read_only": True},
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
        ],
    },
    {
        # Everything Monkey SMP, the owner's Minecraft server, in one place: what to read first, then the
        # chat with the game, the roster and the forum. The channels the SMP repo's launch plan, rulebook and
        # guide send players to (gr8monk3ys/monkey-smp-server: docs/LAUNCH.md, docs/RULES.md,
        # site/src/content/guide/discord.mdx). public_mode.py makes the category after 03 · games with its
        # permissions and moves these channels in from there. Each channel's "post" is pinned by public_mode.py.
        "name": "04 · monkey smp",
        # public_mode.py makes this one if it is missing (after 03 · games, with its permissions); it skips any
        # other missing category, since a copy of the one before it could be the wrong permissions.
        "public_mode_creates": True,
        "channels": [
            {"name": "📜・smp-rules", "topic": "The Monkey SMP rulebook. Staff post here; answers are in "
                                              "#smp-faq, and questions go in #smp-forum or a ticket.", "read_only": True,
             "post": "smp-rules"},
            {"name": "❔・smp-faq", "topic": "Monkey SMP answers: the pass, the modpack, joining. Staff post "
                                           "here; ask anything else in #smp-forum or a ticket.", "read_only": True,
             "post": "smp-faq"},
            {"name": "⌨️・smp-commands", "topic": "Monkey SMP commands, in game and here. Staff post here; "
                                                "ask in #smp-forum or a ticket.", "read_only": True,
             "post": "smp-commands"},
            # Front Desk's bridge to the server: pinned status panel, chat both ways.
            {"name": "⛏️・monkey-smp", "topic": "Monkey SMP, our Minecraft server. Status is pinned; chat here reaches "
                                           "the game, and linked players' chat comes back. Playing needs a "
                                           "pass: monkeysmp.lscaturchio.xyz or a ticket in #help.",
             "slowmode": 2, "was": ["⛏️・realms"]},  # renamed in place by public_mode.py
            # One sign-up message each, so the order of the founding roster is plain to see:
            # 6 h slow mode (Discord's maximum) and no threads.
            {"name": "📝・founding-roster",
             "topic": "Founding roster sign-up: one message with your Minecraft Java name, and your faction's "
                      "name if you have one. First come, first listed. Rules are pinned.",
             "slowmode": 21600, "no_threads": True, "post": "founding-roster"},
            {
                "name": "🚩・smp-forum",
                "type": "forum",
                "topic": "\n".join(
                    [
                        "Monkey SMP factions, recruiting, suggestions, bugs and trading.",
                        "Register a faction (up to four players) with one post: the name, the members' Minecraft "
                        "names, the banner and a charter. One post per faction, tagged Faction.",
                        "Playing solo? Post with the Looking for faction tag and staff will find you one.",
                        "Payment problems, reports and appeals go in a ticket, not here.",
                    ]
                ),
                "tags": [("Faction", "🏰"), ("Recruiting", "📯"), ("Looking for faction", "🔎"),
                         ("Suggestion", "💡"), ("Bug", "🐛"), ("Trading", "⚖️")],
                "layout": "list", "sort": "latest_activity",
            },
        ],
    },
    {
        "name": "05 · off topic",
        "was": ["04 · off topic"],
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
        "name": "06 · voice",
        "was": ["Voice Channels", "05 · voice"],
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
        "name": "07 · squad",
        "was": ["06 · squad"],
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
                "`01`  Be decent. Trash-talk the play, not the person. No racism, sexism or slurs.",
                "`02`  No cheats, no exploits, no selling accounts.",
                "`03`  No spam or self-promo, and no DM advertising to members.",
                "`04`  Clips in {#clips}, memes in {#memes}, music in {#bot-commands}.",
                "`05`  Spoilers go under ||spoiler tags||. No NSFW anywhere.",
                "`06`  Want a squad? Post in {#lfg} and ping {@LFG}, not @everyone.",
                "`07`  Something wrong? Right-click the message → Apps → Report message, or use `/report`.",
                "`08`  Mods have the final word.",
            ]
        ),
        "footer": "FRONT DESK · 01 · UPDATED 2026-10-29",
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
                "`MINECRAFT`  Monkey SMP is the owner's modded server, with its own pass "
                "(monkeysmp.lscaturchio.xyz or a ticket in {#help}). All of it is in the Monkey SMP category: "
                "start with {#smp-rules}, and the commands are in {#smp-commands}.",
                "",
                "**Roles**",
                "{@Keeper}  runs the place",
                "{@Squad}  the regulars",
            ]
        ),
        "footer": "FRONT DESK · 03",
    },
    # Monkey SMP Season I. The source of truth is the SMP repo (gr8monk3ys/monkey-smp-server):
    # docs/RULES.md for the rules, docs/PLAYERS.md and site/index.html for the FAQ, and the
    # "Founding roster" section of docs/LAUNCH.md for the roster. Numbers in brackets are that
    # rulebook's rule numbers. The full rulebook is too long for one message (Discord allows
    # 6000 characters), so this is the condensed version and links the website's.
    # No dates or times here: the season date is announced in #announcements.
    "smp-rules": {
        "title": "Monkey SMP rules",
        "description": "\n".join(
            [
                "The Season I rules, condensed. The numbers match the full rulebook, so \"rule 35\" means "
                "the same thing everywhere. Not sure something is allowed? Ask an admin before you do it (7).",
                "",
                "**Fights**",
                "- You're in combat once a player has hit you. (2)",
                "- No combat logging: don't log out in a fight, or 3 minutes into a chase. (3)",
                "- No respawn or logout trapping, no killing freshly respawned (naked) or AFK players, "
                "and no killing players who are lagging out. (4, 5, 13, 14)",
                "- No rejoining a team fight until 10 minutes after it ends. No restocking mid-fight "
                "from an ender chest, a base or a shulker box. (9, 11)",
                "- Land elytra at least 50 blocks from a fight, and no elytra in combat. No lava PvP, "
                "no Happy Ghast PvP, no leads on dolphins. (12, 15-18)",
                "- No water running: stay within about 30 blocks of shore. No stream sniping without the "
                "streamer's OK. (10, 19)",
                "",
                "**World**",
                "- Spawn is sacred and a no-PvP zone. No TNT traps there. (8, 22, 26, B11)",
                "- Every base needs one feature you can see from the surface. (1)",
                "- The first hour after launch has no PvP, Nether or End; then PvP and only the Nether "
                "open. (23)",
                "- No Netherite armor until staff say so. Netherite tools and weapons are fine. (24)",
                "- The world border is 2500 x 2500. Don't leave it or break it. (25)",
                "- Your first 48 hours come with PvP grace. Attacking a player ends it, and using it to "
                "scout, carry loot or block a fight is a loophole. (26)",
                "- No stealing or killing pets in an offline owner's territory. (20)",
                "- No holes into the void, no breaking the End's exit portal or gateways, nothing on the "
                "Nether roof. (44-46)",
                "",
                "**Strength**",
                "- Craft it only up to +3. Use Strength you're given right away unless you're at +5, and "
                "don't ender-chest it below +5. (27, 28)",
                "",
                "**Factions and land**",
                "- At most 4 players, public, with a banner. Alliances yes, confederations no. (29-32)",
                "- Once you join a faction you stay in it; leaving needs an admin. (33)",
                "- Territory is 2 x 2 chunks per member, marked with your banner: the inside of a base, or "
                "an area walled at least 3 blocks high. Outside it, anything can be griefed. (35, 36)",
                "- No starting KOS in an enemy's base or territory; in your own you may. Retreating into "
                "yours doesn't end a fight. (34, 35, 38)",
                "- Nobody griefs, robs or farms a territory without permission, and pranks must be fully "
                "reversible. Admins roll back griefing; legal raids stay. (37, 39, 42)",
                "- No insiding: never grief, raid or rob your own faction, or use what you had as a member "
                "after you leave. (40)",
                "- No teaming: stay out of fights between other factions, except at events. (41)",
                "",
                "**Trading**",
                "- By hand, items for items. No scams, no renamed fakes, no changing a deal once it's "
                "agreed. (43)",
                "",
                "**Banned**",
                "- Brewing Strength or putting it on arrows or players, enchanted maces, ender pearls, Totems of "
                "Undying, enchanted "
                "golden apples (pearls and gapples are for crafting only), crystal PvP, bed bombing, Fire "
                "Aspect and Flame, TNT minecarts, escape tunnels. (B1-B5, B7-B10, B12)",
                "- Debuffs on others, except Slowness I, Weakness I, Poison I, Instant Harming I and Turtle "
                "Master from a bow or crossbow. (B6)",
                "- Exploits, hacked clients, x-ray (the cushion glitch, F3+A and reloading shaders count), "
                "duping, CIT packs, health indicators, the view-bobbing glitch, Replay Mod or Flashback for an "
                "edge. Other mods need "
                "the owners' OK. No account sharing. (B13-B17, B19, 21)",
                "",
                "**Everyone**",
                "- Respect people: no slurs, hate, harassment, threats or doxxing, in game, in voice or "
                "here. Keep names, skins, builds and chat all-ages.",
                "- No real-money trading.",
                "- Questions go in {#smp-forum}. Payment problems, reports and appeals go in a ticket in "
                "{#help}.",
                "",
                "A warning, then a temporary ban, then a permanent ban. Cheating, hate and doxxing can go "
                "straight to a ban. One appeal per decision, in a ticket; a different admin reviews it.",
                "",
                "The short rules on the website: https://monkeysmp.lscaturchio.xyz/#rules",
            ]
        ),
        "footer": "FRONT DESK · MONKEY SMP · RULES",
    },
    "smp-faq": {
        "title": "Monkey SMP: questions",
        "description": "\n".join(
            [
                "**What is it?**",
                "A modded Minecraft Java server: factions of up to four, PvP outside spawn, and "
                "trading by hand. Up to 30 players online at once.",
                "",
                "**What do I need?**",
                "Minecraft Java Edition on a computer (Windows, macOS or Linux). 16 GB of RAM runs it well. "
                "No Bedrock, console or mobile.",
                "",
                "**What is the pass?**",
                "One price to play, the same for everyone. Supporter tiers are cosmetic only. Prices: "
                "https://monkeysmp.lscaturchio.xyz/#pricing. Until the store opens, a pass is arranged in a "
                "ticket in {#help}, and staff add your Minecraft Java name to the whitelist by hand.",
                "",
                "**How do I get the modpack?**",
                "The server adds blocks, mobs and items, so plain Minecraft can't join. Easiest: the Modrinth "
                "App, search for \"Monkey SMP\" and install it (about 560 MB), then give it 6 GB of memory. "
                "Prism Launcher and an installer for the normal launcher work too: "
                "https://monkeysmp.lscaturchio.xyz/#join",
                "",
                "**How do I join?**",
                "Get a pass, install the pack, then Multiplayer, Add Server, `monkeysmp.lscaturchio.xyz`. "
                "No port needed. When the pack updates, take the update before you play.",
                "",
                "**My computer is slow.**",
                "Shaders, Distant Horizons and the resource packs are optional. Switch off Distant Horizons "
                "first, and keep Sodium and the other speed mods on.",
                "",
                "**How do factions work?**",
                "Up to four players, public, each with a banner. Register with one post in {#smp-forum}, "
                "tagged Faction: the name, the members' Minecraft names, the banner and a charter. Playing "
                "solo? Post with the Looking for faction tag and staff will find you one.",
                "",
                "**What is the founding roster?**",
                "The list of players who are in on launch night, first come, first listed. Sign up in "
                "{#founding-roster}. It's free, and it isn't a pass.",
                "",
                "**Refunds?**",
                "If you can't get the pack running in your first 48 hours, open a ticket and you get your "
                "money back.",
                "",
                "**Can I chat with the game from here?**",
                "Yes, in {#monkey-smp}. `/smp link` connects your Minecraft name to your Discord name.",
                "",
                "**Where do I ask?**",
                "This channel is read-only. Questions, suggestions and bugs go in {#smp-forum}. Payment "
                "problems, reports and appeals go in a ticket in {#help}. The rules are in {#smp-rules}.",
            ]
        ),
        "footer": "FRONT DESK · MONKEY SMP · FAQ",
    },
    # The player commands from the SMP repo's guide (site/src/content/guide/commands.tsx, "players"),
    # condensed, plus Front Desk's /smp commands (bot/cogs/realms.py). The guide has every rank's.
    "smp-commands": {
        "title": "Monkey SMP commands",
        "description": "\n".join(
            [
                "What players type. Menus open in chat; click a line to use it. Type the start of a command "
                "and press Tab to see the rest. Every rank's commands, staff too: "
                "https://monkeysmp.lscaturchio.xyz/guide/commands",
                "",
                "**Menus**",
                "`/trigger wardrobe`  your skins, and how to unlock the rest",
                "`/trigger challenges`  this week's three challenges and how far you've got",
                "`/trigger standings`  this week's faction standings",
                "`/trigger diplomacy`  wars, pacts and offers with every other faction (a leader gets buttons)",
                "`/trigger outposts`  the four outposts, who holds them, and your faction's hours",
                "`/trigger firsts`  the season's Server Firsts and who holds each",
                "`/trigger end_grace`  ends your 48 hours of safety now. There's no getting it back.",
                "",
                "**Factions and land**",
                "`/oparties create`  starts a party: your faction, with you as its leader",
                "`/oparties join <leader>`  joins the party that invited you, named by its leader",
                "`/oclaims claim`  claims the chunk you're standing in (or press M and right-click chunks)",
                "`/opac`  your party and claim settings",
                "Leaders also have `/oparties member invite <player>` (four to a faction) and "
                "`/oparties ally add <leader>` (allies share claims).",
                "",
                "**Arena Glory**",
                "`/trigger arena set 1`  joins while it's open: stand by the entrance, in survival, with an "
                "empty inventory, armor and offhand",
                "`/trigger arena set 2`  leaves, with what you went in with",
                "",
                "**Keys**",
                "`V`  voice chat settings",
                "`M`  the world map, where you claim land",
                "Sneak + right-click with both hands empty picks up a passive animal or a block like an "
                "enchanting table. Nothing that stores items, and nothing in someone else's claim.",
                "",
                "**Here on Discord**",
                "`/smp link <minecraft name>`  connects your Java name, so your game chat shows in "
                "{#monkey-smp} and your playtime counts. It isn't a pass.",
                "`/smp unlink`  forgets your linked name",
                "`/smp status`  is the server up, who's on, and your own link and playtime",
                "`/smp top`  who played the most this week",
                "Only you see the answers, except `/smp top`. Take {@SMP Ping} in {#roles} for a ping when "
                "three or more players are on.",
                "",
                "Questions go in {#smp-forum}. Payment problems, reports and appeals go in a ticket in {#help}.",
            ]
        ),
        "footer": "FRONT DESK · MONKEY SMP · COMMANDS",
    },
    "founding-roster": {
        "title": "Founding roster",
        "description": "\n".join(
            [
                "The founding roster is the list of players who are in on launch night.",
                "",
                "`01`  Post one message: your Minecraft Java name, and your faction's name if you have one.",
                "`02`  One sign-up each. A second message is deleted and the first one counts. Delete yours "
                "and post again, and you go to the back.",
                "`03`  First come, first listed: the order is the time of your message. The first 30 are the "
                "roster; after that there's a waitlist, in order.",
                "`04`  Staff check each name is a real Java name and react with ✅. A wrong name gets a reply, "
                "and you keep your place.",
                "`05`  Can't make it? Tell staff in a ticket in {#help}, and the next on the waitlist moves up.",
                "`06`  Signing up is free and isn't a pass: you still need one to play, at the same price as "
                "everyone. Nobody can pay to be higher on the list.",
                "",
                "Factions register in {#smp-forum}. Questions go there or in a ticket in {#help}, not here.",
            ]
        ),
        "footer": "FRONT DESK · MONKEY SMP · ROSTER",
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
    "📌・rules", "📣・announcements", "💬・general", "🆘・help", "🕹️・gaming", "🎮・lfg", "📸・clips",
    "🔊 Lobby", "➕ New Squad",
    "📓・field-notes",  # one post a week, quiet otherwise
]
# The default list before the core was cut down, word for word: what --revert restores.
ONBOARDING_EVERYTHING = [
    "📌・rules", "📣・announcements", "👋・welcome", "💬・general",
    "🆘・help", "💡・suggestions", "🤣・memes", "📸・clips", "⭐・hall-of-fame", "🔢・counting", "🎲・games", "🕹️・gaming", "🎮・lfg", "🎨・art", "💻・code",
    "👗・fashion", "♟️・chess", "🤖・bot-commands", "📺・creators", "🏆・tournaments", "🎭・roles", "🤝・partners",
    "🔊 Lobby", "➕ New Squad", "🎮 Squad", "💤 AFK",
    "📚・guides", "🖥・setups",
    "⛏️・monkey-smp",
    "📓・field-notes",  # added after the cut, as a default from the start
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
     "description": "Monkey SMP: rules, FAQ, commands, in-game chat, the founding roster and the faction forum.",
     "channels": ["⛏️・monkey-smp", "📝・founding-roster", "🚩・smp-forum", "📜・smp-rules", "❔・smp-faq",
                  "⌨️・smp-commands"]},
    {"title": "Roles and arrivals", "emoji": "🎭",
     "description": "The roles panel, and who just joined.",
     "channels": ["👋・welcome", "🎭・roles"]},
    {"title": "More voice rooms", "emoji": "🔊",
     "description": "The five-seat squad room and the AFK room.",
     "channels": ["🎮 Squad", "💤 AFK"]},
]
# Event-driven channels (a default only while something is live): none. The bot module that
# moved the tournaments and realms channels in and out of the defaults was removed, because a
# default channel only reaches someone who joins during the event. Both are plain opt-in.
FOCUS_EVENT_CHANNELS: dict[str, str] = {}
# Public, but never a default and never an answer. Discord may refuse a Stage as a default
# channel, and one refused channel fails the whole Onboarding edit; a Stage shows itself to
# everyone while an event is live anyway.
FOCUS_UNLISTED = {"🎙・stage": "Discord shows a live Stage to everyone by itself"}
# If Discord says the core does not have five channels @everyone can post in (it may not
# count a forum or a voice channel), focus_mode.py adds these one at a time until it does.
FOCUS_RESERVE = ["🤣・memes", "🤖・bot-commands"]
# More channels a game's "What do you play?" answer opens, after the game's own channel.
# public_mode.py adds the missing ones to the answer and never takes a channel off it. Picking
# Minecraft opens Monkey SMP too; it hands out the @Minecraft role only, never @SMP Ping (that
# ping stays opt-in, on the 🎭・roles panel).
GAME_EXTRA_CHANNELS = {
    "Minecraft": ["⛏️・monkey-smp", "📝・founding-roster", "🚩・smp-forum", "📜・smp-rules", "⌨️・smp-commands"],
}
# Private ("private_to") channels never go in any of these lists.
ONBOARDING_PROMPTS = [
    {
        "title": "What do you play?",
        "multi": True,
        "required": True,
        "options": [
            *(
                {"title": role, "emoji": emoji, "role": role,
                 "channels": [f"{emoji}・{channel}", *GAME_EXTRA_CHANNELS.get(role, [])],
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
# get media back. Off since 2026-10-28 (captcha paused so new members get
# straight in); set True again when Captcha.bot is back.
MEDIA_LOCK = False
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
