"""Regression tests for reduced c00 LightGBM candidate and validation evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bust.model.train import (
    DEFERRED_FEATURE_GROUPS,
    PERMITTED_CANDIDATE_FEATURES,
    apply_reduced_c00_candidate,
    fit_reduced_c00_candidate,
    run_reduced_c00_pipeline,
    validate_candidate_features,
)


def _make_sample_rows() -> pd.DataFrame:
    """Generate minimal synthetic rows covering train, validation, and Day 10."""
    return pd.DataFrame(
        {
            "init_utc": [
                "2015-06-01T00:00:00Z",
                "2015-06-02T00:00:00Z",
                "2015-06-03T00:00:00Z",
                "2015-06-04T00:00:00Z",
                "2016-06-01T00:00:00Z",
                "2016-06-02T00:00:00Z",
                "2016-06-03T00:00:00Z",
                "2015-06-05T00:00:00Z",
            ],
            "split": [
                "train",
                "train",
                "train",
                "train",
                "validation",
                "validation",
                "validation",
                "train",
            ],
            "lead_day": [1, 2, 3, 4, 1, 2, 3, 10],
            "window_quality": [
                "exact",
                "exact",
                "exact",
                "exact",
                "exact",
                "exact",
                "exact",
                "unavailable",
            ],
            "region_id": [
                "R20N-078E",
                "R20N-078E",
                "R22N-080E",
                "R22N-080E",
                "R20N-078E",
                "R20N-078E",
                "R99N-099E",  # unseen validation region
                "R20N-078E",
            ],
            "season": [
                "JJAS",
                "JJAS",
                "other",
                "other",
                "JJAS",
                "JJAS",
                "other",
                "JJAS",
            ],
            "lead_bucket": [
                "1-3",
                "1-3",
                "4-7",
                "4-7",
                "1-3",
                "1-3",
                "1-3",
                "8-10",
            ],
            "f_control_mm": [5.0, 15.0, 2.0, 30.0, 6.0, 14.0, 10.0, 25.0],
            "bust": [0, 1, 0, 1, 0, 1, 0, None],
            "error_mm": [2.0, 18.0, 1.0, 35.0, 1.0, 12.0, 4.0, None],
            "o_imd_mm": [3.0, 33.0, 1.0, 65.0, 5.0, 26.0, 6.0, None],
            "threshold_mm": [12.0, 12.0, 10.0, 15.0, 12.0, 12.0, 10.0, None],
        }
    )


def test_validate_candidate_features_rejects_forbidden_and_unsupported_columns() -> None:
    """Validate that forbidden observation/label columns and arbitrary inputs cannot enter features."""
    assert validate_candidate_features(PERMITTED_CANDIDATE_FEATURES) == PERMITTED_CANDIDATE_FEATURES

    # Rejects forbidden observation / label columns
    forbidden_examples = ["bust", "error_mm", "o_imd_mm", "threshold_mm", "coverage_fraction", "init_utc"]
    for col in forbidden_examples:
        with pytest.raises(ValueError, match="forbidden observation/label/metadata columns"):
            validate_candidate_features(["f_control_mm", col])

    # Rejects unsupported features (like unverified weather features)
    with pytest.raises(ValueError, match="Reduced c00 candidate permits only"):
        validate_candidate_features(["f_control_mm", "pwat_mean"])


def test_changing_validation_labels_does_not_alter_fitted_model() -> None:
    """Ensure train-only fitting discipline: validation rows/labels do not influence fitted model."""
    df1 = _make_sample_rows()
    df2 = _make_sample_rows()
    # Flip validation labels completely in df2
    df2.loc[df2["split"].eq("validation"), "bust"] = [1, 0, 1]

    model1 = fit_reduced_c00_candidate(df1, seed=42)
    model2 = fit_reduced_c00_candidate(df2, seed=42)

    # Booster dump must be identical
    assert model1.booster.model_to_string() == model2.booster.model_to_string()
    assert model1.train_sample_count == model2.train_sample_count

    # Predictions on identical input must match exactly
    pred1 = apply_reduced_c00_candidate(df1, model1)["p_candidate"].to_numpy()
    pred2 = apply_reduced_c00_candidate(df1, model2)["p_candidate"].to_numpy()
    np.testing.assert_allclose(pred1, pred2, equal_nan=True)


def test_unavailable_day10_and_non_exact_receive_null_prediction() -> None:
    """Day 10 rows and non-exact rows must receive NaN / null prediction."""
    df = _make_sample_rows()
    model = fit_reduced_c00_candidate(df, seed=42)

    scored = apply_reduced_c00_candidate(df, model)

    # Day 10 (last row) must have NaN
    assert pd.isna(scored.iloc[7]["p_candidate"])

    # Eligible exact Day 1-9 rows must have finite probability in [0, 1]
    for idx in range(7):
        p = scored.iloc[idx]["p_candidate"]
        assert pd.notna(p)
        assert 0.0 <= p <= 1.0


def test_missing_data_semantics_null_forecast_and_null_structural_features() -> None:
    """Null f_control_mm or null categorical features must always yield null probability."""
    base_df = _make_sample_rows()
    model = fit_reduced_c00_candidate(base_df, seed=42)

    test_cases = pd.DataFrame(
        {
            "split": ["validation"] * 6,
            "lead_day": [1, 2, 3, 4, 10, 2],
            "window_quality": ["exact", "exact", "exact", "exact", "unavailable", "approximate"],
            "region_id": ["R20N-078E", None, "R20N-078E", "R99N-099E", "R20N-078E", "R20N-078E"],
            "season": ["JJAS", "JJAS", None, "JJAS", "JJAS", "JJAS"],
            "lead_bucket": ["1-3", "1-3", "1-3", None, "8-10", "1-3"],
            "f_control_mm": [None, 15.0, 10.0, 20.0, 25.0, 15.0],  # Case 0 has null rain
        }
    )

    scored = apply_reduced_c00_candidate(test_cases, model)
    probs = scored["p_candidate"].tolist()

    # Case 0: null f_control_mm -> strictly NaN
    assert pd.isna(probs[0])
    # Case 1: null region_id -> strictly NaN
    assert pd.isna(probs[1])
    # Case 2: null season -> strictly NaN
    assert pd.isna(probs[2])
    # Case 3: null lead_bucket -> strictly NaN
    assert pd.isna(probs[3])
    # Case 4: Day 10 -> strictly NaN
    assert pd.isna(probs[4])
    # Case 5: non-exact window -> strictly NaN
    assert pd.isna(probs[5])


def test_training_excludes_missing_features_and_reports_count() -> None:
    """Train rows missing any permitted feature are excluded from fitting and counted."""
    df = _make_sample_rows()
    # Introduce null f_control_mm on row 0 and null season on row 1
    df.loc[0, "f_control_mm"] = None
    df.loc[1, "season"] = None

    model = fit_reduced_c00_candidate(df, seed=42)
    # Total eligible train rows was 4; 2 had missing features -> 2 remain
    assert model.train_sample_count == 2
    assert model.train_excluded_missing_features_count == 2


def test_unseen_validation_categories_use_deterministic_fallback() -> None:
    """Non-null validation categories not in train set receive deterministic fallback code (-1)."""
    df = _make_sample_rows()
    model = fit_reduced_c00_candidate(df, seed=42)

    # Row 6 has unseen region_id "R99N-099E"
    assert "R99N-099E" not in model.categorical_encoder.categories["region_id"]

    scored = apply_reduced_c00_candidate(df, model)
    assert pd.notna(scored.iloc[6]["p_candidate"])
    assert 0.0 <= scored.iloc[6]["p_candidate"] <= 1.0


def test_overwrite_safety_when_only_txt_exists(tmp_path: Path) -> None:
    """Pipeline must refuse to overwrite if serialized .txt exists even when JSON does not exist."""
    df = _make_sample_rows()
    parquet_file = tmp_path / "rows.parquet"
    pq.write_table(pa.Table.from_pandas(df), parquet_file)

    manifest_file = tmp_path / "DATA_MANIFEST.csv"
    manifest_file.write_text("manifest_id,source,status\nexample-v1,example,decoded\n", encoding="utf-8")

    model_file = tmp_path / "candidate.json"
    metrics_file = tmp_path / "candidate_metrics.json"
    txt_file = tmp_path / "candidate.txt"

    # Pre-create only the .txt file
    txt_file.write_text("dummy booster\n", encoding="utf-8")
    assert not model_file.exists()
    assert not metrics_file.exists()

    with pytest.raises(FileExistsError, match="Refusing to overwrite existing model booster file"):
        run_reduced_c00_pipeline(
            dataset_path=parquet_file,
            model_output_path=model_file,
            metrics_output_path=metrics_file,
            manifest_path=manifest_file,
            seed=42,
            replace=False,
        )


def test_pipeline_validation_artifacts_contract_and_deferred_disclosure(tmp_path: Path) -> None:
    """End-to-end pipeline produces honest validation-only artifacts with deferred disclosures and analog evidence."""
    df = _make_sample_rows()
    # Add a mock test row to verify it is NOT loaded or scored
    df_with_test = pd.concat(
        [
            df,
            pd.DataFrame(
                {
                    "init_utc": ["2018-06-01T00:00:00Z"],
                    "split": ["test"],
                    "lead_day": [1],
                    "window_quality": ["exact"],
                    "region_id": ["R20N-078E"],
                    "season": ["JJAS"],
                    "lead_bucket": ["1-3"],
                    "f_control_mm": [10.0],
                    "bust": [0],
                    "error_mm": [1.0],
                    "o_imd_mm": [9.0],
                    "threshold_mm": [12.0],
                }
            ),
        ],
        ignore_index=True,
    )

    parquet_file = tmp_path / "rows.parquet"
    pq.write_table(pa.Table.from_pandas(df_with_test), parquet_file)

    manifest_file = tmp_path / "DATA_MANIFEST.csv"
    manifest_file.write_text("manifest_id,source,status\nexample-v1,example,decoded\n", encoding="utf-8")

    model_file = tmp_path / "reduced_c00_candidate.json"
    metrics_file = tmp_path / "reduced_c00_validation.json"

    summary = run_reduced_c00_pipeline(
        dataset_path=parquet_file,
        model_output_path=model_file,
        metrics_output_path=metrics_file,
        manifest_path=manifest_file,
        seed=42,
        replace=False,
    )

    assert Path(summary["model_output"]).exists()
    assert Path(summary["metrics_output"]).exists()
    assert Path(summary["model_txt_output"]).exists()

    # Inspect model artifact
    model_data = json.loads(model_file.read_text(encoding="utf-8"))
    assert model_data["model_type"] == "reduced_c00_only_candidate"
    assert model_data["status"] == "trained"
    assert model_data["train_sample_count"] == 4
    assert model_data["train_excluded_missing_features_count"] == 0
    assert model_data["feature_columns"] == list(PERMITTED_CANDIDATE_FEATURES)
    assert model_data["deferred_feature_groups"] == list(DEFERRED_FEATURE_GROUPS)
    assert model_data["categorical_encoding"]["fallback_code"] == -1

    # Inspect metrics artifact
    metrics_data = json.loads(metrics_file.read_text(encoding="utf-8"))
    assert metrics_data["model_type"] == "reduced_c00_only_candidate"
    assert metrics_data["data_mode"] == "validation_only"
    assert metrics_data["status"] == "validation_comparison_only"
    assert metrics_data["sample_counts"]["train_eligible"] == 4
    assert metrics_data["sample_counts"]["validation_eligible"] == 3
    assert metrics_data["sample_counts"]["test_eligible"] is None
    assert "candidate_metrics" in metrics_data["validation_evaluation"]
    assert "climatology_metrics" in metrics_data["validation_evaluation"]
    assert "brier_score_delta" in metrics_data["validation_evaluation"]
    assert metrics_data["deferred_feature_groups"] == list(DEFERRED_FEATURE_GROUPS)

    # Inspect analog evidence contract
    assert "analog_evidence" in metrics_data
    analog_ev = metrics_data["analog_evidence"]
    assert analog_ev is not None
    assert "query" in analog_ev
    assert analog_ev["query"]["region_id"] == "R20N-078E"
    assert analog_ev["query"]["init_utc"] == "2016-06-01T00:00:00Z"
    assert "status" in analog_ev
    assert "analogs" in analog_ev
    assert "post_hoc_disclosure" in analog_ev
    assert "post-hoc evidence only" in analog_ev["post_hoc_disclosure"]
    for analog in analog_ev["analogs"]:
        assert "init_utc" in analog
        assert "lead_day" in analog
        assert "f_control_mm" in analog
        assert "error_mm" in analog
        assert "bust" in analog

    # Refuse overwrite without --replace
    with pytest.raises(FileExistsError, match="Refusing to overwrite existing model artifact"):
        run_reduced_c00_pipeline(
            dataset_path=parquet_file,
            model_output_path=model_file,
            metrics_output_path=metrics_file,
            manifest_path=manifest_file,
            seed=42,
            replace=False,
        )
