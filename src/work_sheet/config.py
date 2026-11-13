import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from work_sheet.vault import parse_frontmatter


@dataclass
class Role:
    id: str
    description: str
    runner: str
    web: bool
    read: list[str]
    write: list[str]
    skills: list[str]
    timeout_minutes: int
    needs_project: bool
    grantable: list[str]
    prompt: str


@dataclass
class Runner:
    command: list[str]
    env: dict[str, str]


@dataclass
class Config:
    workspace: str
    remote: str
    branch: str
    bot_name: str
    bot_email: str
    runners: dict[str, Runner]
    roles: dict[str, Role]
    routing: str | None
    state_dir: Path  # <state>/<workspace>

    @property
    def clone(self) -> Path:
        return self.state_dir / "clone"

    @property
    def runs(self) -> Path:
        return self.state_dir / "runs"


def load_role(path: Path) -> Role:
    meta, body = parse_frontmatter(path.read_text())
    return Role(
        id=meta["id"],
        description=meta.get("description", ""),
        runner=meta["runner"],
        web=bool(meta.get("web", False)),
        read=list(meta.get("read") or []),
        write=list(meta.get("write") or []),
        skills=list(meta.get("skills") or []),
        timeout_minutes=int(meta.get("timeout_minutes", 20)),
        needs_project=bool(meta.get("needs_project", False)),
        grantable=list(meta.get("grantable") or []),
        prompt=body.strip(),
    )


ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value, source: Path):
    """Replace `${NAME}` in every string, recursively, with the environment variable's value."""
    if isinstance(value, dict):
        return {k: expand_env(v, source) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, source) for v in value]
    if not isinstance(value, str):
        return value

    def lookup(m: re.Match) -> str:
        if m.group(1) not in os.environ:
            raise SystemExit(f"{source}: environment variable {m.group(1)} is not set (see .env.sample)")
        return os.environ[m.group(1)]

    return ENV_REF.sub(lookup, value)


def load_toml(path: Path) -> dict:
    return expand_env(tomllib.loads(path.read_text()), path)


def load_config(config_dir: Path, roles_dir: Path, state_dir: Path, workspace: str | None) -> Config:
    workspaces = load_toml(config_dir / "workspaces.toml")["workspaces"]
    if workspace is None:
        if len(workspaces) != 1:
            raise SystemExit(f"--workspace is required; configured: {', '.join(workspaces)}")
        workspace = next(iter(workspaces))
    ws = workspaces[workspace]
    runners = load_toml(config_dir / "runners.toml")["runners"]
    roles = {r.id: r for r in map(load_role, sorted(roles_dir.glob("*.md")))}
    routing_file = config_dir / "routing.md"
    return Config(
        workspace=workspace,
        remote=ws["remote"],
        branch=ws.get("branch", "main"),
        bot_name=ws["bot_name"],
        bot_email=ws["bot_email"],
        runners={name: Runner(r["command"], r.get("env", {})) for name, r in runners.items()},
        roles=roles,
        routing=routing_file.read_text() if routing_file.exists() else None,
        state_dir=state_dir / workspace,
    )
