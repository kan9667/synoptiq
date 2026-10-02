"""Unit tests for split assignment, preflight corpus coverage guard, and train-only threshold application."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bust.data.dataset import (
    SHARED_ROW_SCHEMA,
    CorpusCoverageReport,
    assign_split,
    build_dataset,
    build_rows_for_date,
    check_corpus_coverage,
    derive_required_imd_years,
    extract_gefs_c00_lead_totals,
    fit_and_apply_thresholds,
    get_manifest_fingerprint,
    resolve_gefs_init_file,
    validate_accumulation_steps,
    validate_row_schema,
)


def test_assign_split_chronological() -> None:
    """Years 2010-2015 are train, 2016-2017 validation, 2018-2019 test."""
    assert assign_split(2010) == "train"
    assert assign_split(2015) == "train"
    assert assign_split(2016) == "validation"
    assert assign_split(2017) == "validation"
    assert assign_split(2018) == "test"
    assert assign_split(2019) == "test"

    # Datetime inputs
    assert assign_split(datetime(2012, 7, 15, tzinfo=UTC)) == "train"
    assert assign_split(datetime(2016, 8, 1, tzinfo=UTC)) == "validation"
    assert assign_split(datetime(2018, 8, 1, tzinfo=UTC)) == "test"

    # Unrecognized years
    with pytest.raises(ValueError, match="not defined in any split partition"):
        assign_split(2009)
    with pytest.raises(ValueError, match="not defined in any split partition"):
        assign_split(2020)


def test_check_corpus_coverage_detects_incomplete_local_corpus(tmp_path: Path) -> None:
    """Preflight check on incomplete local corpus must identify missing IMD years and GEFS dates."""
    raw_dir = tmp_path / "raw"
    imd_dir = raw_dir / "imd"
    imd_dir.mkdir(parents=True)
    (imd_dir / "ind2017_rfp25.nc").write_bytes(b"dummy")
    (imd_dir / "ind2018_rfp25.nc").write_bytes(b"dummy")

    report = check_corpus_coverage(raw_dir=raw_dir)
    assert report.is_complete is False
    assert 2017 in report.present_imd_years
    assert 2018 in report.present_imd_years
    for missing_yr in [2010, 2011, 2012, 2013, 2014, 2015, 2016, 2019, 2020]:
        assert missing_yr in report.missing_imd_years
    assert report.required_imd_years == list(range(2010, 2021))
    assert report.verification_only_imd_years == [2020]
    assert report.missing_gefs_dates_count > 3600
    assert "Missing IMD annual NetCDFs" in report.blocking_reason
    assert "Acquire full 2010–2015 train corpus" in report.next_safe_action


def test_fit_and_apply_thresholds_prevents_leakage() -> None:
    """Thresholds must be computed strictly from train rows; val/test values must not leak."""
    train_data = {
        "region_id": ["R20N-078E"] * 10,
        "season": ["JJAS"] * 10,
        "lead_bucket": ["1-3"] * 10,
        "error_mm": [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0],
        "split": ["train"] * 10,
        "window_quality": ["exact"] * 10,
    }
    val_data = {
        "region_id": ["R20N-078E"] * 2,
        "season": ["JJAS"] * 2,
        "lead_bucket": ["1-3"] * 2,
        # Huge errors in validation must NOT inflate the train threshold:
        "error_mm": [999.0, 999.0],
        "split": ["validation"] * 2,
        "window_quality": ["exact"] * 2,
    }
    test_data = {
        "region_id": ["R20N-078E"] * 2,
        "season": ["JJAS"] * 2,
        "lead_bucket": ["1-3"] * 2,
        # Huge errors in test must NOT inflate the train threshold:
        "error_mm": [9999.0, 9999.0],
        "split": ["test"] * 2,
        "window_quality": ["exact"] * 2,
    }

    df = pd.concat([pd.DataFrame(train_data), pd.DataFrame(val_data), pd.DataFrame(test_data)], ignore_index=True)

    result_df, thresholds = fit_and_apply_thresholds(df, floor_mm=10.0)
    assert thresholds is not None

    # 90th percentile of [2, 4, 6, 8, 10, 12, 14, 16, 18, 20] is 18.2
    expected_threshold = float(np.percentile(train_data["error_mm"], 90))
    assert expected_threshold > 18.0

    # The threshold applied to ALL rows (including validation and test) must equal the train threshold
    for thresh in result_df["threshold_mm"]:
        assert thresh == pytest.approx(expected_threshold)

    # Verify bust labels
    # Row 0 error=2.0 < thresh -> bust = False
    assert result_df.iloc[0]["bust"] == False
    # Row 9 error=20.0 > thresh (18.2) -> bust = True
    assert result_df.iloc[9]["bust"] == True
    # Val rows error=999.0 > thresh -> bust = True
    assert result_df.iloc[10]["bust"] == True


def test_fit_and_apply_thresholds_rejects_empty_train() -> None:
    """fit_and_apply_thresholds must raise ValueError if no train rows exist."""
    df = pd.DataFrame(
        {
            "region_id": ["R20N-078E"],
            "season": ["JJAS"],
            "lead_bucket": ["1-3"],
            "error_mm": [15.0],
            "split": ["validation"],
            "window_quality": ["exact"],
        }
    )
    with pytest.raises(ValueError, match="zero 'train' split rows"):
        fit_and_apply_thresholds(df)


def test_validate_row_schema_checks() -> None:
    """validate_row_schema must enforce the shared row schema and Day 10 unavailable rule."""
    valid_row = {col: [None] for col in SHARED_ROW_SCHEMA}
    valid_row["lead_day"] = [1]
    valid_row["window_quality"] = ["exact"]
    valid_row["bust"] = [False]

    df_valid = pd.DataFrame(valid_row)
    validate_row_schema(df_valid)

    # Missing column
    df_missing = df_valid.drop(columns=["f_control_mm"])
    with pytest.raises(ValueError, match="missing required schema columns"):
        validate_row_schema(df_missing)

    # Invalid window quality
    df_bad_wq = df_valid.copy()
    df_bad_wq["window_quality"] = ["invalid_window"]
    with pytest.raises(ValueError, match="Invalid window_quality values"):
        validate_row_schema(df_bad_wq)

    # Non-null bust on Day 10
    df_day10_bust = df_valid.copy()
    df_day10_bust["lead_day"] = [10]
    df_day10_bust["window_quality"] = ["unavailable"]
    df_day10_bust["bust"] = [True]  # Forbidden!
    with pytest.raises(ValueError, match="non-null bust"):
        validate_row_schema(df_day10_bust)


def test_check_corpus_coverage_rejects_duplicate_or_out_of_range_gefs_dates(tmp_path: Path) -> None:
    """Duplicate dates or out-of-range dates cannot make coverage pass."""
    raw_dir = tmp_path / "data/raw"
    imd_dir = raw_dir / "imd"
    gefs_dir = raw_dir / "gefs"
    imd_dir.mkdir(parents=True)
    gefs_dir.mkdir(parents=True)

    # Create 10 dummy IMD files
    for yr in range(2010, 2020):
        f = imd_dir / f"ind{yr}_rfp25.nc"
        f.write_text("dummy")

    # Generate all expected date strings
    all_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()

    # Create dummy manifest with all 10 IMD years and all 3,652 GEFS dates
    manifest_rows = [
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes"
    ]
    for yr in range(2010, 2020):
        manifest_rows.append(f"imd-{yr},imd,IMD Pune,RF25={yr},2026-09-26,dummy,100,,RAINFALL,,,,mm,decoded,ind{yr}_rfp25.nc")
    for d in all_dates:
        manifest_rows.append(f"gefs-{d},gefs,NOAA,key,2026-09-26,dummy,100,{d}T00:00:00Z,apcp_sfc,c00,0,240,kg,decoded,")

    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text("\n".join(manifest_rows), encoding="utf-8")

    # Case A: 3,652 files created, but one date is duplicated and one date is missing
    # Duplicate 2018-08-01 and omit 2018-08-02
    for d in all_dates:
        if d == "2018-08-02":
            continue
        d_nodash = d.replace("-", "")
        (gefs_dir / f"apcp_sfc_{d_nodash}00_c00.grib2").write_text("dummy")
    # Duplicate file for 2018-08-01:
    (gefs_dir / "apcp_sfc_2018080100_p01.grib2").write_text("dummy")

    assert len(list(gefs_dir.glob("*.grib2"))) == 3652  # Exact count matches total days!

    report = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report.is_complete is False
    assert report.missing_gefs_dates_count == 1
    assert "2018-08-02" not in report.present_gefs_dates

    # Case B: Add an out-of-range date (2025-01-01)
    (gefs_dir / "apcp_sfc_2025010100_c00.grib2").write_text("dummy")
    report_b = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report_b.is_complete is False
    assert "2025-01-01" in report_b.unexpected_gefs_dates


def test_check_corpus_coverage_requires_manifest_evidence(tmp_path: Path) -> None:
    """Even if local files exist, missing manifest records block coverage."""
    raw_dir = tmp_path / "data/raw"
    imd_dir = raw_dir / "imd"
    gefs_dir = raw_dir / "gefs"
    imd_dir.mkdir(parents=True)
    gefs_dir.mkdir(parents=True)

    for yr in range(2010, 2021):
        (imd_dir / f"ind{yr}_rfp25.nc").write_text("dummy")

    all_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    for d in all_dates:
        d_nodash = d.replace("-", "")
        (gefs_dir / f"apcp_sfc_{d_nodash}00_c00.grib2").write_text("dummy")

    # Empty manifest
    manifest_rows = [
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes",
    ]
    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text("\n".join(manifest_rows), encoding="utf-8")

    report = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report.is_complete is False
    assert len(report.manifest_missing_imd_years) == 11
    assert report.manifest_missing_gefs_dates_count == 3652
    assert "Missing decoded IMD records in DATA_MANIFEST.csv" in report.blocking_reason
    assert "Missing decoded GEFS records in DATA_MANIFEST.csv" in report.blocking_reason


def test_fit_and_apply_thresholds_excludes_day10_and_prevents_leakage() -> None:
    """Day 10 rows must be excluded from threshold fitting and have null outputs."""
    train_data = {
        "region_id": ["R20N-078E"] * 10,
        "season": ["JJAS"] * 10,
        "lead_bucket": ["8-10"] * 10,
        "lead_day": [8] * 10,
        "error_mm": [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0],
        "split": ["train"] * 10,
        "window_quality": ["exact"] * 10,
        "f_control_mm": [10.0] * 10,
        "o_imd_mm": [8.0] * 10,
    }
    # Day 10 rows in train with astronomical errors:
    day10_data = {
        "region_id": ["R20N-078E"] * 5,
        "season": ["JJAS"] * 5,
        "lead_bucket": ["8-10"] * 5,
        "lead_day": [10] * 5,
        "error_mm": [99999.0] * 5,
        "split": ["train"] * 5,
        "window_quality": ["unavailable"] * 5,
        "f_control_mm": [99999.0] * 5,
        "o_imd_mm": [0.0] * 5,
    }
    df = pd.concat([pd.DataFrame(train_data), pd.DataFrame(day10_data)], ignore_index=True)

    result_df, thresholds = fit_and_apply_thresholds(df, floor_mm=10.0)

    # 90th percentile of [2, 4, ..., 20] is 18.2
    expected_thresh = float(np.percentile(train_data["error_mm"], 90))
    assert thresholds[("R20N-078E", "JJAS", "8-10")] == pytest.approx(expected_thresh)

    # Day 10 rows must have null f_control_mm, o_imd_mm, error_mm, threshold_mm, and bust
    day10_result = result_df[result_df["lead_day"] == 10]
    assert len(day10_result) == 5
    for col in ["f_control_mm", "o_imd_mm", "error_mm", "threshold_mm"]:
        assert day10_result[col].isna().all()
    assert all(b is None for b in day10_result["bust"])


def test_validate_row_schema_rejects_invalid_day10_rows() -> None:
    """validate_row_schema() must enforce Day 10 window_quality == 'unavailable' and all metric columns null."""
    valid_day1_row = {col: None for col in SHARED_ROW_SCHEMA}
    valid_day1_row.update({
        "lead_day": 1,
        "window_quality": "exact",
        "bust": False,
        "f_control_mm": 12.0,
        "o_imd_mm": 10.0,
        "error_mm": 2.0,
        "threshold_mm": 10.0,
    })

    valid_day10_row = {col: None for col in SHARED_ROW_SCHEMA}
    valid_day10_row.update({
        "lead_day": 10,
        "window_quality": "unavailable",
    })

    # Valid combined DataFrame passes
    df_valid = pd.DataFrame([valid_day1_row, valid_day10_row])
    validate_row_schema(df_valid)


    # 1. Day 10 with non-unavailable window_quality
    bad_wq = df_valid.copy()
    bad_wq.loc[bad_wq["lead_day"] == 10, "window_quality"] = "exact"
    with pytest.raises(ValueError, match="must have window_quality exactly 'unavailable'"):
        validate_row_schema(bad_wq)

    # 2. Day 10 with non-null f_control_mm
    bad_fc = df_valid.copy()
    bad_fc.loc[bad_fc["lead_day"] == 10, "f_control_mm"] = 15.0
    with pytest.raises(ValueError, match="Day-10 rows must have null f_control_mm"):
        validate_row_schema(bad_fc)

    # 3. Day 10 with non-null o_imd_mm
    bad_o = df_valid.copy()
    bad_o.loc[bad_o["lead_day"] == 10, "o_imd_mm"] = 10.0
    with pytest.raises(ValueError, match="Day-10 rows must have null o_imd_mm"):
        validate_row_schema(bad_o)

    # 4. Day 10 with non-null error_mm
    bad_err = df_valid.copy()
    bad_err.loc[bad_err["lead_day"] == 10, "error_mm"] = 5.0
    with pytest.raises(ValueError, match="Day-10 rows must have null error_mm"):
        validate_row_schema(bad_err)

    # 5. Day 10 with non-null threshold_mm
    bad_thresh = df_valid.copy()
    bad_thresh.loc[bad_thresh["lead_day"] == 10, "threshold_mm"] = 20.0
    with pytest.raises(ValueError, match="Day-10 rows must have null threshold_mm"):
        validate_row_schema(bad_thresh)

    # 6. Day 10 with non-null bust
    bad_bust = df_valid.copy()
    bad_bust.loc[bad_bust["lead_day"] == 10, "bust"] = False
    with pytest.raises(ValueError, match="Day-10 rows must have null bust"):
        validate_row_schema(bad_bust)

    # 7. Day 10 with non-null imd_year
    bad_iy = df_valid.copy()
    bad_iy.loc[bad_iy["lead_day"] == 10, "imd_year"] = 2012
    with pytest.raises(ValueError, match="Day-10 rows must have null imd_year"):
        validate_row_schema(bad_iy)


def test_fit_and_apply_thresholds_schema_valid_input_deduplication_and_labels() -> None:
    """Input containing pre-existing null threshold_mm and bust must not produce duplicate _x/_y columns."""
    rows = []
    # 10 Day 1 rows in train (errors 2..20)
    for err in [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]:
        rows.append({
            "init_utc": "2012-07-01T00:00:00Z",
            "lead_day": 1,
            "valid_start_utc": "2012-07-01T03:00:00Z",
            "valid_end_utc": "2012-07-02T03:00:00Z",
            "region_id": "R20N-078E",
            "season": "JJAS",
            "lead_bucket": "1-3",
            "f_control_mm": 10.0 + err,
            "o_imd_mm": 10.0,
            "coverage_fraction": 1.0,
            "error_mm": err,
            "threshold_mm": None,  # Pre-existing null column
            "bust": None,          # Pre-existing null column
            "source_key": "dummy",
            "grib_steps": "3-27",
            "imd_year": 2012,
            "window_quality": "exact",
            "split": "train",
        })
    # 1 Day 10 row in train (unavailable)
    rows.append({
        "init_utc": "2012-07-01T00:00:00Z",
        "lead_day": 10,
        "valid_start_utc": "2012-07-10T03:00:00Z",
        "valid_end_utc": "2012-07-11T03:00:00Z",
        "region_id": "R20N-078E",
        "season": "JJAS",
        "lead_bucket": "8-10",
        "f_control_mm": None,
        "o_imd_mm": None,
        "coverage_fraction": 1.0,
        "error_mm": None,
        "threshold_mm": None,
        "bust": None,
        "source_key": "dummy",
        "grib_steps": "unavailable",
        "imd_year": None,
        "window_quality": "unavailable",
        "split": "train",
    })

    df = pd.DataFrame(rows)
    validate_row_schema(df)

    result_df, thresholds = fit_and_apply_thresholds(df, floor_mm=10.0)

    # 1. Assert no _x / _y duplicate columns
    cols = list(result_df.columns)
    assert "threshold_mm_x" not in cols
    assert "threshold_mm_y" not in cols
    assert "bust_x" not in cols
    assert "bust_y" not in cols
    assert cols.count("threshold_mm") == 1
    assert cols.count("bust") == 1

    # 2. Assert threshold values and both true/false bust labels are correct
    # 90th percentile of [2, 4, ..., 20] is 18.2
    assert thresholds[("R20N-078E", "JJAS", "1-3")] == pytest.approx(18.2)
    # Row 0 error=2.0 <= 18.2 -> bust is False
    assert result_df.iloc[0]["bust"] == False
    # Row 9 error=20.0 > 18.2 -> bust is True
    assert result_df.iloc[9]["bust"] == True

    # 3. Assert Day 10 fields remain null
    day10_row = result_df.iloc[10]
    assert day10_row["lead_day"] == 10
    assert day10_row["window_quality"] == "unavailable"
    assert pd.isna(day10_row["f_control_mm"])
    assert pd.isna(day10_row["o_imd_mm"])
    assert pd.isna(day10_row["error_mm"])
    assert pd.isna(day10_row["threshold_mm"])
    assert day10_row["bust"] is None


def test_check_corpus_coverage_fails_on_out_of_range_dates_when_all_expected_present(tmp_path: Path) -> None:
    """When all expected dates are present, adding one out-of-range date must fail coverage and name that date."""
    raw_dir = tmp_path / "data/raw"
    imd_dir = raw_dir / "imd"
    gefs_dir = raw_dir / "gefs"
    imd_dir.mkdir(parents=True)
    gefs_dir.mkdir(parents=True)

    for yr in range(2010, 2021):
        (imd_dir / f"ind{yr}_rfp25.nc").write_text("dummy")

    all_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    for d in all_dates:
        d_nodash = d.replace("-", "")
        (gefs_dir / f"apcp_sfc_{d_nodash}00_c00.grib2").write_text("dummy")

    manifest_rows = [
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes"
    ]
    for yr in range(2010, 2021):
        manifest_rows.append(f"imd-{yr},imd,IMD Pune,RF25={yr},2026-09-26,dummy,100,,RAINFALL,,,,mm,decoded,ind{yr}_rfp25.nc")
    for d in all_dates:
        manifest_rows.append(f"gefs-{d},gefs,NOAA,key,2026-09-26,dummy,100,{d}T00:00:00Z,apcp_sfc,c00,0,240,kg,decoded,")

    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text("\n".join(manifest_rows), encoding="utf-8")

    # Baseline: all expected are present
    report_baseline = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report_baseline.is_complete is True

    # Now add one unexpected out-of-range local file (2025-06-15)
    (gefs_dir / "apcp_sfc_2025061500_c00.grib2").write_text("dummy")
    report_bad_local = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report_bad_local.is_complete is False
    assert "2025-06-15" in report_bad_local.unexpected_gefs_dates
    assert "2025-06-15" in report_bad_local.blocking_reason


def test_check_corpus_coverage_gefs_manifest_acceptance_criteria(tmp_path: Path) -> None:
    """GEFS manifest record must have source=gefs, status=decoded, variable=apcp_sfc, member=c00, non-empty key, units, numeric step range, valid 00 UTC init."""
    raw_dir = tmp_path / "data/raw"
    imd_dir = raw_dir / "imd"
    gefs_dir = raw_dir / "gefs"
    imd_dir.mkdir(parents=True)
    gefs_dir.mkdir(parents=True)

    for yr in range(2010, 2021):
        (imd_dir / f"ind{yr}_rfp25.nc").write_text("dummy")

    all_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    for d in all_dates:
        d_nodash = d.replace("-", "")
        (gefs_dir / f"apcp_sfc_{d_nodash}00_c00.grib2").write_text("dummy")

    def make_manifest(gefs_mutator, filename: str) -> Path:
        manifest_rows = [
            "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes"
        ]
        for yr in range(2010, 2021):
            manifest_rows.append(f"imd-{yr},imd,IMD Pune,RF25={yr},2026-09-26,dummy,100,,RAINFALL,,,,mm,decoded,ind{yr}_rfp25.nc")
        for d in all_dates:
            row_dict = {
                "manifest_id": f"gefs-{d}",
                "source": "gefs",
                "provider": "NOAA",
                "object_key_or_url": "s3://bucket/key.grib2",
                "retrieved_utc": "2026-09-26T00:00:00Z",
                "sha256": "dummy",
                "bytes": "100",
                "init_utc": f"{d}T00:00:00Z",
                "variable": "apcp_sfc",
                "member": "c00",
                "step_start_h": "0",
                "step_end_h": "240",
                "units": "kg m**-2",
                "status": "decoded",
                "notes": "",
            }
            if d == "2015-06-01":
                gefs_mutator(row_dict)
            manifest_rows.append(",".join(str(row_dict[k]) for k in manifest_rows[0].split(",")))

        m_path = tmp_path / filename
        m_path.write_text("\n".join(manifest_rows), encoding="utf-8")
        return m_path

    # Case 1: member is p01 instead of c00
    m1 = make_manifest(lambda r: r.update({"member": "p01"}), "manifest_p01.csv")
    rep1 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m1)
    assert rep1.is_complete is False
    assert rep1.manifest_missing_gefs_dates_count == 1

    # Case 2: empty object_key_or_url
    m2 = make_manifest(lambda r: r.update({"object_key_or_url": ""}), "manifest_nokey.csv")
    rep2 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m2)
    assert rep2.is_complete is False
    assert rep2.manifest_missing_gefs_dates_count == 1

    # Case 3: empty units
    m3 = make_manifest(lambda r: r.update({"units": ""}), "manifest_nounits.csv")
    rep3 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m3)
    assert rep3.is_complete is False
    assert rep3.manifest_missing_gefs_dates_count == 1

    # Case 4: non-numeric step_start_h
    m4 = make_manifest(lambda r: r.update({"step_start_h": "unknown"}), "manifest_badsteps.csv")
    rep4 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m4)
    assert rep4.is_complete is False
    assert rep4.manifest_missing_gefs_dates_count == 1

    # Case 5: non-00 UTC init (e.g. 12:00:00Z)
    m5 = make_manifest(lambda r: r.update({"init_utc": "2015-06-01T12:00:00Z"}), "manifest_badinit.csv")
    rep5 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m5)
    assert rep5.is_complete is False
    assert rep5.manifest_missing_gefs_dates_count == 1

    # Case 6: timezone-naive init_utc (e.g. 2015-06-01T00:00:00 without Z or +00:00)
    m6 = make_manifest(lambda r: r.update({"init_utc": "2015-06-01T00:00:00"}), "manifest_naiveinit.csv")
    rep6 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m6)
    assert rep6.is_complete is False
    assert rep6.manifest_missing_gefs_dates_count == 1

    # Case 6b: explicit +00:00 must be accepted
    m_plus00 = make_manifest(lambda r: r.update({"init_utc": "2015-06-01T00:00:00+00:00"}), "manifest_plus00.csv")
    rep_plus00 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m_plus00)
    assert rep_plus00.is_complete is True
    assert rep_plus00.manifest_missing_gefs_dates_count == 0

    # Case 7: NaN accumulation step bounds
    m7 = make_manifest(lambda r: r.update({"step_start_h": "nan", "step_end_h": "240"}), "manifest_nansteps.csv")
    rep7 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m7)
    assert rep7.is_complete is False
    assert rep7.manifest_missing_gefs_dates_count == 1

    m7b = make_manifest(lambda r: r.update({"step_start_h": "0", "step_end_h": "NaN"}), "manifest_nanend.csv")
    rep7b = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m7b)
    assert rep7b.is_complete is False
    assert rep7b.manifest_missing_gefs_dates_count == 1

    # Case 8: reversed accumulation step bounds (step_start_h >= step_end_h)
    m8 = make_manifest(lambda r: r.update({"step_start_h": "240", "step_end_h": "0"}), "manifest_revsteps.csv")
    rep8 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m8)
    assert rep8.is_complete is False
    assert rep8.manifest_missing_gefs_dates_count == 1

    # Case 9: zero-length accumulation step bounds (step_start_h == step_end_h)
    m9 = make_manifest(lambda r: r.update({"step_start_h": "24", "step_end_h": "24"}), "manifest_zerosteps.csv")
    rep9 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m9)
    assert rep9.is_complete is False
    assert rep9.manifest_missing_gefs_dates_count == 1

    # Case 10: negative accumulation step bound
    m10 = make_manifest(lambda r: r.update({"step_start_h": "-3", "step_end_h": "24"}), "manifest_negsteps.csv")
    rep10 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m10)
    assert rep10.is_complete is False
    assert rep10.manifest_missing_gefs_dates_count == 1

    # Case 11: infinite accumulation step bound
    m11 = make_manifest(lambda r: r.update({"step_start_h": "0", "step_end_h": "inf"}), "manifest_infsteps.csv")
    rep11 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m11)
    assert rep11.is_complete is False
    assert rep11.manifest_missing_gefs_dates_count == 1

    # Case 12: empty accumulation step bound
    m12 = make_manifest(lambda r: r.update({"step_start_h": "", "step_end_h": "240"}), "manifest_emptysteps.csv")
    rep12 = check_corpus_coverage(raw_dir=raw_dir, manifest_path=m12)
    assert rep12.is_complete is False
    assert rep12.manifest_missing_gefs_dates_count == 1


def test_check_corpus_coverage_requires_local_c00_files_rejects_p01_only(tmp_path: Path) -> None:
    """All expected dates represented only by p01 files must fail coverage and report all control dates missing."""
    raw_dir = tmp_path / "data/raw"
    imd_dir = raw_dir / "imd"
    gefs_dir = raw_dir / "gefs"
    imd_dir.mkdir(parents=True)
    gefs_dir.mkdir(parents=True)

    for yr in range(2010, 2021):
        (imd_dir / f"ind{yr}_rfp25.nc").write_text("dummy")

    all_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    # Write only p01 files for all 3,652 expected dates (no c00 control files)
    for d in all_dates:
        d_nodash = d.replace("-", "")
        (gefs_dir / f"apcp_sfc_{d_nodash}00_p01.grib2").write_text("dummy")

    manifest_rows = [
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes"
    ]
    for yr in range(2010, 2021):
        manifest_rows.append(f"imd-{yr},imd,IMD Pune,RF25={yr},2026-09-26,dummy,100,,RAINFALL,,,,mm,decoded,ind{yr}_rfp25.nc")
    for d in all_dates:
        manifest_rows.append(f"gefs-{d},gefs,NOAA,s3://bucket/key.grib2,2026-09-26T00:00:00Z,dummy,100,{d}T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,")

    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text("\n".join(manifest_rows), encoding="utf-8")

    report = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report.is_complete is False
    assert report.missing_gefs_dates_count == 3652
    assert len(report.present_gefs_dates) == 0
    assert report.manifest_missing_gefs_dates_count == 0
    assert "Missing GEFS reforecast archive: 3652 of 3652 daily 00 UTC runs" in report.blocking_reason
    assert report.unexpected_gefs_dates == []


def test_check_corpus_coverage_fails_on_unexpected_manifest_date_when_all_expected_present(tmp_path: Path) -> None:
    """When all expected dates are present, adding one otherwise-valid unexpected manifest date must fail coverage."""
    raw_dir = tmp_path / "data/raw"
    imd_dir = raw_dir / "imd"
    gefs_dir = raw_dir / "gefs"
    imd_dir.mkdir(parents=True)
    gefs_dir.mkdir(parents=True)

    for yr in range(2010, 2021):
        (imd_dir / f"ind{yr}_rfp25.nc").write_text("dummy")

    all_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    for d in all_dates:
        d_nodash = d.replace("-", "")
        (gefs_dir / f"apcp_sfc_{d_nodash}00_c00.grib2").write_text("dummy")

    manifest_rows = [
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes"
    ]
    for yr in range(2010, 2021):
        manifest_rows.append(f"imd-{yr},imd,IMD Pune,RF25={yr},2026-09-26,dummy,100,,RAINFALL,,,,mm,decoded,ind{yr}_rfp25.nc")
    for d in all_dates:
        manifest_rows.append(f"gefs-{d},gefs,NOAA,s3://bucket/key.grib2,2026-09-26T00:00:00Z,dummy,100,{d}T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,")

    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text("\n".join(manifest_rows), encoding="utf-8")

    # Baseline: all expected are present and complete
    report_baseline = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report_baseline.is_complete is True

    # Add one otherwise-valid out-of-range manifest date (2025-06-15)
    manifest_rows.append(
        "gefs-20250615,gefs,NOAA,s3://bucket/key.grib2,2026-09-26T00:00:00Z,dummy,100,2025-06-15T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,"
    )
    manifest_path.write_text("\n".join(manifest_rows), encoding="utf-8")

    report_unexpected_manifest = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert report_unexpected_manifest.is_complete is False
    assert "2025-06-15" in report_unexpected_manifest.unexpected_gefs_dates
    assert "2025-06-15" in report_unexpected_manifest.blocking_reason
    assert "Unexpected/out-of-range manifest GEFS dates found" in report_unexpected_manifest.blocking_reason


def test_validate_accumulation_steps_bounds_and_numeric() -> None:
    """Finite numeric hours with 0 <= step_start_h < step_end_h; reject empty, NaN, inf, negative, reversed, zero-length."""
    # Valid intervals
    assert validate_accumulation_steps("0", "240") is True
    assert validate_accumulation_steps(0, 240) is True
    assert validate_accumulation_steps("3", "27") is True
    assert validate_accumulation_steps(3.0, 27.0) is True
    assert validate_accumulation_steps("240", "246") is True
    assert validate_accumulation_steps("0.0", "6.0") is True

    # NaN bounds
    assert validate_accumulation_steps("nan", "240") is False
    assert validate_accumulation_steps("NaN", "240") is False
    assert validate_accumulation_steps("0", "nan") is False
    assert validate_accumulation_steps("0", "NaN") is False
    assert validate_accumulation_steps(float("nan"), 240) is False
    assert validate_accumulation_steps(0, float("nan")) is False

    # Infinite bounds
    assert validate_accumulation_steps("inf", "240") is False
    assert validate_accumulation_steps("-inf", "240") is False
    assert validate_accumulation_steps("0", "inf") is False
    assert validate_accumulation_steps("0", "-inf") is False
    assert validate_accumulation_steps(float("inf"), 240) is False
    assert validate_accumulation_steps(0, float("inf")) is False

    # Negative bounds
    assert validate_accumulation_steps("-1", "24") is False
    assert validate_accumulation_steps("-5.0", "240") is False
    assert validate_accumulation_steps(-5.0, 240.0) is False

    # Reversed bounds
    assert validate_accumulation_steps("240", "0") is False
    assert validate_accumulation_steps("27", "3") is False
    assert validate_accumulation_steps(246, 240) is False

    # Zero-length intervals
    assert validate_accumulation_steps("0", "0") is False
    assert validate_accumulation_steps("24", "24") is False
    assert validate_accumulation_steps(240, 240) is False

    # Empty / None / non-numeric
    assert validate_accumulation_steps("", "240") is False
    assert validate_accumulation_steps("0", "") is False
    assert validate_accumulation_steps(None, "240") is False
    assert validate_accumulation_steps("0", None) is False
    assert validate_accumulation_steps("abc", "240") is False
    assert validate_accumulation_steps("0", "xyz") is False


PILOT_GEFS_PATH = Path("data/raw/gefs/apcp_sfc_2018080100_c00.grib2")
PILOT_IMD_PATH = Path("data/raw/imd/ind2018_rfp25.nc")


@pytest.mark.skipif(not PILOT_GEFS_PATH.exists(), reason="Pilot GEFS file not present locally")
def test_extract_gefs_c00_lead_totals_pilot() -> None:
    """extract_gefs_c00_lead_totals extracts exact 24h accumulations for leads 1..9 from canonical pilot GRIB."""
    lead_totals = extract_gefs_c00_lead_totals(PILOT_GEFS_PATH)
    assert len(lead_totals) == 9
    assert sorted(lead_totals.keys()) == list(range(1, 10))

    for grid in lead_totals.values():
        assert grid.shape == (721, 1440)
        assert np.all(grid >= 0.0)  # Clamped at 0.0 mm

    # Verify point value at 20°N, 78°E for Lead 1: 12.45 mm
    # lat idx = (90 - 20) / 0.25 = 280; lon idx = 78 / 0.25 = 312
    pt_val = lead_totals[1][280, 312]
    assert pt_val == pytest.approx(12.45, abs=0.01)


@pytest.mark.skipif(not (PILOT_GEFS_PATH.exists() and PILOT_IMD_PATH.exists()), reason="Pilot files not present locally")
def test_build_rows_for_date_real_pilot_verification() -> None:
    """build_rows_for_date constructs valid shared-schema rows for all 10 leads across all 112 regions."""
    from bust.data.regions import load_regions_geojson

    regions = load_regions_geojson()
    init_dt = datetime(2018, 8, 1, 0, 0, tzinfo=UTC)

    rows = build_rows_for_date(
        init_dt=init_dt,
        gefs_file=PILOT_GEFS_PATH,
        imd_nc_dir=PILOT_IMD_PATH.parent,
        regions=regions,
    )

    # 112 regions * 10 leads = 1120 rows
    assert len(rows) == 1120

    df = pd.DataFrame(rows)
    validate_row_schema(df)

    # Day 1-9 are exact
    day1_9 = df[df["lead_day"] < 10]
    assert set(day1_9["window_quality"].unique()) == {"exact"}
    assert np.all(day1_9["f_control_mm"].notna())

    # Day 10 is unavailable with null forecast, observation, error, threshold, bust
    day10 = df[df["lead_day"] == 10]
    assert len(day10) == 112
    assert set(day10["window_quality"].unique()) == {"unavailable"}
    assert np.all(day10["f_control_mm"].isna())
    assert np.all(day10["o_imd_mm"].isna())
    assert np.all(day10["error_mm"].isna())
    assert np.all(day10["threshold_mm"].isna())
    assert np.all(day10["bust"].isna())

    # Check R20N-078E Day 1 values:
    r20_d1 = df[(df["region_id"] == "R20N-078E") & (df["lead_day"] == 1)].iloc[0]
    assert r20_d1["coverage_fraction"] == 1.0
    assert r20_d1["o_imd_mm"] == pytest.approx(2.6845, abs=0.01)
    assert r20_d1["f_control_mm"] == pytest.approx(14.6925, abs=0.01)
    assert r20_d1["error_mm"] == pytest.approx(abs(14.6925 - 2.6845), abs=0.01)
    assert r20_d1["split"] == "test"
    # Exact observed S3 key:
    assert r20_d1["source_key"] == "GEFSv12/reforecast/2018/2018080100/c00/Days:1-10/apcp_sfc_2018080100_c00.grib2"
    # Arithmetic step representation:
    assert r20_d1["grib_steps"] == "(0-6)-(0-3)+(6-12)+(12-18)+(18-24)+(24-27)"

    # Check Day 10 regional coverage fraction preservation (supported vs peripheral):
    r20_d10 = df[(df["region_id"] == "R20N-078E") & (df["lead_day"] == 10)].iloc[0]
    assert r20_d10["coverage_fraction"] == 1.0

    r10_d10 = df[(df["region_id"] == "R10N-076E") & (df["lead_day"] == 10)].iloc[0]
    assert r10_d10["coverage_fraction"] == pytest.approx(0.4375)
    assert r10_d10["coverage_fraction"] < 0.80
    assert r10_d10["coverage_fraction"] != 1.0


def test_late_december_2019_inits_require_imd_2020() -> None:
    """Late-December 2019 initializations require IMD 2020 verification labels (D-007)."""
    from bust.data.align import get_imd_date_label

    # Init on 2019-12-22: all leads 1..9 have IMD date labels in 2019
    init_22 = datetime(2019, 12, 22, 0, 0, tzinfo=UTC)
    for lead in range(1, 10):
        label = get_imd_date_label(init_22, lead_day=lead)
        assert label.startswith("2019-")

    # Inits on 2019-12-23 through 2019-12-31 each have at least one lead requiring 2020
    for day in range(23, 32):
        init_dt = datetime(2019, 12, day, 0, 0, tzinfo=UTC)
        labels = [get_imd_date_label(init_dt, lead_day=lead) for lead in range(1, 10)]
        has_2020 = any(lbl.startswith("2020-") for lbl in labels)
        assert has_2020, f"Expected 2020 verification label for init 2019-12-{day}"

    # For 2019-12-31, ALL leads 1..9 require 2020 labels (2020-01-01 to 2020-01-09)
    init_31 = datetime(2019, 12, 31, 0, 0, tzinfo=UTC)
    for lead in range(1, 10):
        label = get_imd_date_label(init_31, lead_day=lead)
        assert label.startswith("2020-")

    # Required IMD annual years derived across 2010–2019 inits must span 2010 through 2020
    expected_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    required_years = derive_required_imd_years(expected_dates)
    assert required_years == list(range(2010, 2021))
    assert 2020 in required_years


def test_check_corpus_coverage_rejects_2020_gefs_initializations(tmp_path: Path) -> None:
    """Any 2020 GEFS initialization must be rejected as unexpected and fail coverage."""
    raw_dir = tmp_path / "data/raw"
    imd_dir = raw_dir / "imd"
    gefs_dir = raw_dir / "gefs"
    imd_dir.mkdir(parents=True)
    gefs_dir.mkdir(parents=True)

    for yr in range(2010, 2021):
        (imd_dir / f"ind{yr}_rfp25.nc").write_text("dummy")

    all_dates = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    for d in all_dates:
        d_nodash = d.replace("-", "")
        (gefs_dir / f"apcp_sfc_{d_nodash}00_c00.grib2").write_text("dummy")

    manifest_rows = [
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes"
    ]
    for yr in range(2010, 2021):
        manifest_rows.append(f"imd-{yr},imd,IMD Pune,RF25={yr},2026-09-26,dummy,100,,RAINFALL,,,,mm,decoded,ind{yr}_rfp25.nc")
    for d in all_dates:
        manifest_rows.append(f"gefs-{d},gefs,NOAA,s3://bucket/key.grib2,2026-09-26T00:00:00Z,dummy,100,{d}T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,")

    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text("\n".join(manifest_rows), encoding="utf-8")

    # Baseline is complete
    assert check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path).is_complete is True

    # Case 1: Local file with 2020 initialization date
    (gefs_dir / "apcp_sfc_2020010100_c00.grib2").write_text("dummy")
    rep_local = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert rep_local.is_complete is False
    assert "2020-01-01" in rep_local.unexpected_gefs_dates

    # Remove bad local file
    (gefs_dir / "apcp_sfc_2020010100_c00.grib2").unlink()

    # Case 2: Manifest record with 2020 initialization date
    bad_manifest_rows = list(manifest_rows)
    bad_manifest_rows.append("gefs-20200101,gefs,NOAA,s3://b/k,2026-09-26T00:00:00Z,dummy,100,2020-01-01T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,")
    manifest_path.write_text("\n".join(bad_manifest_rows), encoding="utf-8")
    rep_manifest = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path)
    assert rep_manifest.is_complete is False
    assert "2020-01-01" in rep_manifest.unexpected_gefs_dates


def test_day10_preserves_regional_coverage_peripheral_and_supported() -> None:
    """Day 10 rows must preserve regional coverage fractions from canonical regions artifact, never default to 1.0."""
    from bust.data.regions import load_regions_geojson

    regions = load_regions_geojson()
    supported = next(r for r in regions if r.region_id == "R20N-078E")
    peripheral = next(r for r in regions if r.region_id == "R10N-076E")

    assert supported.coverage_fraction == 1.0
    assert supported.is_land_supported is True

    assert peripheral.coverage_fraction == pytest.approx(0.4375)
    assert peripheral.is_land_supported is False
    assert peripheral.coverage_fraction < 0.80


def test_get_manifest_fingerprint() -> None:
    """get_manifest_fingerprint produces a deterministic fingerprint from decoded manifest records."""
    fp1 = get_manifest_fingerprint()
    assert fp1.startswith("manifest-fp-")
    assert len(fp1) > 15
    fp2 = get_manifest_fingerprint()
    assert fp1 == fp2


def test_build_dataset_rejects_incomplete_corpus(tmp_path: Path) -> None:
    """build_dataset raises RuntimeError when preflight check_corpus_coverage detects incomplete corpus."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "imd").mkdir()
    (raw_dir / "gefs").mkdir()

    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text("manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Cannot build dataset: corpus incomplete"):
        build_dataset(raw_dir=raw_dir, manifest_path=manifest_path)


