"""Read-only ClickHouse query tool for the analyst role: a minimal MCP server over stdio (`wsdata`).

The agent CLI launches it with the machine's system python: `python3 datatool.py`, with WS_DATATOOL_CONFIG naming a
JSON file {"sources": {name: {url, database, user, password_file, force_readonly, max_rows, timeout_seconds}},
"log": path}. `python3 datatool.py --selftest` prints the tools. Standard library only, Python 3.9 syntax.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ALLOWED = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "EXISTS")
MAX_TEXT = 20000
PROBE_SECONDS = 20  # describe_table's min/max query is skipped when slower than this
SOURCE = {"type": "string", "description": "数据源名称"}
TOOLS = [
    {"name": "list_tables", "description": "列出数据源里的表：表名、行数、注释。",
     "inputSchema": {"type": "object", "properties": {"source": SOURCE}, "required": ["source"]}},
    {"name": "describe_table", "description": "描述一张表：字段（名称、类型、注释）、行数、排序键、分区键，以及第一个日期字段的范围。",
     "inputSchema": {"type": "object", "properties": {"source": SOURCE, "table": {"type": "string"}},
                     "required": ["source", "table"]}},
    {"name": "query", "description": "执行一条只读 SQL（SELECT / WITH / SHOW / DESCRIBE / EXPLAIN / EXISTS），返回 Markdown 表格。"
                                     "不要写 FORMAT 子句；结果行数有上限。",
     "inputSchema": {"type": "object", "properties": {"source": SOURCE, "sql": {"type": "string"}},
                     "required": ["source", "sql"]}},
]


class ToolError(Exception):
    pass


def clean_sql(sql: str) -> str:
    """The single read-only statement in `sql`, without comments or a trailing `;`; raises ToolError otherwise."""
    text = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text).strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if ";" in text:
        raise ToolError("只能执行一条语句")
    keyword = re.match(r"[A-Za-z]+", text)
    if not keyword or keyword.group(0).upper() not in ALLOWED:
        raise ToolError("只允许 " + " / ".join(ALLOWED) + " 开头的只读查询")
    if re.search(r"\bFORMAT\s+\w+\s*$", text, re.I) or re.search(r"\bINTO\s+OUTFILE\b", text, re.I):
        raise ToolError("不要写 FORMAT 或 INTO OUTFILE 子句，结果格式由工具决定")
    return text


def post(source: dict, sql: str, timeout: float, settings: bool = True) -> str:
    params = {"database": source["database"]}
    if settings:
        if source.get("force_readonly", True):
            params["readonly"] = "1"
        params.update({"max_result_rows": str(source.get("max_rows", 200)), "result_overflow_mode": "break",
                       "max_execution_time": str(source.get("timeout_seconds", 60))})
    headers = {"X-ClickHouse-User": source.get("user") or "default"}
    if source.get("password_file"):
        with open(source["password_file"]) as f:
            headers["X-ClickHouse-Key"] = f.read().strip()
    request = urllib.request.Request(source["url"].rstrip("/") + "/?" + urllib.parse.urlencode(params),
                                     data=(sql + " FORMAT TabSeparatedWithNames").encode(), headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        error = e.read().decode("utf-8", "replace").strip()
        # A read-only account refuses the settings above; its own profile already enforces the limits.
        if settings and "readonly mode" in error:
            return post(source, sql, timeout, settings=False)
        raise ToolError("查询失败：" + error)
    except (urllib.error.URLError, OSError) as e:
        raise ToolError("查询失败：" + str(e))


def run(source: dict, sql: str, timeout: float = 0) -> tuple[list[str], list[list[str]]]:
    """(column names, rows) of a TabSeparatedWithNames result."""
    text = post(source, sql, timeout or source.get("timeout_seconds", 60) + 5)
    lines = [line for line in text.split("\n") if line]
    if not lines:
        return [], []
    return lines[0].split("\t"), [line.split("\t") for line in lines[1:]]


def table(columns: list[str], rows: list[list[str]], max_rows: int) -> tuple[str, int, bool]:
    """A Markdown table capped at max_rows and MAX_TEXT characters: (text, rows shown, truncated)."""
    def cells(values: list[str]) -> str:
        return "| " + " | ".join(v.replace("|", "\\|") for v in values) + " |"

    out = [cells(columns), "|" + "---|" * len(columns)]
    size, shown = len("\n".join(out)), 0
    for row in rows[:max_rows]:
        line = cells(row)
        if size + len(line) + 1 > MAX_TEXT:
            break
        out.append(line)
        size += len(line) + 1
        shown += 1
    truncated = shown < len(rows)
    return "\n".join(out) + f"\n\n（返回 {shown} 行{'；已截断' if truncated else ''}）", shown, truncated


def literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def list_tables(source: dict, args: dict) -> str:
    columns, rows = run(source, "SELECT name, total_rows, comment FROM system.tables "
                                "WHERE database = currentDatabase() ORDER BY name")
    return table(columns, rows, max(source.get("max_rows", 200), 1000))[0]


def describe_table(source: dict, args: dict) -> str:
    name = str(args.get("table") or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ToolError("表名只能包含字母、数字和下划线")
    columns, rows = run(source, "SELECT name, type, comment FROM system.columns WHERE database = currentDatabase() "
                                f"AND table = {literal(name)} ORDER BY position")
    if not rows:
        raise ToolError(f"找不到表 {name}")
    info_columns, info = run(source, "SELECT total_rows, sorting_key, partition_key FROM system.tables "
                                     f"WHERE database = currentDatabase() AND name = {literal(name)}")
    parts = [f"## {name}", table(columns, rows, 10000)[0]]
    if info:
        parts.append("\n".join(f"- {k}：{v}" for k, v in zip(info_columns, info[0])))
    date_column = next((r[0] for r in rows if re.match(r"(Nullable\()?Date", r[1])), None)
    if date_column:
        try:
            _, span = run(source, f"SELECT min(`{date_column}`), max(`{date_column}`) FROM `{name}`", PROBE_SECONDS)
            parts.append(f"- {date_column} 范围：{span[0][0]} ～ {span[0][1]}")
        except (ToolError, OSError, IndexError):
            pass
    return "\n\n".join(parts)


def query(source: dict, args: dict, log_path: str) -> str:
    sql = str(args.get("sql") or "")
    entry = {"source": args.get("source"), "sql": sql, "ok": False, "rows": 0, "error": None}
    try:
        columns, rows = run(source, clean_sql(sql))
        text, entry["rows"], _ = table(columns, rows, source.get("max_rows", 200))
        entry["ok"] = True
        return text
    except ToolError as e:
        entry["error"] = str(e)
        raise
    finally:
        if log_path:
            with open(log_path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def call(config: dict, name: str, args: dict) -> str:
    source = config["sources"].get(str(args.get("source")))
    if source is None:
        raise ToolError("未知的数据源，可选：" + "、".join(config["sources"]))
    if name == "list_tables":
        return list_tables(source, args)
    if name == "describe_table":
        return describe_table(source, args)
    if name == "query":
        return query(source, args, config.get("log") or "")
    raise ToolError(f"未知的工具 {name}")


def handle(config: dict, message: dict) -> dict | None:
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None  # notifications, e.g. notifications/initialized
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "wsdata", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = {"content": [{"type": "text", "text": call(config, params.get("name"),
                                                                 params.get("arguments") or {})}]}
        except ToolError as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> None:
    if sys.argv[1:] == ["--selftest"]:
        for tool in TOOLS:
            print(f"{tool['name']}: {tool['description']}")
        return
    with open(os.environ["WS_DATATOOL_CONFIG"]) as f:
        config = json.load(f)
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(config, json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
