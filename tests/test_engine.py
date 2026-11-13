import hashlib
from pathlib import Path

from conftest import Env, git, register

from work_sheet.vault import parse_frontmatter


def article(env: Env) -> Path:
    path = env.tmp / "样例文章.md"
    path.write_text("# 样例文章\n\n" + "正文内容。" * 100)
    return path


def last_block(text: str) -> list[str]:
    return text.rstrip("\n").split("\n\n")[-1].split("\n")


def test_happy_path(seeded: Env):
    env = seeded
    src = article(env)
    env.push({"inbox/a.md": f"请收录这篇文章：{src}\n"})
    env.respond(register(capture=[str(src)]))
    env.tick()
    env.pull()

    [task] = env.new_tasks()
    entry = task["context"][0]
    raw_meta, raw_body = parse_frontmatter(env.text(f"{entry}/source.md"))
    assert entry.startswith("raw/") and raw_meta["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert raw_meta["source"] == str(src) and "正文内容" in raw_body

    assert task["status"] == "done" and task["attempts"] == 1 and task["capture"] == [str(src)]
    assert task["outputs"] == ["wiki/concepts/概念甲.md", "wiki/log.md", "wiki/sources/样例文章.md", "wiki/index.md"]
    assert "- [[样例文章]]" in env.text("wiki/index.md")
    for path in task["outputs"]:
        assert (env.human / path).exists()
    note = env.text(f"tasks/{task['id']}.md")
    assert "派发 → strong" in note and "完成" in note

    assert env.text("inbox/a.md").endswith(
        f"\n\n> [!task] [[{task['id']}]] · librarian · done\n"
        "> 新建 [[样例文章]]、[[概念甲]]。\n"
        ">\n"
        "> 新建：[[wiki/concepts/概念甲|概念甲]]、[[wiki/log|log]]、[[wiki/sources/样例文章|样例文章]]\n"
        "> 修改：[[wiki/index|index]]\n")

    view, *listing = env.listing.read_text().split("\n")
    assert not Path(view).exists()
    assert {"_brief.md", "inbox/a.md", f"{entry}/source.md", "wiki/index.md"} <= set(listing)
    assert not any(p.startswith("tasks/") or p == "inbox/other.md" for p in listing)

    subjects = git(env.human, "log", "--format=%s|%an|%(trailers:key=Task,valueonly,separator=,)|"
                   "%(trailers:key=Role,valueonly,separator=,)", "-3").splitlines()
    tid = task["id"]
    assert subjects == [f"task({tid}): 收录样例文章|worksheet-bot|{tid}|librarian",
                        f"task({tid}): running|worksheet-bot|{tid}|librarian",
                        f"dispatch: a|worksheet-bot|{tid}|dispatcher"]

    prompt = (env.state / "vault" / "runs" / "dispatch-1" / "prompt.md").read_text()
    assert "## Already handled\n\n(none)" in prompt and str(src) in prompt
    assert "- librarian: 维护 wiki/" in prompt and "- dispatcher" not in prompt


def run_failing(env: Env, mode: str, monkeypatch) -> dict:
    monkeypatch.setenv("WS_MODE", mode)
    src = article(env)
    env.push({"inbox/a.md": f"收录 {src}\n"})
    env.respond(register(capture=[str(src)]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    return task


def test_write_scope_violation_fails_and_imports_nothing(seeded: Env, monkeypatch):
    task = run_failing(seeded, "outside", monkeypatch)
    assert task["status"] == "failed" and task["outputs"] == []
    assert not (seeded.human / "practice").exists() and not (seeded.human / "wiki" / "sources").exists()
    assert last_block(seeded.text("inbox/a.md")) == [
        f"> [!task] [[{task['id']}]] · librarian · failed", "> 写入越界：practice/我们的做法.md"]


def test_dead_wikilink_fails_lint(seeded: Env, monkeypatch):
    task = run_failing(seeded, "deadlink", monkeypatch)
    assert task["status"] == "failed"
    assert not (seeded.human / "wiki" / "sources").exists()
    block = "\n".join(last_block(seeded.text("inbox/a.md")))
    assert "检查未通过" in block and "[[不存在的页面]]" in block


def test_capture_failure_blocks_task(seeded: Env):
    env = seeded
    env.push({"inbox/a.md": "收录 /nonexistent/report.pdf\n"})
    env.respond(register(capture=["/nonexistent/report.pdf"]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "blocked" and task["context"] == []
    assert last_block(env.text("inbox/a.md")) == [
        f"> [!task] [[{task['id']}]] · librarian · blocked", "> 材料采集失败：/nonexistent/report.pdf：找不到文件"]
    assert not env.listing.exists()


def test_rebase_conflict_blocks_task(seeded: Env, monkeypatch):
    monkeypatch.setenv("WS_HUMAN", str(seeded.human))
    task = run_failing(seeded, "conflict", monkeypatch)
    assert task["status"] == "blocked" and task["outputs"] == []
    assert not (seeded.human / "wiki" / "sources").exists()
    assert seeded.text("inbox/a.md") == (f"人改了这一页\n\n> [!task] [[{task['id']}]] · librarian · blocked\n"
                                         "> 与远端的改动冲突，产出未导入\n")


def test_pending_detection(seeded: Env):
    env = seeded
    page = "第一个问题\n\n> [!note] dispatcher · 未登记任务\n> 无关\n"
    env.push({"inbox/a.md": page})
    head = env.remote_head()
    env.respond({"action": "ignore", "reason": "仍然无关"})
    env.tick()
    assert env.remote_head() == head and env.dispatch_calls() == 0

    env.push({"inbox/a.md": page + "\n新的问题\n"})
    head = env.remote_head()
    env.tick()
    assert env.dispatch_calls() == 1
    assert git(env.remote, "log", "--format=%s", f"{head}..main").splitlines() == ["dispatch: a"]
    prompt = (env.state / "vault" / "runs" / "dispatch-1" / "prompt.md").read_text()
    assert "## Already handled\n\n第一个问题\n\n> [!note] dispatcher · 未登记任务\n> 无关" in prompt
    assert "## New from the human\n\n新的问题\n" in prompt

    head = env.remote_head()
    env.tick()
    assert env.remote_head() == head and env.dispatch_calls() == 1


def test_invalid_dispatcher_output_twice_asks_human(seeded: Env):
    env = seeded
    env.push({"inbox/a.md": "帮我看看\n"})
    env.respond("not json at all", '{"action": "explode"}')
    env.tick()
    env.pull()
    assert env.dispatch_calls() == 2
    retry_prompt = (env.state / "vault" / "runs" / "dispatch-2" / "prompt.md").read_text()
    assert "# Your previous output was invalid\n\n输出里没有 JSON 对象" in retry_prompt
    [task] = env.new_tasks()
    assert task["role"] == "dispatcher" and task["op"] == "ask" and task["status"] == "blocked"
    block = last_block(env.text("inbox/a.md"))
    assert block[0] == f"> [!task] [[{task['id']}]] · dispatcher · blocked"
    assert block[1].startswith("> 调度失败：action 必须是")


def test_ignore_appends_note(seeded: Env):
    env = seeded
    env.push({"inbox/a.md": "今天天气不错\n"})
    env.respond({"action": "ignore", "reason": "与知识库无关"})
    env.tick()
    env.pull()
    assert env.text("inbox/a.md") == "今天天气不错\n\n> [!note] dispatcher · 未登记任务\n> 与知识库无关\n"
    assert env.new_tasks() == []


def test_empty_remote_is_a_no_op(env: Env):
    env.tick()
    assert git(env.remote, "for-each-ref").strip() == ""
