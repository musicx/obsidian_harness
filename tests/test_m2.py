import json

import pytest
from conftest import Env, make_config, register
from test_engine import article, last_block
from test_m1 import decide, subjects_since, task_meta

from work_sheet.dispatch import InvalidDecision

RESEARCH = {"action": "register", "reason": "需要扩展",
            "tasks": [{"skill": "research", "title": "搜集资料", "brief": "围绕 X 搜集", "capture": []}]}


@pytest.fixture
def researching(seeded: Env, monkeypatch) -> Env:
    """A thread whose first dispatch registers the fake researcher, which reports one capture item."""
    src = article(seeded)
    monkeypatch.setenv("WS_RESEARCH_OUT", str(seeded.tmp / "research.json"))
    monkeypatch.setenv("WS_RESEARCH_CAPTURE", json.dumps([str(src)]))
    seeded.push({"inbox/a.md": "围绕 X 扩展研究\n"})
    return seeded


def test_relay_registers_child_and_runs_it(researching: Env):
    env = researching
    src = str(env.tmp / "样例文章.md")
    head = env.remote_head()
    env.respond(RESEARCH, register(capture=[src]))
    env.tick()
    env.pull()

    research, ingest = env.new_tasks()
    rid, lid = research["id"], ingest["id"]
    assert research["status"] == "done" and research["relay"] == "done"
    assert research["outputs"] == [f"work/{rid}/搜集记录.md"]
    assert research["report_capture"] == [src] and research["report_handoff"][0]["role"] == "librarian"
    assert ingest["parent"] == rid and ingest["depth"] == 1 and ingest["status"] == "done"
    assert "relay" not in ingest
    assert subjects_since(env, head) == [
        "dispatch: a", f"task({rid}): running", f"task({rid}): 搜集资料", f"relay: {rid}",
        f"task({lid}): running", f"task({lid}): 收录样例文章"]

    assert json.loads((env.tmp / "research.json").read_text()) == {
        "argv": ["--system", "You are the researcher."], "tag": "web-runner"}

    prompt = (env.state / "vault" / "runs" / "dispatch-2" / "prompt.md").read_text()
    assert "## New from the human\n\n(none)\n" in prompt
    assert prompt.index("# Existing tasks") < prompt.index("# Finished task") < prompt.index("# Projects")
    assert f"# Finished task\n\n- id: {rid}\n- skill: research\n- role: researcher\n- title: 搜集资料\n" in prompt
    assert "## Report summary\n\n找到 1 份资料。\n" in prompt
    assert "已找到资料，可以收录" in prompt and f"## Capture requests\n\n- {src}\n" in prompt


def test_relay_ignore_adds_no_block(researching: Env):
    env = researching
    env.respond(RESEARCH, {"action": "ignore", "reason": "不需要后续"})
    env.tick()
    env.pull()
    [research] = env.new_tasks()
    assert research["relay"] == "done" and env.dispatch_calls() == 2
    assert "[!note]" not in env.text("inbox/a.md")
    assert last_block(env.text("inbox/a.md"))[0] == f"> [!task] [[{research['id']}]] · researcher · done"


def test_relay_cannot_grant(researching: Env):
    env = researching
    granting = register(capture=[])
    granting["tasks"][0]["grant"] = ["practice/新做法.md"]
    env.respond(RESEARCH, granting)
    env.tick()
    env.pull()
    research, ask = env.new_tasks()
    assert env.dispatch_calls() == 3 and research["relay"] == "done"
    assert ask["role"] == "dispatcher" and ask["status"] == "blocked"
    assert last_block(env.text("inbox/a.md"))[1] == "> 调度失败：接力登记的任务不能授予写权限"


def test_decision_validation_capture_and_grant(env: Env):
    cfg = make_config(env.tmp)
    with pytest.raises(InvalidDecision, match="capture 最多 10 项"):
        decide(cfg, {"capture": [f"/x/{i}.md" for i in range(11)]}, [])
    with pytest.raises(InvalidDecision, match="capture_mode"):
        decide(cfg, {"capture_mode": "some"}, [])
    assert decide(cfg, {}, [])["tasks"][0]["capture_mode"] == "all"
    with pytest.raises(InvalidDecision, match="角色 librarian 不能被授予 inbox/x.md"):
        decide(cfg, {"skill": "ingest", "grant": ["inbox/x.md"]}, [])
    assert decide(cfg, {"skill": "ingest", "grant": ["practice/新.md"]}, [])["tasks"][0]["grant"]


