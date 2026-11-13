"""Run one ready task in an isolated view, check its output, and import it."""

import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from work_sheet.capture import capture_all
from work_sheet.config import Config, Role, Skill
from work_sheet.context import query_text, write_packs
from work_sheet.feeds import is_recent
from work_sheet.generated import append_log, regenerate
from work_sheet.gitops import commit, push, tracked_files
from work_sheet.lint import frontmatter_of, lint, normalize_sql
from work_sheet.runner import run_runner
from work_sheet.usage import Ledger, RunResult, add_up, describe
from work_sheet.vault import (add_log, brief_of, is_generated, now, page_link, parse_frontmatter, raw_dir,
                              read_task, thread_path, update_block, write_task)

log = logging.getLogger(__name__)

TASK_PROMPT = ("阅读当前目录下的 _brief.md 并完成任务。操作规程如下，它引用的文件在 _skill/ 目录下；"
               "结束前按 _brief.md 的格式写好 _report.md。")
ENGINE_FILES = {"_brief.md", "_report.md", "_feedback.md"}
REPAIR_PROMPT = "上一次的产出没有通过检查，问题列在 _feedback.md。只修正这些问题，不要改动其他内容；修正后更新 _report.md。"
ENGINE_DIRS = ("_skill/", "_skills/", "_context/", "_scratch/", ".pi/")  # _scratch/: the agent's working notes
DATATOOL = Path(__file__).with_name("datatool.py")
PROTECTED = ("raw/", "tasks/", "inbox/", "briefings/")
MAX_SUMMARY = 600
CONFLICT = "与远端的改动冲突，产出未导入"

BRIEF = """# 任务 {id}：{title}

- 今天的日期：{today}
- 操作规程：_skill/SKILL.md（已附在提示中）
- 线程页：{thread}
- 本次材料：{context}
{failed}{shared}{packs}{sources}- 可写范围：{write}

## 简报
{brief}

## 结束前
在当前目录写 `_report.md`：

    ---
    status: done        # done | failed | blocked
    ---
    （不超过 300 字，用普通段落，不要用标题：做了什么、结论是什么；需要人回答的问题也写在这里。新建和修改的页面清单不用列，系统会自动附上）

只在可写范围内新建或修改文件；不要执行 git。
"""


def expand(patterns: list[str], meta: dict, thread: str) -> list[str]:
    expanded = []
    for pattern in patterns:
        if pattern == "{context}":
            expanded += [f"{d}/**" for d in meta.get("context") or []]
        elif "{project}" in pattern and not meta.get("project"):
            continue
        else:
            expanded.append(pattern.replace("{thread}", thread).replace("{task}", meta["id"])
                            .replace("{project}", str(meta.get("project") or "")))
    return expanded


def matches(path: str, patterns: list[str]) -> bool:
    return any(PurePosixPath(path).full_match(p) for p in patterns)


def snapshot(view: Path) -> dict[str, str]:
    return {p.relative_to(view).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in view.rglob("*") if p.is_file()}


def bullet(label: str, items: list[str]) -> str:
    """A brief bullet with one sub-item per entry, or nothing when there are none."""
    return f"- {label}：" + "".join(f"\n  - {i}" for i in items) + "\n" if items else ""


def render_brief(meta: dict, body: str, thread: str, write: list[str], shared: list[str], packs: list[str],
                 sources: list[str]) -> str:
    context = "".join(f"\n  - {d}" for d in meta.get("context") or []) or "（无）"
    return BRIEF.format(id=meta["id"], title=meta["title"], today=f"{now():%Y-%m-%d}", thread=thread,
                        context=context,
                        failed=bullet("采集失败的材料", meta.get("capture_failed") or []),
                        shared=bullet("共用规范", [f"_skills/{s}/SKILL.md" for s in shared]),
                        packs=bullet("参考资料", [f"_context/{p}" for p in packs]),
                        sources=f"- 数据源：{'、'.join(sources)}（通过查询工具 wsdata 访问：list_tables / describe_table / query）\n"
                        if sources else "",
                        write="、".join(write), brief=brief_of(body))


