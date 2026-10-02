"""Tests for validation-only calibration and evidence-bound evaluation artifacts."""

import numpy as np
import pandas as pd
import pytest

from bust.model.calibrate import apply_sigmoid_calibration, fit_validation_sigmoid_calibrator
from bust.model.evaluate import (
    ProbabilityEvaluation,
    build_evaluation_artifact,
    build_frozen_run_metadata,
)


def _rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"split": "train", "lead_day": 1, "window_quality": "exact", "bust": False, "score": 0.1},
            {"split": "validation", "lead_day": 1, "window_quality": "exact", "bust": False, "score": 0.1},
            {"split": "validation", "lead_day": 2, "window_quality": "exact", "bust": False, "score": 0.2},
            {"split": "validation", "lead_day": 3, "window_quality": "exact", "bust": True, "score": 0.8},
            {"split": "validation", "lead_day": 4, "window_quality": "exact", "bust": True, "score": 0.9},
            {"split": "test", "lead_day": 1, "window_quality": "exact", "bust": False, "score": 0.01},
            {"split": "test", "lead_day": 2, "window_quality": "exact", "bust": True, "score": 0.99},
            {"split": "test", "lead_day": 10, "window_quality": "unavailable", "bust": None, "score": 0.99},
        ]
    )


def test_sigmoid_calibrator_uses_validation_rows_only_and_keeps_day10_null() -> None:
    rows = _rows()
    calibrator = fit_validation_sigmoid_calibrator(rows, "score", seed=42)
    changed_test = rows.copy()
    changed_test.loc[changed_test["split"].eq("test"), ["score", "bust"]] = [999.0, False]
    comparison = fit_validation_sigmoid_calibrator(changed_test, "score", seed=42)

    assert calibrator.status == "ready"
    assert calibrator.sample_count == 4
    assert comparison.status == "ready"
    np.testing.assert_allclose(
        calibrator.estimator.predict_proba([[0.25], [0.75]]),
        comparison.estimator.predict_proba([[0.25], [0.75]]),
    )

    result = apply_sigmoid_calibration(rows, calibrator, "score")
    assert result.loc[result["lead_day"].eq(10), "p_calibrated"].isna().all()
    assert result.loc[result["lead_day"].between(1, 9), "p_calibrated"].notna().all()


def test_sigmoid_calibrator_reports_insufficient_validation_data() -> None:
    rows = _rows().query("split != 'validation'").copy()

    calibrator = fit_validation_sigmoid_calibrator(rows, "score")

    assert calibrator.status == "insufficient_validation_data"
    assert calibrator.sample_count == 0
    assert calibrator.estimator is None


def test_frozen_run_metadata_rejects_split_change_and_label_features() -> None:
    metadata = build_frozen_run_metadata(
        manifest_id="manifest-abc",
        git_commit="deadbeef",
        seed=42,
        feature_columns=["control_rain_mm", "lead_day"],
        hyperparameters={"num_leaves": 15},
    )

    assert metadata.feature_columns == ("control_rain_mm", "lead_day")
    assert len(metadata.feature_set_sha256) == 64
    with pytest.raises(ValueError, match="Frozen split"):
        build_frozen_run_metadata(
            manifest_id="manifest-abc",
            git_commit="deadbeef",
            seed=42,
            feature_columns=["control_rain_mm"],
            hyperparameters={},
            split_id="random",
        )
    with pytest.raises(ValueError, match="forbidden"):
        build_frozen_run_metadata(
            manifest_id="manifest-abc",
            git_commit="deadbeef",
            seed=42,
            feature_columns=["control_rain_mm", "bust"],
            hyperparameters={},
        )


def test_evaluation_artifact_preserves_explicit_no_data_and_requires_ready_calibration() -> None:
    metadata = build_frozen_run_metadata(
        manifest_id="manifest-abc",
        git_commit="deadbeef",
        seed=42,
        feature_columns=["control_rain_mm"],
        hyperparameters={"num_leaves": 15},
    )
    no_data = ProbabilityEvaluation(
        status="insufficient_test_data",
        sample_count=0,
        metrics=None,
        reliability=[],
        message="No eligible held-out rows.",
    )
    artifact = build_evaluation_artifact(
        metadata,
        no_data,
        calibration_status="insufficient_validation_data",
        calibration_sample_count=0,
    )
    assert artifact["metrics"] is None
    assert artifact["reliability"] == []
    assert artifact["run"]["manifest_id"] == "manifest-abc"

    measured = ProbabilityEvaluation(
        status="eligible_test_metrics",
        sample_count=2,
        metrics={"brier_score": 0.2, "bust_prevalence": 0.5},
        reliability=[],
        message="Measured in a test sample only.",
    )
    with pytest.raises(ValueError, match="ready validation-only calibrator"):
        build_evaluation_artifact(
            metadata,
            measured,
            calibration_status="insufficient_validation_data",
            calibration_sample_count=0,
        )
