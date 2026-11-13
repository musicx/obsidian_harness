"""Fake librarian runner, launched by the engine inside a view directory.

$WS_LISTING receives the view path on line 1, then every file in the view.
$WS_MODE picks what it writes: ok (valid wiki pages), outside (a file outside its write scope),
deadlink (a wiki page with a link that resolves nowhere), logrewrite (rewrites wiki/log.md),
status_missing (projects/alpha/status.md without as_of), practice_new / practice_edit (write practice
pages, needing a grant), bad_frontmatter (proposal and practice pages missing fields), conflict (the human clone at $WS_HUMAN
rewrites the thread page and pushes mid-run).
"""

import os
import re
import subprocess
from pathlib import Path

view = Path.cwd()
files = sorted(p.relative_to(view).as_posix() for p in view.rglob("*") if p.is_file())
Path(os.environ["WS_LISTING"]).write_text("\n".join([str(view), *files]) + "\n")
Path(os.environ["WS_LISTING"]).with_suffix(".brief.md").write_text((view / "_brief.md").read_text())


def write(rel: str, text: str) -> None:
    path = view / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


mode = os.environ.get("WS_MODE", "ok")
entry = re.search(r"^  - (raw/\S+)$", (view / "_brief.md").read_text(), re.M)
log = view / "wiki" / "log.md"
if mode == "ok":
    raw_link = f"，原文 [[{entry.group(1)}/source|原文]]" if entry else ""
    write("wiki/sources/样例文章.md", f"---\ntype: source\n---\n要点见 [[概念甲]]{raw_link}。\n")
    write("wiki/concepts/概念甲.md", '---\ntype: concept\nsources: ["[[样例文章]]"]\n---\n定义。\n')
    write("wiki/log.md", (log.read_text() if log.exists() else "") + "## [2026-11-13] ingest | 样例文章\n")
    write("wiki/index.md", "# 目录\n\n- [[样例文章]]\n")
    write("_report.md", "---\nstatus: done\n---\n新建 [[样例文章]]、[[概念甲]]。\n")
elif mode == "outside":
    write("wiki/sources/样例文章.md", "---\ntype: source\n---\n摘要。\n")
    write("practice/我们的做法.md", "改了我方文档。\n")
    write("_report.md", "---\nstatus: done\n---\n完成。\n")
elif mode == "conflict":
    # Simulate the human rewriting the thread page while the task runs.
    human = os.environ["WS_HUMAN"]
    subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], cwd=human, check=True)
    Path(human, "inbox/a.md").write_text("人改了这一页\n")
    subprocess.run(["git", "commit", "-qam", "human rewrite"], cwd=human, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=human, check=True)
    write("wiki/sources/样例文章.md", "---\ntype: source\n---\n摘要。\n")
    write("_report.md", "---\nstatus: done\n---\n完成。\n")
elif mode == "logrewrite":
    write("wiki/log.md", "## [2026-11-13] 改写了旧记录\n")
    write("_report.md", "---\nstatus: done\n---\n完成。\n")
elif mode == "status_missing":
    write("projects/alpha/status.md", "---\ntype: project-status\nproject: alpha\n---\n## 概要\n")
    write("_report.md", "---\nstatus: done\n---\n完成。\n")
elif mode == "practice_new":
    write("practice/新做法.md", "---\ntype: practice\nowner: human\n---\n我们这样做。\n")
    write("_report.md", "---\nstatus: done\n---\n起草了做法。\n")
elif mode == "practice_edit":
    page = view / "practice" / "现有做法.md"
    write("practice/现有做法.md", page.read_text() + "按建议补充的一段。\n")
    write("_report.md", "---\nstatus: done\n---\n按建议修改。\n")
elif mode == "bad_frontmatter":
    write("proposals/2026-11-13-建议.md", "---\ntype: proposal\ntarget: \"[[新做法]]\"\n---\n表格。\n")
    write("practice/新做法.md", "---\ntype: practice\n---\n我们这样做。\n")
    write("_report.md", "---\nstatus: done\n---\n完成。\n")
elif mode == "deadlink":
    write("wiki/sources/样例文章.md", "---\ntype: source\n---\n见 [[不存在的页面]]。\n")
    write("_report.md", "---\nstatus: done\n---\n完成。\n")
