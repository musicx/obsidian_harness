"""Run one ready task in an isolated view, check its output, and import it."""

import hashlib
import json
import logging
import shutil
import tempfile
from pathlib import Path, PurePosixPath

import yaml

from work_sheet.capture import capture_all
from work_sheet.config import Config, Role
from work_sheet.gitops import commit, push, tracked_files
from work_sheet.lint import lint
from work_sheet.runner import run_cli
from work_sheet.vault import add_log, brief_of, parse_frontmatter, read_task, thread_path, update_block, write_task

log = logging.getLogger(__name__)

TASK_PROMPT = "阅读当前目录下的 _brief.md 并完成任务。结束前按其中的格式写好 _report.md。"
ENGINE_FILES = {"_brief.md", "_report.md"}
PROTECTED = ("raw/", "tasks/", "inbox/")
MAX_SUMMARY = 600
CONFLICT = "与远端的改动冲突，产出未导入"

BRIEF = """# 任务 {id}：{title}

- 操作：{op}
- 线程页：{thread}
- 本次材料：{context}
{failed}- 可写范围：{write}

## 简报
{brief}

## 结束前
在当前目录写 `_report.md`：

    ---
    status: done        # done | failed | blocked
    ---
    （不超过 600 字：做了什么、新建和修改了哪些页面；需要人回答的问题也写在这里）

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


def render_brief(meta: dict, body: str, thread: str, write: list[str]) -> str:
    context = "".join(f"\n  - {d}" for d in meta.get("context") or []) or "（无）"
    failed = "".join(f"\n  - {f}" for f in meta.get("capture_failed") or [])
    return BRIEF.format(id=meta["id"], title=meta["title"], op=meta.get("op") or "", thread=thread,
                        context=context, failed=f"- 采集失败的材料：{failed}\n" if failed else "",
                        write="、".join(write), brief=brief_of(body))


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


def page_link(path: str) -> str:
    if not path.endswith(".md"):
        return f"`{path}`"
    target = path.removesuffix(".md")
    return f"[[{target}|{PurePosixPath(target).name}]]"


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


def execute(cfg: Config, meta: dict, body: str, role: Role) -> tuple[str, str, dict, list[str], list[str]]:
    """Run the role in a fresh view; on success import its files.

    Returns (status, summary, extras, created, modified); the path lists are empty unless imported.
    """
    root = cfg.clone
    thread = thread_path(meta)
    grant = meta.get("grant") or []
    read_patterns = expand(role.read, meta, thread) + grant + ([meta["proposal"]] if meta.get("proposal") else [])
    write_patterns = expand(role.write, meta, thread) + grant
    tracked = tracked_files(cfg)
    view = Path(tempfile.mkdtemp(prefix="worksheet-"))
    try:
        for path in tracked:
            if matches(path, read_patterns):
                (view / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / path, view / path)
        (view / "_brief.md").write_text(render_brief(meta, body, thread, write_patterns))
        before = snapshot(view)
        code, stdout = run_cli(cfg.runners[role.runner], role.prompt, TASK_PROMPT, view,
                               cfg.runs / meta["id"], role.timeout_minutes * 60)
        after = snapshot(view)
        created = sorted(after.keys() - before.keys() - ENGINE_FILES)
        modified = sorted(p for p in after.keys() & before.keys() if after[p] != before[p] and p not in ENGINE_FILES)
        deleted = sorted(before.keys() - after.keys() - ENGINE_FILES)

        if code is None:
            return "failed", "运行超时", {}, [], []
        if code:
            return "failed", f"运行失败（退出码 {code}）", {}, [], []
        outside = [p for p in created + modified + deleted
                   if p.startswith(PROTECTED) or not matches(p, write_patterns)]
        if outside:
            return "failed", "写入越界：" + "、".join(outside), {}, [], []
        changed = set(created + modified)
        tree = (set(tracked) | changed) - set(deleted)
        problems = lint(created, modified, tree,
                        lambda p: ((view if p in changed else root) / p).read_text(errors="replace"),
                        lambda p: (root / p).read_text(errors="replace"))
        if problems:
            return "failed", "检查未通过：\n" + "\n".join(problems), {}, [], []
        status, summary, extras = parse_report(view, stdout)
        if status != "done":
            return status, summary, extras, [], []
        for path in created + modified:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(view / path, root / path)
        for path in deleted:
            (root / path).unlink()
        return status, summary, extras, created, modified
    finally:
        shutil.rmtree(view)


def run_task(cfg: Config, task_id: str) -> None:
    root = cfg.clone
    meta, body = read_task(root, task_id)
    role = cfg.roles[meta["role"]]
    if meta.get("capture") and not meta.get("context"):
        meta["context"], failed, blocked = capture_all(meta["capture"], root, meta.get("capture_mode", "all"))
        if blocked:
            meta["status"] = "blocked"
            write_task(root, meta, add_log(body, f"阻塞：{blocked}"))
            update_block(root, meta, blocked)
            commit(cfg, f"task({task_id}): blocked", role.id, [task_id])
            push(cfg)
            return
        if failed:
            meta["capture_failed"] = failed
    meta["status"] = "running"
    meta["attempts"] = int(meta.get("attempts") or 0) + 1
    body = add_log(body, f"派发 → {role.runner}")
    write_task(root, meta, body)
    update_block(root, meta, meta["title"])
    commit(cfg, f"task({task_id}): running", role.id, [task_id])
    if not push(cfg):
        log.warning("task %s: could not mark running; skipped this tick", task_id)
        return
    log.info("task %s: running %s via %s", task_id, role.id, role.runner)

    status, summary, extras, created, modified = execute(cfg, meta, body, role)
    log.info("task %s: %s", task_id, status)
    meta["status"] = status
    if status == "done":
        meta["outputs"] = created + modified
        uncaptured = [f.rsplit("：", 1)[0] for f in meta.get("capture_failed") or []]
        summary += output_list(created, modified, uncaptured)
        if extras:
            meta |= {f"report_{k}": v for k, v in extras.items()}
            meta["relay"] = "pending"
        event = "完成" + "".join(f"；{k}：{json.dumps(v, ensure_ascii=False, default=str)}"
                               for k, v in extras.items())
    else:
        event = ("失败：" if status == "failed" else "阻塞：") + summary
    write_task(root, meta, add_log(body, event))
    update_block(root, meta, summary)
    commit(cfg, f"task({task_id}): {meta['title'] if status == 'done' else status}", role.id, [task_id])
    if push(cfg):
        return
    meta, body = read_task(root, task_id)
    meta["status"] = "blocked"
    write_task(root, meta, add_log(body, f"阻塞：{CONFLICT}"))
    update_block(root, meta, CONFLICT)
    commit(cfg, f"task({task_id}): blocked", role.id, [task_id])
    if not push(cfg):
        log.error("task %s: conflicted again while recording the conflict", task_id)
