"""The driver for logic/quiet.py: reads who responded, keeps each kind's state in `meta` and
tells staff when a kind goes to sleep or wakes. The rule itself is in logic/quiet.py.

A cog that posts a recurring prompt asks before it picks anything and reports after it sent:

    if not await quietgate.allowed(self.bot, Q.QOTD, channel, period.scheduled_at):
        return True  # skipped on purpose: the period is done, the question bank untouched
    message = await channel.send(...)
    await quietgate.posted(self.db, Q.QOTD, message, period.scheduled_at)

Response signals, cheapest first, read for the newest prompt of the kind only:

1. One `fetch_message`: poll votes, a reaction the bot did not add itself, messages in the
   thread opened from the prompt (the bot never writes in its own answers thread, so its
   message count is the answers). Any of them: answered, nothing else is read. A poll that
   shows no votes is checked against its voter list (one voter per answer) before it counts
   as ignored, because the count on an open poll is approximate.
2. Otherwise one history read of the channel after the prompt, at most SCAN messages: a
   member's reply to the prompt is an answer; any member message wakes a sleeping kind.

A cog that already knows when a member last wrote in the channel passes `last_member_at`
and nothing is read from Discord. Whatever cannot be read (a deleted prompt, no Read Message
History, an outage, a page of nothing but bot posts) is "could not tell", which the rule
treats as answered. A failure in here never blocks a prompt either: the gate fails open.
"""

import logging
import os

import discord

import config
import style
from logic import quiet as Q

log = logging.getLogger(__name__)

SCAN = 50  # channel messages read after a prompt, at most
MEMBER_MESSAGES = (discord.MessageType.default, discord.MessageType.reply)  # not joins, pins or boosts
NO_PINGS = discord.AllowedMentions.none()


def _on(environ) -> bool:
    return Q.enabled(os.environ if environ is None else environ)


# ---------------------------------------------------------------- response signals
def responded(message) -> bool:
    """A human did something with this prompt: voted, reacted or wrote in its thread.
    Bots cannot vote in polls. A reaction from another bot would count, which errs towards
    posting."""
    poll = getattr(message, "poll", None)
    if poll is not None and (getattr(poll, "total_votes", 0) or 0) > 0:
        return True
    for reaction in getattr(message, "reactions", None) or ():
        if (getattr(reaction, "count", 0) or 0) > (1 if getattr(reaction, "me", False) else 0):
            return True
    thread = getattr(message, "thread", None)
    return thread is not None and (getattr(thread, "message_count", 0) or 0) > 0


async def has_voter(message) -> bool:
    """Anyone on the voter list of the poll this message carries? The vote count that comes
    with a message is approximate while the poll is open and can be missing altogether, so a
    zero is checked here before a poll is called ignored: one voter asked for per answer."""
    poll = getattr(message, "poll", None)
    for answer in getattr(poll, "answers", None) or ():
        async for _voter in answer.voters(limit=1):
            return True
    return False


async def look(channel, post: Q.Post) -> Q.Evidence:
    """What happened to `post` since it went out: one message fetch (and, for a poll that shows
    no votes, its voter list), then one bounded history read only when that showed no response."""
    if post.message_id is None:
        return Q.Evidence()
    try:
        message = await channel.fetch_message(post.message_id)
        if responded(message) or await has_voter(message):
            return Q.Evidence(answered=True)
    except Exception as e:  # deleted, not readable, Discord is having a moment
        log.info("quiet: couldn't read prompt %s (%s)", post.message_id, type(e).__name__)
        return Q.Evidence()
    seen, member = 0, False
    try:
        async for m in channel.history(limit=SCAN, after=discord.Object(post.message_id)):
            seen += 1
            if m.author.bot or getattr(m, "type", discord.MessageType.default) not in MEMBER_MESSAGES:
                continue
            member = True
            ref = getattr(m, "reference", None)
            if ref is not None and getattr(ref, "message_id", None) == post.message_id:
                return Q.Evidence(answered=True, member_since=True)
    except Exception as e:
        log.info("quiet: couldn't read the channel after prompt %s (%s)", post.message_id, type(e).__name__)
        return Q.Evidence(answered=False)
    if member:
        return Q.Evidence(answered=False, member_since=True)
    # A full page of bot posts and system messages: there may be a member further down.
    return Q.Evidence(answered=False, member_since=None if seen >= SCAN else False)


# ---------------------------------------------------------------- staff
async def tell_staff(bot, line: str) -> bool:
    """One line in the mod log, no pings. False when there is no mod log or it refused."""
    log.info("quiet: %s", line)
    try:
        guild = bot.get_guild(bot.settings.guild_id)
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None
        if channel is None:
            return False
        await channel.send(embed=style.embed(description=line, color=style.MUTED, footer=style.label("quiet")),
                           allowed_mentions=NO_PINGS)
    except Exception:
        log.warning("quiet: couldn't write to the mod log", exc_info=True)
        return False
    return True


async def status_lines(db, environ=None) -> list[str]:
    """For /status: one line per kind that is asleep. Empty when none is, or with QUIET=off."""
    if not _on(environ):
        return []
    lines = []
    for kind in Q.KINDS:
        state = Q.load(await db.meta_get(Q.key(kind)))
        if state.asleep:
            lines.append(Q.status_line(kind, state))
    return lines


# ---------------------------------------------------------------- the gate
async def allowed(bot, kind: Q.Kind, channel, now: int, last_member_at: int | None = None, environ=None) -> bool:
    """True: post the prompt. False: this one is skipped on purpose (the room is not answering).

    `now` is the time the rule counts from: a scheduled job passes its period's scheduled time
    here and to `posted`, so the 7-day probe lands on the slot and not a minute after it.
    `last_member_at`: when a member last wrote in the channel, if the cog keeps that; the
    signals are then not read from Discord at all."""
    if not _on(environ):
        return True
    try:
        state = Q.load(await bot.db.meta_get(Q.key(kind)))
        if last_member_at is not None:
            evidence = Q.heard(state, last_member_at)
        else:
            evidence = await look(channel, state.posts[-1]) if state.posts else Q.Evidence()
        decision = Q.decide(state, now, evidence)
        if decision.state != state:
            await bot.db.meta_set(Q.key(kind), Q.dump(decision.state))
    except Exception:
        log.exception("quiet: the check for %s failed; posting", kind.key)
        return True
    if decision.change == Q.PAUSED:
        await tell_staff(bot, Q.paused_line(kind, decision.state, now))
    elif decision.change == Q.RESUMED:
        await tell_staff(bot, Q.resumed_line(kind, decision.reason))
    elif decision.reason == Q.PROBE:
        log.info("quiet: %s tries once after a quiet week", kind.key)
    return decision.post


async def posted(db, kind: Q.Kind, message, at: int, environ=None) -> None:
    """Record a prompt that went out. Never raises: the prompt is up, so the job must finish."""
    if not _on(environ):
        return
    try:
        state = Q.load(await db.meta_get(Q.key(kind)))
        await db.meta_set(Q.key(kind), Q.dump(Q.posted(state, at, getattr(message, "id", None))))
    except Exception:
        log.exception("quiet: couldn't record the %s prompt", kind.key)
