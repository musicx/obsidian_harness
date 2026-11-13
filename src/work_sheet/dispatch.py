"""Ask the dispatcher role what to do with a thread's new text, then apply its decision."""

import json
import logging
import tempfile
from pathlib import Path, PurePosixPath

from work_sheet.capture import capture_all
from work_sheet.config import Config
from work_sheet.gitops import commit, push
from work_sheet.lint import frontmatter_of
from work_sheet.runner import run_cli
from work_sheet.tasks import matches
from work_sheet.vault import (append_block, block_summary, create_task, list_tasks, quote, read_task, split_thread,
                              task_block, thread_path, write_task)

log = logging.getLogger(__name__)

ACTIONS = ("register", "ignore", "ask_human")
MAX_TASKS_PER_THREAD = 8
MAX_BRIEF = 1500
MAX_DEPTH = 5
MAX_CAPTURE = 10
OUTPUT_FORMAT = """Reply with one JSON object and nothing else:

```json
{"action": "register | ignore | ask_human",
 "reason": "…",
 "question": "… (ask_human only)",
 "tasks": [{"role": "librarian", "op": "ingest", "title": "…", "brief": "…", "capture": ["url or path"],
            "capture_mode": "all", "project": null, "parent": null, "grant": [], "proposal": null}]}
```

Optional task fields: `capture_mode` is `all` (default: any failed item blocks the task) or `any` (run with
whatever could be captured); `grant` lists extra files the role may write (only paths its role allows to be
granted); `proposal` is the proposal page that justifies granting a file that already exists."""


class InvalidDecision(Exception):
    pass


def build_prompt(cfg: Config, path: str, handled: str, new: str, existing: list[dict],
                 finished: str | None = None) -> str:
    projects = cfg.clone / "projects" / "_index.md"
    tasks = "\n".join(
        f"- {t['id']} · {t['role']} · {t['status']} · {t['title']}"
        + (f" · project: {t['project']}" if t.get("project") else "")
        + "".join(f"\n  - {p}" for p in t.get("outputs") or [])
        for t in existing)
    roles = "\n".join(f"- {r.id}: {r.description}" for r in cfg.roles.values() if r.id != "dispatcher")
    finished_section = f"\n# Finished task\n\n{finished}\n" if finished else ""
    return f"""# Thread

Path: {path}

## Already handled

{handled.strip() or "(none)"}

## New from the human

{new.strip()}

# Existing tasks in this thread

{tasks or "(none)"}
{finished_section}
# Projects

{projects.read_text().strip() if projects.exists() else "(none)"}

# Roles

{roles}

# Routing rules

{cfg.routing.strip() if cfg.routing else "(none)"}

# Output format

{OUTPUT_FORMAT}
"""


def nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def proposal_allows(root: Path, proposal, path: str) -> bool:
    """True when `proposal` is an open proposal page whose target is `path`."""
    if not nonempty_str(proposal) or not (root / proposal).is_file():
        return False
    meta = frontmatter_of((root / proposal).read_text()) or {}
    target = str(meta.get("target") or "").strip().removeprefix("[[").removesuffix("]]")
    target = target.split("|")[0].split("#")[0].strip()
    return (meta.get("type") == "proposal" and meta.get("status") in ("proposed", "partially-accepted")
            and target in (path, path.removesuffix(".md"), PurePosixPath(path).stem))


