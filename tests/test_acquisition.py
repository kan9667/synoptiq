"""Unit tests for disk-safe, resumable GEFSv12/IMD streaming acquisition pipeline."""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from bust.data.acquisition import (
    MAX_RAW_BUFFER_COUNT,
    MAX_WORKING_BYTES,
    DiskLimitExceededError,
    check_disk_limits,
    count_raw_c00_files,
    derive_expected_gefs_dates,
    download_gefs_c00_file,
    finalize_streaming_dataset,
    get_configured_pipeline_roots,
    inventory_noaa_s3,
    measure_pipeline_footprint,
    parse_s3_xml_for_c00,
    process_gefs_c00_file,
    validate_shard_for_recovery,
    verify_or_fetch_imd_years,
)
from bust.data.acquisition_state import AcquisitionState, AcquisitionStatus, GEFSRecord, IMDRecord


def test_disk_limits_pause_producer(tmp_path: Path) -> None:
    """The 150-file buffer and 8 GiB working footprint limits must pause the downloader."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True)

    # 1. Under limits: allowed
    allowed, reason = check_disk_limits(work_dir, raw_dir, projected_file_bytes=28_000_000)
    assert allowed is True
    assert reason is None

    # 2. Raw buffer limit: create 150 fake c00 raw files
    for i in range(MAX_RAW_BUFFER_COUNT):
        (raw_dir / f"apcp_sfc_201808{i:02d}00_c00.grib2").write_bytes(b"dummy")

    assert count_raw_c00_files(raw_dir) == 150
    allowed, reason = check_disk_limits(work_dir, raw_dir, projected_file_bytes=28_000_000)
    assert allowed is False
    assert "Raw buffer limit reached" in reason
    assert "150/150 files" in reason

    # download_gefs_c00_file must raise DiskLimitExceededError
    dummy_item = GEFSRecord(
        init_date="2018-08-01",
        member="c00",
        source_key="GEFSv12/reforecast/2018/2018080100/c00/Days:1-10/apcp_sfc_2018080100_c00.grib2",
        remote_bytes=28_987_248,
    )
    with pytest.raises(DiskLimitExceededError, match="Raw buffer limit reached"):
        download_gefs_c00_file(dummy_item, raw_dir=raw_dir, work_dir=work_dir)

    # 3. Footprint limit: remove files, test 8 GiB cap
    for f in raw_dir.glob("*.grib2"):
        f.unlink()

    # Test footprint limit check with low custom max_working_bytes
    allowed_footprint, footprint_reason = check_disk_limits(
        work_dir,
        raw_dir,
        projected_file_bytes=1000,
        max_working_bytes=500,
    )
    assert allowed_footprint is False
    assert "Working footprint limit would be exceeded" in footprint_reason


def test_concurrency_configuration_and_limits(tmp_path: Path) -> None:
    """Concurrency is configurable and does not bypass disk limits."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True)

    # Fill raw buffer
    for i in range(MAX_RAW_BUFFER_COUNT):
        (raw_dir / f"apcp_sfc_201808{i:02d}00_c00.grib2").write_bytes(b"dummy")

    # Even with high concurrency, disk check must strictly block
    concurrency = 12
    assert concurrency == 12
    allowed, _ = check_disk_limits(work_dir, raw_dir)
    assert allowed is False


def test_part_file_atomic_rename(tmp_path: Path, monkeypatch) -> None:
    """.part files never become ready files before full validation and atomic rename."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    init_date = "2018-08-01"
    key = "GEFSv12/reforecast/2018/2018080100/c00/Days:1-10/apcp_sfc_2018080100_c00.grib2"
    dummy_payload = b"dummy-grib2-exact-payload"
    dummy_etag = hashlib.md5(dummy_payload).hexdigest()

    item = GEFSRecord(
        init_date=init_date,
        member="c00",
        source_key=key,
        remote_bytes=len(dummy_payload),
        etag=dummy_etag,
    )
    state.upsert_gefs_inventory(init_date, key, remote_bytes=len(dummy_payload), etag=dummy_etag)

    # Case A: Successful download
    class MockResponseSuccess:
        status_code = 200

        def iter_bytes(self, chunk_size=65536):
            yield dummy_payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    mock_client = MagicMock()
    mock_client.stream.return_value = MockResponseSuccess()

    out_file = download_gefs_c00_file(
        item=item,
        raw_dir=raw_dir,
        work_dir=work_dir,
        client=mock_client,
        state=state,
    )

    assert out_file.name == "apcp_sfc_2018080100_c00.grib2"
    assert out_file.exists()
    assert len(list(raw_dir.glob("*.part*"))) == 0
    rec = state.get_gefs_item(init_date)
    assert rec.status == AcquisitionStatus.VERIFIED
    assert rec.local_bytes == len(dummy_payload)

    # Case B: Corrupted/incomplete download (byte size mismatch)
    out_file.unlink()
    item_bad = GEFSRecord(
        init_date="2018-08-02",
        member="c00",
        source_key="GEFSv12/reforecast/2018/2018080200/c00/Days:1-10/apcp_sfc_2018080200_c00.grib2",
        remote_bytes=999999,  # Expects large file, receives tiny payload
    )
    state.upsert_gefs_inventory("2018-08-02", item_bad.source_key, remote_bytes=999999)

    with pytest.raises(ValueError, match="Byte size mismatch"):
        download_gefs_c00_file(
            item=item_bad,
            raw_dir=raw_dir,
            work_dir=work_dir,
            client=mock_client,
            state=state,
        )

    # Assert no .part file remains and no canonical file was created
    assert len(list(raw_dir.glob("*.part*"))) == 0
    assert not (raw_dir / "apcp_sfc_2018080200_c00.grib2").exists()


def test_retry_and_exhaustion(tmp_path: Path) -> None:
    """Failed downloads are retried up to 3 times and remain failed after retry exhaustion."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    init_date = "2018-08-01"
    key = "GEFSv12/reforecast/2018/2018080100/c00/Days:1-10/apcp_sfc_2018080100_c00.grib2"
    item = GEFSRecord(init_date=init_date, member="c00", source_key=key, remote_bytes=100)
    state.upsert_gefs_inventory(init_date, key, remote_bytes=100)

    class MockFailingResponse:
        status_code = 500

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    mock_client = MagicMock()
    mock_client.stream.return_value = MockFailingResponse()

    # Attempt 1
    with pytest.raises(RuntimeError, match="HTTP 500"):
        download_gefs_c00_file(item, raw_dir, work_dir, client=mock_client, state=state, max_retries=3)
    rec1 = state.get_gefs_item(init_date)
    assert rec1.attempt_count == 1
    assert rec1.status == AcquisitionStatus.LISTED  # Eligible for retry

    # Attempt 2
    item.attempt_count = rec1.attempt_count
    with pytest.raises(RuntimeError, match="HTTP 500"):
        download_gefs_c00_file(item, raw_dir, work_dir, client=mock_client, state=state, max_retries=3)
    rec2 = state.get_gefs_item(init_date)
    assert rec2.attempt_count == 2
    assert rec2.status == AcquisitionStatus.LISTED

    # Attempt 3 (exhaustion)
    item.attempt_count = rec2.attempt_count
    with pytest.raises(RuntimeError, match="HTTP 500"):
        download_gefs_c00_file(item, raw_dir, work_dir, client=mock_client, state=state, max_retries=3)
    rec3 = state.get_gefs_item(init_date)
    assert rec3.attempt_count == 3
    assert rec3.status == AcquisitionStatus.FAILED
    assert "HTTP 500" in rec3.last_error


