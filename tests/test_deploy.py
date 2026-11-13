import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import FAKES, LIBRARIAN_ROLE, Env, git, make_config, register
from test_engine import article, last_block, run_failing
from test_m1 import decide, subjects_since

from work_sheet import cli
from work_sheet.config import load_config
from work_sheet.dispatch import InvalidDecision
from work_sheet.vault import parse_frontmatter

DEPLOY = Path(__file__).parent.parent / "deploy" / "oracle"
PY = json.dumps(sys.executable)


def runner(script: str, *args: str, fallback: str | None = None) -> str:
    command = ", ".join([PY, json.dumps(str(FAKES / script)), *map(json.dumps, args)])
    return f"command = [{command}]\n" + (f'fallback = "{fallback}"\n' if fallback else "")


def write_runners(env: Env, **runners: str) -> None:
    (env.tmp / "config" / "runners.toml").write_text(
        "".join(f"[runners.{name}]\n{body}" for name, body in runners.items()))


# --- runner fallback -------------------------------------------------------------------------------------

def test_task_falls_back_after_restoring_the_view(seeded: Env):
    env = seeded
    write_runners(env, small=runner("dispatcher.py"), strong=runner("broken.py", fallback="backup"),
                  backup=runner("librarian.py"))
    src = article(env)
    env.push({"inbox/a.md": f"收录 {src}\n"})
    env.respond(register(capture=[str(src)]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "done" and task["attempts"] == 1
    assert "主模型运行失败（退出码 3），改用备用 runner backup" in env.text(f"tasks/{task['id']}.md")
    assert "wiki/half.md" not in env.listing.read_text().split("\n")  # the fallback saw a clean view
    assert not (env.human / "wiki/half.md").exists()
    assert (env.state / "vault" / "runs" / task["id"] / "fallback" / "stdout.log").exists()


def test_dispatcher_fallback(seeded: Env):
    env = seeded
    write_runners(env, small=runner("broken.py", fallback="backup"), backup=runner("dispatcher.py"),
                  strong=runner("librarian.py"))
    env.push({"inbox/a.md": "今天天气不错\n"})
    head = env.remote_head()
    env.respond("EXIT:1")  # primary and fallback both down: nothing happens
    env.tick()
    assert env.remote_head() == head and env.dispatch_calls() == 1

    env.respond({"action": "ignore", "reason": "无关"})
    env.tick()
    assert subjects_since(env, head) == ["dispatch: a"]


def test_unknown_fallback_is_a_config_error(env: Env):
    write_runners(env, small=runner("dispatcher.py", fallback="nope"))
    with pytest.raises(SystemExit, match="fallback 'nope' of runner 'small' is not defined"):
        make_config(env.tmp)


def test_skills_without_a_runner_are_not_offered(env: Env, caplog):
    runners = env.tmp / "no-web.toml"
    runners.write_text(f"[runners.small]\n{runner('dispatcher.py')}[runners.strong]\n{runner('librarian.py')}")
    cfg = load_config(env.tmp / "config", env.tmp / "roles", env.tmp / "skills", env.state, None,
                      env.tmp / "vault-template", runners_file=runners)
    assert "research" not in {s.name for s in cfg.dispatchable()}
    with pytest.raises(InvalidDecision, match="可用的有：digest、ingest、restructure、update-project$"):
        decide(cfg, {"skill": "research"}, [])
    with caplog.at_level(logging.INFO, logger="work_sheet"):
        cli.main(["tick", *env.dirs(), "--runners-file", str(runners)])
    assert "skills not offered to the dispatcher (their role's runner is not configured): research" in caplog.text


# --- briefing feeds --------------------------------------------------------------------------------------

@pytest.fixture
def feed_source(seeded: Env, monkeypatch) -> Path:
    source = seeded.tmp / "digest"
    source.mkdir()
    (seeded.tmp / "feeds.toml").write_text(
        f'[feeds.x-digest]\ntitle = "英文推特精选"\nsource = {json.dumps(str(source))}\npattern = "????-??-??.json"\n'
        'cadence = "daily"\nurl = "https://example.org/news/x/?d={date}"\nsince = "2026-11-01"\n'
        f'[feeds.forum]\ntitle = "Forum 周报"\nsource = {json.dumps(str(source / "missing"))}\n'
        'pattern = "????-??-??.json"\ncadence = "weekly"\n')
    monkeypatch.setenv("WORKSHEET_FEEDS_FILE", str(seeded.tmp / "feeds.toml"))
    return source


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False))


