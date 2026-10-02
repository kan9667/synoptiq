"""Build the split-aware empirical rows.parquet dataset with train-only thresholds."""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow.parquet as pq

from _run_context import ROOT, emit
from bust.data.dataset import build_dataset, check_corpus_coverage, get_manifest_fingerprint


def main() -> None:
    manifest_id = get_manifest_fingerprint(ROOT / "DATA_MANIFEST.csv")
    output_path = "data/processed/rows.parquet"
    split = "2010-2015/2016-2017/2018-2019"

    emit("dataset", output_path, manifest_id=manifest_id, split=split)

    # The disk-safe streaming acquisition finalizes rows.parquet and deletes raw
    # GRIB files only after every per-date shard validates.  A later named
    # `make dataset` must report that verified final artifact rather than falsely
    # attempting to rebuild from intentionally deleted raw inputs.
    finalized_path = ROOT / output_path
    summary_path = ROOT / "artifacts/metrics/dataset_summary.json"
    if finalized_path.exists() and summary_path.exists():
        metadata = pq.ParquetFile(finalized_path).metadata
        if metadata.num_rows <= 0:
            print("⚠️ Blocked — D1-07: Existing finalized rows.parquet has no rows.")
            sys.exit(1)
        print("status=already_finalized_streaming_dataset")
        print(f"row_count={metadata.num_rows}")
        print(f"summary_path={summary_path}")
        print("note=Raw GEFS files were intentionally deleted only after validated streaming finalization.")
        return

    # Preflight: audit whether full 2010–2019 real corpus exists
    report = check_corpus_coverage(ROOT / "data/raw")

    if not report.is_complete:
        print(f"⚠️ Blocked — D1-07: {report.blocking_reason}")
        print(f"Next safe action: {report.next_safe_action}")
        print("Never construct rows.parquet from pilots, incomplete years, invented dates, or substituted sources.")
        sys.exit(1)

    # Real build pipeline (executes only when full 2010-2019 corpus is complete):
    built_path = build_dataset(
        raw_dir=ROOT / "data/raw",
        output_path=ROOT / output_path,
        manifest_path=ROOT / "DATA_MANIFEST.csv",
        splits_path=ROOT / "config/splits.yaml",
        regions_path=ROOT / "config/regions_2deg.geojson",
        summary_path=ROOT / "artifacts/metrics/dataset_summary.json",
        floor_mm=10.0,
    )
    print(f"Dataset build complete: {built_path}")


if __name__ == "__main__":
    main()
