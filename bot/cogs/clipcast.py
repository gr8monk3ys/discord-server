"""Clipcast: the week's best member clips on the server's YouTube channel as Shorts, with a
link back to the server's site. Nothing a member posted leaves Discord without their own
explicit yes for that clip.

- Sundays 18:30 local: the top clips of the week (the clip-of-the-week ranking, one per
  author, Discord-hosted mp4/webm/mov attachments only, established accounts only, caption
  through the text filter) are shortlisted and each author is asked by DM, or by a reply in
  the clips channel when DMs are closed: credit by name, anonymous, or no thanks. The buttons
  are DynamicItems, only the author's click counts, and the answer, who gave it and when are
  stored. `/clipcast consent always|ask|never` sets a standing answer.
- Tuesdays 18:30 local: approved clips are posted. With YOUTUBE_CLIENT_ID,
  YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN set, Front Desk uploads them itself (OAuth
  refresh token, resumable upload, plain aiohttp); without them it lists the approved clips
  in the mod log for the owner to post by hand, and says nothing when there are none.
- Deleting the clip, leaving, a ban or `/privacy tracking:off` withdraws consent for anything
  not posted yet. `/clipcast remove` takes a posted clip down (deleted through the API when
  it can be, and staff are told either way).
- The author gets the badge when the clip goes up and the coins only once it has stayed up
  for a week (`run_payouts`, from the same tick): a clip taken down before then never pays,
  so there is nothing to claw back.
- A clip is never uploaded twice: the row is claimed before the first byte is sent, and an
  upload whose result can't be established is parked for staff instead of retried.

Needs Message Content (attachments and captions) and Server Members; in the clips channel
View Channel, Send Messages and Read Message History, and Send Messages in the mod log.
Links to clips on other sites (Medal, YouTube, Twitch) are never re-uploaded."""

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Mapping, NamedTuple, Protocol

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import jobs
import style
from cogkit import is_staff
from cogs.clips import thread_key
from cogs.lfg import ping_only
from errors import reply_error
from logic import achievements as A
from logic import clipcast as L
from logic import clips as clip_rules
from logic import quests as Q

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
USER_AGENT = "FrontDesk Discord bot"
REQUEST_TIMEOUT = 30  # seconds: token, upload start, status probe, delete
TRANSFER_TIMEOUT = 15 * 60  # seconds: one download or one upload
MAX_REPLY = 64 * 1024  # bytes of any API reply that are read
RUN_KEY = "clipcast:last_run"  # meta: {"at", "text"} of the last posting run
ERROR_KEY = "clipcast:last_error"  # meta: {"at", "text"}, never with a secret in it
STAFF_ONLY = "Only Moderators and Keepers can do that."
OPTED_OUT = ("You've turned tracking off with `/privacy`, so your clips stay inside Discord. "
             "Run `/privacy tracking:on` first if you want one posted.")
NOT_A_CLIP = "I couldn't read that. Paste the clip's message link (right-click the message, Copy Message Link)."
PREF_CHOICES = [app_commands.Choice(name="always: post my shortlisted clips without asking", value=L.ALWAYS),
                app_commands.Choice(name="ask: ask me each time (the default)", value=L.ASK),
                app_commands.Choice(name="never: don't ask, don't post", value=L.NEVER)]
CREDIT_CHOICES = [app_commands.Choice(name="credit me by my display name", value=L.NAME),
                  app_commands.Choice(name="anonymous", value=L.ANON)]


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


# ---------------------------------------------------------------- network seam
class Response(NamedTuple):
    status: int
    headers: Mapping[str, str]
    body: bytes


def header(resp: Response, name: str) -> str:
    wanted = name.lower()
    return next((v for k, v in resp.headers.items() if k.lower() == wanted), "")


class Http(Protocol):
    """Everything that touches the network. Tests pass a fake."""

    async def request(self, method: str, url: str, *, params=None, headers=None, data=None, json=None,
                      file: Path | None = None, timeout: int = REQUEST_TIMEOUT) -> Response: ...

    async def download(self, url: str, dest: Path, max_bytes: int) -> tuple[int, str, int]: ...


class AiohttpHttp:
    """Redirects are never followed: a resumable upload answers 308 for "incomplete", and
    a token must not travel to wherever a redirect points."""

    @staticmethod
    def _session(timeout: int) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                     headers={"User-Agent": USER_AGENT})

    async def request(self, method, url, *, params=None, headers=None, data=None, json=None, file=None,
                      timeout=REQUEST_TIMEOUT) -> Response:
        handle = open(file, "rb") if file is not None else None
        try:
            async with self._session(timeout) as session:
                async with session.request(method, url, params=params, headers=headers, json=json,
                                           data=handle if handle is not None else data,
                                           allow_redirects=False) as resp:
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(16 * 1024):
                        body += chunk
                        if len(body) >= MAX_REPLY:
                            break
                    return Response(resp.status, dict(resp.headers), bytes(body[:MAX_REPLY]))
        finally:
            if handle is not None:
                handle.close()

    async def download(self, url, dest, max_bytes) -> tuple[int, str, int]:
        """Stream `url` into `dest`. Returns (status, content type, bytes received); stops
        as soon as more than `max_bytes` have arrived."""
        async with self._session(TRANSFER_TIMEOUT) as session:
            async with session.get(url, allow_redirects=False) as resp:
                size = 0
                if resp.status == 200:
                    with open(dest, "wb") as out:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                break
                            out.write(chunk)
                return resp.status, resp.headers.get("Content-Type", ""), size


# ---------------------------------------------------------------- YouTube
class Quota(Exception):
    """YouTube says stop for now (quota or upload limit). Nothing was created."""


class AuthProblem(Exception):
    """The credentials were refused. Nothing was created."""


class Failed(Exception):
    """This attempt failed cleanly: no video exists, so it's safe to try again."""


class Unsure(Exception):
    """The upload may or may not have produced a video. Never retried automatically."""


