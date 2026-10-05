"""Pure helpers for snapshot_server.py: discord objects (duck-typed) -> plain dicts.

Nothing here imports discord, so the tests can feed it SimpleNamespace fakes.
Names are used instead of IDs everywhere (IDs change when something is recreated
and make noisy diffs), and output order never depends on input order.
"""

import json
import re

from names import slug

SNOWFLAKE = re.compile(r"(?<!\d)\d{17,20}(?!\d)")
INVITE = re.compile(r"discord(?:\.gg|(?:app)?\.com/invite)/\S+", re.IGNORECASE)
VOICE_TYPES = {"voice", "stage_voice"}


# ------------------------------------------------------------ small pieces


def perm_names(permissions) -> list:
    """Permissions (iterable of (name, bool)) -> sorted names that are True."""
    return sorted(name for name, value in permissions if value)


def color_hex(color):
    value = getattr(color, "value", color) or 0
    return f"#{value:06x}" if value else None


def enum_name(value):
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def emoji_str(emoji):
    """Unicode emoji as-is; custom emoji as ':name:' (their IDs stay out)."""
    if emoji is None:
        return None
    if getattr(emoji, "id", None):
        return f":{emoji.name}:"
    return getattr(emoji, "name", None) or str(emoji) or None


def name_of(obj):
    return getattr(obj, "name", None) if obj is not None else None


def sorted_names(objs) -> list:
    return sorted(n for n in (name_of(o) for o in objs or []) if n)


def error_note(what: str, exc: Exception) -> dict:
    return {"error": f"could not fetch {what}: {exc}"}


# ------------------------------------------------------------ roles


def role_dict(role) -> dict:
    return {
        "name": role.name,
        "color": color_hex(role.color),
        "hoist": bool(role.hoist),
        "mentionable": bool(role.mentionable),
        "managed": bool(role.managed),
        "permissions": perm_names(role.permissions),
    }


def roles_list(roles) -> list:
    """Top to bottom, as the Roles settings page shows them."""
    ordered = sorted(roles, key=lambda r: (-r.position, r.id))
    return [role_dict(r) for r in ordered]


# ------------------------------------------------------------ overwrites


def overwrites_dict(overwrites, member_names=None) -> dict:
    """{target: PermissionOverwrite} -> {"role:Name" | "member:name": {"allow": [...], "deny": [...]}}.

    Targets discord.py couldn't resolve (uncached members) use member_names (id -> name)
    or become "unresolved-N", numbered in a stable order; their IDs never appear.
    """
    member_names = member_names or {}
    out = {}
    unresolved = []
    for target, ow in overwrites.items():
        allow, deny = ow.pair()
        entry = {"allow": perm_names(allow), "deny": perm_names(deny)}
        if hasattr(target, "hoist"):
            out[f"role:{target.name}"] = entry
        elif hasattr(target, "display_name"):
            out[f"member:{target.name}"] = entry
        elif target.id in member_names:
            out[f"member:{member_names[target.id]}"] = entry
        else:
            unresolved.append((target.id, entry))
    for n, (_, entry) in enumerate(sorted(unresolved, key=lambda t: t[0]), start=1):
        out[f"unresolved-{n}"] = entry
    return out


# ------------------------------------------------------------ channels


def _type(ch) -> str:
    return enum_name(ch.type)


def _sort_key(ch):
    # Discord's sidebar puts text-like channels above voice ones inside a category.
    bucket = 1 if _type(ch) in VOICE_TYPES else 0
    return (bucket, ch.position, ch.id)


def channel_dict(ch, member_names=None) -> dict:
    kind = _type(ch)
    out = {
        "name": ch.name,
        "type": kind,
        "overwrites": overwrites_dict(ch.overwrites, member_names),
    }
    if kind == "category":
        return out
    out["synced"] = bool(getattr(ch, "permissions_synced", False))
    out["nsfw"] = bool(getattr(ch, "nsfw", False))
    if kind in VOICE_TYPES:
        out["user_limit"] = ch.user_limit
        out["bitrate"] = ch.bitrate
    else:
        out["topic"] = getattr(ch, "topic", None)
    out["slowmode"] = getattr(ch, "slowmode_delay", 0) or 0
    if kind == "forum":
        out["require_tag"] = bool(getattr(getattr(ch, "flags", None), "require_tag", False))
        out["tags"] = [
            {"name": t.name, "emoji": emoji_str(t.emoji), "moderated": bool(t.moderated)}
            for t in ch.available_tags
        ]
    return out


def channels_snapshot(channels, member_names=None) -> dict:
    """All guild channels (categories included) -> categories in order, each with its channels."""
    categories = sorted((c for c in channels if _type(c) == "category"), key=lambda c: (c.position, c.id))
    children = {id(cat): [] for cat in categories}
    loose = []
    for ch in channels:
        if _type(ch) == "category":
            continue
        cat = getattr(ch, "category", None)
        (children[id(cat)] if cat is not None and id(cat) in children else loose).append(ch)

    def listed(chs):
        return [channel_dict(c, member_names) for c in sorted(chs, key=_sort_key)]

    return {
        "categories": [{**channel_dict(cat, member_names), "channels": listed(children[id(cat)])} for cat in categories],
        "uncategorized": listed(loose),
    }


# ------------------------------------------------------------ server settings


