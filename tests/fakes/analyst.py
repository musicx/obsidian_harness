"""Fake analyst runner: acts like pi with the view's .pi/mcp.json, i.e. starts the `wsdata` server from it,
runs one query through MCP, then writes an analysis note and a dataset page.

$WS_LISTING receives the view path and file list (the brief is copied next to it with suffix .brief.md).
$WS_MODE: ok (the note's sql block is the executed query) or unlogged (the note shows a query never run).
"""

import json
import os
import subprocess
from datetime import date
from pathlib import Path

view = Path.cwd()
files = sorted(p.relative_to(view).as_posix() for p in view.rglob("*") if p.is_file())
Path(os.environ["WS_LISTING"]).write_text("\n".join([str(view), *files]) + "\n")
Path(os.environ["WS_LISTING"]).with_suffix(".brief.md").write_text((view / "_brief.md").read_text())

server = json.loads((view / ".pi" / "mcp.json").read_text())["mcpServers"]["wsdata"]
sql = "SELECT 42 AS n"
messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "query", "arguments": {"source": "a-share-data", "sql": sql}}}]
subprocess.run([server["command"], *server["args"]], input="".join(json.dumps(m) + "\n" for m in messages),
               env=os.environ | server["env"], capture_output=True, text=True, check=True)

shown = sql if os.environ.get("WS_MODE", "ok") == "ok" else "SELECT 43 AS n"
(view / "analysis").mkdir(exist_ok=True)
(view / "analysis" / "2026-11-14 答案.md").write_text(
    f"---\ntype: analysis\nquestion: 答案是多少\nsource: a-share-data\nas_of: {date.today()}\n---\n"
    f"# 答案是 42\n\n## 查询\n\n```sql\n{shown};\n```\n")
(view / "datasets" / "a-share-data").mkdir(parents=True, exist_ok=True)
(view / "datasets" / "a-share-data" / "stock_daily.md").write_text(
    "---\ntype: dataset\nsource: a-share-data\ntable: stock_daily\ndescription: A 股日线行情\n---\n粒度：日。\n")
(view / "_report.md").write_text("---\nstatus: done\n---\n答案是 42。\n")
