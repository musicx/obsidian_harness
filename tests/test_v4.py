import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
from conftest import Env, git, make_config, register
from test_engine import article, last_block, run_failing
from test_m1 import decide, subjects_since, task_meta

from work_sheet import cli
from work_sheet.context import candidates_pack, changes_pack, health_pack, load_pages, name_key, names_pack
from work_sheet.dispatch import InvalidDecision
from work_sheet.generated import regenerate
from work_sheet.lint import lint
from work_sheet.vault import raw_dir


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


# --- decisions -------------------------------------------------------------------------------------------

def test_decision_skill_validation(env: Env):
    cfg = make_config(env.tmp)
    with pytest.raises(InvalidDecision, match="skill 'nope' 不存在，可用的有：digest、ingest、research、restructure、"
                                              "update-project$"):
        decide(cfg, {"skill": "nope"}, [])
    with pytest.raises(InvalidDecision, match="可用的有"):  # shared skills are not dispatchable
        decide(cfg, {"skill": "wiki-style"}, [])
    with pytest.raises(InvalidDecision, match="skill ingest 属于角色 librarian"):
        decide(cfg, {"skill": "ingest", "role": "tracker"}, [])
    with pytest.raises(InvalidDecision, match="force"):
        decide(cfg, {"force": "yes"}, [])
    task = decide(cfg, {"skill": "research", "project": None}, [])["tasks"][0]
    assert task["role"] == "researcher"


def test_name_key_and_raw_dir():
    assert name_key("Large Language Models (LLM)") == "large language model"
    assert name_key("Kelly-Criterion（凯利）") == "kelly criterion"
    assert name_key("GPUs") == "gpu" and name_key("gas") == "gas"
    for value in ["raw/2026/11/x", "[[raw/2026/11/x/source|原文]]", "[[raw/2026/11/x/source.md]]", "raw/2026/11/x/"]:
        assert raw_dir(value) == "raw/2026/11/x"


# --- skills in the view ----------------------------------------------------------------------------------

def test_skill_read_patterns_only_for_that_skill(seeded: Env, monkeypatch):
    env = seeded
    monkeypatch.setenv("WS_MODE", "noop")
    env.push({"projects/alpha/status.md": "---\ntype: project-status\nproject: alpha\nas_of: 2026-11-01\n---\n",
              "projects/alpha/log.md": "日志\n", "inbox/a.md": "做个跨项目摘要\n"})
    env.respond(register(capture=[], skill="digest"))
    env.tick()
    listing = env.listing.read_text().split("\n")
    assert "projects/alpha/status.md" in listing and "projects/alpha/log.md" not in listing

    env.pull()
    env.push({"inbox/b.md": "收录点什么\n"})
    env.respond(register(capture=[]))
    env.tick()
    assert not any(p.startswith("projects/") for p in env.listing.read_text().split("\n"))


# --- context packs ---------------------------------------------------------------------------------------

PACK_VAULT = {
    "wiki/sources/kelly-paper.md": "---\ntype: source\ndescription: Kelly 1956 原文摘要\nraw: raw/2026/11/k\n"
                                   "mentions: [Bankroll management, 资金管理]\n---\n讲 [[Kelly criterion]]。\n",
    "wiki/sources/thorp.md": "---\ntype: source\ndescription: Thorp 的论文\nraw: raw/2026/11/t\n"
                             "mentions: [Bankroll Management, Kelly Criterion]\n---\n正文。\n",
    "wiki/concepts/Kelly criterion.md": "---\ntype: concept\ndescription: 按优势下注的比例\naliases: [凯利公式]\n"
                                        "sources: ['[[kelly-paper]]']\n---\n最大化对数增长。\n",
    "wiki/concepts/Kelly Criterions.md": "---\ntype: concept\ndescription: 重复页\nsources: ['[[thorp]]']\n---\n重复。\n",
    "wiki/concepts/对数效用.md": "---\ntype: concept\nsources: ['[[thorp]]']\nupdated: 2020-01-01\n---\n"
                               "## 定义\n\n对数效用函数。\n\n> [!conflict] 两个来源说法不同\n",
    "wiki/concepts/_index.md": "# 概念\n",
    "practice/仓位管理.md": "---\ntype: practice\nowner: human\ndescription: 我们怎么定仓位\n---\n参考 [[Kelly criterion]]。\n",
    "inbox/t.md": "[[Kelly Criterions]] [[对数效用]]\n",
}


