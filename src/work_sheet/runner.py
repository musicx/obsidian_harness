import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from work_sheet.config import Runner
from work_sheet.usage import Ledger, RunResult, parse_output, write_usage_json

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{(system_prompt_file|system_prompt|prompt_file|view)\}")


def run_cli(runner: Runner, system_prompt: str, prompt: str, cwd: Path, run_dir: Path,
            timeout_s: float) -> RunResult:
    """Launch a runner CLI from its argv template; writes stdout.log, stderr.log and usage.json in run_dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    system_file = run_dir / "system_prompt.md"
    prompt_file = run_dir / "prompt.md"
    system_file.write_text(system_prompt)
    prompt_file.write_text(prompt)
    subs = {"system_prompt_file": str(system_file), "system_prompt": system_prompt,
            "prompt_file": str(prompt_file), "view": str(cwd)}
    # One pass, so placeholder-like text inside the substituted prompt is left alone.
    argv = [PLACEHOLDER.sub(lambda m: subs[m.group(1)], arg) for arg in runner.command]
    start = time.monotonic()
    with open(run_dir / "stdout.log", "w") as out, open(run_dir / "stderr.log", "w") as err:
        try:
            code = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                  env=os.environ | runner.env, timeout=timeout_s).returncode
        except subprocess.TimeoutExpired:
            code = None
    text, usage = parse_output(runner.format, (run_dir / "stdout.log").read_text(errors="replace"))
    result = RunResult(runner.name, code, text, usage, time.monotonic() - start)
    write_usage_json(run_dir, result)
    return result


def run_runner(runners: dict[str, Runner], name: str, system_prompt: str, prompt: str, cwd: Path, run_dir: Path,
               timeout_s: float, ledger: Ledger,
               restore: Callable[[], None] | None = None) -> tuple[list[RunResult], str | None]:
    """Run the named runner; if it fails and has a fallback, run that once (after `restore`, which undoes
    whatever the failed run left in cwd). Every invocation goes into the ledger.
    Returns (all invocations, the last one being the result; task-log note when the fallback ran)."""
    result = run_cli(runners[name], system_prompt, prompt, cwd, run_dir, timeout_s)
    ledger.record(result)
    fallback = runners[name].fallback
    if result.code == 0 or not fallback:
        return [result], None
    code = result.code
    log.warning("runner %s failed (%s); falling back to %s", name, "timeout" if code is None else f"exit {code}",
                fallback)
    if restore:
        restore()
    backup = run_cli(runners[fallback], system_prompt, prompt, cwd, run_dir / "fallback", timeout_s)
    backup.fallback = True
    ledger.record(backup)
    return [result, backup], f"主模型运行失败（{'超时' if code is None else f'退出码 {code}'}），改用备用 runner {fallback}"
