"""The Realms: rules for the bridge between the Discord server and the owner's Minecraft
server. Pure: no sockets, no files, no Discord I/O, no database, no clock of its own.

What lives here:

- Settings (`read_setup`): which of the four parts are on. Status needs REALMS_ADDRESS;
  whitelisting and Discord-to-Minecraft chat need RCON (host, port, password);
  Minecraft-to-Discord chat needs REALMS_LOG_PATH. The RCON host must be this machine or
  a private network (same rule as OLLAMA_URL in logic/catchup.py), and the password never
  appears in a repr, a problem line or an error.
- The two wire codecs, as bytes in and bytes out: Server List Ping (VarInt framing,
  handshake, status JSON) and Source RCON (length-prefixed packets). The cog owns the
  sockets and the timeouts.
- Everything that crosses the bridge is hostile input. Minecraft names must match
  `NAME_RE` before they are put in a command; chat going to Minecraft becomes a JSON text
  component built by `json.dumps` (so nothing a member types can end the string or start a
  command); text coming from Minecraft loses its formatting codes and control characters,
  is length-capped, filtered (logic/textfilter.py, links refused) and escaped.
- The server log parser for vanilla-format lines (chat, join, leave, death, advancement),
  the line splitter and the tail position rules (rotation, truncation, falling behind).
- Batching and rate limits in both directions, playtime and its coins, poll back-off and
  the health line.
"""

import json
import re
import struct
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import PurePosixPath, PureWindowsPath

from discord.utils import escape_markdown, escape_mentions

from logic import textfilter
from logic.catchup import local_host
from logic.quests import established  # noqa: F401 (re-exported: the cog gates coins and links on it)

# ---------------------------------------------------------------- names
NAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")


def valid_name(name) -> bool:
    """A Minecraft Java name: 3 to 16 of A-Z a-z 0-9 _ and nothing else (fullmatch, so a
    trailing newline does not slip through)."""
    return isinstance(name, str) and NAME_RE.fullmatch(name) is not None


def name_key(name: str) -> str:
    """Minecraft names are unique ignoring case; this is the stored key."""
    return name.lower()


# ---------------------------------------------------------------- settings
ENV_ADDRESS = "REALMS_ADDRESS"
ENV_RCON_HOST, ENV_RCON_PORT, ENV_RCON_PASSWORD = "REALMS_RCON_HOST", "REALMS_RCON_PORT", "REALMS_RCON_PASSWORD"
ENV_LOG_PATH = "REALMS_LOG_PATH"
ENV_NAMES = (ENV_ADDRESS, ENV_RCON_HOST, ENV_RCON_PORT, ENV_RCON_PASSWORD, ENV_LOG_PATH)
DEFAULT_PORT = 25565
DEFAULT_RCON_HOST = "127.0.0.1"
DEFAULT_RCON_PORT = 25575
HOSTNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
MAX_COMMAND_BYTES = 1400  # Minecraft reads RCON packets into a 1460-byte buffer
MAX_PATH = 1024


@dataclass(frozen=True)
class Setup:
    """What the environment asks for. Each part is on only when its own settings are valid."""
    address: tuple[str, int] | None = None  # Server List Ping target
    rcon: tuple[str, int] | None = None  # set only together with a password
    password: str = field(default="", repr=False)  # never shown, logged or put in an error
    log_path: str | None = None
    problems: tuple[str, ...] = ()  # one line each; never repeats a value

    @property
    def status_on(self) -> bool:
        return self.address is not None

    @property
    def rcon_on(self) -> bool:
        return self.rcon is not None and bool(self.password)

    @property
    def log_on(self) -> bool:
        return self.log_path is not None

    @property
    def any_on(self) -> bool:
        return self.status_on or self.rcon_on or self.log_on

    def parts(self) -> list[str]:
        names = [("status", self.status_on), ("whitelist", self.rcon_on), ("chat to Minecraft", self.rcon_on),
                 ("chat from Minecraft", self.log_on)]
        return [n for n, on in names if on]