def test_names_and_candidates_packs(tmp_path):
    write_files(tmp_path, PACK_VAULT)
    pages = load_pages(tmp_path)
    assert {p.path for p in pages} == {p for p in PACK_VAULT if not p.endswith("_index.md")
                                       and not p.startswith("inbox/")}
    names = names_pack(pages)
    assert "| Kelly criterion | concept | 凯利公式 | 按优势下注的比例 |" in names
    assert "| 对数效用 | concept |  | 定义 |" in names  # no description: first body line without markers

    pack = candidates_pack(pages, "这篇文章讨论凯利公式，以及如何控制仓位。")
    rows = [line for line in pack.split("\n") if line.startswith("| wiki/") or line.startswith("| practice/")]
    assert rows[0].startswith("| wiki/concepts/Kelly criterion.md |") and rows[0].endswith("| 名称出现在材料中 |")
    assert any(r.startswith("| practice/仓位管理.md |") and r.endswith("| 关键词相关 |") for r in rows)
    assert "## 形成中的概念\n\n- Bankroll management：[[kelly-paper]]、[[thorp]]\n- 资金管理：[[kelly-paper]]\n" in pack
    assert "Kelly Criterion：" not in pack  # mentioned concept that already has a page

    latin = candidates_pack(pages, "unkelly criterionx")  # Latin names only count as whole words
    assert "名称出现在材料中" not in latin


def test_health_pack(tmp_path):
    write_files(tmp_path, PACK_VAULT)
    pack = health_pack(tmp_path, load_pages(tmp_path), date(2026, 11, 13))
    section = {s.split("\n", 1)[0]: s.split("\n", 1)[1].strip() for s in pack.split("## ")[1:]}
    assert section["名称相近的页面"] == "- wiki/concepts/Kelly Criterions.md ↔ wiki/concepts/Kelly criterion.md"
    assert section["可升格的概念"] == "- Bankroll management：2 个来源（[[kelly-paper]]、[[thorp]]）"
    # inbox/ links do not count; Kelly criterion is linked from a source and a practice page.
    assert section["孤儿页"] == "- wiki/concepts/Kelly Criterions.md\n- wiki/concepts/对数效用.md"
    assert section["缺少描述的页面"] == "- wiki/concepts/对数效用.md"
    assert section["带冲突标记的页面"] == "- wiki/concepts/对数效用.md"
    assert section["长期未更新的页面"] == "- wiki/concepts/对数效用.md（updated: 2020-01-01）"


def test_changes_pack(tmp_path):
    def commit(files: dict[str, str], message: str) -> None:
        write_files(tmp_path, files)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message],
                       cwd=tmp_path, check=True)

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    commit({"wiki/concepts/a.md": "a\n"}, "human edit")
    commit({"wiki/concepts/凯利.md": "k\n", "wiki/concepts/_index.md": "i\n", "tasks/T-1.md": "t\n",
            "raw/x/source.md": "r\n"}, "task(T-1): 收录\n\nTask: T-1\nRole: librarian")
    commit({"wiki/concepts/凯利.md": "k2\n"}, "task(T-2): 补充\n\nTask: T-2\nRole: librarian")
    commit({"tasks/T-3.md": "t\n"}, "task(T-3): running\n\nTask: T-3\nRole: librarian")
    pack = changes_pack(tmp_path)
    assert pack == (f"# 最近 14 天的改动\n\n## {date.today()}\n\n"
                    "- T-2：task(T-2): 补充\n  - 修改 wiki/concepts/凯利.md\n"
                    "- T-1：task(T-1): 收录\n  - 新建 wiki/concepts/凯利.md\n")


# --- generated files -------------------------------------------------------------------------------------

