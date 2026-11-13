import json

import pytest
from conftest import Env, git, make_config, register
from test_engine import article, last_block, run_failing

from work_sheet.dispatch import InvalidDecision, parse_decision
from work_sheet.engine import STALE_RUNNING
from work_sheet.vault import parse_frontmatter


def task_meta(env: Env, task_id: str) -> dict:
    return parse_frontmatter(env.text(f"tasks/{task_id}.md"))[0]


def set_status(env: Env, task_id: str, old: str, new: str) -> None:
    env.pull()
    path = f"tasks/{task_id}.md"
    env.push({path: env.text(path).replace(f"\nstatus: {old}\n", f"\nstatus: {new}\n", 1)})


def subjects_since(env: Env, head: str) -> list[str]:
    return git(env.remote, "log", "--reverse", "--format=%s", f"{head}..main").splitlines()


def test_followup_with_parent(seeded: Env):
    env = seeded
    src = article(env)
    env.push({"inbox/a.md": f"收录 {src}\n"})
    env.respond(register(capture=[str(src)]))
    env.tick()
    env.pull()
    [first] = env.new_tasks()

    env.push({"inbox/a.md": env.text("inbox/a.md") + "\n再补充一下概念甲\n"})
    followup = register(capture=[], title="补充概念甲")
    followup["tasks"][0]["parent"] = first["id"]
    env.respond(followup)
    env.tick()
    env.pull()

    second = env.new_tasks()[1]
    assert second["parent"] == first["id"] and second["depth"] == 1 and second["status"] == "done"
    assert second["outputs"] == []  # the follow-up rewrote the same pages unchanged
    prompt = (env.state / "vault" / "runs" / "dispatch-2" / "prompt.md").read_text()
    assert f"- {first['id']} · ingest（librarian） · done · 收录样例文章\n  - wiki/concepts/概念甲.md\n" in prompt


def decide(cfg, task: dict, existing: list[dict]) -> dict:
    base = {"skill": "update-project", "title": "t", "brief": "b", "capture": [], "project": "alpha"}
    return parse_decision(json.dumps({"action": "register", "reason": "r", "tasks": [base | task]}), cfg, existing)


def test_decision_validation_parent_and_project(env: Env):
    cfg = make_config(env.tmp)
    existing = [{"id": "T-1", "depth": 0}, {"id": "T-5", "depth": 5}]
    assert decide(cfg, {"parent": "T-1"}, existing)["tasks"][0]["depth"] == 1
    with pytest.raises(InvalidDecision, match="不是本线程已有的任务"):
        decide(cfg, {"parent": "T-9"}, existing)
    with pytest.raises(InvalidDecision, match="接力深度超过上限"):
        decide(cfg, {"parent": "T-5"}, existing)
    with pytest.raises(InvalidDecision, match="skill update-project 需要指定 project"):
        decide(cfg, {"project": None}, existing)
    for bad in ["", "a/b", "a\\b", ".hidden", 3]:
        with pytest.raises(InvalidDecision, match="project"):
            decide(cfg, {"project": bad}, existing)
    assert decide(cfg, {"skill": "ingest", "project": None}, existing)["tasks"][0]["role"] == "librarian"


def test_reconcile_stale_running_and_ready_question(seeded: Env):
    env = seeded
    note = '---\nid: {id}\ntitle: {title}\nstatus: {status}\nrole: {role}\nthread: "[[r]]"\n---\n## 简报\nx\n\n## 记录\n'
    env.push({
        "tasks/T-20200102-001.md": note.format(id="T-20200102-001", title="收录", status="running", role="librarian"),
        "tasks/T-20200102-002.md": note.format(id="T-20200102-002", title="问", status="ready", role="dispatcher"),
        "inbox/r.md": "做点事\n\n> [!task] [[T-20200102-001]] · librarian · running\n> 收录\n\n"
                      "> [!task] [[T-20200102-002]] · dispatcher · blocked\n> 哪个项目？\n",
    })
    head = env.remote_head()
    env.tick()
    env.pull()
    assert subjects_since(env, head) == ["reconcile: task statuses"]
    assert git(env.remote, "log", "-1", "--format=%(trailers:key=Task,valueonly,separator=%x2C)|"
               "%(trailers:key=Role,valueonly,separator=,)") == "T-20200102-001,T-20200102-002|engine\n"
    assert task_meta(env, "T-20200102-001")["status"] == "failed"
    assert task_meta(env, "T-20200102-002")["status"] == "blocked"
    assert "提问类任务不能运行，请在线程页里回答" in env.text("tasks/T-20200102-002.md")
    assert env.text("inbox/r.md") == (
        f"做点事\n\n> [!task] [[T-20200102-001]] · librarian · failed\n> {STALE_RUNNING}\n\n"
        "> [!task] [[T-20200102-002]] · dispatcher · blocked\n> 哪个项目？\n")

    head = env.remote_head()
    env.tick()
    assert env.remote_head() == head


