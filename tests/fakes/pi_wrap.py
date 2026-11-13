"""Fake pi in `--mode json`: runs another fake runner (argv[1:]) and reports its stdout as the assistant text
of a two-turn JSON event stream with fixed usage (input 1500, output 150, cacheRead 5000, cost $0.03 in all)."""

import json
import subprocess
import sys

proc = subprocess.run([sys.executable, *sys.argv[1:]], capture_output=True, text=True)


def usage(inp: int, out: int, cache: int, cost: float) -> dict:
    return {"input": inp, "output": out, "cacheRead": cache, "cacheWrite": 0, "totalTokens": inp + out + cache,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost}}


def assistant(content: list, u: dict) -> dict:
    return {"role": "assistant", "content": content, "provider": "anthropic", "model": "claude-test",
            "usage": u, "stopReason": "stop"}


first = assistant([{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}], usage(1000, 100, 5000, 0.02))
last = assistant([{"type": "text", "text": proc.stdout}], usage(500, 50, 0, 0.01))
events = [{"type": "session", "version": 3, "id": "x"}, {"type": "agent_start"},
          {"type": "message_end", "message": {"role": "user", "content": "prompt"}},
          {"type": "message_update", "usage": usage(999, 999, 0, 9), "assistantMessageEvent": {"type": "text_delta"}},
          {"type": "message_end", "message": first}, {"type": "turn_end", "message": first, "toolResults": []},
          {"type": "message_end", "message": last}, {"type": "turn_end", "message": last, "toolResults": []},
          {"type": "agent_end", "messages": [first, last]}]
print("\n".join(json.dumps(e, ensure_ascii=False) for e in events))
sys.exit(proc.returncode)
