"""`worksheet eval-routing`: replay recorded routing cases against the configured dispatcher."""

import json
import tempfile
from pathlib import Path

from work_sheet.config import Config
from work_sheet.dispatch import InvalidDecision, build_prompt, parse_decision
from work_sheet.runner import run_runner
from work_sheet.usage import Ledger


def run_case(cfg: Config, case: dict, n: int) -> tuple[bool, str]:
    """One dispatcher run for a case: (passed, what it returned)."""
    role = cfg.roles["dispatcher"]
    existing = case.get("tasks") or []
    prompt = build_prompt(cfg, f"inbox/{case['id']}.md", case.get("handled", ""), case["text"], existing,
                          case.get("projects"))
    with tempfile.TemporaryDirectory(prefix="worksheet-") as cwd:
        runs, _ = run_runner(cfg.runners, role.runner, role.prompt, prompt, Path(cwd),
                             cfg.runs / "eval" / f"{case['id']}-{n}", role.timeout_minutes * 60,
                             Ledger(cfg.usage_log, "eval"))
    code, stdout = runs[-1].code, runs[-1].text
    if code != 0:
        return False, "运行超时" if code is None else f"运行失败（退出码 {code}）"
    try:
        decision = parse_decision(stdout, cfg, existing)
    except InvalidDecision as e:
        return False, f"无效输出：{e}"
    expect = case["expect"]
    tasks = decision.get("tasks") or [] if decision["action"] == "register" else []
    got = decision["action"] + "".join(f" {t['skill']}(project={t.get('project')})" for t in tasks)
    passed = decision["action"] == expect["action"]
    if passed and expect["action"] == "register":
        passed = [t["skill"] for t in tasks] == expect.get("skills", []) and (
            "project" not in expect or all(t.get("project") == expect["project"] for t in tasks))
    return passed, got


def run_eval(cfg: Config, cases: Path, runs: int) -> int:
    """Print one line per case and a total; returns 1 if any case passed fewer than half of its runs."""
    weak = passed_runs = total_runs = 0
    for line in cases.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        results = [run_case(cfg, case, n) for n in range(1, runs + 1)]
        passed = sum(ok for ok, _ in results)
        print(f"{case['id']} {passed}/{runs}")
        for n, (ok, got) in enumerate(results, 1):
            if not ok:
                print(f"  run {n}: {got}")
        weak += passed * 2 < runs
        passed_runs += passed
        total_runs += runs
    print(f"total {passed_runs}/{total_runs} runs passed; {weak} case(s) passed fewer than half of their runs")
    return 1 if weak else 0
