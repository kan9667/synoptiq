"""Tests for train-only baselines and held-out probability evaluation."""

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bust.model.baseline import (
    BASELINE_GROUP_KEYS,
    InsufficientBaselineDataError,
    apply_climatology_baseline,
    assess_spread_only_readiness,
    build_climatology_evaluation_artifact,
    fit_climatology_baseline,
    run_climatology_baseline_pipeline,
)
from bust.model.evaluate import ProbabilityEvaluation, evaluate_held_out_probabilities


def _rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"split": "train", "lead_day": 1, "window_quality": "exact", "bust": False, "region_id": "R20", "season": "JJAS", "lead_bucket": "1-3"},
            {"split": "train", "lead_day": 1, "window_quality": "exact", "bust": True, "region_id": "R20", "season": "JJAS", "lead_bucket": "1-3"},
            {"split": "train", "lead_day": 4, "window_quality": "exact", "bust": True, "region_id": "R22", "season": "other", "lead_bucket": "4-7"},
            {"split": "validation", "lead_day": 1, "window_quality": "exact", "bust": True, "region_id": "R20", "season": "JJAS", "lead_bucket": "1-3"},
            {"split": "test", "lead_day": 1, "window_quality": "exact", "bust": False, "region_id": "R20", "season": "JJAS", "lead_bucket": "1-3"},
            {"split": "test", "lead_day": 2, "window_quality": "exact", "bust": True, "region_id": "UNSEEN", "season": "JJAS", "lead_bucket": "1-3"},
            {"split": "test", "lead_day": 10, "window_quality": "unavailable", "bust": None, "region_id": "R20", "season": "JJAS", "lead_bucket": "8-10"},
        ]
    )


def test_climatology_is_train_only_preserves_order_and_keeps_day10_null() -> None:
    rows = _rows()
    model = fit_climatology_baseline(rows)
    result = apply_climatology_baseline(rows, model)

    assert result.index.tolist() == rows.index.tolist()
    assert model.global_train_rate == pytest.approx(2 / 3)
    assert model.group_rates[("R20", "JJAS", "1-3")] == pytest.approx(0.5)
    assert result.loc[4, "p_climatology"] == pytest.approx(0.5)
    # The unseen group must use only global *train* prevalence, not test labels.
    assert result.loc[5, "p_climatology"] == pytest.approx(2 / 3)
    assert result.loc[5, "climatology_status"] == "global_train_fallback"
    assert pd.isna(result.loc[6, "p_climatology"])
    assert result.loc[6, "climatology_status"] == "unavailable_window"


def test_climatology_requires_eligible_train_rows() -> None:
    rows = _rows().query("split != 'train'")
    with pytest.raises(InsufficientBaselineDataError, match="zero eligible"):
        fit_climatology_baseline(rows)


def test_spread_baseline_is_explicitly_blocked_without_measured_spread() -> None:
    readiness = assess_spread_only_readiness(_rows())
    assert readiness.status == "blocked_missing_features"
    assert readiness.missing_columns == ("rain_member_std",)


def test_held_out_evaluation_uses_only_eligible_test_rows() -> None:
    rows = _rows()
    rows["p_climatology"] = [0.5, 0.5, 1.0, 0.5, 0.5, 2 / 3, None]
    result = evaluate_held_out_probabilities(rows, "p_climatology")

    assert result.status == "eligible_test_metrics"
    assert result.sample_count == 2
    assert result.metrics is not None
    assert result.metrics["brier_score"] == pytest.approx(((0.5 - 0) ** 2 + (2 / 3 - 1) ** 2) / 2)
    assert sum(item["count"] for item in result.reliability) == 2


def test_held_out_evaluation_reports_insufficient_data_without_inventing_metrics() -> None:
    rows = _rows().query("split != 'test'").copy()
    rows["p_climatology"] = 0.5
    result = evaluate_held_out_probabilities(rows, "p_climatology")

    assert result.status == "insufficient_test_data"
    assert result.sample_count == 0
    assert result.metrics is None
    assert result.reliability == []


