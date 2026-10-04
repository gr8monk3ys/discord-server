from types import SimpleNamespace

from logic import starboard as S

AUTHOR = 1
DAY = 24 * 60 * 60
T0 = 1_790_000_000


def u(uid, bot=False):
    return SimpleNamespace(id=uid, bot=bot)


# ---------------------------------------------------------------- star emoji
def test_is_star_accepts_the_unicode_star_in_all_shapes():
    assert S.is_star("⭐")
    assert S.is_star("⭐️")
    assert S.is_star(SimpleNamespace(name="⭐", id=None))


def test_is_star_rejects_other_and_custom_emoji():
    assert not S.is_star("🌟")
    assert not S.is_star(SimpleNamespace(name="⭐", id=1234))  # a custom emoji named like it
    assert not S.is_star(SimpleNamespace(name=None, id=None))
    assert not S.is_star(None)


# ---------------------------------------------------------------- counting
def test_count_distinct_non_bot_non_author():
    reactors = [u(2), u(3), u(3), u(AUTHOR), u(50, bot=True), u(4)]
    assert S.count_stars(reactors, AUTHOR) == 3


def test_author_self_star_does_not_count():
    assert S.count_stars([u(AUTHOR), u(2), u(3)], AUTHOR) == 2


def test_bots_do_not_count():
    assert S.count_stars([u(50, bot=True), u(51, bot=True), u(2)], AUTHOR) == 1


def test_no_reactors_is_zero():
    assert S.count_stars([], AUTHOR) == 0


def test_threshold_is_three():
    assert S.THRESHOLD == 3
    assert not S.reaches(2)
    assert S.reaches(3) and S.reaches(10)


# ---------------------------------------------------------------- what to do
def test_plan_posts_when_reaching_threshold_without_a_post():
    assert S.plan(None, 3) is S.Action.POST
    assert S.plan(None, 5) is S.Action.POST


def test_plan_nothing_below_threshold_without_a_post():
    assert S.plan(None, 2) is S.Action.NONE
    assert S.plan(None, 0) is S.Action.NONE


def test_plan_updates_existing_post_when_count_changes_even_below_threshold():
    assert S.plan(S.Board(board_message_id=77, stars=3), 4) is S.Action.UPDATE
    assert S.plan(S.Board(board_message_id=77, stars=3), 1) is S.Action.UPDATE
    assert S.plan(S.Board(board_message_id=77, stars=3), 0) is S.Action.UPDATE


def test_plan_nothing_when_count_unchanged():
    assert S.plan(S.Board(board_message_id=77, stars=4), 4) is S.Action.NONE


def test_plan_never_reposts_a_claimed_but_unfinished_post():
    assert S.plan(S.Board(board_message_id=S.PENDING, stars=3), 5) is S.Action.NONE


# ---------------------------------------------------------------- skip rules
def skip(**kw):
    args = dict(in_hall=False, in_staff=False, nsfw=False)
    args.update(kw)
    return S.skip_channel(**args)


def test_normal_channel_is_not_skipped():
    assert not skip()


def test_hall_staff_and_nsfw_channels_are_skipped():
    assert skip(in_hall=True)
    assert skip(in_staff=True)
    assert skip(nsfw=True)


def test_too_old_after_14_days():
    assert S.MAX_AGE == 14 * DAY
    assert not S.too_old(T0 - 14 * DAY + 1, T0)
    assert not S.too_old(T0 - 14 * DAY, T0)
    assert S.too_old(T0 - 14 * DAY - 1, T0)
    assert not S.too_old(T0 + 5, T0)  # clock skew: a message "from the future" is fine


# ---------------------------------------------------------------- text
def test_text_kept_when_short():
    assert S.text("hello there") == "hello there"


def test_text_truncated_to_1000_chars_with_ellipsis():
    out = S.text("x" * 1500)
    assert len(out) == S.TEXT_LIMIT == 1000
    assert out.endswith("…")
    assert S.text("y" * 1000) == "y" * 1000


def test_text_strips_and_handles_empty():
    assert S.text("  hi  ") == "hi"
    assert S.text("") is None
    assert S.text("   \n ") is None
    assert S.text(None) is None


# ---------------------------------------------------------------- images
def att(filename, content_type=None, url=None):
    return SimpleNamespace(filename=filename, content_type=content_type, url=url or f"https://cdn/{filename}")


def emb(type_="rich", url=None, image=None, thumbnail=None):
    return SimpleNamespace(type=type_, url=url,
                           image=SimpleNamespace(url=image), thumbnail=SimpleNamespace(url=thumbnail))


def test_first_image_attachment_wins():
    atts = [att("notes.txt", "text/plain"), att("a.png", "image/png"), att("b.jpg", "image/jpeg")]
    assert S.pick_image(atts, [emb("image", url="https://e/x.gif")]) == "https://cdn/a.png"


def test_attachment_without_content_type_uses_extension():
    assert S.pick_image([att("CLIP.JPEG")], []) == "https://cdn/CLIP.JPEG"
    assert S.pick_image([att("video.mp4"), att("doc.pdf")], []) is None


def test_image_embed_used_when_no_image_attachment():
    assert S.pick_image([att("a.zip", "application/zip")],
                        [emb("rich"), emb("image", url="https://e/cat.png", thumbnail="https://e/t.png")]
                        ) == "https://e/cat.png"


def test_image_embed_falls_back_to_thumbnail_and_rich_embed_image():
    assert S.pick_image([], [emb("image", url=None, thumbnail="https://e/t.png")]) == "https://e/t.png"
    assert S.pick_image([], [emb("rich", image="https://e/big.png")]) == "https://e/big.png"


def test_gifv_counts_but_link_embeds_without_images_do_not():
    assert S.pick_image([], [emb("gifv", url="https://tenor/x", thumbnail="https://tenor/x.png")]) == "https://tenor/x.png"
    assert S.pick_image([], [emb("link", url="https://example.com")]) is None


def test_no_attachments_no_embeds():
    assert S.pick_image([], []) is None


# ---------------------------------------------------------------- footer
def test_footer_shows_channel_and_count():
    assert S.footer("general", 4) == "#general · ⭐ 4"