def server_dict(guild) -> dict:
    return {
        "name": guild.name,
        "description": guild.description,
        "verification_level": enum_name(guild.verification_level),
        "explicit_content_filter": enum_name(guild.explicit_content_filter),
        "default_notifications": enum_name(guild.default_notifications),
        "mfa_level": enum_name(getattr(guild, "mfa_level", None)),
        "nsfw_level": enum_name(getattr(guild, "nsfw_level", None)),
        "afk_channel": name_of(guild.afk_channel),
        "afk_timeout": guild.afk_timeout,
        "system_channel": name_of(guild.system_channel),
        "rules_channel": name_of(guild.rules_channel),
        "public_updates_channel": name_of(getattr(guild, "public_updates_channel", None)),
        "preferred_locale": str(getattr(guild, "preferred_locale", "") or "") or None,
        "premium_progress_bar_enabled": bool(getattr(guild, "premium_progress_bar_enabled", False)),
        "features": sorted(str(f) for f in guild.features),
    }


def welcome_dict(ws) -> dict:
    return {
        "description": ws.description,
        "enabled": bool(ws.enabled),
        "channels": [
            {"channel": name_of(wc.channel), "description": wc.description, "emoji": emoji_str(wc.emoji)}
            for wc in ws.welcome_channels
        ],
    }


def onboarding_dict(ob) -> dict:
    return {
        "enabled": bool(ob.enabled),
        "mode": enum_name(ob.mode),
        "default_channels": sorted_names(ob.default_channels),
        "prompts": [
            {
                "title": p.title,
                "type": enum_name(p.type),
                "single_select": bool(p.single_select),
                "required": bool(p.required),
                "in_onboarding": bool(p.in_onboarding),
                "options": [
                    {
                        "title": o.title,
                        "description": o.description,
                        "emoji": emoji_str(o.emoji),
                        "roles": sorted_names(o.roles),
                        "channels": sorted_names(o.channels),
                    }
                    for o in p.options
                ],
            }
            for p in ob.prompts
        ],
    }


def automod_list(rules, channel_name=lambda _id: None) -> list:
    """AutoMod rules sorted by name. channel_name maps an alert channel id to its name."""

    def action(a):
        out = {"type": enum_name(a.type)}
        if getattr(a, "channel_id", None):
            out["channel"] = channel_name(a.channel_id) or "unknown channel"
        if getattr(a, "duration", None):
            out["duration_seconds"] = int(a.duration.total_seconds())
        if getattr(a, "custom_message", None):
            out["custom_message"] = a.custom_message
        return out

    def trigger(t):
        return {
            "type": enum_name(t.type),
            "keyword_filter": sorted(t.keyword_filter or []),
            "regex_patterns": sorted(t.regex_patterns or []),
            "presets": perm_names(t.presets) if t.presets is not None else [],
            "allow_list": sorted(t.allow_list or []),
            "mention_limit": t.mention_limit,
            "mention_raid_protection": t.mention_raid_protection,
        }

    return [
        {
            "name": r.name,
            "enabled": bool(r.enabled),
            "event_type": enum_name(r.event_type),
            "trigger": trigger(r.trigger),
            "actions": [action(a) for a in r.actions],
            "exempt_roles": sorted_names(r.exempt_roles),
            "exempt_channels": sorted_names(r.exempt_channels),
        }
        for r in sorted(rules, key=lambda r: r.name)
    ]


# ------------------------------------------------------------ output + safety

MENTION = re.compile(r"<(#|@&|@!?|a?:(\w+):)(\d{17,20})>")


def scrub(data, names=None):
    """Rewrite mentions inside text (<#id>, <@&id>, <@id>, <:emoji:id>) to names, recursively."""
    names = names or {}

    def one(m):
        kind, emoji, ident = m.group(1), m.group(2), int(m.group(3))
        if emoji:
            return f":{emoji}:"
        if kind == "#":
            return f"#{names.get(ident, 'unknown-channel')}"
        if kind == "@&":
            return f"@{names.get(ident, 'unknown-role')}"
        return "@user"  # never name or identify individual members in text

    if isinstance(data, dict):
        return {k: scrub(v, names) for k, v in data.items()}
    if isinstance(data, list):
        return [scrub(v, names) for v in data]
    if isinstance(data, str):
        return MENTION.sub(one, data)
    return data



def dumps(data) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def find_leaks(data, secrets=()) -> list:
    """Problems that must never reach a snapshot: secrets, ID-like numbers, invite links.

    The report names where the problem is, never the offending value.
    """
    problems = []
    secrets = [s for s in secrets if s]

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(k, f"{path}.<key>")
                walk(v, f"{path}.{k}")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, int):
            if len(str(abs(node))) >= 17:
                problems.append(f"{path}: looks like a Discord ID")
        elif isinstance(node, str):
            if any(s in node for s in secrets):
                problems.append(f"{path}: contains a secret")
            elif SNOWFLAKE.search(node):
                problems.append(f"{path}: looks like a Discord ID")
            elif INVITE.search(node):
                problems.append(f"{path}: contains an invite link")

    walk(data, "$")
    return problems


# ------------------------------------------------------------ compare with layout.py


def compare(expected, actual):
    """Names in expected but not actual (missing) and vice versa (extra), matched by slug."""
    exp = {slug(n): n for n in expected}
    act = {slug(n): n for n in actual}
    missing = sorted(n for s, n in exp.items() if s not in act)
    extra = sorted(n for s, n in act.items() if s not in exp)
    return missing, extra


def layout_names(layout):
    roles = [r["name"] for r in layout.ROLES]
    channels = []
    for cat in layout.CATEGORIES:
        channels.append(cat["name"])
        channels.extend(ch["name"] for ch in cat["channels"])
    return roles, channels
