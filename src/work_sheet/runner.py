import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from work_sheet.config import Runner

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{(system_prompt_file|system_prompt|prompt_file|view)\}")


def run_cli(runner: Runner, system_prompt: str, prompt: str, cwd: Path, run_dir: Path,
            timeout_s: float) -> tuple[int | None, str]:
    """Launch a runner CLI from its argv template. Returns (exit code or None on timeout, stdout)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    system_file = run_dir / "system_prompt.md"
    prompt_file = run_dir / "prompt.md"
    system_file.write_text(system_prompt)
    prompt_file.write_text(prompt)
    subs = {"system_prompt_file": str(system_file), "system_prompt": system_prompt,
            "prompt_file": str(prompt_file), "view": str(cwd)}
    # One pass, so placeholder-like text inside the substituted prompt is left alone.
    argv = [PLACEHOLDER.sub(lambda m: subs[m.group(1)], arg) for arg in runner.command]
    with open(run_dir / "stdout.log", "w") as out, open(run_dir / "stderr.log", "w") as err:
        try:
            code = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                  env=os.environ | runner.env, timeout=timeout_s).returncode
        except subprocess.TimeoutExpired:
            code = None
    return code, (run_dir / "stdout.log").read_text(errors="replace")


def run_runner(runners: dict[str, Runner], name: str, system_prompt: str, prompt: str, cwd: Path, run_dir: Path,
               timeout_s: float, restore: Callable[[], None] | None = None) -> tuple[int | None, str, str | None]:
    """Run the named runner; if it fails and has a fallback, run that once (after `restore`, which undoes
    whatever the failed run left in cwd). Returns (exit code, stdout, task-log note when the fallback ran)."""
    code, stdout = run_cli(runners[name], system_prompt, prompt, cwd, run_dir, timeout_s)
    fallback = runners[name].fallback
    if code == 0 or not fallback:
        return code, stdout, None
    reason = "超时" if code is None else f"退出码 {code}"
    log.warning("runner %s failed (%s); falling back to %s", name, "timeout" if code is None else f"exit {code}",
                fallback)
    if restore:
        restore()
    code, stdout = run_cli(runners[fallback], system_prompt, prompt, cwd, run_dir / "fallback", timeout_s)
    return code, stdout, f"主模型运行失败（{reason}），改用备用 runner {fallback}"
