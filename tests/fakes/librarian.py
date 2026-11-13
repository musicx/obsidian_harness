"""Fake librarian/tracker runner, launched by the engine inside a view directory.

$WS_LISTING receives the view path on line 1, then every file in the view; the brief is copied next to it
with the suffix .brief.md. $WS_MODE picks what it writes:
ok (valid source + concept pages, plus a _scratch note), outside (a file outside its write scope),
generated (rewrites wiki/index.md), deadlink (a link that resolves nowhere), projlog_rewrite (rewrites
projects/alpha/log.md), status_missing (projects/alpha/status.md without as_of), practice_new /
practice_edit (practice pages, needing a grant), bad_frontmatter (proposal and practice pages missing fields),
repairable (a dead link, fixed once _feedback.md exists), noop (only a report), conflict (the human clone at $WS_HUMAN rewrites the thread page and pushes mid-run).
"""

import os
import re
import subprocess
from pathlib import Path

view = Path.cwd()
files = sorted(p.relative_to(view).as_posix() for p in view.rglob("*") if p.is_file())
Path(os.environ["WS_LISTING"]).write_text("\n".join([str(view), *files]) + "\n")
brief = (view / "_brief.md").read_text()
Path(os.environ["WS_LISTING"]).with_suffix(".brief.md").write_text(brief)


def write(rel: str, text: str) -> None:
    path = view / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def source_page(body: str) -> str:
    """A valid source page; its raw is this task's material, or the existing page's raw on a follow-up."""
    entry = re.search(r"^  - (raw/\S+)$", brief, re.M)
    existing = view / "wiki/sources/样例文章.md"
    raw = entry.group(1) if entry else re.search(r"^raw: (.+)$", existing.read_text(), re.M).group(1)
    return f"---\ntype: source\ndescription: 一篇样例文章的摘要\nraw: {raw}\n---\n{body}\n"


def done(summary: str = "完成。") -> None:
    write("_report.md", f"---\nstatus: done\n---\n{summary}\n")


mode = os.environ.get("WS_MODE", "ok")
if mode == "ok":
    write("wiki/sources/样例文章.md", source_page("要点见 [[概念甲]]。"))
    write("wiki/concepts/概念甲.md",
          '---\ntype: concept\ndescription: 概念甲的定义\nsources: ["[[样例文章]]"]\n---\n定义。\n')
    write("_scratch/claims.md", "工作笔记，不导入。\n")
    done("新建 [[样例文章]]、[[概念甲]]。")
elif mode == "outside":
    write("practice/我们的做法.md", "改了我方文档。\n")
    done()
elif mode == "generated":
    write("wiki/index.md", "# 手写的目录\n")
    done()
elif mode == "conflict":
    # Simulate the human rewriting the thread page while the task runs.
    human = os.environ["WS_HUMAN"]
    subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], cwd=human, check=True)
    Path(human, "inbox/a.md").write_text("人改了这一页\n")
    subprocess.run(["git", "commit", "-qam", "human rewrite"], cwd=human, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=human, check=True)
    write("wiki/sources/样例文章.md", source_page("摘要。"))
    done()
elif mode == "projlog_rewrite":
    write("projects/alpha/log.md", "## 2026-11-13 改写了旧记录\n")
    done()
elif mode == "status_missing":
    write("projects/alpha/status.md", "---\ntype: project-status\nproject: alpha\n---\n## 概要\n")
    done()
elif mode == "practice_new":
    write("practice/新做法.md", "---\ntype: practice\nowner: human\ndescription: 我们的新做法\n---\n我们这样做。\n")
    done("起草了做法。")
elif mode == "practice_edit":
    page = view / "practice" / "现有做法.md"
    write("practice/现有做法.md", page.read_text() + "按建议补充的一段。\n")
    done("按建议修改。")
elif mode == "bad_frontmatter":
    write("proposals/2026-11-13-建议.md", "---\ntype: proposal\ntarget: \"[[新做法]]\"\n---\n表格。\n")
    write("practice/新做法.md", "---\ntype: practice\ndescription: 新做法\n---\n我们这样做。\n")
    done()
elif mode == "repairable":
    # Writes a dead link, and fixes it only when the engine asks for a repair.
    fixed = (view / "_feedback.md").exists()
    write("wiki/sources/样例文章.md", source_page("要点。" if fixed else "见 [[不存在的页面]]。"))
    done("修正了链接。" if fixed else "完成。")
elif mode == "noop":
    done("没有改动。")
elif mode == "deadlink":
    write("wiki/sources/样例文章.md", source_page("见 [[不存在的页面]]。"))
    done()
