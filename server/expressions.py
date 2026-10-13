"""Upload the server's custom emoji and soundboard sounds.

    python expressions.py           # dry run: shows what would be uploaded
    python expressions.py --apply   # upload

Files come from assets/expressions/emoji/*.png and assets/expressions/sounds/*.mp3
(run make_emoji.py and make_sounds.py there first). Safe to re-run: anything
whose name already exists on the server is skipped, nothing is edited or
deleted. When the server is out of slots, the leftovers are listed instead.

    python expressions.py --replace           # dry run: which emoji have new art
    python expressions.py --replace --apply   # swap them, one at a time

--replace is a separate mode for redrawn emoji and touches nothing else (no
sounds, no missing emoji). Discord cannot change an emoji's picture, so each
one whose local art differs from the server's is uploaded again under the same
name and the old one is deleted. That gives it a new ID: read the dry run.

The bot needs the Create Expressions permission. --replace also needs Manage
Expressions, unless every emoji it deletes was uploaded by this bot.
"""

import argparse
import io
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import discord
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent / "assets" / "expressions"
EMOJI_DIR = ROOT / "emoji"
SOUND_DIR = ROOT / "sounds"

NAME_RE = re.compile(r"^[a-z0-9_]{2,32}$")
EMOJI_PX = 128
MAX_EMOJI_BYTES = 256 * 1024
MAX_SOUND_BYTES = 512 * 1024
MAX_SOUND_SECONDS = 5.2

# The emoji shown next to each sound on the soundboard.
SOUND_EMOJI = {
    "gg_chime": "🏆",
    "oof": "💀",
    "airhorn_ish": "📯",
    "drumroll": "🥁",
    "rimshot": "😂",
    "level_up": "🆙",
    "sad_trombone_ish": "🎺",
    "ding": "🔔",
}

# Soundboard slots per boost tier. discord.py has no property for this, so it
# mirrors Discord's published numbers (8 free, 24 / 36 / 48 with boosts).
SOUNDBOARD_SLOTS = {0: 8, 1: 24, 2: 36, 3: 48}


def soundboard_limit(guild) -> int:
    if "MORE_SOUNDBOARD" in guild.features:
        return SOUNDBOARD_SLOTS[3]
    return SOUNDBOARD_SLOTS.get(guild.premium_tier, SOUNDBOARD_SLOTS[0])


# ---------------------------------------------------------------- local files
def local_files(folder: Path, suffix: str) -> list[Path]:
    return sorted(folder.glob(f"*{suffix}")) if folder.exists() else []


def mp3_duration(data: bytes) -> float:
    """Seconds of audio in an MPEG-1/2 Layer III stream, by walking its frame headers."""
    i = 0
    if data[:3] == b"ID3":  # skip the ID3v2 tag; its size is a 28-bit syncsafe int
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        i = 10 + size
    bitrates = {
        1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],  # MPEG-1
        2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],  # MPEG-2/2.5
    }
    rates = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
    seconds = 0.0
    while i + 4 <= len(data):
        b1, b2 = data[i + 1], data[i + 2]
        if data[i] != 0xFF or (b1 & 0xE0) != 0xE0:
            i += 1
            continue
        version = (b1 >> 3) & 3  # 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5
        layer = (b1 >> 1) & 3
        br_idx, sr_idx = (b2 >> 4) & 0xF, (b2 >> 2) & 3
        if version == 1 or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
            i += 1
            continue
        sr = rates[version][sr_idx]
        bitrate = bitrates[1 if version == 3 else 2][br_idx] * 1000
        per_frame = 1152 if version == 3 else 576
        length = (144 if version == 3 else 72) * bitrate // sr + ((b2 >> 1) & 1)
        seconds += per_frame / sr
        i += length
    return seconds


def check_emoji(path: Path) -> list[str]:
    """Problems that would make Discord reject this emoji (empty list = fine)."""
    from PIL import Image  # only needed for checks, not for uploading

    problems = []
    if not NAME_RE.match(path.stem):
        problems.append(f"name {path.stem!r} must be 2-32 chars of a-z, 0-9, _")
    if path.stat().st_size >= MAX_EMOJI_BYTES:
        problems.append(f"{path.stat().st_size} bytes, limit is {MAX_EMOJI_BYTES}")
    with Image.open(path) as im:
        if im.format != "PNG" or im.size != (EMOJI_PX, EMOJI_PX):
            problems.append(f"{im.format} {im.size}, want PNG {EMOJI_PX}x{EMOJI_PX}")
    return problems


