"""File-backed source of generated historical replay assets."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def load_store() -> dict:
    path = Path(
        os.getenv(
            "REPLAY_ASSET_PATH",
            _project_root() / "artifacts/replay/reduced_c00_replay.json",
        )
    )
    if not path.exists():
        raise FileNotFoundError(
            f"Historical replay asset not found: {path}. "
            "Generate it with `make replay` or set REPLAY_ASSET_PATH."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("data_mode") != "historical_replay":
        raise ValueError("REPLAY_ASSET_PATH must point to a historical replay asset.")
    return payload


def available_inits() -> list[str]:
    return load_store()["available_inits"]


def replay(init: str, lead: int) -> dict | None:
    return load_store().get("replays", {}).get(init, {}).get(str(lead))


def region(init: str, lead: int, region_id: str) -> dict | None:
    return load_store().get("regions", {}).get(f"{init}:{lead}:{region_id}")


def evaluation() -> dict:
    return load_store()["evaluation"]
