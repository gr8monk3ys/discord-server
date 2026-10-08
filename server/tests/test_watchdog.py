"""Tests for server/watchdog.py. Clock, file reads, subprocess and HTTP are all fakes:
nothing touches Task Scheduler or the network."""

import json
import logging

import pytest

import watchdog as W

NOW = 1_800_000_000
MIN = 60
HOUR = 3600
URL = "https://discord.com/api/webhooks/123/fake-token-value"
OWNER = "424242"


# ---------------------------------------------------------------- fakes
class FakeRunner:
    """Answers powershell calls: the task-state query and the restart."""

    def __init__(self, state="Ready", restart_rc=0, query_rc=0):
        self.state = state
        self.restart_rc = restart_rc
        self.query_rc = query_rc
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        script = args[-1]
        if "Get-ScheduledTask" in script:
            return (self.query_rc, self.state + "\r\n" if self.query_rc == 0 else "", "" if self.query_rc == 0 else "not found")
        if "Start-ScheduledTask" in script:
            return (self.restart_rc, "", "" if self.restart_rc == 0 else "Access is denied.")
        raise AssertionError(f"unexpected command {args}")

    @property
    def restarts(self):
        return [c for c in self.calls if "Start-ScheduledTask" in c[-1]]


class FakePoster:
    def __init__(self, status=204, raises=None):
        self.status = status
        self.raises = raises
        self.sent = []

    def __call__(self, url, payload):
        self.sent.append((url, payload))
        if self.raises:
            raise self.raises
        return self.status


def make(tmp_path, *, hb_age=None, state=None, task="Ready", env=None, poster=None, runner=None, now=NOW):
    paths = W.Paths(
        heartbeat=tmp_path / "heartbeat",
        state=tmp_path / "watchdog.json",
        log=tmp_path / "watchdog.log",
    )
    if hb_age is not None:
        paths.heartbeat.write_text(json.dumps({"ts": now - hb_age, "latency": 0.05}))
    if state is not None:
        paths.state.write_text(json.dumps(state))
    deps = W.Deps(
        clock=lambda: now,
        run=runner or FakeRunner(task),
        post=poster or FakePoster(),
        env={"WATCHDOG_WEBHOOK_URL": URL, "WATCHDOG_PING_USER_ID": OWNER} if env is None else env,
    )
    return paths, deps


def saved(paths):
    return json.loads(paths.state.read_text())


# ---------------------------------------------------------------- pure helpers
def test_parse_heartbeat():
    assert W.parse_heartbeat(json.dumps({"ts": 123, "latency": 0.1})) == 123
    assert W.parse_heartbeat(json.dumps({"ts": 12.9, "latency": None})) == 12
    assert W.parse_heartbeat(None) is None
    assert W.parse_heartbeat("") is None
    assert W.parse_heartbeat("not json") is None
    assert W.parse_heartbeat(json.dumps({"latency": 1})) is None
    assert W.parse_heartbeat(json.dumps({"ts": "soon"})) is None
    assert W.parse_heartbeat(json.dumps([1, 2])) is None
    assert W.parse_heartbeat(json.dumps({"ts": True})) is None


def test_load_state_defaults_on_bad_input():
    for text in (None, "", "{", "[]", json.dumps({"restarts": "x", "alerted": "yes"})):
        st = W.load_state(text)
        assert st["alerted"] is False and st["restarts"] == [] and st["down_since"] is None


def test_webhook_config_validates():
    assert W.webhook_config({"WATCHDOG_WEBHOOK_URL": URL, "WATCHDOG_PING_USER_ID": OWNER}) == (URL, OWNER, None)
    url, uid, problem = W.webhook_config({})
    assert url is None and uid is None and "not set" in problem
    url, _, problem = W.webhook_config({"WATCHDOG_WEBHOOK_URL": "http://discord.com/api/webhooks/1/x"})
    assert url is None and "must start with" in problem
    url, _, problem = W.webhook_config({"WATCHDOG_WEBHOOK_URL": "https://evil.example/api/webhooks/1/x"})
    assert url is None
    # a bad user id just drops the ping
    assert W.webhook_config({"WATCHDOG_WEBHOOK_URL": URL, "WATCHDOG_PING_USER_ID": "<@1>"})[1] is None
    assert W.webhook_config({"WATCHDOG_WEBHOOK_URL": f"  {URL}  ", "WATCHDOG_PING_USER_ID": " 1234567 "}) == (URL, "1234567", None)


