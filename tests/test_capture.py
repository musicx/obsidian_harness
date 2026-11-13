import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from work_sheet import capture as cap
from work_sheet.vault import parse_frontmatter


@pytest.fixture
def fetched(monkeypatch):
    calls = []
    page = {"title": "A Great Post: 架构 & Design!", "content": "x" * 300, "author": "Ann", "published": "2026-11-10"}

    def fake_fetch(url):
        calls.append(url)
        return dict(page)

    monkeypatch.setattr(cap, "fetch_url", fake_fetch)
    monkeypatch.setattr(cap, "download", lambda url: ("text/html", b"<html></html>"))
    return calls, page


def test_url_capture_layout_query_stripping_and_dedup(tmp_path, fetched):
    calls, page = fetched
    entry = cap.capture("https://x.com/user/status/1?s=20&t=abc", tmp_path)
    assert calls == ["https://x.com/user/status/1"]
    today = datetime.now()
    assert entry == f"raw/{today:%Y/%m}/{today:%Y-%m-%d}-a-great-post-架构-design"
    meta, body = parse_frontmatter((tmp_path / entry / "source.md").read_text())
    assert meta["source"] == "https://x.com/user/status/1" and meta["author"] == "Ann"
    assert meta["doc_date"] == "2026-11-10" and len(meta["sha256"]) == 64 and body == page["content"]

    assert cap.capture("https://example.com/p?id=7&utm_source=tw&utm_medium=s", tmp_path) == entry
    assert calls[-1] == "https://example.com/p?id=7"
    assert len(list(tmp_path.glob("raw/**/source.md"))) == 1

    page["content"] = "y" * 300
    assert cap.capture("https://example.com/other", tmp_path) == entry + "-2"


def test_short_content_is_capture_error(tmp_path, fetched):
    fetched[1]["content"] = "登录后查看"
    with pytest.raises(cap.CaptureError, match="可能需要登录"):
        cap.capture("https://example.com/private", tmp_path)
    assert not (tmp_path / "raw").exists()


def test_local_file_relative_to_root_and_missing(tmp_path):
    (tmp_path / "inbox" / "attachments").mkdir(parents=True)
    (tmp_path / "inbox" / "attachments" / "Notes 2026.txt").write_text("plain notes")
    entry = cap.capture("inbox/attachments/Notes 2026.txt", tmp_path)
    assert entry.endswith("-notes-2026")
    with pytest.raises(cap.CaptureError, match="找不到文件"):
        cap.capture("inbox/attachments/nope.pdf", tmp_path)


def test_slugify():
    assert cap.slugify("  ***  ") == "untitled"
    assert cap.slugify("Ünïcode 中文——标题") == "n-code-中文-标题"
    assert len(cap.slugify("a" * 100)) == 60


def make_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with the given ASCII lines of text."""
    text = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return out + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


PAPER = make_pdf(["Kelly Criterion Notes", *[f"Line {i}: betting a fixed fraction of the bankroll." for i in range(8)]])


@pytest.fixture
def server():
    """Local HTTP server: route path -> (Content-Type, body); records each request's User-Agent."""
    routes, agents = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            agents.append(self.headers["User-Agent"])
            if self.path not in routes:
                self.send_error(404)
                return
            ctype, body = routes[self.path]
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", routes, agents
    httpd.shutdown()


@pytest.fixture
def no_defuddle(monkeypatch):
    calls = []
    monkeypatch.setattr(cap, "fetch_url", lambda url: calls.append(url) or {"title": "Page", "content": "z" * 300})
    return calls


def test_pdf_detected_by_response_not_suffix(tmp_path, server, no_defuddle):
    base, routes, agents = server
    routes["/pdf/2109.10814"] = ("application/pdf", PAPER)
    routes["/download"] = ("application/octet-stream", PAPER + b"\n%")  # sniffed by %PDF; distinct sha256
    entry = cap.capture(f"{base}/pdf/2109.10814", tmp_path)
    assert entry.endswith("-kelly-criterion-notes")
    meta, body = parse_frontmatter((tmp_path / entry / "source.md").read_text())
    assert meta["title"] == "Kelly Criterion Notes" and meta["source"] == f"{base}/pdf/2109.10814"
    assert "fixed fraction" in body and (tmp_path / entry / "original.pdf").read_bytes() == PAPER
    assert cap.capture(f"{base}/download", tmp_path).endswith("-kelly-criterion-notes-2")
    assert no_defuddle == [] and agents == [cap.USER_AGENT] * 2


def test_html_or_failed_request_goes_to_defuddle(tmp_path, server, no_defuddle):
    base, routes, _ = server
    routes["/paper.pdf"] = ("text/html", b"<html>please accept cookies</html>")
    cap.capture(f"{base}/paper.pdf", tmp_path)
    cap.capture(f"{base}/missing", tmp_path)
    assert no_defuddle == [f"{base}/paper.pdf", f"{base}/missing"]


def test_short_converted_document_is_capture_error(tmp_path):
    (tmp_path / "short.pdf").write_bytes(make_pdf(["Too short"]))
    with pytest.raises(cap.CaptureError, match="内容为空"):
        cap.capture("short.pdf", tmp_path)


def test_doc_title():
    assert cap.doc_title("intro text\n\n## Real Title  \nbody") == "Real Title"
    assert cap.doc_title("\n\n  First line \nsecond") == "First line"
    assert cap.doc_title("#hashtag line\n" + "x" * 150) == "#hashtag line"
    assert cap.doc_title("y" * 150) == "y" * 100
