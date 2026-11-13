"""Vault files the engine maintains itself: directory indexes, the wiki index, the project roster, the log."""

import re
from pathlib import Path

from work_sheet.context import Page, cell, load_pages, read_meta
from work_sheet.vault import as_list, is_generated, now, page_link

INDEX_TITLES = {"wiki/sources": "来源摘要", "wiki/concepts": "概念", "wiki/entities": "实体"}
TOPIC_LINE = re.compile(r"^-\s+(.+?)(?:\s+—\s+(.*))?$")
LOGGED_DIRS = ("wiki/", "practice/", "proposals/")


def parse_topics(text: str) -> dict[str, str]:
    """`- <topic> — <说明>` lines of wiki/topics.md, in file order."""
    topics = {}
    for line in text.splitlines():
        if m := TOPIC_LINE.match(line.strip()):
            topics[m.group(1).strip()] = (m.group(2) or "").strip()
    return topics


def by_name(pages: list[Page]) -> list[Page]:
    return sorted(pages, key=lambda p: (p.name.casefold(), p.path))


def entry(link: str, description: str) -> str:
    return f"- {link} — {description}\n" if description else f"- {link}\n"


def wiki_index(root: Path, pages: list[Page]) -> str:
    topics_file = root / "wiki" / "topics.md"
    topics = parse_topics(topics_file.read_text()) if topics_file.exists() else {}
    entities = [p for p in pages if p.path.startswith(("wiki/concepts/", "wiki/entities/"))]
    count = {d: sum(p.path.startswith(d + "/") for p in pages)
             for d in ("wiki/sources", "wiki/concepts", "wiki/entities", "practice")}
    out = ["# 知识库目录\n",
           f"来源 {count['wiki/sources']} 篇 · 概念 {count['wiki/concepts']} 个 · 实体 {count['wiki/entities']} 个"
           f" · 做法 {count['practice']} 篇\n", "## 按主题\n"]
    groups = [(topic, note, by_name([p for p in entities if topic in as_list(p.meta.get("topics"))]))
              for topic, note in topics.items()]
    unfiled = by_name([p for p in entities if not as_list(p.meta.get("topics"))])
    groups.append(("未归类", "", unfiled))
    filed = [(t, n, ps) for t, n, ps in groups if ps]
    for topic, note, members in filed:
        out.append(f"### {topic}\n")
        if note:
            out.append(note + "\n")
        out.append("、".join(f"[[{p.name}]]" for p in members) + "\n")
    if not filed:
        out.append("（暂无）\n")
    practice = by_name([p for p in pages if p.path.startswith("practice/")])
    out.append("## 我们的做法\n")
    out.append("".join(entry(f"[[{p.path.removesuffix('.md')}|{p.name}]]", p.description) for p in practice)
               or "（暂无）\n")
    out.append("## 目录与日志\n")
    out.append("- [[wiki/sources/_index|来源摘要]]\n- [[wiki/concepts/_index|概念]]\n"
               "- [[wiki/entities/_index|实体]]\n- [[wiki/log|操作日志]]\n")
    return "\n".join(out)


def projects_index(root: Path) -> str:
    rows = []
    for status in (root / "projects").glob("*/status.md"):
        meta, _ = read_meta(status.read_text(errors="replace"))
        slug = status.parent.name
        name = str(meta.get("project") or slug)
        rows.append((name.casefold(), f"| [[projects/{slug}/status\\|{cell(name)}]] | "
                     f"{cell('、'.join(as_list(meta.get('aliases'))))} | {cell(meta.get('description'))} | "
                     f"{cell(meta.get('health'))} | {cell(meta.get('as_of'))} |\n"))
    return ("# 项目名册\n\n| 项目 | 别名 | 说明 | 状态 | 截至 |\n|---|---|---|---|---|\n"
            + "".join(row for _, row in sorted(rows)))


def write_if_dir_exists(root: Path, rel: str, text: str) -> None:
    """Indexes are only kept for directories the vault already has."""
    path = root / rel
    if path.parent.is_dir() and (not path.exists() or path.read_text() != text):
        path.write_text(text)


def regenerate(root: Path) -> None:
    pages = load_pages(root)
    for top, title in INDEX_TITLES.items():
        members = by_name([p for p in pages if p.path.startswith(top + "/")])
        write_if_dir_exists(root, f"{top}/_index.md",
                            f"# {title}\n\n" + "".join(entry(f"[[{p.name}]]", p.description) for p in members))
    write_if_dir_exists(root, "wiki/index.md", wiki_index(root, pages))
    write_if_dir_exists(root, "projects/_index.md", projects_index(root))


def append_log(root: Path, skill: str, title: str, created: list[str], modified: list[str],
               deleted: list[str]) -> None:
    """Append a wiki/log.md entry for a finished task's changes to knowledge files, if it made any."""
    def logged(paths: list[str]) -> list[str]:
        return [p for p in paths if p.startswith(LOGGED_DIRS) and not is_generated(p)]

    lines = [f"- {label}：" + "、".join(map(page_link, paths))
             for label, paths in (("新建", logged(created)), ("修改", logged(modified)), ("删除", logged(deleted)))
             if paths]
    if not lines:
        return
    log = root / "wiki" / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    text = log.read_text() if log.exists() else "# 知识库操作日志\n"
    log.write_text(text.rstrip("\n") + f"\n\n## [{now():%Y-%m-%d}] {skill} | {title}\n\n" + "\n".join(lines) + "\n")
