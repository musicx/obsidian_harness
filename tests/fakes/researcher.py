"""Fake researcher runner, launched by the engine inside a view directory.

Records its argv and $WS_RUNNER_TAG into $WS_RESEARCH_OUT (JSON), writes a note under work/<task>/,
and reports the items in $WS_RESEARCH_CAPTURE (JSON list) as `capture` with a handoff to librarian.
"""

import json
import os
import re
import sys
from pathlib import Path

view = Path.cwd()
Path(os.environ["WS_RESEARCH_OUT"]).write_text(
    json.dumps({"argv": sys.argv[1:], "tag": os.environ.get("WS_RUNNER_TAG")}, ensure_ascii=False))
task_id = re.match(r"# 任务 (\S+)：", (view / "_brief.md").read_text()).group(1)
(view / "work" / task_id).mkdir(parents=True)
(view / "work" / task_id / "搜集记录.md").write_text("找到 1 份资料，舍弃 0 份。\n")
capture = json.loads(os.environ["WS_RESEARCH_CAPTURE"])
(view / "_report.md").write_text(
    "---\nstatus: done\ncapture: " + json.dumps(capture, ensure_ascii=False)
    + "\nhandoff:\n  - role: librarian\n    reason: 已找到资料，可以收录\n---\n找到 1 份资料。\n")
