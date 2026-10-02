"""Start the local API and print reproducibility metadata."""

from __future__ import annotations

import os

import uvicorn
from _run_context import ROOT, emit

from bust.data.dataset import get_manifest_fingerprint
from bust.model.evaluate import FROZEN_SPLIT_ID

replay_asset = os.getenv("REPLAY_ASSET_PATH", str(ROOT / "artifacts/replay/reduced_c00_replay.json.gz"))
host = os.getenv("API_HOST", "127.0.0.1")
port = int(os.getenv("API_PORT", "8000"))
emit(
    "api",
    f"http://{host}:{port}",
    split=FROZEN_SPLIT_ID,
    manifest_id=get_manifest_fingerprint(ROOT / "DATA_MANIFEST.csv"),
)
print(f"data_mode=historical_replay; replay_asset={replay_asset}")
uvicorn.run(
    "bust.api.main:app",
    host=host,
    port=port,
    reload=False,
)
