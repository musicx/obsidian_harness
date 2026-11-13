"""Deterministic context packs (no LLM), written into a run's view as `_context/<pack>.md`."""

import math
import re
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from work_sheet.vault import as_list, is_generated, link_target, parse_frontmatter, split_frontmatter

PACKS = ("names", "candidates", "health", "changes")
PAGE_DIRS = ("wiki/sources", "wiki/concepts", "wiki/entities", "practice")
NOT_PAGES = {"_index.md", "index.md", "log.md", "topics.md"}
CHANGE_DIRS = ("wiki/", "practice/", "proposals/", "projects/", "outputs/")
CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
WIKILINK = re.compile(r"\[\[([^\]]+?)\]\]")
TOP_CANDIDATES = 15
MAX_FORMING = 100
NAME_BONUS = 10
FIELD_WEIGHTS = (3, 2, 1)  # name/aliases, description, body
K1, B = 1.2, 0.75
SIMILAR = 0.6
STALE_DAYS = 180
CHANGE_DAYS = 14


@dataclass
class Page:
    path: str
    name: str
    type: str
    aliases: list[str]
    description: str
    described: bool  # the description comes from frontmatter
    meta: dict
    body: str


def read_meta(text: str) -> tuple[dict, str]:
    try:
        return parse_frontmatter(text)
    except (yaml.YAMLError, ValueError):
        return {}, split_frontmatter(text)[1]


def plain(line: str) -> str:
    """A Markdown line without list/heading/quote markers, emphasis, or link syntax."""
    line = re.sub(r"^\s*(?:[#>]+\s*|[-*+]\s+|\d+[.)]\s+)*", "", line)
    line = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", line)
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    return re.sub(r"\*\*|__|`", "", line).strip()


def load_pages(root: Path) -> list[Page]:
    pages = []
    for top in PAGE_DIRS:
        for path in sorted((root / top).glob("**/*.md")):
            if path.name in NOT_PAGES:
                continue
            meta, body = read_meta(path.read_text(errors="replace"))
            described = str(meta.get("description") or "").strip()
            first = next((plain(line) for line in body.splitlines() if plain(line)), "")
            pages.append(Page(path=path.relative_to(root).as_posix(), name=path.stem,
                              type=str(meta.get("type") or ""), aliases=as_list(meta.get("aliases")),
                              description=(described or first)[:120], described=bool(described),
                              meta=meta, body=body))
    return pages


def tokenize(text: str) -> list[str]:
    """Casefolded Latin/digit words plus CJK character bigrams (a lone CJK character counts as itself)."""
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens = re.findall(r"[a-z0-9]+", text)
    for run in re.findall(f"[{CJK}]+", text):
        tokens += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
    return tokens


def name_key(name: str) -> str:
    """Normalized page name for duplicate detection: `Large Language Models (LLM)` -> `large language model`."""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text)
    words = re.sub(r"[\W_]+", " ", text).split()
    return " ".join(w[:-1] if re.fullmatch(r"[a-z]{4,}", w) and w.endswith("s") else w for w in words)


def cell(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def section(title: str, lines: list[str]) -> str:
    return f"## {title}\n\n" + ("\n".join(lines) or "（无）") + "\n"


def query_text(root: Path, brief: str, thread: str, context_dirs: list[str]) -> str:
    parts = [brief, (root / thread).read_text() if (root / thread).exists() else ""]
    for d in context_dirs:
        meta, body = read_meta((root / d / "source.md").read_text(errors="replace"))
        parts += [str(meta.get("title") or ""), body[:4000]]
    return "\n".join(parts)


def names_pack(pages: list[Page]) -> str:
    rows = [f"| {cell(p.name)} | {cell(p.type)} | {cell('、'.join(p.aliases))} | {cell(p.description)} |"
            for p in sorted(pages, key=lambda p: (p.name.casefold(), p.path))]
    return "# 页面名称表\n\n| 名称 | 类型 | 别名 | 说明 |\n|---|---|---|---|\n" + "".join(r + "\n" for r in rows)


def mentioned(name: str, query: str) -> bool:
    """Whether `name` occurs in the casefolded `query`; Latin names must match whole words."""
    name = name.casefold()
    if name.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", query) is not None
    return name in query


def score_pages(pages: list[Page], query: str) -> list[tuple[float, Page, bool]]:
    """(score, page, name mentioned) for pages scoring above zero, best first."""
    if not pages:
        return []
    docs = []
    for p in pages:
        tf = Counter()
        for weight, text in zip(FIELD_WEIGHTS, (" ".join([p.name, *p.aliases]), p.description, p.body)):
            for token in tokenize(text):
                tf[token] += weight
        docs.append(tf)
    avgdl = sum(sum(d.values()) for d in docs) / len(docs) or 1
    df = Counter(t for d in docs for t in d)
    terms = set(tokenize(query))
    query = query.casefold()
    scored = []
    for p, tf in zip(pages, docs):
        dl = sum(tf.values())
        bm25 = sum(math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                   * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * dl / avgdl)) for t in terms if t in tf)
        named = any(mentioned(n, query) for n in [p.name, *p.aliases])
        score = bm25 + (NAME_BONUS if named else 0)
        if score > 0:
            scored.append((score, p, named))
    return sorted(scored, key=lambda s: (-s[0], s[1].path))