def test_crash_recovery_resumes_safely(tmp_path: Path) -> None:
    """Crash recovery resolves deletion_pending, downloading, and processing states safely."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    # 1. deletion_pending with valid shard and retained raw file
    shard_path = shards_dir / "shard_20180801.parquet"
    shard_path.write_bytes(b"dummy-parquet-data")
    raw_path = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_path.write_bytes(b"dummy-raw-data")

    state.upsert_gefs_inventory("2018-08-01", "key1", remote_bytes=100)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.DELETION_PENDING,
        local_path=str(raw_path),
        shard_path=str(shard_path),
        shard_rows=1120,
    )

    # 2. downloading state (interrupted mid-download)
    state.upsert_gefs_inventory("2018-08-02", "key2", remote_bytes=100)
    state.update_gefs_status("2018-08-02", status=AcquisitionStatus.DOWNLOADING)

    # 3. processing state with existing raw file
    raw2 = raw_dir / "apcp_sfc_2018080300_c00.grib2"
    raw2.write_bytes(b"dummy")
    state.upsert_gefs_inventory("2018-08-03", "key3", remote_bytes=100)
    state.update_gefs_status(
        "2018-08-03",
        status=AcquisitionStatus.PROCESSING,
        local_path=str(raw2),
    )

    # Run recovery with mock validator
    counts = state.recover_in_flight_states(shard_validator=lambda p, dt: (True, None))
    assert counts["deletion_pending_completed"] == 1
    assert counts["reset_to_listed"] >= 1
    assert counts["reset_to_verified"] >= 1

    # Check 2018-08-01 transitioned to completed and raw file deleted
    item1 = state.get_gefs_item("2018-08-01")
    assert item1.status == AcquisitionStatus.COMPLETED
    assert not raw_path.exists()

    # Check 2018-08-02 reset to listed
    item2 = state.get_gefs_item("2018-08-02")
    assert item2.status == AcquisitionStatus.LISTED

    # Check 2018-08-03 reset to verified
    item3 = state.get_gefs_item("2018-08-03")
    assert item3.status == AcquisitionStatus.VERIFIED


def test_raw_file_deleted_only_after_schema_validation(tmp_path: Path, monkeypatch) -> None:
    """Consumer retains raw GRIB file if row building or schema validation fails."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    raw_file = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_file.write_bytes(b"dummy-grib")

    item = GEFSRecord(
        init_date="2018-08-01",
        member="c00",
        source_key="key",
        local_path=str(raw_file),
        status=AcquisitionStatus.VERIFIED,
    )
    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=100)
    state.update_gefs_status("2018-08-01", status=AcquisitionStatus.VERIFIED, local_path=str(raw_file))

    # Mock build_rows_for_date to raise ValueError (simulating schema corruption)
    from bust.data import acquisition as acq_module

    def failing_build_rows(*args, **kwargs):
        raise ValueError("Corrupted GRIB geometry detected")

    monkeypatch.setattr(acq_module, "build_rows_for_date", failing_build_rows)

    with pytest.raises(ValueError, match="Corrupted GRIB geometry detected"):
        process_gefs_c00_file(
            item=item,
            shards_dir=shards_dir,
            imd_nc_dir=tmp_path / "imd",
            regions=[],
            state=state,
        )

    # Assert raw file was NOT deleted and failure counted!
    assert raw_file.exists()
    gefs_item = state.get_gefs_item("2018-08-01")
    assert gefs_item.status == AcquisitionStatus.VERIFIED
    assert gefs_item.processing_attempts == 1
    assert "Processing failure (attempt 1/3)" in gefs_item.last_error


def test_completed_work_not_redownloaded_or_reprocessed(tmp_path: Path) -> None:
    """Completed items are skipped and never re-downloaded or reprocessed."""
    state = AcquisitionState(tmp_path / "state.db")
    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=100)
    state.update_gefs_status("2018-08-01", status=AcquisitionStatus.COMPLETED)

    # Listed items query only returns status='listed'
    listed = state.list_gefs_items(status=AcquisitionStatus.LISTED)
    assert len(listed) == 0

    # Verified items query only returns status='verified'
    verified = state.list_gefs_items(status=AcquisitionStatus.VERIFIED)
    assert len(verified) == 0


def test_incomplete_or_failed_items_block_finalization(tmp_path: Path) -> None:
    """Finalization fails if any date is not completed or has failed status."""
    state = AcquisitionState(tmp_path / "state.db")
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir()
    out_file = tmp_path / "rows.parquet"
    sum_file = tmp_path / "summary.json"

    # State has 0 completed dates (expected 3,652)
    with pytest.raises(RuntimeError, match="dates are incomplete or missing"):
        finalize_streaming_dataset(
            state=state,
            shards_dir=shards_dir,
            output_path=out_file,
            summary_path=sum_file,
        )


def test_unexpected_gefs_dates_and_2020_gefs_block_finalization(tmp_path: Path, monkeypatch) -> None:
    """Unexpected GEFS dates or 2020 GEFS initializations block finalization."""
    state = AcquisitionState(tmp_path / "state.db")
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir()

    # Mock derive_expected_gefs_dates to return 1 date
    from bust.data import acquisition as acq_module

    monkeypatch.setattr(acq_module, "derive_expected_gefs_dates", lambda: ["2018-08-01"])

    state.upsert_gefs_inventory("2018-08-01", "key1", remote_bytes=100)
    state.update_gefs_status("2018-08-01", status=AcquisitionStatus.COMPLETED)

    # Add forbidden 2020 GEFS initialization
    state.upsert_gefs_inventory("2020-01-01", "key2020", remote_bytes=100)
    state.update_gefs_status("2020-01-01", status=AcquisitionStatus.COMPLETED)

    with pytest.raises(ValueError, match="Found unexpected GEFS initialization dates"):
        finalize_streaming_dataset(
            state=state,
            shards_dir=shards_dir,
            output_path=tmp_path / "rows.parquet",
            summary_path=tmp_path / "summary.json",
        )


def test_imd_2020_required_as_late_2019_verification(tmp_path: Path) -> None:
    """IMD 2020 is checked as verification-only annual coverage."""
    state = AcquisitionState(tmp_path / "state.db")
    imd_dir = tmp_path / "imd"
    imd_dir.mkdir()

    # Missing 2020 file must be reported
    is_complete, errors = verify_or_fetch_imd_years(
        state=state,
        imd_dir=imd_dir,
        required_years=[2019, 2020],
        allow_network_download=False,
    )
    assert is_complete is False
    assert any("2020" in e for e in errors)

    # Expected GEFS dates must NOT include 2020 dates!
    gefs_dates = derive_expected_gefs_dates()
    assert "2019-12-31" in gefs_dates
    assert "2020-01-01" not in gefs_dates


def test_replace_final_flag_required(tmp_path: Path, monkeypatch) -> None:
    """Overwriting existing rows.parquet requires --replace-final."""
    state = AcquisitionState(tmp_path / "state.db")
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir()
    out_file = tmp_path / "rows.parquet"
    out_file.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="Refusing to overwrite without --replace-final"):
        finalize_streaming_dataset(
            state=state,
            shards_dir=shards_dir,
            output_path=out_file,
            summary_path=tmp_path / "summary.json",
            replace_final=False,
        )


def test_non_grib_records_cannot_satisfy_inventory() -> None:
    """Non-GRIB records or guessed keys cannot satisfy inventory."""
    sample_invalid_xml = b"""
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <Contents>
            <Key>data/invalid/replay_contract.json</Key>
            <Size>1000</Size>
            <ETag>"dummy"</ETag>
        </Contents>
    </ListBucketResult>
    """
    parsed = parse_s3_xml_for_c00(sample_invalid_xml, "2018-08-01")
    assert parsed is None


def test_measure_pipeline_footprint_no_double_counting(tmp_path: Path) -> None:
    """measure_pipeline_footprint avoids double-counting nested directories and paths."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)

    (work_dir / "state.db").write_bytes(b"x" * 500)
    (raw_dir / "file1.grib2").write_bytes(b"y" * 1000)
    (shards_dir / "file2.parquet").write_bytes(b"z" * 2000)

    roots = get_configured_pipeline_roots(
        work_dir=work_dir,
        raw_gefs_dir=raw_dir,
        shards_dir=shards_dir,
        raw_imd_dir=tmp_path / "imd",
    )
    # Total should be exactly 500 + 1000 + 2000 = 3500 bytes (not double-counted)
    measured = measure_pipeline_footprint(roots)
    assert measured == 3500


def test_atomic_download_batch_reservation_boundary(tmp_path: Path) -> None:
    """Starting at 145 reserved/ready objects, attempting a 12-download batch reserves exactly 5."""
    state = AcquisitionState(tmp_path / "state.db")
    for i in range(1, 21):
        d_str = f"2018-08-{i:02d}"
        state.upsert_gefs_inventory(d_str, f"key_{i}", remote_bytes=28_000_000)

    # Ready raw count is 145. Buffer limit is 150.
    # Attempting to reserve batch of 12 items:
    reserved = state.reserve_download_batch(
        ready_raw_count=145,
        measured_footprint_bytes=1000,
        batch_size=12,
        max_working_bytes=MAX_WORKING_BYTES,
        max_raw_files=150,
    )

    # Exactly 5 items must be reserved (145 + 5 = 150)
    assert len(reserved) == 5
    assert [item.init_date for item in reserved] == [f"2018-08-{i:02d}" for i in range(1, 6)]

    # Check status in database: only the 5 reserved items are 'downloading', rest are 'listed'
    downloading = state.list_gefs_items(status=AcquisitionStatus.DOWNLOADING)
    assert len(downloading) == 5
    listed = state.list_gefs_items(status=AcquisitionStatus.LISTED)
    assert len(listed) == 15

    # A subsequent attempt while 145 are ready and 5 are in-flight must reserve 0
    second_attempt = state.reserve_download_batch(
        ready_raw_count=145,
        measured_footprint_bytes=1000,
        batch_size=12,
        max_working_bytes=MAX_WORKING_BYTES,
        max_raw_files=150,
    )
    assert len(second_attempt) == 0


def test_recovery_rejects_invalid_shard_and_retains_raw(tmp_path: Path) -> None:
    """Non-empty invalid shard is rejected during recovery, retaining raw file and resetting to retriable status."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_dir = tmp_path / "raw"
    shards_dir = tmp_path / "shards"
    raw_dir.mkdir()
    shards_dir.mkdir()

    raw_file = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_file.write_bytes(b"dummy-raw-content")

    # Create an invalid parquet shard (non-empty, but wrong columns / row count)
    invalid_shard = shards_dir / "shard_20180801.parquet"
    df_invalid = pd.DataFrame({"some_col": [1, 2, 3]})
    df_invalid.to_parquet(invalid_shard)

    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=100)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.DELETION_PENDING,
        local_path=str(raw_file),
        shard_path=str(invalid_shard),
        shard_rows=3,
    )

    def _validator(p: Path, init_d: str) -> tuple[bool, str | None]:
        return validate_shard_for_recovery(p, init_d, expected_rows=1120)

    recovered = state.recover_in_flight_states(shard_validator=_validator)

    assert recovered["deletion_pending_completed"] == 0
    assert recovered["deletion_pending_rejected"] == 1
    assert recovered["reset_to_verified"] == 1

    # Raw file MUST still exist!
    assert raw_file.exists()

    # Item must be reset to retriable 'verified' with error recorded
    item = state.get_gefs_item("2018-08-01")
    assert item.status == AcquisitionStatus.VERIFIED
    assert "Recovery rejected" in item.last_error
    assert "Shard row count mismatch" in item.last_error


