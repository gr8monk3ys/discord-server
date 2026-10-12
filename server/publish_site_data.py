"""Publish the landing page's live data: push bot/data/site/stats.json to the `site-data` branch.

    python publish_site_data.py            # dry run: says what it would push, changes nothing
    python publish_site_data.py --apply    # push
    pythonw publish_site_data.py --apply --quiet   # what a scheduled task runs every 30 minutes

The bot (cogs/sitedata.py) writes the file; site/app.js reads it from
raw.githubusercontent.com/<repo>/site-data/stats.json. Each run:

- reads the file and checks it again with the same schema and leak guard the bot used
  (site_schema.py), plus snapshot_lib.find_leaks. A file that fails is never pushed (exit 1);
- skips a file the bot stopped refreshing (older than 6 hours): the page then hides its live
  sections on its own once the published copy is 48 hours old;
- compares it with what the branch already has, ignoring `generated_at` and the online count,
  and pushes only when the content changed or the published copy is a day old;
- commits it as the single file stats.json in ONE parentless commit and force-pushes that
  with a lease on the old tip, so the branch never grows history. This is the only branch the
  script will ever write: any other branch name is refused before git runs.

All git work happens in a throwaway repository under the system temp directory. The main
checkout is only asked for its origin URL; its index, working tree and branches are never
touched. Nothing to do, no file yet, or no network: exit 0 without noise, so it is safe to
run from a scheduled task. Prompts are disabled, so missing credentials fail instead of hanging.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import site_schema as schema
import snapshot_lib

BRANCH = "site-data"  # the only branch this script may write
FILE_NAME = schema.FILE_NAME
SOURCE_MAX_AGE = 6 * 3600  # the bot refreshes every 30 minutes; older means it is down
REFRESH_AFTER = 24 * 3600  # push an unchanged file anyway once the published one is this old
GIT_TIMEOUT = 60
AUTHOR = ("Front Desk site data", "site-data@discord.lscaturchio.xyz")
COMMIT_MESSAGE = "Live site data"

SERVER_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parent
SOURCE = REPO_DIR / "bot" / "data" / "site" / FILE_NAME

# Outcomes. Everything but INVALID and FAILED exits 0.
PUSHED, WOULD_PUSH, UNCHANGED, NO_FILE, STALE, OFFLINE, INVALID, FAILED = (
    "pushed", "would-push", "unchanged", "no-file", "stale", "offline", "invalid", "failed")
QUIET_OUTCOMES = {UNCHANGED, NO_FILE, STALE, OFFLINE, PUSHED, WOULD_PUSH}


@dataclass(frozen=True)
class Result:
    outcome: str
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.outcome in QUIET_OUTCOMES else 1


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash under pythonw
    try:
        p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0]}: {type(e).__name__}") from e
    if check and p.returncode != 0:
        raise GitError(f"git {args[0]} failed ({p.returncode})")
    return p


def origin_url(repo: Path = REPO_DIR) -> str | None:
    """The main checkout's origin URL. A read of its config; nothing in it is changed."""
    try:
        url = git(["config", "--get", "remote.origin.url"], cwd=repo, check=False).stdout.strip()
    except GitError:
        return None
    return url or None


def check_branch(branch: str) -> None:
    if branch != BRANCH:
        raise SystemExit(f"Refusing to run: this script only ever writes the '{BRANCH}' branch.")


