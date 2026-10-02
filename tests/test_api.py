import gzip
import json

from bust.api import store
from bust.api.schemas import RegionResponse


def test_store_loads_compressed_historical_replay(monkeypatch, tmp_path) -> None:
    asset = tmp_path / "replay.json.gz"
    with gzip.open(asset, mode="wt", encoding="utf-8") as asset_file:
        json.dump({"data_mode": "historical_replay"}, asset_file)

    monkeypatch.setenv("REPLAY_ASSET_PATH", str(asset))
    store.load_store.cache_clear()
    try:
        assert store.load_store()["data_mode"] == "historical_replay"
    finally:
        store.load_store.cache_clear()


def test_region_schema_preserves_optional_drilldown_provenance_fields() -> None:
    """Optional drilldown fields remain valid for historical replay responses."""
    payload = {
        "region_id": "R20N-078E",
        "init_utc": "2018-08-01T00:00:00Z",
        "lead_day": 1,
        "p_bust": 0.2,
        "confidence_complement": None,
        "forecast_mm": 8.5,
        "observed_mm": None,
        "threshold_mm": 10.0,
        "window_quality": "exact",
        "data_mode": "historical_replay",
        "provenance": "replay-contract-v1",
        "valid_start_utc": "2018-08-01T03:00:00Z",
        "valid_end_utc": "2018-08-02T03:00:00Z",
        "coverage_fraction": 0.85,
        "source_key": "observed-source-key",
        "grib_steps": "24-48h",
        "no_data_reason": None,
        "tier": "low",
        "reasons": [],
        "analogs": [],
        "caveats": [],
    }
    model = RegionResponse.model_validate(payload)
    assert model.valid_start_utc == payload["valid_start_utc"]
    assert model.coverage_fraction == 0.85
    assert model.source_key == "observed-source-key"
    assert model.grib_steps == "24-48h"


def test_strict_offline_frontend_has_no_remote_tiles_or_cdns() -> None:
    """web/src/ must remain strictly offline: no remote URLs, CartoDB, tileLayer, or CDN references."""
    import re
    from pathlib import Path

    web_src = Path(__file__).resolve().parents[1] / "web/src"
    assert web_src.exists()

    forbidden_patterns = [
        "cartocdn",
        "cartodb",
        "tilelayer",
        "openstreetmap",
        "mapbox",
        "{z}/{x}/{y}",
    ]

    url_regex = re.compile(r"https?://[^\s\"'`<>]+", re.IGNORECASE)
    allowed_local_url = "http://127.0.0.1:8000"

    violations = []
    for f in sorted(web_src.rglob("*")):
        if f.is_file() and f.suffix in (".js", ".html", ".css", ".json"):
            content = f.read_text(encoding="utf-8")
            content_lower = content.lower()

            # 1. Retain checks for tileLayer, CartoDB, mapbox, OpenStreetMap, and {z}/{x}/{y}
            for pat in forbidden_patterns:
                if pat in content_lower:
                    violations.append(f"{f.name} contains forbidden remote pattern {pat!r}")

            # 2. Strict URL rule: reject every http:// or https:// occurrence;
            # allow only the exact local URL http://127.0.0.1:8000 in web/src/api.js
            urls = url_regex.findall(content)
            for url in urls:
                if f.name == "api.js" and url == allowed_local_url:
                    continue
                violations.append(f"{f.name} contains forbidden URL: {url!r}")

    assert not violations, "Strict offline violation(s) detected:\n" + "\n".join(violations)

    # 3. Regression assertion: no client-side tier-to-rainfall mapping exists.
    map_code = (web_src / "map.js").read_text(encoding="utf-8")
    heatmap_code = (web_src / "heatmap.js").read_text(encoding="utf-8")

    tier_rain_patterns = [
        r'tier\s*===?\s*["\']high["\']\s*\?\s*32',
        r'tier\s*===?\s*["\']watch["\']\s*\?\s*14',
        r'32\.0\s*mm',
        r'14\.0\s*mm',
    ]
    for pat in tier_rain_patterns:
        assert not re.search(pat, map_code), f"map.js contains tier-to-rainfall mapping matching {pat!r}"
        assert not re.search(pat, heatmap_code), f"heatmap.js contains tier-to-rainfall mapping matching {pat!r}"
