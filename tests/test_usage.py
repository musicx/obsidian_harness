import json
import logging
from datetime import timedelta

from conftest import FAKES, LIBRARIAN_ROLE, Env, register
from test_deploy import runner, write_runners
from test_engine import article

from work_sheet.usage import add_up, describe, parse_output, RunResult, Usage
from work_sheet.vault import now

PI_SAMPLE = "\n".join(json.dumps(e, ensure_ascii=False) for e in [
    {"type": "session", "version": 3, "id": "s"},
    {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
    {"type": "message_update", "usage": {"input": 7, "output": 7}, "assistantMessageEvent": {"type": "text_delta"}},
    {"type": "message_end", "message": {
        "role": "assistant", "provider": "magpie", "model": "deepseek/deepseek-flash",
        "content": [{"type": "thinking", "thinking": "…"}, {"type": "toolCall", "id": "1", "name": "ls"}],
        "usage": {"input": 200, "output": 3, "cacheRead": 256, "cacheWrite": 0, "reasoning": 0, "totalTokens": 459,
                  "cost": {"total": 3.2568e-05, "totalFromProvider": False}}}},
    {"type": "message_end", "message": {
        "role": "assistant", "provider": "magpie", "model": "deepseek/deepseek-flash",
        "content": [{"type": "text", "text": "第一段 同一段"}, {"type": "text", "text": " pong"}],
        "usage": {"input": 50, "output": 10, "cacheRead": 400, "cacheWrite": 20, "cost": {"total": 1e-05}}}},
    {"type": "agent_settled", "aborted": False},
])

GROK_SAMPLE = json.dumps({
    "text": "pong", "stopReason": "end_turn", "num_turns": 1, "total_cost_usd": 0.01657,
    "usage": {"input_tokens": 5996, "cache_read_input_tokens": 8832, "cache_creation_input_tokens": 0,
              "output_tokens": 27, "reasoning_tokens": 26, "total_tokens": 14855},
    "modelUsage": {"grok-4.7-build": {"inputTokens": 5996, "outputTokens": 27, "costUSD": 0.01657}}})


def test_parse_pi_json_sums_assistant_messages():
    text, usage = parse_output("pi-json", PI_SAMPLE)
    assert text == "第一段 同一段 pong"
    assert usage == Usage(input=250, output=13, cache_read=656, cache_write=20, cost_usd=usage.cost_usd,
                          model="magpie/deepseek/deepseek-flash")
    assert abs(usage.cost_usd - 4.2568e-05) < 1e-12


def test_parse_grok_json():
    assert parse_output("grok-json", GROK_SAMPLE) == ("pong", Usage(5996, 27, 8832, 0, 0.01657, "grok-4.7-build"))


def test_unparsable_output_is_text(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_output("pi-json", "plain words\n") == ("plain words\n", None)
        assert parse_output("grok-json", '{"text": "no usage"}') == ('{"text": "no usage"}', None)
    assert "treating it as text" in caplog.text


def test_record_line_suffix():
    known = RunResult("strong", 0, "", Usage(2300, 2100, 10000, 0, 0.05), 37.6)
    assert describe(add_up(None, [known])) == "（用量：输入 12.3k、输出 2.1k tokens，约 $0.05，38 秒）"
    assert describe(add_up(None, [RunResult("x", 0, "", Usage(800, 20), 3)])) == "（用量：输入 800、输出 20 tokens，3 秒）"
    assert describe(add_up(None, [RunResult("x", 1, "", None, 4.4)])) == "（用量：4 秒）"


def pi_runner(script: str, fallback: str | None = None) -> str:
    """A runner whose output is the fake script's stdout wrapped in pi's JSON event stream."""
    return runner("pi_wrap.py", str(FAKES / script), fallback=fallback) + 'format = "pi-json"\n'


def ledger(env: Env) -> list[dict]:
    return [json.loads(line) for line in (env.state / "vault" / "usage.jsonl").read_text().splitlines()]


def test_usage_recorded_for_dispatch_and_task(seeded: Env):
    env = seeded
    write_runners(env, small=pi_runner("dispatcher.py"), strong=pi_runner("librarian.py"))
    src = article(env)
    env.push({"inbox/a.md": f"收录 {src}\n"})
    env.respond(register(capture=[str(src)]))
    env.tick()
    env.pull()

    [task] = env.new_tasks()  # the decision was read from the assistant text of the event stream
    assert task["status"] == "done"
    assert task["usage"] == {"input": 1500, "output": 150, "cache_read": 5000, "cache_write": 0, "cost_usd": 0.03,
                             "runs": 1, "seconds": task["usage"]["seconds"]}
    note = env.text(f"tasks/{task['id']}.md")
    assert f"完成（用量：输入 6.5k、输出 150 tokens，约 $0.03，{round(task['usage']['seconds'])} 秒）" in note
    lines = ledger(env)
    assert [(line["kind"], line["task"], line["thread"], line["runner"], line["fallback"]) for line in lines] == [
        ("dispatch", None, "inbox/a.md", "small", False), ("task", task["id"], "inbox/a.md", "strong", False)]
    assert lines[1] | {"time": None, "seconds": None} == {
        "time": None, "kind": "task", "task": task["id"], "thread": "inbox/a.md", "runner": "strong",
        "model": "anthropic/claude-test", "input": 1500, "output": 150, "cache_read": 5000, "cache_write": 0,
        "cost_usd": 0.03, "seconds": None, "fallback": False}
    usage_json = json.loads((env.state / "vault" / "runs" / task["id"] / "usage.json").read_text())
    assert usage_json["runner"] == "strong" and usage_json["input"] == 1500
    assert json.loads((env.state / "vault" / "runs" / "dispatch-1" / "usage.json").read_text())["cost_usd"] == 0.03


def test_usage_of_repair_and_fallback_runs(seeded: Env, monkeypatch):
    env = seeded
    write_runners(env, small=runner("dispatcher.py"), strong=runner("broken.py", fallback="backup"),
                  backup=pi_runner("librarian.py"))
    monkeypatch.setenv("WS_MODE", "repairable")
    src = article(env)
    env.push({"inbox/a.md": f"收录 {src}\n"})
    env.respond(register(capture=[str(src)]))
    env.tick()
    env.pull()
    [task] = env.new_tasks()
    assert task["status"] == "done"
    assert [(line["kind"], line["runner"], line["fallback"], line["input"]) for line in ledger(env)] == [
        ("dispatch", "small", False, None), ("task", "strong", False, None), ("task", "backup", True, 1500),
        ("repair", "strong", False, None), ("repair", "backup", True, 1500)]
    usage = task["usage"]
    assert (usage["runs"], usage["input"], usage["output"], usage["cost_usd"]) == (4, 3000, 300, 0.06)
    assert (env.state / "vault" / "runs" / task["id"] / "repair" / "fallback" / "usage.json").exists()


def test_views_only_get_recent_briefings(seeded: Env, monkeypatch):
    env = seeded
    (env.tmp / "roles" / "librarian.md").write_text(
        LIBRARIAN_ROLE.replace('read: ["{thread}",', 'read: ["{thread}", "briefings/**",'))
    monkeypatch.setenv("WS_MODE", "noop")
    today = now().date()

    def briefing(cadence: str, days_ago: int) -> str:
        return f"---\ntype: briefing\ncadence: {cadence}\ndate: {today - timedelta(days=days_ago)}\n---\n简报\n"

    env.push({"briefings/x/old.md": briefing("daily", 8), "briefings/x/recent.md": briefing("daily", 7),
              "briefings/f/w20.md": briefing("weekly", 20), "briefings/f/w22.md": briefing("weekly", 22),
              "briefings/f/nocadence.md": f"---\ndate: {today}\n---\n", "inbox/a.md": "看看简报\n"})
    env.respond(register(capture=[]))
    env.tick()
    listing = env.listing.read_text().split("\n")
    assert sorted(p for p in listing if p.startswith("briefings/")) == ["briefings/f/w20.md", "briefings/x/recent.md"]
    env.pull()
    assert (env.human / "briefings/x/old.md").exists()