def test_train_rows_are_the_only_rows_used_to_fit_rates() -> None:
    """Mutating validation and test split rows cannot alter fitted train baseline rates."""
    rows = _rows()
    original_model = fit_climatology_baseline(rows)

    mutated = rows.copy()
    # Invert or alter all validation and test bust outcomes
    val_test_mask = mutated["split"].isin(["validation", "test"])
    mutated.loc[val_test_mask, "bust"] = True

    mutated_model = fit_climatology_baseline(mutated)

    assert mutated_model.global_train_rate == pytest.approx(original_model.global_train_rate)
    assert mutated_model.group_rates == original_model.group_rates
    assert mutated_model.train_sample_count == original_model.train_sample_count


def test_test_label_mutations_cannot_change_fitted_baseline_rates_or_predictions() -> None:
    """Mutating test labels cannot change fitted rates or predictions applied to test rows."""
    rows = _rows()
    model = fit_climatology_baseline(rows)
    pred_original = apply_climatology_baseline(rows, model)

    mutated = rows.copy()
    mutated.loc[mutated["split"].eq("test"), "bust"] = [True, False, True]

    mutated_model = fit_climatology_baseline(mutated)
    pred_mutated = apply_climatology_baseline(mutated, mutated_model)

    assert mutated_model.group_rates == model.group_rates
    assert mutated_model.global_train_rate == model.global_train_rate
    pd.testing.assert_series_equal(
        pred_original["p_climatology"],
        pred_mutated["p_climatology"],
        check_names=True,
    )
    pd.testing.assert_series_equal(
        pred_original["climatology_status"],
        pred_mutated["climatology_status"],
        check_names=True,
    )


def test_day10_remains_strictly_null_and_unavailable() -> None:
    """Day 10 rows must always receive null probabilities and unavailable status."""
    rows = pd.DataFrame(
        [
            {"split": "train", "lead_day": 1, "window_quality": "exact", "bust": False, "region_id": "R20", "season": "JJAS", "lead_bucket": "1-3"},
            {"split": "train", "lead_day": 10, "window_quality": "unavailable", "bust": None, "region_id": "R20", "season": "JJAS", "lead_bucket": "8-10"},
            {"split": "test", "lead_day": 10, "window_quality": "unavailable", "bust": None, "region_id": "R20", "season": "JJAS", "lead_bucket": "8-10"},
            {"split": "test", "lead_day": 10, "window_quality": "unavailable", "bust": True, "region_id": "R20", "season": "JJAS", "lead_bucket": "8-10"},
        ]
    )
    model = fit_climatology_baseline(rows)
    res = apply_climatology_baseline(rows, model)

    day10_mask = rows["lead_day"].eq(10)
    assert res.loc[day10_mask, "p_climatology"].isna().all()
    assert (res.loc[day10_mask, "climatology_status"] == "unavailable_window").all()


def test_missing_dataset_blocks_pipeline_cleanly(tmp_path: Path) -> None:
    """run_climatology_baseline_pipeline raises FileNotFoundError when the dataset is absent."""
    missing_dataset = tmp_path / "absent_rows.parquet"
    with pytest.raises(FileNotFoundError, match="Processed dataset not found"):
        run_climatology_baseline_pipeline(
            dataset_path=missing_dataset,
            model_output_path=tmp_path / "model.json",
            metrics_output_path=tmp_path / "metrics.json",
        )


