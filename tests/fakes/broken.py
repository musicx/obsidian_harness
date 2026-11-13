"""Fake runner for a failing primary model: leaves half-written files in its cwd, then exits with status 3."""

from pathlib import Path

Path("wiki").mkdir(exist_ok=True)
Path("wiki/half.md").write_text("写到一半\n")
raise SystemExit(3)
