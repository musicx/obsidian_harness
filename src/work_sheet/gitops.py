import logging
import subprocess

from work_sheet.config import Config

log = logging.getLogger(__name__)


def git(cfg: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cfg.clone, check=check, capture_output=True, text=True)


def sync(cfg: Config) -> bool:
    """Make the clone an exact copy of origin/<branch>; False when that branch has no commits."""
    if not (cfg.clone / ".git").exists():
        cfg.clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", cfg.remote, str(cfg.clone)], check=True, capture_output=True)
        git(cfg, "config", "user.name", cfg.bot_name)
        git(cfg, "config", "user.email", cfg.bot_email)
    git(cfg, "fetch", "origin")
    if git(cfg, "rev-parse", "--verify", "--quiet", f"origin/{cfg.branch}", check=False).returncode:
        return False
    git(cfg, "checkout", "-f", "-B", cfg.branch, f"origin/{cfg.branch}")
    git(cfg, "reset", "--hard", f"origin/{cfg.branch}")
    git(cfg, "clean", "-fd")
    return True


def tracked_files(cfg: Config) -> list[str]:
    return [p for p in git(cfg, "ls-files", "-z").stdout.split("\0") if p]


def commit(cfg: Config, subject: str, role: str, task_ids: list[str] = ()) -> None:
    trailers = [f"Task: {t}" for t in task_ids] + [f"Role: {role}"]
    git(cfg, "add", "-A")
    git(cfg, "commit", "-m", subject + "\n\n" + "\n".join(trailers))


def push(cfg: Config) -> bool:
    """Rebase onto the remote and push; on a rebase conflict, drop local commits and return False."""
    if git(cfg, "pull", "--rebase", "origin", cfg.branch, check=False).returncode:
        git(cfg, "rebase", "--abort", check=False)
        git(cfg, "reset", "--hard", f"origin/{cfg.branch}")
        log.warning("rebase onto origin/%s conflicted; local commits dropped", cfg.branch)
        return False
    git(cfg, "push", "origin", f"HEAD:{cfg.branch}")
    return True
