"""logic/tebex.py: settings, Tebex's signature, and reading a cancellation webhook."""

import hashlib
import hmac
import json

import pytest

from logic import tebex as L

SECRET = "s" * 32


def test_off_without_a_long_enough_secret():
    assert L.read_settings({}) == (None, ())
    settings, problems = L.read_settings({"TEBEX_WEBHOOK_SECRET": "short"})
    assert settings is None and "shorter than" in problems[0] and "short" not in problems[0].split()


def test_settings_defaults_and_bad_values():
    settings, problems = L.read_settings({"TEBEX_WEBHOOK_SECRET": SECRET})
    assert (settings.host, settings.port, problems) == ("127.0.0.1", 8790, ())
    settings, problems = L.read_settings({"TEBEX_WEBHOOK_SECRET": SECRET, "TEBEX_WEBHOOK_HOST": "0.0.0.0",
                                          "TEBEX_WEBHOOK_PORT": "9000"})
    assert (settings.host, settings.port) == ("0.0.0.0", 9000)
    settings, problems = L.read_settings({"TEBEX_WEBHOOK_SECRET": SECRET, "TEBEX_WEBHOOK_HOST": "a b",
                                          "TEBEX_WEBHOOK_PORT": "80"})
    assert (settings.host, settings.port) == ("127.0.0.1", 8790) and len(problems) == 2


def test_the_secret_never_shows_in_repr():
    settings, _ = L.read_settings({"TEBEX_WEBHOOK_SECRET": SECRET})
    assert SECRET not in repr(settings)


def test_signature_matches_tebex_recipe():
    body = b'{"id":"x"}'
    expected = hmac.new(SECRET.encode(), hashlib.sha256(body).hexdigest().encode(), hashlib.sha256).hexdigest()
    assert L.signature(SECRET, body) == expected
    assert L.verify(SECRET, body, expected) and L.verify(SECRET, body, expected.upper())
    assert not L.verify(SECRET, body + b" ", expected)
    assert not L.verify("t" * 32, body, expected)
    assert not L.verify(SECRET, body, None) and not L.verify(SECRET, body, "")


@pytest.mark.parametrize("body", [b"nope", b"[]", b'{"type": "x"}', b'{"id": "", "type": "x"}',
                                  b'{"id": "x"}', b"\xff"])
def test_parse_refuses_bad_bodies(body):
    with pytest.raises(L.BadBody):
        L.parse(body)


def subject():
    return {
        "reference": "tbx-r-12345",
        "next_payment_at": "2026-12-07T17:00:00+00:00",
        "cancel_reason": "No time\nthis month",
        "status": {"id": 6, "description": "Pending Cancellation"},
        "initial_payment": {"customer": {"username": {"id": "old", "username": "OldName"}}},
        "last_payment": {"customer": {"username": {"id": "u1", "username": "Steve_99"}},
                         "products": [{"name": "Monthly Pass"}, {"name": "Cape"}]},
    }


def test_ending_reads_a_recurring_payment():
    e = L.ending(subject())
    assert e == L.Ending(player="Steve_99", package="Monthly Pass, Cape", ends_at=1796662800,
                         reference="tbx-r-12345", reason="No time this month")


def test_ending_survives_missing_parts():
    assert L.ending({}) == L.Ending("", "", None, "", "")
    s = subject()
    del s["last_payment"]
    s["next_payment_at"] = "2026-12-07T17:00:00Z"
    e = L.ending(s)
    assert e.player == "OldName" and e.package == "" and e.ends_at == 1796662800
    assert L.when("2026-12-07T17:00:00") is None and L.when("soon") is None and L.when(5) is None


def test_alert_text():
    text = L.alert_text(L.ending(subject()), lambda s: s.replace("_", "\\_"))
    assert text.startswith("⏳ **Pass ending:** **Steve\\_99** cancelled Monthly Pass, Cape.")
    assert "<t:1796662800:D>" in text and "Was there something that would have kept you?" in text
    assert "Reason given to Tebex: No time this month" in text and "tbx-r-12345" in text
    bare = L.alert_text(L.ending({}), str)
    assert "A buyer" in bare and "end of the paid month" in bare and "Reason" not in bare


def test_event_round_trip():
    body = json.dumps({"id": "abc", "type": L.CANCELLATION, "date": "x", "subject": subject()}).encode()
    event = L.parse(body)
    assert event.id == "abc" and event.type == L.CANCELLATION and event.subject["reference"] == "tbx-r-12345"
    assert L.parse(b'{"id": "a", "type": "t", "subject": []}').subject == {}


def test_env_example_and_docs_name_the_keys_with_no_value():
    from pathlib import Path
    root = Path(L.__file__).resolve().parents[2]
    lines = (root / "server" / ".env.example").read_text("utf-8").splitlines()
    for name in (L.ENV_SECRET, L.ENV_HOST, L.ENV_PORT, "FACTIONS_FORUM", "FACTIONS_RESERVED", "FACTIONS_SEASON"):
        assert f"{name}=" in lines
    doc = (root / "docs" / "TEBEX_WEBHOOK.md").read_text("utf-8")
    assert L.PATH in doc and str(L.DEFAULT_PORT) in doc and L.CANCELLATION in doc
