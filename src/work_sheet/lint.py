"""Mechanical checks on Markdown files a task created or modified."""

from collections.abc import Callable
from pathlib import PurePosixPath

import yaml

from work_sheet.context import WIKILINK, name_key
from work_sheet.generated import parse_topics
from work_sheet.vault import as_list, link_target, parse_frontmatter, raw_dir, split_frontmatter

EXEMPT = {"index.md", "_index.md", "log.md", "topics.md"}
PAGE_TYPES = {"source", "concept", "entity"}
PROPOSAL_STATUSES = {"proposed", "accepted", "partially-accepted", "rejected"}
NAMED_PAGES = ("wiki/sources/*.md", "wiki/concepts/*.md", "wiki/entities/*.md")
ENTITY_PAGES = NAMED_PAGES[1:]  # concepts and entities
DESCRIBED_PAGES = (*NAMED_PAGES, "practice/*.md")
MAX_DESCRIPTION = 200
BAD_NAME_CHARS = set('"\\:*?<>|')
MIN_KEPT = 0.7


def frontmatter_of(text: str) -> dict | None:
    try:
        return parse_frontmatter(text)[0]
    except (yaml.YAMLError, ValueError):
        return None


def is_page(path: str, patterns: tuple[str, ...]) -> bool:
    p = PurePosixPath(path)
    return p.name not in EXEMPT and any(p.full_match(pattern) for pattern in patterns)


def bad_file_name(path: str) -> bool:
    """A file name other platforms cannot sync; only the name is checked, not its directories."""
    name = PurePosixPath(path).name
    return bool(set(name) & BAD_NAME_CHARS) or name != name.strip(" ")


def lint(created: list[str], modified: list[str], tree: set[str], read: Callable[[str], str],
         read_old: Callable[[str], str], allow_shrink: bool = False) -> list[str]:
    """Check changed Markdown against the merged tree; `read` gives a tree file's new text, `read_old` the clone's."""
    stems = {PurePosixPath(p).stem for p in tree}
    names = {PurePosixPath(p).name for p in tree}
    topics = parse_topics(read("wiki/topics.md")) if "wiki/topics.md" in tree else {}
    problems = [f"{p} 的文件名含有不能使用的字符（\" \\ : * ? < > |），请改名" for p in created if bad_file_name(p)]
    for path in [p for p in created + modified if p.endswith(".md")]:
        text = read(path)
        meta = frontmatter_of(text)
        wiki_page = path.startswith("wiki/") and PurePosixPath(path).name not in EXEMPT
        if wiki_page:
            if meta is None:
                problems.append(f"{path}：frontmatter 无法解析")
            elif meta.get("type") not in PAGE_TYPES:
                problems.append(f"{path}：type 缺失或不是 source/concept/entity")
            elif meta["type"] != "source" and not (isinstance(meta.get("sources"), list) and meta["sources"]):
                problems.append(f"{path}：concept/entity 页缺少 sources")
        meta = meta or {}
        if is_page(path, DESCRIBED_PAGES):
            description = meta.get("description")
            if not (isinstance(description, str) and description.strip()) or len(description) > MAX_DESCRIPTION:
                problems.append(f"{path} 缺少 description（一句话说明这里有什么、什么时候该读），或超过 200 字")
            if meta.get("topics") is not None:
                values = meta["topics"] if isinstance(meta["topics"], list) else [meta["topics"]]
                problems += [f"{path} 使用了未登记的主题：{t}（先在 wiki/topics.md 登记）"
                             for t in values if str(t) not in topics]
        if is_page(path, ("wiki/sources/*.md",)):
            entry = raw_dir(meta.get("raw"))
            if not (entry.startswith("raw/") and f"{entry}/source.md" in tree):
                problems.append(f"{path} 的 raw 未指向已存档的原文")
        if PurePosixPath(path).full_match("projects/*/status.md"):
            if meta.get("type") != "project-status" or not meta.get("project") or not meta.get("as_of"):
                problems.append(f"{path} 缺少必填的 frontmatter（type/project/as_of）")
        if PurePosixPath(path).full_match("proposals/*.md"):
            if meta.get("type") != "proposal" or not meta.get("target") or meta.get("status") not in PROPOSAL_STATUSES:
                problems.append(f"{path} 的 frontmatter 不合规（type/target/status）")
        if PurePosixPath(path).full_match("practice/*.md"):
            if meta.get("type") != "practice" or meta.get("owner") != "human":
                problems.append(f"{path} 的 frontmatter 不合规（type/owner）")
        if path in modified and PurePosixPath(path).full_match("projects/*/log.md") \
                and not text.startswith(read_old(path).rstrip()):
            problems.append(f"{path} 只能追加，不能改动已有内容")
        if path in modified and not allow_shrink and is_page(path, ENTITY_PAGES):
            new_body, old_body = split_frontmatter(text)[1].strip(), split_frontmatter(read_old(path))[1].strip()
            if len(new_body) < MIN_KEPT * len(old_body):
                problems.append(f"{path} 的内容缩减超过 30%，疑似丢失已有内容")
        for link in WIKILINK.findall(text):
            target = link_target(link)
            if target and not (target in tree or f"{target}.md" in tree or target in stems or target in names):
                problems.append(f"{path}：链接 [[{link}]] 找不到目标")
        if path in created and is_page(path, NAMED_PAGES):
            problems += duplicate_names(path, tree, read)
    return problems


def duplicate_names(path: str, tree: set[str], read: Callable[[str], str]) -> list[str]:
    """Exact names are unique across wiki pages; concept/entity names must also not be near-duplicates
    (normalized key) of another concept/entity page's name or alias."""
    stem = PurePosixPath(path).stem
    near = is_page(path, ENTITY_PAGES)
    clashes = []
    for other in sorted(p for p in tree if p != path and is_page(p, NAMED_PAGES)):
        other_stem = PurePosixPath(other).stem
        if stem.casefold() == other_stem.casefold() or (near and is_page(other, ENTITY_PAGES) and name_key(stem) in {
                name_key(n) for n in [other_stem, *as_list((frontmatter_of(read(other)) or {}).get("aliases"))]}):
            clashes.append(f"{path} 与已有页面 {other} 重名或近似，请并入已有页面")
    return clashes