def test_regenerate_indexes(tmp_path):
    write_files(tmp_path, {
        "wiki/topics.md": "# 主题\n\n- 投资 — 下注与仓位\n- 空主题 — 没有页面\n",
        "wiki/concepts/b概念.md": "---\ntype: concept\ndescription: B\ntopics: [投资]\n---\n",
        "wiki/concepts/A概念.md": "---\ntype: concept\ndescription: A\n---\n",
        "wiki/sources/s.md": "---\ntype: source\ndescription: S\ntopics: [投资]\n---\n",
        "practice/做法.md": "---\ntype: practice\ndescription: 我们的做法\n---\n",
        "projects/beta/status.md": "---\nproject: Beta\naliases: [B计划]\ndescription: 第二个|项目\nhealth: green\n"
                                   "as_of: 2026-11-01\n---\n",
        "projects/alpha/status.md": "---\nproject: alpha\nhealth: red\nas_of: 2026-10-01\n---\n",
    })
    regenerate(tmp_path)
    assert (tmp_path / "wiki/concepts/_index.md").read_text() == "# 概念\n\n- [[A概念]] — A\n- [[b概念]] — B\n"
    assert not (tmp_path / "wiki/entities/_index.md").exists()  # no such directory in this vault
    assert (tmp_path / "wiki/index.md").read_text() == (
        "# 知识库目录\n\n来源 1 篇 · 概念 2 个 · 实体 0 个 · 做法 1 篇\n\n## 按主题\n\n"
        "### 投资\n\n下注与仓位\n\n[[b概念]]\n\n### 未归类\n\n[[A概念]]\n\n"
        "## 我们的做法\n\n- [[practice/做法|做法]] — 我们的做法\n\n"
        "## 目录与日志\n\n- [[wiki/sources/_index|来源摘要]]\n- [[wiki/concepts/_index|概念]]\n"
        "- [[wiki/entities/_index|实体]]\n- [[wiki/log|操作日志]]\n")
    assert (tmp_path / "projects/_index.md").read_text() == (
        "# 项目名册\n\n| 项目 | 别名 | 说明 | 状态 | 截至 |\n|---|---|---|---|---|\n"
        "| [[projects/alpha/status\\|alpha]] |  |  | red | 2026-10-01 |\n"
        "| [[projects/beta/status\\|Beta]] | B计划 | 第二个\\|项目 | green | 2026-11-01 |\n")


# --- lint ------------------------------------------------------------------------------------------------

def run_lint(old: dict[str, str], new: dict[str, str], allow_shrink: bool = False) -> list[str]:
    created = sorted(set(new) - set(old))
    modified = sorted(p for p in set(new) & set(old) if new[p] != old[p])
    tree = set(old) | set(new)
    return lint(created, modified, tree, lambda p: new.get(p, old.get(p)), lambda p: old[p], allow_shrink)


BASE = {
    "raw/2026/11/k/source.md": "原文\n",
    "wiki/topics.md": "- 投资 — 下注\n",
    "wiki/concepts/Kelly criterion.md": "---\ntype: concept\ndescription: 下注比例\naliases: [凯利公式]\n"
                                        "sources: ['[[k]]']\n---\n" + "很长的正文。" * 20 + "\n",
    "wiki/sources/k.md": "---\ntype: source\ndescription: 原文摘要\nraw: '[[raw/2026/11/k/source|原文]]'\n---\n摘要\n",
}


def test_lint_description_topics_raw():
    assert run_lint(BASE, {"wiki/sources/new.md": "---\ntype: source\nraw: raw/2026/11/k\ntopics: [投资, 赌博]\n---\n",
                           "practice/p.md": "---\ntype: practice\nowner: human\ndescription: " + "长" * 201 + "\n---\n",
                           "wiki/sources/bad.md": "---\ntype: source\ndescription: d\nraw: raw/2026/11/none\n---\n"}) == [
        "practice/p.md 缺少 description（一句话说明这里有什么、什么时候该读），或超过 200 字",
        "wiki/sources/bad.md 的 raw 未指向已存档的原文",
        "wiki/sources/new.md 缺少 description（一句话说明这里有什么、什么时候该读），或超过 200 字",
        "wiki/sources/new.md 使用了未登记的主题：赌博（先在 wiki/topics.md 登记）",
    ]
    # A topic registered in the same change is accepted.
    assert run_lint(BASE, {"wiki/topics.md": "- 投资 — 下注\n- 赌博 — 概率\n",
                           "wiki/sources/new.md": "---\ntype: source\ndescription: d\nraw: raw/2026/11/k\n"
                                                  "topics: [赌博]\n---\n"}) == []


def test_lint_normalized_name_collision():
    page = "---\ntype: concept\ndescription: d\nsources: ['[[k]]']\n---\n正文\n"
    assert run_lint(BASE, {"wiki/concepts/Kelly Criterions.md": page, "wiki/entities/凯利公式（公式）.md": page}) == [
        "wiki/concepts/Kelly Criterions.md 与已有页面 wiki/concepts/Kelly criterion.md 重名或近似，请并入已有页面",
        "wiki/entities/凯利公式（公式）.md 与已有页面 wiki/concepts/Kelly criterion.md 重名或近似，请并入已有页面",
    ]


