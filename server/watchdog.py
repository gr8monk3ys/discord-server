"""Front Desk downtime watchdog. Runs outside the bot, every 5 minutes, from the Task
Scheduler task "Front Desk watchdog" (see install_watchdog.ps1), so it still works when
the bot process is dead or hung.

Each run:
- reads bot/data/heartbeat (written every minute by cogs/heartbeat.py while connected);
- older than 10 minutes, or missing, counts as down. If the "Front Desk bot" task is not
  running, or it is running but the heartbeat was already stale on the previous check (hung),
  it restarts the task (Stop-ScheduledTask then Start-ScheduledTask), at most 3 times per hour;
- posts to the Discord webhook in WATCHDOG_WEBHOOK_URL (server/.env), pinging only the user in
  WATCHDOG_PING_USER_ID: one alert when it goes down, at most one reminder per hour while it
  stays down, and one "back up" note when the heartbeat is fresh again.

A disabled bot task, or a pause set with --pause, means "down on purpose": no restarts, no
alerts. The webhook URL is a secret: it is never printed or logged.

Usage: pythonw watchdog.py            one check (what the scheduled task runs)
       python watchdog.py --check     print the status, change nothing
       python watchdog.py --pause 60  no restarts/alerts for 60 minutes (--resume ends it)
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

log = logging.getLogger("watchdog")

TASK_NAME = "Front Desk bot"
STALE_AFTER = 10 * 60  # heartbeat older than this = down
REMIND_EVERY = 60 * 60  # at most one reminder per hour
MAX_RESTARTS = 3  # per RESTART_WINDOW
RESTART_WINDOW = 60 * 60
RESTART_GRACE = 9 * 60  # after a restart, give a running bot this long to connect
WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"
LOG_MAX_BYTES = 1_000_000
RUNNING = {"Running", "Queued"}
POWERSHELL = "powershell.exe"
USER_AGENT = "DiscordBot (front-desk-watchdog, 1.0)"

SERVER_DIR = Path(__file__).resolve().parent
DATA_DIR = SERVER_DIR.parent / "bot" / "data"


# ---------------------------------------------------------------- wiring
@dataclass(frozen=True)
class Paths:
    heartbeat: Path
    state: Path
    log: Path


def default_paths() -> Paths:
    # Same file as bot/config.py HEARTBEAT_FILE (not imported: config needs the bot's env).
    return Paths(DATA_DIR / "heartbeat", DATA_DIR / "watchdog.json", DATA_DIR / "watchdog.log")


Runner = Callable[[list], tuple]  # args -> (returncode, stdout, stderr)
Poster = Callable[[str, dict], int]  # (url, json payload) -> HTTP status


def run_powershell(args: list) -> tuple:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash under pythonw
    p = subprocess.run(args, capture_output=True, text=True, timeout=60, creationflags=flags)
    return p.returncode, p.stdout, p.stderr


def post_webhook(url: str, payload: dict) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def real_env() -> dict:
    from dotenv import load_dotenv

    load_dotenv(SERVER_DIR / ".env")
    return dict(os.environ)


@dataclass
class Deps:
    clock: Callable[[], float] = time.time
    run: Runner = run_powershell
    post: Poster = post_webhook
    env: Mapping = field(default_factory=dict)


# ---------------------------------------------------------------- pure helpers
def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def parse_heartbeat(text: str | None) -> int | None:
    """Unix time of the last heartbeat, or None when missing or unreadable."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    ts = data.get("ts") if isinstance(data, dict) else None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return int(ts)


def load_state(text: str | None) -> dict:
    st = {"down_since": None, "alerted": False, "last_alert": 0, "restarts": [], "paused_until": 0}
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        return st
    if isinstance(data.get("down_since"), int):
        st["down_since"] = data["down_since"]
    if isinstance(data.get("alerted"), bool):
        st["alerted"] = data["alerted"]
    for key in ("last_alert", "paused_until"):
        if isinstance(data.get(key), int):
            st[key] = data[key]
    if isinstance(data.get("restarts"), list):
        st["restarts"] = [r for r in data["restarts"] if isinstance(r, int)]
    return st


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def webhook_config(env: Mapping) -> tuple:
    """(url, ping_user_id, problem). Never include the URL itself in `problem`."""
    url = (env.get("WATCHDOG_WEBHOOK_URL") or "").strip()
    uid = (env.get("WATCHDOG_PING_USER_ID") or "").strip()
    uid = uid if re.fullmatch(r"\d{5,25}", uid) else None
    if not url:
        return None, None, "WATCHDOG_WEBHOOK_URL is not set in server/.env; no alerts sent"
    if not url.startswith(WEBHOOK_PREFIX):
        return None, None, f"WATCHDOG_WEBHOOK_URL must start with {WEBHOOK_PREFIX}; no alerts sent"
    return url, uid, None


