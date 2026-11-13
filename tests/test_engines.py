import json
from datetime import datetime, timedelta

import pytest
from conftest import Env, git, register
from test_deploy import runner
from test_engine import article, last_block
from test_m1 import decide, subjects_since, task_meta

from work_sheet import cli
from work_sheet.config import load_config
from work_sheet.dispatch import InvalidDecision, build_prompt

REGISTRY = """
[engines.oracle]
role = "coordinator"
description = "常驻服务器"
runners = ["small", "strong"]
resources = ["oracle-db"]

[engines.mac]
role = "worker"
description = "个人电脑"
runners = ["small", "strong", "web"]
resources = ["local-files"]
"""

RESEARCH = {"action": "register", "reason": "r", "tasks": [
    {"skill": "research", "title": "搜集资料", "brief": "围绕 X 搜集", "capture": []}]}


class Engines:
    """Two engines on one vault: the coordinator `oracle` (no web runner) and the worker `mac`."""

    def __init__(self, env: Env):
        self.env = env
        (env.tmp / "engines.toml").write_text(REGISTRY)
        (env.tmp / "oracle-runners.toml").write_text(
            f"[runners.small]\n{runner('dispatcher.py')}[runners.strong]\n{runner('librarian.py')}")
        (env.tmp / "mac-runners.toml").write_text(
            f"[runners.small]\n{runner('dispatcher.py')}[runners.strong]\n{runner('librarian.py')}"
            f"[runners.web]\n{runner('researcher.py')}")

    def args(self, engine: str) -> list[str]:
        return [*self.env.dirs(), "--engines-file", str(self.env.tmp / "engines.toml"), "--engine-id", engine,
                "--runners-file", str(self.env.tmp / f"{engine}-runners.toml"),
                "--state-dir", str(self.env.tmp / f"state-{engine}")]

    def tick(self, engine: str) -> None:
        cli.main(["tick", *self.args(engine)])

    def config(self, engine: str = "oracle"):
        return load_config(self.env.tmp / "config", self.env.tmp / "roles", self.env.tmp / "skills",
                           self.env.tmp / f"state-{engine}", None, self.env.tmp / "vault-template",
                           self.env.tmp / f"{engine}-runners.toml", None, self.env.tmp / "engines.toml", engine)


@pytest.fixture
def engines(seeded: Env) -> Engines:
    return Engines(seeded)


def last_commit_files(env: Env) -> set[str]:
    return set(git(env.remote, "-c", "core.quotepath=false", "show", "--name-only", "--format=", "main").split())


def test_single_engine_notes_have_no_engine(seeded: Env):
    src = article(seeded)
    seeded.push({"inbox/a.md": f"收录 {src}\n"})
    seeded.respond(register(capture=[str(src)]))
    seeded.tick()
    seeded.pull()
    [task] = seeded.new_tasks()
    assert task["status"] == "done" and "engine" not in task and task["logged"] is True
    assert task["files_created"] == ["wiki/concepts/概念甲.md", "wiki/sources/样例文章.md"]


def test_routing_prompt_and_validation(engines: Engines):
    cfg = engines.config()
    assert [s.name for s in cfg.dispatchable()] == ["digest", "ingest", "local-ingest", "research", "restructure",
                                                     "update-project"]  # warehouse-query: nobody has its resource
    prompt = build_prompt(cfg, "inbox/a.md", "", "hi", [], None)
    assert ("# Engines\n\n- oracle（coordinator）：常驻服务器；资源：oracle-db\n"
            "- mac（worker）：个人电脑；资源：local-files\n\n# Skills\n") in prompt
    assert "- ingest（librarian）：把一份材料收录进知识库\n" in prompt
    assert "- research（researcher）：联网搜集资料；可运行于：mac\n" in prompt
    assert "- local-ingest（librarian）：收录本机文件；可运行于：mac\n" in prompt
    assert '"force": false, "engine": null}' in prompt
    assert decide(cfg, {"skill": "ingest", "project": None}, [])["tasks"][0]["engine"] == "oracle"
    assert decide(cfg, {"skill": "ingest", "project": None, "engine": "mac"}, [])["tasks"][0]["engine"] == "mac"
    assert decide(cfg, {"skill": "local-ingest", "project": None}, [])["tasks"][0]["engine"] == "mac"
    with pytest.raises(InvalidDecision, match="skill research 不能在引擎 oracle 上运行，可选：mac$"):
        decide(cfg, {"skill": "research", "project": None, "engine": "oracle"}, [])