def test_lint_shrink_guard():
    shrunk = {"wiki/concepts/Kelly criterion.md": BASE["wiki/concepts/Kelly criterion.md"].split("很长")[0] + "短。\n"}
    assert run_lint(BASE, shrunk) == ["wiki/concepts/Kelly criterion.md 的内容缩减超过 30%，疑似丢失已有内容"]
    assert run_lint(BASE, shrunk, allow_shrink=True) == []


# --- skip already-ingested sources -----------------------------------------------------------------------

def test_already_ingested_source_is_skipped_unless_forced(seeded: Env):
    env = seeded
    src = str(article(env))
    env.push({"inbox/a.md": f"收录 {src}\n"})
    env.respond(register(capture=[src]))
    env.tick()
    env.pull()

    env.listing.unlink()
    env.push({"inbox/b.md": f"再收录一次 {src}\n"})
    env.tick()
    env.pull()
    first, again = env.new_tasks()
    assert again["status"] == "done" and again["attempts"] == 0 and again["context"] == first["context"]
    summary = "这份材料已经收录过：[[样例文章]]。如需重新整理，请说明要重做。"
    assert last_block(env.text("inbox/b.md")) == [f"> [!task] [[{again['id']}]] · librarian · done", f"> {summary}"]
    assert summary in env.text(f"tasks/{again['id']}.md") and not env.listing.exists()

    forced = register(capture=[src])
    forced["tasks"][0]["force"] = True
    env.respond(forced)
    env.push({"inbox/c.md": f"重做 {src}\n"})
    env.tick()
    env.pull()
    redo = env.new_tasks()[-1]
    assert redo["force"] is True and redo["status"] == "done" and redo["attempts"] == 1 and env.listing.exists()


# --- scaffold --------------------------------------------------------------------------------------------

def test_scaffold_copies_missing_template_files_once(seeded: Env):
    env = seeded
    write_files(env.tmp / "vault-template", {"wiki/topics.md": "- 投资 — 下注\n",
                                             "wiki/concepts/_index.md": "# 占位\n",
                                             "inbox/other.md": "模板里的页面\n"})
    env.push({"wiki/sources/s.md": "---\ntype: source\ndescription: 一份来源\n---\n"}, regenerated=False)
    head = env.remote_head()
    env.tick()
    assert subjects_since(env, head) == ["scaffold: vault files"]
    assert git(env.remote, "log", "-1", "--format=%(trailers:key=Role,valueonly,separator=%x2C)").strip() == "engine"
    env.pull()
    assert env.text("wiki/topics.md") == "- 投资 — 下注\n"
    assert env.text("wiki/concepts/_index.md") == "# 概念\n\n"  # copied, then regenerated
    assert env.text("wiki/sources/_index.md") == "# 来源摘要\n\n- [[s]] — 一份来源\n"
    assert env.text("inbox/other.md").startswith("旧的要求")  # existing files are never overwritten

    head = env.remote_head()
    env.tick()
    assert env.remote_head() == head


# --- routing eval ----------------------------------------------------------------------------------------

def test_eval_routing(env: Env, capsys):
    cases = env.tmp / "routing.jsonl"
    cases.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in [
        {"id": "ingest-url", "text": "收录 https://example.com/a", "expect": {"action": "register", "skills": ["ingest"]}},
        {"id": "status", "text": "alpha 周报", "projects": "| alpha |",
         "tasks": [{"id": "T-1", "skill": "ingest", "role": "librarian", "status": "done", "title": "旧", "outputs": []}],
         "expect": {"action": "register", "skills": ["update-project"], "project": "alpha"}},
    ]) + "\n")
    ingest = register(capture=["https://example.com/a"])
    update = register(capture=[], skill="update-project")
    update["tasks"][0]["project"] = "beta"
    env.respond(ingest, ingest, update, {"action": "ignore", "reason": "x"})
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["eval-routing", "--cases", str(cases), "--runs", "2", *env.dirs()])
    out = capsys.readouterr().out
    assert exit_info.value.code == 1
    assert out.splitlines() == [
        "ingest-url 2/2", "status 0/2", "  run 1: register update-project(project=beta)", "  run 2: ignore",
        "total 2/4 runs passed; 1 case(s) passed fewer than half of their runs"]
    prompt = (env.state / "vault" / "runs" / "eval" / "status-1" / "prompt.md").read_text()
    assert "# Projects\n\n| alpha |" in prompt and "- T-1 · ingest（librarian） · done · 旧" in prompt

    env.respond(ingest)
    cases.write_text(cases.read_text().splitlines()[0] + "\n")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["eval-routing", "--cases", str(cases), "--runs", "3", *env.dirs()])
    assert exit_info.value.code == 0