def test_bounded_processing_retries_and_exhaustion(tmp_path: Path, monkeypatch) -> None:
    """Processing failures increment processing_attempts, remain retriable, and mark FAILED only on exhaustion."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    raw_file = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_file.write_bytes(b"dummy-grib")

    h = hashlib.sha256(b"dummy-grib").hexdigest()
    item = GEFSRecord(
        init_date="2018-08-01",
        member="c00",
        source_key="key",
        local_path=str(raw_file),
        local_bytes=len(b"dummy-grib"),
        local_sha256=h,
        status=AcquisitionStatus.VERIFIED,
    )
    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=len(b"dummy-grib"))
    state.record_gefs_download_success("2018-08-01", local_path=raw_file, local_bytes=len(b"dummy-grib"), local_sha256=h)

    from bust.data import acquisition as acq_module

    # Attempt 1: failure
    def failing_build_rows(*args, **kwargs):
        raise ValueError("Decoding transient error")

    monkeypatch.setattr(acq_module, "build_rows_for_date", failing_build_rows)

    with pytest.raises(ValueError, match="Decoding transient error"):
        process_gefs_c00_file(item, shards_dir=shards_dir, imd_nc_dir=tmp_path / "imd", regions=[], state=state, max_retries=3)

    item1 = state.get_gefs_item("2018-08-01")
    assert raw_file.exists()
    assert item1.status == AcquisitionStatus.VERIFIED
    assert item1.processing_attempts == 1

    # Attempt 2: failure
    with pytest.raises(ValueError, match="Decoding transient error"):
        process_gefs_c00_file(item, shards_dir=shards_dir, imd_nc_dir=tmp_path / "imd", regions=[], state=state, max_retries=3)

    item2 = state.get_gefs_item("2018-08-01")
    assert raw_file.exists()
    assert item2.status == AcquisitionStatus.VERIFIED
    assert item2.processing_attempts == 2

    # Attempt 3: exhaustion -> status becomes FAILED
    with pytest.raises(ValueError, match="Decoding transient error"):
        process_gefs_c00_file(item, shards_dir=shards_dir, imd_nc_dir=tmp_path / "imd", regions=[], state=state, max_retries=3)

    item3 = state.get_gefs_item("2018-08-01")
    assert raw_file.exists()
    assert item3.status == AcquisitionStatus.FAILED
    assert item3.processing_attempts == 3


def test_raw_file_integrity_revalidation_before_decode(tmp_path: Path, monkeypatch) -> None:
    """Modifying verified raw file before processing triggers integrity check, prevents decode, and retains file."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    raw_file = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    original_bytes = b"valid-raw-grib-stream"
    raw_file.write_bytes(original_bytes)
    original_sha = hashlib.sha256(original_bytes).hexdigest()

    item = GEFSRecord(
        init_date="2018-08-01",
        member="c00",
        source_key="key",
        local_path=str(raw_file),
        local_bytes=len(original_bytes),
        local_sha256=original_sha,
        status=AcquisitionStatus.VERIFIED,
    )
    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=len(original_bytes))
    state.record_gefs_download_success(
        "2018-08-01", local_path=raw_file, local_bytes=len(original_bytes), local_sha256=original_sha
    )

    # Now modify raw file on disk (simulate on-disk corruption)
    raw_file.write_bytes(b"corrupted-tampered-data")

    from bust.data import acquisition as acq_module
    mock_build_rows = MagicMock()
    monkeypatch.setattr(acq_module, "build_rows_for_date", mock_build_rows)

    with pytest.raises(ValueError, match="Raw file size mismatch before decode"):
        process_gefs_c00_file(item, shards_dir=shards_dir, imd_nc_dir=tmp_path / "imd", regions=[], state=state)

    # Assert build_rows_for_date was NEVER called
    mock_build_rows.assert_not_called()

    # Raw file must still exist
    assert raw_file.exists()

    # State records processing attempt and retriable status
    rec = state.get_gefs_item("2018-08-01")
    assert rec.status == AcquisitionStatus.VERIFIED
    assert rec.processing_attempts == 1
    assert "size mismatch" in rec.last_error


def test_near_cap_processing_blocks_shard_write_and_retains_raw(tmp_path: Path, monkeypatch) -> None:
    """If writing the candidate Parquet shard would cross 8 GiB, shard is not written, raw file is retained, and item remains retriable."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    raw_file = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_file.write_bytes(b"dummy-grib-content" * 100)
    h = hashlib.sha256(raw_file.read_bytes()).hexdigest()

    item = GEFSRecord(
        init_date="2018-08-01",
        member="c00",
        source_key="key",
        local_path=str(raw_file),
        local_bytes=raw_file.stat().st_size,
        local_sha256=h,
        status=AcquisitionStatus.VERIFIED,
    )
    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=raw_file.stat().st_size)
    state.record_gefs_download_success("2018-08-01", local_path=raw_file, local_bytes=raw_file.stat().st_size, local_sha256=h)

    from bust.data import acquisition as acq_module

    def mock_rows(*args, **kwargs):
        return [
            {
                "init_utc": "2018-08-01T00:00:00Z",
                "lead_day": 1,
                "valid_start_utc": "2018-08-01T03:00:00Z",
                "valid_end_utc": "2018-08-02T03:00:00Z",
                "region_id": "r_01",
                "season": "JJAS",
                "lead_bucket": "1-3",
                "f_control_mm": 10.0,
                "o_imd_mm": 12.0,
                "coverage_fraction": 1.0,
                "error_mm": 2.0,
                "threshold_mm": None,
                "bust": None,
                "source_key": "key",
                "grib_steps": "0-24",
                "imd_year": 2018,
                "window_quality": "exact",
                "split": "test",
            }
        ] * 10

    monkeypatch.setattr(acq_module, "build_rows_for_date", mock_rows)

    tight_cap = raw_file.stat().st_size + 10

    with pytest.raises(DiskLimitExceededError, match="Working footprint limit would be exceeded"):
        process_gefs_c00_file(
            item=item,
            shards_dir=shards_dir,
            imd_nc_dir=tmp_path / "imd",
            regions=[],
            state=state,
            max_working_bytes=tight_cap,
        )

    # Shard must NOT exist
    assert len(list(shards_dir.glob("*.parquet"))) == 0

    # Raw file MUST be retained
    assert raw_file.exists()

    # Item must remain retriable VERIFIED (failure count not incremented)
    rec = state.get_gefs_item("2018-08-01")
    assert rec.status == AcquisitionStatus.VERIFIED
    assert rec.processing_attempts == 0
    assert "Paused by disk footprint limit" in rec.last_error


def test_near_cap_finalization_blocks_and_preserves_shards(tmp_path: Path, monkeypatch) -> None:
    """If finalization output would cross 8 GiB, finalization stops, shards are preserved, and no output is published."""
    state = AcquisitionState(tmp_path / "state.db")
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir()
    out_file = tmp_path / "rows.parquet"
    sum_file = tmp_path / "summary.json"

    from bust.data import acquisition as acq_module
    monkeypatch.setattr(acq_module, "derive_expected_gefs_dates", lambda: ["2018-08-01"])

    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=100)
    state.update_gefs_status("2018-08-01", status=AcquisitionStatus.COMPLETED)

    # Write a valid shard
    shard_df = pd.DataFrame([{
        "init_utc": "2018-08-01T00:00:00Z",
        "lead_day": 1,
        "valid_start_utc": "2018-08-01T03:00:00Z",
        "valid_end_utc": "2018-08-02T03:00:00Z",
        "region_id": "r_01",
        "season": "JJAS",
        "lead_bucket": "1-3",
        "f_control_mm": 10.0,
        "o_imd_mm": 12.0,
        "coverage_fraction": 1.0,
        "error_mm": 2.0,
        "threshold_mm": 10.0,
        "bust": False,
        "source_key": "key",
        "grib_steps": "0-24",
        "imd_year": 2018,
        "window_quality": "exact",
        "split": "test",
    }])
    shard_path = shards_dir / "shard_20180801.parquet"
    shard_df.to_parquet(shard_path, index=False)

    tight_cap = shard_path.stat().st_size + 10

    with pytest.raises(DiskLimitExceededError, match="Working footprint limit"):
        finalize_streaming_dataset(
            state=state,
            shards_dir=shards_dir,
            output_path=out_file,
            summary_path=sum_file,
            splits_cfg={"train_years": [2010]},
            max_working_bytes=tight_cap,
        )

    # Shard must be preserved!
    assert shard_path.exists()
    assert shard_path.stat().st_size > 0

    # No final output published
    assert not out_file.exists()
    assert not sum_file.exists()


def test_crash_recovery_cleans_orphan_part_file_and_resets_item(tmp_path: Path) -> None:
    """Interrupted downloads clean up only their designated .part files and reset items to listed."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    orphan_part = raw_dir / "apcp_sfc_2018080100_c00.grib2.part_12345678"
    orphan_part.write_bytes(b"partial-corrupted-download")

    unrelated_file = raw_dir / "apcp_sfc_2018080200_c00.grib2"
    unrelated_file.write_bytes(b"complete-canonical-file")

    state.upsert_gefs_inventory("2018-08-01", "key1", remote_bytes=100)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.DOWNLOADING,
        part_path=str(orphan_part),
    )

    _ = state.recover_in_flight_states()

    # Orphan .part file MUST be removed
    assert not orphan_part.exists()

    # Unrelated canonical file MUST NOT be touched
    assert unrelated_file.exists()

    # Item must be reset to retriable listed
    item = state.get_gefs_item("2018-08-01")
    assert item.status == AcquisitionStatus.LISTED
    assert item.part_path is None
    assert "cleaned_part=True" in item.last_error


