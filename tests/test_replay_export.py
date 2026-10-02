"""Regression coverage for guarded historical replay export."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from bust.api import store
from bust.api.main import app
from bust.api.replay_export import (
    MODEL_NAME,
    ReplayInputs,
    export_replay_asset,
    select_replay_inits,
    tier_for_probability,
    validate_replay_inputs,
)
from bust.data.dataset import get_manifest_fingerprint
from bust.model.train import PERMITTED_CANDIDATE_FEATURES


def test_tier_boundaries_keep_missing_scores_as_no_data() -> None:
    assert tier_for_probability(None) == "no_data"
    assert tier_for_probability(0.0) == "low"
    assert tier_for_probability(0.299999) == "low"
    assert tier_for_probability(0.30) == "watch"
    assert tier_for_probability(0.50) == "high"


def test_select_replay_inits_is_chronological_and_availability_only(tmp_path: Path) -> None:
    dates = [f"2018-01-{day:02d}T00:00:00Z" for day in range(1, 8)]
    parquet_path = tmp_path / "rows.parquet"
    pq.write_table(pa.table({"init_utc": dates, "split": ["test"] * len(dates)}), parquet_path)

    assert select_replay_inits(parquet_path) == ["2018-01-01", "2018-01-04", "2018-01-07"]


def _minimal_inputs(tmp_path: Path) -> ReplayInputs:
    dataset = tmp_path / "rows.parquet"
    pq.write_table(pa.table({"init_utc": ["2018-01-01T00:00:00Z"], "split": ["test"]}), dataset)
    manifest = tmp_path / "DATA_MANIFEST.csv"
    manifest.write_text(
        "manifest_id,status,object_key_or_url,sha256\nexample,decoded,key,hash\n",
        encoding="utf-8",
    )
    regions = tmp_path / "regions.geojson"
    regions.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "R20N-078E"},
                        "geometry": {"type": "Polygon", "coordinates": []},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate.json"
    booster = candidate.with_suffix(".txt")
    booster.write_bytes(b"saved-booster")
    booster_sha = sha256(booster.read_bytes()).hexdigest()
    manifest_id = get_manifest_fingerprint(manifest)
    run = {
        "manifest_id": manifest_id,
        "model_file_sha256": booster_sha,
        "feature_columns": list(PERMITTED_CANDIDATE_FEATURES),
    }
    candidate.write_text(
        json.dumps({"model_name": MODEL_NAME, "feature_columns": list(PERMITTED_CANDIDATE_FEATURES)}),
        encoding="utf-8",
    )
    frozen = tmp_path / "frozen.json"
    frozen.write_text(
        json.dumps(
            {
                "model_name": MODEL_NAME,
                "manifest_id": manifest_id,
                "model_file_sha256": booster_sha,
                "feature_columns": list(PERMITTED_CANDIDATE_FEATURES),
                "split_id": "2010-2015/2016-2017/2018-2019",
                "frozen_before_test_access": True,
            }
        ),
        encoding="utf-8",
    )
    calibrator = tmp_path / "calibrator.json"
    calibrator.write_text(
        json.dumps(
            {
                "status": "ready",
                "method": "sigmoid_platt",
                "parameters": {"coef": [[1.0]], "intercept": [0.0]},
                "run": run,
            }
        ),
        encoding="utf-8",
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text(json.dumps({"status": "eligible_test_metrics", "run": run}), encoding="utf-8")
    return ReplayInputs(dataset, candidate, frozen, calibrator, evaluation, manifest, regions)


def test_replay_input_validation_rejects_changed_booster(tmp_path: Path) -> None:
    inputs = _minimal_inputs(tmp_path)
    assert validate_replay_inputs(inputs)["manifest_id"] == get_manifest_fingerprint(inputs.manifest_path)

    inputs.candidate_model_path.with_suffix(".txt").write_bytes(b"changed-booster")
    with pytest.raises(ValueError, match="SHA-256"):
        validate_replay_inputs(inputs)


def test_real_export_and_api_contract_when_local_artifacts_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    required = [
        root / "data/processed/rows.parquet",
        root / "artifacts/model/reduced_c00_candidate.json",
        root / "artifacts/model/reduced_c00_frozen_run.json",
        root / "artifacts/model/reduced_c00_calibrator.json",
        root / "artifacts/metrics/reduced_c00_evaluation.json",
    ]
    if not all(path.exists() for path in required):
        pytest.skip("real replay artifacts are intentionally local and unavailable")

    output = tmp_path / "replay.json"
    metadata = tmp_path / "replay_metadata.json"
    inputs = ReplayInputs(
        root / "data/processed/rows.parquet",
        root / "artifacts/model/reduced_c00_candidate.json",
        root / "artifacts/model/reduced_c00_frozen_run.json",
        root / "artifacts/model/reduced_c00_calibrator.json",
        root / "artifacts/metrics/reduced_c00_evaluation.json",
        root / "DATA_MANIFEST.csv",
        root / "config/regions_2deg.geojson",
    )
    result = export_replay_asset(
        inputs=inputs,
        replay_output_path=output,
        metadata_output_path=metadata,
        replace=False,
        repo_root=root,
    )
    assert result["row_count"] == result["feature_count"] == 3360

    payload = json.loads(output.read_text(encoding="utf-8"))
    init = payload["available_inits"][0]
    day_one = payload["replays"][init]["1"]
    day_ten = payload["replays"][init]["10"]
    assert day_one["data_mode"] == "historical_replay"
    assert len(day_one["features"]) == 112
    assert any(feature["properties"]["p_bust"] is not None for feature in day_one["features"])
    assert any(feature["properties"]["f_control_mm"] is not None for feature in day_one["features"])
    assert {feature["properties"]["p_bust"] for feature in day_ten["features"]} == {None}
    assert {feature["properties"]["tier"] for feature in day_ten["features"]} == {"no_data"}
    assert {feature["properties"]["f_control_mm"] for feature in day_ten["features"]} == {None}

    monkeypatch.setenv("REPLAY_ASSET_PATH", str(output))
    store.load_store.cache_clear()
    try:
        client = TestClient(app)
        replay = client.get("/v1/replay", params={"init": init, "lead": 1})
        assert replay.status_code == 200
        assert replay.json()["data_mode"] == "historical_replay"
        region_id = next(
            feature["properties"]["region_id"]
            for feature in day_one["features"]
            if feature["properties"]["p_bust"] is not None
        )
        region = client.get(f"/v1/region/{region_id}", params={"init": init, "lead": 1})
        assert region.status_code == 200
        assert region.json()["source_key"]
        assert region.json()["grib_steps"]
        assert region.json()["reasons"]
        assert region.json()["forecast_mm"] is not None
        day_ten_response = client.get("/v1/replay", params={"init": init, "lead": 10})
        assert day_ten_response.status_code == 200
        assert all(item["properties"]["p_bust"] is None for item in day_ten_response.json()["features"])
    finally:
        store.load_store.cache_clear()