def payload(text: str, user_id: str | None) -> dict:
    content = f"<@{user_id}> {text}" if user_id else text
    return {
        "content": content[:2000],
        "username": "Front Desk watchdog",
        "allowed_mentions": {"parse": [], "users": [user_id] if user_id else []},
    }


@dataclass(frozen=True)
class Plan:
    fresh: bool
    restart: bool = False
    alert: str | None = None  # "down" | "remind" | "up" | None
    capped: bool = False  # wanted a restart but hit MAX_RESTARTS
    skip: str | None = None  # why nothing is done (paused / disabled)


def recent_restarts(state: dict, now: int) -> list:
    return [r for r in state["restarts"] if now - r < RESTART_WINDOW]


def decide(state: dict, now: int, hb_ts: int | None, task: str | None) -> Plan:
    """What this run should do. `task` is the bot task's state, None if it can't be read."""
    fresh = hb_ts is not None and now - hb_ts <= STALE_AFTER
    if fresh:
        return Plan(True, alert="up" if state["alerted"] else None)
    if state["paused_until"] > now:
        return Plan(False, skip="paused")
    if task == "Disabled":
        return Plan(False, skip="bot task is disabled")
    restart = False
    capped = False
    if task is not None:
        hung = task in RUNNING and state["down_since"] is not None
        wants = task not in RUNNING or hung
        recent = recent_restarts(state, now)
        in_grace = task in RUNNING and bool(recent) and now - max(recent) <= RESTART_GRACE
        if wants and not in_grace:
            if len(recent) >= MAX_RESTARTS:
                capped = True
            else:
                restart = True
    if not state["alerted"]:
        alert = "down"
    elif now - state["last_alert"] >= REMIND_EVERY:
        alert = "remind"
    else:
        alert = None
    return Plan(False, restart=restart, alert=alert, capped=capped)


def minutes(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} min"


def message(plan: Plan, kind: str, state: dict, now: int, hb_ts, task, restart_result) -> str:
    since = hb_ts if hb_ts is not None else state["down_since"] or now
    if kind == "up":
        start = state["down_since"] or since
        return f"Front Desk is back up (heartbeat is fresh again after about {minutes(now - start)} down)."
    if hb_ts is None:
        head = "no heartbeat file"
    else:
        head = f"no heartbeat for {minutes(now - hb_ts)}"
    first = "Front Desk looks down" if kind == "down" else "Front Desk is still down"
    if kind == "remind" and state["down_since"]:
        first += f" (for {minutes(now - state['down_since'])})"
    parts = [f"{first}: {head}; task '{TASK_NAME}' is {task or 'unknown (not found?)'}."]
    if restart_result is not None:
        ok, detail = restart_result
        parts.append("Restarted the task." if ok else f"Restart failed: {detail[:300]}")
    elif plan.capped:
        parts.append(f"Restart limit reached ({MAX_RESTARTS} in the last hour); not restarting.")
    elif task is None:
        parts.append("Could not read the task, so it was not restarted.")
    else:
        parts.append("Waiting for the last restart to come up.")
    return " ".join(parts)


