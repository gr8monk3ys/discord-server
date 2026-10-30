"""logic/referral.py: codes, reading a Tebex payment, the decision and the unlock's commands; plus the
bridge's narrow allow-list for those commands (logic/monkey.py `grant_sendable`)."""

import itertools

import pytest

from logic import monkey as R
from logic import referral as L

ON = {"REFERRALS": "on"}


def esc(text):
    return text.replace("_", "\\_").replace("*", "\\*")


def settings(**env):
    s, problems = L.read_settings({**ON, **env})
    assert problems == ()
    return s


def payment(code="MKABCDEF", buyer="NewKid", products=None, creator=None, tx="tbx-1"):
    products = products if products is not None else [
        {"id": 7, "name": "Server Pass", "variables": [{"identifier": "referral", "option": code}],
         "username": {"id": "u", "username": buyer}}]
    return {"transaction_id": tx, "creator_code": creator,
            "customer": {"username": {"id": "u", "username": buyer}}, "products": products}


# ---------------------------------------------------------------- settings
def test_off_by_default_and_on_with_defaults():
    assert L.read_settings({}) == (None, ())
    assert L.read_settings({"REFERRALS": "off"}) == (None, ())
    assert settings() == L.Settings()
    assert L.Settings().grant == L.GRANT_STAFF and not L.Settings().creator_codes


def test_settings_problems_name_the_variable_never_the_value():
    s, problems = L.read_settings({**ON, "REFERRAL_VARIABLE": "bad id!", "REFERRAL_GRANT": "console"})
    assert s.variable == L.DEFAULT_VARIABLE and s.grant == L.GRANT_STAFF
    assert len(problems) == 2 and all("bad id" not in p and "console" not in p for p in problems)
    s = settings(REFERRAL_VARIABLE="ref_code", REFERRAL_CREATOR_CODES="yes", REFERRAL_GRANT="RCON",
                 REFERRAL_PACKAGES=" Server  Pass , 12 ,")
    assert (s.variable, s.creator_codes, s.grant, s.packages) == ("ref_code", True, "rcon", ("server pass", "12"))


# ---------------------------------------------------------------- codes
def test_codes_have_a_prefix_and_no_lookalike_letters():
    code = L.new_code()
    assert L.CODE_RE.fullmatch(code) and not set(code[2:]) & set("01OI")
    letters = itertools.cycle("AB")
    assert L.new_code(lambda alphabet: next(letters)) == "MKABABAB"


def test_normalize_ignores_case_spaces_and_dashes():
    assert L.normalize(" mk-abc def ") == "MKABCDEF"
    assert L.normalize("MK_ABCDEF") == "MKABCDEF"
    for bad in ("MKABCDE", "MKABCDEFG", "XXABCDEF", "MKABCDE0", "", None, 12, "MK" + "A" * 50):
        assert L.normalize(bad) is None


# ---------------------------------------------------------------- the payment
def test_purchase_reads_the_variable_buyer_and_packages():
    p = L.purchase(payment(code="mk abcdef"), settings())
    assert p == L.Purchase(transaction="tbx-1", buyer="NewKid", packages=(("7", "Server Pass"),), codes=("MKABCDEF",))


def test_purchase_variable_identifier_is_case_insensitive_and_others_are_ignored():
    products = [{"id": 1, "name": "Pass", "variables": [{"identifier": "colour", "option": "MKZZZZZZ"},
                                                         {"identifier": "Referral", "option": "MKABCDEF"}]}]
    assert L.purchase(payment(products=products), settings()).codes == ("MKABCDEF",)
    assert L.purchase(payment(products=products), settings(REFERRAL_VARIABLE="ref")).codes == ()


def test_creator_codes_only_when_the_owner_opts_in():
    body = payment(code="", creator="MKCCCCCC")
    assert L.purchase(body, settings()).codes == ()
    assert L.purchase(body, settings(REFERRAL_CREATOR_CODES="on")).codes == ("MKCCCCCC",)
    both = payment(code="MKABCDEF", creator="MKABCDEF")
    assert L.purchase(both, settings(REFERRAL_CREATOR_CODES="on")).codes == ("MKABCDEF",)


def test_purchase_survives_odd_payloads():
    for subject in ({}, {"products": "x"}, {"products": [None, 3]}, {"customer": "x"}):
        p = L.purchase(subject, settings())
        assert p.codes == () and p.buyer == ""
    p = L.purchase(payment(buyer="not a name!"), settings())
    assert p.buyer == ""