def load_source(path: Path) -> tuple[str | None, dict | None, list[str]]:
    """(text, document, problems). text is None when there is no file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None, []
    except (OSError, UnicodeDecodeError):
        return "", None, ["$: unreadable"]
    doc, problems = schema.check_text(text)
    if doc is not None:
        problems = problems + [p for p in snapshot_lib.find_leaks(doc) if p not in problems]
    return text, doc, problems


def needs_push(local: dict, remote_text: str | None, now: int) -> bool:
    """True when the branch is missing, unreadable, different, or a day old."""
    if remote_text is None:
        return True
    remote, problems = schema.check_text(remote_text)
    if remote is None or problems:
        return True  # replace whatever is there with a clean file
    if schema.content_key(remote) != schema.content_key(local):
        return True
    published = schema.parse_iso(remote.get("generated_at"))
    return published is None or now - published >= REFRESH_AFTER


def _force_remove(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)  # git marks objects read-only; Windows won't delete those
    func(path)


def remove_tree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


def remote_tip(remote: str, branch: str) -> str | None:
    """The branch's commit on the remote, '' when the branch does not exist yet, None when
    the remote cannot be reached."""
    try:
        p = git(["ls-remote", "--heads", remote, f"refs/heads/{branch}"], check=False)
    except GitError:
        return None
    if p.returncode != 0:
        return None
    line = p.stdout.strip().splitlines()
    return line[0].split()[0] if line else ""


def publish(source: Path, remote: str, branch: str = BRANCH, apply: bool = False,
            now: int | None = None, workdir: Path | None = None) -> Result:
    check_branch(branch)
    now = int(time.time()) if now is None else now
    text, doc, problems = load_source(source)
    if text is None:
        return Result(NO_FILE, f"{source.name} has not been written yet")
    if problems or doc is None:
        return Result(INVALID, "; ".join(problems) or "$: not an object")
    generated = schema.parse_iso(doc.get("generated_at"))
    if generated is None or now - generated > SOURCE_MAX_AGE:
        return Result(STALE, "the bot has not refreshed the file recently")

    tip = remote_tip(remote, branch)
    if tip is None:
        return Result(OFFLINE, "the remote is unreachable")

    tmp = Path(tempfile.mkdtemp(prefix="site-data-", dir=workdir))
    try:
        git(["init", "--quiet", str(tmp)])
        remote_text = None
        if tip:
            try:
                git(["fetch", "--quiet", "--depth", "1", "--no-tags", remote, f"refs/heads/{branch}"], cwd=tmp)
            except GitError:
                return Result(OFFLINE, "the remote is unreachable")
            shown = git(["show", f"FETCH_HEAD:{FILE_NAME}"], cwd=tmp, check=False)
            remote_text = shown.stdout if shown.returncode == 0 else None
        if not needs_push(doc, remote_text, now):
            return Result(UNCHANGED, "the branch already has this data")
        size = len(text.encode("utf-8"))
        sections = ", ".join(k for k in schema.TOP_KEYS if k in doc and k not in ("v", "generated_at")) or "none"
        what = f"{FILE_NAME} ({size} bytes; sections: {sections})"
        if not apply:
            return Result(WOULD_PUSH, f"would push {what} to {branch}. Run with --apply to push.")

        # One parentless commit holding exactly one file.
        git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=tmp)
        (tmp / FILE_NAME).write_text(schema.dumps(doc), encoding="utf-8", newline="\n")
        git(["add", "--", FILE_NAME], cwd=tmp)
        git(["-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}", "-c", "commit.gpgsign=false",
             "-c", "core.autocrlf=false", "commit", "--quiet", "--no-verify", "-m", COMMIT_MESSAGE], cwd=tmp)
        tracked = git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=tmp).stdout.split()
        parents = git(["rev-list", "--count", "HEAD"], cwd=tmp).stdout.strip()
        if tracked != [FILE_NAME] or parents != "1":
            return Result(FAILED, "the commit is not a single file in a single commit; nothing pushed")
        try:  # the lease: only replace the tip we looked at (or create the branch if there was none)
            git(["push", "--quiet", "--no-verify", f"--force-with-lease=refs/heads/{branch}:{tip}", remote,
                 f"HEAD:refs/heads/{branch}"], cwd=tmp)
        except GitError:
            return Result(FAILED, "the push was refused (credentials, or the branch moved); nothing changed")
        return Result(PUSHED, f"pushed {what} to {branch}")
    except GitError as e:
        return Result(FAILED, str(e))
    finally:
        try:
            remove_tree(tmp)
        except OSError:
            pass  # a leftover temp folder is not worth failing the run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="push (without it this is a dry run)")
    parser.add_argument("--quiet", action="store_true", help="print only problems (for the scheduled task)")
    parser.add_argument("--file", type=Path, default=SOURCE, help="the stats.json to publish")
    parser.add_argument("--remote", help="git remote URL (default: this checkout's origin)")
    parser.add_argument("--branch", default=BRANCH, help=f"must be {BRANCH}; anything else is refused")
    args = parser.parse_args(argv)

    check_branch(args.branch)
    remote = args.remote or origin_url()
    if not remote:
        print("No git remote: pass --remote or run from a checkout with an origin.")
        return 1
    result = publish(args.file, remote, args.branch, apply=args.apply)
    if (result.exit_code != 0 or not args.quiet) and sys.stdout is not None:  # None under pythonw
        print(f"{result.outcome}: {result.detail}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
