"""publish_site_data.py against a local bare repository in tmp_path: no network, no real remote."""

import json
import subprocess
from pathlib import Path

import pytest

import publish_site_data as pub
import site_schema as schema

T = 1_800_000_000
HOUR = 3600
SNOWFLAKE = 123456789012345678  # shaped like a Discord ID; not a real one


def git(*args, cwd=None) -> str:
    p = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True)
    return p.stdout.strip()


def document(now=T, **over) -> dict:
    doc = {"v": 1, "generated_at": schema.iso(now), "members": {"total": 15, "online": 6},
           "week": {"messages": 1240, "voice_hours": 34},
           "events": [{"title": "Valorant game night", "game": "Valorant", "kind": "gamenight",
                       "start": schema.iso(now + 86400)}],
           "games": [{"name": "Valorant", "hours": 12}],
           "seasonal": {"name": "Spooky Season", "emoji": "🎃", "ends": schema.iso(now + 9 * 86400)}}
    doc.update(over)
    return doc


@pytest.fixture
def world(tmp_path):
    """A bare 'GitHub', a main checkout whose origin it is, a stats.json and a temp work dir."""
    remote = tmp_path / "remote.git"
    git("init", "--bare", "--initial-branch=main", str(remote))
    checkout = tmp_path / "checkout"
    git("init", "--initial-branch=main", str(checkout))
    (checkout / "README.md").write_text("the real repo\n", encoding="utf-8")
    git("add", "README.md", cwd=checkout)
    git("commit", "-m", "init", cwd=checkout)
    git("remote", "add", "origin", str(remote), cwd=checkout)
    git("push", "origin", "main", cwd=checkout)
    (checkout / "scratch.txt").write_text("uncommitted work\n", encoding="utf-8")  # must survive
    source = tmp_path / "data" / "stats.json"
    source.parent.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    class World:
        pass

    w = World()
    w.remote, w.checkout, w.source, w.work = remote, checkout, source, work
    w.write = lambda doc: source.write_text(schema.dumps(doc), encoding="utf-8", newline="\n")
    w.run = lambda apply=True, now=T, branch=pub.BRANCH: pub.publish(
        source, str(remote), branch, apply=apply, now=now, workdir=work)
    w.heads = lambda: git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=remote).split()
    w.published = lambda: json.loads(git("show", f"{pub.BRANCH}:stats.json", cwd=remote))
    w.tip = lambda: git("rev-parse", pub.BRANCH, cwd=remote)
    return w


def checkout_state(w) -> tuple:
    return (git("rev-parse", "HEAD", cwd=w.checkout), git("status", "--porcelain", cwd=w.checkout),
            git("for-each-ref", cwd=w.checkout), (w.checkout / ".git" / "index").read_bytes())


# ---------------------------------------------------------------- the happy path
def test_dry_run_is_the_default_and_pushes_nothing(world):
    world.write(document())
    result = world.run(apply=False)
    assert result.outcome == pub.WOULD_PUSH and result.exit_code == 0
    assert "--apply" in result.detail and "stats.json" in result.detail
    assert world.heads() == ["main"]
    assert list(world.work.iterdir()) == []  # the temp repository is cleaned up


def test_apply_creates_a_single_file_single_commit_branch(world):
    world.write(document())
    before = checkout_state(world)
    result = world.run()
    assert result.outcome == pub.PUSHED and result.exit_code == 0
    assert sorted(world.heads()) == ["main", pub.BRANCH]
    assert git("ls-tree", "-r", "--name-only", pub.BRANCH, cwd=world.remote).split() == ["stats.json"]
    assert git("rev-list", "--count", pub.BRANCH, cwd=world.remote) == "1"
    assert world.published() == document()
    raw = subprocess.run(["git", "show", f"{pub.BRANCH}:stats.json"], cwd=world.remote, capture_output=True,
                         check=True).stdout
    assert b"\r" not in raw and raw.decode("utf-8") == schema.dumps(document())
    # the main checkout: same HEAD, same refs, same index bytes, scratch file untouched
    assert checkout_state(world) == before
    assert (world.checkout / "scratch.txt").read_text(encoding="utf-8") == "uncommitted work\n"
    assert not (world.checkout / "stats.json").exists()
    assert git("rev-parse", "main", cwd=world.remote) == before[0]  # main on the remote did not move
    assert list(world.work.iterdir()) == []