def run_decision(env: Env, decision: dict) -> dict:
    env.push({"inbox/a.md": "处理一下\n"})
    env.respond(decision)
    env.tick()
    env.pull()
    return env.new_tasks()[-1]


def test_capture_any_runs_with_partial_material(seeded: Env):
    src = article(seeded)
    decision = register(capture=[str(src), "/nonexistent/x.md"])
    decision["tasks"][0]["capture_mode"] = "any"
    task = run_decision(seeded, decision)
    assert task["status"] == "done" and len(task["context"]) == 1 and task["capture_mode"] == "any"
    assert task["capture_failed"] == ["/nonexistent/x.md：找不到文件"]
    brief = seeded.listing.with_suffix(".brief.md").read_text()
    assert "\n- 采集失败的材料：\n  - /nonexistent/x.md：找不到文件\n- 共用规范：" in brief
    assert ">\n> 未能采集：/nonexistent/x.md\n> 新建：" in seeded.text("inbox/a.md")


def test_capture_any_all_failing_blocks(seeded: Env):
    decision = register(capture=["/nonexistent/x.md", "/nonexistent/y.md"])
    decision["tasks"][0]["capture_mode"] = "any"
    task = run_decision(seeded, decision)
    assert task["status"] == "blocked" and task["context"] == [] and "capture_failed" not in task
    assert last_block(seeded.text("inbox/a.md"))[1:] == [
        "> 材料采集失败：/nonexistent/x.md：找不到文件", "> 材料采集失败：/nonexistent/y.md：找不到文件"]


def granted(path: str, proposal: str | None = None) -> dict:
    decision = register(capture=[], title="起草做法")
    decision["tasks"][0] |= {"grant": [path]} | ({"proposal": proposal} if proposal else {})
    return decision


def test_grant_new_practice_file(seeded: Env, monkeypatch):
    monkeypatch.setenv("WS_MODE", "practice_new")
    task = run_decision(seeded, granted("practice/新做法.md"))
    assert task["status"] == "done" and task["grant"] == ["practice/新做法.md"]
    assert seeded.text("practice/新做法.md").endswith("我们这样做。\n")


def test_without_grant_practice_write_is_out_of_scope(seeded: Env, monkeypatch):
    monkeypatch.setenv("WS_MODE", "practice_new")
    task = run_decision(seeded, register(capture=[]))
    assert task["status"] == "failed"
    assert last_block(seeded.text("inbox/a.md"))[1] == "> 写入越界：practice/新做法.md"


PRACTICE = "---\ntype: practice\nowner: human\ndescription: 现有做法\n---\n原文。\n"
PROPOSAL = '---\ntype: proposal\ntarget: "[[现有做法]]"\nstatus: proposed\n---\n| # | 建议 |\n'


def test_grant_existing_file_needs_proposal(seeded: Env):
    seeded.push({"practice/现有做法.md": PRACTICE})
    task = run_decision(seeded, granted("practice/现有做法.md"))
    assert task["role"] == "dispatcher" and task["status"] == "blocked"
    assert last_block(seeded.text("inbox/a.md"))[1] == (
        "> 调度失败：修改已有的 practice/现有做法.md 需要一份状态为 proposed 的建议页")


def test_grant_existing_file_with_proposal(seeded: Env, monkeypatch):
    monkeypatch.setenv("WS_MODE", "practice_edit")
    proposal = "proposals/2026-11-12-建议.md"
    seeded.push({"practice/现有做法.md": PRACTICE, proposal: PROPOSAL})
    task = run_decision(seeded, granted("practice/现有做法.md", proposal))
    assert task["status"] == "done" and task["proposal"] == proposal
    assert task_meta(seeded, task["id"])["outputs"] == ["practice/现有做法.md"]
    assert seeded.text("practice/现有做法.md") == PRACTICE + "按建议补充的一段。\n"
    assert proposal in seeded.listing.read_text().split("\n")


def test_proposal_and_practice_frontmatter_lint(seeded: Env, monkeypatch):
    monkeypatch.setenv("WS_MODE", "bad_frontmatter")
    task = run_decision(seeded, granted("practice/新做法.md"))
    assert task["status"] == "failed"
    block = last_block(seeded.text("inbox/a.md"))
    assert "> proposals/2026-11-13-建议.md 的 frontmatter 不合规（type/target/status）" in block
    assert "> practice/新做法.md 的 frontmatter 不合规（type/owner）" in block
    assert not (seeded.human / "practice").exists()
