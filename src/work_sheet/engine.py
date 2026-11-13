import logging
import shutil

from work_sheet.config import Config
from work_sheet.dispatch import dispatch_relay, dispatch_thread
from work_sheet.feeds import import_feeds
from work_sheet.generated import regenerate
from work_sheet.gitops import commit, dirty, push, sync
from work_sheet.tasks import run_task
from work_sheet.vault import (add_log, block_status, list_tasks, read_task, set_block_status, split_thread,
                              thread_path, update_block, write_task)

log = logging.getLogger(__name__)

STALE_RUNNING = "运行中断，或状态被手动改成了 running；把卡片拖回 ready 可重跑"
ASK_NOT_RUNNABLE = "提问类任务不能运行，请在线程页里回答"
MAX_PASSES = 10


def pending_threads(cfg: Config) -> list[str]:
    return [p.relative_to(cfg.clone).as_posix() for p in sorted((cfg.clone / "inbox").glob("*.md"))
            if split_thread(p.read_text())[1].strip()]


def scaffold(cfg: Config) -> None:
    """Add missing vault-template files (never overwriting) and refresh the generated indexes."""
    if cfg.template_dir.is_dir():
        for src in sorted(cfg.template_dir.rglob("*")):
            dest = cfg.clone / src.relative_to(cfg.template_dir)
            if src.is_file() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
    regenerate(cfg.clone)
    if dirty(cfg):
        log.info("scaffolded vault files")
        commit(cfg, "scaffold: vault files", "engine")
        push(cfg)


def feeds(cfg: Config) -> None:
    if count := import_feeds(cfg):
        log.info("imported %d briefings", count)
        commit(cfg, f"feed: {count} 篇简报", "engine")
        push(cfg)


def reconcile(cfg: Config) -> None:
    """Bring task notes and their thread blocks back in line before this tick acts on them."""
    root = cfg.clone
    changed = []
    for meta in list_tasks(root):
        meta, body = read_task(root, meta["id"])
        page = root / thread_path(meta)
        status = meta.get("status")
        if status == "running":
            # The engine is serial, so a task still running at tick start was interrupted.
            meta["status"] = "failed"
            write_task(root, meta, add_log(body, f"失败：{STALE_RUNNING}"))
            update_block(root, meta, STALE_RUNNING)
        elif meta["role"] == "dispatcher" and status == "ready":
            meta["status"] = "blocked"
            write_task(root, meta, add_log(body, ASK_NOT_RUNNABLE))
            page.write_text(set_block_status(page.read_text(), meta["id"], meta["role"], "blocked"))
        else:
            shown = block_status(page.read_text(), meta["id"]) if page.exists() else None
            if shown is None or shown == status:
                continue
            write_task(root, meta, add_log(body, f"状态被手动改为 {status}"))
            page.write_text(set_block_status(page.read_text(), meta["id"], meta["role"], status))
        changed.append(meta["id"])
    if changed:
        log.info("reconciled task statuses: %s", ", ".join(changed))
        commit(cfg, "reconcile: task statuses", "engine", changed)
        push(cfg)


def tick(cfg: Config) -> None:
    if not sync(cfg):
        log.info("origin/%s has no commits yet; nothing to do", cfg.branch)
        return
    scaffold(cfg)
    feeds(cfg)
    reconcile(cfg)
    for path in pending_threads(cfg):
        dispatch_thread(cfg, path)
    # Relays can register new ready tasks and finished tasks can request relays, so repeat until quiet.
    unavailable = set()  # relays whose dispatcher call failed; retried next tick, not in every pass
    for _ in range(MAX_PASSES):
        relays = [m["id"] for m in list_tasks(cfg.clone) if m.get("relay") == "pending" and m["id"] not in unavailable]
        for task_id in relays:
            if not dispatch_relay(cfg, task_id):
                unavailable.add(task_id)
        ready = [m["id"] for m in list_tasks(cfg.clone) if m.get("status") == "ready"]  # sorted by id
        for task_id in ready:
            run_task(cfg, task_id)
        if not relays and not ready:
            break