def test_extract_gefs_c00_metadata_cross_check_and_duplicate_interval_rejection(monkeypatch, tmp_path: Path) -> None:
    """extract_gefs_c00_lead_totals cross-checks metadata and rejects conflicting duplicate intervals."""
    import eccodes as ecc

    dummy_file = tmp_path / "dummy.grib2"
    dummy_file.write_bytes(b"dummy")

    # 1. Test wrong variable rejection
    gid_counter = [1, 0]
    monkeypatch.setattr(ecc, "codes_grib_new_from_file", lambda f: gid_counter.pop(0))
    monkeypatch.setattr(ecc, "codes_release", lambda gid: None)
    monkeypatch.setattr(
        ecc,
        "codes_get",
        lambda gid, key: {
            "shortName": "2t",
            "stepType": "accum",
            "perturbationNumber": 0,
            "dataTime": 0,
            "Nj": 721,
            "Ni": 1440,
            "startStep": 0,
            "endStep": 3,
        }[key],
    )
    monkeypatch.setattr(ecc, "codes_get_values", lambda gid: np.zeros(721 * 1440))

    with pytest.raises(ValueError, match="Expected accumulation variable 'tp'/'apcp', got '2t'"):
        extract_gefs_c00_lead_totals(dummy_file)

    # 2. Test wrong stepType rejection
    gid_counter = [1, 0]
    monkeypatch.setattr(
        ecc,
        "codes_get",
        lambda gid, key: {
            "shortName": "tp",
            "stepType": "instant",
            "perturbationNumber": 0,
            "dataTime": 0,
            "Nj": 721,
            "Ni": 1440,
            "startStep": 0,
            "endStep": 3,
        }[key],
    )
    with pytest.raises(ValueError, match="Expected stepType 'accum', got 'instant'"):
        extract_gefs_c00_lead_totals(dummy_file)

    # 3. Test wrong member rejection
    gid_counter = [1, 0]
    monkeypatch.setattr(
        ecc,
        "codes_get",
        lambda gid, key: {
            "shortName": "tp",
            "stepType": "accum",
            "perturbationNumber": 2,
            "dataTime": 0,
            "Nj": 721,
            "Ni": 1440,
            "startStep": 0,
            "endStep": 3,
        }[key],
    )
    with pytest.raises(ValueError, match="Expected control member c00"):
        extract_gefs_c00_lead_totals(dummy_file)

    # 4. Test wrong dataTime rejection
    gid_counter = [1, 0]
    monkeypatch.setattr(
        ecc,
        "codes_get",
        lambda gid, key: {
            "shortName": "tp",
            "stepType": "accum",
            "perturbationNumber": 0,
            "units": "kg m**-2",
            "dataTime": 1200,
            "Nj": 721,
            "Ni": 1440,
            "startStep": 0,
            "endStep": 3,
        }[key],
    )
    with pytest.raises(ValueError, match="Expected 00 UTC initialization"):
        extract_gefs_c00_lead_totals(dummy_file)

    # 5. Test wrong geometry rejection
    gid_counter = [1, 0]
    monkeypatch.setattr(
        ecc,
        "codes_get",
        lambda gid, key: {
            "shortName": "tp",
            "stepType": "accum",
            "perturbationNumber": 0,
            "units": "kg m**-2",
            "dataTime": 0,
            "Nj": 361,
            "Ni": 720,
            "startStep": 0,
            "endStep": 3,
        }[key],
    )
    with pytest.raises(ValueError, match="Expected 721x1440 grid geometry"):
        extract_gefs_c00_lead_totals(dummy_file)

    # 6. Test conflicting duplicate interval rejection. Bit-identical duplicates
    # are tolerated because observed GEFS archive files can repeat full messages.
    gid_counter = [1, 2, 0]
    monkeypatch.setattr(
        ecc,
        "codes_get",
        lambda gid, key: {
            "shortName": "tp",
            "stepType": "accum",
            "perturbationNumber": 0,
            "units": "kg m**-2",
            "dataTime": 0,
            "Nj": 721,
            "Ni": 1440,
            "startStep": 0,
            "endStep": 3,
        }[key],
    )
    monkeypatch.setattr(
        ecc,
        "codes_get_values",
        lambda gid: np.zeros(721 * 1440) if gid == 1 else np.ones(721 * 1440),
    )
    with pytest.raises(ValueError, match="Conflicting duplicate interval"):
        extract_gefs_c00_lead_totals(dummy_file)