def test_payload_only_pings_the_owner():
    p = W.payload("Front Desk is down.", OWNER)
    assert p["content"].startswith(f"<@{OWNER}> ")
    assert p["allowed_mentions"] == {"parse": [], "users": [OWNER]}
    p = W.payload("x", None)
    assert p["content"] == "x" and p["allowed_mentions"] == {"parse": [], "users": []}


def test_decide_fresh_quiet():
    plan = W.decide(W.load_state(None), NOW, NOW - 30, "Running")
    assert plan.fresh and not plan.restart and plan.alert is None


def test_decide_stale_and_task_not_running_restarts_and_alerts():
    plan = W.decide(W.load_state(None), NOW, NOW - 11 * MIN, "Ready")
    assert not plan.fresh and plan.restart and plan.alert == "down"


def test_decide_missing_heartbeat_is_stale():
    plan = W.decide(W.load_state(None), NOW, None, "Ready")
    assert not plan.fresh and plan.restart and plan.alert == "down"


def test_decide_stale_exactly_at_threshold_is_fresh():
    assert W.decide(W.load_state(None), NOW, NOW - W.STALE_AFTER, "Running").fresh
    assert not W.decide(W.load_state(None), NOW, NOW - W.STALE_AFTER - 1, "Running").fresh


def test_decide_running_but_stale_waits_one_check_then_restarts():
    st = W.load_state(None)
    first = W.decide(st, NOW, NOW - 11 * MIN, "Running")
    assert not first.restart and first.alert == "down"
    st.update(down_since=NOW, alerted=True, last_alert=NOW)
    second = W.decide(st, NOW + 5 * MIN, NOW - 11 * MIN, "Running")
    assert second.restart and second.alert is None


def test_decide_restart_cap_three_per_hour():
    st = W.load_state(None)
    st.update(down_since=NOW - HOUR, alerted=True, last_alert=NOW - 10 * MIN,
              restarts=[NOW - 50 * MIN, NOW - 30 * MIN, NOW - 15 * MIN])
    plan = W.decide(st, NOW, None, "Ready")
    assert not plan.restart and plan.capped
    st["restarts"] = [NOW - 61 * MIN, NOW - 30 * MIN, NOW - 15 * MIN]
    assert W.decide(st, NOW, None, "Ready").restart


def test_decide_grace_after_restart_while_running():
    st = W.load_state(None)
    st.update(down_since=NOW - 20 * MIN, alerted=True, last_alert=NOW - 5 * MIN, restarts=[NOW - 5 * MIN])
    assert not W.decide(st, NOW, None, "Running").restart  # still starting up
    assert W.decide(st, NOW, None, "Ready").restart  # it exited again
    st["restarts"] = [NOW - W.RESTART_GRACE - 1]
    assert W.decide(st, NOW, None, "Running").restart


def test_decide_reminder_at_most_hourly():
    st = W.load_state(None)
    st.update(down_since=NOW - 2 * HOUR, alerted=True, last_alert=NOW - 59 * MIN,
              restarts=[NOW - 2 * HOUR, NOW - 90 * MIN, NOW - 61 * MIN])
    assert W.decide(st, NOW, None, "Ready").alert is None
    st["last_alert"] = NOW - HOUR
    assert W.decide(st, NOW, None, "Ready").alert == "remind"


def test_decide_back_up_only_after_an_alert():
    st = W.load_state(None)
    st.update(down_since=NOW - 30 * MIN, alerted=True, last_alert=NOW - 25 * MIN)
    assert W.decide(st, NOW, NOW - 20, "Running").alert == "up"
    st.update(alerted=False)
    assert W.decide(st, NOW, NOW - 20, "Running").alert is None


def test_decide_disabled_task_or_pause_does_nothing():
    plan = W.decide(W.load_state(None), NOW, None, "Disabled")
    assert not plan.restart and plan.alert is None and plan.skip
    st = W.load_state(None)
    st["paused_until"] = NOW + 10 * MIN
    plan = W.decide(st, NOW, None, "Ready")
    assert not plan.restart and plan.alert is None and plan.skip
    st["paused_until"] = NOW - 1
    assert W.decide(st, NOW, None, "Ready").restart


def test_decide_missing_task_alerts_without_restart():
    plan = W.decide(W.load_state(None), NOW, None, None)
    assert not plan.restart and plan.alert == "down"


# ---------------------------------------------------------------- task commands
def test_task_state_and_restart_use_powershell_without_shell():
    runner = FakeRunner("Running")
    assert W.task_state(runner) == "Running"
    ok, detail = W.restart_task(runner)
    assert ok and detail == ""
    for args in runner.calls:
        assert args[0].lower().endswith("powershell.exe") or args[0].lower() == "powershell"
        assert "-NoProfile" in args and "-NonInteractive" in args
        assert "'Front Desk bot'" in args[-1]
    script = runner.calls[1][-1]
    assert script.index("Stop-ScheduledTask") < script.index("Start-ScheduledTask")


