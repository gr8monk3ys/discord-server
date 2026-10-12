"""Rules for clipcast (logic/clipcast.py): the consent state machine, what may be uploaded,
selection, member text, and reading YouTube's replies. Pure, no I/O."""

import json

import pytest

from logic import clipcast as L
from logic import helpdesk
from logic import quests as Q
from logic.clips import Clip

DAY = 24 * 60 * 60
T = 1_790_000_000  # autumn 2026
OLD_A, OLD_B, OLD_C, OLD_D = 1, 2, 3, 4  # snowflakes from 2015: long-established accounts
EVERY_STATUS = (L.ASKED, L.APPROVED, L.DECLINED, L.WITHDRAWN, L.EXPIRED, L.SKIPPED, L.UPLOADING, L.PUBLISHED,
                L.HANDED, L.UNSURE, L.FAILED, L.REMOVED, L.REMOVAL)


def young(seq: int = 0) -> int:
    """A snowflake for an account made ten days before T."""
    return (((T - 10 * DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22) + seq


def body(obj) -> bytes:
    return json.dumps(obj).encode()


# ---------------------------------------------------------------- consent state machine
def test_every_status_has_words():
    assert set(L.STATUS_WORDS) == set(EVERY_STATUS)


@pytest.mark.parametrize("status", [L.ASKED, L.APPROVED, L.DECLINED])
def test_an_open_question_takes_any_answer(status):
    assert L.answer(status, L.NAME) == (L.APPROVED, L.NAME)
    assert L.answer(status, L.ANON) == (L.APPROVED, L.ANON)
    assert L.answer(status, L.NO) == (L.DECLINED, None)


@pytest.mark.parametrize("status", [s for s in EVERY_STATUS if s not in (L.ASKED, L.APPROVED, L.DECLINED)])
def test_a_closed_clip_takes_no_answer(status):
    for choice in L.CHOICES:
        assert L.answer(status, choice) is None


def test_unknown_answers_are_refused():
    assert L.answer(L.ASKED, "yes please") is None
    assert L.answer(L.ASKED, "") is None


def row(**kw):
    base = dict(status=L.APPROVED, credit=L.NAME, answered_at=T, answered_by=OLD_A, user_id=OLD_A, video_id=None)
    return {**base, **kw}


def test_only_a_recorded_yes_from_the_author_may_upload():
    assert L.may_upload(row())
    assert L.may_upload(row(credit=L.ANON))
    for status in EVERY_STATUS:
        assert L.may_upload(row(status=status)) is (status == L.APPROVED), status
    assert not L.may_upload(row(credit=None))
    assert not L.may_upload(row(credit="yes"))
    assert not L.may_upload(row(answered_at=None))  # no time on record
    assert not L.may_upload(row(answered_by=None))  # nobody on record
    assert not L.may_upload(row(answered_by=OLD_B))  # someone else's yes
    assert not L.may_upload(row(video_id="abc12345678"))  # already up


def test_removal_action_per_status():
    want = {L.ASKED: "withdraw", L.APPROVED: "withdraw", L.PUBLISHED: "takedown", L.HANDED: "takedown",
            L.UNSURE: "takedown", L.REMOVAL: "takedown", L.UPLOADING: "busy"}
    for status in EVERY_STATUS:
        assert L.removal_action(status) == want.get(status), status


def test_failures_retry_then_park():
    assert [L.after_failure(n) for n in (1, 2, 3, 4)] == [L.APPROVED, L.APPROVED, L.FAILED, L.FAILED]
    assert L.MAX_ATTEMPTS == 3


def test_status_groups_are_consistent():
    assert set(L.PENDING) <= set(L.ANSWERABLE)
    assert L.UPLOADING not in L.SKIPPABLE and L.PUBLISHED not in L.SKIPPABLE
    assert not set(L.PENDING) & set(L.OUTSIDE)


# ---------------------------------------------------------------- media
CDN = "https://cdn.discordapp.com/attachments/1/2/clip.mp4?ex=1&is=2&hm=3"


def test_only_discord_hosted_links_count():
    assert L.discord_hosted(CDN)
    assert L.discord_hosted("https://media.discordapp.net/attachments/1/2/clip.mp4")
    for url in ("http://cdn.discordapp.com/a.mp4", "https://medal.tv/games/x/clips/abc", "https://youtu.be/abc",
                "https://clips.twitch.tv/Funny", "https://cdn.discordapp.com.evil.example/a.mp4",
                "https://evil.example/cdn.discordapp.com/a.mp4", "", None, "https://[bad"):
        assert not L.discord_hosted(url), url


def test_media_problem():
    assert L.media_problem("clip.mp4", "video/mp4", 5_000_000, CDN) is None
    assert L.media_problem("CLIP.MOV", "video/quicktime", 1, CDN) is None
    assert L.media_problem("clip.webm", "video/webm; codecs=vp9", 1, CDN) is None
    assert L.media_problem("clip.mp4", "video/mp4", 1, "https://medal.tv/x.mp4") == "not a Discord attachment"
    assert L.media_problem("clip.mkv", "video/x-matroska", 1, CDN) == "not an mp4, webm or mov video"
    assert L.media_problem("clip.exe", "video/mp4", 1, CDN) == "not an mp4, webm or mov video"  # type and name agree
    assert L.media_problem("pic.png", "image/png", 1, CDN) == "not an mp4, webm or mov video"
    assert L.media_problem("clip.mp4", None, 1, CDN) == "not an mp4, webm or mov video"
    assert L.media_problem("clip.mp4", "video/mp4", None, CDN) == "unknown size"
    assert L.media_problem("clip.mp4", "video/mp4", 0, CDN) == "unknown size"
    assert L.media_problem("clip.mp4", "video/mp4", L.MAX_BYTES, CDN) is None
    assert L.media_problem("clip.mp4", "video/mp4", L.MAX_BYTES + 1, CDN) == "larger than the upload limit"


def test_pick_attachment_takes_the_first_uploadable():
    atts = [("pic.png", "image/png", 10, CDN), ("a.mp4", "video/mp4", 10, CDN), ("b.mp4", "video/mp4", 10, CDN)]
    assert L.pick_attachment(atts) == 1
    assert L.pick_attachment(atts[:1]) is None
    assert L.pick_attachment([]) is None


def test_download_problem_checks_what_actually_arrived():
    assert L.download_problem(200, "video/mp4", 10) is None
    assert L.download_problem(200, "Video/MP4; x=y", 10) is None
    assert L.download_problem(404, "video/mp4", 0) == "download failed (HTTP 404)"
    assert L.download_problem(200, "text/html", 10) == "download was not a video"
    assert L.download_problem(200, "video/mp4", 0) == "download was empty"
    assert L.download_problem(200, "video/mp4", L.MAX_BYTES + 1) == "larger than the upload limit"


# ---------------------------------------------------------------- member text
def test_caption_drops_links_mentions_and_markup():
    assert L.caption("insane 1v4 https://cdn.discordapp.com/x.mp4 <https://youtu.be/abc>") == "insane 1v4"
    assert L.caption("gg <@123456789012345678> in <#123456789012345678> <:pog:123456789012345678>") == "gg in"
    assert L.caption("<script>alert(1)</script>") == "scriptalert(1)/script"
    assert L.caption("line one\nline\ttwo\x00\x1b[0m") == "line one line two [0m"
    assert L.caption(None) == "" and L.caption("   ") == ""
    assert len(L.caption("x" * 500)) == L.CAPTION_LEN


def test_text_ok_uses_the_filter_with_links_refused():
    assert L.text_ok("clean clutch") and L.text_ok("")
    assert not L.text_ok("join discord.gg/abcdef")
    assert not L.text_ok("more at example.com")
    assert not L.text_ok("what a r3tard")


def test_credit_name():
    assert L.credit_name("  Sam  the\nGreat ") == "Sam the Great"
    assert L.credit_name("<b>Sam</b>") == "bSam/b"
    assert L.credit_name("") is None and L.credit_name(None) is None and L.credit_name("<>") is None
    assert L.credit_name("visit example.com") is None
    assert L.credit_name("faggot") is None
    assert len(L.credit_name("n" * 200)) == L.NAME_LEN


def test_video_title_is_templated_and_fits():
    assert L.video_title("insane 1v4", "Front Porch") == "insane 1v4 | Clip of the week from Front Porch #Shorts"
    assert L.video_title("", "Front Porch") == "Clip of the week from Front Porch #Shorts"
    long = L.video_title("y" * 300, "S" * 300)
    assert len(long) <= L.YT_TITLE_MAX and long.endswith("#Shorts")
    assert "<" not in L.video_title("<3 this", "A <B> C") and ">" not in L.video_title("<3 this", "A <B> C")


def test_video_description_links_back_to_the_site():
    assert L.JOIN_URL == f"{helpdesk.SITE_URL}/join"
    named = L.video_description("Sam", "Front Porch")
    assert named.startswith("Clip by Sam, posted with their permission.")
    assert L.JOIN_URL in named and "#Shorts" in named and "Front Porch" in named
    anon = L.video_description(None, "Front Porch")
    assert anon.startswith("Clip by one of our members, posted with their permission.")
    assert "discord.gg" not in named  # the invite itself is never hard-coded: the site redirects
    assert len(L.video_description("n" * 9999, "s" * 9999)) <= L.YT_DESCRIPTION_MAX


# ---------------------------------------------------------------- selection
def cand(mid, uid, reactions=0, posted=None, **kw):
    return L.Candidate(mid, uid, T + mid if posted is None else posted, reactions, **kw)


def test_why_not():
    assert L.why_not(cand(1, OLD_A)) is None
    assert L.why_not(cand(1, OLD_A, present=False)) == "left"
    assert L.why_not(cand(1, OLD_A, tracking=False)) == "privacy"
    assert L.why_not(cand(1, OLD_A, pref=L.NEVER)) == "never"
    assert L.why_not(cand(1, OLD_A, pref=L.ALWAYS)) is None
    assert L.why_not(cand(1, young(), posted=T)) == "new account"
    assert L.why_not(cand(1, OLD_A, media="larger than the upload limit")) == "larger than the upload limit"
    assert L.why_not(cand(1, OLD_A, caption_ok=False)) == "caption"


def test_top_uses_the_clip_poll_ranking_one_per_author():
    pool = [Clip(10, OLD_A, T + 10, 1), Clip(11, OLD_A, T + 11, 9), Clip(12, OLD_B, T + 12, 5),
            Clip(13, OLD_C, T + 13, 5), Clip(14, OLD_D, T + 14, 0)]
    # A's best (9), then the two 5s with the tie to the earlier post; D's 0 misses out.
    assert {c.message_id for c in L.top(pool, 3)} == {11, 12, 13}
    assert {c.message_id for c in L.top(pool, 2)} == {11, 12}
    assert L.top([], 3) == []
    assert [c.message_id for c in L.top(pool[:2], 3)] == [11]  # one author, one slot


def test_shortlist_filters_before_ranking():
    cands = [cand(10, OLD_A, 50, tracking=False), cand(11, OLD_B, 40, pref=L.NEVER),
             cand(12, OLD_C, 30, media="not an mp4, webm or mov video"), cand(13, young(1), 20, posted=T),
             cand(14, OLD_D, 3), cand(15, OLD_A, 2, caption_ok=False), cand(16, OLD_B, 1, present=False),
             cand(17, 5, 1), cand(18, 6, 0)]
    assert {c.message_id for c in L.shortlist(cands, 3)} == {14, 17, 18}
    assert L.shortlist(cands[:4], 3) == []
    assert L.TOP_N == 3


def test_pays_only_established_tracked_authors_with_a_backer():
    assert L.pays(OLD_A, T, True, 1)
    assert not L.pays(OLD_A, T, True, 0)  # nobody else reacted: can't be farmed alone
    assert not L.pays(OLD_A, T, False, 3)
    assert not L.pays(young(), T, True, 3)
    assert L.coin_ref(123) == "clipcast:123" and L.COINS == 100 and L.REASON == "clipcast"


def test_parse_clip_ref():
    mid = 1234567890123456789
    assert L.parse_clip_ref(str(mid)) == mid
    assert L.parse_clip_ref(f" https://discord.com/channels/111/222/{mid} ") == mid
    assert L.parse_clip_ref(f"https://ptb.discord.com/channels/111/222/{mid}?x=1") == mid
    for bad in ("", None, "12", "abc", "1234567890123456789; DROP TABLE clips", "https://youtu.be/1234567890123456789"):
        assert L.parse_clip_ref(bad) is None, bad


def test_jobs():
    assert (L.ASK_JOB.name, L.ASK_JOB.weekday, L.ASK_JOB.hour, L.ASK_JOB.minute) == ("clipcast-ask", 6, 18, 30)
    assert (L.PUBLISH_JOB.name, L.PUBLISH_JOB.weekday, L.PUBLISH_JOB.hour) == ("clipcast", 1, 18)
    # `jobs` rows are found with LIKE 'name:%': neither name may be a prefix of the other's keys.
    assert not f"{L.ASK_JOB.name}:".startswith(f"{L.PUBLISH_JOB.name}:")


# ---------------------------------------------------------------- settings
FULL = {L.ENV_ID: " id ", L.ENV_SECRET: "sec-ret", L.ENV_REFRESH: "ref-resh"}


def test_read_setup():
    s = L.read_setup(FULL)
    assert s.configured and s.missing == [] and s.privacy == "unlisted"
    assert (s.client_id, s.client_secret, s.refresh_token) == ("id", "sec-ret", "ref-resh")
    assert L.read_setup({**FULL, L.ENV_PRIVACY: "PUBLIC"}).privacy == "public"
    assert L.read_setup({**FULL, L.ENV_PRIVACY: "private"}).privacy == "private"
    assert L.read_setup({**FULL, L.ENV_PRIVACY: "everyone"}).privacy == "unlisted"
    empty = L.read_setup({})
    assert not empty.configured and empty.missing == [L.ENV_ID, L.ENV_SECRET, L.ENV_REFRESH]
    partial = L.read_setup({L.ENV_ID: "id", L.ENV_SECRET: "  "})
    assert not partial.configured and partial.missing == [L.ENV_SECRET, L.ENV_REFRESH]


def test_setup_never_prints_its_secrets():
    s = L.read_setup(FULL)
    for text in (repr(s), str(s), f"{s}", str([s])):
        assert "sec-ret" not in text and "ref-resh" not in text
    assert L.token_form(s) == {"client_id": "id", "client_secret": "sec-ret", "refresh_token": "ref-resh",
                               "grant_type": "refresh_token"}


# ---------------------------------------------------------------- YouTube replies
def test_parse_token():
    assert L.parse_token(body({"access_token": "abc", "expires_in": 3600}), T) == ("abc", T + 3600 - L.TOKEN_MARGIN)
    assert L.parse_token(body({"access_token": "abc"}), T) == ("abc", T)  # no lifetime: refresh next time
    assert L.parse_token(body({"access_token": "abc", "expires_in": "soon"}), T) == ("abc", T)
    for bad in (b"", b"not json", body([]), body({"access_token": ""}), body({"access_token": 5}), b"\xff\xfe"):
        assert L.parse_token(bad, T) is None


def google_error(reason, code=403):
    return body({"error": {"code": code, "message": "secret-looking text Bearer ya29.abc",
                           "errors": [{"reason": reason, "domain": "youtube.quota"}]}})


def test_error_reason_keeps_only_an_identifier():
    assert L.error_reason(google_error("quotaExceeded")) == "quotaExceeded"
    assert L.error_reason(body({"error": "invalid_grant", "error_description": "Token has been revoked."})) == "invalid_grant"
    assert L.error_reason(body({"error": {"status": "PERMISSION_DENIED"}})) == "PERMISSION_DENIED"
    for hostile in (google_error("<@everyone> click http://evil.example"), google_error("x" * 500),
                    google_error({"a": 1}), google_error(None), b"", b"<html>", body({"error": []}), body("hi"),
                    body({"error": {"errors": "nope"}})):
        assert L.error_reason(hostile) == ""


def test_is_quota():
    assert L.is_quota(403, google_error("quotaExceeded"))
    assert L.is_quota(403, google_error("rateLimitExceeded"))
    assert L.is_quota(400, google_error("uploadLimitExceeded", 400))
    assert L.is_quota(429, b"")
    assert not L.is_quota(403, google_error("forbidden"))
    assert not L.is_quota(500, google_error("quotaExceeded"))
    assert not L.is_quota(200, b"{}")


def test_describe_never_carries_the_reply_text():
    text = L.describe("upload start", 403, google_error("forbidden"))
    assert text == "upload start: HTTP 403 (forbidden)"
    assert "ya29" not in text and "secret" not in text
    assert L.describe("token", 500) == "token: HTTP 500"
    assert L.describe("token", 400, b"garbage") == "token: HTTP 400"


def test_video_resource():
    r = L.video_resource("t", "d", "public")
    assert r == {"snippet": {"title": "t", "description": "d", "categoryId": "20"},
                 "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    assert L.video_resource("t", "d", "world")["status"]["privacyStatus"] == "unlisted"


def test_safe_upload_url():
    good = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=xyz"
    assert L.safe_upload_url(good)
    assert L.safe_upload_url(good.replace("googleapis.com/", "googleapis.com:443/"))
    for bad in ("http://www.googleapis.com/upload/youtube/v3/videos?upload_id=x",
                "https://evil.example/upload/youtube/v3/videos?upload_id=x",
                "https://www.googleapis.com.evil.example/upload/youtube/v3/videos",
                "https://user@www.googleapis.com/upload/youtube/v3/videos",
                "https://www.googleapis.com:8443/upload/youtube/v3/videos",
                "https://www.googleapis.com/somewhere/else", "//www.googleapis.com/upload/youtube/v3/videos",
                "", None, "https://[bad"):
        assert not L.safe_upload_url(bad), bad


def test_video_id_and_watch_url():
    assert L.video_id(body({"id": "dQw4w9WgXcQ", "kind": "youtube#video"})) == "dQw4w9WgXcQ"
    for bad in (body({"id": "abc"}), body({"id": "a b c d e f"}), body({"id": "x" * 40}), body({"id": 12345678}),
                body({"id": "../../etc/passwd"}), body({}), body([]), b"", None):
        assert L.video_id(bad) is None
    assert L.watch_url("dQw4w9WgXcQ") == "https://youtu.be/dQw4w9WgXcQ"


# ---------------------------------------------------------------- staff text
def test_queue_line_and_credit_label():
    counts = L.queue_counts([L.ASKED, L.ASKED, L.APPROVED, L.UNSURE, L.PUBLISHED])
    assert counts == {L.ASKED: 2, L.APPROVED: 1, L.UNSURE: 1, L.PUBLISHED: 1}
    assert L.queue_line(counts) == ("2 waiting for an answer; 1 approved, waiting to be posted; "
                                    "1 upload result unknown")
    assert L.queue_line({}) == "empty"
    assert L.credit_label(L.NAME, "Sam") == "credited as Sam"
    assert L.credit_label(L.ANON, None) == "anonymous" and L.credit_label(L.NAME, None) == "anonymous"