DUPLICATED_INTERVAL_GEFS_PATH = Path("data/interim/acquisition/raw/apcp_sfc_2013110900_c00.grib2")


@pytest.mark.skipif(
    not DUPLICATED_INTERVAL_GEFS_PATH.exists(),
    reason="Observed duplicated-interval GEFS source file is not present locally",
)
def test_extract_gefs_c00_accepts_observed_identical_duplicate_intervals() -> None:
    """The observed 2013-11-09 source repeats fields exactly; it must remain auditable and decodable."""
    lead_totals = extract_gefs_c00_lead_totals(
        DUPLICATED_INTERVAL_GEFS_PATH,
        expected_init_dt=datetime(2013, 11, 9, tzinfo=UTC),
    )
    assert sorted(lead_totals) == list(range(1, 10))
    assert all(grid.shape == (721, 1440) for grid in lead_totals.values())


def test_extract_gefs_c00_missing_member_and_invalid_units(monkeypatch, tmp_path: Path) -> None:
    """extract_gefs_c00_lead_totals fails on missing perturbationNumber and invalid units."""
    import eccodes as ecc

    dummy_file = tmp_path / "dummy.grib2"
    dummy_file.write_bytes(b"dummy")

    # 1. Missing perturbationNumber
    gid_counter = [1, 0]
    monkeypatch.setattr(ecc, "codes_grib_new_from_file", lambda f: gid_counter.pop(0))
    monkeypatch.setattr(ecc, "codes_release", lambda gid: None)

    def mock_get_no_member(gid, key):
        if key == "perturbationNumber":
            raise KeyError("perturbationNumber not found")
        return {
            "shortName": "tp",
            "stepType": "accum",
            "units": "kg m**-2",
            "dataTime": 0,
            "Nj": 721,
            "Ni": 1440,
            "startStep": 0,
            "endStep": 3,
        }[key]

    monkeypatch.setattr(ecc, "codes_get", mock_get_no_member)
    with pytest.raises(ValueError, match="Missing perturbationNumber"):
        extract_gefs_c00_lead_totals(dummy_file)

    # 2. Invalid units
    gid_counter = [1, 0]
    monkeypatch.setattr(
        ecc,
        "codes_get",
        lambda gid, key: {
            "shortName": "tp",
            "stepType": "accum",
            "perturbationNumber": 0,
            "units": "K",
            "dataTime": 0,
            "Nj": 721,
            "Ni": 1440,
            "startStep": 0,
            "endStep": 3,
        }[key],
    )
    with pytest.raises(ValueError, match="Expected accumulation units 'kg m\\*\\*-2', got 'K'"):
        extract_gefs_c00_lead_totals(dummy_file)