def test_feeds_import_briefings(seeded: Env, feed_source: Path, caplog):
    env = seeded
    write_json(feed_source / "2026-11-10.json", {"date": "2026-11-10", "title": "今日精选", "summary": "三条要闻",
                                                 "md": "## 要闻\n\n- 一\n", "updated_at": "2026-11-10T08:00:00Z"})
    write_json(feed_source / "2026-11-12.json", {"title": "只有 HTML", "html": "<h1>标题</h1><p>正文 <b>粗</b></p>"})
    write_json(feed_source / "2026-10-30.json", {"title": "太早", "md": "x"})
    write_json(feed_source / "index.json", {"title": "目录"})
    (feed_source / "2026-11-11.json").write_text("{not json")
    head = env.remote_head()
    env.tick()
    assert subjects_since(env, head) == ["feed: 2 篇简报"]
    assert git(env.remote, "log", "-1", "--format=%(trailers:key=Role,valueonly,separator=%x2C)").strip() == "engine"
    assert "2026-11-11.json" in caplog.text and "does not exist" in caplog.text
    env.pull()
    assert sorted(p.name for p in (env.human / "briefings").rglob("*.md")) == ["2026-11-10.md", "2026-11-12.md"]
    meta, body = parse_frontmatter(env.text("briefings/x-digest/2026/11/2026-11-10.md"))
    assert meta == {"type": "briefing", "stream": "x-digest", "stream_title": "英文推特精选", "cadence": "daily",
                    "date": meta["date"], "title": "今日精选", "description": "三条要闻",
                    "url": "https://example.org/news/x/?d=2026-11-10", "source_updated": "2026-11-10T08:00:00Z"}
    assert str(meta["date"]) == "2026-11-10" and body == "## 要闻\n\n- 一\n"
    meta, body = parse_frontmatter(env.text("briefings/x-digest/2026/11/2026-11-12.md"))
    assert meta["description"] == "只有 HTML" and meta["source_updated"] is None and body == "# 标题\n\n正文 **粗**\n"

    head = env.remote_head()
    env.tick()
    assert env.remote_head() == head  # nothing changed at the source

    write_json(feed_source / "2026-11-10.json", {"title": "今日精选（更正）", "md": "更正后\n",
                                                 "updated_at": "2026-11-10T09:00:00Z"})
    env.tick()
    assert subjects_since(env, head) == ["feed: 1 篇简报"]
    env.pull()
    assert env.text("briefings/x-digest/2026/11/2026-11-10.md").endswith("---\n更正后\n")


def test_roles_cannot_write_briefings(seeded: Env, monkeypatch):
    (seeded.tmp / "roles" / "librarian.md").write_text(
        LIBRARIAN_ROLE.replace('write: ["wiki/**", "proposals/**"]', 'write: ["wiki/**", "briefings/**"]'))
    task = run_failing(seeded, "briefing", monkeypatch)
    assert task["status"] == "failed"
    assert last_block(seeded.text("inbox/a.md"))[1] == "> 写入越界：briefings/x-digest/2026/11/2026-11-10.md"


# --- deploy/oracle ---------------------------------------------------------------------------------------

def test_oracle_config_loads(env: Env, monkeypatch):
    for name, value in {"WORKSHEET_AGENT_WRAPPER": "/opt/worksheet/deploy/oracle/run-as-agent.sh",
                        "WORKSHEET_PI_BIN": "pi", "WORKSHEET_SMALL_MODEL": "anthropic/claude-haiku-5-5",
                        "WORKSHEET_STRONG_MODEL": "anthropic/claude-sonnet-5-5",
                        "WORKSHEET_FALLBACK_MODEL": "deepseek/deepseek-chat",
                        "WORKSHEET_DIGEST_DATA_DIR": "/srv/digest/www/data", "WORKSHEET_SITE_URL": "https://example.org",
                        "WORKSHEET_FEED_SINCE": "2026-11-01"}.items():
        monkeypatch.setenv(name, value)
    cfg = load_config(env.tmp / "config", env.tmp / "roles", env.tmp / "skills", env.state, None,
                      env.tmp / "vault-template", DEPLOY / "runners.toml", DEPLOY / "feeds.toml")
    assert {n: r.fallback for n, r in cfg.runners.items()} == {
        "small": "fallback_small", "strong": "fallback_strong", "fallback_small": None, "fallback_strong": None}
    for r in cfg.runners.values():
        assert r.command[:3] == ["/opt/worksheet/deploy/oracle/run-as-agent.sh", "{view}", "pi"]
    assert "deepseek/deepseek-chat" in cfg.runners["fallback_strong"].command
    assert "--no-tools" in cfg.runners["small"].command and "read,grep,find,ls,edit,write" in cfg.runners["strong"].command
    assert {n: (str(f.source), f.cadence, f.since) for n, f in cfg.feeds.items()} == {
        "x-digest": ("/srv/digest/www/data", "daily", "2026-11-01"),
        "build": ("/srv/digest/www/data/build", "daily", "2026-11-01"),
        "buyers": ("/srv/digest/www/data/buyers", "daily", "2026-11-01"),
        "forum": ("/srv/digest/www/data/forum", "weekly", "2026-11-01")}
    assert cfg.feeds["forum"].url == "https://example.org/news/forum/?d={date}"
    assert "research" not in {s.name for s in cfg.dispatchable()}  # no web runner on that host


# --- time zone -------------------------------------------------------------------------------------------

@pytest.fixture
def restore_tz(monkeypatch):
    """main() sets TZ and calls tzset(); put the process zone back afterwards."""
    monkeypatch.setenv("TZ", os.environ.get("TZ", "UTC"))
    yield
    monkeypatch.undo()
    time.tzset()


def test_timezone_sets_task_dates(seeded: Env, monkeypatch, restore_tz):
    env = seeded
    utc_today = datetime.now(timezone.utc).date()
    # One of these is always on a different calendar day than UTC.
    zone = next(z for z in ("Pacific/Kiritimati", "Etc/GMT+12") if datetime.now(ZoneInfo(z)).date() != utc_today)
    local_today = datetime.now(ZoneInfo(zone)).date()
    monkeypatch.setenv("WS_MODE", "noop")
    monkeypatch.setenv("WORKSHEET_TIMEZONE", zone)
    env.push({"inbox/a.md": "收录点什么\n"})
    env.respond(register(capture=[]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    assert task["id"].startswith(f"T-{local_today:%Y%m%d}-") and task["created"].startswith(str(local_today))
    brief = env.listing.with_suffix(".brief.md").read_text()
    assert brief.split("\n")[2] == f"- 今天的日期：{local_today}"


def test_invalid_timezone_aborts(env: Env, monkeypatch, restore_tz):
    monkeypatch.setenv("WORKSHEET_TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(SystemExit, match="WORKSHEET_TIMEZONE='Mars/Olympus_Mons' is not a valid IANA time zone"):
        env.tick()
