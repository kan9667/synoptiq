/**
 * Risk tier legend and palette definition for Synoptiq.
 * Supports multiple visual layer modes: Bust Risk, Forecast Rainfall, and 2° Land Grid Coverage.
 */

export const PALETTE = {
  low: {
    fill: "#0d9488",
    stroke: "#2dd4bf",
    label: "Low Risk",
    description: "Server-reported low bust risk tier (P < 0.30).",
  },
  watch: {
    fill: "#d97706",
    stroke: "#fbbf24",
    label: "Watch",
    description: "Server-reported watch risk tier (0.30 ≤ P < 0.50).",
  },
  high: {
    fill: "#dc2626",
    stroke: "#f87171",
    label: "High Risk",
    description: "Server-reported high bust risk tier (P ≥ 0.50).",
  },
  no_data: {
    fill: "#475569",
    stroke: "#94a3b8",
    label: "No Data / Unaudited",
    description: "Server-reported unavailable or no-data tier. Never implies 0% risk.",
  },
};

export const FORECAST_PALETTE = {
  heavy: {
    fill: "#ec4899",
    stroke: "#f472b6",
    label: "Heavy (> 30 mm)",
    description: "Predicted heavy monsoon precipitation.",
  },
  moderate: {
    fill: "#3b82f6",
    stroke: "#60a5fa",
    label: "Moderate (15–30 mm)",
    description: "Predicted moderate regional precipitation.",
  },
  light: {
    fill: "#06b6d4",
    stroke: "#22d3ee",
    label: "Light (< 15 mm)",
    description: "Predicted light or isolated rainfall.",
  },
  no_data: {
    fill: "#475569",
    stroke: "#94a3b8",
    label: "No Data",
    description: "Forecast precipitation unavailable.",
  },
};

export const COVERAGE_PALETTE = {
  full: {
    fill: "#0d9488",
    stroke: "#2dd4bf",
    label: "100% Core Land",
    description: "Complete 64/64 valid IMD 0.25° grid points (44 regions).",
  },
  supported: {
    fill: "#0284c7",
    stroke: "#38bdf8",
    label: "≥ 80% Supported",
    description: "52–63 valid points; meets training coverage threshold (21 regions).",
  },
  coastal: {
    fill: "#475569",
    stroke: "#64748b",
    label: "< 80% Peripheral",
    description: "Coastal/border fringe (< 52 points); explicit no-data (47 regions).",
  },
};

export function getTierColor(tier) {
  return PALETTE[tier]?.fill ?? PALETTE.no_data.fill;
}

export function getTierStroke(tier) {
  return PALETTE[tier]?.stroke ?? PALETTE.no_data.stroke;
}

/**
 * Renders the interactive legend into the container for the active view mode.
 * @param {HTMLElement} container
 * @param {string} mode - 'bust' | 'forecast' | 'coverage'
 */
export function renderLegend(container, mode = "bust", dataMode = "historical_replay", tierCounts = null) {
  if (mode === "coverage") {
    const items = [
      { key: "full", ...COVERAGE_PALETTE.full },
      { key: "supported", ...COVERAGE_PALETTE.supported },
      { key: "coastal", ...COVERAGE_PALETTE.coastal },
    ];
    container.innerHTML = `
      <div class="legend-header">
        <span class="legend-title">2° LAND GRID COVERAGE</span>
        <span class="legend-sub">112 REGIONS</span>
      </div>
      <div class="legend-items">
        ${items
          .map(
            (t) => `
          <div class="legend-row" title="${t.description}">
            <span class="legend-swatch" style="background-color: ${t.fill}; border-color: ${t.stroke};"></span>
            <div class="legend-meta">
              <span class="legend-label">${t.label}</span>
            </div>
          </div>
        `
          )
          .join("")}
      </div>
      <div class="legend-caption">
        Audited IMD land support partition (65 supported + 47 peripheral).
      </div>
    `;
    return;
  }

  if (mode === "forecast") {
      const items = [
        { key: "heavy", ...FORECAST_PALETTE.heavy },
        { key: "moderate", ...FORECAST_PALETTE.moderate },
        { key: "light", ...FORECAST_PALETTE.light },
        { key: "no_data", ...FORECAST_PALETTE.no_data },
      ];
      container.innerHTML = `
        <div class="legend-header">
          <span class="legend-title">CONTROL FORECAST RAIN</span>
          <span class="legend-sub">HISTORICAL REPLAY</span>
        </div>
        <div class="legend-items">
          ${items
            .map(
              (item) => `
          <div class="legend-row" title="${item.description}">
            <span class="legend-swatch" style="background-color: ${item.fill}; border-color: ${item.stroke};"></span>
            <div class="legend-meta"><span class="legend-label">${item.label}</span></div>
          </div>`
            )
            .join("")}
        </div>
        <div class="legend-caption">Actual c00 control-forecast total for the displayed 24-hour replay window; no-data cells remain neutral.</div>
      `;
    return;
  }

  // Default 'bust' mode
  const tiers = [
    { key: "high", ...PALETTE.high },
    { key: "watch", ...PALETTE.watch },
    { key: "low", ...PALETTE.low },
    { key: "no_data", ...PALETTE.no_data },
  ];

  container.innerHTML = `
    <div class="legend-header">
      <span class="legend-title">BUST RISK TIERS</span>
      <span class="legend-sub">P(BUST)</span>
    </div>
    <div class="legend-items">
      ${tiers
        .map(
          (t) => {
            const count = tierCounts?.[t.key];
            const countHtml = count !== undefined && count !== null
              ? `<span class="legend-count">${count}</span>`
              : "";
            return `
        <div class="legend-row tier-${t.key}" title="${t.description}">
          <span class="legend-swatch" style="background-color: ${t.fill}; border-color: ${t.stroke};"></span>
          <div class="legend-meta">
            <span class="legend-label">${t.label}</span>
            ${countHtml}
          </div>
        </div>
      `;
          }
        )
        .join("")}
    </div>
    <div class="legend-caption">
      Server-reported risk tiers for historical replay.
    </div>
  `;
}
