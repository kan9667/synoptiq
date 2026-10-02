# Why is this folder empty?

This folder is kept in the repository so readers can see where the source data
would live when Synoptiq is rebuilt from scratch. The large weather-data files
are intentionally not included in GitHub.

| Folder | Contains |
| --- | --- |
| raw/ | Original GEFSv12 forecast and IMD rainfall files. |
| interim/ | Temporary decoded and aligned data. |
| processed/ | Rebuilt datasets used for modelling. |

You do not need any of these files to view the included historical replay. The
dashboard reads its ready-to-use replay bundle from
artifacts/replay/reduced_c00_replay.json.gz.

To learn about the sources and method, see the
[project reference](../docs/SYNOPTIQ.md).
