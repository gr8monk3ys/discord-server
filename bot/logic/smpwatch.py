"""SMP watch: one ping when people are on Monkey SMP. Pure: no sockets, no Discord I/O, no
database, no clock of its own (the cog passes `now`).

- Every CHECK_EVERY the cog asks the Minecraft server how many are on, with a Server List Ping
  (the codec in logic/monkey.py) to the public address players join with (DEFAULT_ADDRESS),
  found through its `_minecraft._tcp` SRV record as the game finds it. No RCON, no secrets, and
  no player names: only the count is read.
- When MIN_ONLINE or more are on, it posts one line in ⛏️・monkey-smp mentioning the opt-in
  SMP Ping role ("3 on Monkey SMP now"). Then it waits: no other ping until the count has been
  seen below MIN_ONLINE (so a long evening is one ping) and COOLDOWN has passed since the last.
- Never in quiet hours (QUIET, local time). A crowd still on when they end gets its ping then.
- A ping that fails (the server restarting, the network) changes nothing, so it can't re-arm.
- SMP_WATCH=off turns it off. SMP_WATCH_ADDRESS points it elsewhere: `host` (looked up through
  SRV, else port 25565) or `host:port` (no SRV lookup). SMP_WATCH_MIN sets the count (2 to 50).

State: one JSON value in `meta` (STATE_KEY): when the last ping went out and whether the next
may go. Nothing about members.
"""

import json
import struct
from dataclasses import dataclass, replace
from datetime import tzinfo

import config
from logic.crew import is_quiet
from logic.monkey import DEFAULT_PORT, HOSTNAME_RE, parse_address

MINUTE = 60
HOUR = 60 * MINUTE

DEFAULT_ADDRESS = "monkeysmp.lscaturchio.xyz"  # the public join address (on the site), not a secret
MIN_ONLINE = 3
MIN_ONLINE_RANGE = (2, 50)
CHECK_EVERY = 5 * MINUTE
COOLDOWN = 4 * HOUR
QUIET = (22, 9)  # local hours: none from 10 PM up to 9 AM
SRV_CACHE = HOUR  # an SRV answer is used this long (or until a ping through it fails)

STATE_KEY = "smpwatch:state"  # meta; live data, never rename

ENV_ON, ENV_ADDRESS, ENV_MIN = "SMP_WATCH", "SMP_WATCH_ADDRESS", "SMP_WATCH_MIN"
OFF_WORDS = {"off", "0", "false", "no"}


# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    host: str = DEFAULT_ADDRESS
    port: int | None = None  # None: look the port (and host) up through SRV
    min_online: int = MIN_ONLINE
    problems: tuple[str, ...] = ()  # one line each; never repeats a value

    @property
    def address(self) -> str:
        """As players type it (shown in the ping)."""
        return self.host if self.port is None else f"{self.host}:{self.port}"


def read_settings(environ) -> Settings:
    """Settings from the environment (a mapping). Never raises: a bad value keeps its default and
    adds a problem line that names the variable but not the value."""
    problems: list[str] = []
    if str(environ.get(ENV_ON) or "on").strip().lower() in OFF_WORDS:
        return Settings(enabled=False)
    host, port = DEFAULT_ADDRESS, None
    raw = str(environ.get(ENV_ADDRESS) or "").strip()
    if raw:
        parsed = parse_address(raw)
        if parsed is None or raw.startswith("["):
            problems.append(f"{ENV_ADDRESS} is not host or host:port; using {DEFAULT_ADDRESS}")
        else:
            host, port = parsed[0], parsed[1] if ":" in raw else None
    min_online = MIN_ONLINE
    raw_min = str(environ.get(ENV_MIN) or "").strip()
    if raw_min:
        lo, hi = MIN_ONLINE_RANGE
        if raw_min.isascii() and raw_min.isdigit() and lo <= int(raw_min) <= hi:
            min_online = int(raw_min)
        else:
            problems.append(f"{ENV_MIN} must be a whole number from {lo} to {hi}; using {MIN_ONLINE}")
    return Settings(True, host, port, min_online, tuple(problems))


# ---------------------------------------------------------------- DNS: just enough for one SRV lookup
DNS_PORT = 53
DNS_TIMEOUT = 3  # seconds per resolver
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")  # tried after the system's own (/etc/resolv.conf)
TYPE_SRV, CLASS_IN = 33, 1
MAX_REPLY = 4096
MAX_JUMPS = 16  # compression pointers followed in one name


class DnsError(Exception):
    """A reply that can't be used."""


@dataclass(frozen=True)
class Srv:
    priority: int
    weight: int
    port: int
    target: str


def srv_name(host: str) -> str:
    return f"_minecraft._tcp.{host}"