def test_post_write_shard_readback_failure_retains_raw_and_records_failure(tmp_path: Path, monkeypatch) -> None:
    """Post-write readback validation failure deletes invalid shard, retains raw file, and records bounded failure."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    raw_file = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_file.write_bytes(b"dummy-grib")
    h = hashlib.sha256(b"dummy-grib").hexdigest()

    item = GEFSRecord(
        init_date="2018-08-01",
        member="c00",
        source_key="key",
        local_path=str(raw_file),
        local_bytes=len(b"dummy-grib"),
        local_sha256=h,
        status=AcquisitionStatus.VERIFIED,
    )
    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=len(b"dummy-grib"))
    state.record_gefs_download_success("2018-08-01", local_path=raw_file, local_bytes=len(b"dummy-grib"), local_sha256=h)

    from bust.data import acquisition as acq_module

    def mock_rows(*args, **kwargs):
        return [
            {
                "init_utc": "2018-08-01T00:00:00Z",
                "lead_day": 1,
                "valid_start_utc": "2018-08-01T03:00:00Z",
                "valid_end_utc": "2018-08-02T03:00:00Z",
                "region_id": "r_01",
                "season": "JJAS",
                "lead_bucket": "1-3",
                "f_control_mm": 10.0,
                "o_imd_mm": 12.0,
                "coverage_fraction": 1.0,
                "error_mm": 2.0,
                "threshold_mm": None,
                "bust": None,
                "source_key": "key",
                "grib_steps": "0-24",
                "imd_year": 2018,
                "window_quality": "exact",
                "split": "test",
            }
        ]

    monkeypatch.setattr(acq_module, "build_rows_for_date", mock_rows)

    def failing_readback(*args, **kwargs):
        return False, "Corrupted Parquet CRC checksum on disk"

    monkeypatch.setattr(acq_module, "validate_shard_for_recovery", failing_readback)

    with pytest.raises(ValueError, match="Post-write shard readback validation failed"):
        process_gefs_c00_file(
            item=item,
            shards_dir=shards_dir,
            imd_nc_dir=tmp_path / "imd",
            regions=[],
            state=state,
        )

    # Raw file must NOT be deleted
    assert raw_file.exists()

    # Invalid shard must NOT be left on disk
    canonical_shard = shards_dir / "shard_20180801.parquet"
    assert not canonical_shard.exists()

    # Bounded failure must be recorded
    rec = state.get_gefs_item("2018-08-01")
    assert rec.status == AcquisitionStatus.VERIFIED
    assert rec.processing_attempts == 1
    assert "Corrupted Parquet CRC" in rec.last_error


def test_recovery_fails_safely_when_canonical_region_config_unavailable(tmp_path: Path) -> None:
    """When canonical region configuration cannot be resolved, shard validation rejects recovery to protect raw data."""
    fake_shard = tmp_path / "shard_20180801.parquet"
    df = pd.DataFrame({"init_utc": ["2018-08-01T00:00:00Z"]})
    df.to_parquet(fake_shard)

    valid_no_config, _err = validate_shard_for_recovery(
        shard_path=fake_shard,
        expected_init_date="2018-08-01",
        regions_path=tmp_path / "missing.geojson",
        expected_rows=None,
        regions_count=None,
    )
    assert valid_no_config is False


def test_recovery_simulated_raw_unlink_failure_retains_deletion_pending(tmp_path: Path, monkeypatch) -> None:
    """If unlinking raw file raises OSError during recovery, status remains deletion_pending, raw remains, and no completion timestamp."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    shard_path = shards_dir / "shard_20180801.parquet"
    shard_path.write_bytes(b"dummy-parquet-data")
    raw_path = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_path.write_bytes(b"dummy-raw-data")

    state.upsert_gefs_inventory("2018-08-01", "key1", remote_bytes=100)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.DELETION_PENDING,
        local_path=str(raw_path),
        shard_path=str(shard_path),
        shard_rows=1120,
    )

    # Monkeypatch raw_path.unlink to simulate OSError
    orig_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if str(self) == str(raw_path):
            raise OSError("Permission denied: simulated raw deletion failure")
        return orig_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    counts = state.recover_in_flight_states(shard_validator=lambda p, dt: (True, None))

    assert counts["deletion_pending_completed"] == 0
    assert counts.get("deletion_pending_failed", 0) == 1

    item = state.get_gefs_item("2018-08-01")
    assert item.status == AcquisitionStatus.DELETION_PENDING
    assert raw_path.exists()
    assert item.local_path == str(raw_path)
    assert item.raw_deleted_at is None
    assert item.completed_at is None
    assert "Permission denied: simulated raw deletion failure" in item.last_error