def check_sound(path: Path) -> list[str]:
    data = path.read_bytes()
    problems = []
    if not NAME_RE.match(path.stem):
        problems.append(f"name {path.stem!r} must be 2-32 chars of a-z, 0-9, _")
    if len(data) > MAX_SOUND_BYTES:
        problems.append(f"{len(data)} bytes, limit is {MAX_SOUND_BYTES}")
    if not (data.startswith(b"ID3") or data.startswith(b"\xff\xfb")):
        problems.append("not an MP3 discord.py recognises (needs an ID3 tag or an MPEG-1 frame first)")
    seconds = mp3_duration(data)
    if not 0 < seconds <= MAX_SOUND_SECONDS:
        problems.append(f"{seconds:.2f}s long, limit is {MAX_SOUND_SECONDS}s")
    return problems


# ---------------------------------------------------------------- planning
@dataclass
class Plan:
    create: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)  # already on the server
    no_room: list[str] = field(default_factory=list)  # wanted, but over the slot limit


def plan(wanted: list[str], existing: set[str], used: int, limit: int) -> Plan:
    """Decide which of `wanted` to upload given what's there and the free slots."""
    p = Plan()
    free = max(0, limit - used)
    for name in wanted:
        if name in existing:
            p.skip.append(name)
        elif len(p.create) < free:
            p.create.append(name)
        else:
            p.no_room.append(name)
    return p


def missing_permissions(perms: discord.Permissions) -> list[str]:
    return [] if perms.administrator or perms.create_expressions else ["Create Expressions"]


def show(kind: str, p: Plan, used: int, limit: int):
    print(f"\n{kind}: {used}/{limit} slots used")
    for name in p.create:
        print(f"  upload  {name}")
    for name in p.skip:
        print(f"  ok      {name} (already there)")
    if p.no_room:
        print(f"  no room for {len(p.no_room)}: {', '.join(p.no_room)}")
        print("          (free some slots or boost the server, then re-run)")


# ---------------------------------------------------------------- replacing redrawn emoji
# How far apart two pictures may be and still count as the same one: the largest per-channel
# mean difference, on a 0-255 scale. Discord may hand an emoji back re-encoded; a lossless
# re-encode measures 0, a palette or lossy one up to about 3.6, and a redrawn emoji 75 or more.
# A small touch-up can land under the line: name it with --force.
SAME_IMAGE_TOLERANCE = 4.0


