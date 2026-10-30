"""Tebex cancellation alerts: the rules. Pure: no sockets, no Discord I/O, no database.

When a buyer cancels a monthly pass, Tebex keeps it running to the end of the paid month and lists it
as Ending. Tebex sends a webhook, `recurring-payment.cancellation.requested`; Front Desk posts the
player, the package and the end date in the staff channel so staff DM them while the pass still runs
(the SMP repo's docs/RETENTION.md, "When a pass is ending").

Tebex's webhooks (docs.tebex.io, Webhooks):
- Every request carries X-Signature: HMAC-SHA256, keyed with the webhook secret, of the hex SHA-256
  of the raw body. A request without a matching signature is refused before its body is read as JSON.
- The body is {"id", "type", "date", "subject"}. The endpoint is checked once with a
  `validation.webhook`, answered with {"id": <its id>}.
- Any 2xx stops retries, so a type we don't use is answered 200 and ignored.

Settings (server/.env), all off unless TEBEX_WEBHOOK_SECRET is set:
- TEBEX_WEBHOOK_SECRET: the secret Tebex shows for the endpoint (16+ characters). Never logged.
- TEBEX_WEBHOOK_HOST: the address to listen on (default 127.0.0.1: only a local tunnel or reverse
  proxy can reach it).
- TEBEX_WEBHOOK_PORT: the port (default 8790).
The path is fixed: PATH.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime

PATH = "/tebex/webhook"
MAX_BODY = 256 * 1024
SECRET_MIN = 16
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
PORT_RANGE = (1024, 65535)

ENV_SECRET, ENV_HOST, ENV_PORT = "TEBEX_WEBHOOK_SECRET", "TEBEX_WEBHOOK_HOST", "TEBEX_WEBHOOK_PORT"
VALIDATION = "validation.webhook"
CANCELLATION = "recurring-payment.cancellation.requested"
# Handed to the referral cog when it is on (logic/referral.py).
REFERRAL_TYPES = ("payment.completed", "payment.refunded", "payment.dispute.opened")


@dataclass(frozen=True)
class Settings:
    secret: str = field(repr=False)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


def read_settings(environ) -> tuple[Settings | None, tuple[str, ...]]:
    """(settings, problems). Settings is None (the receiver is off) without a long enough secret.
    Problem lines name the variable, never its value."""
    problems: list[str] = []
    secret = str(environ.get(ENV_SECRET) or "").strip()
    if not secret:
        return None, ()
    if len(secret) < SECRET_MIN:
        return None, (f"{ENV_SECRET} is shorter than {SECRET_MIN} characters; the Tebex receiver stays off",)
    host = str(environ.get(ENV_HOST) or "").strip() or DEFAULT_HOST
    if any(c.isspace() or c in "/[]" for c in host):
        problems.append(f"{ENV_HOST} is not a host name or IP address; using {DEFAULT_HOST}")
        host = DEFAULT_HOST
    port = DEFAULT_PORT
    raw = str(environ.get(ENV_PORT) or "").strip()
    if raw:
        lo, hi = PORT_RANGE
        if raw.isascii() and raw.isdigit() and lo <= int(raw) <= hi:
            port = int(raw)
        else:
            problems.append(f"{ENV_PORT} is not a port from {lo} to {hi}; using {DEFAULT_PORT}")
    return Settings(secret=secret, host=host, port=port), tuple(problems)


# ---------------------------------------------------------------- signature
def signature(secret: str, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    return hmac.new(secret.encode(), digest.encode(), hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, header) -> bool:
    if not header or not isinstance(header, str):
        return False
    return hmac.compare_digest(signature(secret, body), header.strip().lower())


# ---------------------------------------------------------------- the body
class BadBody(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    subject: dict


def parse(body: bytes) -> Event:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadBody("not JSON") from exc
    if not isinstance(data, dict):
        raise BadBody("not an object")
    event_id, kind = data.get("id"), data.get("type")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 100:
        raise BadBody("no id")
    if not isinstance(kind, str) or not kind:
        raise BadBody("no type")
    subject = data.get("subject")
    return Event(id=event_id, type=kind, subject=subject if isinstance(subject, dict) else {})


def _get(obj, *path):
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def _text(value, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def when(value) -> int | None:
    """A Tebex date ("2026-11-14T17:00:00+00:00", or "Z") as a Unix time; None if missing or odd."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp())


def transaction(subject: dict) -> str:
    """A payment's Tebex transaction id, for the webhook record ("" when missing)."""
    return _text(_get(subject, "transaction_id"), 60)


@dataclass(frozen=True)
class Ending:
    player: str
    package: str
    ends_at: int | None
    reference: str
    reason: str


def ending(subject: dict) -> Ending:
    """The pass that is ending, from a recurring payment subject. Missing parts come back empty, so
    a payload Tebex changed still posts what it can."""
    payment = next((p for p in (_get(subject, "last_payment"), _get(subject, "initial_payment"))
                    if isinstance(p, dict)), {})
    player = _text(_get(payment, "customer", "username", "username") or _get(payment, "customer", "username"), 40)
    products = _get(payment, "products")
    names = [_text(_get(p, "name"), 80) for p in products if isinstance(p, dict)] if isinstance(products, list) else []
    return Ending(player=player, package=", ".join(n for n in names if n)[:200],
                  ends_at=when(_get(subject, "next_payment_at")),
                  reference=_text(_get(subject, "reference"), 60),
                  reason=_text(_get(subject, "cancel_reason"), 300))


def alert_text(e: Ending, esc) -> str:
    """The staff-channel post. `esc` escapes member-authored text (markdown, mentions)."""
    player = f"**{esc(e.player)}**" if e.player else "A buyer (no name in the webhook)"
    package = esc(e.package) if e.package else "their pass"
    end = f"<t:{e.ends_at}:D> (<t:{e.ends_at}:R>)" if e.ends_at else "the end of the paid month"
    lines = [f"⏳ **Pass ending:** {player} cancelled {package}. It still runs until {end}.",
             "DM them while it runs: what's coming this season, the next event, and one question, "
             "\"Was there something that would have kept you?\" Write their answer down here."]
    if e.reason:
        lines.append(f"Reason given to Tebex: {esc(e.reason)}")
    if e.reference:
        lines.append(f"-# Tebex {esc(e.reference)}")
    return "\n".join(lines)