def test_package_filter():
    p = L.purchase(payment(), settings())
    assert L.counts(p, settings())
    assert L.counts(p, settings(REFERRAL_PACKAGES="server pass"))
    assert L.counts(p, settings(REFERRAL_PACKAGES="7"))
    assert not L.counts(p, settings(REFERRAL_PACKAGES="Crown"))
    assert not L.counts(L.purchase(payment(products=[]), settings()), settings())


# ---------------------------------------------------------------- the decision
def decide(p, **kw):
    args = dict(code_owner=1, referrer_name="Steve", buyer_owner=None, buyer_claimed=False, transaction_claimed=False)
    return L.decide(p, settings(), **{**args, **kw})


def test_decision_order():
    p = L.purchase(payment(), settings())
    assert decide(p) == L.OK
    assert decide(L.purchase(payment(code=""), settings())) == L.NO_CODE
    assert decide(p, code_owner=None) == L.UNKNOWN
    assert decide(L.purchase(payment(buyer="?"), settings())) == L.NO_BUYER
    assert decide(L.purchase(payment(products=[{"name": "x"}]), settings())) == L.NO_CODE
    assert decide(p, buyer_claimed=True) == L.REPEAT
    assert decide(p, transaction_claimed=True) == L.REPEAT
    assert decide(p, referrer_name=None) == L.NO_REFERRER


def test_self_referral_by_name_or_by_link():
    p = L.purchase(payment(buyer="Steve"), settings())
    assert decide(p, referrer_name="steve") == L.SELF
    p = L.purchase(payment(buyer="SteveAlt"), settings())
    assert decide(p, buyer_owner=1) == L.SELF
    assert decide(p, buyer_owner=2) == L.OK


# ---------------------------------------------------------------- the unlock
def test_grant_commands_put_the_only_unrepeatable_one_last():
    cmds = L.grant_commands("Steve", "NewKid")
    assert cmds == ["scoreboard players set NewKid rw.kin 1", "scoreboard players set Steve rw.kin 1",
                    "scoreboard players add Steve rw.invites 1"]
    assert all(R.grant_sendable(c) for c in cmds)
    with pytest.raises(ValueError):
        L.grant_commands("Steve", "bad name")
    with pytest.raises(ValueError):
        L.grant_commands("x", "NewKid")


def test_the_bridge_allows_only_the_unlock_shapes():
    for bad in ("whitelist add Steve", "scoreboard players set Steve rw.tier 3", "scoreboard players add Steve rw.kin 1",
                "scoreboard players set Steve rw.kin 5", "scoreboard players add Steve rw.invites 2",
                "scoreboard players set @a rw.kin 1", "scoreboard players set Steve rw.kin 1\nop Steve",
                "scoreboard players set Steve rw.kin 1 ", "op Steve", "", None):
        assert not R.grant_sendable(bad), bad
    # and the chat path still refuses them
    assert not any(R.sendable(c) for c in L.grant_commands("Steve", "NewKid"))


def test_staff_text_lists_what_is_left():
    cmds = L.grant_commands("Ste_ve", "NewKid")
    text = L.staff_text(5, "Ste_ve", "NewKid", 0, cmds, esc)
    assert "<@5>" in text and "Ste\\_ve" in text and "Run over RCON" in text and all(c in text for c in cmds)
    text = L.staff_text(5, "Ste_ve", "NewKid", 2, cmds, esc)
    assert "stopped after 2 of 3" in text and cmds[2] in text and cmds[0] not in text
    assert "Done over RCON" in L.staff_text(5, "Ste_ve", "NewKid", 3, cmds, esc)


def test_refund_and_problem_texts():
    text = L.refund_text(5, "Steve", "NewKid", L.PAYMENT_REFUNDED, esc)
    assert "refunded" in text and "scoreboard players remove Steve rw.invites 1" in text
    assert "chargeback" in L.refund_text(5, "Steve", "NewKid", L.CHARGEBACK, esc)
    assert "own referral code" in L.problem_text(L.SELF, "Steve", esc)
    assert "/smp link" in L.problem_text(L.NO_REFERRER, "NewKid", esc)
    assert L.problem_text(L.REPEAT, "NewKid", esc) is None and L.problem_text(L.UNKNOWN, "x", esc) is None
    assert "MKABCDEF" in L.code_text("MKABCDEF", "Steve", esc) and "No discount" in L.code_text("MKABCDEF", "S", esc)
