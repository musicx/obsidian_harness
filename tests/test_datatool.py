import json
import subprocess
import sys

import pytest
from conftest import FakeClickHouse

from work_sheet import datatool
from work_sheet.datatool import ToolError, clean_sql


def source(fake: FakeClickHouse, **overrides) -> dict:
    return {"url": fake.url, "database": "db_stock", "user": "reader", "password_file": "", "force_readonly": True,
            "max_rows": 200, "timeout_seconds": 60} | overrides


def call(config: dict, name: str, **args) -> dict:
    """A tools/call through the MCP request handler."""
    reply = datatool.handle(config, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}})
    return reply["result"]


@pytest.mark.parametrize("sql", ["INSERT INTO t VALUES (1)", "DROP TABLE t", "select 1; select 2",
                                 "SELECT 1 FORMAT JSON", "SELECT 1 INTO OUTFILE 'x.csv'", "-- only a comment"])
def test_rejected_sql(sql):
    with pytest.raises(ToolError):
        clean_sql(sql)


def test_accepted_sql():
    assert clean_sql("/* why */ select formatDateTime(d, '%Y') -- note\nFROM t;") == \
        "select formatDateTime(d, '%Y')  \nFROM t"
    for sql in ["WITH x AS (SELECT 1) SELECT * FROM x", "SHOW TABLES", "DESC t", "EXPLAIN SELECT 1", "EXISTS t"]:
        assert clean_sql(sql) == sql


def test_query_sends_readonly_settings_and_credentials(clickhouse, tmp_path):
    password = tmp_path / "pw"
    password.write_text("s3cret\n")
    log = tmp_path / "queries.jsonl"
    config = {"sources": {"db": source(clickhouse, password_file=str(password))}, "log": str(log)}
    result = call(config, "query", source="db", sql="SELECT 42 AS n;")
    assert result == {"content": [{"type": "text", "text": "| n |\n|---|\n| 42 |\n\n（返回 1 行）"}]}
    request = clickhouse.requests[0]
    assert request["sql"] == "SELECT 42 AS n FORMAT TabSeparatedWithNames"
    assert request["params"] == {"database": "db_stock", "readonly": "1", "max_result_rows": "200",
                                 "result_overflow_mode": "break", "max_execution_time": "60"}
    assert request["headers"]["x-clickhouse-user"] == "reader" and request["headers"]["x-clickhouse-key"] == "s3cret"

    call(config, "query", source="db", sql="DELETE FROM t")
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        {"source": "db", "sql": "SELECT 42 AS n;", "ok": True, "rows": 1, "error": None},
        {"source": "db", "sql": "DELETE FROM t", "ok": False, "rows": 0,
         "error": "只允许 SELECT / WITH / SHOW / DESCRIBE / DESC / EXPLAIN / EXISTS 开头的只读查询"}]
    assert len(clickhouse.requests) == 1  # the rejected statement never reached the server


def test_readonly_account_retry_without_settings(clickhouse):
    def respond(sql, params):
        if "readonly" in params or "max_result_rows" in params:
            return 500, "Code: 164. DB::Exception: Cannot modify 'max_result_rows' setting in readonly mode."
        return 200, "n\n1\n"

    clickhouse.respond = respond
    result = call({"sources": {"db": source(clickhouse, force_readonly=False)}}, "query", source="db", sql="SELECT 1")
    assert "isError" not in result and [r["params"] for r in clickhouse.requests] == [
        {"database": "db_stock", "max_result_rows": "200", "result_overflow_mode": "break",
         "max_execution_time": "60"}, {"database": "db_stock"}]


def test_row_cap_and_errors(clickhouse):
    clickhouse.respond = lambda sql, params: (200, "a\tb\n" + "".join(f"{i}\tx|y\n" for i in range(5)))
    config = {"sources": {"db": source(clickhouse, max_rows=3)}}
    text = call(config, "query", source="db", sql="SELECT a, b FROM t")["content"][0]["text"]
    assert text == "| a | b |\n|---|---|\n| 0 | x\\|y |\n| 1 | x\\|y |\n| 2 | x\\|y |\n\n（返回 3 行；已截断）"

    clickhouse.respond = lambda sql, params: (500, "Code: 60. DB::Exception: Unknown table db_stock.nope")
    result = call(config, "query", source="db", sql="SELECT * FROM nope")
    assert result["isError"] is True and result["content"][0]["text"].startswith("查询失败：Code: 60.")
    assert call(config, "query", source="other", sql="SELECT 1")["content"][0]["text"] == "未知的数据源，可选：db"


def test_describe_table(clickhouse):
    def respond(sql, params):
        if "system.columns" in sql:
            return 200, "name\ttype\tcomment\ncode\tString\t代码\ntrade_date\tDate\t交易日\n"
        if "system.tables" in sql:
            return 200, "total_rows\tsorting_key\tpartition_key\n100\tcode, trade_date\ttoYear(trade_date)\n"
        return 200, "min\tmax\n2010-01-04\t2026-11-13\n"

    clickhouse.respond = respond
    config = {"sources": {"db": source(clickhouse)}}
    text = call(config, "describe_table", source="db", table="stock_daily")["content"][0]["text"]
    assert "| trade_date | Date | 交易日 |" in text and "- sorting_key：code, trade_date" in text
    assert "- trade_date 范围：2010-01-04 ～ 2026-11-13" in text
    assert "min(`trade_date`), max(`trade_date`) FROM `stock_daily`" in clickhouse.requests[-1]["sql"]
    assert call(config, "describe_table", source="db", table="x; DROP")["isError"] is True


def test_mcp_stdio_round_trip(clickhouse, tmp_path):
    config = tmp_path / "datatool.json"
    config.write_text(json.dumps({"sources": {"db": source(clickhouse)}, "log": str(tmp_path / "q.jsonl")}))
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "query", "arguments": {"source": "db", "sql": "SELECT 42 AS n"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]
    proc = subprocess.run([sys.executable, datatool.__file__], input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, env={"WS_DATATOOL_CONFIG": str(config)}, timeout=30)
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert replies[0]["result"]["protocolVersion"] == "2025-03-26"
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["list_tables", "describe_table", "query"]
    assert replies[2]["result"]["content"][0]["text"].startswith("| n |")
    assert replies[3]["result"] == {}

    selftest = subprocess.run([sys.executable, datatool.__file__, "--selftest"], capture_output=True, text=True)
    assert selftest.returncode == 0 and selftest.stdout.startswith("list_tables:")
