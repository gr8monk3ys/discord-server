"""register_linked_roles against a fake HTTP layer (no Discord, no token)."""

import pytest

import register_linked_roles as script
from logic import linkedroles

APP_ID = "100000000000000001"
META = f"/applications/{APP_ID}/role-connections/metadata"


class FakeHttp:
    """Answers GETs from what it was given and records every request."""

    def __init__(self, records=(), url=None, fail=None):
        self.records = list(records)
        self.url = url
        self.fail = fail or {}
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if (method, path) in self.fail:
            raise script.ApiError(method, path, self.fail[(method, path)], "Missing Access")
        if (method, path) == ("GET", "/applications/@me"):
            return {"id": APP_ID, "name": "Front Desk", "role_connections_verification_url": self.url}
        if (method, path) == ("GET", META):
            return list(self.records)
        if (method, path) == ("PUT", META):
            self.records = body
            return body
        if (method, path) == ("PATCH", "/applications/@me"):
            self.url = body["role_connections_verification_url"]
            return {"id": APP_ID}
        raise AssertionError(f"unexpected {method} {path}")

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def run(http, apply=False, url=None):
    lines = []
    ok = script.run(http, apply, url, out=lines.append)
    return ok, "\n".join(lines)


def test_dry_run_prints_the_diff_and_writes_nothing():
    http = FakeHttp()
    ok, text = run(http)
    assert ok
    assert http.writes == []
    assert "add     steam" in text and "add     platforms" in text
    assert "set     verification URL: not set -> https://" in text
    assert "Dry run: nothing was changed" in text


def test_apply_puts_the_records_and_sets_the_url():
    defs = linkedroles.load()
    http = FakeHttp()
    ok, text = run(http, apply=True)
    assert ok
    assert http.writes == [
        ("PUT", META, linkedroles.records(defs)),
        ("PATCH", "/applications/@me", {"role_connections_verification_url": defs["verification_url"]}),
    ]
    # Only Discord's four fields go out: our "service"/"count" markers stay home.
    assert all(set(r) == {"type", "key", "name", "description"} for r in http.writes[0][2])
    assert "wrote 5 records" in text and "set the verification URL" in text


def test_a_second_apply_changes_nothing():
    http = FakeHttp()
    run(http, apply=True)
    http.calls.clear()
    ok, text = run(http, apply=True)
    assert ok and http.writes == []
    assert "Nothing to change." in text


def test_only_what_differs_is_written():
    defs = linkedroles.load()
    http = FakeHttp(records=linkedroles.records(defs), url="https://old.example.com/api/linked-role/start")
    run(http, apply=True)
    assert [c[:2] for c in http.writes] == [("PATCH", "/applications/@me")]

    stale = linkedroles.records(defs)
    stale[2]["name"] = "PSN"
    http = FakeHttp(records=stale, url=defs["verification_url"])
    ok, text = run(http, apply=True)
    assert [c[:2] for c in http.writes] == [("PUT", META)]
    assert "change  playstation: name 'PSN' -> 'PlayStation'" in text


def test_url_override_and_a_bad_override():
    http = FakeHttp()
    other = "https://preview.example.com/api/linked-role/start"
    run(http, apply=True, url=other)
    assert http.url == other

    http = FakeHttp()
    with pytest.raises(ValueError):
        run(http, apply=True, url="https://example.com/anything")
    assert http.writes == []


def test_a_refused_request_stops_before_any_later_write():
    http = FakeHttp(fail={("GET", META): 403})
    with pytest.raises(script.ApiError, match="403: Missing Access"):
        run(http, apply=True)
    assert http.writes == []

    http = FakeHttp(fail={("PUT", META): 400})
    with pytest.raises(script.ApiError):
        run(http, apply=True)
    assert [c[:2] for c in http.writes] == [("PUT", META)]  # the URL is not set after a failed PUT
    assert http.url is None


def test_a_reply_without_an_application_id_is_refused():
    class NoId(FakeHttp):
        def request(self, method, path, body=None):
            self.calls.append((method, path, body))
            return {"message": "401: Unauthorized"}

    http = NoId()
    with pytest.raises(script.ApiError, match="no application id"):
        run(http, apply=True)
    assert len(http.calls) == 1


def test_main_without_a_token_exits_before_any_request(monkeypatch):
    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setattr(script.sys, "argv", ["register_linked_roles.py", "--apply"])
    monkeypatch.setattr(script, "Http", lambda token: pytest.fail("no request without a token"))
    with pytest.raises(SystemExit, match="Set DISCORD_TOKEN"):
        script.main()


def test_main_never_prints_the_token(monkeypatch, capsys):
    token = "not-a-real-token-1234567890"
    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DISCORD_TOKEN", token)
    monkeypatch.setattr(script.sys, "argv", ["register_linked_roles.py"])
    seen = {}

    class Leaky(FakeHttp):
        def __init__(self, given):
            super().__init__()
            seen["token"] = given

        def request(self, method, path, body=None):
            raise script.ApiError(method, path, 401, f"bad token {token}")

    monkeypatch.setattr(script, "Http", Leaky)
    with pytest.raises(SystemExit) as stop:
        script.main()
    assert seen["token"] == token
    out = capsys.readouterr()
    assert token not in str(stop.value) and token not in out.out and token not in out.err
    assert "[token]" in str(stop.value)


def test_discord_message_is_cut_and_cleaned():
    assert script._discord_message(b'{"message": "Missing Access", "code": 50001}') == "Missing Access"
    assert script._discord_message(b"<html>") == ""
    assert script._discord_message(b"[1]") == ""
    assert script._discord_message(b'{"message": 5}') == ""
    assert "\n" not in script._discord_message(b'{"message": "a\\nb"}')
    assert len(script._discord_message(b'{"message": "' + b"x" * 900 + b'"}')) == 200