def _flattened(data: bytes):
    """The picture as premultiplied colour plus coverage, so invisible pixels can't differ."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGBA")
    if im.size != (EMOJI_PX, EMOJI_PX):
        im = im.resize((EMOJI_PX, EMOJI_PX), Image.LANCZOS)
    on_black = Image.alpha_composite(Image.new("RGBA", im.size, (0, 0, 0, 255)), im).convert("RGB")
    return Image.merge("RGBA", (*on_black.split(), im.getchannel("A")))


def image_distance(a: bytes, b: bytes) -> float:
    """0 for the same picture, up to 255; infinity when either is not an image."""
    if a == b:
        return 0.0
    from PIL import ImageChops, ImageStat

    try:
        return max(ImageStat.Stat(ImageChops.difference(_flattened(a), _flattened(b))).mean)
    except OSError:  # includes PIL.UnidentifiedImageError
        return float("inf")


def same_image(a: bytes, b: bytes) -> bool:
    return image_distance(a, b) <= SAME_IMAGE_TOLERANCE


@dataclass
class Replacement:
    name: str
    stale: list  # the server's emoji of this name that show something else
    upload: bool  # False when the new art is already up there (an earlier run stopped half way)
    delete_first: bool  # False: upload the new one, then delete; True: the other way round


@dataclass
class ReplacePlan:
    replace: list[Replacement] = field(default_factory=list)
    same: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)  # not on the server: the plain run adds these


def plan_replace(local: dict[str, bytes], remote: dict[str, list[tuple]], free: int,
                 delete_first: bool = False, force: frozenset[str] = frozenset()) -> ReplacePlan:
    """Decide which emoji to swap, and in which order.

    `local` maps name -> file bytes. `remote` maps name -> [(emoji, its current image bytes)];
    a name can be there twice when an earlier run stopped between its upload and its delete.
    `free` is the number of open emoji slots. Uploading first keeps the name alive throughout
    and loses nothing when the upload fails, but it needs one free slot for a moment; without
    one (or with `delete_first`) the old emoji goes first. Names in `force` are swapped even
    when the pictures compare as the same.
    """
    p = ReplacePlan()
    for name, data in local.items():
        there = remote.get(name, [])
        if not there:
            p.absent.append(name)
            continue
        forced = name in force
        stale = [emoji for emoji, image in there if forced or not same_image(data, image)]
        if not stale:
            p.same.append(name)
            continue
        upload = len(stale) == len(there)  # none of them is the new art yet
        first = upload and (delete_first or free < 1)
        p.replace.append(Replacement(name, stale, upload, first))
        free += len(stale) - (1 if upload else 0)
    return p


def replace_missing_permissions(perms: discord.Permissions, to_delete: list, me_id) -> list[str]:
    """Create Expressions uploads, and deletes what this bot uploaded; the rest takes Manage Expressions."""
    if perms.administrator:
        return []
    missing = [] if perms.create_expressions else ["Create Expressions"]
    foreign = [e for e in to_delete if getattr(getattr(e, "user", None), "id", None) != me_id]
    if foreign and not perms.manage_expressions:
        missing.append("Manage Expressions")
    return missing


def show_replace(p: ReplacePlan, used: int, limit: int):
    print(f"\nEmoji art: {used}/{limit} slots used")
    for r in p.replace:
        old = "the old one" if len(r.stale) == 1 else f"the {len(r.stale)} old ones"
        if not r.upload:
            print(f"  tidy    {r.name} (the new art is already there; delete {old})")
        elif r.delete_first:
            print(f"  replace {r.name} (delete {old}, then upload the new one)")
        else:
            print(f"  replace {r.name} (upload the new one, then delete {old})")
    for name in p.same:
        print(f"  same    {name}")
    for name in p.absent:
        print(f"  absent  {name} (not on the server; a run without --replace adds it)")


REPLACE_WARNING = """
Replacing gives each of these emoji a new ID, because Discord cannot change an emoji's picture.
  - Typing :name: and the emoji picker use the new art straight away.
  - Messages and reactions that already used the old emoji keep pointing at the old ID. They do
    not switch to the new art: Discord goes on showing the old picture as an emoji from nowhere
    (it cannot be picked or added to), or falls back to the text :name:.
  - Anything in Server Settings that was set to one of these emoji (an Onboarding option, a forum
    tag or default reaction, the welcome screen) points at the old ID too: check it afterwards and
    set it again if it went blank."""


async def run_replace(guild, apply: bool, delete_first: bool = False, force: frozenset[str] = frozenset()) -> int:
    """Swap emoji whose local art differs from the server's. Returns the process exit code."""
    files = {p.stem: p for p in local_files(EMOJI_DIR, ".png")}
    bad = {p.name: problems for p in files.values() if (problems := check_emoji(p))}
    if bad:
        print("These files break Discord's limits; fix make_emoji.py and re-run it:")
        for name, problems in bad.items():
            print(f"  {name}: {'; '.join(problems)}")
        return 1
    unknown = sorted(force - set(files))
    if unknown:
        print(f"--force names no file in {EMOJI_DIR}: {', '.join(unknown)}")
        return 1
    if not files:
        print(f"Nothing to compare. Run make_emoji.py in {ROOT} first.")
        return 1

    emojis = await guild.fetch_emojis()
    static = [e for e in emojis if not e.animated]
    remote: dict[str, list[tuple]] = {}
    for e in static:
        if e.name in files and not getattr(e, "managed", False):
            remote.setdefault(e.name, []).append((e, await e.read()))  # a download, changes nothing
    limit = guild.emoji_limit
    local = {name: path.read_bytes() for name, path in files.items()}
    p = plan_replace(local, remote, limit - len(static), delete_first, force)
    show_replace(p, len(static), limit)
    if not p.replace:
        print("\nNothing to replace.")
        return 0
    print(REPLACE_WARNING)

    to_delete = [e for r in p.replace for e in r.stale]
    missing = replace_missing_permissions(guild.me.guild_permissions, to_delete, guild.me.id)
    if missing:
        print(f"\nThe bot's role is missing: {', '.join(missing)}.")
        print("Server Settings -> Roles -> (the bot's role) -> Permissions -> turn on "
              + " and ".join(f'"{m}"' for m in missing) + ", then re-run.")
        return 2

    if not apply:
        print("\nThat's the plan. Run again with --replace --apply.")
        return 0

    reason = "expressions.py --replace: redrawn emoji"

    async def upload(r: Replacement):
        # the new emoji keeps the old one's role restriction, if it had one
        await guild.create_custom_emoji(name=r.name, image=local[r.name],
                                        roles=getattr(r.stale[0], "roles", None) or [], reason=reason)
        print(f"  added   :{r.name}: (new art)")

    for r in p.replace:  # one at a time, so a failure leaves at most one emoji half done
        if r.upload and not r.delete_first:
            try:
                await upload(r)
            except discord.HTTPException as e:
                print(f"\nDiscord refused the new :{r.name}: while the old one is still there: {e}")
                print("Nothing was deleted for it. If the reason is the name being taken, run again with "
                      "--replace --delete-first --apply.")
                return 1
        for old in r.stale:
            await old.delete(reason=reason)
            print(f"  deleted :{r.name}: (old art)")
        if r.upload and r.delete_first:
            try:
                await upload(r)
            except discord.HTTPException as e:
                print(f"\nDiscord refused the new :{r.name}: after the old one was deleted: {e}")
                print("Fix the cause, then a run without --replace (python expressions.py --apply) uploads it.")
                return 1
    print("\nDone.")
    return 0