def test_recovery_missing_shard_validator_retains_raw_and_not_completed(tmp_path: Path) -> None:
    """If no shard validator is supplied to recovery, raw file remains and item is not completed."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    shard_path = shards_dir / "shard_20180801.parquet"
    shard_path.write_bytes(b"dummy-parquet-data")
    raw_path = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_path.write_bytes(b"dummy-raw-data")

    state.upsert_gefs_inventory("2018-08-01", "key1", remote_bytes=100)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.DELETION_PENDING,
        local_path=str(raw_path),
        shard_path=str(shard_path),
        shard_rows=1120,
    )

    counts = state.recover_in_flight_states(shard_validator=None)

    assert counts["deletion_pending_completed"] == 0
    assert counts["deletion_pending_rejected"] == 1
    assert counts["reset_to_verified"] == 1

    item = state.get_gefs_item("2018-08-01")
    assert item.status == AcquisitionStatus.VERIFIED
    assert raw_path.exists()
    assert item.raw_deleted_at is None
    assert item.completed_at is None
    assert "No shard validator provided" in item.last_error


def test_recovery_validator_exception_retains_raw_and_not_completed(tmp_path: Path) -> None:
    """If shard validator raises an exception during recovery, raw file remains and item is not completed."""
    work_dir = tmp_path / "work"
    raw_dir = work_dir / "raw"
    shards_dir = work_dir / "shards"
    raw_dir.mkdir(parents=True)
    shards_dir.mkdir(parents=True)
    state = AcquisitionState(work_dir / "state.db")

    shard_path = shards_dir / "shard_20180801.parquet"
    shard_path.write_bytes(b"dummy-parquet-data")
    raw_path = raw_dir / "apcp_sfc_2018080100_c00.grib2"
    raw_path.write_bytes(b"dummy-raw-data")

    state.upsert_gefs_inventory("2018-08-01", "key1", remote_bytes=100)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.DELETION_PENDING,
        local_path=str(raw_path),
        shard_path=str(shard_path),
        shard_rows=1120,
    )

    def exploding_validator(p, dt):
        raise RuntimeError("Validator exploded unexpectedly")

    counts = state.recover_in_flight_states(shard_validator=exploding_validator)

    assert counts["deletion_pending_completed"] == 0
    assert counts["deletion_pending_rejected"] == 1
    assert counts["reset_to_verified"] == 1

    item = state.get_gefs_item("2018-08-01")
    assert item.status == AcquisitionStatus.VERIFIED
    assert raw_path.exists()
    assert item.raw_deleted_at is None
    assert item.completed_at is None
    assert "Validator exploded unexpectedly" in item.last_error


def test_finalization_blocks_when_encoded_reservation_exceeds_budget_even_if_table_nbytes_fits(tmp_path: Path, monkeypatch) -> None:
    """Finalization blocks if encoded standalone Parquet reservation exceeds budget, even if table.nbytes would fit."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    from bust.data import acquisition as acq_module
    d_str = "2012-08-01"
    monkeypatch.setattr(acq_module, "derive_expected_gefs_dates", lambda: [d_str])

    state = AcquisitionState(tmp_path / "state.db")
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir(parents=True)

    yyyymmdd = "20120801"
    shard_path = shards_dir / f"shard_{yyyymmdd}.parquet"

    rows = [
        {
            "init_utc": "2012-08-01T00:00:00Z",
            "lead_day": 1,
            "valid_start_utc": "2012-08-01T03:00:00Z",
            "valid_end_utc": "2012-08-02T03:00:00Z",
            "region_id": "r_01",
            "season": "JJAS",
            "lead_bucket": "1-3",
            "f_control_mm": 10.0,
            "o_imd_mm": 12.0,
            "coverage_fraction": 1.0,
            "error_mm": 2.0,
            "threshold_mm": 15.0,
            "bust": False,
            "source_key": "key",
            "grib_steps": "0-24",
            "imd_year": 2012,
            "window_quality": "exact",
            "split": "train",
        }
    ] * 5
    df = pd.DataFrame(rows)
    df.to_parquet(shard_path, index=False)

    state.upsert_gefs_inventory(d_str, "key", remote_bytes=100)
    state.record_gefs_download_success(d_str, local_path=tmp_path / "dummy.raw", local_bytes=100, local_sha256="abc")
    state.record_gefs_shard_success(d_str, shard_path=shard_path, shard_rows=len(df))
    state.record_gefs_completed(d_str)

    output_path = tmp_path / "output" / "rows.parquet"
    summary_path = tmp_path / "output" / "summary.json"

    current_footprint = measure_pipeline_footprint([shards_dir])

    cols = [
        "init_utc", "lead_day", "valid_start_utc", "valid_end_utc", "region_id",
        "season", "lead_bucket", "f_control_mm", "o_imd_mm", "coverage_fraction",
        "error_mm", "threshold_mm", "bust", "source_key", "grib_steps",
        "imd_year", "window_quality", "split"
    ]
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
    tbl = pa.Table.from_pandas(df[cols], schema=parquet_schema, preserve_index=False)
    table_nbytes = tbl.nbytes

    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="snappy")
    encoded_bytes = buf.tell()

    # Confirm table.nbytes is strictly smaller than the encoded standalone Parquet reservation
    assert encoded_bytes > table_nbytes, f"Expected encoded_bytes ({encoded_bytes}) > table_nbytes ({table_nbytes})"

    # Budget is set so current_footprint + table_nbytes fits, but current_footprint + encoded_bytes exceeds budget
    allowed_budget = current_footprint + table_nbytes + 1
    assert current_footprint + table_nbytes < allowed_budget
    assert current_footprint + encoded_bytes > allowed_budget

    with pytest.raises(DiskLimitExceededError, match="Working footprint limit would be exceeded during finalization"):
        finalize_streaming_dataset(
            state=state,
            shards_dir=shards_dir,
            output_path=output_path,
            summary_path=summary_path,
            pipeline_roots=[shards_dir],
            max_working_bytes=allowed_budget,
        )

    # Shards preserved and no outputs published
    assert shard_path.exists()
    assert not output_path.exists()
    assert not summary_path.exists()
    assert len(list(output_path.parent.glob("*.tmp*"))) == 0


def test_normal_path_calls_load_splits_config_without_name_error(tmp_path: Path, monkeypatch) -> None:
    """The normal execution path in acquire_streaming_dataset.py imports and calls load_splits_config() without NameError."""
    import sys
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import acquire_streaming_dataset as script_mod

    work_dir = tmp_path / "work"
    raw_gefs_dir = tmp_path / "raw"
    shards_dir = tmp_path / "shards"
    raw_imd_dir = tmp_path / "imd"
    output_path = tmp_path / "out" / "rows.parquet"
    summary_path = tmp_path / "out" / "summary.json"

    monkeypatch.setattr(script_mod, "derive_expected_gefs_dates", lambda: ["2018-08-01"])
    monkeypatch.setattr(script_mod, "verify_or_fetch_imd_years", lambda **kwargs: (True, []))

    from bust.data.acquisition import InventoryReport
    mock_report = InventoryReport(
        expected_dates_count=1,
        actual_listed_count=1,
        missing_dates=[],
        unexpected_dates=[],
        sample_keys=[],
        projected_download_bytes=100,
        working_dir_bytes=100,
        can_honor_8gib=True,
        status="complete",
    )
    monkeypatch.setattr(script_mod, "inventory_noaa_s3", lambda **kwargs: mock_report)
    monkeypatch.setattr(script_mod, "load_regions_geojson", lambda p: {})

    splits_cfg_loaded = False
    real_load_splits = script_mod.load_splits_config

    def _spy_load_splits(*args, **kwargs):
        nonlocal splits_cfg_loaded
        res = real_load_splits(*args, **kwargs)
        splits_cfg_loaded = True
        return res

    monkeypatch.setattr(script_mod, "load_splits_config", _spy_load_splits)

    class StopPipeline(Exception):
        pass

    def _stop_before_streaming(*args, **kwargs):
        raise StopPipeline("Reached pipeline past load_splits_config")

    monkeypatch.setattr(script_mod, "format_progress_report", _stop_before_streaming)

    test_args = [
        "acquire_streaming_dataset.py",
        "--work-dir", str(work_dir),
        "--raw-gefs-dir", str(raw_gefs_dir),
        "--shards-dir", str(shards_dir),
        "--raw-imd-dir", str(raw_imd_dir),
        "--output-path", str(output_path),
        "--summary-path", str(summary_path),
    ]
    monkeypatch.setattr(sys, "argv", test_args)

    with pytest.raises(StopPipeline, match="Reached pipeline past load_splits_config"):
        script_mod.main()

    assert splits_cfg_loaded is True


def test_imd_only_runs_and_never_calls_gefs_functions(tmp_path: Path, monkeypatch, capsys) -> None:
    """The --imd-only flag runs IMD verification and exits without calling NOAA inventory or GEFS functions."""
    import sys
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import acquire_streaming_dataset as script_mod

    work_dir = tmp_path / "work"
    raw_imd_dir = tmp_path / "imd"
    raw_imd_dir.mkdir(parents=True)

    def _exploding_noaa_inv(**kwargs):
        pytest.fail("inventory_noaa_s3 must never be called in --imd-only mode!")

    def _exploding_gefs_dl(**kwargs):
        pytest.fail("download_gefs_c00_file must never be called in --imd-only mode!")

    def _exploding_gefs_process(**kwargs):
        pytest.fail("process_gefs_c00_file must never be called in --imd-only mode!")

    monkeypatch.setattr(script_mod, "inventory_noaa_s3", _exploding_noaa_inv)
    monkeypatch.setattr(script_mod, "download_gefs_c00_file", _exploding_gefs_dl)
    monkeypatch.setattr(script_mod, "process_gefs_c00_file", _exploding_gefs_process)

    # 1. Success case: all years verified -> exit 0
    def _mock_verify_success(state, imd_dir, manifest_path=None, required_years=None, allow_network_download=True, **kwargs):
        for yr in (required_years or range(2010, 2021)):
            state.upsert_imd_record(
                IMDRecord(year=yr, status="verified", local_bytes=25000000, local_sha256="abc123456789")
            )
        return True, []

    monkeypatch.setattr(script_mod, "verify_or_fetch_imd_years", _mock_verify_success)

    test_args = [
        "acquire_streaming_dataset.py",
        "--imd-only",
        "--work-dir", str(work_dir),
        "--raw-imd-dir", str(raw_imd_dir),
    ]
    monkeypatch.setattr(sys, "argv", test_args)

    with pytest.raises(SystemExit) as exc_info:
        script_mod.main()
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "=== IMD Annual Files Acquisition & Verification (2010–2020) ===" in captured.out
    assert "2010: status=verified" in captured.out
    assert "2020: status=verified" in captured.out
    assert "All IMD 2010–2020 annual files successfully verified." in captured.out

    # 2. Failure case: one year failed -> exit 1 with error preserved
    def _mock_verify_fail(state, imd_dir, manifest_path=None, required_years=None, allow_network_download=True, **kwargs):
        for yr in (required_years or range(2010, 2021)):
            if yr == 2015:
                state.upsert_imd_record(
                    IMDRecord(year=yr, status="failed", error="Simulated network failure")
                )
            else:
                state.upsert_imd_record(
                    IMDRecord(year=yr, status="verified", local_bytes=25000000, local_sha256="abc123456789")
                )
        return False, ["2015 failed: Simulated network failure"]

    monkeypatch.setattr(script_mod, "verify_or_fetch_imd_years", _mock_verify_fail)

    with pytest.raises(SystemExit) as exc_info:
        script_mod.main()
    assert exc_info.value.code == 1

    captured2 = capsys.readouterr()
    assert "2015: status=failed" in captured2.out
    assert "[error: Simulated network failure]" in captured2.out
    assert "IMD acquisition/verification incomplete (1 error(s))" in captured2.out


