/**
 * Boundary adapter for frozen replay API responses.
 *
 * The dashboard renders only values supplied by the API.  This module gives
 * missing optional detail fields explicit nulls so UI components can show a
 * no-data state instead of deriving or guessing them.
 */

const VALID_WINDOW_QUALITIES = new Set(["exact", "approximate", "unavailable"]);

function optionalNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function optionalString(value) {
  return typeof value === "string" && value.trim() ? value : null;
}

function windowQuality(value) {
  return VALID_WINDOW_QUALITIES.has(value) ? value : "unavailable";
}

/** Normalize a replay feature without manufacturing a missing score. */
export function normalizeReplayFeature(feature) {
  const props = feature?.properties || {};
  return {
    ...feature,
    properties: {
      ...props,
      region_id: optionalString(props.region_id) || "unknown",
      p_bust: optionalNumber(props.p_bust),
      threshold_mm: optionalNumber(props.threshold_mm),
      valid_start_utc: optionalString(props.valid_start_utc),
      valid_end_utc: optionalString(props.valid_end_utc),
      window_quality: windowQuality(props.window_quality),
      provenance: optionalString(props.provenance) || "not supplied",
      no_data_reason: optionalString(props.no_data_reason),
    },
  };
}

/** Normalize the fixed FeatureCollection contract used by the map. */
export function normalizeReplay(payload) {
  return {
    ...payload,
    data_mode: payload?.data_mode || "historical_replay",
    model: payload?.model || "not supplied",
    truth_source: payload?.truth_source || "not supplied",
    features: Array.isArray(payload?.features)
      ? payload.features.map(normalizeReplayFeature)
      : [],
  };
}

/** Normalize an optional region-detail response for the inspector. */
export function normalizeRegion(payload) {
  if (!payload) return null;
  return {
    ...payload,
    region_id: optionalString(payload.region_id) || "unknown",
    lead_day: Number.isInteger(Number(payload.lead_day)) ? Number(payload.lead_day) : null,
    p_bust: optionalNumber(payload.p_bust),
    threshold_mm: optionalNumber(payload.threshold_mm),
    forecast_mm: optionalNumber(payload.forecast_mm),
    observed_mm: optionalNumber(payload.observed_mm),
    coverage_fraction: optionalNumber(payload.coverage_fraction),
    valid_start_utc: optionalString(payload.valid_start_utc),
    valid_end_utc: optionalString(payload.valid_end_utc),
    source_key: optionalString(payload.source_key),
    grib_steps: optionalString(payload.grib_steps),
    provenance: optionalString(payload.provenance) || "not supplied",
    no_data_reason: optionalString(payload.no_data_reason),
    window_quality: windowQuality(payload.window_quality),
    analogs: Array.isArray(payload.analogs) ? payload.analogs : [],
    reasons: Array.isArray(payload.reasons) ? payload.reasons : [],
    caveats: Array.isArray(payload.caveats) ? payload.caveats : [],
  };
}

/** A curve datum is available only when a region endpoint supplied a score. */
export function normalizeLeadCurveItem(lead, payload, error = null) {
  const detail = normalizeRegion(payload);
  const unavailable = detail?.window_quality === "unavailable" || detail?.p_bust === null;
  return {
    lead,
    status: error ? (error.status === 404 ? "not_supplied" : "error") : unavailable ? "unavailable" : "available",
    reason: error?.message || detail?.no_data_reason || null,
    p_bust: unavailable ? null : detail?.p_bust ?? null,
    threshold_mm: unavailable ? null : detail?.threshold_mm ?? null,
    window_quality: detail?.window_quality || "unavailable",
  };
}