def test_refuses_to_overwrite_existing_artifacts_without_replace(tmp_path: Path) -> None:
    """Pipeline refuses to overwrite existing artifacts unless replace=True is passed."""
    dataset_file = tmp_path / "test_rows.parquet"
    model_file = tmp_path / "model.json"
    metrics_file = tmp_path / "metrics.json"

    # Write a small valid parquet
    df = _rows()
    table = pa.Table.from_pandas(df)
    pq.write_table(table, dataset_file)

    manifest_file = tmp_path / "DATA_MANIFEST.csv"
    manifest_file.write_text("manifest_id,source,status\nexample-v1,example,decoded\n", encoding="utf-8")

    # First run succeeds and creates artifacts
    res = run_climatology_baseline_pipeline(
        dataset_path=dataset_file,
        model_output_path=model_file,
        metrics_output_path=metrics_file,
        manifest_path=manifest_file,
        replace=False,
    )
    assert Path(res["model_output"]).exists()
    assert Path(res["metrics_output"]).exists()

    # Second run without replace must fail with FileExistsError
    with pytest.raises(FileExistsError, match="Refusing to overwrite existing model artifact"):
        run_climatology_baseline_pipeline(
            dataset_path=dataset_file,
            model_output_path=model_file,
            metrics_output_path=metrics_file,
            manifest_path=manifest_file,
            replace=False,
        )

    # With replace=True, it succeeds
    res_replace = run_climatology_baseline_pipeline(
        dataset_path=dataset_file,
        model_output_path=model_file,
        metrics_output_path=metrics_file,
        manifest_path=manifest_file,
        replace=True,
    )
    assert res_replace["sample_counts"]["train_eligible"] == 3


def test_generated_artifact_contract_and_insufficient_test_data_preservation() -> None:
    """Artifact contract preserves run metadata, explicit spread-only blocked status, and insufficient_test_data."""
    insufficient_eval = ProbabilityEvaluation(
        status="insufficient_test_data",
        sample_count=0,
        metrics=None,
        reliability=[],
        message="No eligible test rows.",
    )
    readiness = assess_spread_only_readiness(_rows())

    artifact = build_climatology_evaluation_artifact(
        manifest_id="manifest-fp-test",
        git_commit="deadbeef",
        seed=42,
        train_count=100,
        validation_count=20,
        test_count=0,
        test_evaluation=insufficient_eval,
        spread_readiness=readiness,
    )

    assert artifact["status"] == "insufficient_test_data"
    assert artifact["test_evaluation"]["metrics"] is None
    assert artifact["test_evaluation"]["reliability"] == []
    assert artifact["test_evaluation"]["sample_count"] == 0
    assert artifact["sample_counts"]["train_eligible"] == 100
    assert artifact["sample_counts"]["test_eligible"] == 0
    assert artifact["spread_only_baseline"]["status"] == "blocked_missing_features"
    assert artifact["spread_only_baseline"]["missing_columns"] == ["rain_member_std"]
    assert artifact["run"]["manifest_id"] == "manifest-fp-test"
    assert artifact["run"]["git_commit"] == "deadbeef"
    assert artifact["run"]["split_id"] == "2010-2015/2016-2017/2018-2019"
    assert artifact["run"]["seed"] == 42
    assert artifact["run"]["group_keys"] == list(BASELINE_GROUP_KEYS)
    assert len(artifact["run"]["group_keys_sha256"]) == 64
    assert artifact["run"]["hyperparameters"]["policy"] == "regional_x_season_x_lead_bucket_train_only"


def test_spread_only_status_remains_blocked_without_rain_member_std() -> None:
    """Spread-only readiness reports blocked when rain_member_std is absent."""
    df_no_spread = _rows()
    assert "rain_member_std" not in df_no_spread.columns

    readiness = assess_spread_only_readiness(df_no_spread)
    assert readiness.status == "blocked_missing_features"
    assert readiness.missing_columns == ("rain_member_std",)
    assert "Spread-only baseline requires measured ensemble spread" in readiness.message


