import os

import pytest
from conftest import Env, git

from work_sheet import cli
from work_sheet.config import load_config


def forget(monkeypatch, *names: str) -> None:
    """Unset the variables and have monkeypatch remove them again after the test, even if code sets them."""
    for name in names:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def test_load_dotenv(tmp_path, monkeypatch):
    forget(monkeypatch, "WS_A", "WS_B", "WS_C", "WS_D", "WS_E")
    monkeypatch.setenv("WS_KEEP", "original")
    dotenv = tmp_path / ".env"
    dotenv.write_text("# comment\n\n  WS_A = plain value  \nWS_B=\"double quoted\"\nWS_C='single'\n"
                      "WS_D=\"mismatched'\nWS_KEEP=from-file\n# WS_E=commented\n")
    cli.load_dotenv(dotenv)
    assert os.environ["WS_A"] == "plain value" and os.environ["WS_B"] == "double quoted"
    assert os.environ["WS_C"] == "single" and os.environ["WS_D"] == "\"mismatched'"
    assert os.environ["WS_KEEP"] == "original" and "WS_E" not in os.environ


def write_config(tmp_path, runners: str):
    config = tmp_path / "config"
    config.mkdir()
    (config / "workspaces.toml").write_text(
        '[workspaces.vault]\nremote = "${WS_REMOTE}"\nbot_name = "bot"\nbot_email = "bot@example.com"\n')
    (config / "runners.toml").write_text(runners)
    (tmp_path / "roles").mkdir()
    return config


def test_config_expands_env_references(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_REMOTE", "git@example.com:me/vault.git")
    monkeypatch.setenv("WS_MODEL", "provider/model-x")
    config = write_config(tmp_path, '[runners.strong]\n'
                          'command = ["pi", "--model", "${WS_MODEL}", "@{prompt_file}", "$HOME", "{view}"]\n'
                          'env = { MODEL_HINT = "use-${WS_MODEL}-now" }\n')
    cfg = load_config(config, tmp_path / "roles", tmp_path / "state", None)
    assert cfg.remote == "git@example.com:me/vault.git"
    assert cfg.runners["strong"].command == ["pi", "--model", "provider/model-x", "@{prompt_file}", "$HOME", "{view}"]
    assert cfg.runners["strong"].env == {"MODEL_HINT": "use-provider/model-x-now"}


def test_undefined_env_reference_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_REMOTE", "r")
    forget(monkeypatch, "WS_MISSING")
    config = write_config(tmp_path, '[runners.strong]\ncommand = ["pi", "${WS_MISSING}"]\n')
    with pytest.raises(SystemExit, match=r"runners\.toml: environment variable WS_MISSING is not set \(see \.env\.sample\)"):
        load_config(config, tmp_path / "roles", tmp_path / "state", None)


def test_cli_defaults_from_environment_and_dotenv(seeded: Env, monkeypatch):
    env = seeded
    monkeypatch.setenv("WORKSHEET_CONFIG_DIR", str(env.tmp / "config"))
    monkeypatch.setenv("WORKSHEET_ROLES_DIR", str(env.tmp / "roles"))
    forget(monkeypatch, "WORKSHEET_STATE_DIR")
    (env.tmp / ".env").write_text(f"WORKSHEET_STATE_DIR={env.tmp / 'from-dotenv'}\n")
    env.push({"inbox/a.md": "今天天气不错\n"})
    env.respond({"action": "ignore", "reason": "无关"})
    cli.main(["tick"])
    assert (env.tmp / "from-dotenv" / "vault" / "engine.log").exists()
    assert git(env.remote, "log", "-1", "--format=%s").strip() == "dispatch: a"