def test_research_runs_on_the_worker_and_the_coordinator_relays(engines: Engines, monkeypatch):
    env = engines.env
    notes = env.tmp / "本机笔记.md"
    notes.write_text("# 本机笔记\n\n" + "笔记内容。" * 100)
    monkeypatch.setenv("WS_RESEARCH_OUT", str(env.tmp / "research.json"))
    monkeypatch.setenv("WS_RESEARCH_CAPTURE", json.dumps(["https://example.com/a"]))
    research = json.loads(json.dumps(RESEARCH))
    research["tasks"][0]["capture"] = [str(notes)]
    env.push({"inbox/a.md": "围绕 X 扩展研究\n"})
    env.respond(research, {"action": "ignore", "reason": "不需要后续"})

    engines.tick("oracle")
    env.pull()
    [task] = env.new_tasks()
    tid = task["id"]
    assert task["engine"] == "mac" and task["status"] == "ready" and task["context"] == []
    assert last_block(env.text("inbox/a.md")) == [f"> [!task] [[{tid}]] · researcher · ready",
                                                  "> 搜集资料（等待 mac 引擎执行）"]
    assert not list(env.tmp.glob("state-oracle/vault/runs/T-*"))  # the coordinator did not run it

    head = env.remote_head()
    engines.tick("mac")
    env.pull()
    task = task_meta(env, tid)
    assert task["status"] == "done" and task["claimed_at"] and task["logged"] is False
    assert task["context"][0].startswith("raw/") and (env.human / task["context"][0] / "source.md").exists()
    assert task["relay"] == "pending" and task["files_created"] == [f"work/{tid}/搜集记录.md"]
    assert subjects_since(env, head) == [f"task({tid}): running", f"task({tid}): 搜集资料"]
    assert last_commit_files(env) == {f"work/{tid}/搜集记录.md", f"tasks/{tid}.md", "inbox/a.md"}
    assert env.dispatch_calls() == 1  # the worker never asks the dispatcher

    head = env.remote_head()
    engines.tick("oracle")
    env.pull()
    assert subjects_since(env, head) == [f"post: {tid}", f"relay: {tid}"]
    task = task_meta(env, tid)
    assert task["logged"] is True and task["relay"] == "done"
    ledger = [json.loads(line) for line in (env.tmp / "state-mac/vault/usage.jsonl").read_text().splitlines()]
    assert [(line["engine"], line["kind"]) for line in ledger] == [("mac", "task")]


def test_worker_task_is_logged_by_the_coordinator_and_dedupes(engines: Engines, monkeypatch):
    env = engines.env
    src = article(env)
    ingest = register(capture=[str(src)], skill="local-ingest")
    env.push({"inbox/a.md": f"收录 {src}\n"})
    env.respond(ingest)
    engines.tick("oracle")
    engines.tick("mac")
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "done" and task["logged"] is False and task["engine"] == "mac"
    assert "wiki/log.md" not in last_commit_files(env) and not (env.human / "wiki/sources/_index.md").exists()

    engines.tick("oracle")
    env.pull()
    assert task_meta(env, task["id"])["logged"] is True
    assert "## " in env.text("wiki/log.md") and "local-ingest | 收录样例文章" in env.text("wiki/log.md")
    assert env.text("wiki/sources/_index.md") == "# 来源摘要\n\n- [[样例文章]] — 一篇样例文章的摘要\n"

    env.listing.unlink()
    env.push({"inbox/b.md": f"再收录一次 {src}\n"})
    engines.tick("oracle")
    engines.tick("mac")
    env.pull()
    again = env.new_tasks()[-1]
    assert again["status"] == "done" and again["attempts"] == 0 and not env.listing.exists()
    assert last_block(env.text("inbox/b.md"))[1] == "> 这份材料已经收录过：[[样例文章]]。如需重新整理，请说明要重做。"


