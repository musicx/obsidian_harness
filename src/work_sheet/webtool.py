"""Web search and page fetch for the researcher role: a minimal MCP server over stdio (`wsweb`).

Backed by DeepSeek's standalone Search and Fetch APIs; the key comes from `pi auth print-api-key --provider deepseek`
(pi from WORKSHEET_PI_BIN), so it stays with the agent CLI. The agent CLI launches it with the machine's system
python: `python3 webtool.py`; `python3 webtool.py --selftest` prints the tools. Standard library only, Python 3.9 syntax.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.deepseek.com/v1/tools"
MAX_TEXT = 50000  # characters of one fetched page handed back to the model
TIMEOUT = 60
TOOLS = [
    {"name": "web_search", "description": "联网搜索，返回标题、链接、日期和摘要。支持 site:、-site:、lang:、after:、before: "
                                          "和带引号的短语。摘要只用来挑选页面，采用前要用 web_fetch 打开全文。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "搜索词，最多 128 个字符"},
         "count": {"type": "integer", "description": "结果数，1–20，默认 8"}}, "required": ["query"]}},
    {"name": "web_fetch", "description": "打开一个网页，返回标题和 Markdown 正文（过长时截断）。",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]


class ToolError(Exception):
    pass


def api_key() -> str:
    out = subprocess.run([os.environ.get("WORKSHEET_PI_BIN", "pi"), "auth", "print-api-key", "--provider", "deepseek"],
                         capture_output=True, text=True)
    if out.returncode or not out.stdout.strip():
        raise ToolError("取不到 DeepSeek 的 API key（pi auth print-api-key --provider deepseek 失败）")
    return out.stdout.strip()


def post(path: str, body: dict) -> dict:
    request = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {api_key()}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise ToolError(f"请求失败（HTTP {e.code}）：" + e.read().decode("utf-8", "replace").strip())
    except (urllib.error.URLError, OSError) as e:
        raise ToolError("请求失败：" + str(e))


def web_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query 不能为空")
    count = min(max(int(args.get("count") or 8), 1), 20)
    results = post("search", {"query": query[:128], "count": count}).get("results") or []
    if not results:
        return "没有结果"
    return "\n\n".join(f"{i}. {r.get('title') or '（无标题）'}\n   {r.get('url')}\n"
                       f"   {r.get('site_name') or ''} {r.get('date') or ''}\n   {r.get('summary') or r.get('snippet') or ''}"
                       for i, r in enumerate(results, 1))


def web_fetch(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("url 必须以 http:// 或 https:// 开头")
    page = post("fetch", {"url": url})
    content = page.get("content") or ""
    if len(content) > MAX_TEXT:
        content = content[:MAX_TEXT] + f"\n\n（正文共 {len(content)} 字符，已截断）"
    return f"# {page.get('title') or url}\n\n{content}"


def handle(message: dict) -> dict | None:
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None  # notifications, e.g. notifications/initialized
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "wsweb", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        tool = {"web_search": web_search, "web_fetch": web_fetch}.get(params.get("name"))
        try:
            if tool is None:
                raise ToolError(f"未知的工具 {params.get('name')}")
            result = {"content": [{"type": "text", "text": tool(params.get("arguments") or {})}]}
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
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
