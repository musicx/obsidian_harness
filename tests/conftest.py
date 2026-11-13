import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from work_sheet import cli
from work_sheet.vault import parse_frontmatter

FAKES = Path(__file__).parent / "fakes"

DISPATCHER_ROLE = """---
id: dispatcher
description: decides what to do
runner: small
read: []
write: []
timeout_minutes: 1
---
You are the dispatcher.
"""

LIBRARIAN_ROLE = """---
id: librarian
description: 维护 wiki/
runner: strong
web: false
read: ["{thread}", "{context}", "wiki/**", "practice/**"]
write: ["wiki/**", "proposals/**"]
grantable: ["practice/*.md"]
skills: []
timeout_minutes: 1
---
You are the librarian.
"""

TRACKER_ROLE = """---
id: tracker
description: 维护项目记录
runner: strong
needs_project: true
read: ["{thread}", "{context}", "projects/_index.md", "projects/{project}/**"]
write: ["projects/_index.md", "projects/{project}/**"]
timeout_minutes: 1
---
You are the tracker.
"""

RESEARCHER_ROLE = """---
id: researcher
description: 联网搜集资料
runner: web
read: ["{thread}", "wiki/index.md"]
write: ["work/{task}/**"]
timeout_minutes: 1
---
You are the researcher.
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def register(*, capture: list[str], title: str = "收录样例文章") -> dict:
    return {"action": "register", "reason": "用户要求收录",
            "tasks": [{"role": "librarian", "op": "ingest", "title": title, "brief": "收录这篇材料。",
                       "capture": capture, "project": None}]}


@dataclass
class Env:
    tmp: Path
    remote: Path
    human: Path
    state: Path
    dispatch: Path
    listing: Path

    def respond(self, *responses) -> None:
        """Queue dispatcher outputs: dicts are sent as JSON, strings verbatim."""
        out = [r if isinstance(r, str) else "```json\n" + json.dumps(r, ensure_ascii=False) + "\n```"
               for r in responses]
        self.dispatch.write_text(json.dumps(out))
        self.dispatch.with_suffix(".count").unlink(missing_ok=True)

    def dispatch_calls(self) -> int:
        counter = self.dispatch.with_suffix(".count")
        return int(counter.read_text()) if counter.exists() else 0

    def push(self, files: dict[str, str]) -> None:
        for rel, text in files.items():
            (self.human / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.human / rel).write_text(text)
        git(self.human, "add", "-A")
        git(self.human, "commit", "-m", "human edit")
        git(self.human, "push", "origin", "HEAD:main")

    def pull(self) -> None:
        git(self.human, "pull", "--rebase", "origin", "main")

    def tick(self) -> None:
        cli.main(["tick", "--config-dir", str(self.tmp / "config"), "--roles-dir", str(self.tmp / "roles"),
                  "--state-dir", str(self.state)])

    def remote_head(self) -> str:
        return git(self.remote, "rev-parse", "main").strip()

    def text(self, rel: str) -> str:
        return (self.human / rel).read_text()

    def new_tasks(self) -> list[dict]:
        """Task notes created today (excludes the seeded old one)."""
        return [parse_frontmatter(p.read_text())[0] for p in sorted((self.human / "tasks").glob("T-*.md"))
                if not p.name.startswith("T-2020")]


def clean_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's .env and WORKSHEET_* variables out of tests that call main()."""
    monkeypatch.chdir(tmp_path)
    for name in [n for n in os.environ if n.startswith("WORKSHEET_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    clean_environment(tmp_path, monkeypatch)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    human = tmp_path / "human"
    git(tmp_path, "clone", str(remote), str(human))
    git(human, "config", "user.name", "Human")
    git(human, "config", "user.email", "human@example.com")

    config = tmp_path / "config"
    config.mkdir()
    (config / "workspaces.toml").write_text(
        f'[workspaces.vault]\nremote = {json.dumps(str(remote))}\nbranch = "main"\n'
        'bot_name = "worksheet-bot"\nbot_email = "worksheet-bot@users.noreply.github.com"\n')
    py = json.dumps(sys.executable)
    (config / "runners.toml").write_text(
        f'[runners.small]\ncommand = [{py}, {json.dumps(str(FAKES / "dispatcher.py"))}, "@{{prompt_file}}"]\n'
        f'[runners.strong]\ncommand = [{py}, {json.dumps(str(FAKES / "librarian.py"))}, "{{system_prompt_file}}"]\n'
        f'[runners.web]\ncommand = [{py}, {json.dumps(str(FAKES / "researcher.py"))}, "--system", "{{system_prompt}}"]\n'
        'env = { WS_RUNNER_TAG = "web-runner" }\n')
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "dispatcher.md").write_text(DISPATCHER_ROLE)
    (roles / "librarian.md").write_text(LIBRARIAN_ROLE)
    (roles / "tracker.md").write_text(TRACKER_ROLE)
    (roles / "researcher.md").write_text(RESEARCHER_ROLE)

    e = Env(tmp=tmp_path, remote=remote, human=human, state=tmp_path / "state",
            dispatch=tmp_path / "dispatch.json", listing=tmp_path / "listing.txt")
    monkeypatch.setenv("WS_DISPATCH", str(e.dispatch))
    monkeypatch.setenv("WS_LISTING", str(e.listing))
    monkeypatch.setenv("WS_MODE", "ok")
    return e


@pytest.fixture
def seeded(env: Env) -> Env:
    """A vault with a finished thread, its task note, and a wiki index already on the remote."""
    env.push({
        "inbox/other.md": "旧的要求\n\n> [!task] [[T-20200101-001]] · librarian · done\n> 已完成\n",
        "tasks/T-20200101-001.md": '---\nid: T-20200101-001\ntitle: old\nstatus: done\nrole: librarian\n'
                                   'thread: "[[other]]"\n---\n## 简报\nold\n\n## 记录\n',
        "wiki/index.md": "# 目录\n",
    })
    return env
