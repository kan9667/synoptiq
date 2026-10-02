"""Export a guarded, small real held-out historical replay asset."""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

from bust.api.replay_export import ReplayInputs, export_replay_asset
from bust.data.dataset import get_manifest_fingerprint
from bust.model.evaluate import FROZEN_SPLIT_ID

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/processed/rows.parquet")
    parser.add_argument("--model", type=Path, default=ROOT / "artifacts/model/reduced_c00_candidate.json")
    parser.add_argument("--frozen-run", type=Path, default=ROOT / "artifacts/model/reduced_c00_frozen_run.json")
    parser.add_argument("--calibrator", type=Path, default=ROOT / "artifacts/model/reduced_c00_calibrator.json")
    parser.add_argument("--evaluation", type=Path, default=ROOT / "artifacts/metrics/reduced_c00_evaluation.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "DATA_MANIFEST.csv")
    parser.add_argument("--regions", type=Path, default=ROOT / "config/regions_2deg.geojson")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/replay/reduced_c00_replay.json")
    parser.add_argument(
        "--metadata-output",
        type=Path,
        default=ROOT / "artifacts/replay/reduced_c00_replay_metadata.json",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--replace", action="store_true", help="Explicitly allow replacing generated replay artifacts.")
    args = parser.parse_args()
    compressed_output = args.output.with_suffix(args.output.suffix + ".gz")

    inputs = ReplayInputs(
        dataset_path=args.dataset,
        candidate_model_path=args.model,
        frozen_run_path=args.frozen_run,
        calibrator_path=args.calibrator,
        evaluation_path=args.evaluation,
        manifest_path=args.manifest,
        regions_path=args.regions,
    )
    try:
        if compressed_output.exists() and not args.replace:
            raise FileExistsError(
                f"Refusing to overwrite replay artifact: {compressed_output}. Use --replace to allow."
            )
        result = export_replay_asset(
            inputs=inputs,
            replay_output_path=args.output,
            metadata_output_path=args.metadata_output,
            replace=args.replace,
            seed=args.seed,
            repo_root=ROOT,
        )
        compressed_output.write_bytes(gzip.compress(args.output.read_bytes(), mtime=0))
    except (FileExistsError, FileNotFoundError, ValueError, OSError, KeyError) as exc:
        print("command=replay")
        print(f"manifest_id={get_manifest_fingerprint(args.manifest)}")
        print(f"split={FROZEN_SPLIT_ID}")
        print(f"seed={args.seed}")
        print(f"output_path={args.output}")
        print("status=blocked")
        sys.stderr.write(f"Replay export blocked: {exc}\n")
        raise SystemExit(1) from exc

    print("command=replay")
    print(f"manifest_id={result['manifest_id']}")
    print(f"git_commit={result['git_commit']}")
    print(f"split={result['split']}")
    print(f"seed={result['seed']}")
    print(f"output_path={args.output}")
    print(f"compressed_output={compressed_output}")
    print(f"metadata_output={args.metadata_output}")
    print("status=historical_replay")
    print(f"selected_inits={','.join(result['selected_inits'])}")
    print(f"row_count={result['row_count']}")
    print(f"feature_count={result['feature_count']}")
    print(f"booster_sha256={result['booster_sha256']}")


if __name__ == "__main__":
    main()