def parse_port(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 5:
        return None
    port = int(raw)
    return port if 1 <= port <= 65535 else None


def parse_address(raw: str | None) -> tuple[str, int] | None:
    """`host`, `host:port` or `[v6]:port` -> (host, port), else None. The host is a plain
    host name or IP address: no scheme, no path, no user name, no whitespace."""
    raw = (raw or "").strip()
    port_text = ""
    if raw.startswith("["):
        host, sep, rest = raw[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            return None
        port_text = rest[1:] if rest else ""
        if rest == ":" or not re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", host):
            return None
    else:
        host, sep, port_text = raw.partition(":")
        if (sep and not port_text) or not HOSTNAME_RE.fullmatch(host):
            return None
    port = parse_port(port_text) if port_text else DEFAULT_PORT
    return (host, port) if port is not None else None


def absolute_path(raw: str) -> bool:
    return PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute()


def read_setup(environ) -> Setup:
    """Settings from the environment (a mapping). Never raises; a bad value turns its own
    part off and adds a problem line that names the variable but not the value."""
    problems: list[str] = []
    address = None
    raw_address = (environ.get(ENV_ADDRESS) or "").strip()
    if raw_address:
        address = parse_address(raw_address)
        if address is None:
            problems.append(f"{ENV_ADDRESS} is not host:port; the status panel is off")

    rcon = None
    password = environ.get(ENV_RCON_PASSWORD) or ""
    raw_host = (environ.get(ENV_RCON_HOST) or "").strip()
    raw_port = (environ.get(ENV_RCON_PORT) or "").strip()
    if password:
        host = raw_host or DEFAULT_RCON_HOST
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        port = parse_port(raw_port) if raw_port else DEFAULT_RCON_PORT
        if not local_host(host):
            problems.append(f"{ENV_RCON_HOST} refused: it must be localhost or a loopback/private-network IP "
                            "address; whitelisting and chat to Minecraft are off")
        elif port is None:
            problems.append(f"{ENV_RCON_PORT} is not a port number; whitelisting and chat to Minecraft are off")
        elif "\x00" in password or len(password.encode("utf-8")) > MAX_COMMAND_BYTES:
            problems.append(f"{ENV_RCON_PASSWORD} is not usable (too long, or it holds a NUL); "
                            "whitelisting and chat to Minecraft are off")
        else:
            rcon = (host, port)
    elif raw_host or raw_port:
        problems.append(f"{ENV_RCON_PASSWORD} is empty; whitelisting and chat to Minecraft are off")
    if rcon is None:
        password = ""

    log_path = None
    raw_path = (environ.get(ENV_LOG_PATH) or "").strip()
    if raw_path:
        if "\x00" in raw_path or len(raw_path) > MAX_PATH or not absolute_path(raw_path):
            problems.append(f"{ENV_LOG_PATH} must be the full path of the server's latest.log; "
                            "chat from Minecraft is off")
        else:
            log_path = raw_path
    return Setup(address, rcon, password, log_path, tuple(problems))


# ---------------------------------------------------------------- cleaning text
FORMAT_CODE_RE = re.compile("§.?")  # the section sign and the code letter after it
SPACE_RE = re.compile(r"\s+")


def clean_plain(text, cap: int) -> str:
    """One line of plain text: no Minecraft formatting codes, no control or invisible
    characters (newlines included), runs of whitespace collapsed, at most `cap` characters."""
    if not isinstance(text, str):
        return ""
    text = FORMAT_CODE_RE.sub("", text[: cap * 8 + 64])
    text = "".join(c if c.isprintable() else " " for c in text)
    text = SPACE_RE.sub(" ", text).strip()
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def for_discord(text: str) -> str:
    """Escape markdown and neutralise @mentions in already-cleaned text."""
    return escape_mentions(escape_markdown(text))


# ---------------------------------------------------------------- Server List Ping
MAX_STATUS_BYTES = 512 * 1024  # a modded server's status (mod list, icon) is a few hundred KB at most
PING_TIMEOUT = 5  # seconds for connect + request + reply together
STATUS_REQUEST = b"\x01\x00"  # length 1, packet id 0
MAX_SAMPLE = 100
VERSION_CAP, MOTD_CAP = 60, 200
MAX_PLAYERS_SHOWN = 20


class Incomplete(ValueError):
    """The buffer ended in the middle of a value."""


def pack_varint(n: int) -> bytes:
    """A protocol VarInt (32-bit, 7 bits per byte, low bits first; -1 takes five bytes)."""
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    """(value, next position). Values are unsigned here (they are lengths and ids).
    Raises Incomplete when `data` ends first and ValueError past five bytes."""
    value = 0
    for i in range(5):
        if pos + i >= len(data):
            raise Incomplete("varint cut short")
        byte = data[pos + i]
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value & 0xFFFFFFFF, pos + i + 1
    raise ValueError("varint longer than five bytes")


def frame(payload: bytes) -> bytes:
    return pack_varint(len(payload)) + payload


def handshake(host: str, port: int, protocol: int = -1) -> bytes:
    """The framed handshake packet asking for the status state. Protocol -1 means "just
    asking", which every server since 1.7 answers."""
    name = host.encode("utf-8")[:255]
    return frame(b"\x00" + pack_varint(protocol) + pack_varint(len(name)) + name + struct.pack(">H", port)
                 + pack_varint(1))


def parse_status(payload: bytes) -> dict:
    """The status reply's packet body (after the length prefix) -> the JSON object.
    Raises ValueError for anything else."""
    if len(payload) > MAX_STATUS_BYTES:
        raise ValueError("status reply too large")
    packet_id, pos = read_varint(payload)
    if packet_id != 0:
        raise ValueError("not a status reply")
    length, pos = read_varint(payload, pos)
    body = payload[pos:pos + length]
    if length > MAX_STATUS_BYTES or len(body) != length:
        raise ValueError("status reply cut short")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("status reply is not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("status reply is not an object")
    return data


def flatten_component(node, depth: int = 0, budget: int = 4000) -> str:
    """The plain text of a chat component (a string, or {"text", "extra": [...]}); bounded
    in depth and length so a hostile reply cannot make this expensive."""
    if depth > 8 or budget <= 0:
        return ""
    if isinstance(node, str):
        return node[:budget]
    if isinstance(node, list):
        out = ""
        for child in node[:64]:
            out += flatten_component(child, depth + 1, budget - len(out))
        return out
    if isinstance(node, dict):
        text = node.get("text")
        out = text[:budget] if isinstance(text, str) else ""
        return out + flatten_component(node.get("extra") if isinstance(node.get("extra"), list) else [],
                                       depth + 1, budget - len(out))
    return ""


def _count(value) -> int:
    return min(max(value, 0), 100_000) if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class Status:
    online: int
    max: int
    names: tuple[str, ...]  # the server's sample: valid names only; may be shorter than `online`
    version: str
    motd: str


def read_status(raw: dict) -> Status:
    """A sanitised view of a status JSON object. Every field is optional in the wild."""
    players = raw.get("players") if isinstance(raw.get("players"), dict) else {}
    sample = players.get("sample") if isinstance(players.get("sample"), list) else []
    names: list[str] = []
    for entry in sample[:MAX_SAMPLE * 4]:
        name = entry.get("name") if isinstance(entry, dict) else None
        if valid_name(name) and name_key(name) not in {name_key(n) for n in names} and len(names) < MAX_SAMPLE:
            names.append(name)
    version = raw.get("version") if isinstance(raw.get("version"), dict) else {}
    return Status(online=_count(players.get("online")), max=_count(players.get("max")), names=tuple(names),
                  version=clean_plain(version.get("name"), VERSION_CAP),
                  motd=clean_plain(flatten_component(raw.get("description")), MOTD_CAP))


# ---------------------------------------------------------------- RCON
RCON_AUTH, RCON_AUTH_RESPONSE, RCON_COMMAND, RCON_RESPONSE = 3, 2, 2, 0
RCON_TIMEOUT = 5  # seconds for one command, connecting and signing in included
RCON_MAX_BODY = 4096  # the largest reply body a server sends in one packet
RCON_MIN_LENGTH, RCON_MAX_LENGTH = 10, RCON_MAX_BODY + 10  # id + type + two NULs around the body
RCON_AUTH_FAILED = -1
RCON_MAX_SKIP = 8  # stale packets tolerated before a reply (leftovers of a split reply)


def rcon_packet(request_id: int, kind: int, body: str) -> bytes:
    """One packet: int32 length, int32 id, int32 type (all little-endian), body, two NULs.
    Raises ValueError for a body that is too long or holds a NUL (it would end the body)."""
    data = body.encode("utf-8")
    if b"\x00" in data:
        raise ValueError("NUL in an RCON body")
    if len(data) > MAX_COMMAND_BYTES:
        raise ValueError("RCON body too long")
    payload = struct.pack("<ii", request_id, kind) + data + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def rcon_length(header: bytes) -> int:
    """The payload length announced by a packet's first four bytes; ValueError when it is
    outside what a real server sends, so a bad peer cannot make the reader allocate."""
    if len(header) != 4:
        raise ValueError("RCON header cut short")
    (length,) = struct.unpack("<i", header)
    if not RCON_MIN_LENGTH <= length <= RCON_MAX_LENGTH:
        raise ValueError("RCON length out of range")
    return length


def parse_rcon(payload: bytes) -> tuple[int, int, str]:
    """(request id, type, body) from a packet's payload (everything after the length)."""
    if len(payload) < RCON_MIN_LENGTH or payload[-2:] != b"\x00\x00":
        raise ValueError("malformed RCON packet")
    request_id, kind = struct.unpack("<ii", payload[:8])
    return request_id, kind, payload[8:-2].decode("utf-8", "replace")


# ---------------------------------------------------------------- commands sent over RCON
BRIDGE_TAG = "[Discord]"  # prefix of relayed chat in Minecraft; lines carrying it never come back
SENDER_CAP = 32
CHAT_CAP = 256  # Minecraft's own chat length
LIST_COMMAND = "list"
LIST_RE = re.compile(r"There are (\d{1,6}) of a max(?: of)? (\d{1,6}) players online:?(.*)", re.DOTALL)


def whitelist_command(action: str, name: str) -> str:
    """`whitelist add|remove <name>`. The name is checked again here, at the last moment
    before it becomes part of a command, whatever the caller already did."""
    if action not in ("add", "remove"):
        raise ValueError("unknown whitelist action")
    if not valid_name(name):
        raise ValueError("not a Minecraft name")
    return f"whitelist {action} {name}"


def whitelist_result(action: str, reply: str) -> str:
    """What the server's answer means: "ok" (this command put it in the wanted state, or a
    removal found it already gone), "already" (an add for a name that was on the whitelist
    before: somebody else put it there), "unknown" (no such Minecraft account) or "error"
    (anything else; try again later)."""
    text = clean_plain(reply, 300).lower()
    if "does not exist" in text:
        return "unknown"
    if action == "add" and "already whitelisted" in text:
        return "already"
    wanted = ("added",) if action == "add" else ("removed", "not whitelisted")
    return "ok" if any(w in text for w in wanted) else "error"


def member_long_enough(joined_at: int | None, now: int) -> bool:
    """Linking is for members who have been in the server LINK_MEMBER_DAYS (on top of the
    account-age gate): a fresh join can't put a name on the whitelist. Unknown join time: no."""
    return joined_at is not None and now - joined_at >= LINK_MEMBER_DAYS * 24 * 60 * 60


def parse_list(reply: str) -> list[str] | None:
    """Player names from the `list` command's answer; None when it is not that answer."""
    m = LIST_RE.search(FORMAT_CODE_RE.sub("", reply or "")[:RCON_MAX_BODY])
    if m is None:
        return None
    names: list[str] = []
    for part in m.group(3).split(","):
        name = part.strip()
        if valid_name(name) and name_key(name) not in {name_key(n) for n in names}:
            names.append(name)
    return names


def discord_text(content: str | None, attachments: int = 0) -> str:
    """What a member's message becomes on its way to Minecraft: one clean line, capped."""
    text = clean_plain(content or "", CHAT_CAP)
    if not text and attachments:
        text = "[attachment]"
    return text


def tellraw_command(sender: str, text: str) -> str:
    """`tellraw @a <json>` for one relayed line. The JSON is built by json.dumps from a
    fixed structure, with ensure_ascii, so quotes, backslashes, braces and anything else a
    member types stay inside one string; the text is shortened until the command fits."""
    sender = clean_plain(sender, SENDER_CAP) or "someone"
    text = clean_plain(text, CHAT_CAP)
    while True:
        payload = json.dumps(["", {"text": BRIDGE_TAG + " ", "color": "aqua"}, {"text": f"<{sender}> {text}"}],
                             ensure_ascii=True, separators=(",", ":"))
        command = "tellraw @a " + payload
        if len(command) <= MAX_COMMAND_BYTES or len(text) <= 1:
            return command
        text = text[: len(text) * 3 // 4]


# ---------------------------------------------------------------- the server log
# Vanilla layout: "[12:34:56] [Server thread/INFO]: text". Forge and some launchers add a
# date to the time or a "[logger]" / "(logger)" before the colon; both are accepted. Only
# INFO lines from the server thread count, and the text is matched from its first
# character, so nothing a player types (it always follows "<name> ") can pose as an event.
LINE_RE = re.compile(r"\[[0-9A-Za-z:. -]{8,30}\] \[Server thread/INFO\](?: \[[^\]]{1,60}\]| \([^)]{1,60}\))?: (.*)")
_NAME = r"([A-Za-z0-9_]{3,16})"
CHAT_RE = re.compile(rf"(?:\[Not Secure\] )?<{_NAME}> (.*)")
JOIN_RE = re.compile(rf"{_NAME} joined the game")
LEAVE_RE = re.compile(rf"{_NAME} left the game")
ADVANCEMENT_RE = re.compile(rf"{_NAME} has (made the advancement|reached the goal|completed the challenge) \[(.+)\]")
DEATH_RE = re.compile(
    rf"{_NAME} ((?:was (?:slain|shot|blown up|killed|squished|squashed|pricked|impaled|fireballed|pummeled|stung"
    r"|frozen|struck|burned|burnt|skewered|obliterated|poked|doomed|roasted|smashed|stabbed)\b"
    r"|drowned|fell |hit the ground too hard|blew up|burned to death|went up in flames|tried to swim in lava"
    r"|died|starved to death|suffocated in a wall|walked into |withered away|froze to death"
    r"|experienced kinetic energy|discovered the floor was lava|didn't want to live"
    r"|left the confines of this world|went off with a bang).*)")
CHAT, JOIN, LEAVE, DEATH, ADVANCEMENT = "chat", "join", "leave", "death", "advancement"
MC_TEXT_CAP = 300
MAX_LINE_BYTES = 4096  # longer log lines are dropped whole
TAIL_CHUNK = 64 * 1024  # bytes read per pass
MAX_BEHIND = 256 * 1024  # further behind than this (a flood, or the bot was stuck): skip to the end


@dataclass(frozen=True)
class Event:
    kind: str
    name: str
    text: str = ""  # chat text, death sentence after the name, or advancement title
    verb: str = ""  # advancement only


def parse_line(line: str) -> Event | None:
    """The chat/join/leave/death/advancement event in one log line, or None."""
    m = LINE_RE.fullmatch(line.rstrip("\r\n"))
    if m is None:
        return None
    body = m.group(1)
    if (c := CHAT_RE.fullmatch(body)) is not None:
        return Event(CHAT, c.group(1), c.group(2))
    if (j := JOIN_RE.fullmatch(body)) is not None:
        return Event(JOIN, j.group(1))
    if (q := LEAVE_RE.fullmatch(body)) is not None:
        return Event(LEAVE, q.group(1))
    if (a := ADVANCEMENT_RE.fullmatch(body)) is not None:
        return Event(ADVANCEMENT, a.group(1), a.group(3), a.group(2))
    if (d := DEATH_RE.fullmatch(body)) is not None:
        return Event(DEATH, d.group(1), d.group(2))
    return None


def discord_line(event: Event) -> str | None:
    """The line to post in Discord for an event, or None when it must not be relayed: an
    empty message, an echo of our own relay, or text the filter refuses (invites, any
    link, blocked words). Everything is escaped; the caller still sends with no pings."""
    name = for_discord(event.name)
    if event.kind == DEATH:
        # The cause is somebody else's words as often as not (the killer's renamed weapon, a
        # name-tagged mob), and the relay gate only vouches for the player who died: say
        # that they died and nothing more.
        return f"💀 **{name}** died"
    text = clean_plain(event.text, MC_TEXT_CAP)
    if BRIDGE_TAG.lower() in text.lower():
        return None  # never relay a relay
    ok, _ = textfilter.check(text, allow_links=False)
    if not ok:
        return None
    shown = for_discord(text)
    if event.kind == CHAT:
        return f"**<{name}>** {shown}" if shown else None
    if event.kind == JOIN:
        return f"➕ **{name}** joined The Realms"
    if event.kind == LEAVE:
        return f"➖ **{name}** left The Realms"
    if event.kind == ADVANCEMENT:
        return f"🏅 **{name}** has {event.verb} **{shown}**" if shown else None
    return None


class LineSplitter:
    """Turns chunks of a log file into whole lines. A line longer than `max_line` bytes is
    dropped (all of it, however many chunks it spans), so memory stays bounded."""

    def __init__(self, max_line: int = MAX_LINE_BYTES):
        self.max_line = max_line
        self.carry = b""
        self.skipping = False

    def reset(self) -> None:
        self.carry = b""
        self.skipping = False

    def feed(self, data: bytes) -> list[str]:
        parts = (self.carry + data).split(b"\n")
        self.carry = parts.pop()
        lines: list[str] = []
        for part in parts:
            if self.skipping:  # the tail of a line that was already too long
                self.skipping = False
                continue
            if len(part) <= self.max_line:
                lines.append(part.rstrip(b"\r").decode("utf-8", "replace"))
        if len(self.carry) > self.max_line:
            self.carry = b""
            self.skipping = True
        return lines


@dataclass(frozen=True)
class TailState:
    file_id: int | None = None  # the file's inode / file index (0 where the platform has none)
    pos: int | None = None  # next byte to read; None before the file was ever seen


def tail_start(state: TailState, file_id: int, size: int) -> tuple[int, bool]:
    """(where to read from, whether half-read line state must be thrown away).

    First sight: the end of the file, so history is never replayed. A different file or a
    shorter one (rotated at a restart, or truncated): the top of the new file. Too far
    behind: the end."""
    if state.pos is None:
        return size, True
    start, reset = state.pos, False
    if file_id != state.file_id or size < state.pos:
        start, reset = 0, True
    if size - start > MAX_BEHIND:
        return size, True
    return start, reset


# ---------------------------------------------------------------- Minecraft -> Discord batching
FLUSH_EVERY = 3  # seconds between posts: at most 20 messages a minute, whatever Minecraft does
MAX_BATCH_LINES = 15
MAX_BATCH_CHARS = 1800
MAX_BACKLOG = 60  # lines kept waiting; older ones are dropped and counted


class Batcher:
    """Collects relay lines and hands them out as one Discord message per flush."""

    def __init__(self):
        self.lines: deque[str] = deque()
        self.dropped = 0

    def add(self, line: str) -> None:
        self.lines.append(line[:MAX_BATCH_CHARS // 2])
        while len(self.lines) > MAX_BACKLOG:
            self.lines.popleft()
            self.dropped += 1

    def take(self) -> str | None:
        """The next message (at most MAX_BATCH_LINES lines and MAX_BATCH_CHARS characters),
        or None when nothing is waiting."""
        out: list[str] = []
        used = 0
        if self.dropped:
            out.append(f"*… {self.dropped} earlier line{'s' if self.dropped != 1 else ''} skipped*")
            used = len(out[0]) + 1
            self.dropped = 0
        while self.lines and len(out) < MAX_BATCH_LINES and used + len(self.lines[0]) + 1 <= MAX_BATCH_CHARS:
            line = self.lines.popleft()
            out.append(line)
            used += len(line) + 1
        return "\n".join(out) if out else None


# ---------------------------------------------------------------- Discord -> Minecraft rate limit
USER_GAP = 2  # seconds between one member's relayed messages
WINDOW, WINDOW_MAX = 60, 20  # relayed messages per minute, everyone together
NOTICE_COOLDOWN = 60  # seconds between "not relayed" reactions for one reason and member
RCON_RETRY = 30  # seconds before chat tries an unreachable RCON again


class RateLimit:
    def __init__(self, gap: int = USER_GAP, window: int = WINDOW, limit: int = WINDOW_MAX):
        self.gap, self.window, self.limit = gap, window, limit
        self.last: dict[int, int] = {}
        self.recent: deque[int] = deque()

    def allow(self, user_id: int, now: int) -> bool:
        while self.recent and self.recent[0] <= now - self.window:
            self.recent.popleft()
        last = self.last.get(user_id)
        if (last is not None and now - last < self.gap) or len(self.recent) >= self.limit:
            return False
        self.last = {u: t for u, t in self.last.items() if now - t < self.gap}
        self.last[user_id] = now
        self.recent.append(now)
        return True


# ---------------------------------------------------------------- polling
TICK = 15  # seconds between looks at "is a poll due"
POLL_EVERY = 120  # seconds between polls while the server answers
OFFLINE_AFTER = 2  # failed polls in a row before the panel says offline (one blip is ignored)
BACKOFF = (30, 120, 240, 480, 900)  # delay after the 1st, 2nd, ... failed poll in a row
SWEEP_EVERY = 30 * 60  # how often linked members are checked against the member list
SYNC_BATCH = 10  # whitelist changes sent per poll
MAX_ATTEMPTS = 5  # refused whitelist changes are retried this often at poll speed...
SLOW_RETRY = 60 * 60  # ...then once an hour
LINK_COOLDOWN = 30  # seconds between one member's /realms link attempts
LINK_MEMBER_DAYS = 14  # days in this server before /realms link works (staff can /realms adopt sooner)
# A player's words reach Discord only for a confirmed link whose member may talk there now.
# When that member isn't in the gateway cache the API is asked, and its answer reused for:
MEMBER_GONE_FOR = 5 * 60  # "not a member"
MEMBER_FRESH_FOR = 60  # a member it returned (a later timeout never reaches a fetched object)
HINT_EVERY = 6 * 60 * 60  # per name: "some players aren't linked, so their chat stays in the game"


def poll_delay(failures: int) -> int:
    """Seconds until the next poll after `failures` failed polls in a row."""
    return POLL_EVERY if failures <= 0 else BACKOFF[min(failures, len(BACKOFF)) - 1]


def retry_due(attempts: int, updated_at: int, now: int) -> bool:
    return attempts < MAX_ATTEMPTS or now - updated_at >= SLOW_RETRY


# ---------------------------------------------------------------- playtime and coins
REASON = "realms"  # ledger reason
SLOT_SECONDS = 30 * 60
SLOT_COINS = 5  # per full 30 minutes online
DAILY_SLOTS = 8  # hard cap: 40 coins (4 hours) a local day
MAX_GAP = 5 * 60  # two polls further apart than this prove nothing about the time between
MIN_PLAYERS = 2  # players that must be on in both polls for the time to count: company, like voice coins
TOP_SIZE = 10


def credit_seconds(previous_at: int | None, now: int) -> int:
    """Seconds a player seen in two polls in a row was online between them; 0 when the
    polls are too far apart (the bot or the server was away in between)."""
    if previous_at is None:
        return 0
    gap = now - previous_at
    return gap if 0 < gap <= MAX_GAP else 0


def slots(seconds: int) -> int:
    """Paid half hours in a day's playtime, capped."""
    return min(max(seconds, 0) // SLOT_SECONDS, DAILY_SLOTS)


def ref(user_id: int, day: str, slot: int) -> str:
    return f"realms:{user_id}:{day}:{slot}"


def day_key(now: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(now, tz).date().isoformat()


def week_start(now: int, tz: tzinfo) -> date:
    """Monday of the local week containing `now`."""
    today = datetime.fromtimestamp(now, tz).date()
    return today - timedelta(days=today.weekday())


def fmt_duration(seconds: int) -> str:
    minutes = max(seconds, 0) // 60
    return f"{minutes // 60}h {minutes % 60:02}m" if minutes >= 60 else f"{minutes}m"


# ---------------------------------------------------------------- panel and health
def panel_signature(status: Status | None, names, down_since: int | None) -> tuple:
    """What the panel shows; it is edited only when this changes."""
    if status is None:
        return ("offline", down_since)
    return ("online", status.online, status.max, tuple(names), status.version, status.motd)


def panel_text(status: Status | None, names, down_since: int | None) -> str:
    """The panel's description. Names are validated Minecraft names, shown escaped."""
    if status is None:
        return "🔴 **Offline**" + (f" since <t:{down_since}:R>" if down_since else "")
    lines = [f"🟢 **Online** · {status.online} / {status.max} players"]
    names = sorted(names, key=str.lower)
    if names:
        shown = ", ".join(for_discord(n) for n in names[:MAX_PLAYERS_SHOWN])
        more = max(status.online, len(names)) - min(len(names), MAX_PLAYERS_SHOWN)
        lines.append(shown + (f" and {more} more" if more > 0 else ""))
    elif status.online:
        lines.append("The server isn't sharing who is on.")
    else:
        lines.append("Nobody is on right now.")
    if status.version:
        lines.append(f"Version: {for_discord(status.version)}")
    if status.motd and textfilter.check(status.motd, allow_links=False)[0]:
        lines.append(f"*{for_discord(status.motd)}*")
    return "\n".join(lines)


def health(setup: Setup, *, down_since: int | None = None, rcon_down_since: int | None = None,
           rcon_refused: bool = False, log_missing: bool = False, waiting: int = 0) -> str:
    """The self-check line: "off", "ok ..." or "unreachable since ...". Times are Discord
    timestamps, since this is shown in an embed."""
    if not setup.any_on:
        return "off"
    if down_since is not None:
        return f"unreachable since <t:{down_since}:f>"
    notes: list[str] = []
    if rcon_refused:
        notes.append("RCON refused the password")
    elif rcon_down_since is not None:
        notes.append(f"RCON unreachable since <t:{rcon_down_since}:f>")
    if log_missing:
        notes.append("log file not found")
    if waiting:
        notes.append(f"{waiting} whitelist change{'s' if waiting != 1 else ''} waiting")
    notes += [f"setting problem: {p}" for p in setup.problems]
    text = "ok (" + ", ".join(setup.parts()) + ")"
    return text + ("; " + "; ".join(notes) if notes else "")