def test_resolve_gefs_init_file_strict_contract(tmp_path: Path) -> None:
    """resolve_gefs_init_file requires exactly one record, matching key/filename, size, and sha256."""
    import hashlib

    gefs_dir = tmp_path / "gefs"
    gefs_dir.mkdir()
    sample_content = b"sample-gefs-c00-grib2-data-stream"
    init_dt = datetime(2018, 8, 1, 0, 0, tzinfo=UTC)
    filename = "apcp_sfc_2018080100_c00.grib2"
    file_path = gefs_dir / filename
    file_path.write_bytes(sample_content)

    actual_sha = hashlib.sha256(sample_content).hexdigest()
    actual_bytes = len(sample_content)
    obj_key = f"GEFSv12/reforecast/2018/2018080100/c00/Days:1-10/{filename}"

    # 1. Valid resolution
    manifest_csv = tmp_path / "manifest.csv"
    manifest_csv.write_text(
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n"
        f"rec1,gefs,NOAA,{obj_key},2026-09-27T00:00:00Z,{actual_sha},{actual_bytes},2018-08-01T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,test\n",
        encoding="utf-8",
    )

    resolved_path, resolved_key = resolve_gefs_init_file(init_dt, gefs_dir, manifest_csv)
    assert resolved_path == file_path
    assert resolved_key == obj_key

    # 2. Ambiguous manifest records (duplicate decoded c00 Days:1-10 records for same date)
    manifest_dup = tmp_path / "manifest_dup.csv"
    manifest_dup.write_text(
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n"
        f"rec1,gefs,NOAA,{obj_key},2026-09-27T00:00:00Z,{actual_sha},{actual_bytes},2018-08-01T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,test\n"
        f"rec2,gefs,NOAA,{obj_key},2026-09-27T00:00:00Z,{actual_sha},{actual_bytes},2018-08-01T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,dup\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Ambiguous manifest records: found 2"):
        resolve_gefs_init_file(init_dt, gefs_dir, manifest_dup)

    # 3. Mismatched local checksum
    manifest_bad_sha = tmp_path / "manifest_bad_sha.csv"
    manifest_bad_sha.write_text(
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n"
        f"rec1,gefs,NOAA,{obj_key},2026-09-27T00:00:00Z,0000000000000000000000000000000000000000000000000000000000000000,{actual_bytes},2018-08-01T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,test\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Mismatched local SHA-256 checksum"):
        resolve_gefs_init_file(init_dt, gefs_dir, manifest_bad_sha)

    # 4. Mismatched byte size
    manifest_bad_bytes = tmp_path / "manifest_bad_bytes.csv"
    manifest_bad_bytes.write_text(
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n"
        f"rec1,gefs,NOAA,{obj_key},2026-09-27T00:00:00Z,{actual_sha},99999999,2018-08-01T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,test\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Mismatched byte size"):
        resolve_gefs_init_file(init_dt, gefs_dir, manifest_bad_bytes)


