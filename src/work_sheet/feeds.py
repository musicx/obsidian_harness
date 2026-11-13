"""Import externally generated briefings (JSON files on this machine) into briefings/<feed>/ (no LLM)."""

import io
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from markitdown import MarkItDown, StreamInfo

from work_sheet.config import Config, Feed
from work_sheet.context import read_meta
from work_sheet.vault import dump_frontmatter

log = logging.getLogger(__name__)

HTML_FAILED = "（原文只有 HTML，转换失败）"
RECENT_DAYS = {"daily": 7, "weekly": 21}  # how far back a briefing is shown to agents


def html_to_markdown(html: str, source: Path) -> str:
    try:
        text = MarkItDown().convert_stream(io.BytesIO(html.encode()),
                                           stream_info=StreamInfo(extension=".html", charset="utf-8")).text_content
    except Exception:
        text = ""
    if not text.strip():
        log.warning("feed %s: could not convert the HTML to Markdown", source)
        return HTML_FAILED
    return text


def briefing(feed: Feed, day: date, data: dict, source: Path) -> str:
    title = str(data.get("title") or day)
    meta = {"type": "briefing", "stream": feed.name, "stream_title": feed.title, "cadence": feed.cadence,
            "date": day, "title": title, "description": str(data.get("summary") or title)[:200]}
    if feed.url:
        meta["url"] = feed.url.replace("{date}", day.isoformat())
    meta["source_updated"] = data.get("updated_at")
    body = data.get("md") or html_to_markdown(str(data.get("html") or ""), source)
    return dump_frontmatter(meta, body.strip() + "\n")


def is_recent(path: Path, today: date) -> bool:
    """Whether a briefing file is recent enough to put into a view, judged from its frontmatter."""
    meta = read_meta(path.read_text(errors="replace"))[0]
    days = RECENT_DAYS.get(str(meta.get("cadence")))
    try:
        day = date.fromisoformat(str(meta.get("date"))[:10])
    except ValueError:
        return False
    return days is not None and day >= today - timedelta(days=days)


def import_feeds(cfg: Config) -> int:
    """Write new or updated briefings into the clone; returns how many files were written."""
    written = 0
    for feed in cfg.feeds.values():
        if not feed.source.is_dir():
            log.warning("feed %s: source directory %s does not exist", feed.name, feed.source)
            continue
        for source in sorted(feed.source.glob(feed.pattern)):
            try:
                data = json.loads(source.read_text())
                day = date.fromisoformat(str(data.get("date") or source.stem))
            except (OSError, ValueError, AttributeError) as e:
                log.warning("feed %s: skipping %s: %s", feed.name, source, e)
                continue
            if feed.since and day < date.fromisoformat(feed.since):
                continue
            target = cfg.clone / "briefings" / feed.name / f"{day:%Y}" / f"{day:%m}" / f"{day}.md"
            if target.exists() and read_meta(target.read_text())[0].get("source_updated") == data.get("updated_at"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(briefing(feed, day, data, source))
            written += 1
    return written
