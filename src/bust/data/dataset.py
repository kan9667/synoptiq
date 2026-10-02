"""Dataset construction, split validation, corpus coverage preflight, and row schema guards."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from array import array
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from bust.data.labels import compute_bust, fit_thresholds

SHARED_ROW_SCHEMA = [
    "init_utc",
    "lead_day",
    "valid_start_utc",
    "valid_end_utc",
    "region_id",
    "season",
    "lead_bucket",
    "f_control_mm",
    "o_imd_mm",
    "coverage_fraction",
    "error_mm",
    "threshold_mm",
    "bust",
    "source_key",
    "grib_steps",
    "imd_year",
    "window_quality",
]


@dataclass(frozen=True)
class CorpusCoverageReport:
    is_complete: bool
    required_train_years: list[int]
    required_val_years: list[int]
    required_test_years: list[int]
    expected_dates_count: int
    present_imd_years: list[int]
    missing_imd_years: list[int]
    present_gefs_dates: list[str]
    missing_gefs_dates_count: int
    unexpected_gefs_dates: list[str]
    manifest_present_imd_years: list[int]
    manifest_missing_imd_years: list[int]
    manifest_present_gefs_dates_count: int
    manifest_missing_gefs_dates_count: int
    blocking_reason: str | None
    next_safe_action: str | None
    required_imd_years: list[int] = ()
    verification_only_imd_years: list[int] = ()


def load_splits_config(splits_path: Path | None = None) -> dict[str, Any]:
    """Load config/splits.yaml and return the configuration dict."""
    if splits_path is None:
        splits_path = Path(__file__).resolve().parents[3] / "config/splits.yaml"
    if not splits_path.exists():
        raise FileNotFoundError(f"Splits config not found: {splits_path}")
    with open(splits_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def assign_split(dt_or_year: datetime | int, splits_cfg: dict[str, Any] | None = None) -> str:
    """Assign a chronological split ('train', 'validation', 'test') based on year.

    Strict rules:
    - train: 2010–2015
    - validation: 2016–2017
    - test: 2018–2019
    - No random row split.
    """
    if splits_cfg is None:
        splits_cfg = load_splits_config()

    year = dt_or_year.year if isinstance(dt_or_year, datetime) else int(dt_or_year)

    if year in splits_cfg.get("train_years", []):
        return "train"
    if year in splits_cfg.get("validation_years", []):
        return "validation"
    if year in splits_cfg.get("test_years", []):
        return "test"

    raise ValueError(f"Year {year} is not defined in any split partition in config/splits.yaml")


def validate_accumulation_steps(step_start_val: Any, step_end_val: Any) -> bool:
    """Validate that accumulation steps represent a finite, non-negative, forward interval [s, e).

    Requirements:
    - Reject empty, None, or non-numeric values
    - Reject NaN and infinite bounds
    - Reject negative hours
    - Reject reversed or zero-length bounds (require 0 <= step_start_h < step_end_h)
    """
    if step_start_val is None or step_end_val is None:
        return False
    s_str = str(step_start_val).strip()
    e_str = str(step_end_val).strip()
    if not s_str or not e_str:
        return False
    try:
        s = float(s_str)
        e = float(e_str)
    except (ValueError, TypeError):
        return False
    if not math.isfinite(s) or not math.isfinite(e):
        return False
    return not (s < 0.0 or e <= s)


def derive_required_imd_years(expected_gefs_dates: list[str] | set[str]) -> list[int]:
    """Derive required IMD annual years from actual Day 1–9 verification labels of expected GEFS initializations.

    Per D-005 and D-007:
    - IMD daily date label D maps to [D-1 03:00Z, D 03:00Z).
    - GEFS Day 1-9 lead windows end between init + 27h and init + 219h.
    - Late-December initializations (2019-12-23 through 2019-12-31) have Day 1-9 valid windows ending in 2020.
    - Therefore IMD 2020 is required as verification-only coverage.
    """
    from bust.data.align import get_imd_date_label

    years: set[int] = set()
    for d_str in expected_gefs_dates:
        p = d_str.split("-")
        init_dt = datetime(int(p[0]), int(p[1]), int(p[2]), 0, 0, tzinfo=UTC)
        for lead in range(1, 10):
            label = get_imd_date_label(init_dt, lead_day=lead)
            years.add(int(label.split("-")[0]))
    return sorted(years)


def check_corpus_coverage(
    raw_dir: Path | None = None,
    manifest_path: Path | None = None,
    splits_path: Path | None = None,
) -> CorpusCoverageReport:
    """Preflight audit: establish whether real, inventoried GEFS and IMD coverage exists for all 2010–2019 years.

    Rules:
    - Requires all train (2010–2015), validation (2016–2017), and test (2018–2019) initialization years.
    - Requires exact expected 00 UTC calendar-date set for 2010-01-01 through 2019-12-31 (3,652 dates).
    - Compares sets of valid in-scope dates, rejecting duplicate or out-of-range dates.
    - Derives required IMD annual years from Day 1-9 verification labels (requiring 2010–2020, with 2020 verification-only).
    - Requires real manifest evidence from DATA_MANIFEST.csv with observed decoded metadata.
    - Never construct rows.parquet from pilots, incomplete years, invented dates, or substituted sources.
    - If any required year or archive object is missing, blocks dataset construction.
    """
    root = Path(__file__).resolve().parents[3]
    if raw_dir is None:
        raw_dir = root / "data/raw"
    if manifest_path is None:
        manifest_path = root / "DATA_MANIFEST.csv"

    splits_cfg = load_splits_config(splits_path)
    train_years = splits_cfg.get("train_years", [2010, 2011, 2012, 2013, 2014, 2015])
    val_years = splits_cfg.get("validation_years", [2016, 2017])
    test_years = splits_cfg.get("test_years", [2018, 2019])
    init_years = set(train_years + val_years + test_years)

    # 1. Exact expected 00 UTC calendar dates (2010-01-01 through 2019-12-31)
    expected_dates_list = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()
    expected_dates = set(expected_dates_list)
    total_expected_days = len(expected_dates)

    # 2. Derive required IMD annual years from actual Day 1-9 verification labels
    all_required_imd_years = derive_required_imd_years(expected_dates_list)
    verification_only_imd_years = [y for y in all_required_imd_years if y not in init_years]

    # 3. Audit local IMD annual files
    imd_dir = raw_dir / "imd"
    present_imd_years = []
    missing_imd_years = []

    for yr in all_required_imd_years:
        imd_file = imd_dir / f"ind{yr}_rfp25.nc"
        if imd_file.exists() and imd_file.stat().st_size > 0:
            present_imd_years.append(yr)
        else:
            missing_imd_years.append(yr)

    # 4. Audit local GEFS reforecast files
    gefs_dir = raw_dir / "gefs"
    valid_local_gefs_dates = set()
    unexpected_local_gefs_dates = set()

    if gefs_dir.exists():
        for grib_file in gefs_dir.glob("*.grib2"):
            name = grib_file.name
            # Check for any APCP forecast file with an unexpected / out-of-range date
            apcp_match = re.search(r"apcp_sfc_(\d{4})(\d{2})(\d{2})00", name)
            if apcp_match:
                y, m, d = apcp_match.group(1), apcp_match.group(2), apcp_match.group(3)
                try:
                    datetime(int(y), int(m), int(d), tzinfo=UTC)
                    d_str = f"{y}-{m}-{d}"
                    if d_str not in expected_dates:
                        unexpected_local_gefs_dates.add(d_str)
                except ValueError:
                    unexpected_local_gefs_dates.add(f"{y}-{m}-{d}")

            # Local control-date collection must count ONLY the expected c00 control filename/object convention.
            # p01-p04 may exist for later feature work, but must not substitute for c00.
            c00_match = re.search(r"apcp_sfc_(\d{4})(\d{2})(\d{2})00_c00", name)
            if c00_match:
                y, m, d = c00_match.group(1), c00_match.group(2), c00_match.group(3)
                try:
                    datetime(int(y), int(m), int(d), tzinfo=UTC)
                    d_str = f"{y}-{m}-{d}"
                    if d_str in expected_dates:
                        valid_local_gefs_dates.add(d_str)
                except ValueError:
                    pass

    missing_local_gefs_dates = expected_dates - valid_local_gefs_dates

    # 5. Audit DATA_MANIFEST.csv records
    manifest_present_imd_years = set()
    valid_manifest_gefs_dates = set()
    unexpected_manifest_gefs_dates = set()

    if manifest_path.exists():
        manifest_df = pd.read_csv(manifest_path, dtype=str).fillna("")
        for _, row in manifest_df.iterrows():
            src = row.get("source", "").strip()
            status = row.get("status", "").strip()

            if src == "imd" and status == "decoded":
                url_match = re.search(r"RF25=(\d{4})", row.get("object_key_or_url", ""))
                notes_match = re.search(r"ind(\d{4})_rfp25\.nc", row.get("notes", ""))
                yr = None
                if url_match:
                    yr = int(url_match.group(1))
                elif notes_match:
                    yr = int(notes_match.group(1))
                if yr is not None and yr in all_required_imd_years:
                    manifest_present_imd_years.add(yr)

            elif (
                src == "gefs"
                and status == "decoded"
                and row.get("variable", "").strip() == "apcp_sfc"
                and row.get("member", "").strip() == "c00"
                and bool(row.get("object_key_or_url", "").strip())
                and bool(row.get("units", "").strip())
            ):
                # Verify numeric accumulation-step start and end (finite, 0 <= step_start_h < step_end_h)
                step_start = row.get("step_start_h", "")
                step_end = row.get("step_end_h", "")
                if validate_accumulation_steps(step_start, step_end):
                    init_str = row.get("init_utc", "").strip()
                    # Require explicit UTC timezone (Z or +00:00); reject timezone-naive datetimes
                    date_match = re.search(r"^(\d{4})-(\d{2})-(\d{2})[T ]00:00:00(?:\.0+)?(Z|\+00:00)$", init_str)
                    if date_match:
                        y, m, d = date_match.group(1), date_match.group(2), date_match.group(3)
                        try:
                            datetime(int(y), int(m), int(d), tzinfo=UTC)
                            d_str = f"{y}-{m}-{d}"
                            if d_str in expected_dates:
                                valid_manifest_gefs_dates.add(d_str)
                            else:
                                unexpected_manifest_gefs_dates.add(d_str)
                        except ValueError:
                            unexpected_manifest_gefs_dates.add(f"{y}-{m}-{d}")

    manifest_missing_imd_years = [y for y in all_required_imd_years if y not in manifest_present_imd_years]
    manifest_missing_gefs_dates = expected_dates - valid_manifest_gefs_dates

    is_complete = (
        len(missing_imd_years) == 0
        and len(missing_local_gefs_dates) == 0
        and len(manifest_missing_imd_years) == 0
        and len(manifest_missing_gefs_dates) == 0
        and len(unexpected_local_gefs_dates) == 0
        and len(unexpected_manifest_gefs_dates) == 0
    )

    blocking_reason = None
    next_safe_action = None

    if not is_complete:
        reasons = []
        if missing_imd_years:
            vo_missing = sorted(set(missing_imd_years) & set(verification_only_imd_years))
            vo_note = f" (including verification-only years: {vo_missing})" if vo_missing else ""
            reasons.append(f"Missing IMD annual NetCDFs for years: {missing_imd_years}{vo_note}")
        if manifest_missing_imd_years:
            vo_manifest_missing = sorted(set(manifest_missing_imd_years) & set(verification_only_imd_years))
            vo_note = f" (including verification-only years: {vo_manifest_missing})" if vo_manifest_missing else ""
            reasons.append(f"Missing decoded IMD records in DATA_MANIFEST.csv for years: {manifest_missing_imd_years}{vo_note}")
        if missing_local_gefs_dates:
            reasons.append(
                f"Missing GEFS reforecast archive: {len(missing_local_gefs_dates)} of {total_expected_days} daily 00 UTC runs "
                f"(only {sorted(valid_local_gefs_dates)} present)"
            )
        if manifest_missing_gefs_dates:
            reasons.append(
                f"Missing decoded GEFS records in DATA_MANIFEST.csv: {len(manifest_missing_gefs_dates)} of {total_expected_days} daily 00 UTC runs "
                f"(only {sorted(valid_manifest_gefs_dates)} present)"
            )
        if unexpected_local_gefs_dates:
            reasons.append(f"Unexpected/out-of-range local GEFS dates found: {sorted(unexpected_local_gefs_dates)}")
        if unexpected_manifest_gefs_dates:
            reasons.append(f"Unexpected/out-of-range manifest GEFS dates found: {sorted(unexpected_manifest_gefs_dates)}")

        blocking_reason = "; ".join(reasons)
        next_safe_action = (
            "Acquire full 2010–2015 train corpus (IMD annual NetCDFs and GEFSv12 00 UTC reforecasts) "
            "and record verified decoded entries in DATA_MANIFEST.csv before creating data/processed/rows.parquet."
        )

    return CorpusCoverageReport(
        is_complete=is_complete,
        required_train_years=train_years,
        required_val_years=val_years,
        required_test_years=test_years,
        expected_dates_count=total_expected_days,
        present_imd_years=present_imd_years,
        missing_imd_years=missing_imd_years,
        present_gefs_dates=sorted(valid_local_gefs_dates),
        missing_gefs_dates_count=len(missing_local_gefs_dates),
        unexpected_gefs_dates=sorted(unexpected_local_gefs_dates | unexpected_manifest_gefs_dates),
        manifest_present_imd_years=sorted(manifest_present_imd_years),
        manifest_missing_imd_years=manifest_missing_imd_years,
        manifest_present_gefs_dates_count=len(valid_manifest_gefs_dates),
        manifest_missing_gefs_dates_count=len(manifest_missing_gefs_dates),
        blocking_reason=blocking_reason,
        next_safe_action=next_safe_action,
        required_imd_years=all_required_imd_years,
        verification_only_imd_years=verification_only_imd_years,
    )


def fit_and_apply_thresholds(
    df: pd.DataFrame,
    floor_mm: float = 10.0,
) -> tuple[pd.DataFrame, pd.Series]:
    """Fit thresholds only on eligible train rows, then apply thresholds and compute bust labels across all splits.

    Guards:
    - Input handling: Safely removes any pre-existing 'threshold_mm' and 'bust' columns from the working copy.
    - Preserves input row order.
    - Train-only fit: thresholds are computed exclusively on eligible train rows (split == 'train').
    - Exclude Day 10 and unavailable windows: Day 10 and window_quality == 'unavailable' rows are strictly excluded from threshold fitting.
    - Minimum error floor: thresholds are clipped to at least floor_mm (default 10.0 mm/day).
    - Leakage prevention: validation, test, and Day-10 rows never influence threshold values.
    - Merges using threshold keys with validate='many_to_one'.
    - Returns exactly one 'threshold_mm' column and exactly one 'bust' column.
    - Day-10 / window_quality=unavailable rows retain null forecast, observation, error, threshold, and bust fields.
    """
    if "split" not in df.columns:
        raise ValueError("DataFrame must contain a 'split' column")

    working_df = df.copy()

    # Track original row order to guarantee preservation across any merge operations
    working_df["_row_order"] = np.arange(len(working_df))

    # Safely remove pre-existing threshold_mm and bust columns to prevent duplicate columns (_x, _y)
    drop_cols = [c for c in ["threshold_mm", "bust"] if c in working_df.columns]
    if drop_cols:
        working_df = working_df.drop(columns=drop_cols)

    # Exclude every unavailable / Day-10 row from threshold fitting
    is_day10 = (working_df["lead_day"] == 10) if "lead_day" in working_df.columns else pd.Series(False, index=working_df.index)
    is_unavail = (working_df["window_quality"] == "unavailable") if "window_quality" in working_df.columns else pd.Series(False, index=working_df.index)

    eligible_train_mask = (working_df["split"] == "train") & (~is_day10) & (~is_unavail)
    train_rows = working_df[eligible_train_mask]
    if len(train_rows) == 0:
        raise ValueError("Cannot fit thresholds: DataFrame contains zero 'train' split rows")

    thresholds = fit_thresholds(train_rows, floor_mm=floor_mm)

    keys = ["region_id", "season", "lead_bucket"]
    merged = working_df.merge(
        thresholds.rename("threshold_mm"),
        on=keys,
        how="left",
        validate="many_to_one",
    )

    # Restore exact input row order and clean up helper column
    merged = merged.sort_values("_row_order").drop(columns=["_row_order"]).reset_index(drop=True)

    # Identify unavailable or Day-10 rows
    is_day10_merged = (merged["lead_day"] == 10) if "lead_day" in merged.columns else pd.Series(False, index=merged.index)
    is_unavail_merged = (merged["window_quality"] == "unavailable") if "window_quality" in merged.columns else pd.Series(False, index=merged.index)
    unavailable_mask = is_day10_merged | is_unavail_merged

    # For unavailable rows, threshold_mm must be null
    merged.loc[unavailable_mask, "threshold_mm"] = np.nan

    # Compute bust label for each row: unavailable or Day 10 rows must have null bust
    merged["bust"] = [
        compute_bust(err, thresh) if (not unavail and pd.notna(err) and pd.notna(thresh)) else None
        for err, thresh, unavail in zip(merged["error_mm"], merged["threshold_mm"], unavailable_mask)
    ]

    # Day 10 / window_quality=unavailable rows must retain null forecast, observation, error, threshold, bust, and imd_year fields
    if unavailable_mask.any():
        for col in ["f_control_mm", "o_imd_mm", "error_mm", "threshold_mm"]:
            if col in merged.columns:
                merged.loc[unavailable_mask, col] = np.nan
        if "imd_year" in merged.columns:
            merged.loc[unavailable_mask, "imd_year"] = np.nan
        merged.loc[unavailable_mask, "bust"] = None

    return merged, thresholds



def validate_row_schema(df: pd.DataFrame) -> None:
    """Validate that df conforms strictly to the frozen shared row schema."""
    missing = set(SHARED_ROW_SCHEMA) - set(df.columns)
    if missing:
        raise ValueError(f"Rows DataFrame is missing required schema columns: {sorted(missing)}")

    # Check window quality values
    allowed_wq = {"exact", "approximate", "unavailable"}
    found_wq = set(df["window_quality"].unique())
    if not found_wq.issubset(allowed_wq):
        raise ValueError(f"Invalid window_quality values: {found_wq - allowed_wq}; allowed: {allowed_wq}")

    # Check Day-10 rows
    day10_rows = df[df["lead_day"] == 10]
    if len(day10_rows) > 0:
        # validate_row_schema() must reject Day-10 rows unless window_quality is exactly unavailable
        invalid_wq = day10_rows[day10_rows["window_quality"] != "unavailable"]
        if len(invalid_wq) > 0:
            raise ValueError(
                f"Found {len(invalid_wq)} Day 10 rows with window_quality != 'unavailable'; "
                "Day-10 rows must have window_quality exactly 'unavailable'."
            )

        # Day-10 rows must have null f_control_mm, o_imd_mm, error_mm, threshold_mm, bust, and imd_year
        for col in ["f_control_mm", "o_imd_mm", "error_mm", "threshold_mm", "bust", "imd_year"]:
            non_null = day10_rows[day10_rows[col].notna()]
            if len(non_null) > 0:
                raise ValueError(
                    f"Found {len(non_null)} Day 10 rows with non-null {col}; "
                    f"Day-10 rows must have null {col}."
                )


def extract_gefs_c00_lead_totals(grib_path: str | Path, expected_init_dt: datetime | None = None) -> dict[int, np.ndarray]:
    """Decode a GEFS c00 Days 1-10 GRIB file and derive exact 24h accumulations for leads 1..9.

    Audited D1-04 interval alignment:
    - Lead L (1..9): target window is [init + (24L-21)h, init + (24L+3)h).
    - Intervals derived from 3-hour non-overlapping pieces:
      [24L-21, 24L-18) = (24L-18h accum) - (24L-21h accum)
      [24L-18, 24L-12) = 6h accum
      [24L-12, 24L-6)  = 6h accum
      [24L-6, 24L)     = 6h accum
      [24L, 24L+3)     = 3h accum
    - Clamped at 0.0 mm to remove tiny GRIB packing precision artifacts.
    - Day 10 is unavailable because +240–+243h is not evidenced in the archive.

    Cross-checks GRIB metadata:
    - shortName must be 'tp' or 'apcp'
    - stepType must be 'accum'
    - perturbationNumber must be 0 (c00 control)
    - dataTime must be 0 (00 UTC init)
    - grid geometry must be 721x1440 (0.25° grid)
    - accepts only bit-identical duplicate step intervals; rejects conflicting duplicates
    - verifies all required intervals for leads 1..9 are present
    """
    import eccodes as ecc

    path = Path(grib_path)
    if not path.exists():
        raise FileNotFoundError(f"GEFS GRIB file not found: {path}")

    steps_map: dict[tuple[int, int], np.ndarray] = {}
    with open(path, "rb") as f:
        while True:
            gid = ecc.codes_grib_new_from_file(f)
            if not gid:
                break
            try:
                var_name = ecc.codes_get(gid, "shortName")
                if var_name not in ("tp", "apcp"):
                    raise ValueError(f"Expected accumulation variable 'tp'/'apcp', got '{var_name}' in {path.name}")

                step_type = ecc.codes_get(gid, "stepType")
                if step_type != "accum":
                    raise ValueError(f"Expected stepType 'accum', got '{step_type}' in {path.name}")

                try:
                    pert_num = int(ecc.codes_get(gid, "perturbationNumber"))
                except Exception as err:
                    raise ValueError(f"Missing perturbationNumber in GRIB message in {path.name}") from err

                if pert_num != 0:
                    raise ValueError(f"Expected control member c00 (number 0), got member {pert_num} in {path.name}")

                try:
                    units = ecc.codes_get(gid, "units")
                except Exception as err:
                    raise ValueError(f"Missing units in GRIB message in {path.name}") from err

                if units not in ("kg m**-2", "kg m^-2", "kg/m^2", "mm"):
                    raise ValueError(f"Expected accumulation units 'kg m**-2', got '{units}' in {path.name}")

                data_time = int(ecc.codes_get(gid, "dataTime"))
                if data_time != 0:
                    raise ValueError(f"Expected 00 UTC initialization (dataTime=0), got {data_time} in {path.name}")

                if expected_init_dt is not None:
                    data_date = int(ecc.codes_get(gid, "dataDate"))
                    expected_date_int = int(expected_init_dt.strftime("%Y%m%d"))
                    if data_date != expected_date_int:
                        raise ValueError(f"GRIB dataDate {data_date} does not match expected {expected_date_int} in {path.name}")

                nj = int(ecc.codes_get(gid, "Nj"))
                ni = int(ecc.codes_get(gid, "Ni"))
                if (nj, ni) != (721, 1440):
                    raise ValueError(f"Expected 721x1440 grid geometry, got {nj}x{ni} in {path.name}")

                ss = int(ecc.codes_get(gid, "startStep"))
                es = int(ecc.codes_get(gid, "endStep"))

                vals = np.asarray(ecc.codes_get_values(gid))
                existing = steps_map.get((ss, es))
                if existing is not None:
                    # Some observed GEFS archive objects repeat complete GRIB messages.
                    # A duplicate is usable only when it is exactly the same decoded field;
                    # otherwise choosing either value would silently invent an accumulation.
                    if existing.shape != vals.shape or not np.array_equal(existing, vals, equal_nan=True):
                        raise ValueError(
                            f"Conflicting duplicate interval ({ss}, {es}) detected in {path.name}"
                        )
                    continue

                steps_map[(ss, es)] = vals
            finally:
                ecc.codes_release(gid)

    # Verify all required intervals for leads 1..9 are present
    for lead in range(1, 10):
        s = 24 * lead - 21
        required_steps = [
            (s - 3, s + 3),
            (s - 3, s),
            (s + 3, s + 9),
            (s + 9, s + 15),
            (s + 15, s + 21),
            (s + 21, s + 24),
        ]
        for step in required_steps:
            if step not in steps_map:
                raise ValueError(f"Missing required GRIB accumulation step {step} for lead {lead} in {path.name}")

    lead_totals: dict[int, np.ndarray] = {}
    for lead in range(1, 10):
        s = 24 * lead - 21
        # Piece 1: [s, s+3)
        p1 = steps_map[(s - 3, s + 3)] - steps_map[(s - 3, s)]
        # Middle 3 6-hour accumulations:
        p2 = steps_map[(s + 3, s + 9)]
        p3 = steps_map[(s + 9, s + 15)]
        p4 = steps_map[(s + 15, s + 21)]
        # Piece 5: [e-3, e)
        p5 = steps_map[(s + 21, s + 24)]

        total_1d = np.clip(p1 + p2 + p3 + p4 + p5, 0.0, None)
        lead_totals[lead] = total_1d.reshape(721, 1440)

    return lead_totals


def resolve_gefs_init_file(
    init_dt: datetime,
    gefs_dir: Path | str,
    manifest_path: Path | str | None = None,
) -> tuple[Path, str]:
    """Strictly resolve the canonical local GEFS c00 APCP Days:1-10 file and its source key.

    Requirements:
    - Exactly one decoded c00 APCP Days:1-10 manifest record for the given 00 UTC init date.
    - The observed object key and local canonical filename must agree (apcp_sfc_YYYYMMDD00_c00.grib2).
    - The local file must exist, and its byte size and SHA-256 checksum must match the manifest record.
    - No guessed source-key fallback, no broad exception swallowing, and no *_c00*.grib2 first-match globbing.
    """
    from bust.data.align import validate_utc_datetime

    validate_utc_datetime(init_dt, "init_dt")
    if init_dt.hour != 0 or init_dt.minute != 0 or init_dt.second != 0:
        raise ValueError(f"init_dt must be 00:00:00 UTC; got {init_dt.isoformat()}")

    root = Path(__file__).resolve().parents[3]
    gefs_p = Path(gefs_dir)
    mpath = Path(manifest_path) if manifest_path else (root / "DATA_MANIFEST.csv")

    if not mpath.exists():
        raise FileNotFoundError(f"DATA_MANIFEST.csv not found at {mpath}")

    manifest_df = pd.read_csv(mpath, dtype=str).fillna("")
    d_str = init_dt.strftime("%Y-%m-%d")
    expected_filename = f"apcp_sfc_{init_dt.strftime('%Y%m%d')}00_c00.grib2"

    matches: list[pd.Series] = []
    for _, row in manifest_df.iterrows():
        if (
            row.get("source", "").strip() == "gefs"
            and row.get("status", "").strip() == "decoded"
            and row.get("variable", "").strip() == "apcp_sfc"
            and row.get("member", "").strip() == "c00"
        ):
            init_utc = row.get("init_utc", "").strip()
            date_match = re.search(r"^(\d{4}-\d{2}-\d{2})[T ]00:00:00(?:\.0+)?(Z|\+00:00)$", init_utc)
            if not date_match or date_match.group(1) != d_str:
                continue

            obj_key = row.get("object_key_or_url", "").strip()
            if "Days:1-10" not in obj_key and "/c00/Days:1-10/" not in obj_key:
                continue

            matches.append(row)

    if len(matches) == 0:
        raise FileNotFoundError(f"Missing decoded c00 APCP Days:1-10 manifest record for date {d_str} in {mpath}")
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous manifest records: found {len(matches)} decoded c00 APCP Days:1-10 records for date {d_str} in {mpath}"
        )

    rec = matches[0]
    source_key = rec.get("object_key_or_url", "").strip()
    key_filename = Path(source_key).name
    if key_filename != expected_filename:
        raise ValueError(
            f"Observed object key filename '{key_filename}' does not agree with canonical name "
            f"'{expected_filename}' for date {d_str}"
        )

    local_path = gefs_p / key_filename
    if not local_path.exists():
        raise FileNotFoundError(f"Canonical local GEFS file not found: {local_path}")

    manifest_bytes_str = rec.get("bytes", "").strip()
    if not manifest_bytes_str:
        raise ValueError(f"Manifest record missing 'bytes' field for {source_key}")
    expected_bytes = int(manifest_bytes_str)
    actual_bytes = local_path.stat().st_size
    if actual_bytes != expected_bytes:
        raise ValueError(
            f"Mismatched byte size for {local_path.name}: manifest specifies {expected_bytes}, "
            f"actual local file has {actual_bytes}"
        )

    manifest_sha256 = rec.get("sha256", "").strip().lower()
    if not manifest_sha256:
        raise ValueError(f"Manifest record missing 'sha256' checksum for {source_key}")

    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    actual_sha256 = h.hexdigest().lower()
    if actual_sha256 != manifest_sha256:
        raise ValueError(
            f"Mismatched local SHA-256 checksum for {local_path.name}: "
            f"manifest specifies {manifest_sha256}, actual local file has {actual_sha256}"
        )

    return local_path, source_key


def build_rows_for_date(
    init_dt: datetime,
    gefs_file: Path,
    imd_nc_dir: Path,
    regions: list[Any],
    splits_cfg: dict[str, Any] | None = None,
    open_imd_datasets: dict[int, Any] | None = None,
    manifest_path: Path | None = None,
    source_key: str | None = None,
) -> list[dict[str, Any]]:
    """Build shared-schema row dictionaries for a single 00 UTC GEFS initialization across leads 1..10."""
    import xarray as xr

    from bust.data.align import get_imd_date_label, get_lead_window
    from bust.data.labels import get_lead_bucket, get_season
    from bust.data.regions import aggregate_imd_daily_region

    if splits_cfg is None:
        splits_cfg = load_splits_config()

    split_name = assign_split(init_dt.year, splits_cfg)
    init_str = f"{init_dt.strftime('%Y-%m-%d')}T00:00:00Z"

    if source_key is None:
        root = Path(__file__).resolve().parents[3]
        mpath = manifest_path or (root / "DATA_MANIFEST.csv")
        resolved_file, resolved_key = resolve_gefs_init_file(
            init_dt=init_dt,
            gefs_dir=gefs_file.parent,
            manifest_path=mpath,
        )
        if gefs_file.resolve() != resolved_file.resolve():
            raise ValueError(
                f"Supplied gefs_file '{gefs_file}' does not match canonical resolved file '{resolved_file}'"
            )
        source_key = resolved_key

    lead_totals = extract_gefs_c00_lead_totals(gefs_file, expected_init_dt=init_dt)
    rows: list[dict[str, Any]] = []

    for lead_day in range(1, 11):
        start_utc, end_utc, window_quality = get_lead_window(init_dt, lead_day=lead_day)
        valid_start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        valid_end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        season = get_season(start_utc)
        lead_bucket = get_lead_bucket(lead_day)

        if lead_day == 10 or window_quality == "unavailable":
            for r in regions:
                cov_frac = getattr(r, "coverage_fraction", None)
                rows.append({
                    "init_utc": init_str,
                    "lead_day": 10,
                    "valid_start_utc": valid_start_str,
                    "valid_end_utc": valid_end_str,
                    "region_id": r.region_id,
                    "season": season,
                    "lead_bucket": lead_bucket,
                    "f_control_mm": None,
                    "o_imd_mm": None,
                    "coverage_fraction": cov_frac,
                    "error_mm": None,
                    "threshold_mm": None,
                    "bust": None,
                    "source_key": source_key,
                    "grib_steps": "unavailable",
                    "imd_year": None,
                    "window_quality": "unavailable",
                    "split": split_name,
                })
            continue

        imd_date_str = get_imd_date_label(init_dt, lead_day=lead_day)
        imd_year = int(imd_date_str.split("-")[0])

        if open_imd_datasets is not None and imd_year in open_imd_datasets:
            ds_imd = open_imd_datasets[imd_year]
        else:
            imd_path = imd_nc_dir / f"ind{imd_year}_rfp25.nc"
            if not imd_path.exists():
                raise FileNotFoundError(f"Missing required IMD file: {imd_path}")
            ds_imd = xr.open_dataset(imd_path)
            if open_imd_datasets is not None:
                open_imd_datasets[imd_year] = ds_imd

        lats_imd = ds_imd.LATITUDE.values
        lons_imd = ds_imd.LONGITUDE.values
        rf_daily = ds_imd.RAINFALL.sel(TIME=imd_date_str).values

        grib_2d = lead_totals[lead_day]
        s = 24 * lead_day - 21
        grib_steps = f"({s - 3}-{s + 3})-({s - 3}-{s})+({s + 3}-{s + 9})+({s + 9}-{s + 15})+({s + 15}-{s + 21})+({s + 21}-{s + 24})"

        for r in regions:
            imd_res = aggregate_imd_daily_region(r, lats_imd, lons_imd, rf_daily, minimum_coverage=0.80)
            cov_frac = imd_res["coverage_fraction"]

            top_lat_idx = round((90.0 - r.center_lat - 0.75) / 0.25)
            left_lon_idx = round((r.center_lon - 1.0) / 0.25)
            region_slice = grib_2d[top_lat_idx:top_lat_idx + 8, left_lon_idx:left_lon_idx + 8]
            f_mm = float(region_slice.mean())

            if imd_res["has_sufficient_coverage"]:
                o_mm = float(imd_res["o_imd_mm"])
                err_mm = abs(f_mm - o_mm)
            else:
                o_mm = None
                err_mm = None

            rows.append({
                "init_utc": init_str,
                "lead_day": lead_day,
                "valid_start_utc": valid_start_str,
                "valid_end_utc": valid_end_str,
                "region_id": r.region_id,
                "season": season,
                "lead_bucket": lead_bucket,
                "f_control_mm": f_mm,
                "o_imd_mm": o_mm,
                "coverage_fraction": cov_frac,
                "error_mm": err_mm,
                "threshold_mm": None,
                "bust": None,
                "source_key": source_key,
                "grib_steps": grib_steps,
                "imd_year": imd_year,
                "window_quality": "exact",
                "split": split_name,
            })

    return rows


def get_manifest_fingerprint(manifest_path: Path | str | None = None) -> str:
    """Derive a deterministic manifest fingerprint from actual selected decoded records in DATA_MANIFEST.csv."""
    root = Path(__file__).resolve().parents[3]
    if manifest_path is None:
        manifest_path = root / "DATA_MANIFEST.csv"
    else:
        manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        return "manifest-none"
    df = pd.read_csv(manifest_path, dtype=str).fillna("")
    decoded = df[df["status"] == "decoded"]
    if decoded.empty:
        return "manifest-empty"
    records = []
    for _, row in decoded.sort_values("manifest_id").iterrows():
        mid = row.get("manifest_id", "").strip()
        key = row.get("object_key_or_url", "").strip()
        h = row.get("sha256", "").strip() or row.get("bytes", "").strip()
        records.append(f"{mid}:{key}:{h}")
    combined = "|".join(records)
    fp = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]
    return f"manifest-fp-{fp}"


def build_dataset(
    raw_dir: Path | None = None,
    output_path: Path | None = None,
    manifest_path: Path | None = None,
    splits_path: Path | None = None,
    regions_path: Path | None = None,
    summary_path: Path | None = None,
    floor_mm: float = 10.0,
    chunk_days: int = 365,
) -> Path:
    """Build the real split-aware rows.parquet dataset.

    Enforces all preflight gates, leakage guards, train-only threshold fitting,
    and schema validation before writing to data/processed/rows.parquet.
    Uses bounded-memory chunked streaming with pyarrow.parquet.ParquetWriter.
    """
    root = Path(__file__).resolve().parents[3]
    if raw_dir is None:
        raw_dir = root / "data/raw"
    if output_path is None:
        output_path = root / "data/processed/rows.parquet"
    if manifest_path is None:
        manifest_path = root / "DATA_MANIFEST.csv"
    if splits_path is None:
        splits_path = root / "config/splits.yaml"
    if regions_path is None:
        regions_path = root / "config/regions_2deg.geojson"
    if summary_path is None:
        summary_path = root / "artifacts/metrics/dataset_summary.json"

    # Preflight coverage check
    report = check_corpus_coverage(raw_dir=raw_dir, manifest_path=manifest_path, splits_path=splits_path)
    if not report.is_complete:
        raise RuntimeError(
            f"Cannot build dataset: corpus incomplete: {report.blocking_reason}. "
            f"Next safe action: {report.next_safe_action}"
        )

    import pyarrow as pa
    import pyarrow.parquet as pq
    import xarray as xr

    from bust.data.regions import load_regions_geojson

    regions = load_regions_geojson(regions_path)
    splits_cfg = load_splits_config(splits_path)
    train_years = set(splits_cfg.get("train_years", []))

    gefs_dir = raw_dir / "gefs"
    imd_dir = raw_dir / "imd"

    open_imd_datasets: dict[int, xr.Dataset] = {}

    expected_dates_list = pd.date_range("2010-01-01", "2019-12-31", freq="D").strftime("%Y-%m-%d").tolist()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output_path = output_path.with_name(
        f"{output_path.stem}.tmp_{uuid.uuid4().hex[:8]}{output_path.suffix}"
    )

    try:
        # Pass 1: Bounded-memory accumulation of numeric errors per threshold key (region_id, season, lead_bucket)
        # Using array.array('d') to store raw 64-bit C doubles with NO python dict/row overhead
        key_errors: dict[tuple[str, str, str], array] = defaultdict(lambda: array("d"))

        for d_str in expected_dates_list:
            yr = int(d_str[:4])
            if yr not in train_years:
                continue

            init_dt = datetime.strptime(d_str, "%Y-%m-%d").replace(tzinfo=UTC)
            gefs_file, src_key = resolve_gefs_init_file(
                init_dt=init_dt,
                gefs_dir=gefs_dir,
                manifest_path=manifest_path,
            )

            date_rows = build_rows_for_date(
                init_dt=init_dt,
                gefs_file=gefs_file,
                imd_nc_dir=imd_dir,
                regions=regions,
                splits_cfg=splits_cfg,
                open_imd_datasets=open_imd_datasets,
                manifest_path=manifest_path,
                source_key=src_key,
            )
            for r in date_rows:
                if (
                    r["split"] == "train"
                    and r["lead_day"] < 10
                    and r["window_quality"] == "exact"
                    and r["error_mm"] is not None
                ):
                    k = (r["region_id"], r["season"], r["lead_bucket"])
                    key_errors[k].append(float(r["error_mm"]))

            # date_rows is released immediately; no dictionary list is accumulated globally!

        if not key_errors:
            raise ValueError("No eligible train rows found to compute thresholds")

        thresh_dict: dict[tuple[str, str, str], float] = {}
        for k, errs in key_errors.items():
            arr = np.array(errs, dtype=np.float64)
            q90 = float(np.quantile(arr, 0.90, method="linear"))
            thresh_dict[k] = max(q90, floor_mm)

        del key_errors  # Free the numeric error storage before Pass 2

        # Pass 2: Bounded-memory chunked generation and Parquet writing to temp_output_path
        parquet_schema = pa.schema([
            ("init_utc", pa.string()),
            ("lead_day", pa.int64()),
            ("valid_start_utc", pa.string()),
            ("valid_end_utc", pa.string()),
            ("region_id", pa.string()),
            ("season", pa.string()),
            ("lead_bucket", pa.string()),
            ("f_control_mm", pa.float64()),
            ("o_imd_mm", pa.float64()),
            ("coverage_fraction", pa.float64()),
            ("error_mm", pa.float64()),
            ("threshold_mm", pa.float64()),
            ("bust", pa.bool_()),
            ("source_key", pa.string()),
            ("grib_steps", pa.string()),
            ("imd_year", pa.int64()),
            ("window_quality", pa.string()),
            ("split", pa.string()),
        ])

        summary_counts: dict[str, Any] = {
            "total_rows": 0,
            "counts_by_init_year": {},
            "counts_by_lead": {},
            "counts_by_season": {},
            "counts_by_split": {},
            "counts_by_coverage_outcome": {
                "supported_sufficient_coverage": 0,
                "peripheral_insufficient_coverage": 0,
                "unavailable_window": 0,
            },
            "missingness": {
                "f_control_mm_null": 0,
                "o_imd_mm_null": 0,
                "error_mm_null": 0,
                "threshold_mm_null": 0,
                "bust_null": 0,
                "imd_year_null": 0,
            },
        }

        with pq.ParquetWriter(temp_output_path, parquet_schema) as writer:
            for chunk_start in range(0, len(expected_dates_list), chunk_days):
                chunk_dates = expected_dates_list[chunk_start:chunk_start + chunk_days]
                chunk_rows: list[dict[str, Any]] = []

                for d_str in chunk_dates:
                    init_dt = datetime.strptime(d_str, "%Y-%m-%d").replace(tzinfo=UTC)
                    gefs_file, src_key = resolve_gefs_init_file(
                        init_dt=init_dt,
                        gefs_dir=gefs_dir,
                        manifest_path=manifest_path,
                    )

                    date_rows = build_rows_for_date(
                        init_dt=init_dt,
                        gefs_file=gefs_file,
                        imd_nc_dir=imd_dir,
                        regions=regions,
                        splits_cfg=splits_cfg,
                        open_imd_datasets=open_imd_datasets,
                        manifest_path=manifest_path,
                        source_key=src_key,
                    )
                    chunk_rows.extend(date_rows)

                chunk_df = pd.DataFrame(chunk_rows)

                # Apply thresholds and bust labels
                for idx, row in chunk_df.iterrows():
                    ld = row["lead_day"]
                    wq = row["window_quality"]
                    if ld == 10 or wq == "unavailable":
                        chunk_df.at[idx, "threshold_mm"] = np.nan
                        chunk_df.at[idx, "bust"] = None
                        chunk_df.at[idx, "imd_year"] = np.nan
                    else:
                        key = (row["region_id"], row["season"], row["lead_bucket"])
                        thresh = thresh_dict.get(key, np.nan)
                        chunk_df.at[idx, "threshold_mm"] = thresh
                        err = row["error_mm"]
                        if pd.notna(err) and pd.notna(thresh):
                            chunk_df.at[idx, "bust"] = compute_bust(err, thresh)
                        else:
                            chunk_df.at[idx, "bust"] = None

                validate_row_schema(chunk_df)

                # Update summary statistics
                summary_counts["total_rows"] += len(chunk_df)
                for yr, count in chunk_df["init_utc"].str[:4].astype(int).value_counts().items():
                    summary_counts["counts_by_init_year"][int(yr)] = summary_counts["counts_by_init_year"].get(int(yr), 0) + int(count)
                for ld, count in chunk_df["lead_day"].value_counts().items():
                    summary_counts["counts_by_lead"][int(ld)] = summary_counts["counts_by_lead"].get(int(ld), 0) + int(count)
                for sn, count in chunk_df["season"].value_counts().items():
                    summary_counts["counts_by_season"][str(sn)] = summary_counts["counts_by_season"].get(str(sn), 0) + int(count)
                for sp, count in chunk_df["split"].value_counts().items():
                    summary_counts["counts_by_split"][str(sp)] = summary_counts["counts_by_split"].get(str(sp), 0) + int(count)

                summary_counts["counts_by_coverage_outcome"]["unavailable_window"] += int((chunk_df["lead_day"] == 10).sum())
                summary_counts["counts_by_coverage_outcome"]["supported_sufficient_coverage"] += int(
                    ((chunk_df["lead_day"] < 10) & (chunk_df["o_imd_mm"].notna())).sum()
                )
                summary_counts["counts_by_coverage_outcome"]["peripheral_insufficient_coverage"] += int(
                    ((chunk_df["lead_day"] < 10) & (chunk_df["o_imd_mm"].isna())).sum()
                )

                summary_counts["missingness"]["f_control_mm_null"] += int(chunk_df["f_control_mm"].isna().sum())
                summary_counts["missingness"]["o_imd_mm_null"] += int(chunk_df["o_imd_mm"].isna().sum())
                summary_counts["missingness"]["error_mm_null"] += int(chunk_df["error_mm"].isna().sum())
                summary_counts["missingness"]["threshold_mm_null"] += int(chunk_df["threshold_mm"].isna().sum())
                summary_counts["missingness"]["bust_null"] += int(chunk_df["bust"].isna().sum())
                summary_counts["missingness"]["imd_year_null"] += int(chunk_df["imd_year"].isna().sum())

                cols = [c.name for c in parquet_schema]
                table = pa.Table.from_pandas(chunk_df[cols], schema=parquet_schema, preserve_index=False)
                writer.write_table(table)
                del chunk_df
                del chunk_rows

        if summary_path:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary_counts, indent=2), encoding="utf-8")

        # Atomic rename only after every chunk, schema check, and summary write succeeds
        temp_output_path.replace(output_path)

        print("=== Dataset Build Summary ===")
        print(f"Total rows: {summary_counts['total_rows']}")
        print(f"By initialization year: {summary_counts['counts_by_init_year']}")
        print(f"By lead: {summary_counts['counts_by_lead']}")
        print(f"By season: {summary_counts['counts_by_season']}")
        print(f"By split: {summary_counts['counts_by_split']}")
        print(f"By coverage/no-data outcome: {summary_counts['counts_by_coverage_outcome']}")
        print(f"Missingness: {summary_counts['missingness']}")

        return output_path

    finally:
        # Clean up temporary output on failure
        if temp_output_path.exists():
            try:
                temp_output_path.unlink()
            except OSError:
                pass
        for ds in open_imd_datasets.values():
            ds.close()
