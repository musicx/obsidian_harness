"""Token usage of runner invocations: parsing the CLIs' JSON output, per-run records and the ledger."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

TOKENS = ("input", "output", "cache_read", "cache_write")


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float | None = None
    model: str | None = None


@dataclass
class RunResult:
    runner: str
    code: int | None  # None: timed out
    text: str  # the final assistant text (the raw stdout for text runners)
    usage: Usage | None  # None: not reported
    seconds: float
    fallback: bool = False


@dataclass
class Ledger:
    """Where and under which labels to record each invocation."""
    path: Path  # <state>/<workspace>/usage.jsonl
    kind: str  # dispatch | relay | task | repair | eval
    task: str | None = None
    thread: str | None = None

    def record(self, result: RunResult) -> None:
        line = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "kind": self.kind,
                "task": self.task, "thread": self.thread} | usage_record(result)
        line["fallback"] = result.fallback
        with self.path.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def parse_pi(stdout: str) -> tuple[str, Usage]:
    """pi --mode json: JSONL events; usage is summed over the assistant `message_end` events."""
    usage, text, found = Usage(), "", False
    for line in stdout.split("\n"):  # records are LF-framed; U+2028 may occur inside strings
        if not line.strip():
            continue
        event = json.loads(line)
        message = event.get("message") or {}
        if event.get("type") != "message_end" or message.get("role") != "assistant":
            continue
        found = True
        u = message["usage"]
        usage.input += u["input"]
        usage.output += u["output"]
        usage.cache_read += u.get("cacheRead", 0)
        usage.cache_write += u.get("cacheWrite", 0)
        if cost := (u.get("cost") or {}).get("total"):  # 0 means pi has no price for the model
            usage.cost_usd = (usage.cost_usd or 0) + cost
        usage.model = "/".join(filter(None, [message.get("provider"), message.get("model")])) or usage.model
        if reply := "".join(c["text"] for c in message.get("content") or [] if c.get("type") == "text"):
            text = reply
    if not found:
        raise ValueError("no assistant message in the event stream")
    return text, usage


def parse_grok(stdout: str) -> tuple[str, Usage]:
    """grok --output-format json: one object with `text`, `usage`, `total_cost_usd`, `modelUsage`."""
    data = json.loads(stdout)
    u = data["usage"]
    return str(data.get("text") or ""), Usage(
        input=u["input_tokens"], output=u["output_tokens"], cache_read=u.get("cache_read_input_tokens", 0),
        cache_write=u.get("cache_creation_input_tokens", 0), cost_usd=data.get("total_cost_usd"),
        model=", ".join(data.get("modelUsage") or {}) or None)


PARSERS = {"pi-json": parse_pi, "grok-json": parse_grok}
FORMATS = ("text", *PARSERS)


def parse_output(fmt: str, stdout: str) -> tuple[str, Usage | None]:
    """(final text, usage) from a runner's stdout; unparsable output is treated as plain text."""
    if fmt == "text":
        return stdout, None
    try:
        return PARSERS[fmt](stdout)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("runner output is not valid %s (%s); treating it as text", fmt, e)
        return stdout, None


def usage_record(result: RunResult) -> dict:
    """The usage.json / ledger fields of one invocation."""
    u = result.usage
    return {"runner": result.runner, "model": u.model if u else None,
            **{k: getattr(u, k) if u else None for k in TOKENS},
            "cost_usd": u.cost_usd if u else None, "seconds": round(result.seconds, 1)}


def add_up(total: dict | None, results: list[RunResult]) -> dict:
    """Add invocations to a task note's flat `usage` map (tokens and cost stay empty until reported)."""
    total = dict(total or {"input": None, "output": None, "cache_read": None, "cache_write": None,
                           "cost_usd": None, "runs": 0, "seconds": 0})
    for r in results:
        total["runs"] += 1
        total["seconds"] = round(total["seconds"] + r.seconds, 1)
        if r.usage:
            for k in TOKENS:
                total[k] = (total[k] or 0) + getattr(r.usage, k)
            if r.usage.cost_usd is not None:
                total["cost_usd"] = round((total["cost_usd"] or 0) + r.usage.cost_usd, 6)
    return total


def describe(total: dict) -> str:
    """`（用量：输入 12.3k、输出 2.1k tokens，约 $0.05，38 秒）` for a task's record line."""
    def k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    parts = []
    if total["input"] is not None:
        prompt = total["input"] + total["cache_read"] + total["cache_write"]
        parts.append(f"输入 {k(prompt)}、输出 {k(total['output'])} tokens")
    if total["cost_usd"] is not None:
        parts.append(f"约 ${total['cost_usd']:.2f}")
    parts.append(f"{round(total['seconds'])} 秒")
    return f"（用量：{'，'.join(parts)}）"


def summary(results: list[RunResult]) -> str:
    """One-line usage for the engine log."""
    total = add_up(None, results)
    model = ", ".join(dict.fromkeys(r.usage.model for r in results if r.usage and r.usage.model)) or "?"
    return (f"model={model} input={total['input']} output={total['output']} cache_read={total['cache_read']} "
            f"cost_usd={total['cost_usd']} runs={total['runs']} seconds={total['seconds']}")


def write_usage_json(run_dir: Path, result: RunResult) -> None:
    (run_dir / "usage.json").write_text(json.dumps(usage_record(result), ensure_ascii=False, indent=2) + "\n")