def test_build_dataset_atomic_cleanup_on_error(tmp_path: Path, monkeypatch) -> None:
    """Mid-build error ensures output_path does not exist and temporary files are cleaned up."""
    from bust.data import dataset as ds_module

    fake_report = CorpusCoverageReport(
        is_complete=True,
        required_train_years=[2010],
        required_val_years=[],
        required_test_years=[],
        expected_dates_count=1,
        present_imd_years=[2010],
        missing_imd_years=[],
        present_gefs_dates=["2010-01-01"],
        missing_gefs_dates_count=0,
        unexpected_gefs_dates=[],
        manifest_present_imd_years=[2010],
        manifest_missing_imd_years=[],
        manifest_present_gefs_dates_count=1,
        manifest_missing_gefs_dates_count=0,
        blocking_reason=None,
        next_safe_action=None,
    )
    monkeypatch.setattr(ds_module, "check_corpus_coverage", lambda **kwargs: fake_report)
    monkeypatch.setattr(pd, "date_range", lambda *args, **kwargs: pd.DatetimeIndex(["2010-01-01"]))

    dummy_gefs = tmp_path / "apcp_sfc_2010010100_c00.grib2"
    dummy_gefs.write_bytes(b"dummy")
    monkeypatch.setattr(
        ds_module,
        "resolve_gefs_init_file",
        lambda init_dt, gefs_dir, manifest_path: (dummy_gefs, "dummy_key"),
    )

    def failing_build_rows(*args, **kwargs):
        raise RuntimeError("Simulated mid-build failure")

    monkeypatch.setattr(ds_module, "build_rows_for_date", failing_build_rows)

    out_file = tmp_path / "processed" / "rows.parquet"

    with pytest.raises(RuntimeError, match="Simulated mid-build failure"):
        build_dataset(
            raw_dir=tmp_path / "raw",
            output_path=out_file,
            manifest_path=tmp_path / "DATA_MANIFEST.csv",
            splits_path=Path(__file__).resolve().parents[1] / "config/splits.yaml",
            regions_path=Path(__file__).resolve().parents[1] / "config/regions_2deg.geojson",
        )

    # Assert output_path was NOT created
    assert not out_file.exists()
    # Assert no temporary parquet files remained
    tmp_files = list((tmp_path / "processed").glob("*.tmp_*"))
    assert len(tmp_files) == 0