def test_worker_leaves_global_work_alone(engines: Engines):
    env = engines.env
    (env.tmp / "vault-template" / "wiki").mkdir(parents=True)
    (env.tmp / "vault-template" / "wiki" / "topics.md").write_text("- 投资 — 下注\n")
    env.push({"inbox/a.md": "新的要求\n"})
    head = env.remote_head()
    engines.tick("mac")
    assert env.remote_head() == head and env.dispatch_calls() == 0


def test_reconcile_running_tasks_across_engines(engines: Engines):
    env = engines.env
    now = datetime.now().astimezone()

    def note(tid: str, minutes_ago: float) -> str:
        claimed = (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        return (f"---\nid: {tid}\ntitle: t\nstatus: running\nrole: librarian\nskill: ingest\nengine: mac\n"
                f'thread: "[[r]]"\nclaimed_at: "{claimed}"\n---\n## 简报\nx\n\n## 记录\n')

    env.push({"tasks/T-20200103-001.md": note("T-20200103-001", 3),
              "tasks/T-20200103-002.md": note("T-20200103-002", 0.5),
              "inbox/r.md": "做事\n\n> [!task] [[T-20200103-001]] · librarian · running\n> t\n\n"
                            "> [!task] [[T-20200103-002]] · librarian · running\n> t\n"})
    engines.tick("oracle")  # librarian timeout is 1 minute: 3 minutes is overdue, 30 seconds is not
    env.pull()
    assert task_meta(env, "T-20200103-001")["status"] == "blocked"
    assert task_meta(env, "T-20200103-002")["status"] == "running"
    assert "> 引擎 mac 失联或超时未完成。确认它不再运行这个任务后，把卡片拖回 ready 可重跑。" in env.text("inbox/r.md")

    engines.tick("mac")  # the worker's own task, still running at its tick start, was interrupted
    env.pull()
    assert task_meta(env, "T-20200103-002")["status"] == "failed"


@pytest.mark.parametrize("registry, engine_id, message", [
    (REGISTRY, None, "WORKSHEET_ENGINE_ID .* is required"),
    (REGISTRY, "pc", "engine id 'pc' is not in .*; known: oracle, mac"),
    (REGISTRY.replace('"worker"', '"coordinator"'), "mac",
     r'exactly one engine must have role = "coordinator" \(found 2\)'),
    (REGISTRY.replace('"worker"', '"helper"'), "mac", r"engines \['mac'\] need role"),
])
def test_registry_errors(engines: Engines, registry, engine_id, message):
    (engines.env.tmp / "engines.toml").write_text(registry)
    with pytest.raises(SystemExit, match=message):
        load_config(engines.env.tmp / "config", engines.env.tmp / "roles", engines.env.tmp / "skills",
                    engines.env.state, None, engines.env.tmp / "vault-template", None, None,
                    engines.env.tmp / "engines.toml", engine_id)


def test_eval_routing_with_engines(env: Env, capsys):
    cases = env.tmp / "routing.jsonl"
    case = {"id": "web", "text": "搜一下 X", "engines": REGISTRY,
            "expect": {"action": "register", "skills": ["research"], "engines": ["mac"]}}
    cases.write_text(json.dumps(case, ensure_ascii=False) + "\n")
    wrong = json.loads(json.dumps(RESEARCH))
    wrong["tasks"][0]["skill"] = "ingest"
    env.respond(RESEARCH, wrong)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["eval-routing", "--cases", str(cases), "--runs", "2", *env.dirs()])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.splitlines()[:3] == [
        "web 1/2", "  run 2: register ingest(project=None, engine=oracle)",
        "total 1/2 runs passed; 0 case(s) passed fewer than half of their runs"]