def mount_datatool(cfg: Config, view: Path, sources: list[str]) -> None:
    """Give the run the read-only query tool for these data sources (pi reads the view's .pi/mcp.json)."""
    scratch = view / "_scratch"
    scratch.mkdir(exist_ok=True)
    config = scratch / "datatool.json"
    config.write_text(json.dumps({"sources": {name: cfg.datasources[name] for name in sources},
                                  "log": str(scratch / "queries.jsonl")}, ensure_ascii=False, indent=2))
    (view / ".pi").mkdir(exist_ok=True)
    server = {"command": os.environ.get("WORKSHEET_DATATOOL_PYTHON", "python3"), "args": [str(DATATOOL)],
              # direct: declared to the model like a built-in tool, so a print-mode run can call it
              "env": {"WS_DATATOOL_CONFIG": str(config)}, "exposure": "direct"}
    (view / ".pi" / "mcp.json").write_text(json.dumps({"mcpServers": {"wsdata": server}}, indent=2))


def executed_queries(view: Path) -> set[str]:
    """The successful queries of this run, normalized for comparison with the SQL in the agent's notes."""
    log_file = view / "_scratch" / "queries.jsonl"
    entries = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()] if log_file.exists() else []
    return {normalize_sql(e["sql"]) for e in entries if e.get("ok")}


def engine_owned(path: str) -> bool:
    """Files the engine puts into the view for the run; never imported."""
    return path in ENGINE_FILES or path.startswith(ENGINE_DIRS)


def truncate(text: str, keep_end: bool = False) -> str:
    """Shorten to MAX_SUMMARY at line boundaries, marking the cut with a `…` line."""
    text = text.strip()
    if len(text) <= MAX_SUMMARY:
        return text
    if keep_end:
        lines = text.split("\n")
        while len(lines) > 1 and len("\n".join(lines)) > MAX_SUMMARY:
            lines.pop(0)
        return "…\n" + "\n".join(lines)[-MAX_SUMMARY:]
    cut = text.rfind("\n", 0, MAX_SUMMARY + 1)
    return (text[:cut] if cut != -1 else text[:MAX_SUMMARY]).rstrip() + "\n…"


def output_list(created: list[str], modified: list[str], uncaptured: list[str]) -> str:
    lines = [f"未能采集：{'、'.join(uncaptured)}"] if uncaptured else []
    lines += [f"{label}：" + "、".join(map(page_link, paths))
              for label, paths in (("新建", created), ("修改", modified)) if paths]
    return "\n\n" + "\n".join(lines) if lines else ""


def parse_report(view: Path, stdout: str) -> tuple[str, str, dict]:
    report = view / "_report.md"
    if not report.exists():
        return "done", truncate(stdout, keep_end=True), {}
    try:
        meta, body = parse_frontmatter(report.read_text())
    except (yaml.YAMLError, ValueError):
        meta, body = {}, ""
    if meta.get("status") not in ("done", "failed", "blocked"):
        return "failed", "报告无效：_report.md 缺少合法的 status", {}
    extras = {k: meta[k] for k in ("handoff", "capture") if meta.get(k)}
    return meta["status"], truncate(body), extras


@dataclass
class Outcome:
    status: str
    summary: str
    extras: dict = field(default_factory=dict)
    created: list[str] = field(default_factory=list)  # the path lists are only filled when imported
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # task-log lines: fallback used, repair requested


def runner_failure(code: int | None) -> str | None:
    if code is None:
        return "运行超时"
    return f"运行失败（退出码 {code}）" if code else None


