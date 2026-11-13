import json
import sys
from datetime import date, timedelta

import pytest
from conftest import Env, FakeClickHouse, make_config, register
from test_deploy import runner, write_runners
from test_engine import last_block
from test_engines import Engines
from test_m1 import decide

from work_sheet import cli
from work_sheet.dispatch import InvalidDecision, build_prompt
from work_sheet.generated import regenerate
from work_sheet.lint import lint

ANALYST_ROLE = """---
id: analyst
description: 查数据回答问题
runner: analysis
read: ["{thread}", "datasets/**", "analysis/**"]
write: ["analysis/**", "datasets/**"]
timeout_minutes: 1
---
You are the analyst.
"""


@pytest.fixture
def analysis(seeded: Env, clickhouse: FakeClickHouse, monkeypatch) -> Env:
    """The seeded vault plus an analyst role, a data-analysis skill and a local data source `a-share-data`."""
    env = seeded
    (env.tmp / "roles" / "analyst.md").write_text(ANALYST_ROLE)
    skill = env.tmp / "skills" / "data-analysis"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: data-analysis\ndescription: 回答数据问题\nmetadata:\n  role: analyst\n"
                                    "---\nDATA ANALYSIS PROCEDURE\n")
    write_runners(env, small=runner("dispatcher.py"), strong=runner("librarian.py"), analysis=runner("analyst.py"))
    (env.tmp / "config" / "datasources.toml").write_text(
        f'[sources.a-share-data]\nkind = "clickhouse"\nurl = "{clickhouse.url}"\ndatabase = "db_stock"\n'
        'user = "default"\nforce_readonly = true\nmax_rows = 200\ntimeout_seconds = 60\n')
    monkeypatch.setenv("WORKSHEET_DATATOOL_PYTHON", sys.executable)
    return env


def analysis_task(resources: list[str]) -> dict:
    decision = register(capture=[], title="答案是多少", skill="data-analysis")
    decision["tasks"][0]["resources"] = resources
    return decision


