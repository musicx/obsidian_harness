import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from work_sheet.context import PACKS
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
    grantable: list[str]
    prompt: str


@dataclass
class Skill:
    name: str
    description: str
    role: str | None  # None: a shared reference skill, not dispatchable
    context: list[str]
    uses: list[str]
    read: list[str]  # extra read patterns for tasks using this skill
    needs_project: bool
    dedupe: str | None
    allow_shrink: bool
    dir: Path
    body: str


@dataclass
class Runner:
    command: list[str]
    env: dict[str, str]
    fallback: str | None  # runner to use once when this one fails


@dataclass
class Feed:
    name: str  # directory name under briefings/
    title: str
    source: Path
    pattern: str
    cadence: str
    url: str | None  # `{date}` is substituted
    since: str | None  # YYYY-MM-DD


@dataclass
class Config:
    workspace: str
    remote: str
    branch: str
    bot_name: str
    bot_email: str
    runners: dict[str, Runner]
    feeds: dict[str, Feed]
    roles: dict[str, Role]
    skills: dict[str, Skill]
    routing: str | None
    state_dir: Path  # <state>/<workspace>
    template_dir: Path

    def dispatchable(self) -> list[Skill]:
        """Skills the dispatcher may choose: they name a role, and that role's runner exists on this host."""
        return [s for s in self.skills.values()
                if s.role and s.role != "dispatcher" and self.roles[s.role].runner in self.runners]

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
        grantable=list(meta.get("grantable") or []),
        prompt=body.strip(),
    )


def load_skill(path: Path, roles: dict[str, Role]) -> Skill:
    meta, body = parse_frontmatter(path.read_text())
    md = {k: str(v) for k, v in (meta.get("metadata") or {}).items()}
    skill = Skill(
        name=meta.get("name"),
        description=str(meta.get("description") or ""),
        role=md.get("role") or None,
        context=md.get("context", "").split(),
        uses=md.get("uses", "").split(),
        read=md.get("read", "").split(),
        needs_project=md.get("needs_project", "").lower() == "true",
        dedupe=md.get("dedupe") or None,
        allow_shrink=md.get("allow_shrink", "").lower() == "true",
        dir=path.parent,
        body=body.strip(),
    )
    if skill.name != path.parent.name:
        raise SystemExit(f"{path}: name {skill.name!r} must equal the folder name {path.parent.name!r}")
    if skill.role and skill.role not in roles:
        raise SystemExit(f"{path}: role {skill.role!r} is not defined in the roles directory")
    if unknown := set(skill.context) - set(PACKS):
        raise SystemExit(f"{path}: unknown context packs {sorted(unknown)}; known: {', '.join(PACKS)}")
    return skill


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


def load_config(config_dir: Path, roles_dir: Path, skills_dir: Path, state_dir: Path, workspace: str | None,
                template_dir: Path, runners_file: Path | None = None, feeds_file: Path | None = None) -> Config:
    workspaces = load_toml(config_dir / "workspaces.toml")["workspaces"]
    if workspace is None:
        if len(workspaces) != 1:
            raise SystemExit(f"--workspace is required; configured: {', '.join(workspaces)}")
        workspace = next(iter(workspaces))
    ws = workspaces[workspace]
    runners_file = runners_file or config_dir / "runners.toml"
    runners = {name: Runner(r["command"], r.get("env", {}), r.get("fallback"))
               for name, r in load_toml(runners_file)["runners"].items()}
    for name, runner in runners.items():
        if runner.fallback and runner.fallback not in runners:
            raise SystemExit(f"{runners_file}: fallback {runner.fallback!r} of runner {name!r} is not defined")
    feeds_file = feeds_file or config_dir / "feeds.toml"
    feeds = {name: Feed(name, f["title"], Path(f["source"]), f["pattern"], f["cadence"], f.get("url"),
                        f.get("since") or None)
             for name, f in (load_toml(feeds_file)["feeds"] if feeds_file.exists() else {}).items()}
    roles = {r.id: r for r in map(load_role, sorted(roles_dir.glob("*.md")))}
    skills = {s.name: s for s in (load_skill(p, roles) for p in sorted(skills_dir.glob("*/SKILL.md")))}
    for skill in skills.values():
        if missing := [u for u in skill.uses if u not in skills]:
            raise SystemExit(f"{skill.dir / 'SKILL.md'}: uses unknown skills {missing}")
    routing_file = config_dir / "routing.md"
    return Config(
        workspace=workspace,
        remote=ws["remote"],
        branch=ws.get("branch", "main"),
        bot_name=ws["bot_name"],
        bot_email=ws["bot_email"],
        runners=runners,
        feeds=feeds,
        roles=roles,
        skills=skills,
        routing=routing_file.read_text() if routing_file.exists() else None,
        state_dir=state_dir / workspace,
        template_dir=template_dir,
    )
