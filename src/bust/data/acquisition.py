"""Disk-safe, resumable GEFSv12 and IMD streaming acquisition and processing pipeline."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from array import array
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from bust.data.acquisition_state import (
    AcquisitionState,
    AcquisitionStatus,
    GEFSRecord,
    IMDRecord,
    iso_now,
)
from bust.data.dataset import (
    build_rows_for_date,
    load_splits_config,
    validate_row_schema,
)
from bust.data.labels import compute_bust
from bust.data.regions import load_regions_geojson

# Hard runtime limits frozen in project specification:
MAX_WORKING_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB (8,589,934,592 bytes)
MAX_RAW_BUFFER_COUNT = 150  # ~4.05 GiB buffer at ~28 MB per c00 file
DEFAULT_CONCURRENCY = 12
MAX_RETRIES = 3

NOAA_S3_BUCKET_URL = "https://noaa-gefs-retrospective.s3.amazonaws.com"
IMD_SELECTOR_URL = "https://imdpune.gov.in/cmpg/Griddata/RF25.php"


class DiskLimitExceededError(RuntimeError):
    """Raised when working directory or raw buffer limits are exceeded."""


@dataclass(frozen=True)
class InventoryReport:
    expected_dates_count: int
    actual_listed_count: int
    missing_dates: list[str]
    unexpected_dates: list[str]
    sample_keys: list[dict[str, Any]]
    projected_download_bytes: int
    working_dir_bytes: int
    can_honor_8gib: bool
    status: str


def derive_expected_gefs_dates(start_date: str = "2010-01-01", end_date: str = "2019-12-31") -> list[str]:
    """Generate exact list of expected 00 UTC initialization dates (3,652 dates for 2010-2019)."""
    return pd.date_range(start_date, end_date, freq="D").strftime("%Y-%m-%d").tolist()


def get_directory_size(path: Path | str) -> int:
    """Calculate recursive total byte size of all files in path."""
    p = Path(path)
    if not p.exists():
        return 0
    total = 0
    try:
        for entry in p.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def measure_pipeline_footprint(paths: list[Path | str]) -> int:
    """Measure total bytes across all configured pipeline paths without double-counting nested roots."""
    seen_files: set[Path] = set()
    total_bytes = 0
    for p_raw in paths:
        if not p_raw:
            continue
        p = Path(p_raw).resolve()
        if not p.exists():
            continue
        if p.is_file():
            if p not in seen_files:
                seen_files.add(p)
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass
        elif p.is_dir():
            try:
                for entry in p.rglob("*"):
                    if entry.is_file():
                        entry_res = entry.resolve()
                        if entry_res not in seen_files:
                            seen_files.add(entry_res)
                            try:
                                total_bytes += entry_res.stat().st_size
                            except OSError:
                                pass
            except OSError:
                pass
    return total_bytes


def get_configured_pipeline_roots(
    work_dir: Path | str,
    raw_gefs_dir: Path | str,
    shards_dir: Path | str,
    raw_imd_dir: Path | str,
    output_path: Path | str | None = None,
    summary_path: Path | str | None = None,
) -> list[Path]:
    """Return unique list of all configured pipeline roots and file targets."""
    roots: list[Path] = [
        Path(work_dir),
        Path(raw_gefs_dir),
        Path(shards_dir),
        Path(raw_imd_dir),
    ]
    if output_path:
        out_p = Path(output_path)
        roots.append(out_p)
        roots.append(out_p.parent)
    if summary_path:
        sum_p = Path(summary_path)
        roots.append(sum_p)
        roots.append(sum_p.parent)
    return roots


def count_raw_c00_files(raw_dir: Path | str) -> int:
    """Count fully downloaded raw c00 GRIB files (excluding .part files)."""
    p = Path(raw_dir)
    if not p.exists():
        return 0
    count = 0
    try:
        for f in p.glob("apcp_sfc_*00_c00.grib2"):
            if f.is_file() and not f.name.endswith(".part") and ".part_" not in f.name:
                count += 1
    except OSError:
        pass
    return count


def check_disk_limits(
    work_dir: Path | str | None = None,
    raw_dir: Path | str | None = None,
    pipeline_roots: list[Path | str] | None = None,
    measured_footprint_bytes: int | None = None,
    projected_file_bytes: int = 30_000_000,
    max_working_bytes: int = MAX_WORKING_BYTES,
    max_raw_files: int = MAX_RAW_BUFFER_COUNT,
) -> tuple[bool, str | None]:
    """Verify that both hard limits (8 GiB total footprint and 150-file raw buffer) are honored.

    Returns (is_allowed, pause_reason).
    """
    if raw_dir is not None:
        raw_count = count_raw_c00_files(raw_dir)
        if raw_count >= max_raw_files:
            return False, f"Raw buffer limit reached ({raw_count}/{max_raw_files} files; max 150 c00 buffer)"

    if measured_footprint_bytes is not None:
        current_working_bytes = measured_footprint_bytes
    elif pipeline_roots is not None:
        current_working_bytes = measure_pipeline_footprint(pipeline_roots)
    elif work_dir is not None:
        current_working_bytes = get_directory_size(work_dir)
    else:
        current_working_bytes = 0

    if current_working_bytes + projected_file_bytes > max_working_bytes:
        return False, (
            f"Working footprint limit would be exceeded ({current_working_bytes + projected_file_bytes} > "
            f"{max_working_bytes} bytes; max 8 GiB working cap)"
        )

    return True, None


def parse_s3_xml_for_c00(xml_content: bytes, expected_date_str: str) -> tuple[str, int, str | None] | None:
    """Parse NOAA S3 ListObjectsV2 XML response and extract observed c00 Days:1-10 APCP key, size, and ETag."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    yyyymmdd = expected_date_str.replace("-", "")
    canonical_filename = f"apcp_sfc_{yyyymmdd}00_c00.grib2"

    for contents in root.findall("s3:Contents", ns):
        key_elem = contents.find("s3:Key", ns)
        if key_elem is None or not key_elem.text:
            continue
        key = key_elem.text.strip()
        # Must be the .grib2 file, not the .idx sidecar:
        if not key.endswith(".grib2"):
            continue
        if Path(key).name != canonical_filename:
            continue
        if "c00" not in key or "Days:1-10" not in key:
            continue

        size_elem = contents.find("s3:Size", ns)
        size = int(size_elem.text) if size_elem is not None and size_elem.text else 0

        etag_elem = contents.find("s3:ETag", ns)
        etag = etag_elem.text.strip(' \t\n\r"') if etag_elem is not None and etag_elem.text else None

        return key, size, etag

    return None


