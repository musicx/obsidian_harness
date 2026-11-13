import argparse
import logging
import os
import time
from pathlib import Path

from work_sheet.config import load_config
from work_sheet.engine import tick

log = logging.getLogger("work_sheet")


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.handlers.clear()
    for handler in (logging.StreamHandler(), logging.FileHandler(log_file)):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def load_dotenv(path: Path) -> None:
    """Set KEY=VALUE lines from `path` into the environment without overriding existing variables."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> None:
    load_dotenv(Path(".env"))
    env = os.environ.get
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir", type=Path, default=Path(env("WORKSHEET_CONFIG_DIR", "config")))
    common.add_argument("--roles-dir", type=Path, default=Path(env("WORKSHEET_ROLES_DIR", "roles")))
    common.add_argument("--state-dir", type=Path, default=Path(env("WORKSHEET_STATE_DIR", ".state")))
    common.add_argument("--workspace", default=env("WORKSHEET_WORKSPACE"),
                        help="workspace name (default: the only one configured)")
    parser = argparse.ArgumentParser(prog="worksheet")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tick", parents=[common], help="run one pass")
    run = commands.add_parser("run", parents=[common], help="run passes in a loop until Ctrl-C")
    run.add_argument("--interval", type=int, default=60, help="seconds between passes")
    args = parser.parse_args(argv)

    cfg = load_config(args.config_dir, args.roles_dir, args.state_dir.resolve(), args.workspace)
    setup_logging(cfg.state_dir / "engine.log")
    if args.command == "tick":
        tick(cfg)
        return
    try:
        while True:
            try:
                tick(cfg)
            except Exception:
                log.exception("tick failed")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("stopped")