def test_human_sets_failed_task_ready_reruns(seeded: Env, monkeypatch):
    env = seeded
    task = run_failing(env, "deadlink", monkeypatch)
    monkeypatch.setenv("WS_MODE", "ok")
    set_status(env, task["id"], "failed", "ready")
    head = env.remote_head()
    env.tick()
    env.pull()
    tid = task["id"]
    assert subjects_since(env, head) == ["reconcile: task statuses", f"task({tid}): running",
                                         f"task({tid}): 收录样例文章"]
    meta = task_meta(env, tid)
    assert meta["status"] == "done" and meta["attempts"] == 2
    assert "状态被手动改为 ready" in env.text(f"tasks/{tid}.md")


def test_human_cancels_task(seeded: Env, monkeypatch):
    env = seeded
    task = run_failing(env, "deadlink", monkeypatch)
    set_status(env, task["id"], "failed", "cancelled")
    env.listing.unlink()
    env.tick()
    env.pull()
    block = last_block(env.text("inbox/a.md"))
    assert block[0] == f"> [!task] [[{task['id']}]] · librarian · cancelled"
    assert block[1] == "> 检查未通过："
    assert task_meta(env, task["id"])["status"] == "cancelled" and not env.listing.exists()


def test_recapture_on_retry(seeded: Env):
    env = seeded
    item = "inbox/attachments/notes.md"
    env.push({"inbox/a.md": f"收录 {item}\n"})
    env.respond(register(capture=[item]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "blocked" and task["context"] == []

    env.push({item: "# 笔记\n\n" + "内容。" * 100})
    set_status(env, task["id"], "blocked", "ready")
    env.tick()
    env.pull()
    meta = task_meta(env, task["id"])
    assert meta["status"] == "done" and meta["context"][0].startswith("raw/")
    assert (env.human / meta["context"][0] / "source.md").exists()


def test_two_tasks_have_independent_outcomes(seeded: Env):
    env = seeded
    src = article(env)
    decision = register(capture=[str(src)])
    decision["tasks"].append(decision["tasks"][0] | {"title": "收录缺失文件", "capture": ["/nonexistent/x.md"]})
    env.push({"inbox/a.md": "收录两份材料\n"})
    env.respond(decision)
    env.tick()
    env.pull()
    ok, missing = env.new_tasks()
    assert ok["status"] == "done" and missing["status"] == "blocked"
    page = env.text("inbox/a.md")
    assert f"> [!task] [[{ok['id']}]] · librarian · done" in page
    assert f"> [!task] [[{missing['id']}]] · librarian · blocked\n> 材料采集失败：/nonexistent/x.md：找不到文件" in page


def run_tracker(env: Env, mode: str, monkeypatch) -> dict:
    monkeypatch.setenv("WS_MODE", mode)
    decision = register(capture=[], skill="update-project")
    decision["tasks"][0]["project"] = "alpha"
    env.push({"inbox/a.md": "更新 alpha 项目\n"})
    env.respond(decision)
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    return task


def test_project_log_md_is_append_only(seeded: Env, monkeypatch):
    seeded.push({"projects/alpha/log.md": "## 2026-11-01 旧记录\n"})
    task = run_tracker(seeded, "projlog_rewrite", monkeypatch)
    assert task["status"] == "failed"
    assert "> projects/alpha/log.md 只能追加，不能改动已有内容" in last_block(seeded.text("inbox/a.md"))
    assert seeded.text("projects/alpha/log.md") == "## 2026-11-01 旧记录\n"


def test_project_status_frontmatter_required(seeded: Env, monkeypatch):
    env = seeded
    task = run_tracker(env, "status_missing", monkeypatch)
    assert task["status"] == "failed" and task["project"] == "alpha" and task["role"] == "tracker"
    assert "> projects/alpha/status.md 缺少必填的 frontmatter（type/project/as_of）" in last_block(
        env.text("inbox/a.md"))
    assert not (env.human / "projects").exists()
