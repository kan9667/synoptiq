# Data workspace

This directory is intentionally included as an empty workspace for people who
want to reproduce the data pipeline or train a new model. The large source and
intermediate files are excluded from GitHub.

| Directory | Purpose |
| --- | --- |
| `raw/` | Downloaded GEFSv12 forecast and IMD rainfall source files. |
| `interim/` | Temporary decoded and aligned processing outputs. |
| `processed/` | Rebuilt model-ready datasets. |

You do **not** need this directory's contents to run the historical replay
dashboard. The reviewed replay data used by the API is bundled separately in
`../artifacts/replay/reduced_c00_replay.json.gz`.

Run the replay with:

```bash
make web
make api
```

Only populate this workspace when acquiring source data or regenerating the
dataset, model, or replay artifact.