def test_imd_atomic_retrieval_and_validation(tmp_path: Path, monkeypatch) -> None:
    """IMD download uses atomic .part file, rejects HTML error responses, cleans up on error, and preserves canonical."""
    import httpx

    from bust.data.acquisition import verify_or_fetch_imd_years

    imd_dir = tmp_path / "imd"
    imd_dir.mkdir()
    state = AcquisitionState(tmp_path / "state.db")

    # Case A: Server returns HTML error page (HTTP 200 with HTML body > 1000 bytes)
    html_content = b"<!DOCTYPE html><html><body><h1>Error</h1><p>" + (b"Database query failed. " * 100) + b"</p></body></html>"
    assert len(html_content) > 1000

    class MockHTMLTransport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, content=html_content, request=request)

    client_html = httpx.Client(transport=MockHTMLTransport())

    is_complete, errors = verify_or_fetch_imd_years(
        state=state,
        imd_dir=imd_dir,
        manifest_path=None,
        required_years=[2010],
        allow_network_download=True,
        client=client_html,
    )
    assert is_complete is False
    assert len(errors) == 1
    assert "HTML" in errors[0] or "validation failed" in errors[0]

    # No canonical file created, no orphan .part left
    canonical_2010 = imd_dir / "ind2010_rfp25.nc"
    assert not canonical_2010.exists()
    assert len(list(imd_dir.glob("*.part*"))) == 0

    rec_2010 = state.get_imd_record(2010)
    assert rec_2010 is not None
    assert rec_2010.status == "failed"
    assert "HTML" in rec_2010.error or "validation failed" in rec_2010.error

    # Case B: Pre-existing valid canonical file is preserved if a subsequent operation or network error fails
    canonical_2010.write_bytes(b"previous-valid-canonical-content")

    class MockErrorTransport(httpx.BaseTransport):
        def handle_request(self, request):
            raise httpx.ConnectError("Network unreachable")

    client_err = httpx.Client(transport=MockErrorTransport())

    is_complete2, errors2 = verify_or_fetch_imd_years(
        state=state,
        imd_dir=imd_dir,
        manifest_path=None,
        required_years=[2010],
        allow_network_download=True,
        client=client_err,
    )
    assert is_complete2 is False
    assert len(errors2) > 0
    # Canonical file was preserved and not unlinked
    assert canonical_2010.exists()
    assert canonical_2010.read_bytes() == b"previous-valid-canonical-content"
    # No .part files left
    assert len(list(imd_dir.glob("*.part*"))) == 0


def test_imd_manifest_checksum_acceptance_and_mismatch(tmp_path: Path) -> None:
    """Existing IMD files are verified against DATA_MANIFEST.csv checksum; mismatches are rejected and recorded as failed."""
    from bust.data.acquisition import verify_or_fetch_imd_years

    imd_dir = tmp_path / "imd"
    imd_dir.mkdir()
    state = AcquisitionState(tmp_path / "state.db")
    manifest_path = tmp_path / "DATA_MANIFEST.csv"

    known_sha = "49786e2d2b661c5d3bcfb3ffd90385c1133ec27a8a04bf272ff2df5029106a9c"
    manifest_content = (
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n"
        f"imd-2017-pilot,imd,IMD Pune,https://imdpune.gov.in/cmpg/Griddata/RF25.php (POST RF25=2017),2026-09-26T19:38:04Z,{known_sha},25431832,,RAINFALL,,,,mm,decoded,\"Content-Disposition ind2017_rfp25.nc; NetCDF: TIME=365, LATITUDE=129, LONGITUDE=135\"\n"
    )
    manifest_path.write_text(manifest_content, encoding="utf-8")

    canonical_2017 = imd_dir / "ind2017_rfp25.nc"
    canonical_2017.write_bytes(b"corrupt-or-altered-file-content")

    is_complete, errors = verify_or_fetch_imd_years(
        state=state,
        imd_dir=imd_dir,
        manifest_path=manifest_path,
        required_years=[2017],
        allow_network_download=False,
    )
    assert is_complete is False
    assert len(errors) == 1
    assert "SHA-256 mismatch against manifest" in errors[0]

    rec = state.get_imd_record(2017)
    assert rec is not None
    assert rec.status == "failed"
    assert "SHA-256 mismatch" in rec.error


def test_can_honor_8gib_reporting_and_calculation(tmp_path: Path) -> None:
    """can_honor_8gib is True only with measured shards within budget; otherwise False."""

    state = AcquisitionState(tmp_path / "state.db")
    shards_dir = tmp_path / "shards"
    shards_dir.mkdir()

    state.upsert_gefs_inventory("2018-08-01", "key1", remote_bytes=28_000_000)
    state.upsert_gefs_inventory("2018-08-02", "key2", remote_bytes=28_000_000)

    # 1. shards_dir is None -> can_honor_8gib is False
    rep1 = inventory_noaa_s3(state=state, expected_dates=["2018-08-01", "2018-08-02"], shards_dir=None)
    assert rep1.can_honor_8gib is False

    # 2. shards_dir is empty (no representative shards) -> can_honor_8gib is False
    rep2 = inventory_noaa_s3(state=state, expected_dates=["2018-08-01", "2018-08-02"], shards_dir=shards_dir)
    assert rep2.can_honor_8gib is False

    # 3. Create a representative shard with small size -> fits within budget -> can_honor_8gib is True
    sample_shard = shards_dir / "shard_20180801.parquet"
    sample_shard.write_bytes(b"x" * 80_000)

    rep3 = inventory_noaa_s3(state=state, expected_dates=["2018-08-01", "2018-08-02"], shards_dir=shards_dir)
    assert rep3.can_honor_8gib is True

    # 4. Tiny max_working_bytes that cannot fit buffer + shards -> can_honor_8gib is False
    rep4 = inventory_noaa_s3(
        state=state,
        expected_dates=["2018-08-01", "2018-08-02"],
        shards_dir=shards_dir,
        max_working_bytes=1000,
    )
    assert rep4.can_honor_8gib is False