def parse_decision(stdout: str, cfg: Config, existing: list[dict], relay_parent: str | None = None) -> dict:
    """Validate the dispatcher output. `relay_parent` is set when relaying that finished task's report."""
    start, end = stdout.find("{"), stdout.rfind("}")
    if start < 0 or end < start:
        raise InvalidDecision("输出里没有 JSON 对象")
    try:
        decision = json.loads(stdout[start:end + 1])
    except json.JSONDecodeError as e:
        raise InvalidDecision(f"JSON 无法解析：{e}")
    if not isinstance(decision, dict):
        raise InvalidDecision("输出不是 JSON 对象")
    action = decision.get("action")
    if action not in ACTIONS:
        raise InvalidDecision(f"action 必须是 register、ignore 或 ask_human，实际为 {action!r}")
    if action == "ask_human" and not nonempty_str(decision.get("question")):
        raise InvalidDecision("ask_human 需要非空的 question")
    if action != "register":
        return decision
    tasks = decision.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise InvalidDecision("register 至少需要一个任务")
    if len(existing) + len(tasks) > MAX_TASKS_PER_THREAD:
        raise InvalidDecision(f"本线程已有 {len(existing)} 个任务，加上新任务超过上限 {MAX_TASKS_PER_THREAD}")
    depths = {t["id"]: int(t.get("depth") or 0) for t in existing}
    for i, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            raise InvalidDecision(f"任务 {i} 不是 JSON 对象")
        role = task.get("role")
        if role not in cfg.roles or role == "dispatcher":
            raise InvalidDecision(f"任务 {i} 的角色 {role!r} 不存在")
        if not nonempty_str(task.get("title")) or not nonempty_str(task.get("brief")):
            raise InvalidDecision(f"任务 {i} 的 title 和 brief 不能为空")
        if len(task["brief"]) > MAX_BRIEF:
            raise InvalidDecision(f"任务 {i} 的 brief 超过 {MAX_BRIEF} 字")
        capture_items = task.get("capture", [])
        if not isinstance(capture_items, list) or not all(isinstance(c, str) for c in capture_items):
            raise InvalidDecision(f"任务 {i} 的 capture 必须是字符串列表")
        if len(capture_items) > MAX_CAPTURE:
            raise InvalidDecision(f"任务 {i}：capture 最多 {MAX_CAPTURE} 项")
        task.setdefault("capture_mode", "all")
        if task["capture_mode"] not in ("all", "any"):
            raise InvalidDecision(f"任务 {i} 的 capture_mode 必须是 all 或 any")
        project = task.get("project")
        if project is not None and not (nonempty_str(project) and not set(project) & {"/", "\\"}
                                        and not project.startswith(".")):
            raise InvalidDecision(f"任务 {i} 的 project 必须是不含 / 或 \\、不以 . 开头的名称")
        if cfg.roles[role].needs_project and project is None:
            raise InvalidDecision(f"角色 {role} 需要指定 project")
        grant = task.get("grant") or []
        if not isinstance(grant, list) or not all(nonempty_str(g) for g in grant):
            raise InvalidDecision(f"任务 {i} 的 grant 必须是路径列表")
        if grant and relay_parent:
            raise InvalidDecision("接力登记的任务不能授予写权限")
        for path in grant:
            if not matches(path, cfg.roles[role].grantable):
                raise InvalidDecision(f"角色 {role} 不能被授予 {path}")
            if (cfg.clone / path).exists() and not proposal_allows(cfg.clone, task.get("proposal"), path):
                raise InvalidDecision(f"修改已有的 {path} 需要一份状态为 proposed 的建议页")
        if task.get("proposal") is not None and not nonempty_str(task["proposal"]):
            raise InvalidDecision(f"任务 {i} 的 proposal 必须是路径")
        if task.get("parent") is None and relay_parent:
            task["parent"] = relay_parent
        parent = task.get("parent")
        if parent is not None:
            if parent not in depths:
                raise InvalidDecision(f"任务 {i} 的 parent {parent!r} 不是本线程已有的任务")
            if depths[parent] + 1 > MAX_DEPTH:
                raise InvalidDecision(f"任务 {i}：接力深度超过上限 {MAX_DEPTH}")
            task["depth"] = depths[parent] + 1
    return decision


def new_run_dir(cfg: Config) -> Path:
    numbers = [int(p.name.removeprefix("dispatch-")) for p in cfg.runs.glob("dispatch-*")]
    return cfg.runs / f"dispatch-{max(numbers, default=0) + 1}"


def ask_dispatcher(cfg: Config, prompt: str, existing: list[dict], relay_parent: str | None = None) -> dict:
    role = cfg.roles["dispatcher"]
    error = None
    for _ in range(2):
        if error:
            prompt += f"\n# Your previous output was invalid\n\n{error}\n"
        with tempfile.TemporaryDirectory(prefix="worksheet-") as cwd:
            code, stdout = run_cli(cfg.runners[role.runner], role.prompt, prompt, Path(cwd),
                                   new_run_dir(cfg), role.timeout_minutes * 60)
        try:
            if code is None:
                raise InvalidDecision("运行超时")
            if code:
                raise InvalidDecision(f"运行失败（退出码 {code}）")
            return parse_decision(stdout, cfg, existing, relay_parent)
        except InvalidDecision as e:
            error = str(e)
            log.warning("invalid dispatcher output: %s", error)
    return {"action": "ask_human", "reason": error, "question": f"调度失败：{error}"}