def srv_query(name: str, qid: int) -> bytes:
    """A recursive query for the SRV records of `name`, with id `qid`."""
    labels = name.rstrip(".").split(".")
    if not all(label.isascii() and 0 < len(label) <= 63 for label in labels):
        raise DnsError("bad name")
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"
    return struct.pack(">HHHHHH", qid & 0xFFFF, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", TYPE_SRV, CLASS_IN)


def _name(data: bytes, pos: int) -> tuple[str, int]:
    """The name at `pos` (following compression pointers) and where the record goes on after it."""
    labels: list[str] = []
    end, jumps = None, 0
    while True:
        if pos >= len(data):
            raise DnsError("truncated name")
        length = data[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data) or jumps >= MAX_JUMPS:
                raise DnsError("bad pointer")
            end = pos + 2 if end is None else end
            pos, jumps = ((length & 0x3F) << 8) | data[pos + 1], jumps + 1
            continue
        if length & 0xC0:
            raise DnsError("bad label")
        pos += 1
        if length == 0:
            break
        if pos + length > len(data):
            raise DnsError("truncated label")
        labels.append(data[pos:pos + length].decode("ascii", "replace"))
        pos += length
    name = ".".join(labels)
    if len(name) > 253:
        raise DnsError("name too long")
    return name, pos if end is None else end


def parse_srv_reply(data: bytes, qid: int) -> list[Srv]:
    """The SRV records in a reply to query `qid`. A name with no records (or no such name) is [];
    anything malformed, truncated, or not the answer to this query raises DnsError."""
    if not 12 <= len(data) <= MAX_REPLY:
        raise DnsError("reply size")
    rid, flags, qdcount, ancount = struct.unpack(">HHHH", data[:8])
    if rid != qid & 0xFFFF or not flags & 0x8000:
        raise DnsError("not our reply")
    if flags & 0x0200:
        raise DnsError("truncated reply")
    rcode = flags & 0x000F
    if rcode == 3:  # no such name
        return []
    if rcode:
        raise DnsError(f"rcode {rcode}")
    pos = 12
    for _ in range(qdcount):
        _, pos = _name(data, pos)
        pos += 4
    records = []
    for _ in range(ancount):
        _, pos = _name(data, pos)
        if pos + 10 > len(data):
            raise DnsError("truncated record")
        rtype, rclass, _ttl, rdlength = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if pos + rdlength > len(data):
            raise DnsError("truncated record")
        if rtype == TYPE_SRV and rclass == CLASS_IN and rdlength >= 7:
            priority, weight, port = struct.unpack(">HHH", data[pos:pos + 6])
            target, _ = _name(data, pos + 6)
            records.append(Srv(priority, weight, port, target))
        pos += rdlength
    return records


def pick_srv(records: list[Srv]) -> tuple[str, int] | None:
    """Where an SRV answer sends players: the lowest priority, then the heaviest weight. None when
    there is no usable record (no records, or "." for "no service here")."""
    usable = [r for r in records if 1 <= r.port <= 65535 and HOSTNAME_RE.fullmatch(r.target.rstrip("."))]
    if not usable:
        return None
    best = min(usable, key=lambda r: (r.priority, -r.weight, r.target, r.port))
    return best.target.rstrip("."), best.port


def fallback(host: str) -> tuple[str, int]:
    """No SRV record: the game connects to the name itself on the default port."""
    return host, DEFAULT_PORT


def resolv_conf_servers(text: str) -> list[str]:
    """`nameserver` lines of an /etc/resolv.conf (IPv4 only; the cog's socket is IPv4)."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver" and parts[1].count(".") == 3:
            out.append(parts[1])
    return out


# ---------------------------------------------------------------- when to ping
@dataclass(frozen=True)
class State:
    last_post_at: int | None = None
    armed: bool = True  # False after a ping, until the count is seen below the minimum


def load(raw: str | None) -> State:
    try:
        data = json.loads(raw) if raw else {}
        last = data.get("last_post_at")
        return State(last if isinstance(last, int) and not isinstance(last, bool) else None,
                     data.get("armed") is not False)
    except (ValueError, AttributeError):
        return State()


def dump(state: State) -> str:
    return json.dumps({"last_post_at": state.last_post_at, "armed": state.armed}, sort_keys=True)


def decide(state: State, online: int | None, now: int, tz: tzinfo, min_online: int = MIN_ONLINE,
           quiet: tuple[int, int] | None = QUIET) -> tuple[bool, State]:
    """(ping now?, the state to keep once the ping is out). `online` None: the server didn't answer."""
    if online is None:
        return False, state
    if online < min_online:
        return False, replace(state, armed=True)
    if not state.armed:
        return False, state
    if state.last_post_at is not None and now - state.last_post_at < COOLDOWN:
        return False, state
    if is_quiet(now, tz, quiet):
        return False, state
    return True, State(last_post_at=now, armed=False)


def ping_text(role_mention: str, online: int, address: str) -> str:
    """The one line: the count, the role, the address. No names."""
    return (f"⛏️ {role_mention} **{online} on Monkey SMP now.** Hop on: `{address}`\n"
            f"-# Opt out in {config.ROLES_CHANNEL} ({config.SMP_PING_ROLE}).")
