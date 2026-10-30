"""Monkey SMP referrals: the rules. Pure: no sockets, no Discord I/O, no database, no clock of its own.

A member runs `/referral` and gets a personal code. A new buyer types it at Tebex checkout; when the
purchase's `payment.completed` webhook (cogs/tebex.py) carries it, both players get the Kindred Band,
a wardrobe helm that only changes how a helmet looks, and the code's owner gets one more "friend
invited" (`rw.invites`, the SMP's invite ladder: Verdant at 1, the Horned Helm at 3). No money and no
discount change hands.

Where the code comes from (docs/TEBEX_WEBHOOK.md, "Referrals"), both read from the webhook:
- A Tebex custom variable (Settings > Custom Variables, type Alphanumeric) enabled on the pass
  packages. Its value is in each product's `variables` as {"identifier": <REFERRAL_VARIABLE>,
  "option": <what the buyer typed>}.
- Tebex's own creator codes (`creator_code` on the payment), only with REFERRAL_CREATOR_CODES=on: the
  owner then makes a Tebex creator code equal to the member's code. Tebex's creator codes carry a
  revenue share of the owner's choosing, so they stay off unless the owner opts in.

The unlock is the same as every other `rw.*` score: a scoreboard command on the player's name, which
works while they are offline (the SMP repo's tools/wardrobe.py). `grant_commands` builds exactly three;
the `rw.invites` add, the only one that isn't safe to repeat, goes last. REFERRAL_GRANT=staff (the
default) posts them in the staff channel to run over RCON; REFERRAL_GRANT=rcon sends them through the
Monkey SMP bridge, which accepts these three shapes and nothing else (logic/monkey.py `grant_sendable`).

Guards: a buyer's Minecraft name is referred at most once, ever; a Tebex transaction counts once; a
code can't refer its owner's own linked name (nor a name linked to the owner's Discord account).

Settings (server/.env), all off unless REFERRALS=on:
- REFERRALS: on / off (default off).
- REFERRAL_VARIABLE: the custom variable's identifier (default "referral").
- REFERRAL_CREATOR_CODES: on to also read Tebex creator codes (default off).
- REFERRAL_PACKAGES: package names or ids that count, comma-separated (empty: any package).
- REFERRAL_GRANT: staff or rcon (default staff).
"""

import re
import secrets
from dataclasses import dataclass

ENV_ON, ENV_VARIABLE, ENV_CREATOR, ENV_PACKAGES, ENV_GRANT = (
    "REFERRALS", "REFERRAL_VARIABLE", "REFERRAL_CREATOR_CODES", "REFERRAL_PACKAGES", "REFERRAL_GRANT")
DEFAULT_VARIABLE = "referral"
GRANT_STAFF, GRANT_RCON = "staff", "rcon"

PAYMENT_COMPLETED = "payment.completed"
PAYMENT_REFUNDED = "payment.refunded"
CHARGEBACK = "payment.dispute.opened"

CODE_PREFIX = "MK"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
CODE_BODY = 6
CODE_RE = re.compile(rf"{CODE_PREFIX}[{CODE_ALPHABET}]{{{CODE_BODY}}}")
NAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
VARIABLE_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")

SKIN = "Kindred Band"
KIN = "rw.kin"
INVITES = "rw.invites"