def register(root: Path, stem: str, task: dict) -> tuple[dict, str]:
    """Capture the task's material and create its note; return (meta, block summary)."""
    items, mode = task.get("capture", []), task["capture_mode"]
    context, failed, blocked = capture_all(items, root, mode)
    extra = {"capture_mode": mode}
    if failed and not blocked:
        extra["capture_failed"] = failed
    extra |= {k: task[k] for k in ("grant", "proposal") if task.get(k)}
    meta = create_task(root, title=task["title"].strip(), status="blocked" if blocked else "ready",
                       role=task["role"], op=str(task.get("op") or ""), project=task.get("project"),
                       thread_stem=stem, brief=task["brief"].strip(), capture=items, context=context,
                       blocked_reason=blocked, parent=task.get("parent"), depth=task.get("depth", 0), **extra)
    return meta, blocked or meta["title"]


def apply_decision(cfg: Config, path: str, decision: dict, note_on_ignore: bool = True) -> list[str]:
    """Write the decision into the vault; returns the ids of the task notes created."""
    root = cfg.clone
    page = root / path
    stem = PurePosixPath(path).stem
    action = decision["action"]
    log.info("dispatch %s: %s", path, action)
    text = page.read_text()
    ids = []
    if action == "ignore":
        if note_on_ignore:
            reason = str(decision.get("reason") or "")
            text = append_block(text, "> [!note] dispatcher · 未登记任务\n" + quote(reason.splitlines() or [""]))
    elif action == "ask_human":
        question = decision["question"].strip()
        meta = create_task(root, title=question.splitlines()[0][:60], status="blocked", role="dispatcher",
                           op="ask", project=None, thread_stem=stem, brief=question, capture=[], context=[],
                           blocked_reason=question)
        text = append_block(text, task_block(meta["id"], "dispatcher", "blocked", question))
        ids.append(meta["id"])
    else:
        for task in decision["tasks"]:
            meta, summary = register(root, stem, task)
            text = append_block(text, task_block(meta["id"], meta["role"], meta["status"], summary))
            ids.append(meta["id"])
    page.write_text(text)
    return ids


def thread_tasks(root: Path, stem: str) -> list[dict]:
    return [t for t in list_tasks(root) if t.get("thread") == f"[[{stem}]]"]


def dispatch_thread(cfg: Config, path: str) -> None:
    root = cfg.clone
    stem = PurePosixPath(path).stem
    handled, new = split_thread((root / path).read_text())
    existing = thread_tasks(root, stem)
    decision = ask_dispatcher(cfg, build_prompt(cfg, path, handled, new, existing), existing)
    ids = apply_decision(cfg, path, decision)
    commit(cfg, f"dispatch: {stem}", "dispatcher", ids)
    if not push(cfg):
        log.warning("dispatch of %s dropped by a conflict; the page stays pending", path)


def dispatch_relay(cfg: Config, task_id: str) -> None:
    """Ask the dispatcher what follows from a finished task's report."""
    root = cfg.clone
    meta, body = read_task(root, task_id)
    path = thread_path(meta)
    text = (root / path).read_text()
    handled, _ = split_thread(text)
    existing = thread_tasks(root, PurePosixPath(path).stem)
    capture_requests = "\n".join(f"- {c}" for c in meta.get("report_capture") or []) or "(none)"
    handoff = meta.get("report_handoff")
    finished = f"""- id: {task_id}
- role: {meta['role']}
- title: {meta['title']}

## Report summary

{block_summary(text, task_id)}

## Handoff

{json.dumps(handoff, ensure_ascii=False, indent=2, default=str) if handoff else "(none)"}

## Capture requests

{capture_requests}"""
    prompt = build_prompt(cfg, path, handled, "(none)", existing, finished)
    decision = ask_dispatcher(cfg, prompt, existing, relay_parent=task_id)
    ids = apply_decision(cfg, path, decision, note_on_ignore=False)
    meta["relay"] = "done"
    write_task(root, meta, body)
    commit(cfg, f"relay: {task_id}", "dispatcher", [task_id, *ids])
    if not push(cfg):
        log.warning("relay of %s dropped by a conflict; it stays pending", task_id)