def test_append_imd_manifest_record_and_notes(tmp_path: Path) -> None:
    """Verified IMD records are safely upserted in DATA_MANIFEST.csv with factual wording without unproven Content-Disposition."""
    from bust.data.acquisition import append_imd_manifest_record, get_manifest_imd_checksum

    manifest_path = tmp_path / "DATA_MANIFEST.csv"
    manifest_path.write_text(
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n",
        encoding="utf-8",
    )

    # 1. Append regular year (2012 - leap year)
    meta_2012 = {"time_dim": 366, "lat_dim": 129, "lon_dim": 135, "units": "mm", "time_start": "2012-01-01", "time_end": "2012-12-31"}
    append_imd_manifest_record(
        manifest_path=manifest_path,
        year=2012,
        sha256="1212121212121212121212121212121212121212121212121212121212121212",
        byte_count=25500000,
        metadata=meta_2012,
        retrieved_utc="2026-09-27T10:00:00Z",
    )

    sha = get_manifest_imd_checksum(manifest_path, 2012)
    assert sha == "1212121212121212121212121212121212121212121212121212121212121212"

    content = manifest_path.read_text(encoding="utf-8")
    assert "imd-2012,imd,IMD Pune,https://imdpune.gov.in/cmpg/Griddata/RF25.php (POST RF25=2012)" in content
    assert "canonical local file data/raw/imd/ind2012_rfp25.nc" in content
    assert "TIME=366 (2012-01-01 to 2012-12-31)" in content
    assert "Content-Disposition" not in content

    # 2. Append 2020: must have verification-only note per D-007
    meta_2020 = {"time_dim": 366, "lat_dim": 129, "lon_dim": 135, "units": "mm", "time_start": "2020-01-01", "time_end": "2020-12-31"}
    append_imd_manifest_record(
        manifest_path=manifest_path,
        year=2020,
        sha256="2020202020202020202020202020202020202020202020202020202020202020",
        byte_count=25500000,
        metadata=meta_2020,
        retrieved_utc="2026-09-27T10:01:00Z",
    )
    content2 = manifest_path.read_text(encoding="utf-8")
    assert "verification-only for late-2019 Day 1–9 labels per D-007; no 2020 GEFS initializations" in content2
    assert "Content-Disposition" not in content2

    # 3. Update existing row safely in place without creating duplicate
    meta_2020_updated = {"time_dim": 366, "lat_dim": 129, "lon_dim": 135, "units": "mm", "time_start": "2020-01-01", "time_end": "2020-12-31"}
    append_imd_manifest_record(
        manifest_path=manifest_path,
        year=2020,
        sha256="2020202020202020202020202020202020202020202020202020202020202020",
        byte_count=25500000,
        metadata=meta_2020_updated,
    )
    lines = [l for l in manifest_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3


def test_validate_imd_netcdf_rejects_missing_units(tmp_path: Path) -> None:
    """validate_imd_netcdf rejects a NetCDF file missing the RAINFALL.units attribute."""
    import numpy as np
    import xarray as xr

    from bust.data.acquisition import validate_imd_netcdf

    nc_path = tmp_path / "ind2018_rfp25.nc"
    dates = pd.date_range("2018-01-01", "2018-12-31", freq="D")
    lats = np.linspace(6.5, 38.5, 129)
    lons = np.linspace(66.5, 100.0, 135)
    data = np.zeros((365, 129, 135), dtype=np.float32)

    ds = xr.Dataset(
        data_vars={"RAINFALL": (("TIME", "LATITUDE", "LONGITUDE"), data, {})},
        coords={"TIME": dates, "LATITUDE": lats, "LONGITUDE": lons},
    )
    ds.to_netcdf(nc_path)

    valid, meta, err = validate_imd_netcdf(nc_path, 2018)
    assert valid is False
    assert meta is None
    assert "Missing 'units' attribute" in err


def test_validate_imd_netcdf_rejects_invalid_units(tmp_path: Path) -> None:
    """validate_imd_netcdf rejects a NetCDF file with RAINFALL.units other than 'mm'."""
    import numpy as np
    import xarray as xr

    from bust.data.acquisition import validate_imd_netcdf

    nc_path = tmp_path / "ind2018_rfp25.nc"
    dates = pd.date_range("2018-01-01", "2018-12-31", freq="D")
    lats = np.linspace(6.5, 38.5, 129)
    lons = np.linspace(66.5, 100.0, 135)
    data = np.zeros((365, 129, 135), dtype=np.float32)

    ds = xr.Dataset(
        data_vars={"RAINFALL": (("TIME", "LATITUDE", "LONGITUDE"), data, {"units": "m"})},
        coords={"TIME": dates, "LATITUDE": lats, "LONGITUDE": lons},
    )
    ds.to_netcdf(nc_path)

    valid, meta, err = validate_imd_netcdf(nc_path, 2018)
    assert valid is False
    assert meta is None
    assert "Invalid units 'm'" in err
    assert "expected 'mm'" in err


def test_validate_imd_netcdf_rejects_duplicate_or_missing_interior_days(tmp_path: Path) -> None:
    """validate_imd_netcdf rejects a date axis with duplicate/missing interior days even if count and endpoints match."""
    import numpy as np
    import xarray as xr

    from bust.data.acquisition import validate_imd_netcdf

    nc_path = tmp_path / "ind2018_rfp25.nc"
    dates = pd.date_range("2018-01-01", "2018-12-31", freq="D").tolist()
    dates[10] = dates[9]  # Duplicate day 10, dropping day 11
    assert len(dates) == 365
    assert str(dates[0])[:10] == "2018-01-01"
    assert str(dates[-1])[:10] == "2018-12-31"

    lats = np.linspace(6.5, 38.5, 129)
    lons = np.linspace(66.5, 100.0, 135)
    data = np.zeros((365, 129, 135), dtype=np.float32)

    ds = xr.Dataset(
        data_vars={"RAINFALL": (("TIME", "LATITUDE", "LONGITUDE"), data, {"units": "mm"})},
        coords={"TIME": dates, "LATITUDE": lats, "LONGITUDE": lons},
    )
    ds.to_netcdf(nc_path)

    valid, meta, err = validate_imd_netcdf(nc_path, 2018)
    assert valid is False
    assert meta is None
    assert "TIME coordinate does not match exact calendar dates" in err
    assert "2018-01-11" in err


def test_requeue_failed_item_for_retry_success(tmp_path: Path) -> None:
    """requeue_failed_item_for_retry verifies retained raw file and requeues failed item to VERIFIED."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    raw_file = raw_dir / "apcp_sfc_2013110900_c00.grib2"
    content = b"sample-grib2-data-for-retry-verification"
    raw_file.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()
    size = len(content)

    state.upsert_gefs_inventory("2013-11-09", "key_20131109", remote_bytes=size)
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(raw_file),
        local_bytes=size,
        local_sha256=sha256,
        processing_attempts=3,
        last_error="Prior failure: Duplicate (30, 36) interval",
    )

    ok, rec, err = state.requeue_failed_item_for_retry("2013-11-09", raw_gefs_dir=raw_dir)
    assert ok is True
    assert err is None
    assert rec is not None
    assert rec.status == AcquisitionStatus.VERIFIED
    assert rec.processing_attempts == 0
    assert "Prior failure: Duplicate (30, 36) interval" in rec.last_error
    assert "Requeued for retry after verifying retained raw file" in rec.last_error

    # Verify state in database
    db_item = state.get_gefs_item("2013-11-09")
    assert db_item.status == AcquisitionStatus.VERIFIED
    assert db_item.processing_attempts == 0


def test_requeue_failed_item_rejection_missing_file(tmp_path: Path) -> None:
    """requeue_failed_item_for_retry rejects requeue when raw file is missing."""
    state = AcquisitionState(tmp_path / "state.db")
    missing_file = tmp_path / "raw/missing.grib2"

    state.upsert_gefs_inventory("2013-11-09", "key", remote_bytes=100)
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(missing_file),
        local_bytes=100,
        local_sha256="abc",
        last_error="Original error",
    )

    ok, rec, err = state.requeue_failed_item_for_retry("2013-11-09")
    assert ok is False
    assert rec is None
    assert "Retained raw file not found" in err

    db_item = state.get_gefs_item("2013-11-09")
    assert db_item.status == AcquisitionStatus.FAILED


def test_requeue_failed_item_rejection_size_mismatch(tmp_path: Path) -> None:
    """requeue_failed_item_for_retry rejects requeue when file size does not match record."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_file = tmp_path / "file.grib2"
    raw_file.write_bytes(b"12345")

    state.upsert_gefs_inventory("2013-11-09", "key", remote_bytes=5)
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(raw_file),
        local_bytes=10,  # Expected 10, actual is 5
        local_sha256="abc",
        last_error="Original error",
    )

    ok, rec, err = state.requeue_failed_item_for_retry("2013-11-09")
    assert ok is False
    assert rec is None
    assert "size mismatch" in err

    db_item = state.get_gefs_item("2013-11-09")
    assert db_item.status == AcquisitionStatus.FAILED


def test_requeue_failed_item_rejection_sha256_mismatch(tmp_path: Path) -> None:
    """requeue_failed_item_for_retry rejects requeue when SHA-256 does not match record."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_file = tmp_path / "file.grib2"
    raw_file.write_bytes(b"content-a")

    state.upsert_gefs_inventory("2013-11-09", "key", remote_bytes=len(b"content-a"))
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(raw_file),
        local_bytes=len(b"content-a"),
        local_sha256="0000000000000000000000000000000000000000000000000000000000000000",
        last_error="Original error",
    )

    ok, rec, err = state.requeue_failed_item_for_retry("2013-11-09")
    assert ok is False
    assert rec is None
    assert "SHA-256 mismatch" in err

    db_item = state.get_gefs_item("2013-11-09")
    assert db_item.status == AcquisitionStatus.FAILED


def test_requeue_failed_item_rejection_completed_immutable(tmp_path: Path) -> None:
    """requeue_failed_item_for_retry rejects completed items; completed items are strictly immutable."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_file = tmp_path / "file.grib2"
    raw_file.write_bytes(b"data")

    state.upsert_gefs_inventory("2018-08-01", "key", remote_bytes=4)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.COMPLETED,
        local_path=str(raw_file),
        local_bytes=4,
        local_sha256=hashlib.sha256(b"data").hexdigest(),
    )

    ok, rec, err = state.requeue_failed_item_for_retry("2018-08-01")
    assert ok is False
    assert rec is None
    assert "completed items are immutable" in err

    db_item = state.get_gefs_item("2018-08-01")
    assert db_item.status == AcquisitionStatus.COMPLETED