def inventory_noaa_s3(
    state: AcquisitionState,
    expected_dates: list[str] | None = None,
    client: httpx.Client | None = None,
    max_workers: int = 24,
    progress_callback: Callable[[int, int], None] | None = None,
    shards_dir: Path | str | None = None,
    max_working_bytes: int = MAX_WORKING_BYTES,
) -> InventoryReport:
    """Query the real NOAA S3 retrospective archive to discover observed c00 Days:1-10 APCP keys.

    Strict rules:
    - Never guess object keys from templates.
    - Only keys observed in the real S3 REST response are recorded.
    - Persist each observed key, size, and ETag into the SQLite database.
    - Skip dates already listed with valid remote size to avoid redundant queries.
    """
    if expected_dates is None:
        expected_dates = derive_expected_gefs_dates()

    expected_set = set(expected_dates)
    total_expected = len(expected_dates)

    # 1. Check existing state database:
    existing_items = {item.init_date: item for item in state.list_gefs_items()}
    dates_to_query = [d for d in expected_dates if d not in existing_items or not existing_items[d].remote_bytes]

    if dates_to_query:
        close_client_at_end = False
        if client is None:
            client = httpx.Client(
                timeout=12.0,
                limits=httpx.Limits(max_connections=max_workers * 2, max_keepalive_connections=max_workers),
            )
            close_client_at_end = True

        def _fetch_date_inventory(d: str) -> tuple[str, tuple[str, int, str | None] | None]:
            y = d[:4]
            ymd = d.replace("-", "")
            prefix = f"GEFSv12/reforecast/{y}/{ymd}00/c00/Days:1-10/apcp_sfc"
            url = f"{NOAA_S3_BUCKET_URL}/?list-type=2&prefix={prefix}"
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    parsed = parse_s3_xml_for_c00(resp.content, d)
                    return d, parsed
                return d, None
            except Exception:  # noqa: BLE001
                return d, None

        completed_count = total_expected - len(dates_to_query)
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_fetch_date_inventory, d): d for d in dates_to_query}
                for fut in as_completed(futures):
                    d, result = fut.result()
                    if result is not None:
                        key, size, etag = result
                        state.upsert_gefs_inventory(
                            init_date=d,
                            source_key=key,
                            remote_bytes=size,
                            etag=etag,
                            member="c00",
                        )
                    completed_count += 1
                    if progress_callback:
                        progress_callback(completed_count, total_expected)
        finally:
            if close_client_at_end:
                client.close()

    # Re-read final inventory from state
    all_items = {item.init_date: item for item in state.list_gefs_items()}
    actual_listed = [item for item in all_items.values() if item.remote_bytes and item.remote_bytes > 0]
    listed_dates_set = {item.init_date for item in actual_listed}

    missing_dates = sorted(expected_set - listed_dates_set)
    unexpected_dates = sorted(listed_dates_set - expected_set)

    sample_keys = [
        {
            "init_date": item.init_date,
            "source_key": item.source_key,
            "remote_bytes": item.remote_bytes,
            "etag": item.etag,
        }
        for item in actual_listed[:5]
    ]

    total_projected_bytes = sum(item.remote_bytes or 0 for item in actual_listed)
    work_bytes = get_directory_size(state.db_path.parent)

    # Real measured/reserved calculation for can_honor_8gib:
    can_honor_8gib = False
    if shards_dir is not None:
        p_shards = Path(shards_dir)
        existing_shards = list(p_shards.glob("shard_*.parquet")) if p_shards.exists() else []
        if existing_shards:
            avg_shard_bytes = sum(s.stat().st_size for s in existing_shards) / len(existing_shards)
            projected_shards_total = int(avg_shard_bytes * total_expected)
            max_raw_buffer_bytes = MAX_RAW_BUFFER_COUNT * 30_000_000
            if work_bytes + max_raw_buffer_bytes + projected_shards_total <= max_working_bytes:
                can_honor_8gib = True

    status = "complete" if len(missing_dates) == 0 and len(unexpected_dates) == 0 else "incomplete"

    return InventoryReport(
        expected_dates_count=total_expected,
        actual_listed_count=len(actual_listed),
        missing_dates=missing_dates,
        unexpected_dates=unexpected_dates,
        sample_keys=sample_keys,
        projected_download_bytes=total_projected_bytes,
        working_dir_bytes=work_bytes,
        can_honor_8gib=can_honor_8gib,
        status=status,
    )


def compute_sha256(path: Path | str) -> str:
    """Calculate SHA-256 hex digest of a local file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def validate_imd_netcdf(nc_path: Path | str, expected_year: int) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Validate that nc_path is a genuine, non-corrupted IMD daily rainfall annual NetCDF file."""
    p = Path(nc_path)
    if not p.exists() or p.stat().st_size == 0:
        return False, None, f"File missing or empty: {p}"

    # Disallow HTML or error response bodies
    with open(p, "rb") as f:
        head = f.read(1024)
    stripped = head.lstrip()
    head_lower = head.lower()
    if (
        stripped.startswith(b"<")
        or b"<html" in head_lower
        or b"<!doctype" in head_lower
        or b"<body" in head_lower
        or b"<head" in head_lower
        or b"<title>" in head_lower
    ):
        return False, None, "File content is an HTML/error response, not a NetCDF file"

    try:
        import xarray as xr
        with xr.open_dataset(p) as ds:
            if "RAINFALL" not in ds.variables:
                return False, None, f"Missing 'RAINFALL' variable in {p.name}"

            expected_dates = pd.date_range(f"{expected_year}-01-01", f"{expected_year}-12-31", freq="D").strftime("%Y-%m-%d").tolist()
            expected_days = len(expected_dates)

            t_dim = int(ds.sizes.get("TIME", 0))
            lat_dim = int(ds.sizes.get("LATITUDE", 0))
            lon_dim = int(ds.sizes.get("LONGITUDE", 0))

            if t_dim != expected_days or lat_dim != 129 or lon_dim != 135:
                return False, None, (
                    f"Unexpected dimensions in {p.name}: TIME={t_dim} (expected {expected_days}), "
                    f"LATITUDE={lat_dim} (expected 129), LONGITUDE={lon_dim} (expected 135)"
                )

            rainfall_attrs = ds["RAINFALL"].attrs
            if "units" not in rainfall_attrs:
                return False, None, f"Missing 'units' attribute on RAINFALL in {p.name}"

            units = str(rainfall_attrs["units"]).strip()
            if units != "mm":
                return False, None, f"Invalid units '{units}' on RAINFALL in {p.name}; expected 'mm'"

            time_coord = ds.coords.get("TIME")
            if time_coord is None:
                return False, None, f"TIME coordinate missing in {p.name}"

            time_values = pd.to_datetime(time_coord.values)
            actual_dates = time_values.strftime("%Y-%m-%d").tolist()
            if actual_dates != expected_dates:
                mismatches = [
                    f"idx {i}: expected {exp} != actual {act}"
                    for i, (exp, act) in enumerate(zip(expected_dates, actual_dates))
                    if exp != act
                ]
                diag = f"mismatches: {mismatches[:3]}" if mismatches else f"length mismatch: actual {len(actual_dates)} != expected {len(expected_dates)}"
                return False, None, f"TIME coordinate does not match exact calendar dates for {expected_year} in {p.name}: {diag}"

            t0 = str(time_coord.values[0])
            t_end = str(time_coord.values[-1])

            metadata = {
                "year": expected_year,
                "time_dim": t_dim,
                "lat_dim": lat_dim,
                "lon_dim": lon_dim,
                "units": units,
                "time_start": t0,
                "time_end": t_end,
                "variable": "RAINFALL",
            }
            return True, metadata, None
    except Exception as e:  # noqa: BLE001
        return False, None, f"NetCDF decoding failed for {p.name}: {e}"


