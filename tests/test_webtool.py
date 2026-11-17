import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from work_sheet import webtool
from work_sheet.tasks import WEBTOOL, mount_tools


@pytest.fixture
def deepseek(monkeypatch):
    """A fake DeepSeek tools API; yields the list of (path, body, auth header) it received."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body, self.headers["Authorization"]))
            if self.path.endswith("/search"):
                reply = {"results": [{"title": "T", "url": "https://a.example/x", "site_name": "A",
                                      "date": "2026-11-01", "summary": "S", "snippet": "s"}]}
            elif body["url"] == "https://gone.example/":
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "url_not_accessible"}}')
                return
            else:
                reply = {"title": "Page", "content": "x" * (webtool.MAX_TEXT + 10)}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(webtool, "API", f"http://127.0.0.1:{server.server_port}/v1/tools")
    monkeypatch.setattr(webtool, "api_key", lambda: "k")
    yield requests
    server.shutdown()


def call(name: str, **args) -> dict:
    return webtool.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})["result"]


def test_search_and_fetch(deepseek):
    result = call("web_search", query="q", count=99)
    assert result == {"content": [{"type": "text", "text": "1. T\n   https://a.example/x\n   A 2026-11-01\n   S"}]}
    assert deepseek[0] == ("/v1/tools/search", {"query": "q", "count": 20}, "Bearer k")

    text = call("web_fetch", url="https://a.example/x")["content"][0]["text"]
    assert text.startswith("# Page\n\nxxx") and text.endswith(f"（正文共 {webtool.MAX_TEXT + 10} 字符，已截断）")


def test_errors_are_tool_errors(deepseek):
    assert call("web_fetch", url="https://gone.example/")["isError"]
    assert call("web_fetch", url="file:///etc/passwd")["isError"]
    assert call("web_search", query=" ")["isError"]
    assert len(deepseek) == 1  # the invalid arguments never reached the API


def test_only_web_roles_get_the_web_tool(tmp_path):
    mount_tools(None, tmp_path, [], web=False)
    assert not (tmp_path / ".pi").exists()
    mount_tools(None, tmp_path, [], web=True)
    servers = json.loads((tmp_path / ".pi" / "mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["wsweb"] and servers["wsweb"]["args"] == [str(WEBTOOL)]
