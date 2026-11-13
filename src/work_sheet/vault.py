"""Markdown formats stored in the vault: frontmatter, thread pages, task notes."""

import re
from datetime import datetime
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"---\n(.*?\n)?---[ \t]*(?:\n|$)", re.S)
ENGINE_HEADERS = ("> [!task]", "> [!note] dispatcher")
TASK_KEYS = ["id", "title", "status", "role", "op", "project", "thread", "parent", "depth", "attempts",
             "created", "capture", "capture_mode", "capture_failed", "context", "grant", "proposal", "outputs",
             "relay", "report_handoff", "report_capture"]


class _Dumper(yaml.SafeDumper):
    pass


# Write empty values as `key:` like hand-written Obsidian properties, not `key: null`.
_Dumper.add_representer(type(None), lambda d, _: d.represent_scalar("tag:yaml.org,2002:null", ""))


def split_frontmatter(text: str) -> tuple[str | None, str]:
    m = FRONTMATTER.match(text)
    if not m:
        return None, text
    return m.group(1) or "", text[m.end():]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Raises yaml.YAMLError or ValueError when the frontmatter is not a mapping."""
    raw, body = split_frontmatter(text)
    meta = yaml.safe_load(raw) if raw else {}
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a mapping")
    return meta, body


def dump_frontmatter(meta: dict, body: str) -> str:
    return "---\n" + yaml.dump(meta, Dumper=_Dumper, sort_keys=False, allow_unicode=True) + "---\n" + body


def now() -> datetime:
    return datetime.now()


# --- thread pages -----------------------------------------------------------

def split_thread(text: str) -> tuple[str, str]:
    """Split the page body into (handled: up to the last engine block, pending: the rest)."""
    _, body = split_frontmatter(text)
    lines = body.splitlines(keepends=True)
    end = i = 0
    while i < len(lines):
        if lines[i].startswith(ENGINE_HEADERS):
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                i += 1
            end = i
        else:
            i += 1
    return "".join(lines[:end]), "".join(lines[end:])


def quote(lines: list[str]) -> str:
    return "\n".join(f"> {line}".rstrip() for line in lines)


def task_block(task_id: str, role: str, status: str, summary: str) -> str:
    return f"> [!task] [[{task_id}]] · {role} · {status}\n" + quote(summary.splitlines() or [""])


def append_block(text: str, block: str) -> str:
    return text.rstrip("\n") + "\n\n" + block + "\n"


def find_task_header(lines: list[str], task_id: str) -> int | None:
    for i, line in enumerate(lines):
        if line.startswith("> [!task]") and f"[[{task_id}]]" in line:
            return i
    return None


def block_status(text: str, task_id: str) -> str | None:
    """The status shown in the task block header, or None when the page has no block for it."""
    lines = text.split("\n")
    i = find_task_header(lines, task_id)
    return None if i is None else lines[i].rsplit(" · ", 1)[-1].strip()


def block_summary(text: str, task_id: str) -> str:
    lines = text.split("\n")
    i = find_task_header(lines, task_id)
    summary = []
    for line in lines[i + 1:] if i is not None else []:
        if not line.startswith(">"):
            break
        summary.append(line.removeprefix(">").removeprefix(" "))
    return "\n".join(summary)


def set_block_status(text: str, task_id: str, role: str, status: str) -> str:
    lines = text.split("\n")
    i = find_task_header(lines, task_id)
    if i is not None:
        lines[i] = f"> [!task] [[{task_id}]] · {role} · {status}"
    return "\n".join(lines)


def replace_task_block(text: str, task_id: str, block: str) -> str:
    lines = text.split("\n")
    i = find_task_header(lines, task_id)
    if i is not None:
        j = i + 1
        while j < len(lines) and lines[j].startswith(">"):
            j += 1
        lines[i:j] = block.split("\n")
        return "\n".join(lines)
    # The human removed the block; put it back at the end so the result stays visible.
    return append_block(text, block)


def thread_path(meta: dict) -> str:
    return f"inbox/{meta['thread'].removeprefix('[[').removesuffix(']]')}.md"


# --- task notes -------------------------------------------------------------

def task_file(root: Path, task_id: str) -> Path:
    return root / "tasks" / f"{task_id}.md"


def list_tasks(root: Path) -> list[dict]:
    tasks = []
    for path in sorted((root / "tasks").glob("T-*.md")):
        meta, _ = parse_frontmatter(path.read_text())
        tasks.append(meta)
    return tasks


def read_task(root: Path, task_id: str) -> tuple[dict, str]:
    return parse_frontmatter(task_file(root, task_id).read_text())


def write_task(root: Path, meta: dict, body: str) -> None:
    ordered = {k: meta[k] for k in TASK_KEYS if k in meta} | meta
    path = task_file(root, meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(ordered, body))


def add_log(body: str, event: str) -> str:
    event = " ".join(event.split())
    return body.rstrip("\n") + f"\n- {now():%H:%M} {event}\n"


def brief_of(body: str) -> str:
    m = re.search(r"^## 简报\n(.*?)(?=^## 记录$)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


def next_task_id(root: Path) -> str:
    prefix = f"T-{now():%Y%m%d}-"
    seqs = [int(p.stem.removeprefix(prefix)) for p in (root / "tasks").glob(prefix + "[0-9][0-9][0-9].md")]
    return f"{prefix}{max(seqs, default=0) + 1:03d}"


def create_task(root: Path, *, title: str, status: str, role: str, op: str, project: str | None,
                thread_stem: str, brief: str, capture: list[str], context: list[str],
                blocked_reason: str | None = None, parent: str | None = None, depth: int = 0,
                **extra) -> dict:
    meta = {
        "id": next_task_id(root), "title": title, "status": status, "role": role, "op": op,
        "project": project or None, "thread": f"[[{thread_stem}]]", "parent": parent, "depth": depth,
        "attempts": 0, "created": f"{now():%Y-%m-%dT%H:%M}", "capture": capture,
        "context": context, "outputs": [], **extra,
    }
    body = f"## 简报\n{brief}\n\n## 记录\n- {now():%H:%M} 登记\n"
    if blocked_reason:
        body = add_log(body, f"阻塞：{blocked_reason}")
    write_task(root, meta, body)
    return meta


def update_block(root: Path, meta: dict, summary: str) -> None:
    page = root / thread_path(meta)
    block = task_block(meta["id"], meta["role"], meta["status"], summary)
    page.write_text(replace_task_block(page.read_text(), meta["id"], block))