def execute(cfg: Config, meta: dict, body: str, role: Role, skill: Skill, runs: list[RunResult]) -> Outcome:
    """Run the role in a fresh view (plus one repair pass if checks fail); on success import its files.
    Every runner invocation is appended to `runs`."""
    root = cfg.clone
    thread = thread_path(meta)
    grant = meta.get("grant") or []
    read_patterns = (expand(role.read + skill.read, meta, thread) + grant
                     + ([meta["proposal"]] if meta.get("proposal") else []))
    write_patterns = expand(role.write, meta, thread) + grant
    sources = meta.get("resources") or []  # a task's resources name the data sources it queries
    if missing := [name for name in sources if name not in cfg.datasources]:
        return Outcome("failed", f"这台机器没有配置数据源 {missing[0]}")
    tracked = tracked_files(cfg)
    view = Path(tempfile.mkdtemp(prefix="worksheet-"))

    def check() -> tuple[list[str], list[str], list[str], str | None]:
        """Diff the view against the original snapshot; returns (created, modified, deleted, failure)."""
        after = snapshot(view)
        created = sorted(p for p in after.keys() - before.keys() if not engine_owned(p))
        modified = sorted(p for p in after.keys() & before.keys() if after[p] != before[p] and not engine_owned(p))
        deleted = sorted(p for p in before.keys() - after.keys() if not engine_owned(p))
        outside = [p for p in created + modified + deleted
                   if p.startswith(PROTECTED) or is_generated(p) or not matches(p, write_patterns)]
        if outside:
            return created, modified, deleted, "写入越界：" + "、".join(outside)
        changed = set(created + modified)
        tree = (set(tracked) | changed) - set(deleted)
        problems = lint(created, modified, tree,
                        lambda p: ((view if p in changed else root) / p).read_text(errors="replace"),
                        lambda p: (root / p).read_text(errors="replace"), skill.allow_shrink, executed_queries(view))
        return created, modified, deleted, "检查未通过：\n" + "\n".join(problems) if problems else None

    events = []

    def run(prompt: str, run_dir: Path, kind: str) -> tuple[int | None, str]:
        saved = Path(tempfile.mkdtemp(prefix="worksheet-saved-"))
        shutil.copytree(view, saved, dirs_exist_ok=True)

        def restore() -> None:
            """Undo a failed run before its fallback starts."""
            shutil.rmtree(view)
            shutil.copytree(saved, view)

        try:
            results, note = run_runner(cfg.runners, role.runner, role.prompt, prompt, view, run_dir,
                                       role.timeout_minutes * 60,
                                       Ledger(cfg.usage_log, cfg.engine_id, kind, meta["id"], thread), restore)
        finally:
            shutil.rmtree(saved)
        runs.extend(results)
        if note:
            events.append(note)
        return results[-1].code, results[-1].text

    try:
        today = now().date()
        for path in tracked:
            # The vault keeps every briefing; agents only see the recent ones.
            if matches(path, read_patterns) and (not path.startswith("briefings/") or is_recent(root / path, today)):
                (view / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / path, view / path)
        shutil.copytree(skill.dir, view / "_skill")
        for name in skill.uses:
            shutil.copytree(cfg.skills[name].dir, view / "_skills" / name)
        query = query_text(root, brief_of(body), thread, meta.get("context") or [])
        packs = write_packs(root, skill.context, view / "_context", query) if skill.context else []
        if sources:
            mount_datatool(cfg, view, sources)
        (view / "_brief.md").write_text(render_brief(meta, body, thread, write_patterns, skill.uses, packs, sources))
        prompt = f'{TASK_PROMPT}\n\n<skill name="{skill.name}" dir="_skill">\n{skill.body}\n</skill>\n'
        before = snapshot(view)
        run_dir = cfg.runs / meta["id"]
        code, stdout = run(prompt, run_dir, "task")
        if failure := runner_failure(code):
            return Outcome("failed", failure, events=events)
        created, modified, deleted, failure = check()
        if failure:
            events.append("检查未通过，已要求修正一次")
            (view / "_feedback.md").write_text(failure + "\n")
            code, stdout = run(REPAIR_PROMPT, run_dir / "repair", "repair")
            if failure := runner_failure(code):
                return Outcome("failed", failure, events=events)
            created, modified, deleted, failure = check()
            if failure:
                return Outcome("failed", failure, events=events)
        status, summary, extras = parse_report(view, stdout)
        if status != "done":
            return Outcome(status, summary, extras, events=events)
        for path in created + modified:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(view / path, root / path)
        for path in deleted:
            (root / path).unlink()
        return Outcome(status, summary, extras, created, modified, deleted, events)
    finally:
        if (queries := view / "_scratch" / "queries.jsonl").exists():
            shutil.copyfile(queries, cfg.runs / meta["id"] / "queries.jsonl")
        shutil.rmtree(view)


def finish(cfg: Config, meta: dict, body: str, status: str, summary: str, event: str) -> None:
    """Record a final status in the note and block, commit and push; a rebase conflict blocks the task."""
    root, task_id, role = cfg.clone, meta["id"], meta["role"]
    meta["status"] = status
    write_task(root, meta, add_log(body, event))
    update_block(root, meta, summary)
    commit(cfg, f"task({task_id}): {meta['title'] if status == 'done' else status}", role, [task_id])
    if push(cfg):
        return
    meta, body = read_task(root, task_id)
    meta["status"] = "blocked"
    write_task(root, meta, add_log(body, f"阻塞：{CONFLICT}"))
    update_block(root, meta, CONFLICT)
    commit(cfg, f"task({task_id}): blocked", role, [task_id])
    if not push(cfg):
        log.error("task %s: conflicted again while recording the conflict", task_id)


