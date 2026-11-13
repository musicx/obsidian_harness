"""Deterministic capture of URLs and local files into raw/ entries."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from markitdown import MarkItDown

from work_sheet.vault import dump_frontmatter, now, parse_frontmatter

MIN_CONTENT = 200
USER_AGENT = "Mozilla/5.0 (compatible; worksheet-capture)"
FETCH_FAILED = "抓取失败或内容为空，可能需要登录或页面依赖 JS 渲染；请下载后把文件拖进来"


class CaptureError(Exception):
    pass


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.hostname or "").removeprefix("www.") in {"x.com", "twitter.com"}:
        query = ""
    else:
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                           if not k.startswith("utm_")])
    return urlunsplit(parts._replace(query=query))


def download(url: str) -> tuple[str, bytes] | None:
    """GET the URL; returns (Content-Type, body), or None when the request fails."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.headers.get("Content-Type", ""), response.read()
    except OSError:
        return None


def fetch_url(url: str) -> dict:
    """Return defuddle's JSON (title, content, author, published, ...) for a web page."""
    try:
        proc = subprocess.run(["npx", "--yes", "defuddle", "parse", url, "--markdown", "--json"],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode:
            raise CaptureError(FETCH_FAILED)
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        raise CaptureError(FETCH_FAILED)


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9぀-ヿ㐀-鿿가-힯]+", "-", title.lower()).strip("-")
    return slug[:60].strip("-") or "untitled"


def find_duplicate(root: Path, sha: str) -> str | None:
    for source in sorted((root / "raw").glob("**/source.md")):
        try:
            meta, _ = parse_frontmatter(source.read_text())
        except (yaml.YAMLError, ValueError):
            continue
        if meta.get("sha256") == sha:
            return source.parent.relative_to(root).as_posix()
    return None


def write_entry(root: Path, title: str, markdown: str, meta: dict, sha: str,
                original: Path | None = None) -> str:
    if existing := find_duplicate(root, sha):
        return existing
    today = now()
    base = f"raw/{today:%Y/%m}/{today:%Y-%m-%d}-{slugify(title)}"
    entry, n = base, 1
    while (root / entry).exists():
        n += 1
        entry = f"{base}-{n}"
    (root / entry).mkdir(parents=True)
    fm = {"title": title, **meta, "captured": f"{today:%Y-%m-%dT%H:%M}", "sha256": sha}
    (root / entry / "source.md").write_text(dump_frontmatter(fm, markdown))
    if original:
        shutil.copyfile(original, root / entry / f"original{original.suffix.lower()}")
    return entry


def doc_title(markdown: str) -> str:
    """First Markdown heading, else first non-empty line, at most 100 characters."""
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    heading = next((line.lstrip("#").strip() for line in lines if re.match(r"#{1,6}\s", line)), None)
    return (heading or lines[0])[:100]


def capture_file(root: Path, path: Path, source: str) -> str:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if path.suffix.lower() in {".md", ".txt"}:
        return write_entry(root, path.stem, data.decode("utf-8", errors="replace"), {"source": source}, sha)
    try:
        markdown = MarkItDown().convert(str(path)).text_content
    except Exception:
        raise CaptureError("不支持或无法转换的文件格式")
    if len(markdown.strip()) < MIN_CONTENT:
        raise CaptureError(FETCH_FAILED)
    return write_entry(root, doc_title(markdown), markdown, {"source": source}, sha, original=path)


def capture_all(items: list[str], root: Path, mode: str = "all") -> tuple[list[str], list[str], str | None]:
    """Capture the items. Returns (entry dirs, failures as `<item>：<reason>`, block summary if the task
    must block). Mode `all` blocks on the first failure; `any` blocks only when every item fails."""
    context, failed = [], []
    for item in items:
        try:
            context.append(capture(item, root))
        except CaptureError as e:
            failed.append(f"{item}：{e}")
            if mode == "all":
                # Leave context empty so a retry captures every item again (dedup makes that cheap).
                return [], failed, f"材料采集失败：{failed[0]}"
    if failed and not context:
        return [], failed, "\n".join(f"材料采集失败：{f}" for f in failed)
    return context, failed, None


def capture(item: str, root: Path) -> str:
    """Capture a URL or local path into raw/; return the entry dir relative to root."""
    if item.startswith(("http://", "https://")):
        url = clean_url(item)
        # The response, not the URL suffix, decides: arXiv PDFs have no .pdf suffix and some .pdf links serve HTML.
        response = download(url)
        if response and (response[0].lower().startswith("application/pdf") or response[1].startswith(b"%PDF")):
            with tempfile.TemporaryDirectory() as tmp:
                pdf = Path(tmp) / "download.pdf"
                pdf.write_bytes(response[1])
                return capture_file(root, pdf, url)
        page = fetch_url(url)
        content = page.get("content") or ""
        if len(content) < MIN_CONTENT:
            raise CaptureError(FETCH_FAILED)
        title = page.get("title") or Path(urlsplit(url).path).stem or urlsplit(url).hostname
        meta = {"source": url}
        if page.get("author"):
            meta["author"] = page["author"]
        if page.get("published"):
            meta["doc_date"] = page["published"]
        return write_entry(root, title, content, meta, hashlib.sha256(content.encode()).hexdigest())
    path = Path(item).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise CaptureError("找不到文件")
    return capture_file(root, path, item)