# ---------------------------------------------------------------- run
async def run(guild, apply: bool) -> int:
    """Returns the process exit code."""
    bad = {p.name: check_emoji(p) for p in local_files(EMOJI_DIR, ".png")}
    bad |= {p.name: check_sound(p) for p in local_files(SOUND_DIR, ".mp3")}
    bad = {k: v for k, v in bad.items() if v}
    if bad:
        print("These files break Discord's limits; fix the generators and re-run them:")
        for name, problems in bad.items():
            print(f"  {name}: {'; '.join(problems)}")
        return 1

    emoji_files = {p.stem: p for p in local_files(EMOJI_DIR, ".png")}
    sound_files = {p.stem: p for p in local_files(SOUND_DIR, ".mp3")}
    if not emoji_files and not sound_files:
        print(f"Nothing to upload. Run make_emoji.py and make_sounds.py in {ROOT} first.")
        return 1

    emojis = await guild.fetch_emojis()
    static = [e for e in emojis if not e.animated]
    e_limit = guild.emoji_limit
    e_plan = plan(list(emoji_files), {e.name for e in emojis}, len(static), e_limit)
    show("Emoji", e_plan, len(static), e_limit)

    sounds = await guild.fetch_soundboard_sounds()
    s_limit = soundboard_limit(guild)
    s_plan = plan(list(sound_files), {s.name for s in sounds}, len(sounds), s_limit)
    show("Soundboard", s_plan, len(sounds), s_limit)

    # Checked after the plan so a dry run still shows what would happen.
    missing = missing_permissions(guild.me.guild_permissions)
    if missing:
        print(f"\nThe bot's role is missing: {', '.join(missing)}.")
        print("Server Settings -> Roles -> (the bot's role) -> Permissions -> turn on "
              + " and ".join(f'"{m}"' for m in missing) + ", then re-run.")
        return 2

    if not apply:
        print("\nThat's the plan. Run again with --apply.")
        return 0

    for name in e_plan.create:
        await guild.create_custom_emoji(name=name, image=emoji_files[name].read_bytes(),
                                        reason="expressions.py: server emoji pack")
        print(f"  added   :{name}:")
    for name in s_plan.create:
        await guild.create_soundboard_sound(name=name, sound=sound_files[name].read_bytes(),
                                            emoji=SOUND_EMOJI.get(name),
                                            reason="expressions.py: server soundboard pack")
        print(f"  added   sound {name}")
    print("\nDone.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="upload (default is a dry run)")
    parser.add_argument("--replace", action="store_true",
                        help="instead of adding what is missing, swap emoji whose art has changed")
    parser.add_argument("--delete-first", action="store_true",
                        help="with --replace: delete each old emoji before uploading its new one")
    parser.add_argument("--force", nargs="+", default=[], metavar="NAME",
                        help="with --replace: swap these even if the pictures compare as the same")
    args = parser.parse_args()
    if (args.delete_first or args.force) and not args.replace:
        parser.error("--delete-first and --force only go with --replace")

    load_dotenv(Path(__file__).with_name(".env"))
    token, guild_id = os.getenv("DISCORD_TOKEN"), os.getenv("GUILD_ID")
    if not token or not guild_id:
        sys.exit("Set DISCORD_TOKEN and GUILD_ID in server/.env (copy .env.example).")

    client = discord.Client(intents=discord.Intents.default())
    result = {"code": 1}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(guild_id))
            if guild is None:
                print(f"The bot isn't in server {guild_id}.")
                return
            if args.replace:
                result["code"] = await run_replace(guild, args.apply, args.delete_first, frozenset(args.force))
            else:
                result["code"] = await run(guild, args.apply)
        except discord.Forbidden as e:
            print(f"\nDiscord said no (missing permission?): {e}")
        except discord.HTTPException as e:
            print(f"\nDiscord refused a change: {e}")
        finally:
            await client.close()

    client.run(token, log_handler=None)
    sys.exit(result["code"])


if __name__ == "__main__":
    main()
