"""Mechanical checks on Markdown files a task created or modified."""

import re
from collections.abc import Callable
from pathlib import PurePosixPath

import yaml

from work_sheet.vault import parse_frontmatter

WIKILINK = re.compile(r"!?\[\[([^\]]+?)\]\]")
EXEMPT = {"index.md", "_index.md", "log.md"}
PAGE_TYPES = {"source", "concept", "entity"}
PROPOSAL_STATUSES = {"proposed", "accepted", "partially-accepted", "rejected"}


def frontmatter_of(text: str) -> dict | None:
    try:
        return parse_frontmatter(text)[0]
    except (yaml.YAMLError, ValueError):
        return None


def lint(created: list[str], modified: list[str], tree: set[str], read: Callable[[str], str],
         read_old: Callable[[str], str]) -> list[str]:
    """Check changed Markdown against the merged tree; `read` gives a tree file's new text, `read_old` the clone's."""
    stems = {PurePosixPath(p).stem for p in tree}
    names = {PurePosixPath(p).name for p in tree}
    problems = []
    for path in [p for p in created + modified if p.endswith(".md")]:
        text = read(path)
        wiki_page = path.startswith("wiki/") and PurePosixPath(path).name not in EXEMPT
        if wiki_page:
            meta = frontmatter_of(text)
            if meta is None:
                problems.append(f"{path}：frontmatter 无法解析")
            elif meta.get("type") not in PAGE_TYPES:
                problems.append(f"{path}：type 缺失或不是 source/concept/entity")
            elif meta["type"] != "source" and not (isinstance(meta.get("sources"), list) and meta["sources"]):
                problems.append(f"{path}：concept/entity 页缺少 sources")
        if PurePosixPath(path).full_match("projects/*/status.md"):
            meta = frontmatter_of(text) or {}
            if meta.get("type") != "project-status" or not meta.get("project") or not meta.get("as_of"):
                problems.append(f"{path} 缺少必填的 frontmatter（type/project/as_of）")
        if PurePosixPath(path).full_match("proposals/*.md"):
            meta = frontmatter_of(text) or {}
            if meta.get("type") != "proposal" or not meta.get("target") or meta.get("status") not in PROPOSAL_STATUSES:
                problems.append(f"{path} 的 frontmatter 不合规（type/target/status）")
        if PurePosixPath(path).full_match("practice/*.md"):
            meta = frontmatter_of(text) or {}
            if meta.get("type") != "practice" or meta.get("owner") != "human":
                problems.append(f"{path} 的 frontmatter 不合规（type/owner）")
        if path in modified and PurePosixPath(path).name == "log.md" and not text.startswith(read_old(path).rstrip()):
            problems.append(f"{path} 只能追加，不能改动已有内容")
        for link in WIKILINK.findall(text):
            target = link.split("|")[0].split("#")[0].strip()
            if target and not (target in tree or f"{target}.md" in tree or target in stems or target in names):
                problems.append(f"{path}：链接 [[{link}]] 找不到目标")
        if wiki_page and path in created:
            problems += duplicate_names(path, tree, read)
    return problems


def duplicate_names(path: str, tree: set[str], read: Callable[[str], str]) -> list[str]:
    stem = PurePosixPath(path).stem.casefold()
    clashes = []
    for other in sorted(tree):
        if other == path or not (other.startswith("wiki/") and other.endswith(".md")):
            continue
        names = {PurePosixPath(other).stem.casefold()}
        aliases = (frontmatter_of(read(other)) or {}).get("aliases") or []
        names |= {str(a).casefold() for a in ([aliases] if isinstance(aliases, str) else aliases)}
        if stem in names:
            clashes.append(f"{path}：与已有页面 {other} 重名")
    return clashes