ON_WORDS = {"1", "on", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    variable: str = DEFAULT_VARIABLE
    creator_codes: bool = False
    packages: tuple[str, ...] = ()  # lower-cased names or ids; empty: any
    grant: str = GRANT_STAFF


def read_settings(environ) -> tuple[Settings | None, tuple[str, ...]]:
    """(settings, problems). None: referrals are off. Problem lines name the variable, never a value."""
    if str(environ.get(ENV_ON) or "").strip().lower() not in ON_WORDS:
        return None, ()
    problems: list[str] = []
    variable = str(environ.get(ENV_VARIABLE) or "").strip() or DEFAULT_VARIABLE
    if not VARIABLE_RE.fullmatch(variable):
        problems.append(f"{ENV_VARIABLE} isn't a Tebex variable identifier; using {DEFAULT_VARIABLE}")
        variable = DEFAULT_VARIABLE
    creator = str(environ.get(ENV_CREATOR) or "").strip().lower() in ON_WORDS
    packages = tuple(p for p in (" ".join(x.split()).lower() for x in str(environ.get(ENV_PACKAGES) or "").split(","))
                     if p)
    grant = str(environ.get(ENV_GRANT) or "").strip().lower() or GRANT_STAFF
    if grant not in (GRANT_STAFF, GRANT_RCON):
        problems.append(f"{ENV_GRANT} is neither {GRANT_STAFF} nor {GRANT_RCON}; using {GRANT_STAFF}")
        grant = GRANT_STAFF
    return Settings(variable=variable, creator_codes=creator, packages=packages, grant=grant), tuple(problems)


# ---------------------------------------------------------------- codes
def new_code(choice=secrets.choice) -> str:
    return CODE_PREFIX + "".join(choice(CODE_ALPHABET) for _ in range(CODE_BODY))


def normalize(text) -> str | None:
    """What a buyer typed, as a code: case, spaces and dashes don't matter. None when it isn't one."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    code = re.sub(r"[\s_-]+", "", text).upper()
    return code if CODE_RE.fullmatch(code) else None


# ---------------------------------------------------------------- the payment
def _get(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


@dataclass(frozen=True)
class Purchase:
    transaction: str
    buyer: str  # the Minecraft name Tebex has for the buyer; "" when missing or not a valid name
    packages: tuple[tuple[str, str], ...]  # (id, name) per product
    codes: tuple[str, ...]  # normalized codes found, in the order found, no repeats


def purchase(subject: dict, settings: Settings) -> Purchase:
    transaction = _get(subject, "transaction_id")
    transaction = transaction.strip()[:80] if isinstance(transaction, str) else ""
    products = _get(subject, "products")
    products = [p for p in products if isinstance(p, dict)] if isinstance(products, list) else []
    names = [_get(subject, "customer", "username", "username"), _get(subject, "customer", "username")]
    names += [_get(p, "username", "username") for p in products]
    buyer = next((n for n in names if isinstance(n, str) and NAME_RE.fullmatch(n)), "")
    packages = tuple((str(_get(p, "id") or ""), " ".join(str(_get(p, "name") or "").split())[:80]) for p in products)
    found: list[str] = []
    for p in products:
        variables = _get(p, "variables")
        for v in variables if isinstance(variables, list) else []:
            ident = _get(v, "identifier")
            if isinstance(ident, str) and ident.strip().lower() == settings.variable.lower():
                found.append(_get(v, "option"))
    if settings.creator_codes:
        found.append(_get(subject, "creator_code"))
    codes: list[str] = []
    for raw in found:
        code = normalize(raw)
        if code and code not in codes:
            codes.append(code)
    return Purchase(transaction=transaction, buyer=buyer, packages=packages, codes=tuple(codes))


def counts(p: Purchase, settings: Settings) -> bool:
    """Whether the purchase is for a package that counts (any, when REFERRAL_PACKAGES is empty)."""
    if not settings.packages:
        return bool(p.packages)
    return any(pid.lower() in settings.packages or name.lower() in settings.packages for pid, name in p.packages)


# ---------------------------------------------------------------- the decision
OK, NO_CODE, UNKNOWN, NO_BUYER, NOT_COUNTED, SELF, REPEAT, NO_REFERRER = (
    "ok", "no_code", "unknown_code", "no_buyer", "not_counted", "self", "repeat", "no_referrer")


def decide(p: Purchase, settings: Settings, *, code_owner: int | None, referrer_name: str | None,
           buyer_owner: int | None, buyer_claimed: bool, transaction_claimed: bool) -> str:
    """What to do with a purchase. code_owner: the member whose code it is (None: no such code).
    referrer_name: their Minecraft name now. buyer_owner: the member the buyer's name is linked to."""
    if not p.codes:
        return NO_CODE
    if code_owner is None:
        return UNKNOWN
    if not p.buyer:
        return NO_BUYER
    if not counts(p, settings):
        return NOT_COUNTED
    if transaction_claimed or buyer_claimed:
        return REPEAT
    if buyer_owner == code_owner or (referrer_name and referrer_name.lower() == p.buyer.lower()):
        return SELF
    if not referrer_name:
        return NO_REFERRER
    return OK


def grant_commands(referrer: str, buyer: str) -> list[str]:
    """The unlock, in the order to run it. Both names must be valid Minecraft names (checked again here)."""
    if not (NAME_RE.fullmatch(referrer or "") and NAME_RE.fullmatch(buyer or "")):
        raise ValueError("not a Minecraft name")
    return [f"scoreboard players set {buyer} {KIN} 1",
            f"scoreboard players set {referrer} {KIN} 1",
            f"scoreboard players add {referrer} {INVITES} 1"]


def undo_commands(referrer: str, buyer: str) -> list[str]:
    """What staff may run after a refund. Never sent by the bot: taking back a cosmetic is staff's call."""
    return [f"scoreboard players remove {referrer} {INVITES} 1",
            f"scoreboard players reset {buyer} {KIN}"]


# ---------------------------------------------------------------- what is said
def code_block(commands) -> str:
    return "```\n" + "\n".join(commands) + "\n```"


def staff_text(referrer_id: int, referrer: str, buyer: str, sent: int, commands: list[str], esc) -> str:
    """The staff-channel post for a referral. sent: how many of `commands` already went over RCON."""
    lines = [f"🤝 **Referral:** {esc(buyer)} bought a pass with <@{referrer_id}>'s code ({esc(referrer)}). "
             f"Both get the {SKIN}, and {esc(referrer)}'s friends-invited count goes up by one."]
    left = commands[sent:]
    if not left:
        lines.append("-# Done over RCON.")
    else:
        lines.append("Run over RCON (works while they're offline):" if not sent
                     else f"RCON stopped after {sent} of {len(commands)}. Run the rest:")
        lines.append(code_block(left))
    return "\n".join(lines)


def refund_text(referrer_id: int, referrer: str, buyer: str, kind: str, esc) -> str:
    what = "was refunded" if kind == PAYMENT_REFUNDED else "has a chargeback"
    return (f"↩️ **Referral payment {what}:** {esc(buyer)}, referred by <@{referrer_id}> ({esc(referrer)}). "
            "Nothing was taken back. If you want to, run:\n" + code_block(undo_commands(referrer, buyer)))


def problem_text(outcome: str, buyer: str, esc) -> str | None:
    """A staff note for a code that didn't count, when staff should know; None when it's not news."""
    who = esc(buyer) if buyer else "A buyer"
    if outcome == SELF:
        return f"🚫 **Referral refused:** {who} used their own referral code."
    if outcome == NO_REFERRER:
        return (f"⚠️ **Referral waiting:** {who} used a code whose owner has no linked Minecraft name. "
                "Ask them to `/smp link`, then grant it by hand.")
    if outcome == NO_BUYER:
        return "⚠️ **Referral:** a purchase carried a code but no Minecraft name; grant it by hand."
    return None


def referrer_dm(buyer: str, esc) -> str:
    return (f"🤝 **{esc(buyer)}** joined Monkey SMP with your referral code. You both get the {SKIN} in "
            "`/trigger wardrobe`, and it counts toward your friends-invited skins. Thanks for bringing them!")


def code_text(code: str, name: str, esc) -> str:
    return (f"Your referral code is **{code}**. A friend types it in the referral box at checkout when they buy "
            f"their first Monkey SMP pass; once it goes through you both get the {SKIN}, a wardrobe helm "
            f"(looks only), and it counts as a friend invited for `{esc(name)}`. No discount and no money: just "
            "the skin. Each new player can be referred once, and your own purchases don't count.")
