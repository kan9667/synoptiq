"""Shared, explicit run metadata for every automation entry point."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def emit(command: str, output: str, split: str = "not-applicable", seed: int = 42, manifest_id: str = "not-applicable") -> None:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except subprocess.CalledProcessError:
        commit = "uncommitted"
    print(f"command={command}")
    print(f"manifest_id={manifest_id}")
    print(f"git_commit={commit}")
    print(f"split={split}")
    print(f"seed={seed}")
    print(f"output_path={output}")