def forming_concepts(pages: list[Page]) -> list[tuple[str, list[str]]]:
    """Concepts named in source pages' `mentions` that have no page yet: (concept, source page names)."""
    known = {name_key(n) for p in pages for n in [p.name, *p.aliases]}
    forming: dict[str, tuple[str, list[str]]] = {}
    for p in pages:
        if not p.path.startswith("wiki/sources/"):
            continue
        for mention in map(link_target, as_list(p.meta.get("mentions"))):
            key = name_key(mention)
            if key and key not in known:
                label, sources = forming.setdefault(key, (mention, []))
                if p.name not in sources:
                    sources.append(p.name)
    ranked = sorted(forming.values(), key=lambda f: (-len(f[1]), f[0].casefold()))
    return ranked[:MAX_FORMING]


def candidates_pack(pages: list[Page], query: str) -> str:
    rows = [f"| {cell(p.path)} | {cell(p.type)} | {cell('、'.join(p.aliases))} | {cell(p.description)} | "
            f"{'名称出现在材料中' if named else '关键词相关'} |"
            for _, p, named in score_pages(pages, query)[:TOP_CANDIDATES]]
    table = ("| 页面 | 类型 | 别名 | 说明 | 原因 |\n|---|---|---|---|---|\n" + "".join(r + "\n" for r in rows)
             if rows else "（无）\n")
    forming = [f"- {c}：" + "、".join(f"[[{s}]]" for s in sources) for c, sources in forming_concepts(pages)]
    return "# 可能相关的已有页面\n\n" + table + "\n" + section("形成中的概念", forming)


def health_pack(root: Path, pages: list[Page], today: date) -> str:
    entities = [p for p in pages if p.type in ("concept", "entity")]
    similar = []
    for i, a in enumerate(entities):
        for b in entities[i + 1:]:
            ka, kb = name_key(a.name), name_key(b.name)
            ta, tb = set(tokenize(ka)), set(tokenize(kb))
            if ka == kb or (ta and tb and len(ta & tb) / len(ta | tb) >= SIMILAR):
                similar.append(f"- {a.path} ↔ {b.path}")
    promotable = [f"- {c}：{len(s)} 个来源（" + "、".join(f"[[{x}]]" for x in s) + "）"
                  for c, s in forming_concepts(pages) if len(s) >= 2]
    links = []  # (linking file, target)
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).as_posix()
        # Thread pages and task notes link every output page, so they would hide every orphan.
        if rel.startswith((".git/", "inbox/", "tasks/")) or is_generated(rel):
            continue
        links += [(rel, link_target(t)) for t in WIKILINK.findall(path.read_text(errors="replace"))]
    orphans = [f"- {p.path}" for p in entities
               if not any(src != p.path and t in (p.name, p.path, p.path.removesuffix(".md")) for src, t in links)]
    undescribed = [f"- {p.path}" for p in pages if not p.described]
    conflicts = [f"- {p.path}" for p in pages if "[!conflict]" in p.body]
    stale = []
    for p in pages:
        try:
            updated = date.fromisoformat(str(p.meta.get("updated"))[:10])
        except ValueError:
            continue
        if updated < today - timedelta(days=STALE_DAYS):
            stale.append(f"- {p.path}（updated: {updated}）")
    return "# 知识库健康检查\n\n" + "\n".join([
        section("名称相近的页面", similar), section("可升格的概念", promotable), section("孤儿页", orphans),
        section("缺少描述的页面", undescribed), section("带冲突标记的页面", conflicts),
        section("长期未更新的页面", stale)])


def changes_pack(root: Path) -> str:
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "log", f"--since={CHANGE_DAYS} days ago", "--date=short",
         "--format=%x00%ad%x1f%s%x1f%(trailers:key=Task,valueonly,separator=%x2C)", "--name-status"],
        cwd=root, check=True, capture_output=True, text=True).stdout
    days: dict[str, list[str]] = {}
    for chunk in out.split("\0")[1:]:
        header, _, files = chunk.partition("\n")
        day, subject, tasks = header.split("\x1f")
        changed = []
        for line in files.splitlines():
            fields = line.split("\t")
            path = fields[-1]
            if fields[0] in ("A", "M") and path.startswith(CHANGE_DIRS) and not is_generated(path):
                changed.append(f"  - {'新建' if fields[0] == 'A' else '修改'} {path}")
        if tasks.strip() and changed:
            days.setdefault(day, []).append(f"- {tasks.strip()}：{subject}\n" + "\n".join(changed))
    body = "\n".join(f"## {day}\n\n" + "\n".join(entries) + "\n" for day, entries in days.items())
    return f"# 最近 {CHANGE_DAYS} 天的改动\n\n" + (body or "（无）\n")


def write_packs(root: Path, packs: list[str], out_dir: Path, query: str) -> list[str]:
    """Write the named packs into out_dir; returns their file names."""
    pages = load_pages(root)
    build = {"names": lambda: names_pack(pages), "candidates": lambda: candidates_pack(pages, query),
             "health": lambda: health_pack(root, pages, date.today()), "changes": lambda: changes_pack(root)}
    out_dir.mkdir(parents=True, exist_ok=True)
    for pack in packs:
        (out_dir / f"{pack}.md").write_text(build[pack]())
    return [f"{pack}.md" for pack in packs]