def get_manifest_imd_checksum(manifest_path: Path | str | None, year: int) -> str | None:
    """Return recorded SHA-256 for IMD year from DATA_MANIFEST.csv if present and decoded."""
    if not manifest_path:
        return None
    p = Path(manifest_path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("source", "").strip() != "imd" or row.get("status", "").strip() != "decoded":
                    continue
                url = row.get("object_key_or_url", "")
                notes = row.get("notes", "")
                m_id = row.get("manifest_id", "")
                match_url = re.search(r"RF25=(\d{4})", url)
                match_notes = re.search(r"ind(\d{4})_rfp25\.nc", notes)
                match_id = re.search(r"imd-(\d{4})", m_id)

                row_yr = None
                if match_url:
                    row_yr = int(match_url.group(1))
                elif match_notes:
                    row_yr = int(match_notes.group(1))
                elif match_id:
                    row_yr = int(match_id.group(1))

                if row_yr == year:
                    sha = row.get("sha256", "").strip().lower()
                    if sha:
                        return sha
    except (OSError, csv.Error):
        return None
    return None


def append_imd_manifest_record(
    manifest_path: Path | str | None,
    year: int,
    sha256: str,
    byte_count: int,
    metadata: dict[str, Any],
    retrieved_utc: str | None = None,
    content_disposition: str | None = None,
) -> None:
    """Safely append or update an IMD annual record in DATA_MANIFEST.csv without unproven claims."""
    if not manifest_path:
        return
    p = Path(manifest_path)
    if not p.exists():
        return

    t0_date = str(metadata.get("time_start", f"{year}-01-01"))[:10]
    tend_date = str(metadata.get("time_end", f"{year}-12-31"))[:10]
    t_dim = metadata.get("time_dim", 366 if pd.Timestamp(f"{year}-01-01").is_leap_year else 365)
    lat_dim = metadata.get("lat_dim", 129)
    lon_dim = metadata.get("lon_dim", 135)
    units = metadata.get("units", "mm")

    verification_suffix = ""
    if year == 2020:
        verification_suffix = "; verification-only for late-2019 Day 1–9 labels per D-007; no 2020 GEFS initializations"

    cd_prefix = f"Content-Disposition {content_disposition}; " if content_disposition else ""

    notes_text = (
        f"{cd_prefix}Official selector POST route https://imdpune.gov.in/cmpg/Griddata/RF25.php (POST RF25={year}); "
        f"canonical local file data/raw/imd/ind{year}_rfp25.nc; "
        f"NetCDF: TIME={t_dim} ({t0_date} to {tend_date}), LATITUDE={lat_dim}, LONGITUDE={lon_dim}; "
        f"RAINFALL units={units}{verification_suffix}"
    )

    with open(p, "r", encoding="utf-8", newline="") as f:
        reader = list(csv.reader(f))

    if not reader:
        return

    found_idx = None
    for idx, row in enumerate(reader[1:], start=1):
        if len(row) > 1 and row[1].strip() == "imd":
            url = row[3] if len(row) > 3 else ""
            notes = row[14] if len(row) > 14 else ""
            m_id = row[0] if len(row) > 0 else ""
            match_url = re.search(r"RF25=(\d{4})", url)
            match_notes = re.search(r"ind(\d{4})_rfp25\.nc", notes)
            match_id = re.search(r"imd-(\d{4})", m_id)

            row_yr = None
            if match_url:
                row_yr = int(match_url.group(1))
            elif match_notes:
                row_yr = int(match_notes.group(1))
            elif match_id:
                row_yr = int(match_id.group(1))

            if row_yr == year:
                found_idx = idx
                break

    if found_idx is not None:
        row = reader[found_idx]
        while len(row) < 15:
            row.append("")
        row[1] = "imd"
        row[2] = "IMD Pune"
        row[3] = f"https://imdpune.gov.in/cmpg/Griddata/RF25.php (POST RF25={year})"
        if not row[4].strip():
            row[4] = retrieved_utc or iso_now()
        if not row[5].strip():
            row[5] = sha256
        if not row[6].strip():
            row[6] = str(byte_count)
        row[8] = "RAINFALL"
        row[12] = units
        row[13] = "decoded"
        row[14] = notes_text
        reader[found_idx] = row
    else:
        retrieved = retrieved_utc or iso_now()
        new_row = [
            f"imd-{year}",
            "imd",
            "IMD Pune",
            f"https://imdpune.gov.in/cmpg/Griddata/RF25.php (POST RF25={year})",
            retrieved,
            sha256,
            str(byte_count),
            "",
            "RAINFALL",
            "",
            "",
            "",
            units,
            "decoded",
            notes_text,
        ]
        reader.append(new_row)

    temp_path = p.with_name(f"{p.name}.tmp_{uuid.uuid4().hex[:8]}")
    with open(temp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(reader)
    temp_path.replace(p)


def verify_or_fetch_imd_years(
    state: AcquisitionState,
    imd_dir: Path | str,
    manifest_path: Path | str | None = None,
    required_years: list[int] | None = None,
    allow_network_download: bool = False,
    timeout_seconds: float = 180.0,
    max_retries: int = MAX_RETRIES,
    client: httpx.Client | None = None,
) -> tuple[bool, list[str]]:
    """Upfront validation and acquisition of IMD annual files for 2010–2020.

    Requirements:
    - Verifies 2010–2020 annual files (including verification-only 2020).
    - Checks file exists, size > 0, compares against DATA_MANIFEST.csv checksum if present.
    - Decodes with xarray and validates RAINFALL variable, dimensions, units, and date axis.
    - If missing or invalid and allow_network_download is True:
      - Downloads only to a uniquely named .part file.
      - Validates .part file before renaming.
      - Disallows HTML/error responses.
      - Atomically renames .part to canonical file.
      - Appends verified record to DATA_MANIFEST.csv with computed SHA-256.
      - If download or validation fails: deletes .part, preserves any existing canonical file,
        records failure in SQLite, and leaves year unverified.
    """
    if required_years is None:
        required_years = list(range(2010, 2021))

    p_imd = Path(imd_dir)
    p_imd.mkdir(parents=True, exist_ok=True)
    manifest_p = Path(manifest_path) if manifest_path else None
    if manifest_p is None:
        default_m = Path(__file__).resolve().parents[3] / "DATA_MANIFEST.csv"
        if default_m.exists():
            manifest_p = default_m

    blocking_errors: list[str] = []

    for year in required_years:
        canonical_file = p_imd / f"ind{year}_rfp25.nc"

        # 1. Check existing canonical file if present
        if canonical_file.exists() and canonical_file.stat().st_size > 0:
            manifest_sha = get_manifest_imd_checksum(manifest_p, year)
            local_sha = compute_sha256(canonical_file)
            size = canonical_file.stat().st_size

            if manifest_sha is not None and local_sha != manifest_sha:
                err_msg = (
                    f"Local IMD annual file for year {year} SHA-256 mismatch against manifest: "
                    f"expected {manifest_sha}, got {local_sha}"
                )
                state.upsert_imd_record(
                    IMDRecord(
                        year=year,
                        status="failed",
                        local_path=str(canonical_file),
                        local_bytes=size,
                        local_sha256=local_sha,
                        error=err_msg,
                    )
                )
                blocking_errors.append(err_msg)
                if not allow_network_download:
                    continue
            else:
                valid, meta, val_err = validate_imd_netcdf(canonical_file, year)
                if valid and meta:
                    if manifest_sha is None and manifest_p and allow_network_download:
                        mtime_str = datetime.fromtimestamp(canonical_file.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                        append_imd_manifest_record(manifest_p, year, local_sha, size, meta, retrieved_utc=mtime_str)

                    state.upsert_imd_record(
                        IMDRecord(
                            year=year,
                            status="verified",
                            local_path=str(canonical_file),
                            local_bytes=size,
                            local_sha256=local_sha,
                            time_dim=meta["time_dim"],
                            lat_dim=meta["lat_dim"],
                            lon_dim=meta["lon_dim"],
                            retrieved_at=datetime.fromtimestamp(canonical_file.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            error=None,
                        )
                    )
                    continue
                else:
                    err_msg = f"Existing IMD annual file for year {year} failed validation: {val_err}"
                    state.upsert_imd_record(
                        IMDRecord(
                            year=year,
                            status="failed",
                            local_path=str(canonical_file),
                            local_bytes=size,
                            local_sha256=local_sha,
                            error=err_msg,
                        )
                    )
                    blocking_errors.append(err_msg)
                    if not allow_network_download:
                        continue

        # 2. File missing or invalid: if network download disallowed, record missing and block
        if not allow_network_download:
            err_msg = f"Missing required IMD annual NetCDF file for year {year}: {canonical_file}"
            state.upsert_imd_record(
                IMDRecord(
                    year=year,
                    status="missing",
                    local_path=str(canonical_file),
                    error=err_msg,
                )
            )
            blocking_errors.append(err_msg)
            continue

        # 3. Network download allowed: download to uniquely named .part, validate, then rename
        part_path = canonical_file.with_name(f"{canonical_file.name}.part_{uuid.uuid4().hex[:8]}")
        try:
            def _fetch_content(c: httpx.Client, y: int = year) -> bytes:
                resp = c.post(IMD_SELECTOR_URL, data={"RF25": str(y)})
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} from IMD selector: {resp.text[:200]}")
                return resp.content

            if client is not None:
                content = _fetch_content(client)
            else:
                last_exc = None
                content = None
                for attempt in range(max_retries):
                    try:
                        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as c:
                            content = _fetch_content(c)
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        if attempt + 1 < max_retries:
                            time.sleep(1.0 * (attempt + 1))
                if content is None:
                    raise last_exc or RuntimeError(f"Failed retrieving IMD year {year} after {max_retries} attempts")

            part_path.write_bytes(content)

            valid, meta, val_err = validate_imd_netcdf(part_path, year)
            if not valid or not meta:
                raise ValueError(f"Downloaded content validation failed: {val_err}")

            local_sha = compute_sha256(part_path)
            local_bytes = part_path.stat().st_size

            manifest_sha = get_manifest_imd_checksum(manifest_p, year)
            if manifest_sha is not None and local_sha != manifest_sha:
                raise ValueError(
                    f"Downloaded file SHA-256 mismatch against manifest: expected {manifest_sha}, got {local_sha}"
                )

            # Atomic rename to canonical filename
            part_path.replace(canonical_file)
            now_str = iso_now()

            if manifest_sha is None and manifest_p:
                append_imd_manifest_record(manifest_p, year, local_sha, local_bytes, meta, retrieved_utc=now_str)

            state.upsert_imd_record(
                IMDRecord(
                    year=year,
                    status="verified",
                    local_path=str(canonical_file),
                    local_bytes=local_bytes,
                    local_sha256=local_sha,
                    time_dim=meta["time_dim"],
                    lat_dim=meta["lat_dim"],
                    lon_dim=meta["lon_dim"],
                    retrieved_at=now_str,
                    error=None,
                )
            )

        except Exception as fetch_err:  # noqa: BLE001
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            err_msg = f"Failed acquiring/verifying IMD year {year}: {fetch_err}"
            state.upsert_imd_record(
                IMDRecord(
                    year=year,
                    status="failed",
                    local_path=str(canonical_file),
                    local_bytes=canonical_file.stat().st_size if canonical_file.exists() else None,
                    error=str(fetch_err),
                )
            )
            blocking_errors.append(err_msg)

    is_complete = len(blocking_errors) == 0
    return is_complete, blocking_errors


def download_gefs_c00_file(
    item: GEFSRecord,
    raw_dir: Path | str,
    work_dir: Path | str,
    client: httpx.Client | None = None,
    state: AcquisitionState | None = None,
    max_retries: int = MAX_RETRIES,
) -> Path:
    """Download a single GEFS c00 file observing disk limits, atomic .part write, and checksum verification."""
    raw_p = Path(raw_dir)
    raw_p.mkdir(parents=True, exist_ok=True)
    work_p = Path(work_dir)

    yyyymmdd = item.init_date.replace("-", "")
    canonical_filename = f"apcp_sfc_{yyyymmdd}00_c00.grib2"
    target_path = raw_p / canonical_filename

    # If canonical file already exists and is verified, reuse it
    if target_path.exists() and item.remote_bytes and target_path.stat().st_size == item.remote_bytes:
        if state:
            h = hashlib.sha256()
            with open(target_path, "rb") as f:
                while c := f.read(65536):
                    h.update(c)
            state.record_gefs_download_success(
                item.init_date,
                local_path=target_path,
                local_bytes=target_path.stat().st_size,
                local_sha256=h.hexdigest(),
            )
        return target_path

    # Check hard disk limits before downloading:
    projected = item.remote_bytes or 30_000_000
    allowed, pause_reason = check_disk_limits(work_dir=work_p, raw_dir=raw_p, projected_file_bytes=projected)
    if not allowed:
        raise DiskLimitExceededError(pause_reason)

    if state:
        state.update_gefs_status(
            item.init_date,
            status=AcquisitionStatus.DOWNLOADING,
        )

    part_path = raw_p / f"{canonical_filename}.part_{uuid.uuid4().hex[:8]}"
    url = f"{NOAA_S3_BUCKET_URL}/{item.source_key}"

    if state:
        state.update_gefs_status(
            item.init_date,
            status=AcquisitionStatus.DOWNLOADING,
            part_path=str(part_path),
        )

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        close_client = True

    try:
        sha256_hash = hashlib.sha256()
        md5_hash = hashlib.md5()
        total_downloaded = 0

        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code} retrieving {url}")

            with open(part_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    sha256_hash.update(chunk)
                    md5_hash.update(chunk)
                    total_downloaded += len(chunk)

        # Integrity validations
        if item.remote_bytes and total_downloaded != item.remote_bytes:
            raise ValueError(
                f"Byte size mismatch for {canonical_filename}: expected {item.remote_bytes}, got {total_downloaded}"
            )

        if item.etag and "-" not in item.etag:  # S3 standard single-part ETag is hex MD5
            computed_md5 = md5_hash.hexdigest().lower()
            expected_etag = item.etag.lower()
            if computed_md5 != expected_etag:
                raise ValueError(
                    f"ETag MD5 mismatch for {canonical_filename}: expected {expected_etag}, got {computed_md5}"
                )

        # Atomic rename only after full verification:
        part_path.replace(target_path)

        local_sha256 = sha256_hash.hexdigest().lower()
        if state:
            state.record_gefs_download_success(
                item.init_date,
                local_path=target_path,
                local_bytes=total_downloaded,
                local_sha256=local_sha256,
            )

        return target_path

    except Exception as err:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        if state:
            state.update_gefs_status(item.init_date, status=AcquisitionStatus.LISTED, part_path=None)
            state.record_gefs_failure(item.init_date, str(err), max_retries=max_retries)
        raise
    finally:
        if close_client:
            client.close()


def validate_shard_for_recovery(
    shard_path: Path | str,
    expected_init_date: str,
    regions_path: Path | str | None = None,
    expected_rows: int | None = None,
    regions_count: int | None = None,
) -> tuple[bool, str | None]:
    """Validate a candidate Parquet shard before deleting the retained raw GRIB file during recovery.

    Checks:
    - File exists and is non-empty.
    - Can be read with pandas/pyarrow.
    - Canonical region configuration must be available to determine expected row count.
    - Exact expected row count (number of canonical regions * 10 lead days).
    - Conforms to shared row schema via validate_row_schema.
    - All rows have init_utc matching expected_init_date.
    """
    p = Path(shard_path)
    if not p.exists() or p.stat().st_size == 0:
        return False, f"Shard file missing or empty: {shard_path}"

    if expected_rows is None:
        if regions_count is not None:
            expected_rows = regions_count * 10
        elif regions_path is not None and Path(regions_path).exists():
            regs = load_regions_geojson(regions_path)
            expected_rows = len(regs) * 10
        else:
            canonical_reg = Path(__file__).resolve().parents[3] / "config" / "regions_2deg.geojson"
            if canonical_reg.exists():
                regs = load_regions_geojson(canonical_reg)
                expected_rows = len(regs) * 10
            else:
                return False, "Canonical region configuration is unavailable; recovery blocked to protect raw data"

    try:
        df = pd.read_parquet(p)
    except Exception as e:  # noqa: BLE001
        return False, f"Failed to read Parquet shard: {e}"

    if len(df) != expected_rows:
        return False, f"Shard row count mismatch: expected {expected_rows}, got {len(df)}"

    try:
        validate_row_schema(df)
    except Exception as e:  # noqa: BLE001
        return False, f"Shard schema validation failed: {e}"

    if "init_utc" not in df.columns:
        return False, "Shard missing init_utc column"

    invalid_inits = df[~df["init_utc"].str.startswith(expected_init_date)]
    if not invalid_inits.empty:
        return False, f"Shard contains rows not matching expected init {expected_init_date}"

    return True, None


def process_gefs_c00_file(
    item: GEFSRecord,
    shards_dir: Path | str,
    imd_nc_dir: Path | str,
    regions: list[Any],
    splits_cfg: dict[str, Any] | None = None,
    open_imd_datasets: dict[int, Any] | None = None,
    state: AcquisitionState | None = None,
    max_retries: int = MAX_RETRIES,
    pipeline_roots: list[Path | str] | None = None,
    max_working_bytes: int = MAX_WORKING_BYTES,
    regions_path: Path | str | None = None,
) -> Path:
    """Process a verified raw GEFS c00 file: generate row shard, validate schema, atomically write shard, and delete raw file."""
    shards_p = Path(shards_dir)
    shards_p.mkdir(parents=True, exist_ok=True)
    imd_p = Path(imd_nc_dir)

    raw_path = Path(item.local_path) if item.local_path else None
    if not raw_path or not raw_path.exists():
        err_msg = f"Raw GEFS file not found for {item.init_date}: {raw_path}"
        if state:
            state.record_gefs_processing_failure(item.init_date, err_msg, max_retries=max_retries)
        raise FileNotFoundError(err_msg)

    # Re-validate raw file integrity before opening/decoding GRIB
    actual_size = raw_path.stat().st_size
    if item.local_bytes is not None and actual_size != item.local_bytes:
        err_msg = (
            f"Raw file size mismatch before decode for {item.init_date}: "
            f"expected {item.local_bytes} bytes, found {actual_size} bytes"
        )
        if state:
            state.record_gefs_processing_failure(item.init_date, err_msg, max_retries=max_retries)
        raise ValueError(err_msg)

    if item.local_sha256 is not None:
        h = hashlib.sha256()
        with open(raw_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        actual_sha256 = h.hexdigest().lower()
        if actual_sha256 != item.local_sha256.lower():
            err_msg = (
                f"Raw file SHA-256 mismatch before decode for {item.init_date}: "
                f"expected {item.local_sha256.lower()}, found {actual_sha256}"
            )
            if state:
                state.record_gefs_processing_failure(item.init_date, err_msg, max_retries=max_retries)
            raise ValueError(err_msg)

    if pipeline_roots is None:
        work_p = shards_p.parent
        pipeline_roots = get_configured_pipeline_roots(
            work_dir=work_p,
            raw_gefs_dir=raw_path.parent,
            shards_dir=shards_p,
            raw_imd_dir=imd_p,
        )

    if state:
        state.update_gefs_status(item.init_date, status=AcquisitionStatus.PROCESSING)

    init_dt = datetime.strptime(item.init_date, "%Y-%m-%d").replace(tzinfo=UTC)
    yyyymmdd = item.init_date.replace("-", "")
    canonical_shard_path = shards_p / f"shard_{yyyymmdd}.parquet"
    temp_shard_path = shards_p / f"shard_{yyyymmdd}.tmp_{uuid.uuid4().hex[:8]}.parquet"

    try:
        # Build shared-schema rows using verified logic:
        rows = build_rows_for_date(
            init_dt=init_dt,
            gefs_file=raw_path,
            imd_nc_dir=imd_p,
            regions=regions,
            splits_cfg=splits_cfg,
            open_imd_datasets=open_imd_datasets,
            source_key=item.source_key,
        )

        df = pd.DataFrame(rows)
        validate_row_schema(df)

        # In-memory serialization to determine exact candidate shard bytes before writing to disk
        import io
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        candidate_shard_bytes = buf.tell()
        shard_buffer = buf.getvalue()

        # Check 8 GiB cap across all configured pipeline roots before placing shard on disk
        current_footprint = measure_pipeline_footprint(pipeline_roots)
        if current_footprint + candidate_shard_bytes > max_working_bytes:
            pause_reason = (
                f"Working footprint limit would be exceeded to write shard for {item.init_date}: "
                f"{current_footprint + candidate_shard_bytes} > {max_working_bytes} bytes; max 8 GiB working cap"
            )
            if state:
                state.update_gefs_status(
                    item.init_date,
                    status=AcquisitionStatus.VERIFIED,
                    last_error=f"Paused by disk footprint limit: {pause_reason}",
                )
            raise DiskLimitExceededError(pause_reason)

        # Write Parquet shard atomically using measured in-memory bytes
        temp_shard_path.write_bytes(shard_buffer)
        temp_shard_path.replace(canonical_shard_path)

        # Readback validation of the written shard on disk before setting deletion_pending
        shard_valid, validation_err = validate_shard_for_recovery(
            shard_path=canonical_shard_path,
            expected_init_date=item.init_date,
            regions_path=regions_path,
            regions_count=len(regions),
        )
        if not shard_valid:
            if canonical_shard_path.exists():
                try:
                    canonical_shard_path.unlink()
                except OSError:
                    pass
            raise ValueError(f"Post-write shard readback validation failed: {validation_err}")

        if state:
            state.record_gefs_shard_success(
                item.init_date,
                shard_path=canonical_shard_path,
                shard_rows=len(df),
            )

        # Only after shard is verified and durably stored: delete raw file to free disk space!
        try:
            raw_path.unlink()
        except OSError as e:
            raise RuntimeError(f"Failed to delete raw file after shard generation: {e}") from e

        if state:
            state.record_gefs_completed(item.init_date)

        return canonical_shard_path

    except DiskLimitExceededError:
        if temp_shard_path.exists():
            try:
                temp_shard_path.unlink()
            except OSError:
                pass
        raise
    except Exception as err:
        if temp_shard_path.exists():
            try:
                temp_shard_path.unlink()
            except OSError:
                pass
        if state:
            state.record_gefs_processing_failure(item.init_date, str(err), max_retries=max_retries)
        raise


def finalize_streaming_dataset(
    state: AcquisitionState,
    shards_dir: Path | str,
    output_path: Path | str,
    summary_path: Path | str,
    splits_cfg: dict[str, Any] | None = None,
    replace_final: bool = False,
    floor_mm: float = 10.0,
    pipeline_roots: list[Path | str] | None = None,
    max_working_bytes: int = MAX_WORKING_BYTES,
) -> Path:
    """Finalize the dataset from interim row shards: fit train-only thresholds, apply labels, and publish rows.parquet atomically."""
    out_p = Path(output_path)
    sum_p = Path(summary_path)
    shards_p = Path(shards_dir)

    if out_p.exists() and not replace_final:
        raise FileExistsError(
            f"Final output dataset already exists at {out_p}. Refusing to overwrite without --replace-final flag."
        )

    if pipeline_roots is None:
        work_p = shards_p.parent
        pipeline_roots = get_configured_pipeline_roots(
            work_dir=work_p,
            raw_gefs_dir=work_p / "raw",
            shards_dir=shards_p,
            raw_imd_dir=shards_p.parents[1] / "raw/imd",
            output_path=out_p,
            summary_path=sum_p,
        )

    # Pre-finalization budget check across all configured roots
    current_footprint = measure_pipeline_footprint(pipeline_roots)
    if current_footprint > max_working_bytes:
        raise DiskLimitExceededError(
            f"Working footprint limit exceeded before finalization: "
            f"{current_footprint} > {max_working_bytes} bytes; max 8 GiB working cap"
        )

    if splits_cfg is None:
        splits_cfg = load_splits_config()

    train_years = set(splits_cfg.get("train_years", [2010, 2011, 2012, 2013, 2014, 2015]))
    expected_dates = derive_expected_gefs_dates()

    # Preflight completeness check against durable SQLite state
    all_items = {item.init_date: item for item in state.list_gefs_items()}
    missing_dates = [d for d in expected_dates if d not in all_items or all_items[d].status != AcquisitionStatus.COMPLETED]
    if missing_dates:
        raise RuntimeError(
            f"Cannot finalize dataset: {len(missing_dates)} of {len(expected_dates)} dates are incomplete or missing. "
            f"Sample incomplete: {missing_dates[:5]}"
        )

    failed_items = state.list_gefs_items(status=AcquisitionStatus.FAILED)
    if failed_items:
        raise RuntimeError(
            f"Cannot finalize dataset: {len(failed_items)} items have failed status. "
            f"Sample failed: {[f.init_date for f in failed_items[:5]]}"
        )

    # Reject unexpected GEFS dates or 2020 GEFS initializations
    unexpected = [d for d in all_items if d not in set(expected_dates)]
    if unexpected:
        raise ValueError(f"Found unexpected GEFS initialization dates in state database: {unexpected[:5]}")

    # Pass 1: Bounded-memory accumulation of numeric errors per threshold key (region_id, season, lead_bucket)
    key_errors: dict[tuple[str, str, str], array] = defaultdict(lambda: array("d"))

    for d_str in expected_dates:
        yr = int(d_str[:4])
        if yr not in train_years:
            continue
        yyyymmdd = d_str.replace("-", "")
        shard_path = shards_p / f"shard_{yyyymmdd}.parquet"
        if not shard_path.exists():
            raise FileNotFoundError(f"Missing required row shard: {shard_path}")

        shard_df = pd.read_parquet(shard_path)
        for _, r in shard_df.iterrows():
            if (
                r["split"] == "train"
                and r["lead_day"] < 10
                and r["window_quality"] == "exact"
                and pd.notna(r["error_mm"])
            ):
                k = (str(r["region_id"]), str(r["season"]), str(r["lead_bucket"]))
                key_errors[k].append(float(r["error_mm"]))

    if not key_errors:
        raise ValueError("No eligible train rows found to compute thresholds")

    thresh_dict: dict[tuple[str, str, str], float] = {}
    for k, errs in key_errors.items():
        arr = np.array(errs, dtype=np.float64)
        q90 = float(np.quantile(arr, 0.90, method="linear"))
        thresh_dict[k] = max(q90, floor_mm)

    del key_errors

    # Pass 2: Stream all shards, apply thresholds and bust labels, and write to temporary Parquet path
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

    out_p.parent.mkdir(parents=True, exist_ok=True)
    temp_output_path = out_p.with_name(f"{out_p.stem}.tmp_{uuid.uuid4().hex[:8]}{out_p.suffix}")
    temp_summary_path = sum_p.with_name(f"{sum_p.stem}.tmp_{uuid.uuid4().hex[:8]}{sum_p.suffix}")

    # Track active temporary targets as part of the measured footprint
    active_roots = list(pipeline_roots) + [temp_output_path, temp_summary_path]

    try:
        with pq.ParquetWriter(temp_output_path, parquet_schema, compression="snappy") as writer:
            for d_str in expected_dates:
                yyyymmdd = d_str.replace("-", "")
                shard_path = shards_p / f"shard_{yyyymmdd}.parquet"
                chunk_df = pd.read_parquet(shard_path)

                for idx, row in chunk_df.iterrows():
                    ld = row["lead_day"]
                    wq = row["window_quality"]
                    if ld == 10 or wq == "unavailable":
                        chunk_df.at[idx, "threshold_mm"] = np.nan
                        chunk_df.at[idx, "bust"] = None
                        chunk_df.at[idx, "imd_year"] = np.nan
                    else:
                        key = (str(row["region_id"]), str(row["season"]), str(row["lead_bucket"]))
                        thresh = thresh_dict.get(key, np.nan)
                        chunk_df.at[idx, "threshold_mm"] = thresh
                        err = row["error_mm"]
                        if pd.notna(err) and pd.notna(thresh):
                            chunk_df.at[idx, "bust"] = compute_bust(err, thresh)
                        else:
                            chunk_df.at[idx, "bust"] = None

                validate_row_schema(chunk_df)

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

                # Serialize exact table to an in-memory standalone Parquet buffer using the same
                # compression/options as the final writer. Because a standalone Parquet file
                # includes its own file metadata and footer, its actual byte length serves as a
                # conservative upper-bound reservation for the appended row group.
                chunk_buf = io.BytesIO()
                pq.write_table(table, chunk_buf, compression="snappy")
                projected_chunk_bytes = chunk_buf.tell()

                current_bytes = measure_pipeline_footprint(active_roots)
                if current_bytes + projected_chunk_bytes > max_working_bytes:
                    raise DiskLimitExceededError(
                        f"Working footprint limit would be exceeded during finalization on {d_str}: "
                        f"{current_bytes + projected_chunk_bytes} > {max_working_bytes} bytes; max 8 GiB working cap"
                    )
                writer.write_table(table)

        # Write summary JSON with budget check
        sum_p.parent.mkdir(parents=True, exist_ok=True)
        summary_text = json.dumps(summary_counts, indent=2)
        summary_bytes = len(summary_text.encode("utf-8"))
        current_bytes = measure_pipeline_footprint(active_roots)
        if current_bytes + summary_bytes > max_working_bytes:
            raise DiskLimitExceededError(
                f"Working footprint limit would be exceeded writing dataset summary during finalization: "
                f"{current_bytes + summary_bytes} > {max_working_bytes} bytes; max 8 GiB working cap"
            )
        temp_summary_path.write_text(summary_text, encoding="utf-8")

        # Atomic publish only after verification and budget checks pass
        temp_output_path.replace(out_p)
        temp_summary_path.replace(sum_p)

        return out_p

    finally:
        if temp_output_path.exists():
            try:
                temp_output_path.unlink()
            except OSError:
                pass
        if temp_summary_path.exists():
            try:
                temp_summary_path.unlink()
            except OSError:
                pass


def format_progress_report(
    state: AcquisitionState,
    pipeline_roots: list[Path | str] | Path | str,
    raw_dir: Path | str,
    start_time: float,
    pause_reason: str | None = None,
) -> str:
    """Format real-time computed progress metrics using the full configured pipeline footprint."""
    counts = state.count_gefs_by_status()
    total = counts.get("total", 0)
    completed = counts.get(AcquisitionStatus.COMPLETED, 0)
    failed = counts.get(AcquisitionStatus.FAILED, 0)
    remaining = total - completed - failed

    raw_files = count_raw_c00_files(raw_dir)
    if isinstance(pipeline_roots, list):
        work_bytes = measure_pipeline_footprint(pipeline_roots)
    else:
        work_bytes = measure_pipeline_footprint([pipeline_roots])
    work_gib = work_bytes / (1024**3)

    elapsed = time.time() - start_time
    if completed > 0 and elapsed > 0:
        throughput_items = completed / elapsed
        eta_seconds = remaining / throughput_items if throughput_items > 0 else 0
        eta_str = f"{eta_seconds / 60:.1f} min" if eta_seconds < 3600 else f"{eta_seconds / 3600:.1f} hours"
    else:
        throughput_items = 0.0
        eta_str = "ETA: unavailable"

    lines = [
        f"Progress: completed={completed}/{total} ({completed/total*100:.1f}%)" if total else "Progress: 0/0",
        f"Remaining={remaining}, Failed={failed}",
        f"Raw buffer={raw_files}/{MAX_RAW_BUFFER_COUNT} files, Working footprint={work_gib:.2f}/8.00 GiB",
        f"Throughput={throughput_items:.2f} dates/sec, {eta_str}",
    ]
    if pause_reason:
        lines.append(f"PAUSED: {pause_reason}")

    return " | ".join(lines)


def sync_manifest_with_streaming_corpus(
    manifest_path: Path | str,
    state: AcquisitionState,
) -> int:
    """Synchronize DATA_MANIFEST.csv with actual provenance from the completed streaming corpus in SQLite.

    Requirements:
    - Exports only completed GEFS c00 items.
    - Preserves all existing IMD, pilot, p01-p04, PWAT, and Day-10 rows in their original order.
    - For 2018-08-01 c00 pilot: updates/reuses record deterministically without duplicating it.
    - Appends/inserts remaining completed GEFS c00 records with factual metadata:
      actual source_key, actual retrieval timestamp, actual byte count, actual SHA-256,
      init_utc, variable=apcp_sfc, member=c00, step_start_h=0, step_end_h=240, units=kg m**-2,
      status=decoded, and accurate shard path / raw deletion note.
    - Atomic file replacement: writes to temporary file and atomically replaces manifest_path.

    Returns the count of synchronized GEFS c00 records in the manifest.
    """
    m_path = Path(manifest_path)
    if not m_path.exists():
        raise FileNotFoundError(f"Manifest file not found at {m_path}")

    with open(m_path, "r", encoding="utf-8", newline="") as f:
        reader = list(csv.reader(f))

    if not reader:
        raise ValueError(f"Manifest file at {m_path} is empty")

    header = reader[0]
    existing_rows = reader[1:]

    completed_items = {
        item.init_date: item
        for item in state.list_gefs_items(status=AcquisitionStatus.COMPLETED)
    }

    synced_dates: set[str] = set()
    output_rows: list[list[str]] = []

    # Process existing rows: preserve all non-c00 rows; update existing c00 rows in-place
    for row in existing_rows:
        while len(row) < 15:
            row.append("")

        is_gefs = len(row) > 1 and row[1].strip() == "gefs"
        is_apcp = len(row) > 8 and row[8].strip() == "apcp_sfc"
        is_c00 = len(row) > 9 and row[9].strip() == "c00"
        is_day10 = (
            "day10" in row[0].lower()
            or "days:10-35" in (row[3] or "").lower()
            or (len(row) > 11 and row[10].strip() == "240" and row[11].strip() == "246")
        )

        if is_gefs and is_apcp and is_c00 and not is_day10:
            init_str = row[7].strip()[:10] if len(row) > 7 else ""
            if init_str in completed_items:
                item = completed_items[init_str]
                # For existing 2018-08-01 c00 pilot row, preserve manifest_id and verified note
                if row[0].strip() == "gefs-20180801-c00-apcp" or init_str == "2018-08-01":
                    row[0] = "gefs-20180801-c00-apcp"
                    row[1] = "gefs"
                    row[2] = "NOAA GEFSv12 reforecast"
                    row[3] = item.source_key or row[3]
                    if not row[4].strip() and (item.downloaded_at or item.verified_at):
                        row[4] = item.downloaded_at or item.verified_at or ""
                    row[5] = item.local_sha256 or row[5]
                    row[6] = str(item.local_bytes or row[6])
                    row[7] = f"{init_str}T00:00:00Z"
                    row[8] = "apcp_sfc"
                    row[9] = "c00"
                    row[10] = "0"
                    row[11] = "240"
                    row[12] = "kg m**-2"
                    row[13] = "decoded"
                    # Preserve existing verified pilot note
                else:
                    init_compact = init_str.replace("-", "")
                    row[0] = f"gefs-{init_compact}-c00-apcp"
                    row[1] = "gefs"
                    row[2] = "NOAA GEFSv12 reforecast"
                    row[3] = item.source_key
                    row[4] = item.downloaded_at or item.verified_at or item.completed_at or ""
                    row[5] = item.local_sha256 or ""
                    row[6] = str(item.local_bytes or item.remote_bytes or "")
                    row[7] = f"{init_str}T00:00:00Z"
                    row[8] = "apcp_sfc"
                    row[9] = "c00"
                    row[10] = "0"
                    row[11] = "240"
                    row[12] = "kg m**-2"
                    row[13] = "decoded"
                    shard_rel = f"data/interim/acquisition/shards/shard_{init_compact}.parquet"
                    del_time = item.raw_deleted_at or item.completed_at or ""
                    row[14] = (
                        f"Streamed c00 Days:1-10 reforecast; interim shard {shard_rel}; "
                        f"raw GRIB deleted after shard validation at {del_time}; "
                        f"0-240h accumulation in kg m**-2"
                    )
                synced_dates.add(init_str)
                output_rows.append(row)
            else:
                output_rows.append(row)
        else:
            # Preserve all non-c00 rows (IMD, pilots, p01-p04, PWAT, Day-10, method) unchanged
            output_rows.append(row)

    # Append remaining completed GEFS c00 records in chronological order
    for init_date in sorted(completed_items.keys()):
        if init_date in synced_dates:
            continue
        item = completed_items[init_date]
        init_compact = init_date.replace("-", "")
        manifest_id = f"gefs-{init_compact}-c00-apcp"
        source = "gefs"
        provider = "NOAA GEFSv12 reforecast"
        obj_key = item.source_key
        retrieved = item.downloaded_at or item.verified_at or item.completed_at or ""
        sha256 = item.local_sha256 or ""
        bytes_str = str(item.local_bytes or item.remote_bytes or "")
        init_utc = f"{init_date}T00:00:00Z"
        variable = "apcp_sfc"
        member = "c00"
        step_start = "0"
        step_end = "240"
        units = "kg m**-2"
        status = "decoded"
        shard_rel = f"data/interim/acquisition/shards/shard_{init_compact}.parquet"
        del_time = item.raw_deleted_at or item.completed_at or ""
        notes = (
            f"Streamed c00 Days:1-10 reforecast; interim shard {shard_rel}; "
            f"raw GRIB deleted after shard validation at {del_time}; "
            f"0-240h accumulation in kg m**-2"
        )
        output_rows.append([
            manifest_id, source, provider, obj_key, retrieved, sha256, bytes_str,
            init_utc, variable, member, step_start, step_end, units, status, notes
        ])
        synced_dates.add(init_date)

    # Atomic write to temporary file with flush and fsync
    with tempfile.NamedTemporaryFile("w", dir=m_path.parent, delete=False, encoding="utf-8", newline="") as tf:
        writer = csv.writer(tf, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(output_rows)
        tf.flush()
        os.fsync(tf.fileno())
        temp_name = tf.name

    Path(temp_name).replace(m_path)
    return len(synced_dates)