# --- fixes from the first real-model run -----------------------------------------------------------------

def test_source_page_may_share_a_concept_name_with_a_qualifier():
    source = "---\ntype: source\ndescription: d\nraw: raw/2026/11/k\n---\n摘要\n"
    assert run_lint(BASE, {"wiki/sources/Kelly criterion (Wikipedia).md": source}) == []
    assert run_lint(BASE, {"wiki/sources/kelly CRITERION.md": source}) == [
        "wiki/sources/kelly CRITERION.md 与已有页面 wiki/concepts/Kelly criterion.md 重名或近似，请并入已有页面"]


def test_failed_checks_get_one_repair_pass(seeded: Env, monkeypatch):
    task = run_failing(seeded, "repairable", monkeypatch)
    assert task["status"] == "done" and task["attempts"] == 1
    assert seeded.text("wiki/sources/样例文章.md").endswith("要点。\n")
    assert not (seeded.human / "_feedback.md").exists()
    note = seeded.text(f"tasks/{task['id']}.md")
    assert "检查未通过，已要求修正一次" in note and note.index("修正一次") < note.index("完成")
    repair = (seeded.state / "vault" / "runs" / task["id"] / "repair" / "prompt.md").read_text()
    assert repair == "上一次的产出没有通过检查，问题列在 _feedback.md。只修正这些问题，不要改动其他内容；修正后更新 _report.md。"
    assert last_block(seeded.text("inbox/a.md"))[1] == "> 修正了链接。"


def test_repair_that_does_not_fix_fails(seeded: Env, monkeypatch):
    task = run_failing(seeded, "deadlink", monkeypatch)
    assert task["status"] == "failed" and task["attempts"] == 1
    assert (seeded.state / "vault" / "runs" / task["id"] / "repair" / "stdout.log").exists()
    assert "检查未通过，已要求修正一次" in seeded.text(f"tasks/{task['id']}.md")
    assert last_block(seeded.text("inbox/a.md"))[1:] == [
        "> 检查未通过：", "> wiki/sources/样例文章.md：链接 [[不存在的页面]] 找不到目标"]


def test_dispatcher_outage_creates_nothing(seeded: Env):
    env = seeded
    env.push({"inbox/a.md": "今天天气不错\n"})
    head = env.remote_head()
    env.respond("EXIT:1")
    env.tick()
    assert env.remote_head() == head and env.dispatch_calls() == 1

    env.respond({"action": "ignore", "reason": "无关"})
    env.tick()
    assert subjects_since(env, head) == ["dispatch: a"]


def test_relay_outage_leaves_relay_pending(seeded: Env, monkeypatch):
    env = seeded
    monkeypatch.setenv("WS_RESEARCH_OUT", str(env.tmp / "research.json"))
    monkeypatch.setenv("WS_RESEARCH_CAPTURE", json.dumps(["https://example.com/a"]))
    env.push({"inbox/a.md": "围绕 X 扩展研究\n"})
    env.respond({"action": "register", "reason": "r", "tasks": [
        {"skill": "research", "title": "搜集资料", "brief": "围绕 X 搜集", "capture": []}]}, "EXIT:1")
    env.tick()
    env.pull()
    [research] = env.new_tasks()
    assert research["status"] == "done" and research["relay"] == "pending" and env.dispatch_calls() == 2

    env.respond({"action": "ignore", "reason": "不需要后续"})
    env.tick()
    env.pull()
    assert task_meta(env, research["id"])["relay"] == "done"


def test_created_file_names_must_sync_everywhere():
    page = "---\ntype: source\ndescription: d\nraw: raw/2026/11/k\n---\n摘要\n"
    message = "{} 的文件名含有不能使用的字符（\" \\ : * ? < > |），请改名"
    bad = ['outputs/Kelly 准则："我有优势".md', "outputs/a:b.md", "outputs/what?.png", "outputs/ lead.md",
           "outputs/trail.md "]
    assert run_lint(BASE, {p: "x\n" for p in bad}) == [message.format(p) for p in sorted(bad)]
    assert run_lint(BASE, {"outputs/2026-11-13 Kelly 准则：全角冒号可以.md": "x\n", "wiki/sources/a b.md": page}) == []
