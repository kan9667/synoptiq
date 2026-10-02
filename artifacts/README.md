# Artifacts

- **`replay/`** — contains the committed replay bundle (`reduced_c00_replay.json.gz`) that makes the dashboard work on a fresh clone.
- **`metrics/`** — empty on GitHub; holds evaluation JSON files generated locally by `make evaluate-candidate`.
- **`model/`** — empty on GitHub; holds the trained LightGBM model and calibrator generated locally by `make candidate`.

`metrics/` and `model/` are gitignored because their contents are derived from the raw 98 GB training corpus which is not committed.