def test_spread_only_readiness_all_states() -> None:
    """assess_spread_only_readiness covers blocked, ready, and insufficient_data states."""
    # 1. Blocked: Column missing
    df_blocked = _rows()
    assert assess_spread_only_readiness(df_blocked).status == "blocked_missing_features"

    # 2. Ready: Column present with numeric values on eligible exact Day 1-9 rows
    df_ready = _rows()
    df_ready["rain_member_std"] = [1.2, 0.8, 2.1, 1.5, 0.4, 1.1, None]
    readiness_ready = assess_spread_only_readiness(df_ready)
    assert readiness_ready.status == "ready"
    assert readiness_ready.missing_columns == ()

    # 3. Insufficient data: Column present, but all null on eligible exact Day 1-9 rows
    df_insufficient = _rows()
    df_insufficient["rain_member_std"] = None
    readiness_insufficient = assess_spread_only_readiness(df_insufficient)
    assert readiness_insufficient.status == "insufficient_data"
    assert "no eligible measured ensemble-spread rows" in readiness_insufficient.message

    # Also unusable if non-null only on Day 10
    df_day10_only = _rows()
    df_day10_only["rain_member_std"] = [None, None, None, None, None, None, 5.0]
    assert assess_spread_only_readiness(df_day10_only).status == "insufficient_data"


def test_pipeline_spread_readiness_parquet_schema_inspection(tmp_path: Path) -> None:
    """run_climatology_baseline_pipeline inspects Parquet schema and handles missing, ready, and unusable spread."""
    import json

    manifest_file = tmp_path / "DATA_MANIFEST.csv"
    manifest_file.write_text("manifest_id,source,status\nexample-v1,example,decoded\n", encoding="utf-8")

    # Scenario A: Parquet without rain_member_std -> blocked_missing_features
    df_no_spread = _rows()
    pq_no_spread = tmp_path / "rows_no_spread.parquet"
    pq.write_table(pa.Table.from_pandas(df_no_spread), pq_no_spread)

    model_a = tmp_path / "model_a.json"
    metrics_a = tmp_path / "metrics_a.json"
    res_a = run_climatology_baseline_pipeline(
        dataset_path=pq_no_spread,
        model_output_path=model_a,
        metrics_output_path=metrics_a,
        manifest_path=manifest_file,
    )
    assert res_a["spread_only_status"] == "blocked_missing_features"
    data_metrics_a = json.loads(metrics_a.read_text(encoding="utf-8"))
    assert data_metrics_a["spread_only_baseline"]["status"] == "blocked_missing_features"
    assert data_metrics_a["spread_only_baseline"]["missing_columns"] == ["rain_member_std"]

    # Scenario B: Parquet with measured numeric rain_member_std -> ready
    df_with_spread = _rows()
    df_with_spread["rain_member_std"] = [1.2, 0.8, 2.1, 1.5, 0.4, 1.1, None]
    pq_with_spread = tmp_path / "rows_with_spread.parquet"
    pq.write_table(pa.Table.from_pandas(df_with_spread), pq_with_spread)

    model_b = tmp_path / "model_b.json"
    metrics_b = tmp_path / "metrics_b.json"
    res_b = run_climatology_baseline_pipeline(
        dataset_path=pq_with_spread,
        model_output_path=model_b,
        metrics_output_path=metrics_b,
        manifest_path=manifest_file,
    )
    assert res_b["spread_only_status"] == "ready"
    data_metrics_b = json.loads(metrics_b.read_text(encoding="utf-8"))
    assert data_metrics_b["spread_only_baseline"]["status"] == "ready"
    assert data_metrics_b["spread_only_baseline"]["missing_columns"] == []

    # Scenario C: Parquet with rain_member_std but all null -> insufficient_data
    df_unusable = _rows()
    df_unusable["rain_member_std"] = None
    pq_unusable = tmp_path / "rows_unusable.parquet"
    pq.write_table(pa.Table.from_pandas(df_unusable), pq_unusable)

    model_c = tmp_path / "model_c.json"
    metrics_c = tmp_path / "metrics_c.json"
    res_c = run_climatology_baseline_pipeline(
        dataset_path=pq_unusable,
        model_output_path=model_c,
        metrics_output_path=metrics_c,
        manifest_path=manifest_file,
    )
    assert res_c["spread_only_status"] == "insufficient_data"
    data_metrics_c = json.loads(metrics_c.read_text(encoding="utf-8"))
    assert data_metrics_c["spread_only_baseline"]["status"] == "insufficient_data"
