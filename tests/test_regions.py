"""Unit and hand-checked tests for 2-degree India-land regions, coverage, and IMD aggregation."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from bust.data.regions import (
    aggregate_imd_daily_region,
    compute_region_coverage,
    create_region,
    generate_candidate_lattice,
    has_sufficient_coverage,
    load_regions_geojson,
    parse_region_id,
)

IMD_2018_PATH = Path("data/raw/imd/ind2018_rfp25.nc")


def test_deterministic_candidate_lattice_and_geojson() -> None:
    """Candidate lattice must generate 240 deterministic 2x2 cells covering the India domain (38°N excluded)."""
    candidates = generate_candidate_lattice()
    assert len(candidates) == 240

    # Verify unique region IDs
    ids = [r.region_id for r in candidates]
    assert len(ids) == len(set(ids))

    # Verify ID formatting and parse roundtrip
    for r in candidates:
        lat, lon = parse_region_id(r.region_id)
        assert lat == r.center_lat
        assert lon == r.center_lon

        geom = r.geojson_geometry
        assert geom["type"] == "Polygon"
        coords = geom["coordinates"][0]
        assert len(coords) == 5
        assert coords[0] == coords[-1]  # Closed ring
        assert coords[0] == [r.lon_min, r.lat_min]
        assert coords[2] == [r.lon_max, r.lat_max]


def test_region_coverage_threshold_synthetic() -> None:
    """Exact 0.80 coverage is accepted; strictly below 0.80 is rejected."""
    assert has_sufficient_coverage(0.80, minimum=0.80) is True
    assert has_sufficient_coverage(0.8001, minimum=0.80) is True
    assert has_sufficient_coverage(0.7999, minimum=0.80) is False
    assert has_sufficient_coverage(None, minimum=0.80) is False

    # On a 64-point grid: 51/64 = 0.796875 (< 0.80, rejected), 52/64 = 0.8125 (>= 0.80, accepted)
    assert has_sufficient_coverage(51 / 64.0, minimum=0.80) is False
    assert has_sufficient_coverage(52 / 64.0, minimum=0.80) is True


def test_missing_values_are_not_treated_as_zero() -> None:
    """Missing (NaN) cells represent unobserved/ocean points and must not drag down the mean or count as 0 mm."""
    region = create_region(20.0, 78.0)
    lats = np.arange(19.0, 21.0, 0.25)
    lons = np.arange(77.0, 79.0, 0.25)

    # 56 points with 10.0 mm rain, 8 points NaN (ocean) -> 56/64 = 0.875 coverage (>= 0.80)
    grid = np.full((8, 8), 10.0)
    grid[0, :8] = np.nan

    result = aggregate_imd_daily_region(region, lats, lons, grid, minimum_coverage=0.80)
    assert result["has_sufficient_coverage"] is True
    assert result["valid_points"] == 56
    assert result["total_points"] == 64
    assert result["coverage_fraction"] == 56 / 64.0

    # If NaNs were treated as 0, mean would be (56 * 10) / 64 = 8.75 mm.
    # The true unweighted mean of valid land observations is 10.0 mm.
    assert result["o_imd_mm"] == pytest.approx(10.0)
    assert result["o_imd_mm"] != pytest.approx(8.75)


@pytest.mark.skipif(not IMD_2018_PATH.exists(), reason="Real IMD 2018 pilot file not found locally")
def test_hand_check_case_1_accepted_coverage_real_data() -> None:
    """Hand-check Case 1: Central inland cell with complete coverage (1.0 >= 0.80).

    Target: R20N-078E (lat [19, 21), lon [77, 79)) on date 2018-08-02.
    Transparent expected arithmetic:
    - 64 out of 64 grid points are valid land points (coverage = 1.0).
    - Sum of all 64 grid points = 171.810974 mm.
    - Expected spatial mean = 171.810974 / 64 = 2.6845465 mm.
    """
    ds = xr.open_dataset(IMD_2018_PATH)
    lats = ds.LATITUDE.values
    lons = ds.LONGITUDE.values
    rf_day = ds.RAINFALL.sel(TIME="2018-08-02").values

    region = create_region(20.0, 78.0)
    assert region.region_id == "R20N-078E"

    result = aggregate_imd_daily_region(region, lats, lons, rf_day, minimum_coverage=0.80)

    assert result["has_sufficient_coverage"] is True
    assert result["valid_points"] == 64
    assert result["total_points"] == 64
    assert result["coverage_fraction"] == 1.0
    assert result["no_data_reason"] is None
    assert result["o_imd_mm"] == pytest.approx(2.6845465, abs=1e-5)
    assert result["min_mm"] == pytest.approx(0.0, abs=1e-5)
    assert result["max_mm"] == pytest.approx(18.546318, abs=1e-5)


@pytest.mark.skipif(not IMD_2018_PATH.exists(), reason="Real IMD 2018 pilot file not found locally")
def test_hand_check_case_2_rejected_low_coverage_no_data_real_data() -> None:
    """Hand-check Case 2: Coastal cells with coverage strictly below 0.80 produce explicit no-data.

    Target A: R10N-076E (Kerala coast: lat [9, 11), lon [75, 77)) on 2018-08-02.
    Transparent expected arithmetic:
    - Exactly 28 out of 64 grid points are valid land points; 36 points are ocean NaN.
    - Coverage fraction = 28 / 64 = 0.4375 (< 0.80).
    - Result must be o_imd_mm = None, has_sufficient_coverage = False,
      with explicit no_data_reason stating coverage_fraction 0.4375 is below minimum 0.80.

    Target B: R22N-070E (Gujarat coast: lat [21, 23), lon [69, 71)) on 2018-08-02.
    Transparent expected arithmetic:
    - Exactly 51 out of 64 grid points are valid land points (coverage = 51 / 64 = 0.796875).
    - Because 0.796875 < 0.80, this near-threshold border cell is also rejected as explicit no-data.
    """
    ds = xr.open_dataset(IMD_2018_PATH)
    lats = ds.LATITUDE.values
    lons = ds.LONGITUDE.values
    rf_day = ds.RAINFALL.sel(TIME="2018-08-02").values

    # Target A: R10N-076E (Kerala coast)
    region_kerala = create_region(10.0, 76.0)
    result_kerala = aggregate_imd_daily_region(region_kerala, lats, lons, rf_day, minimum_coverage=0.80)

    assert result_kerala["has_sufficient_coverage"] is False
    assert result_kerala["valid_points"] == 28
    assert result_kerala["total_points"] == 64
    assert result_kerala["coverage_fraction"] == 0.4375
    assert result_kerala["o_imd_mm"] is None
    assert "below minimum 0.80" in result_kerala["no_data_reason"]

    # Over the 28 valid points, mean is 6.087 mm; assert this was not filled with zeros
    lat_idx = np.where((lats >= 9.0) & (lats < 11.0))[0]
    lon_idx = np.where((lons >= 75.0) & (lons < 77.0))[0]
    sub_kerala = rf_day[np.ix_(lat_idx, lon_idx)]
    valid_vals = sub_kerala[~np.isnan(sub_kerala)]
    assert len(valid_vals) == 28
    assert np.mean(valid_vals) == pytest.approx(6.0870004, abs=1e-5)

    # Target B: R22N-070E (Gujarat coast near-boundary cell)
    region_gujarat = create_region(22.0, 70.0)
    result_gujarat = aggregate_imd_daily_region(region_gujarat, lats, lons, rf_day, minimum_coverage=0.80)

    assert result_gujarat["has_sufficient_coverage"] is False
    assert result_gujarat["valid_points"] == 51
    assert result_gujarat["total_points"] == 64
    assert result_gujarat["coverage_fraction"] == 51 / 64.0
    assert result_gujarat["o_imd_mm"] is None


def test_config_regions_geojson_is_valid_and_deterministic() -> None:
    """The frozen config/regions_2deg.geojson artifact must load deterministically."""
    geojson_path = Path("config/regions_2deg.geojson")
    assert geojson_path.exists()

    all_regions = load_regions_geojson(geojson_path, supported_only=False)
    supported_regions = load_regions_geojson(geojson_path, supported_only=True)

    assert len(all_regions) == 112
    assert len(supported_regions) == 65

    # Check expected regions exist in supported set
    supp_ids = {r.region_id for r in supported_regions}
    assert {"R20N-078E", "R22N-080E", "R24N-076E"}.issubset(supp_ids)


def test_candidate_38n_is_absent_and_rejected_by_geometry() -> None:
    """center_lat=38 candidates must be absent from candidate lattice and rejected by validate_grid_geometry."""
    # 1. Absent from candidate lattice
    candidates = generate_candidate_lattice()
    assert not any(r.center_lat == 38.0 for r in candidates)
    assert not any(r.region_id.startswith("R38N") for r in candidates)

    # 2. If a 38°N region is evaluated against the audited IMD latitude grid (which ends at 38.5°N),
    # it contains only 7 latitude points in [37, 39) and must be rejected with ValueError.
    region_38n = create_region(38.0, 74.0)
    # Simulate audited IMD grid: lats up to 38.5°N at 0.25° spacing
    lats_imd = np.arange(36.0, 38.75, 0.25)  # 36.0, 36.25, ..., 38.5 (ends at 38.5)
    lons_imd = np.arange(73.0, 76.0, 0.25)
    mask_imd = np.ones((len(lats_imd), len(lons_imd)), dtype=bool)

    with pytest.raises(ValueError, match="intersected 7x8 grid points|expected exact 8x8=64 geometry"):
        compute_region_coverage(region_38n, lats_imd, lons_imd, mask_imd)

    with pytest.raises(ValueError, match="intersected 7x8 grid points|expected exact 8x8=64 geometry"):
        aggregate_imd_daily_region(region_38n, lats_imd, lons_imd, np.ones((len(lats_imd), len(lons_imd))))


def test_rejects_non_8x8_grid_geometry() -> None:
    """A coordinate grid shape that is not exactly 8x8 for a nominal 2° cell must be rejected."""
    region = create_region(20.0, 78.0)

    # 1. 0.5° resolution grid (4x4 points for a 2° cell)
    lats_05 = np.arange(18.0, 22.0, 0.5)
    lons_05 = np.arange(76.0, 80.0, 0.5)
    mask_05 = np.ones((len(lats_05), len(lons_05)), dtype=bool)

    with pytest.raises(ValueError, match="latitude resolution is not 0.25°|geometry"):
        compute_region_coverage(region, lats_05, lons_05, mask_05)

    with pytest.raises(ValueError, match="latitude resolution is not 0.25°|geometry"):
        aggregate_imd_daily_region(region, lats_05, lons_05, np.ones((len(lats_05), len(lons_05))))

    # 2. 0.25° grid with missing rows/columns (e.g. 7x8 points inside an interior region)
    lats_7x8 = np.array([19.0, 19.25, 19.5, 19.75, 20.0, 20.25, 20.5])  # 7 points in [19, 21)
    lons_7x8 = np.arange(77.0, 79.0, 0.25)  # 8 points in [77, 79)
    # Give it bounding box context so it's treated as interior
    full_lats = np.concatenate([[18.0], lats_7x8, [22.0]])
    full_lons = np.concatenate([[76.0], lons_7x8, [80.0]])
    mask_7x8 = np.ones((len(full_lats), len(full_lons)), dtype=bool)

    with pytest.raises(ValueError, match="expected exact 8x8=64 geometry"):
        compute_region_coverage(region, full_lats, full_lons, mask_7x8)


def test_regions_grid_json_matches_config_geojson() -> None:
    """web/src/regions_grid.json must be semantically identical to config/regions_2deg.geojson."""
    import json

    config_path = Path("config/regions_2deg.geojson")
    web_path = Path("web/src/regions_grid.json")

    assert config_path.exists(), f"Missing {config_path}"
    assert web_path.exists(), f"Missing {web_path}"

    config_data = json.loads(config_path.read_text(encoding="utf-8"))
    web_data = json.loads(web_path.read_text(encoding="utf-8"))

    assert config_data["type"] == web_data["type"] == "FeatureCollection"
    assert len(config_data["features"]) == len(web_data["features"]) == 112

    config_by_id = {f["properties"]["region_id"]: f for f in config_data["features"]}
    web_by_id = {f["properties"]["region_id"]: f for f in web_data["features"]}

    assert set(config_by_id.keys()) == set(web_by_id.keys())

    for reg_id, c_feat in config_by_id.items():
        w_feat = web_by_id[reg_id]
        assert c_feat["properties"] == w_feat["properties"]
        assert c_feat["geometry"] == w_feat["geometry"]


def test_load_regions_geojson_rejects_malformed(tmp_path: Path) -> None:
    """load_regions_geojson must reject invalid JSON, non-FeatureCollections, and malformed features."""
    import json

    # 1. Non-existent file
    with pytest.raises(FileNotFoundError):
        load_regions_geojson(tmp_path / "non_existent.geojson")

    # 2. Invalid JSON
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        load_regions_geojson(bad_json)

    # 3. Not a FeatureCollection
    not_fc = tmp_path / "not_fc.json"
    not_fc.write_text(json.dumps({"type": "Point", "coordinates": [0, 0]}), encoding="utf-8")
    with pytest.raises(ValueError, match="Expected GeoJSON FeatureCollection"):
        load_regions_geojson(not_fc)

    # 4. Missing required properties
    missing_prop = tmp_path / "missing_prop.json"
    feat = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"region_id": "R20N-078E"},  # missing center_lat, etc.
                "geometry": {"type": "Polygon", "coordinates": [[[77, 19], [79, 19], [79, 21], [77, 21], [77, 19]]]},
            }
        ],
    }
    missing_prop.write_text(json.dumps(feat), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required properties"):
        load_regions_geojson(missing_prop)

    # 5. Unclosed coordinate ring
    unclosed = tmp_path / "unclosed.json"
    feat_unclosed = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "region_id": "R20N-078E",
                    "center_lat": 20.0,
                    "center_lon": 78.0,
                    "lat_min": 19.0,
                    "lat_max": 21.0,
                    "lon_min": 77.0,
                    "lon_max": 79.0,
                    "coverage_fraction": 1.0,
                    "is_land_supported": True,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[77, 19], [79, 19], [79, 21], [77, 21], [77, 20]]],  # Not closed!
                },
            }
        ],
    }
    unclosed.write_text(json.dumps(feat_unclosed), encoding="utf-8")
    with pytest.raises(ValueError, match="not closed"):
        load_regions_geojson(unclosed)