class YouTube:
    """YouTube Data API v3 with a refresh token. Messages raised here are built from our own
    words plus a status code and Google's short reason code: never a body, token or header."""

    def __init__(self, http: Http, setup: L.Setup, clock):
        self.http = http
        self.setup = setup
        self.clock = clock
        self._token: str | None = None
        self._refresh_by = 0

    async def token(self, fresh: bool = False) -> str:
        if not fresh and self._token and self.clock() < self._refresh_by:
            return self._token
        self._token = None
        try:
            resp = await self.http.request("POST", L.TOKEN_URL, data=L.token_form(self.setup))
        except Exception as exc:
            raise Failed(f"token: {type(exc).__name__}") from None
        if resp.status in (400, 401):
            raise AuthProblem(L.describe("Google refused the refresh token", resp.status, resp.body))
        parsed = L.parse_token(resp.body, self.clock()) if resp.status == 200 else None
        if parsed is None:
            raise Failed(L.describe("token", resp.status))
        self._token, self._refresh_by = parsed
        return self._token

    async def _authorized(self, method: str, url: str, *, step: str, **kwargs) -> Response:
        """One API call with the bearer token; a 401 refreshes the token and tries once more."""
        extra = kwargs.pop("headers", {})
        for attempt in (0, 1):
            token = await self.token(fresh=attempt == 1)
            try:
                resp = await self.http.request(method, url, headers={"Authorization": f"Bearer {token}", **extra},
                                               **kwargs)
            except Exception as exc:
                raise Failed(f"{step}: {type(exc).__name__}") from None
            if resp.status != 401:
                return resp
        raise AuthProblem(L.describe(step, 401, resp.body))

    async def upload(self, path: Path, size: int, content_type: str, resource: dict) -> str:
        """Upload one file; returns the new video's id."""
        step = "upload start"
        resp = await self._authorized(
            "POST", L.UPLOAD_URL, step=step, params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": content_type}, json=resource)
        if L.is_quota(resp.status, resp.body):
            raise Quota(L.describe(step, resp.status, resp.body))
        if resp.status != 200:
            raise Failed(L.describe(step, resp.status, resp.body))
        session_url = header(resp, "Location")
        if not L.safe_upload_url(session_url):
            raise Failed(f"{step}: unexpected upload address")

        # From here on a video may exist, so nothing below may be reported as a clean failure
        # unless Google itself says the session is incomplete or refused the request.
        auth = {"Authorization": f"Bearer {await self.token()}"}
        try:
            sent = await self.http.request("PUT", session_url, file=path, timeout=TRANSFER_TIMEOUT,
                                           headers={**auth, "Content-Length": str(size), "Content-Type": content_type})
        except Exception:
            sent = None
        if sent is not None:
            done = self._finished("upload", sent)
            if done is not None:
                return done
        try:
            probe = await self.http.request("PUT", session_url,
                                            headers={**auth, "Content-Length": "0", "Content-Range": f"bytes */{size}"})
        except Exception as exc:
            raise Unsure(f"upload interrupted and the status check failed ({type(exc).__name__})") from None
        done = self._finished("upload check", probe)
        if done is not None:
            return done
        if probe.status == 308:  # "resume incomplete": the session never finished, so no video
            raise Failed("upload interrupted before it finished")
        raise Unsure(L.describe("upload interrupted; status check", probe.status, probe.body))

    @staticmethod
    def _finished(step: str, resp: Response) -> str | None:
        """The video id from a finished upload; None when the reply settles nothing (5xx,
        308). A 4xx means Google refused the request, so nothing was created."""
        if resp.status in (200, 201):
            vid = L.video_id(resp.body)
            if vid is None:
                raise Unsure(f"{step}: finished without a readable video id")
            return vid
        if L.is_quota(resp.status, resp.body):
            raise Quota(L.describe(step, resp.status, resp.body))
        if 400 <= resp.status < 500 and resp.status != 404:  # 404: the session is gone, cause unknown
            raise Failed(L.describe(step, resp.status, resp.body))
        return None

    async def delete(self, vid: str) -> tuple[bool, str]:
        """(deleted or already gone, secret-free detail)."""
        try:
            resp = await self._authorized("DELETE", L.VIDEOS_URL, step="delete", params={"id": vid})
        except (Failed, AuthProblem) as exc:
            return False, str(exc)
        if resp.status in (204, 404):
            return True, ""
        return False, L.describe("delete", resp.status, resp.body)


# ---------------------------------------------------------------- UI
class ConsentButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"clipcast:(?P<choice>name|anon|no):(?P<id>\d+)"):
    STYLES = {L.NAME: discord.ButtonStyle.success, L.ANON: discord.ButtonStyle.primary,
              L.NO: discord.ButtonStyle.secondary}

    def __init__(self, choice: str, message_id: int, label: str | None = None):
        super().__init__(discord.ui.Button(label=(label or choice)[:80], style=self.STYLES[choice],
                                           custom_id=f"clipcast:{choice}:{message_id}"))
        self.choice = choice
        self.message_id = message_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["choice"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Clipcast" = interaction.client.get_cog("Clipcast")
        try:
            await cog.handle_button(interaction, self.choice, self.message_id)
        except Exception:
            log.exception("clipcast: %s button on clip %s failed", self.choice, self.message_id)
            await reply_error(interaction)


def build_view(message_id: int, name: str | None) -> discord.ui.View:
    """The consent buttons. `name`: the credit the member would get, None when their
    display name can't be used (then only the anonymous yes is offered)."""
    view = discord.ui.View(timeout=None)
    if name:
        view.add_item(ConsentButton(L.NAME, message_id, f"Yes, credit me as {name}"))
    view.add_item(ConsentButton(L.ANON, message_id, "Yes, anonymously"))
    view.add_item(ConsentButton(L.NO, message_id, "No thanks"))
    return view


class Seen(NamedTuple):
    """A clip's message as it is right now."""

    message: object
    channel: object
    reactions: int
    attachment: object | None
    media: str | None  # why it can't be uploaded, or None
    caption: str


GONE, UNKNOWN = "gone", "unknown"  # inspect() results when there's no message to look at


# ---------------------------------------------------------------- the cog
class Clipcast(commands.Cog):
    clipcast = app_commands.Group(name="clipcast", description="The week's best clips on the server's YouTube",
                                  guild_only=True)

    def __init__(self, bot, http: Http | None = None, environ=None, tmp_dir: Path | None = None):
        self.bot = bot
        self.http = http or AiohttpHttp()
        self.setup = L.read_setup(config.clipcast_env(os.environ if environ is None else environ))
        self.youtube = YouTube(self.http, self.setup, lambda: now())
        self.tmp_dir = Path(tmp_dir) if tmp_dir is not None else config.DATA_DIR / "clipcast"
        self.publishing = asyncio.Lock()  # one posting run at a time
        # Clips whose consent was withdrawn while their upload was in flight: taken down
        # the moment the upload returns.
        self.late: set[int] = set()
        # Weeks of a job that are waiting for the server or a channel (for the log only).
        self.waiting: set[str] = set()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ConsentButton)
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()
        self.bot.remove_dynamic_items(ConsentButton)

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    @staticmethod
    def jump(guild_id: int, channel_id: int, message_id: int) -> str:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    def jump_row(self, guild, row) -> str:
        return self.jump(guild.id, row["channel_id"], row["message_id"])

    async def row(self, message_id: int):
        return await self.db.fetchone("SELECT * FROM clipcast WHERE message_id = ?", (message_id,))

    async def mod_log(self, guild, text: str | None = None, *, embed=None, ping_owner: bool = False) -> bool:
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None
        if channel is None:
            log.info("clipcast: no %s channel for a staff note", config.MOD_LOG_CHANNEL)
            return False
        owner = getattr(guild, "owner_id", None)
        if ping_owner and owner and text:
            text = f"<@{owner}> {text}"
        mentions = ping_only(users=[discord.Object(owner)]) if ping_owner and owner else NO_PINGS
        try:
            await channel.send(text, embed=embed, allowed_mentions=mentions)
            return True
        except discord.HTTPException:
            log.warning("clipcast: couldn't write to the mod log", exc_info=True)
            return False

    async def set_meta(self, key: str, t: int, text: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                              (key, json.dumps({"at": t, "text": text[:300]})))

    async def get_meta(self, key: str) -> dict | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        try:
            data = json.loads(row["value"]) if row else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------ status changes
    async def move(self, message_id: int, sources, new: str, note: str | None = None) -> bool:
        """Atomic status change; False if the row wasn't in one of `sources` any more."""
        marks = ",".join("?" * len(sources))
        return bool(await self.db.execute(
            f"UPDATE clipcast SET status = ?, note = ?, updated_at = ? WHERE message_id = ? AND status IN ({marks})",
            (new, note, now(), message_id, *sources)))

    async def withdraw(self, column: str, values, note: str) -> int:
        """Withdraw consent for everything not posted yet where `column` (ours, never user
        input) is one of `values`. Uploads already in flight are flagged for takedown."""
        values = list(values)
        if not values:
            return 0
        marks = ",".join("?" * len(values))
        flying = await self.db.fetchall(
            f"SELECT message_id FROM clipcast WHERE status = ? AND {column} IN ({marks})", (L.UPLOADING, *values))
        self.late.update(r["message_id"] for r in flying)
        return await self.db.execute(
            f"UPDATE clipcast SET status = ?, note = ?, updated_at = ? WHERE status IN (?, ?) AND {column} IN ({marks})",
            (L.WITHDRAWN, note, now(), *L.PENDING, *values))

    async def forget_member(self, user_id: int, note: str) -> None:
        n = await self.withdraw("user_id", [user_id], note)
        await self.db.execute("DELETE FROM clipcast_prefs WHERE user_id = ?", (user_id,))
        if n:
            log.info("clipcast: withdrew %d clip(s) of %s (%s)", n, user_id, note)

    async def sweep(self) -> None:
        """Catch what has no event: /privacy tracking:off, and an upload cut off by a crash."""
        t = now()
        await self.db.execute(
            "UPDATE clipcast SET status = ?, note = 'privacy', updated_at = ? WHERE status IN (?, ?)"
            " AND user_id IN (SELECT user_id FROM privacy_optout)", (L.WITHDRAWN, t, *L.PENDING))
        await self.db.execute(
            "UPDATE clipcast_prefs SET pref = ?, set_at = ? WHERE pref = ?"
            " AND user_id IN (SELECT user_id FROM privacy_optout)", (L.ASK, t, L.ALWAYS))
        if self.publishing.locked():
            return
        guild = self.guild()
        for r in await self.db.fetchall("SELECT * FROM clipcast WHERE status = ?", (L.UPLOADING,)):
            if await self.move(r["message_id"], [L.UPLOADING], L.UNSURE, "interrupted by a restart"):
                await self.alert_unsure(guild, r, "the bot stopped in the middle of the upload")

    async def alert_unsure(self, guild, row, why: str) -> None:
        await self.set_meta(ERROR_KEY, now(), f"upload result unknown: {why}")
        if guild is None:
            return
        await self.mod_log(guild, f"⚠️ Clipcast: I can't tell whether {self.jump_row(guild, row)} reached YouTube "
                                  f"({why}). It is never retried automatically: check YouTube Studio, then "
                                  f"`/clipcast skip` it.", ping_owner=True)

    # ------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload) -> None:
        try:
            await self.withdraw("message_id", [payload.message_id], "clip deleted")
        except Exception:
            log.exception("clipcast: delete handling failed")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload) -> None:
        try:
            await self.withdraw("message_id", list(payload.message_ids), "clip deleted")
        except Exception:
            log.exception("clipcast: bulk delete handling failed")

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload) -> None:
        try:
            if payload.guild_id == self.bot.settings.guild_id:
                await self.forget_member(payload.user.id, "left the server")
        except Exception:
            log.exception("clipcast: member-leave handling failed")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user) -> None:
        try:
            if self.ours(guild):
                await self.forget_member(user.id, "banned")
        except Exception:
            log.exception("clipcast: ban handling failed")

    # ------------------------------------------------------------ schedule
    @tasks.loop(minutes=5)
    async def ticker(self) -> None:
        await self.tick()

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self) -> None:
        for name, step in (("sweep", self.sweep), ("ask", self.run_ask), ("post", self.run_publish),
                           ("payout", self.run_payouts)):
            try:
                await step()
            except Exception:
                log.exception("clipcast: %s step failed", name)

    async def run_ask(self) -> None:
        await self.run_job(L.ASK_JOB, self.ask_week)

    async def run_publish(self) -> None:
        await self.run_job(L.PUBLISH_JOB, self.publish_week)

    async def run_job(self, job, work) -> None:
        # Marked done only when the work says the week is handled. No server or channel, or
        # a Discord error, leaves it for the next tick; the planner drops a week once the
        # next one is due. The clipcast rows keep a retry from asking or uploading twice.
        await jobs.run_due(self.db, job, self.tz, now, work)

    def wait(self, period, why: str) -> bool:
        """A week that can't be handled right now: logged once, not on every tick. Returns
        False, which is what tells `run_job` to leave the week for the next tick."""
        if period.key not in self.waiting:
            self.waiting.add(period.key)
            log.warning("clipcast %s: %s, trying again on the next tick", period.key, why)
        return False

    # ------------------------------------------------------------ looking at a clip
    async def clip_channel_id(self, message_id: int, default: int) -> int:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (thread_key(message_id),))
        return int(row["value"]) if row else default

    async def inspect(self, guild, channel_id: int, message_id: int):
        """The clip as it is now: a Seen, GONE (deleted) or UNKNOWN (couldn't look)."""
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return UNKNOWN
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return GONE
        except discord.HTTPException:
            log.warning("clipcast: couldn't fetch clip %s", message_id)
            return UNKNOWN
        attachments = list(getattr(message, "attachments", None) or [])
        facts = [(a.filename, a.content_type, a.size, a.url) for a in attachments]
        index = L.pick_attachment(facts)
        if index is not None:
            attachment, media = attachments[index], None
        else:
            attachment = None
            media = L.media_problem(*facts[0]) if facts else "not a Discord attachment"
        return Seen(message, channel, sum(r.count for r in message.reactions), attachment, media,
                    L.caption(message.content))

    async def member_state(self, guild, user_id: int) -> str:
        """'here', 'left', or 'unknown' when Discord wouldn't say."""
        if guild.get_member(user_id) is not None:
            return "here"
        try:
            await guild.fetch_member(user_id)
            return "here"
        except discord.NotFound:
            return "left"
        except discord.HTTPException:
            return "unknown"

    @staticmethod
    async def backers(message, author_id: int, at: int) -> int:
        """Established members other than the author who reacted to the clip."""
        seen: set[int] = set()
        try:
            for reaction in message.reactions:
                async for user in reaction.users():
                    if not getattr(user, "bot", False) and user.id != author_id and Q.established(user.id, at):
                        seen.add(user.id)
        except discord.HTTPException:
            log.warning("clipcast: couldn't read who reacted to %s", getattr(message, "id", "?"))
        return len(seen)

    # ------------------------------------------------------------ Sunday: shortlist and ask
    async def ask_week(self, period) -> bool:
        """True when the week's shortlist is handled (authors asked, or nothing to ask about);
        False when it couldn't be looked at right now. Nothing is written before the False."""
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.CLIPS_CHANNEL) if guild is not None else None
        if channel is None:
            return self.wait(period, "the server isn't available" if guild is None
                             else f"no {config.CLIPS_CHANNEL} channel, nothing shortlisted")
        t = now()
        week = clip_rules.week_of(period.key)
        await self.sweep()
        # Last week's unanswered questions close; a late answer could no longer be honoured.
        await self.db.execute("UPDATE clipcast SET status = ?, updated_at = ? WHERE status = ? AND week != ?",
                              (L.EXPIRED, t, L.ASKED, week))

        rows = await self.db.fetchall(
            "SELECT c.message_id, c.user_id, c.posted_at, c.url FROM clips c WHERE c.posted_at >= ?"
            " AND c.posted_at < ? AND c.message_id NOT IN (SELECT message_id FROM clipcast)"
            " ORDER BY c.posted_at, c.message_id", (period.window_start, period.window_end))
        prefs = {r["user_id"]: r for r in await self.db.fetchall("SELECT * FROM clipcast_prefs")}
        candidates, seen_by = [], {}
        for r in rows:
            mid, uid = r["message_id"], r["user_id"]
            if not L.discord_hosted(r["url"]):
                continue  # a link to another site: never ours to re-upload, so not worth a fetch
            seen = await self.inspect(guild, await self.clip_channel_id(mid, channel.id), mid)
            if not isinstance(seen, Seen):
                continue  # deleted (the clips cog forgets it) or unreadable right now
            seen_by[mid] = seen
            pref = prefs[uid]["pref"] if uid in prefs else L.ASK
            candidates.append(L.Candidate(
                mid, uid, r["posted_at"], seen.reactions, present=guild.get_member(uid) is not None,
                tracking=await self.db.tracking_allowed(uid), pref=pref, media=seen.media,
                caption_ok=L.text_ok(seen.caption)))

        asked = approved = 0
        for c in L.shortlist(candidates, L.TOP_N):
            seen = seen_by[c.message_id]
            member = guild.get_member(c.user_id)
            backers = await self.backers(seen.message, c.user_id, c.posted_at)
            name = L.credit_name(member.display_name)
            standing = c.pref == L.ALWAYS
            credit = None
            if standing:
                wants = prefs[c.user_id]["credit"]
                credit = L.NAME if wants == L.NAME and name else L.ANON
            # A standing yes is recorded as given by the author when they set the preference.
            inserted = await self.db.execute(
                "INSERT OR IGNORE INTO clipcast (message_id, user_id, channel_id, posted_at, reactions, backers,"
                " week, status, credit, credit_name, answered_by, answered_at, answered_via, asked_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c.message_id, c.user_id, seen.channel.id, c.posted_at, c.reactions, backers, week,
                 L.APPROVED if standing else L.ASKED, credit, name if credit == L.NAME else None,
                 c.user_id if standing else None, prefs[c.user_id]["set_at"] if standing else None,
                 L.VIA_STANDING if standing else None, None if standing else t, t))
            if not inserted:
                continue  # already on the list (a retried run)
            if standing:
                approved += 1
                continue
            via = await self.send_ask(guild, member, seen, c.message_id, name)
            await self.db.execute("UPDATE clipcast SET asked_via = ? WHERE message_id = ?", (via, c.message_id))
            asked += 1
        log.info("clipcast %s: asked %d author(s), %d approved by standing consent", week, asked, approved)
        return True

    async def send_ask(self, guild, member, seen: Seen, message_id: int, name: str | None) -> str:
        """DM the question, or reply under the clip when DMs are closed. Returns how it went."""
        link = self.jump(guild.id, seen.channel.id, message_id)
        body = ("OK to post it on the server's YouTube channel as a Short? Nothing leaves Discord unless you "
                "say yes, and you can change your answer until it's posted. `/clipcast consent` sets a standing "
                "answer; `/clipcast remove` takes a posted clip down again.")
        view = build_view(message_id, name)
        try:
            await member.send(f"🎬 Your clip made this week's shortlist in {esc(guild.name)}: {link}\n{body}",
                              view=view, allowed_mentions=NO_PINGS)
            return "dm"
        except discord.HTTPException:
            pass
        try:
            await seen.channel.send(f"🎬 <@{member.id}> your clip made this week's shortlist. {body}",
                                    view=view, reference=seen.message.to_reference(fail_if_not_exists=False),
                                    allowed_mentions=ping_only(users=[discord.Object(member.id)]))
            return "channel"
        except discord.HTTPException:
            log.warning("clipcast: couldn't ask %s about clip %s", member.id, message_id)
            return "unsent"

    # ------------------------------------------------------------ the consent buttons
    async def handle_button(self, interaction: discord.Interaction, choice: str, message_id: int) -> None:
        async def say(text: str) -> None:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

        user = interaction.user
        row = await self.row(message_id)
        if row is None:
            return await say("That clip isn't on the list any more.")
        if user.id != row["user_id"]:
            return await say("Only the member who posted that clip can answer this.")
        guild = self.guild()
        member = guild.get_member(user.id) if guild is not None else None
        if member is None:
            return await say("I can only take an answer from someone who's in the server.")
        if not await self.db.tracking_allowed(user.id):
            await self.withdraw("user_id", [user.id], "privacy")
            return await say(OPTED_OUT)
        moved = L.answer(row["status"], choice)
        if moved is None:
            return await say(f"This one is closed ({L.STATUS_WORDS.get(row['status'], row['status'])}).")
        status, credit = moved
        name = L.credit_name(member.display_name) if credit == L.NAME else None
        if credit == L.NAME and name is None:
            return await say("Your display name can't be used as a credit. Pick **Yes, anonymously** instead, "
                             "or change your server nickname and press the button again.")
        t = now()
        marks = ",".join("?" * len(L.ANSWERABLE))
        changed = await self.db.execute(
            "UPDATE clipcast SET status = ?, credit = ?, credit_name = ?, answered_by = ?, answered_at = ?,"
            f" answered_via = ?, note = NULL, updated_at = ? WHERE message_id = ? AND status IN ({marks})",
            (status, credit, name, user.id, t, L.VIA_BUTTON, t, message_id, *L.ANSWERABLE))
        if not changed:
            return await say("This one was just closed.")
        log.info("clipcast: %s answered %s for clip %s", user.id, choice, message_id)
        if status == L.DECLINED:
            return await say("No problem. It stays in Discord.")
        how = f"credited as **{esc(name)}**" if name else "with no name on it"
        await say(f"Thanks! It'll go up {how} on the next posting run. Changed your mind? Press **No thanks** "
                  "before then, or use `/clipcast remove` afterwards.")

    # ------------------------------------------------------------ Tuesday: post
    async def recheck(self, guild, row) -> Seen | None:
        """Everything consent rests on, looked at again right before posting. Returns the
        clip if it may still go out; otherwise the row is closed (or left for the next run
        when Discord wouldn't answer)."""
        mid = row["message_id"]
        state = await self.member_state(guild, row["user_id"])
        if state == "left":
            await self.forget_member(row["user_id"], "left the server")
            return None
        if state != "here":
            return None
        if not await self.db.tracking_allowed(row["user_id"]):
            await self.withdraw("user_id", [row["user_id"]], "privacy")
            return None
        seen = await self.inspect(guild, row["channel_id"], mid)
        if seen == GONE:
            await self.withdraw("message_id", [mid], "clip deleted")
            return None
        if not isinstance(seen, Seen):
            return None
        if seen.media is not None:
            await self.move(mid, [L.APPROVED], L.SKIPPED, f"clip changed: {seen.media}")
            return None
        if not L.text_ok(seen.caption):
            await self.move(mid, [L.APPROVED], L.SKIPPED, "caption failed the text filter")
            return None
        return seen

    async def publish_week(self, period) -> bool:
        """True when the week's run is handled: nothing approved, the picks listed for the
        owner, or the uploads tried (a clip that failed stays in line for next week's run and
        staff are told, so that is handled too). False when there is no server to look at, or
        the picks couldn't be listed: nothing has been uploaded or handed over by then."""
        async with self.publishing:
            guild = self.guild()
            if guild is None:
                return self.wait(period, "the server isn't available")
            t = now()
            await self.sweep()
            ready = {}
            for row in await self.db.fetchall("SELECT * FROM clipcast WHERE status = ? ORDER BY posted_at, message_id",
                                              (L.APPROVED,)):
                if not L.may_upload(row):
                    continue
                seen = await self.recheck(guild, row)
                if seen is not None:
                    ready[row["message_id"]] = (row, seen)
            pool = [clip_rules.Clip(mid, row["user_id"], row["posted_at"], seen.reactions)
                    for mid, (row, seen) in ready.items()]
            chosen = [ready[c.message_id] for c in L.top(pool, L.TOP_N)]
            if not chosen:
                await self.set_meta(RUN_KEY, t, "no approved clips")
                return True
            if not self.setup.configured:
                listed = await self.hand_over(guild, clip_rules.week_of(period.key), chosen, t)
                return listed or self.wait(period, f"couldn't list the picks in {config.MOD_LOG_CHANNEL}")

            self.clear_tmp()
            counts: dict[str, int] = {}
            for row, seen in chosen:
                outcome = await self.publish_one(guild, row["message_id"], seen)
                counts[outcome] = counts.get(outcome, 0) + 1
                if outcome in ("quota", "auth"):
                    break  # the rest wait for the next run rather than hammer a closed door
            summary = ", ".join(f"{n} {word}" for word, n in sorted(counts.items()))
            await self.set_meta(RUN_KEY, now(), summary)
            log.info("clipcast %s: %s", period.key, summary)
            if counts.get("quota") or counts.get("auth") or counts.get("failed"):
                error = await self.get_meta(ERROR_KEY)
                await self.mod_log(guild, f"⚠️ Clipcast: this week's run ended with {summary}. "
                                          f"Last error: {esc((error or {}).get('text', 'unknown'))}. "
                                          "Clips that failed cleanly are tried again next week; "
                                          "`/clipcast status` has the details.")
            return True

    async def hand_over(self, guild, week: str, chosen, t: int) -> bool:
        """No YouTube credentials: list the approved clips for the owner to post by hand.
        False when the list couldn't be posted; the clips are then still approved."""
        lines = ["YouTube isn't connected, so these are yours to post by hand. Each author said yes "
                 "to this clip; anything else stays in Discord.", ""]
        for n, (row, _) in enumerate(chosen, start=1):
            lines.append(f"`#{n}` {self.jump_row(guild, row)} · <@{row['user_id']}> · "
                         f"{esc(L.credit_label(row['credit'], row['credit_name']))} · "
                         f"yes <t:{row['answered_at']}:R>")
        lines += ["", "If an author later runs `/clipcast remove`, you'll be told here to take it down."]
        embed = style.embed(title=f"This week's clipcast picks · {week}", description="\n".join(lines),
                            footer=style.label("clipcast", "post by hand"))
        if not await self.mod_log(guild, embed=embed):
            await self.set_meta(RUN_KEY, t, "picks not listed: no mod log")
            return False  # still approved: listed on the next tick that can reach the mod log
        for row, _ in chosen:
            await self.move(row["message_id"], [L.APPROVED], L.HANDED)
        await self.set_meta(RUN_KEY, t, f"{len(chosen)} listed for posting by hand")
        return True

    def clear_tmp(self) -> None:
        """Remove files a crashed run left behind. The folder holds nothing else."""
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            for path in self.tmp_dir.glob("clipcast-*"):
                self.discard(path)
        except OSError:
            log.warning("clipcast: couldn't prepare %s", self.tmp_dir, exc_info=True)

    @staticmethod
    def discard(path: Path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.warning("clipcast: couldn't delete temp file %s", path)

    async def release(self, message_id: int, attempts: int, error: str, counted: bool = True) -> str:
        """Close a claimed upload that created nothing: back in line for the next run, parked
        after MAX_ATTEMPTS, or withdrawn if consent went away meanwhile."""
        t = now()
        late = message_id in self.late
        self.late.discard(message_id)
        status = L.WITHDRAWN if late else L.after_failure(attempts) if counted else L.APPROVED
        await self.db.execute(
            "UPDATE clipcast SET status = ?, attempts = ?, upload_started_at = NULL, note = ?, updated_at = ?"
            " WHERE message_id = ? AND status = ?", (status, attempts, error[:200], t, message_id, L.UPLOADING))
        await self.set_meta(ERROR_KEY, t, error)
        log.warning("clipcast: clip %s not posted: %s", message_id, error)
        return "withdrawn" if late else "failed"

    async def publish_one(self, guild, message_id: int, seen: Seen) -> str:
        """Upload one clip. Returns posted, failed, unsure, withdrawn, skipped, quota or auth."""
        row = await self.row(message_id)
        if row is None or not L.may_upload(row):
            return "skipped"
        t = now()
        attachment = seen.attachment
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="clipcast-", dir=self.tmp_dir,
                                        suffix=L.VIDEO_TYPES[L.base_type(attachment.content_type)])
            os.close(fd)
        except OSError as exc:
            await self.set_meta(ERROR_KEY, t, f"no temp file ({type(exc).__name__})")
            log.warning("clipcast: couldn't create a temp file in %s", self.tmp_dir, exc_info=True)
            return "failed"
        path = Path(name)
        # The claim repeats the consent check inside the UPDATE itself, so nothing without a
        # recorded yes from its own author can reach the network, and nothing is claimed twice.
        claimed = await self.db.execute(
            "UPDATE clipcast SET status = ?, attempts = attempts + 1, upload_started_at = ?, updated_at = ?"
            " WHERE message_id = ? AND status = ? AND credit IN (?, ?) AND answered_at IS NOT NULL"
            " AND answered_by = user_id AND video_id IS NULL",
            (L.UPLOADING, t, t, message_id, L.APPROVED, L.NAME, L.ANON))
        if not claimed:
            self.discard(path)
            return "skipped"
        attempts = row["attempts"] + 1
        try:
            try:
                status, content_type, size = await self.http.download(attachment.url, path, L.MAX_BYTES)
            except Exception as exc:
                return await self.release(message_id, attempts, f"download failed ({type(exc).__name__})")
            problem = L.download_problem(status, content_type, size)
            if problem:
                return await self.release(message_id, attempts, problem)
            credit = row["credit_name"] if row["credit"] == L.NAME else None
            resource = L.video_resource(L.video_title(seen.caption, guild.name),
                                        L.video_description(credit, guild.name), self.setup.privacy)
            try:
                vid = await self.youtube.upload(path, size, L.base_type(content_type), resource)
            except Quota as exc:
                await self.release(message_id, attempts - 1, str(exc), counted=False)
                return "quota"
            except AuthProblem as exc:
                await self.release(message_id, attempts - 1, str(exc), counted=False)
                return "auth"
            except Failed as exc:
                return await self.release(message_id, attempts, str(exc))
            except Exception as exc:  # Unsure, and anything unforeseen once bytes may have left
                why = str(exc) if isinstance(exc, Unsure) else type(exc).__name__
                if not isinstance(exc, Unsure):
                    log.exception("clipcast: upload of clip %s broke", message_id)
                await self.move(message_id, [L.UPLOADING], L.UNSURE, why[:200])
                await self.alert_unsure(guild, row, why)
                return "unsure"
        finally:
            self.discard(path)

        t = now()
        await self.db.execute(
            "UPDATE clipcast SET status = ?, video_id = ?, published_at = ?, note = NULL, updated_at = ?"
            " WHERE message_id = ? AND status = ?", (L.PUBLISHED, vid, t, t, message_id, L.UPLOADING))
        log.info("clipcast: clip %s posted as video %s (%s)", message_id, vid, self.setup.privacy)
        late = message_id in self.late
        self.late.discard(message_id)
        if late or await self.member_state(guild, row["user_id"]) == "left" \
                or not await self.db.tracking_allowed(row["user_id"]):
            # Consent went away while the file was in flight: it comes straight back down.
            await self.take_down(guild, await self.row(message_id), "consent was withdrawn during the upload")
            return "withdrawn"
        await self.celebrate(guild, row, seen, vid, t)
        return "posted"

    async def celebrate(self, guild, row, seen: Seen, vid: str, t: int) -> None:
        """Badge, the public "as seen on" reply and a mod-log line. No coins yet: those come
        from `run_payouts` once the clip has stayed up. The video id is already stored, so
        nothing here can cause a second upload."""
        uid, mid = row["user_id"], row["message_id"]
        url = L.watch_url(vid)
        coins_later = False  # only said when it would pay as things stand today
        try:
            coins_later = L.pays(uid, t, await self.db.tracking_allowed(uid), row["backers"])
        except Exception:
            log.exception("clipcast: checking the payout for clip %s failed", mid)
        badge = False
        try:
            if L.BADGE_KEY in A.BY_KEY:
                badge = bool(await self.db.execute(
                    "INSERT OR IGNORE INTO achievements (user_id, key, at) VALUES (?, ?, ?)", (uid, L.BADGE_KEY, t)))
        except Exception:
            log.exception("clipcast: badge for %s failed", uid)

        private = self.setup.privacy == "private"
        if not private:  # a private video's link would only show members an error page
            lines = [f"📺 As seen on the server's YouTube: {url}"]
            if badge:
                lines.append(f"<@{uid}> earned the {A.BY_KEY[L.BADGE_KEY].emoji} **{A.BY_KEY[L.BADGE_KEY].name}** "
                             "badge for it.")
            if coins_later:
                lines.append(("And " if badge else f"<@{uid}> ") + f"{L.COINS} coins follow once it has stayed up "
                             f"for {L.PAYOUT_DAYS} days.")
            text = lines[0] + ("\n" + " ".join(lines[1:]) if lines[1:] else "")
            try:
                notice = await seen.channel.send(text, reference=seen.message.to_reference(fail_if_not_exists=False),
                                                 allowed_mentions=ping_only(users=[discord.Object(uid)]))
                await self.db.execute("UPDATE clipcast SET notice_message_id = ? WHERE message_id = ?",
                                      (notice.id, mid))
            except discord.HTTPException:
                log.warning("clipcast: couldn't post the notice for clip %s", mid, exc_info=True)
        await self.mod_log(guild, f"📺 Clipcast: uploaded {self.jump_row(guild, row)} as **{self.setup.privacy}**: "
                                  f"{url} ({esc(L.credit_label(row['credit'], row['credit_name']))})."
                                  + (" Members aren't shown the link while it's private." if private else ""))

    # ------------------------------------------------------------ the delayed payout
    async def run_payouts(self) -> int:
        """Pay for clips that have stayed published for L.PAYOUT_DAYS. Everything it needs is
        in the database, so a restart in between changes nothing, and the ledger ref makes a
        clip pay once whatever happens. Only a row that is still PUBLISHED is looked at: one
        taken down, withdrawn or deleted in the meantime has left that status and never pays.
        The author has to still be here, established and allowing tracking, and the clip to
        have had a backer. Returns how many clips were paid."""
        guild = self.guild()
        if guild is None:
            return 0
        t = now()
        rows = await self.db.fetchall(
            "SELECT * FROM clipcast WHERE status = ? AND payout_at IS NULL ORDER BY published_at, message_id",
            (L.PUBLISHED,))
        paid = 0
        for row in rows:
            if not L.payout_due(row["published_at"], t):
                continue
            mid, uid = row["message_id"], row["user_id"]
            state = await self.member_state(guild, uid)
            if state == "unknown":
                continue  # Discord wouldn't say whether they're still here: ask again next time
            async with self.db.transaction() as tx:
                # Settled and paid together, and only if it is still published at this moment.
                if not (await tx.execute("UPDATE clipcast SET payout_at = ? WHERE message_id = ? AND status = ?"
                                         " AND payout_at IS NULL", (t, mid, L.PUBLISHED))).rowcount:
                    continue
                tracking = await tx.fetchone("SELECT 1 FROM privacy_optout WHERE user_id = ?", (uid,)) is None
                if state != "here" or not L.pays(uid, t, tracking, row["backers"]):
                    continue
                if (await economy.apply_tx(tx, uid, L.COINS, L.REASON, t, ref=L.coin_ref(mid))).ok:
                    paid += 1
                    log.info("clipcast: paid %d coins to %s for clip %s", L.COINS, uid, mid)
        return paid

    # ------------------------------------------------------------ takedown
    async def take_down(self, guild, row, why: str) -> str:
        """Bring a posted (or possibly posted) clip back. Returns the new status."""
        mid, vid = row["message_id"], row["video_id"]
        deleted, detail = False, ""
        if vid and self.setup.configured:
            deleted, detail = await self.youtube.delete(vid)
        status = L.REMOVED if deleted else L.REMOVAL
        await self.move(mid, list(L.OUTSIDE), status, why[:200])
        if row["notice_message_id"]:
            channel = guild.get_channel_or_thread(row["channel_id"])
            try:
                if channel is not None:
                    await channel.get_partial_message(row["notice_message_id"]).delete()
            except discord.HTTPException:
                pass
        if deleted:
            action = "I deleted the video from YouTube."
        elif vid:
            action = f"I couldn't delete it ({detail or 'YouTube is not connected'}): delete {L.watch_url(vid)} by hand."
        else:
            action = "I have no video id for it: if it was posted by hand, take it down."
        await self.mod_log(guild, f"🧹 Clipcast: {self.jump_row(guild, row)} by <@{row['user_id']}> has to come "
                                  f"down ({why}). {action}", ping_owner=not deleted)
        return status

    # ------------------------------------------------------------ member commands
    @clipcast.command(name="consent", description="Your standing answer for posting your clips on the server's YouTube")
    @app_commands.describe(answer="What Front Desk should do when one of your clips is shortlisted",
                           credit="With 'always': how you're credited (default: your display name)")
    @app_commands.choices(answer=PREF_CHOICES, credit=CREDIT_CHOICES)
    async def consent(self, interaction: discord.Interaction, answer: app_commands.Choice[str],
                      credit: app_commands.Choice[str] | None = None) -> None:
        uid, pref = interaction.user.id, answer.value
        t = now()
        if pref == L.ALWAYS and not await self.db.tracking_allowed(uid):
            await interaction.response.send_message(OPTED_OUT, ephemeral=True)
            return
        style_ = (credit.value if credit is not None else L.NAME) if pref == L.ALWAYS else None
        await self.db.execute("INSERT OR REPLACE INTO clipcast_prefs (user_id, pref, credit, set_at) VALUES (?, ?, ?, ?)",
                              (uid, pref, style_, t))
        if pref == L.NEVER:
            n = await self.db.execute(
                "UPDATE clipcast SET status = ?, credit = NULL, credit_name = NULL, answered_by = ?, answered_at = ?,"
                " answered_via = ?, updated_at = ? WHERE user_id = ? AND status IN (?, ?)",
                (L.DECLINED, uid, t, L.VIA_STANDING, t, uid, *L.PENDING))
            text = ("Done. Your clips are never posted outside Discord and I won't ask."
                    + (f" {n} clip(s) waiting were taken off the list." if n else "")
                    + " Something already posted? `/clipcast remove` takes it down.")
        elif pref == L.ALWAYS:
            how = "by your display name" if style_ == L.NAME else "anonymously"
            text = (f"Done. When one of your clips makes the weekly shortlist it's posted on the server's YouTube, "
                    f"credited {how}, without asking first. A clip you're being asked about right now still needs "
                    "its button. `/clipcast consent ask` or `never` changes this any time.")
        else:
            # "Ask me" also covers what the old standing yes already approved and has not been
            # posted yet: those go back to being a question instead of going up unasked.
            again = await self.db.fetchall(
                "SELECT message_id, channel_id FROM clipcast WHERE user_id = ? AND status = ? AND answered_via = ?",
                (uid, L.APPROVED, L.VIA_STANDING))
            reopened = []
            for row in again:
                if await self.db.execute(
                        "UPDATE clipcast SET status = ?, credit = NULL, credit_name = NULL, answered_by = NULL,"
                        " answered_at = NULL, answered_via = NULL, asked_at = ?, asked_via = NULL, updated_at = ?"
                        " WHERE message_id = ? AND status = ? AND answered_via = ?",
                        (L.ASKED, t, t, row["message_id"], L.APPROVED, L.VIA_STANDING)):
                    reopened.append(row)
            text = "Done. I'll ask you each time one of your clips makes the shortlist."
            if reopened:
                text += (f" {len(reopened)} clip(s) your standing yes had already approved won't be posted "
                         "unless you say yes to the question I'm sending you now.")
        await interaction.response.send_message(text, ephemeral=True)
        if pref == L.ASK:
            for row in reopened:
                try:
                    await self.ask_again(interaction.user, row["message_id"], row["channel_id"])
                except Exception:
                    log.exception("clipcast: re-asking about clip %s failed", row["message_id"])

    async def ask_again(self, member, message_id: int, channel_id: int) -> None:
        """Send the consent question for a clip that went back to "asked"."""
        guild = self.guild()
        seen = await self.inspect(guild, channel_id, message_id) if guild is not None else UNKNOWN
        via = "unsent"  # the clip can't be looked at right now: it simply stays unanswered
        if isinstance(seen, Seen):
            via = await self.send_ask(guild, member, seen, message_id, L.credit_name(member.display_name))
        await self.db.execute("UPDATE clipcast SET asked_via = ? WHERE message_id = ? AND status = ?",
                              (via, message_id, L.ASKED))

    @clipcast.command(name="remove", description="Take one of your clips off the server's YouTube (or off the list)")
    @app_commands.describe(clip="The clip's message link (pick from the list, or paste a link)")
    async def remove(self, interaction: discord.Interaction, clip: str) -> None:
        mid = L.parse_clip_ref(clip)
        row = await self.row(mid) if mid is not None else None
        if mid is None:
            await interaction.response.send_message(NOT_A_CLIP, ephemeral=True)
            return
        user = interaction.user
        if row is None or (row["user_id"] != user.id and not is_staff(user)):
            await interaction.response.send_message("That isn't one of your clips on the clipcast list.", ephemeral=True)
            return
        action = L.removal_action(row["status"])
        if action == "busy":
            self.late.add(mid)
            await interaction.response.send_message(
                "It's uploading this minute. I'll take it straight back down when the upload returns.", ephemeral=True)
            return
        if action == "withdraw":
            await self.withdraw("message_id", [mid], "author asked")
            await interaction.response.send_message("Done. It won't be posted.", ephemeral=True)
            return
        if action != "takedown":
            await interaction.response.send_message(
                f"Nothing to take down there ({L.STATUS_WORDS.get(row['status'], row['status'])}).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        status = await self.take_down(interaction.guild, row, f"requested by <@{user.id}>")
        await interaction.followup.send(
            "Done. The video is deleted from YouTube." if status == L.REMOVED else
            "Got it. I couldn't delete it myself, so the owner has been told to take it down by hand.", ephemeral=True)

    @remove.autocomplete("clip")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await self.db.fetchall(
            "SELECT message_id, posted_at, status FROM clipcast WHERE user_id = ? ORDER BY posted_at DESC LIMIT 25",
            (interaction.user.id,))
        out = []
        for r in rows:
            if L.removal_action(r["status"]) is None:
                continue
            day = time.strftime("%b %d", time.gmtime(r["posted_at"]))
            name = f"Clip from {day} · {L.STATUS_WORDS[r['status']]}"
            if current.lower() in name.lower() or current in str(r["message_id"]):
                out.append(app_commands.Choice(name=name[:100], value=str(r["message_id"])))
        return out

    # ------------------------------------------------------------ staff commands
    @clipcast.command(name="status", description="Clipcast setup, queue and last run (staff only)")
    async def status(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        guild = interaction.guild
        if self.setup.configured:
            connection = f"connected; uploads are **{self.setup.privacy}**"
        else:
            connection = ("not set up (missing " + ", ".join(f"`{n}`" for n in self.setup.missing)
                          + "); approved clips are listed in the mod log to post by hand")
        open_rows = await self.db.fetchall(
            "SELECT * FROM clipcast WHERE status IN (?, ?, ?, ?, ?, ?) ORDER BY posted_at, message_id",
            (L.ASKED, L.APPROVED, L.UPLOADING, *L.ATTENTION))
        posted = await self.db.fetchone("SELECT COUNT(*) AS n FROM clipcast WHERE status = ?", (L.PUBLISHED,))
        run, error = await self.get_meta(RUN_KEY), await self.get_meta(ERROR_KEY)
        lines = [f"**YouTube:** {connection}",
                 f"**Queue:** {L.queue_line(L.queue_counts(r['status'] for r in open_rows))}",
                 f"**Posted so far:** {posted['n']}",
                 "**Last run:** " + (f"<t:{int(run['at'])}:R>, {esc(str(run['text']))}" if run else "never"),
                 "**Last error:** " + (f"<t:{int(error['at'])}:R>, {esc(str(error['text']))}" if error else "none"),
                 "**Schedule:** authors are asked Sundays 18:30, clips are posted Tuesdays 18:30."]
        for r in open_rows[:10]:
            note = f" ({esc(r['note'])})" if r["note"] and r["status"] in L.ATTENTION else ""
            lines.append(f"• {self.jump_row(guild, r)} · <@{r['user_id']}> · {L.STATUS_WORDS[r['status']]}{note}")
        embed = style.embed(title="Clipcast", description="\n".join(lines)[:4000],
                            footer=style.label("clipcast", "staff"))
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @clipcast.command(name="skip", description="Keep a clip off the server's YouTube (staff only)")
    @app_commands.describe(clip="The clip's message link")
    async def skip(self, interaction: discord.Interaction, clip: str) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        mid = L.parse_clip_ref(clip)
        if mid is None:
            await interaction.response.send_message(NOT_A_CLIP, ephemeral=True)
            return
        t = now()
        note = f"skipped by {interaction.user.id}"
        row = await self.row(mid)
        if row is None:
            # Not shortlisted yet: a skipped row keeps it from ever being picked.
            clip_row = await self.db.fetchone("SELECT * FROM clips WHERE message_id = ?", (mid,))
            if clip_row is None:
                await interaction.response.send_message("That message isn't a clip I know about.", ephemeral=True)
                return
            channel = config.match_by_name(interaction.guild.text_channels, config.CLIPS_CHANNEL)
            await self.db.execute(
                "INSERT OR IGNORE INTO clipcast (message_id, user_id, channel_id, posted_at, week, status, note,"
                " updated_at) VALUES (?, ?, ?, ?, '', ?, ?, ?)",
                (mid, clip_row["user_id"], await self.clip_channel_id(mid, channel.id if channel else 0),
                 clip_row["posted_at"], L.SKIPPED, note, t))
            await interaction.response.send_message("Skipped. It won't be shortlisted.", ephemeral=True)
            return
        if row["status"] == L.UPLOADING:
            await interaction.response.send_message("It's uploading this minute. Try again shortly.", ephemeral=True)
            return
        if not await self.move(mid, list(L.SKIPPABLE), L.SKIPPED, note):
            await interaction.response.send_message(
                f"Nothing to skip: it's {L.STATUS_WORDS.get(row['status'], row['status'])}."
                + (" Use `/clipcast remove` to take a posted clip down." if row["status"] in L.OUTSIDE else ""),
                ephemeral=True)
            return
        await interaction.response.send_message("Skipped. It won't be posted.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Clipcast(bot))
