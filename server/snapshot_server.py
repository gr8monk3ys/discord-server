"""Read-only snapshot of the live server's configuration, for git diffs.

    python snapshot_server.py              # writes server/snapshot/*.json
    python snapshot_server.py --compare    # ...then lists drift from layout.py

Writes server.json, roles.json, channels.json, onboarding.json, automod.json and
welcome.json. Names are used instead of IDs, and the output is sorted and stable, so
two runs with no server changes give identical files and `git diff` shows exactly
what changed. Nothing on the server is changed. Member lists, messages and invites
are never read. A fetch the bot lacks permission for is recorded as an "error" note
in its file instead of stopping the run.
"""

import argparse
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

import layout
import snapshot_lib as lib

sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = Path(__file__).with_name("snapshot")


async def member_names_for_overwrites(guild: discord.Guild) -> dict:
    """Names for members with their own channel overwrites (uncached without the members intent)."""
    ids = set()
    for ch in guild.channels:
        for target in ch.overwrites:
            if isinstance(target, discord.Object) and target.type is discord.Member:
                ids.add(target.id)
    names = {}
    for member_id in sorted(ids):
        try:
            names[member_id] = (await guild.fetch_member(member_id)).name
        except discord.HTTPException:
            pass  # left as "unresolved-N"
    return names


async def fetch(what, coro, convert):
    try:
        return convert(await coro)
    except discord.HTTPException as e:
        print(f"  note    {what}: {e}")
        return lib.error_note(what, e)


async def snapshot(guild: discord.Guild) -> dict:
    member_names = await member_names_for_overwrites(guild)
    names = {r.id: r.name for r in guild.roles}
    names.update({c.id: c.name for c in guild.channels})

    def channel_name(channel_id):
        return names.get(channel_id)

    files = {
        "server": lib.server_dict(guild),
        "roles": lib.roles_list(guild.roles),
        "channels": lib.channels_snapshot(guild.channels, member_names),
        "welcome": await fetch("welcome screen", guild.welcome_screen(), lib.welcome_dict),
        "onboarding": await fetch("onboarding", guild.onboarding(), lib.onboarding_dict),
        "automod": await fetch(
            "automod rules",
            guild.fetch_automod_rules(),
            lambda rules: lib.automod_list(rules, channel_name=channel_name),
        ),
    }
    return lib.scrub(files, names)


def write(files: dict, token: str) -> bool:
    problems = lib.find_leaks(files, secrets=[token])
    if problems:
        print("\nRefusing to write the snapshot; it would contain:")
        for p in problems:
            print(f"  {p}")
        return False
    OUT_DIR.mkdir(exist_ok=True)
    for name, data in files.items():
        path = OUT_DIR / f"{name}.json"
        text = lib.dumps(data)
        changed = not path.exists() or path.read_text(encoding="utf-8") != text
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"  {'wrote' if changed else 'same':<7} snapshot/{path.name}")
    return True


def summary(files: dict):
    chans = files["channels"]
    n_channels = sum(len(c["channels"]) for c in chans["categories"]) + len(chans["uncategorized"])
    automod = files["automod"]
    print(
        f"\n{len(files['roles'])} roles, {len(chans['categories'])} categories, {n_channels} channels, "
        f"{'?' if isinstance(automod, dict) else len(automod)} AutoMod rules"
    )
    admins = [r["name"] for r in files["roles"] if "administrator" in r["permissions"]]
    if admins:
        print(f"Roles with Administrator: {', '.join(admins)}")


def print_compare(files: dict):
    want_roles, want_channels = lib.layout_names(layout)
    have_roles = [r["name"] for r in files["roles"] if r["name"] != "@everyone"]
    chans = files["channels"]
    have_channels = [c["name"] for c in chans["uncategorized"]]
    for cat in chans["categories"]:
        have_channels.append(cat["name"])
        have_channels.extend(c["name"] for c in cat["channels"])
    for label, want, have in (("Roles", want_roles, have_roles), ("Channels", want_channels, have_channels)):
        missing, extra = lib.compare(want, have)
        print(f"\n{label} vs layout.py")
        for n in missing:
            print(f"  missing {n} (in layout.py, not on the server)")
        for n in extra:
            print(f"  extra   {n} (on the server, not in layout.py)")
        if not missing and not extra:
            print("  ok      no drift")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare", action="store_true", help="also list roles/channels that differ from layout.py")
    args = parser.parse_args()

    load_dotenv(Path(__file__).with_name(".env"))
    token, guild_id = os.getenv("DISCORD_TOKEN"), os.getenv("GUILD_ID")
    if not token or not guild_id:
        sys.exit("Set DISCORD_TOKEN and GUILD_ID in server/.env (copy .env.example).")

    client = discord.Client(intents=discord.Intents.default())
    result = {"ok": False}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(guild_id))
            if guild is None:
                print(f"The bot isn't in server {guild_id}.")
                return
            print(f"Snapshot of {guild.name}")
            files = await snapshot(guild)
            result["ok"] = write(files, token)
            if result["ok"]:
                summary(files)
                if args.compare:
                    print_compare(files)
        finally:
            await client.close()

    client.run(token, log_handler=None)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