def test_task_state_unknown_on_error():
    assert W.task_state(FakeRunner(query_rc=1)) is None

    def boom(args):
        raise OSError("no powershell")

    assert W.task_state(boom) is None
    ok, detail = W.restart_task(boom)
    assert not ok and "OSError" in detail


def test_restart_failure_reports_stderr():
    ok, detail = W.restart_task(FakeRunner(restart_rc=1))
    assert not ok and "Access is denied" in detail


# ---------------------------------------------------------------- run_once
def test_fresh_heartbeat_does_nothing(tmp_path):
    paths, deps = make(tmp_path, hb_age=90, task="Running")
    status = W.run_once(paths, deps)
    assert status["fresh"] and deps.run.restarts == [] and deps.post.sent == []


def test_down_restarts_then_alerts_once(tmp_path):
    paths, deps = make(tmp_path, hb_age=15 * MIN, task="Ready")
    W.run_once(paths, deps)
    assert len(deps.run.restarts) == 1
    assert len(deps.post.sent) == 1
    url, body = deps.post.sent[0]
    assert url == URL
    assert body["allowed_mentions"] == {"parse": [], "users": [OWNER]}
    assert f"<@{OWNER}>" in body["content"] and "down" in body["content"].lower()
    assert "15 min" in body["content"] and "restarted" in body["content"].lower()
    st = saved(paths)
    assert st["alerted"] and st["down_since"] == NOW and st["restarts"] == [NOW] and st["last_alert"] == NOW


def test_still_down_five_minutes_later_restarts_again_without_new_alert(tmp_path):
    paths, deps = make(tmp_path, hb_age=15 * MIN, task="Ready")
    W.run_once(paths, deps)
    later = W.Deps(clock=lambda: NOW + 5 * MIN, run=deps.run, post=deps.post, env=deps.env)
    W.run_once(paths, later)
    assert len(deps.run.restarts) == 2
    assert len(deps.post.sent) == 1


def test_whole_outage_alert_reminder_cap_and_back_up(tmp_path):
    paths, deps = make(tmp_path, task="Ready")  # no heartbeat at all
    runner, poster = deps.run, deps.post
    for i in range(14):  # every 5 minutes for 65 minutes
        t = NOW + i * 5 * MIN
        W.run_once(paths, W.Deps(clock=lambda t=t: t, run=runner, post=poster, env=deps.env))
    # 3 restarts (0, 5, 10 min), capped until each falls out of the hour window (60, 65)
    assert len(runner.restarts) == 5
    kinds = [b["content"] for _, b in poster.sent]
    assert len(kinds) == 2 and "still down" in kinds[1].lower()
    # back up
    t = NOW + 70 * MIN
    paths.heartbeat.write_text(json.dumps({"ts": t - 30, "latency": 0.04}))
    W.run_once(paths, W.Deps(clock=lambda: t, run=runner, post=poster, env=deps.env))
    assert "back up" in poster.sent[-1][1]["content"].lower()
    assert len(poster.sent) == 3
    st = saved(paths)
    assert not st["alerted"] and st["down_since"] is None
    # and stays quiet afterwards
    W.run_once(paths, W.Deps(clock=lambda: t + 5 * MIN, run=runner, post=poster, env=deps.env))
    assert len(poster.sent) == 3


def test_failed_alert_is_retried_next_run(tmp_path):
    paths, deps = make(tmp_path, hb_age=15 * MIN, poster=FakePoster(status=500))
    W.run_once(paths, deps)
    assert saved(paths)["alerted"] is False
    good = FakePoster()
    W.run_once(paths, W.Deps(clock=lambda: NOW + 5 * MIN, run=deps.run, post=good, env=deps.env))
    assert len(good.sent) == 1 and saved(paths)["alerted"] is True


def test_failed_back_up_is_retried(tmp_path):
    state = {"down_since": NOW - HOUR, "alerted": True, "last_alert": NOW - HOUR, "restarts": []}
    paths, deps = make(tmp_path, hb_age=30, task="Running", state=state, poster=FakePoster(raises=OSError("x")))
    W.run_once(paths, deps)
    assert saved(paths)["alerted"] is True
    good = FakePoster()
    W.run_once(paths, W.Deps(clock=lambda: NOW + 5 * MIN, run=deps.run, post=good, env=deps.env))
    assert "back up" in good.sent[0][1]["content"].lower() and saved(paths)["alerted"] is False