def test_unchanged_content_is_not_pushed_again(world):
    world.write(document())
    world.run()
    tip = world.tip()
    # 30 minutes later: a new timestamp and a new online count, nothing else
    world.write(document(now=T, generated_at=schema.iso(T + 1800), members={"total": 15, "online": 9}))
    result = world.run(now=T + 1800)
    assert result.outcome == pub.UNCHANGED and result.exit_code == 0
    assert world.tip() == tip and world.published()["members"]["online"] == 6


def test_changed_content_replaces_the_branch_without_growing_history(world):
    world.write(document())
    world.run()
    first = world.tip()
    world.write(document(now=T, generated_at=schema.iso(T + 1800), week={"messages": 1300, "voice_hours": 35}))
    assert world.run(now=T + 1800).outcome == pub.PUSHED
    assert world.tip() != first
    assert git("rev-list", "--count", pub.BRANCH, cwd=world.remote) == "1"  # squashed: still one commit
    assert world.published()["week"]["messages"] == 1300
    assert git("ls-tree", "-r", "--name-only", pub.BRANCH, cwd=world.remote).split() == ["stats.json"]


def test_refreshes_at_least_daily(world):
    world.write(document())
    world.run()
    later = T + pub.REFRESH_AFTER - 60
    world.write(document(now=T, generated_at=schema.iso(later)))
    assert world.run(now=later).outcome == pub.UNCHANGED
    later = T + pub.REFRESH_AFTER
    world.write(document(now=T, generated_at=schema.iso(later)))
    assert world.run(now=later).outcome == pub.PUSHED
    assert world.published()["generated_at"] == schema.iso(later)


def test_a_broken_published_file_is_replaced(world):
    world.write(document())
    world.run()
    scratch = world.work.parent / "vandal"
    git("clone", "--branch", pub.BRANCH, str(world.remote), str(scratch))
    (scratch / "stats.json").write_text("not json", encoding="utf-8")
    git("commit", "-am", "break it", cwd=scratch)
    git("push", "origin", pub.BRANCH, cwd=scratch)
    assert world.run(now=T + 60).outcome == pub.PUSHED
    assert world.published() == document()
    assert git("rev-list", "--count", pub.BRANCH, cwd=world.remote) == "1"


# ---------------------------------------------------------------- refusing
@pytest.mark.parametrize("branch", ["main", "master", "front-desk-bot", "site-data2", "refs/heads/site-data",
                                    "Site-Data", "", "site-data "])
def test_any_other_branch_is_refused_before_git_runs(world, branch, monkeypatch):
    world.write(document())
    monkeypatch.setattr(pub, "git", lambda *a, **k: pytest.fail("git ran"))
    with pytest.raises(SystemExit) as e:
        world.run(branch=branch)
    assert "site-data" in str(e.value)
    with pytest.raises(SystemExit):
        pub.main(["--apply", "--branch", branch, "--remote", str(world.remote), "--file", str(world.source)])


def test_force_push_can_only_name_the_data_branch():
    source = Path(pub.__file__).read_text(encoding="utf-8")
    assert source.count("--force-with-lease") == 1 and '"--force"' not in source and "push -f" not in source
    assert 'f"--force-with-lease=refs/heads/{branch}:{tip}"' in source and 'f"HEAD:refs/heads/{branch}"' in source
    assert pub.BRANCH == "site-data"


@pytest.mark.parametrize("mutate", [
    lambda d: d["members"].update(total=SNOWFLAKE),
    lambda d: d["events"][0].update(title=f"party for <@{SNOWFLAKE}>"),
    lambda d: d["events"][0].update(title="join discord.gg/abcdef"),
    lambda d: d["events"][0].update(host="someone"),
    lambda d: d.update(top_chatters=[{"name": "someone", "messages": 5}]),
    lambda d: d["games"][0].update(name="Some Private Hobby Game"),
    lambda d: d.update(v=2),
])
def test_a_file_that_fails_the_checks_is_never_pushed(world, mutate):
    doc = document()
    mutate(doc)
    world.write(doc)
    result = world.run()
    assert result.outcome == pub.INVALID and result.exit_code == 1
    assert str(SNOWFLAKE) not in result.detail and "someone" not in result.detail  # the place, not the value
    assert world.heads() == ["main"]


