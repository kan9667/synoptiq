# Artifacts

This directory holds the generated outputs of the Synoptiq pipeline —
trained models, evaluation evidence, and the frozen historical replay bundle.
It is structured into three sub-directories, each with a distinct commit
policy.

---

## Directory overview

```
artifacts/
├── replay/     ← PARTIALLY committed (replay bundle is included in the repo)
├── metrics/    ← gitignored (generated locally by the evaluation pipeline)
└── model/      ← gitignored (generated locally by the training pipeline)
```

---

## `replay/` — Historical replay bundle

| File | In repo? | Description |
| --- | :---: | --- |
| `reduced_c00_replay.json.gz` | ✅ Yes | Compressed frozen replay for 3 held-out initialisation dates. Loaded directly by the API at startup. |
| `reduced_c00_replay_metadata.json` | ✅ Yes | Replay provenance: model ID, manifest fingerprint, Booster SHA-256, split, seed, calibration method, and selected dates. |
| `reduced_c00_replay.json` | ❌ gitignored | Uncompressed source produced by `make replay`. Regenerated locally; too large to commit. |

**Why the two files above are committed:** a normal `git clone` must be enough
to run the dashboard without downloading the 98 GB raw data archive. The
compressed bundle (≈ 423 KB) satisfies that requirement. Its content is
reviewed and pinned to a known Booster SHA-256 before each release.

**Replay covers:** `2018-01-01`, `2019-01-01`, `2019-12-31` — 3 dates × 112
regions × 10 leads = **3,360 records**.

To regenerate the replay from the trained model:

```bash
make replay
```

---

## `metrics/` — Evaluation results

> **This folder appears empty on GitHub.** All files inside it are gitignored.

The following files exist locally after running `make evaluate-candidate`:

| File | Description |
| --- | --- |
| `climatology_baseline_evaluation.json` | Held-out Brier score for the train-only climatology comparator across 427,050 eligible 2018–2019 rows. |
| `reduced_c00_validation.json` | Candidate vs. climatology comparison on the 427,635 validation rows; includes an earlier-case retrieval example. |
| `reduced_c00_evaluation.json` | Calibrated and uncalibrated held-out Brier, reliability bin data, and bust prevalence for the frozen candidate. |
| `dataset_summary.json` | Row counts, split membership, missingness, and label statistics for the aligned dataset. |

**Why gitignored:** these files are derived outputs of `make evaluate-candidate`.
Anyone with the raw training corpus can reproduce them exactly using the frozen
model and calibrator identities recorded in `model/`. Committing them would
also risk silently stale numbers if the evaluation script were re-run without
a corresponding model update.

To regenerate locally:

```bash
make evaluate-candidate
```

Key headline results from `reduced_c00_evaluation.json`:

| Metric | Value |
| --- | ---: |
| Held-out bust prevalence | 4.743% |
| Climatology Brier score | 0.04359 |
| Calibrated candidate Brier score | 0.03049 |
| Brier skill vs. climatology | **30.1%** |

---

## `model/` — Trained model and calibrator

> **This folder appears empty on GitHub.** All files inside it are gitignored.

The following files exist locally after running `make candidate`:

| File | Description |
| --- | --- |
| `reduced_c00_candidate.txt` | Serialised LightGBM Booster (≈ 354 KB). Loaded by the replay exporter and verified against the SHA-256 stored in `replay/reduced_c00_replay_metadata.json`. |
| `reduced_c00_candidate.json` | Frozen hyperparameters, feature schema, category mappings, and training configuration. |
| `reduced_c00_frozen_run.json` | Run identity: manifest fingerprint, split, seed, code commit, and output paths. |
| `reduced_c00_calibrator.json` | Platt/sigmoid calibrator fitted on the 2016–2017 validation set only. |
| `climatology_baseline.json` | Train-only bust-frequency lookup by region, season, and lead bucket (390 groups). |

**Why gitignored:** model files are produced by `make candidate` and require
the local aligned Parquet dataset (~60 MB), which is itself derived from the
98 GB raw GEFS/IMD source corpus. Neither the raw corpus nor the Parquet is
committed to the repository. Storing binary model files in git also pollutes
repository history with large blobs that cannot be meaningfully diffed.

**Identity verification:** the replay service checks the Booster SHA-256 at
startup. If the local `reduced_c00_candidate.txt` does not match the hash in
`replay/reduced_c00_replay_metadata.json`, the service refuses to start —
ensuring no silent model mismatch between a locally retrained booster and the
committed replay bundle.

To retrain locally (requires the aligned dataset):

```bash
make candidate           # train and freeze the reduced c00 model
make evaluate-candidate  # calibrate and evaluate against held-out data
make replay              # export and compress the frozen replay bundle
```

---

## Reproducing from scratch

The full pipeline, from raw sources to a running dashboard:

```
make dataset             # aligned Parquet — requires raw GEFS + IMD corpus (~98 GB)
make candidate           # → artifacts/model/
make evaluate-candidate  # → artifacts/metrics/
make replay              # → artifacts/replay/reduced_c00_replay.json(.gz)
make api                 # starts the replay service at http://127.0.0.1:8000
```

A fresh clone skips all of the above and serves the committed
`reduced_c00_replay.json.gz` directly. See the [root README](../README.md)
for quick-start instructions.

---

## Commit policy summary

| Path | Committed | Reason |
| --- | :---: | --- |
| `replay/reduced_c00_replay.json.gz` | ✅ | Makes a fresh clone instantly runnable |
| `replay/reduced_c00_replay_metadata.json` | ✅ | Pinned provenance for the committed bundle |
| `replay/reduced_c00_replay.json` | ❌ | Large uncompressed source; regenerable via `make replay` |
| `metrics/*.json` | ❌ | Generated outputs; reproducible from the frozen model |
| `model/*.json` / `model/*.txt` | ❌ | Generated outputs; require the local corpus to produce |
| `*/.gitkeep` | ✅ | Preserves directory structure in git when contents are gitignored |