def test_no_webhook_still_restarts_and_never_logs_url(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    paths, deps = make(tmp_path, hb_age=15 * MIN, env={})
    W.run_once(paths, deps)
    assert len(deps.run.restarts) == 1 and deps.post.sent == []
    assert "WATCHDOG_WEBHOOK_URL" in caplog.text


def test_url_never_in_logs_even_on_errors(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    err = OSError(f"connect failed for {URL}")
    paths, deps = make(tmp_path, hb_age=15 * MIN, poster=FakePoster(raises=err))
    W.run_once(paths, deps)
    assert "fake-token-value" not in caplog.text
    assert "webhooks" not in caplog.text


def test_restart_limit_message(tmp_path):
    state = {"down_since": NOW - HOUR, "alerted": True, "last_alert": NOW - HOUR,
             "restarts": [NOW - 40 * MIN, NOW - 30 * MIN, NOW - 20 * MIN]}
    paths, deps = make(tmp_path, state=state, task="Ready")
    W.run_once(paths, deps)
    assert deps.run.restarts == []
    content = deps.post.sent[0][1]["content"].lower()
    assert "still down" in content and "restart limit" in content


def test_failed_restart_counts_and_is_reported(tmp_path):
    paths, deps = make(tmp_path, hb_age=15 * MIN, runner=FakeRunner("Ready", restart_rc=1))
    W.run_once(paths, deps)
    assert saved(paths)["restarts"] == [NOW]
    assert "restart failed" in deps.post.sent[0][1]["content"].lower()


def test_corrupt_state_file_is_tolerated(tmp_path):
    paths, deps = make(tmp_path, hb_age=15 * MIN)
    paths.state.write_text("{oops")
    W.run_once(paths, deps)
    assert saved(paths)["alerted"] is True


def test_check_mode_acts_on_nothing(tmp_path, capsys):
    paths, deps = make(tmp_path, hb_age=15 * MIN, task="Ready")
    status = W.run_once(paths, deps, act=False)
    assert deps.run.restarts == [] and deps.post.sent == [] and not paths.state.exists()
    assert status["would_restart"] and status["would_alert"] == "down"
    W.print_status(status)
    out = capsys.readouterr().out
    assert "heartbeat" in out.lower() and "Ready" in out
    assert URL not in out and "configured" in out.lower()


def test_pause_and_resume(tmp_path):
    paths, deps = make(tmp_path, task="Ready")
    W.set_pause(paths, deps, minutes=60)
    W.run_once(paths, deps)
    assert deps.run.restarts == [] and deps.post.sent == []
    W.set_pause(paths, deps, minutes=0)
    W.run_once(paths, deps)
    assert len(deps.run.restarts) == 1


def test_state_write_is_atomic_and_leaves_no_tmp(tmp_path):
    paths, deps = make(tmp_path, hb_age=15 * MIN)
    W.run_once(paths, deps)
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_run_once_never_raises(tmp_path):
    def bad_clock():
        raise RuntimeError("clock broke")

    paths, deps = make(tmp_path)
    deps = W.Deps(clock=bad_clock, run=deps.run, post=deps.post, env=deps.env)
    status = W.run_once(paths, deps)
    assert status.get("error")


def test_main_check_uses_injected_deps(tmp_path, capsys):
    paths, deps = make(tmp_path, hb_age=30, task="Running")
    assert W.main(["--check"], paths=paths, deps=deps) == 0
    assert "fresh" in capsys.readouterr().out.lower()
    assert deps.post.sent == [] and deps.run.restarts == []


def test_main_configures_rotating_log(tmp_path):
    paths, deps = make(tmp_path, hb_age=30, task="Running")
    W.main([], paths=paths, deps=deps)
    handlers = [h for h in logging.getLogger("watchdog").handlers
                if getattr(h, "baseFilename", "").endswith("watchdog.log")]
    try:
        assert handlers and handlers[0].maxBytes == 1_000_000
    finally:
        for h in handlers:
            logging.getLogger("watchdog").removeHandler(h)
            h.close()


def test_default_paths_point_at_bot_data():
    p = W.default_paths()
    assert p.heartbeat.parts[-3:] == ("bot", "data", "heartbeat")
    assert p.state.name == "watchdog.json" and p.log.name == "watchdog.log"


@pytest.mark.parametrize("latency", [None, 0.1])
def test_heartbeat_written_by_cog_format_is_readable(latency):
    assert W.parse_heartbeat(json.dumps({"ts": NOW, "latency": latency})) == NOW
