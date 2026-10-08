"""Offline tests for cogs.heartbeat: a fake bot, a temp heartbeat file, no network."""

import asyncio
import json
import sys
from pathlib import Path

import config
from cogs import heartbeat as cogmod
from cogs.heartbeat import Heartbeat, write_heartbeat

NOW = 1_800_000_000


class FakeBot:
    def __init__(self, ready=True, closed=False, latency=0.0421):
        self.ready, self.closed, self.latency = ready, closed, latency

    def is_ready(self):
        return self.ready

    def is_closed(self):
        return self.closed

    async def wait_until_ready(self):
        return None


def run(coro):
    return asyncio.run(coro)


def read(path):
    return json.loads(Path(path).read_text())


def test_writes_time_and_latency_when_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(cogmod, "now", lambda: NOW)
    hb = tmp_path / "data" / "heartbeat"
    cog = Heartbeat(FakeBot(), path=hb)
    assert run(cog.write_once()) is True
    assert read(hb) == {"ts": NOW, "latency": 0.0421}
    assert not (tmp_path / "data" / "heartbeat.tmp").exists()


def test_skips_when_not_ready_or_closed(tmp_path):
    hb = tmp_path / "heartbeat"
    assert run(Heartbeat(FakeBot(ready=False), path=hb).write_once()) is False
    assert run(Heartbeat(FakeBot(closed=True), path=hb).write_once()) is False
    assert not hb.exists()


def test_overwrites_previous_heartbeat(tmp_path, monkeypatch):
    hb = tmp_path / "heartbeat"
    cog = Heartbeat(FakeBot(), path=hb)
    monkeypatch.setattr(cogmod, "now", lambda: NOW)
    run(cog.write_once())
    monkeypatch.setattr(cogmod, "now", lambda: NOW + 60)
    run(cog.write_once())
    assert read(hb)["ts"] == NOW + 60


def test_non_finite_latency_is_null(tmp_path):
    for value in (float("nan"), float("inf"), None, "x"):
        write_heartbeat(tmp_path / "hb", NOW, value)
        assert read(tmp_path / "hb") == {"ts": NOW, "latency": None}


def test_write_errors_never_raise(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cogmod, "write_heartbeat", boom)
    assert run(Heartbeat(FakeBot(), path=tmp_path / "hb").write_once()) is False


def test_broken_bot_state_counts_as_disconnected(tmp_path):
    class Weird(FakeBot):
        def is_ready(self):
            raise RuntimeError("no")

    assert run(Heartbeat(Weird(), path=tmp_path / "hb").write_once()) is False


def test_default_path_is_config_heartbeat_file():
    assert Heartbeat(FakeBot()).path == config.HEARTBEAT_FILE


def test_loop_interval_is_one_minute():
    assert Heartbeat.beat.seconds == 60


def test_watchdog_reads_what_the_cog_writes(tmp_path, monkeypatch):
    sys.path.insert(0, str(config.SERVER_DIR))
    import watchdog

    monkeypatch.setattr(cogmod, "now", lambda: NOW)
    hb = tmp_path / "heartbeat"
    run(Heartbeat(FakeBot(latency=float("nan")), path=hb).write_once())
    assert watchdog.parse_heartbeat(hb.read_text()) == NOW
    assert watchdog.default_paths().heartbeat == config.HEARTBEAT_FILE


def test_setup_adds_cog():
    added = []

    class B(FakeBot):
        async def add_cog(self, cog):
            added.append(cog)

    run(cogmod.setup(B()))
    assert isinstance(added[0], Heartbeat)


def test_gateway_disconnect_stops_writes_until_resumed(tmp_path):
    # discord.py keeps is_ready() True through a reconnect loop, so track it ourselves.
    hb = tmp_path / "heartbeat"
    cog = Heartbeat(FakeBot(), path=hb)
    run(cog.on_disconnect())
    assert run(cog.write_once()) is False and not hb.exists()
    run(cog.on_resumed())
    assert run(cog.write_once()) is True
    run(cog.on_disconnect())
    run(cog.on_ready())
    assert run(cog.write_once()) is True


def test_stale_keep_alive_or_missing_socket_counts_as_offline(tmp_path, monkeypatch):
    from types import SimpleNamespace
    hb = tmp_path / "heartbeat"
    bot = FakeBot()
    bot.ws = None
    assert run(Heartbeat(bot, path=hb).write_once()) is False
    monkeypatch.setattr(cogmod.time, "perf_counter", lambda: 1000.0)
    bot.ws = SimpleNamespace(_keep_alive=SimpleNamespace(interval=41.25, _last_ack=1000.0 - 3 * 41.25))
    assert run(Heartbeat(bot, path=hb).write_once()) is False
    bot.ws = SimpleNamespace(_keep_alive=SimpleNamespace(interval=41.25, _last_ack=1000.0 - 10))
    assert run(Heartbeat(bot, path=hb).write_once()) is True
    bot.ws, bot.latency = SimpleNamespace(_keep_alive=None), float("nan")
    assert run(Heartbeat(bot, path=hb).write_once()) is False


def test_loop_lag_probe_warns_on_stalls(tmp_path, caplog):
    cog = Heartbeat(FakeBot(), path=tmp_path / "hb")
    assert cog.note_tick(100.0) is None  # first tick: nothing to compare
    assert cog.note_tick(100.0 + cogmod.LAG_INTERVAL + 0.2) is None  # normal jitter
    with caplog.at_level("WARNING"):
        lag = cog.note_tick(100.0 + 2 * cogmod.LAG_INTERVAL + 0.2 + 3.0)
    assert lag is not None and abs(lag - 3.0) < 1e-6
    assert "event loop was blocked" in caplog.text