# ---------------------------------------------------------------- task commands
def _ps(script: str) -> list:
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def _quoted(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def task_state(run: Runner) -> str | None:
    """'Running', 'Ready', 'Disabled', 'Queued', ... or None if it can't be read."""
    try:
        rc, out, _ = run(_ps(f"(Get-ScheduledTask -TaskName {_quoted(TASK_NAME)} -ErrorAction Stop).State"))
    except Exception as e:  # noqa: BLE001 (powershell missing, timeout)
        log.warning("task state query failed: %s", type(e).__name__)
        return None
    state = (out or "").strip()
    return state if rc == 0 and state else None


def restart_task(run: Runner) -> tuple:
    """(ok, detail). Stop first so a hung process is killed; Stop on a stopped task is fine."""
    name = _quoted(TASK_NAME)
    script = (f"Stop-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue; "
              f"Start-Sleep -Seconds 3; Start-ScheduledTask -TaskName {name} -ErrorAction Stop")
    try:
        rc, _, err = run(_ps(script))
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__
    if rc != 0:
        return False, " ".join((err or "").split()) or f"exit code {rc}"
    return True, ""


# ---------------------------------------------------------------- one run
def send(deps: Deps, text: str) -> bool:
    url, uid, problem = webhook_config(deps.env)
    if url is None:
        log.warning(problem)
        return False
    try:
        status = deps.post(url, payload(text, uid))
    except Exception as e:  # noqa: BLE001 (never log str(e): it may contain the URL)
        log.warning("webhook post failed: %s", type(e).__name__)
        return False
    if not 200 <= status < 300:
        log.warning("webhook post failed: HTTP %s", status)
        return False
    return True


def run_once(paths: Paths, deps: Deps, act: bool = True) -> dict:
    """One check. Never raises; returns a status dict (also used by --check)."""
    try:
        return _run_once(paths, deps, act)
    except Exception as e:  # noqa: BLE001
        log.exception("watchdog run failed")
        return {"error": type(e).__name__}


def _run_once(paths: Paths, deps: Deps, act: bool) -> dict:
    now = int(deps.clock())
    hb_ts = parse_heartbeat(read_text(paths.heartbeat))
    state = load_state(read_text(paths.state))
    task = task_state(deps.run)
    plan = decide(state, now, hb_ts, task)
    url, _, problem = webhook_config(deps.env)
    status = {
        "now": now, "heartbeat_age": None if hb_ts is None else now - hb_ts, "fresh": plan.fresh,
        "task": task, "would_restart": plan.restart, "would_alert": plan.alert, "capped": plan.capped,
        "skip": plan.skip, "webhook": "configured" if url else problem,
        "restarts_last_hour": len(recent_restarts(state, now)), "state": state,
    }
    if not act:
        return status

    if plan.fresh:
        if plan.alert == "up":
            if send(deps, message(plan, "up", state, now, hb_ts, task, None)) or url is None:
                log.info("bot is back up")
                state.update(down_since=None, alerted=False, last_alert=0)
        elif state["down_since"] is not None:
            log.info("heartbeat fresh again")
            state["down_since"] = None
    elif plan.skip:
        log.info("heartbeat stale but %s; doing nothing", plan.skip)
        state.update(down_since=None, alerted=False, last_alert=0)
    else:
        if state["down_since"] is None:
            state["down_since"] = now
        restart_result = None
        if plan.restart:
            restart_result = restart_task(deps.run)
            state["restarts"].append(now)
            log.warning("heartbeat stale (task %s); restart %s", task,
                        "ok" if restart_result[0] else f"failed: {restart_result[1]}")
        else:
            log.warning("heartbeat stale (task %s); not restarting%s", task,
                        " (restart limit)" if plan.capped else "")
        if plan.alert and send(deps, message(plan, plan.alert, state, now, hb_ts, task, restart_result)):
            state.update(alerted=True, last_alert=now)
    state["restarts"] = recent_restarts(state, now)
    write_json_atomic(paths.state, state)
    return status


def set_pause(paths: Paths, deps: Deps, minutes: int) -> None:
    now = int(deps.clock())
    state = load_state(read_text(paths.state))
    state["paused_until"] = now + minutes * 60 if minutes > 0 else 0
    write_json_atomic(paths.state, state)


def print_status(status: dict) -> None:
    if status.get("error"):
        print(f"watchdog error: {status['error']}")
        return
    age = status["heartbeat_age"]
    hb = "missing" if age is None else f"{age} s old"
    print(f"heartbeat:   {hb} ({'fresh' if status['fresh'] else 'STALE'})")
    print(f"bot task:    {status['task'] or 'unknown (not found or not readable)'}")
    print(f"webhook:     {status['webhook']}")
    st = status["state"]
    print(f"alerted:     {st['alerted']}  down since: {st['down_since'] or '-'}")
    print(f"restarts:    {status['restarts_last_hour']} in the last hour (max {MAX_RESTARTS})")
    if st["paused_until"] > status["now"]:
        print(f"paused:      {minutes(st['paused_until'] - status['now'])} left")
    if status["skip"]:
        print(f"would do:    nothing ({status['skip']})")
    else:
        print(f"would do:    restart={status['would_restart']} alert={status['would_alert'] or 'none'}"
              + (" (restart limit reached)" if status["capped"] else ""))


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main(argv: list | None = None, paths: Paths | None = None, deps: Deps | None = None) -> int:
    ap = argparse.ArgumentParser(description="Front Desk downtime watchdog")
    ap.add_argument("--check", action="store_true", help="print the status without acting")
    ap.add_argument("--pause", type=int, metavar="MINUTES", help="no restarts or alerts for MINUTES")
    ap.add_argument("--resume", action="store_true", help="end a pause")
    args = ap.parse_args(argv)
    paths = paths or default_paths()
    deps = deps or Deps(env=real_env())
    if args.check:
        print_status(run_once(paths, deps, act=False))
        return 0
    setup_logging(paths.log)
    if args.pause is not None or args.resume:
        set_pause(paths, deps, 0 if args.resume else max(0, args.pause))
        log.info("paused for %s min" % args.pause if not args.resume else "pause ended")
        return 0
    status = run_once(paths, deps)
    return 1 if status.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
