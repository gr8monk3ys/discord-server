"""Register Front Desk's Linked Roles metadata with Discord and point the application's
"Linked Roles Verification URL" at the site. Dry run by default.

    python register_linked_roles.py              # show current vs wanted, change nothing
    python register_linked_roles.py --apply      # write the records and the URL
    python register_linked_roles.py --url https://example.com/api/linked-role/start

The records come from site/api/linked-role/metadata.json (the same file the site's functions
read). Needs DISCORD_TOKEN in .env (see .env.example); no GUILD_ID: this edits the application,
not a server. The token is sent to discord.com only and never printed. Run it AFTER the site
functions are deployed and their settings are in place, or the Link button leads to a
"not set up yet" page. See docs/LINKED_ROLES.md.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Appended, not inserted: nothing in bot/ may shadow a module of server/.
sys.path.append(str(Path(__file__).resolve().parent.parent / "bot"))

from logic import linkedroles  # noqa: E402  (bot/ has to be on the path first)

API = "https://discord.com/api/v10"
TIMEOUT = 15
MAX_REPLY = 256 * 1024
USER_AGENT = "DiscordBot (https://github.com/gr8monk3ys/discord-server, 1.0)"


class ApiError(Exception):
    """Discord refused a request. Carries the status and Discord's own short message only."""

    def __init__(self, method, path, status, message=""):
        self.status = status
        detail = f": {message}" if message else ""
        super().__init__(f"{method} {path} -> {status}{detail}")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the token must never follow a redirect
        return None


class Http:
    """The only code here that talks to Discord. Tests replace it with a fake."""

    def __init__(self, token: str):
        self._token = token
        self._opener = urllib.request.build_opener(_NoRedirects)

    def request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", f"Bot {self._token}")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as e:
            raise ApiError(method, path, e.code, _discord_message(e.read(4096))) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(method, path, 0, type(e).__name__) from None
        if len(raw) > MAX_REPLY:
            raise ApiError(method, path, 0, "reply too large")
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(method, path, 0, "reply was not JSON") from None


def _discord_message(raw: bytes) -> str:
    try:
        message = json.loads(raw).get("message", "")
    except (ValueError, AttributeError):
        return ""
    message = message if isinstance(message, str) else ""
    return "".join(ch if ch.isprintable() else "?" for ch in message)[:200]


def run(http, apply: bool, url: str | None = None, out=print) -> bool:
    """Show the diff and, with apply, make the changes. True when everything went through."""
    defs = linkedroles.load()
    app = http.request("GET", "/applications/@me")
    app_id = str(app.get("id", "")) if isinstance(app, dict) else ""
    if not app_id.isdigit():
        raise ApiError("GET", "/applications/@me", 0, "no application id in the reply")
    metadata_path = f"/applications/{app_id}/role-connections/metadata"
    plan = linkedroles.plan(http.request("GET", metadata_path), app.get("role_connections_verification_url"), defs, url)

    out("Linked Roles metadata (current -> wanted):")
    for line in plan.lines:
        out(line)
    if not plan.changes:
        out("Nothing to change.")
        return True
    if not apply:
        out("Dry run: nothing was changed. Re-run with --apply.")
        return True
    if plan.put_records is not None:
        http.request("PUT", metadata_path, plan.put_records)
        out(f"  done    wrote {len(plan.put_records)} records")
    if plan.set_url is not None:
        http.request("PATCH", "/applications/@me", {"role_connections_verification_url": plan.set_url})
        out("  done    set the verification URL")
    return True


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="change the application (default: dry run)")
    parser.add_argument("--url", help="verification URL, if not the one in metadata.json")
    args = parser.parse_args()
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit("Set DISCORD_TOKEN in server/.env (copy .env.example).")
    try:
        run(Http(token.strip()), args.apply, args.url)
    except (ApiError, ValueError) as e:
        sys.exit(f"Stopped: {str(e).replace(token.strip(), '[token]')}")


if __name__ == "__main__":
    main()