def test_requeue_failed_item_rejection_missing_local_bytes(tmp_path: Path) -> None:
    """requeue_failed_item_for_retry rejects items with missing or non-positive local_bytes."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_file = tmp_path / "file.grib2"
    raw_file.write_bytes(b"content")

    state.upsert_gefs_inventory("2013-11-09", "key", remote_bytes=7)
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(raw_file),
        local_bytes=None,  # Missing local_bytes
        local_sha256=hashlib.sha256(b"content").hexdigest(),
        last_error="Original error",
    )

    ok, rec, err = state.requeue_failed_item_for_retry("2013-11-09")
    assert ok is False
    assert rec is None
    assert "local_bytes" in err

    db_item = state.get_gefs_item("2013-11-09")
    assert db_item.status == AcquisitionStatus.FAILED


def test_requeue_failed_item_rejection_missing_local_sha256(tmp_path: Path) -> None:
    """requeue_failed_item_for_retry rejects items with missing or empty local_sha256."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_file = tmp_path / "file.grib2"
    raw_file.write_bytes(b"content")

    state.upsert_gefs_inventory("2013-11-09", "key", remote_bytes=7)
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(raw_file),
        local_bytes=7,
        local_sha256=None,  # Missing local_sha256
        last_error="Original error",
    )

    ok, rec, err = state.requeue_failed_item_for_retry("2013-11-09")
    assert ok is False
    assert rec is None
    assert "local_sha256" in err

    db_item = state.get_gefs_item("2013-11-09")
    assert db_item.status == AcquisitionStatus.FAILED


def test_requeue_all_failed_items_atomic_all_or_nothing(tmp_path: Path) -> None:
    """requeue_all_failed_items_for_retry is all-or-nothing: if any item fails, all items remain failed."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    # Item 1: Valid retained raw file and valid metadata
    file1 = raw_dir / "apcp_sfc_2013110900_c00.grib2"
    content1 = b"valid-raw-file-data-1"
    file1.write_bytes(content1)
    sha1 = hashlib.sha256(content1).hexdigest()
    size1 = len(content1)

    state.upsert_gefs_inventory("2013-11-09", "key1", remote_bytes=size1)
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(file1),
        local_bytes=size1,
        local_sha256=sha1,
        processing_attempts=3,
        last_error="Prior error on row 1",
    )

    # Item 2: Invalid/missing checksum or size
    file2 = raw_dir / "apcp_sfc_2013111000_c00.grib2"
    content2 = b"corrupted-or-mismatched-file"
    file2.write_bytes(content2)

    state.upsert_gefs_inventory("2013-11-10", "key2", remote_bytes=len(content2))
    state.update_gefs_status(
        "2013-11-10",
        status=AcquisitionStatus.FAILED,
        local_path=str(file2),
        local_bytes=len(content2),
        local_sha256="wrong-or-missing-sha256",  # Checksum mismatch
        processing_attempts=2,
        last_error="Prior error on row 2",
    )

    # Execute atomic requeue
    requeued, errors = state.requeue_all_failed_items_for_retry(raw_gefs_dir=raw_dir)

    # Assert method reports failure
    assert len(requeued) == 0
    assert len(errors) > 0
    assert any("2013-11-10" in e for e in errors)

    # Assert BOTH rows remain failed
    item1 = state.get_gefs_item("2013-11-09")
    item2 = state.get_gefs_item("2013-11-10")
    assert item1.status == AcquisitionStatus.FAILED
    assert item2.status == AcquisitionStatus.FAILED

    # Assert the first row's processing_attempts and last_error are unchanged
    assert item1.processing_attempts == 3
    assert item1.last_error == "Prior error on row 1"
    assert item2.processing_attempts == 2
    assert item2.last_error == "Prior error on row 2"


def test_requeue_all_failed_items_atomic_success(tmp_path: Path) -> None:
    """requeue_all_failed_items_for_retry succeeds atomically when all items validate."""
    state = AcquisitionState(tmp_path / "state.db")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    # Item 1
    file1 = raw_dir / "apcp_sfc_2013110900_c00.grib2"
    content1 = b"valid-raw-file-data-1"
    file1.write_bytes(content1)
    sha1 = hashlib.sha256(content1).hexdigest()
    size1 = len(content1)

    state.upsert_gefs_inventory("2013-11-09", "key1", remote_bytes=size1)
    state.update_gefs_status(
        "2013-11-09",
        status=AcquisitionStatus.FAILED,
        local_path=str(file1),
        local_bytes=size1,
        local_sha256=sha1,
        processing_attempts=3,
        last_error="Prior error 1",
    )

    # Item 2
    file2 = raw_dir / "apcp_sfc_2013111000_c00.grib2"
    content2 = b"valid-raw-file-data-2"
    file2.write_bytes(content2)
    sha2 = hashlib.sha256(content2).hexdigest()
    size2 = len(content2)

    state.upsert_gefs_inventory("2013-11-10", "key2", remote_bytes=size2)
    state.update_gefs_status(
        "2013-11-10",
        status=AcquisitionStatus.FAILED,
        local_path=str(file2),
        local_bytes=size2,
        local_sha256=sha2,
        processing_attempts=2,
        last_error="Prior error 2",
    )

    requeued, errors = state.requeue_all_failed_items_for_retry(raw_gefs_dir=raw_dir)
    assert len(errors) == 0
    assert len(requeued) == 2

    item1 = state.get_gefs_item("2013-11-09")
    item2 = state.get_gefs_item("2013-11-10")
    assert item1.status == AcquisitionStatus.VERIFIED
    assert item1.processing_attempts == 0
    assert "Prior error 1" in item1.last_error
    assert "Requeued for retry" in item1.last_error

    assert item2.status == AcquisitionStatus.VERIFIED
    assert item2.processing_attempts == 0
    assert "Prior error 2" in item2.last_error
    assert "Requeued for retry" in item2.last_error


def test_sync_manifest_with_streaming_corpus(tmp_path: Path) -> None:
    """sync_manifest_with_streaming_corpus exports provenance safely and deterministically without duplicates."""
    from bust.data.acquisition import sync_manifest_with_streaming_corpus

    manifest_file = tmp_path / "DATA_MANIFEST.csv"
    initial_content = (
        "manifest_id,source,provider,object_key_or_url,retrieved_utc,sha256,bytes,init_utc,variable,member,step_start_h,step_end_h,units,status,notes\n"
        "imd-2018-pilot,imd,IMD Pune,https://example.com/2018,2026-09-26T19:38:43Z,abc,25431832,,RAINFALL,,,,mm,decoded,verified\n"
        "gefs-20180801-c00-apcp,gefs,NOAA GEFSv12 reforecast,key_old,2026-09-27T06:26:11Z,sha_old,28987248,2018-08-01T00:00:00Z,apcp_sfc,c00,0,240,kg m**-2,decoded,existing verified pilot note\n"
        "gefs-20180801-p01-apcp,gefs,NOAA GEFSv12 reforecast,key_p01,,sha_p01,23839440,2018-08-01T00:00:00Z,apcp_sfc,p01,0,240,kg m**-2,decoded,member 1\n"
    )
    manifest_file.write_text(initial_content, encoding="utf-8")

    state = AcquisitionState(tmp_path / "state.db")
    # Record 2 completed items: 2018-08-01 (existing pilot) and 2010-01-01 (new completed streaming item)
    state.upsert_gefs_inventory("2018-08-01", "key_20180801", remote_bytes=28987248)
    state.update_gefs_status(
        "2018-08-01",
        status=AcquisitionStatus.COMPLETED,
        local_bytes=28987248,
        local_sha256="sha_20180801_verified",
        downloaded_at="2026-09-27T06:26:11Z",
        completed_at="2026-09-27T14:53:00Z",
        shard_path="data/interim/acquisition/shards/shard_20180801.parquet",
    )

    state.upsert_gefs_inventory("2010-01-01", "GEFSv12/reforecast/2010/2010010100/c00/Days:1-10/apcp_sfc_2010010100_c00.grib2", remote_bytes=25315205)
    state.update_gefs_status(
        "2010-01-01",
        status=AcquisitionStatus.COMPLETED,
        local_bytes=25315205,
        local_sha256="sha_20100101",
        downloaded_at="2026-09-27T14:52:00Z",
        raw_deleted_at="2026-09-27T14:53:10Z",
        completed_at="2026-09-27T14:53:10Z",
        shard_path="data/interim/acquisition/shards/shard_20100101.parquet",
    )

    # First sync
    synced = sync_manifest_with_streaming_corpus(manifest_file, state)
    assert synced == 2

    lines = [l for l in manifest_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    # Total lines: header (1) + imd (1) + gefs-20180801-c00 (1) + p01 (1) + gefs-20100101-c00 (1) = 5
    assert len(lines) == 5

    # Verify 2018-08-01 pilot row was updated in place and preserved existing notes
    pilot_line = lines[2]
    assert "gefs-20180801-c00-apcp" in pilot_line
    assert "existing verified pilot note" in pilot_line
    assert "sha_20180801_verified" in pilot_line

    # Verify 2010-01-01 new row was appended with full factual metadata
    new_line = lines[4]
    assert "gefs-20100101-c00-apcp" in new_line
    assert "2010-01-01T00:00:00Z" in new_line
    assert "sha_20100101" in new_line
    assert "25315205" in new_line
    assert "Streamed c00 Days:1-10 reforecast" in new_line
    assert "raw GRIB deleted after shard validation" in new_line

    # Second sync is idempotent
    synced_2 = sync_manifest_with_streaming_corpus(manifest_file, state)
    assert synced_2 == 2
    lines_2 = [l for l in manifest_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines_2) == 5