def test_garbage_and_oversized_files_are_refused(world):
    world.source.write_text("{not json", encoding="utf-8")
    assert world.run().outcome == pub.INVALID
    world.source.write_text(schema.dumps(document()) + " " * schema.MAX_BYTES, encoding="utf-8")
    assert world.run().outcome == pub.INVALID
    world.source.write_bytes(b"\xff\xfe\x00")
    assert world.run().outcome == pub.INVALID
    world.source.write_text("[1, 2]", encoding="utf-8")
    assert world.run().outcome == pub.INVALID
    assert world.heads() == ["main"]


def test_snapshot_leak_guard_is_reused(world, monkeypatch):
    world.write(document())
    monkeypatch.setattr(pub.snapshot_lib, "find_leaks", lambda data, secrets=(): ["$.x: contains a secret"])
    result = world.run()
    assert result.outcome == pub.INVALID and "contains a secret" in result.detail
    assert world.heads() == ["main"]


# ---------------------------------------------------------------- quiet exits
def test_no_file_yet_is_quiet(world):
    result = world.run()
    assert result.outcome == pub.NO_FILE and result.exit_code == 0
    assert world.heads() == ["main"]


def test_a_file_the_bot_stopped_refreshing_is_not_published(world):
    world.write(document())
    result = world.run(now=T + pub.SOURCE_MAX_AGE + 1)
    assert result.outcome == pub.STALE and result.exit_code == 0
    assert world.heads() == ["main"]


def test_unreachable_remote_is_quiet(world, tmp_path):
    world.write(document())
    result = pub.publish(world.source, str(tmp_path / "no-such-remote.git"), apply=True, now=T, workdir=world.work)
    assert result.outcome == pub.OFFLINE and result.exit_code == 0
    assert list(world.work.iterdir()) == []


def test_a_branch_that_moved_under_us_is_not_overwritten(world, monkeypatch):
    """The lease: if the tip changed between our look and our push, the push is refused."""
    world.write(document())
    world.run()
    stale_tip = world.tip()
    world.write(document(week={"messages": 1300, "voice_hours": 35}))
    world.run(now=T + 60)
    newer = world.tip()
    assert newer != stale_tip
    world.write(document(week={"messages": 1400, "voice_hours": 36}))
    monkeypatch.setattr(pub, "remote_tip", lambda remote, branch: stale_tip)
    result = world.run(now=T + 120)
    assert result.outcome == pub.FAILED and result.exit_code == 1
    assert world.tip() == newer


# ---------------------------------------------------------------- command line
def test_main_dry_run_then_apply(world, capsys, monkeypatch):
    monkeypatch.setattr(pub.time, "time", lambda: T)
    world.write(document())
    args = ["--remote", str(world.remote), "--file", str(world.source)]
    assert pub.main(args) == 0
    assert capsys.readouterr().out.startswith("would-push:")
    assert world.heads() == ["main"]
    assert pub.main([*args, "--apply", "--quiet"]) == 0
    assert capsys.readouterr().out == ""
    assert world.published() == document()
    assert pub.main([*args, "--apply", "--quiet"]) == 0  # nothing to do: silent
    assert capsys.readouterr().out == ""


def test_main_reports_a_bad_file_even_when_quiet(world, capsys, monkeypatch):
    monkeypatch.setattr(pub.time, "time", lambda: T)
    world.write(document(members={"total": SNOWFLAKE}))
    assert pub.main(["--remote", str(world.remote), "--file", str(world.source), "--apply", "--quiet"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("invalid:") and str(SNOWFLAKE) not in out


def test_origin_url_reads_the_checkout_without_changing_it(world):
    before = checkout_state(world)
    assert pub.origin_url(world.checkout) == str(world.remote)
    assert checkout_state(world) == before
    assert pub.origin_url(world.work) is None or isinstance(pub.origin_url(world.work), str)


def test_defaults_point_at_the_bot_data_folder():
    assert pub.SOURCE.parts[-4:] == ("bot", "data", "site", "stats.json")
    assert pub.FILE_NAME == "stats.json"