def already_ingested(cfg: Config, task: dict, context: list[str]) -> str | None:
    """For a `dedupe: source` skill: the block summary when every captured entry already has a source page."""
    if not context or cfg.skills[task["skill"]].dedupe != "source" or task.get("force"):
        return None
    sources = {}
    for page in sorted((cfg.clone / "wiki" / "sources").glob("*.md")):
        sources.setdefault(raw_dir((frontmatter_of(page.read_text()) or {}).get("raw")), page.stem)
    if not all(d in sources for d in context):
        return None
    pages = "、".join(dict.fromkeys(f"[[{sources[d]}]]" for d in context))
    return f"这份材料已经收录过：{pages}。如需重新整理，请说明要重做。"


def claim(cfg: Config, task_id: str, role: Role) -> tuple[dict, str] | None:
    """Capture pending material and mark the task running; the successful push is the claim.
    Returns the claimed (meta, body), or None when the task was finished here without a run or was lost."""
    root = cfg.clone
    for _ in range(2):
        meta, body = read_task(root, task_id)
        if meta.get("status") != "ready" or not cfg.owns(meta):
            log.info("task %s is no longer this engine's ready task", task_id)
            return None
        if meta.get("capture") and not meta.get("context"):
            meta["context"], failed, blocked = capture_all(meta["capture"], root, meta.get("capture_mode", "all"))
            if blocked:
                finish(cfg, meta, body, "blocked", blocked, f"阻塞：{blocked}")
                return None
            if failed:
                meta["capture_failed"] = failed
            if skipped := already_ingested(cfg, meta, meta["context"]):
                finish(cfg, meta, body, "done", skipped, skipped)
                return None
        meta["status"] = "running"
        meta["attempts"] = int(meta.get("attempts") or 0) + 1
        meta["claimed_at"] = now().astimezone().isoformat(timespec="seconds")
        body = add_log(body, f"派发 → {role.runner}")
        write_task(root, meta, body)
        update_block(root, meta, meta["title"])
        commit(cfg, f"task({task_id}): running", role.id, [task_id])
        if push(cfg):
            return meta, body
        log.warning("task %s: claim was not pushed; re-checking after the sync", task_id)
    return None


def run_task(cfg: Config, task_id: str) -> None:
    root = cfg.clone
    meta, body = read_task(root, task_id)
    role = cfg.roles[meta["role"]]
    skill = cfg.skills.get(meta.get("skill"))
    if skill is None or role.runner not in cfg.runners:
        reason = (f"找不到 skill {meta.get('skill')!r}，无法运行" if skill is None
                  else f"这台机器没有配置 runner {role.runner}，无法运行")
        finish(cfg, meta, body, "failed", reason, f"失败：{reason}")
        return
    if not (claimed := claim(cfg, task_id, role)):
        return
    meta, body = claimed
    log.info("task %s: running %s/%s via %s", task_id, role.id, skill.name, role.runner)

    runs = []
    outcome = execute(cfg, meta, body, role, skill, runs)
    meta["usage"] = add_up(meta.get("usage"), runs)
    status, summary, extras = outcome.status, outcome.summary, outcome.extras
    created, modified, deleted = outcome.created, outcome.modified, outcome.deleted
    log.info("task %s: %s", task_id, status)
    for event in outcome.events:
        body = add_log(body, event)
    if status == "done":
        meta["outputs"] = created + modified
        meta |= {"files_created": created, "files_modified": modified, "files_deleted": deleted}
        uncaptured = [f.rsplit("：", 1)[0] for f in meta.get("capture_failed") or []]
        summary += output_list(created, modified, uncaptured)
        if extras:
            meta |= {f"report_{k}": v for k, v in extras.items()}
            meta["relay"] = "pending"
        # Only the coordinator writes the log and generated files; a worker's task is logged by its post step.
        meta["logged"] = cfg.is_coordinator
        if cfg.is_coordinator:
            append_log(root, skill.name, meta["title"], created, modified, deleted)
            regenerate(root)
        event = "完成" + "".join(f"；{k}：{json.dumps(v, ensure_ascii=False, default=str)}"
                               for k, v in extras.items())
    else:
        event = ("失败：" if status == "failed" else "阻塞：") + summary
    finish(cfg, meta, body, status, summary, event + describe(add_up(None, runs)))
