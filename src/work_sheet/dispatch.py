"""Ask the dispatcher role what to do with a thread's new text, then apply its decision."""

import json
import logging
import tempfile
from pathlib import Path, PurePosixPath

from work_sheet.capture import capture_all
from work_sheet.config import Config, Skill
from work_sheet.gitops import commit, push
from work_sheet.lint import frontmatter_of
from work_sheet.runner import run_runner
from work_sheet.usage import Ledger, summary
from work_sheet.tasks import already_ingested, matches
from work_sheet.vault import (append_block, block_summary, create_task, list_tasks, quote, read_task,
                              split_thread, task_block, thread_path, write_task)

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
 "tasks": [{"skill": "<one of the skills above>", "title": "…", "brief": "…", "capture": ["url or path"],
            "capture_mode": "all", "project": null, "parent": null, "grant": [], "proposal": null,
            "force": false}]}
```

Each task names the `skill` to run; the role that runs it follows from the skill. Optional task fields:
`capture_mode` is `all` (default: any failed item blocks the task) or `any` (run with whatever could be
captured); `grant` lists extra files the role may write (only paths its role allows to be granted);
`proposal` is the proposal page that justifies granting a file that already exists; `force: true` processes
material again even if it was already ingested."""
RESOURCES_NOTE = """
`resources` lists the resources (see # Engines) this task needs, e.g. the data source to query."""
ENGINE_NOTE = """
`engine` names the engine (see # Engines) that runs the task: needed when the material or data lives on one
machine. Leave it null to use the coordinator, or the first engine that can run the skill."""


class InvalidDecision(Exception):
    pass


def build_prompt(cfg: Config, path: str, handled: str, new: str, existing: list[dict], projects: str | None,
                 finished: str | None = None) -> str:
    tasks = "\n".join(
        f"- {t['id']} · {t.get('skill') or '-'}（{t['role']}） · {t['status']} · {t['title']}"
        + (f" · project: {t['project']}" if t.get("project") else "")
        + "".join(f"\n  - {p}" for p in t.get("outputs") or [])
        for t in existing)
    skills = "\n".join(f"- {s.name}（{s.role}）：{s.description}" + runs_on(cfg, s) for s in cfg.dispatchable())
    finished_section = f"\n# Finished task\n\n{finished}\n" if finished else ""
    engines, output_format = "", OUTPUT_FORMAT
    if cfg.registry or cfg.datasources:
        engines = "# Engines\n\n" + "\n".join(
            f"- {e.id}（{e.role}）：{e.description}；资源：" + ("、".join(
                f"{name}（{about}）" if about else name for name, about in e.resources.items()) or "无")
            for e in cfg.engines.values()) + "\n\n"
        fields = ', "resources": []' + (', "engine": null' if cfg.registry else "")
        output_format = (OUTPUT_FORMAT.replace('"force": false}', '"force": false' + fields + "}") + RESOURCES_NOTE
                         + (ENGINE_NOTE if cfg.registry else ""))
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

{projects.strip() if projects else "(none)"}

{engines}# Skills

{skills}

# Routing rules

{cfg.routing.strip() if cfg.routing else "(none)"}

# Output format

{output_format}
"""


def runs_on(cfg: Config, skill: Skill) -> str:
    eligible = cfg.eligible(skill)
    return "" if len(eligible) == len(cfg.engines) else f"；可运行于：{'、'.join(eligible)}"


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
        skill = next((s for s in cfg.dispatchable() if s.name == task.get("skill")), None)
        if skill is None:
            valid = "、".join(sorted(s.name for s in cfg.dispatchable()))
            raise InvalidDecision(f"任务 {i} 的 skill {task.get('skill')!r} 不存在，可用的有：{valid}")
        if task.get("role") is not None and task["role"] != skill.role:
            raise InvalidDecision(f"skill {skill.name} 属于角色 {skill.role}")
        role = task["role"] = skill.role
        if not isinstance(task.get("force", False), bool):
            raise InvalidDecision(f"任务 {i} 的 force 必须是 true 或 false")
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
        if skill.needs_project and project is None:
            raise InvalidDecision(f"skill {skill.name} 需要指定 project")
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
        resources = task.get("resources") or []
        if not isinstance(resources, list) or not all(isinstance(r, str) for r in resources):
            raise InvalidDecision(f"任务 {i} 的 resources 必须是资源名称列表")
        if unknown := [r for r in resources if r not in cfg.resources()]:
            raise InvalidDecision(f"没有引擎提供资源 {unknown[0]}，可选：{'、'.join(cfg.resources()) or '（无）'}")
        eligible = cfg.eligible(skill, resources)
        if not eligible:
            raise InvalidDecision(f"没有一个引擎能同时满足 skill {skill.name} 和资源 {'、'.join(resources)}")
        if task.get("engine") is None:
            task["engine"] = cfg.coordinator if cfg.coordinator in eligible else eligible[0]
        elif task["engine"] not in eligible:
            raise InvalidDecision(f"skill {skill.name} 不能在引擎 {task['engine']} 上运行，可选：{'、'.join(eligible)}")
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


def ask_dispatcher(cfg: Config, prompt: str, existing: list[dict], thread: str,
                   relay_parent: str | None = None) -> dict | None:
    """The validated decision; ask_human when the output is invalid twice; None when the runner (and its
    fallback) failed."""
    role = cfg.roles["dispatcher"]
    ledger = Ledger(cfg.usage_log, cfg.engine_id, "relay" if relay_parent else "dispatch", relay_parent, thread)
    results = []
    try:
        error = None
        for _ in range(2):
            if error:
                prompt += f"\n# Your previous output was invalid\n\n{error}\n"
            with tempfile.TemporaryDirectory(prefix="worksheet-") as cwd:
                runs, _ = run_runner(cfg.runners, role.runner, role.prompt, prompt, Path(cwd), new_run_dir(cfg),
                                     role.timeout_minutes * 60, ledger)
            results += runs
            if runs[-1].code != 0:
                # An outage, not a routing problem: create nothing and let the next tick try again.
                log.warning("dispatcher runner failed (%s); will retry next tick",
                            "timeout" if runs[-1].code is None else f"exit {runs[-1].code}")
                return None
            try:
                return parse_decision(runs[-1].text, cfg, existing, relay_parent)
            except InvalidDecision as e:
                error = str(e)
                log.warning("invalid dispatcher output: %s", error)
        return {"action": "ask_human", "reason": error, "question": f"调度失败：{error}"}
    finally:
        log.info("%s %s usage: %s", ledger.kind, relay_parent or thread, summary(results))


def register(cfg: Config, stem: str, task: dict) -> tuple[dict, str]:
    """Create the task's note (capturing its material first when this engine runs it); returns (meta, summary).
    Another engine's task is registered `ready` with empty context: that engine captures when it runs it."""
    root = cfg.clone
    items, mode, engine = task.get("capture", []), task["capture_mode"], task["engine"]
    context, failed, blocked, status, summary, event = [], [], None, "ready", None, None
    if engine == cfg.engine_id:
        context, failed, blocked = capture_all(items, root, mode)
        if blocked:
            status, summary, event = "blocked", blocked, f"阻塞：{blocked}"
        elif skipped := already_ingested(cfg, task, context):
            status, summary, event = "done", skipped, skipped
    else:
        summary = f"{task['title'].strip()}（等待 {engine} 引擎执行）"
    extra = {"capture_mode": mode}
    if failed and not blocked:
        extra["capture_failed"] = failed
    extra |= {k: task[k] for k in ("force", "grant", "proposal", "resources") if task.get(k)}
    if cfg.registry:
        extra["engine"] = engine
    meta = create_task(root, title=task["title"].strip(), status=status, role=task["role"], skill=task["skill"],
                       project=task.get("project"), thread_stem=stem, brief=task["brief"].strip(), capture=items,
                       context=context, event=event, parent=task.get("parent"), depth=task.get("depth", 0),
                       **extra)
    return meta, summary or meta["title"]


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
                           skill="ask", project=None, thread_stem=stem, brief=question, capture=[], context=[],
                           event=f"阻塞：{question}")
        text = append_block(text, task_block(meta["id"], "dispatcher", "blocked", question))
        ids.append(meta["id"])
    else:
        for task in decision["tasks"]:
            meta, summary = register(cfg, stem, task)
            text = append_block(text, task_block(meta["id"], meta["role"], meta["status"], summary))
            ids.append(meta["id"])
    page.write_text(text)
    return ids


def thread_tasks(root: Path, stem: str) -> list[dict]:
    return [t for t in list_tasks(root) if t.get("thread") == f"[[{stem}]]"]


def projects_text(root: Path) -> str | None:
    path = root / "projects" / "_index.md"
    return path.read_text() if path.exists() else None


def dispatch_thread(cfg: Config, path: str) -> None:
    root = cfg.clone
    stem = PurePosixPath(path).stem
    handled, new = split_thread((root / path).read_text())
    existing = thread_tasks(root, stem)
    decision = ask_dispatcher(cfg, build_prompt(cfg, path, handled, new, existing, projects_text(root)), existing,
                              path)
    if decision is None:
        return
    ids = apply_decision(cfg, path, decision)
    commit(cfg, f"dispatch: {stem}", "dispatcher", ids)
    if not push(cfg):
        log.warning("dispatch of %s dropped by a conflict; the page stays pending", path)


def dispatch_relay(cfg: Config, task_id: str) -> bool:
    """Ask the dispatcher what follows from a finished task's report; False if the dispatcher was unavailable."""
    root = cfg.clone
    meta, body = read_task(root, task_id)
    path = thread_path(meta)
    text = (root / path).read_text()
    handled, _ = split_thread(text)
    existing = thread_tasks(root, PurePosixPath(path).stem)
    capture_requests = "\n".join(f"- {c}" for c in meta.get("report_capture") or []) or "(none)"
    handoff = meta.get("report_handoff")
    finished = f"""- id: {task_id}
- skill: {meta.get('skill') or '-'}
- role: {meta['role']}
- title: {meta['title']}

## Report summary

{block_summary(text, task_id)}

## Handoff

{json.dumps(handoff, ensure_ascii=False, indent=2, default=str) if handoff else "(none)"}

## Capture requests

{capture_requests}"""
    prompt = build_prompt(cfg, path, handled, "(none)", existing, projects_text(root), finished)
    decision = ask_dispatcher(cfg, prompt, existing, path, relay_parent=task_id)
    if decision is None:
        return False
    ids = apply_decision(cfg, path, decision, note_on_ignore=False)
    meta["relay"] = "done"
    write_task(root, meta, body)
    commit(cfg, f"relay: {task_id}", "dispatcher", [task_id, *ids])
    if not push(cfg):
        log.warning("relay of %s dropped by a conflict; it stays pending", task_id)
    return True