def test_analysis_task_gets_the_query_tool(analysis: Env, clickhouse: FakeClickHouse):
    env = analysis
    env.push({"inbox/a.md": "答案是多少？\n"})
    env.respond(analysis_task(["a-share-data"]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "done" and task["resources"] == ["a-share-data"]
    assert task["outputs"] == ["analysis/2026-11-14 答案.md", "datasets/a-share-data/stock_daily.md"]
    assert not (env.human / ".pi").exists() and not (env.human / "_scratch").exists()
    assert env.text("datasets/_index.md") == (
        "# 数据字典\n\n- [[datasets/a-share-data/stock_daily|stock_daily]]（a-share-data）— A 股日线行情\n")
    listing = env.listing.read_text().split("\n")
    assert {".pi/mcp.json", "_scratch/datatool.json"} <= set(listing)
    assert "- 数据源：a-share-data（通过查询工具 wsdata 访问：list_tables / describe_table / query）\n" in \
        env.listing.with_suffix(".brief.md").read_text()
    assert clickhouse.requests[0]["params"]["readonly"] == "1"
    runs = env.state / "vault" / "runs"
    assert json.loads((runs / task["id"] / "queries.jsonl").read_text())["sql"] == "SELECT 42 AS n"
    prompt = (runs / "dispatch-1" / "prompt.md").read_text()
    assert "# Engines\n\n- main（coordinator）：；资源：a-share-data\n" in prompt and '"resources": []' in prompt


def test_sql_without_an_execution_record_fails(analysis: Env, monkeypatch):
    env = analysis
    monkeypatch.setenv("WS_MODE", "unlogged")
    env.push({"inbox/a.md": "答案是多少？\n"})
    env.respond(analysis_task(["a-share-data"]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "failed" and not (env.human / "analysis").exists()
    assert "> analysis/2026-11-14 答案.md 里的查询没有执行记录：SELECT 43 AS n…" in last_block(env.text("inbox/a.md"))


def test_unknown_resource_is_rejected(analysis: Env):
    cfg = make_config(analysis.tmp)
    with pytest.raises(InvalidDecision, match="没有引擎提供资源 nope，可选：a-share-data$"):
        decide(cfg, {"skill": "data-analysis", "resources": ["nope"]}, [])
    assert decide(cfg, {"skill": "data-analysis", "resources": ["a-share-data"]}, [])["tasks"][0]["engine"] == "main"


def test_resources_route_to_the_engine_that_has_them(analysis: Env):
    engines = Engines(analysis)
    registry = (analysis.tmp / "engines.toml").read_text()
    registry = registry.replace('runners = ["small", "strong", "web"]', 'runners = ["small", "strong", "web", "analysis"]')
    registry = registry.replace('runners = ["small", "strong"]', 'runners = ["small", "strong", "analysis"]')
    registry = registry.replace('resources = ["local-files"]',
                                '[engines.mac.resources]\n"local-files" = ""\n"a-share-data" = "本机 ClickHouse：A 股日线"')
    (analysis.tmp / "engines.toml").write_text(registry)
    cfg = engines.config()
    assert decide(cfg, {"skill": "data-analysis", "resources": ["a-share-data"]}, [])["tasks"][0]["engine"] == "mac"
    assert decide(cfg, {"skill": "data-analysis"}, [])["tasks"][0]["engine"] == "oracle"
    prompt = build_prompt(cfg, "inbox/a.md", "", "hi", [], None)
    assert "- mac（worker）：个人电脑；资源：local-files、a-share-data（本机 ClickHouse：A 股日线）\n" in prompt
    assert '"resources": [], "engine": null}' in prompt


def test_missing_local_data_source_fails(analysis: Env):
    env = analysis
    (env.tmp / "config" / "datasources.toml").unlink()
    (env.tmp / "engines.toml").write_text(
        '[engines.main]\nrole = "coordinator"\ndescription = "本机"\nrunners = ["small", "strong", "analysis"]\n'
        '[engines.main.resources]\n"a-share-data" = "本机 ClickHouse"\n')
    env.push({"inbox/a.md": "答案是多少？\n"})
    env.respond(analysis_task(["a-share-data"]))
    cli.main(["tick", *env.dirs(), "--engines-file", str(env.tmp / "engines.toml"), "--engine-id", "main"])
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "failed" and last_block(env.text("inbox/a.md"))[1] == "> 这台机器没有配置数据源 a-share-data"


def test_analysis_and_dataset_lint():
    today = date.today()

    def note(as_of, sql: str) -> str:
        return (f"---\ntype: analysis\nquestion: q\nsource: s\nas_of: {as_of}\n---\n"
                f"```sql\n{sql}\n```\n\n```SQL\nselect   1\n;\n```\n")

    files = {"analysis/a.md": note(today, "SELECT\n  42 AS n;"),
             "projects/p/analysis/b.md": note(today + timedelta(days=1), "SELECT 7"),
             "datasets/s/t.md": "---\ntype: dataset\nsource: s\ntable: t\n---\n",
             "datasets/s/ok.md": "---\ntype: dataset\nsource: s\ntable: ok\ndescription: 好\n---\n"}
    problems = lint(sorted(files), [], set(files), files.get, files.get, executed={"SELECT 42 AS n", "select 1"})
    assert problems == [
        "datasets/s/t.md 的 frontmatter 不合规（type/source/table/description）",
        "projects/p/analysis/b.md 的 frontmatter 不合规（type/question/source/as_of）",
        "projects/p/analysis/b.md 里的查询没有执行记录：SELECT 7…",
    ]


def test_datasets_index_is_generated(tmp_path):
    (tmp_path / "datasets" / "src").mkdir(parents=True)
    (tmp_path / "datasets" / "src" / "b.md").write_text("---\nsource: src\ntable: b\ndescription: B 表\n---\n")
    (tmp_path / "datasets" / "src" / "A.md").write_text("---\nsource: src\ntable: A\ndescription: A 表\n---\n")
    regenerate(tmp_path)
    assert (tmp_path / "datasets" / "_index.md").read_text() == (
        "# 数据字典\n\n- [[datasets/src/A|A]]（src）— A 表\n- [[datasets/src/b|b]]（src）— B 表\n")