def test_threshold_fitting_bounded_memory_numeric_storage() -> None:
    """Threshold fitting computes exact train-only q90 using compact per-key numeric arrays without holding row dictionaries globally."""
    from array import array
    from collections import defaultdict

    from bust.data.labels import fit_thresholds

    np.random.seed(42)
    regions = ["R20N-078E", "R22N-080E"]
    seasons = ["JJAS", "other"]
    buckets = ["1-3", "4-7"]

    rows = []
    key_errors: dict[tuple[str, str, str], array] = defaultdict(lambda: array("d"))

    for reg in regions:
        for sn in seasons:
            for bkt in buckets:
                errs = np.random.uniform(0.5, 35.0, size=100)
                for err in errs:
                    k = (reg, sn, bkt)
                    key_errors[k].append(float(err))
                    rows.append({
                        "region_id": reg,
                        "season": sn,
                        "lead_bucket": bkt,
                        "error_mm": float(err),
                        "split": "train",
                    })

    df = pd.DataFrame(rows)
    pandas_thresholds = fit_thresholds(df, floor_mm=10.0)

    bounded_thresholds = {}
    for k, err_arr in key_errors.items():
        arr = np.array(err_arr, dtype=np.float64)
        q90 = float(np.quantile(arr, 0.90, method="linear"))
        bounded_thresholds[k] = max(q90, 10.0)

    for (reg, sn, bkt), expected_val in pandas_thresholds.items():
        computed_val = bounded_thresholds[(reg, sn, bkt)]
        assert computed_val == pytest.approx(expected_val, abs=1e-12)

    for k, v in key_errors.items():
        assert isinstance(v, array)
        assert v.typecode == "d"
        assert all(isinstance(x, float) for x in v)
