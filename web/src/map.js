/**
 * Center Geospatial Leaflet Map component for Synoptiq.
 * Renders fixed 2-degree regional GeoJSON choropleth strictly offline without external map tiles.
 * Supports risk, forecast-total (when a real artifact supplies it), and 2° land-grid views.
 * Features live telemetry coordinates, tactical compass rose, scale bar, geographic anchors,
 * decorative particle streamlines, and visual display smoothing filters.
 */

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { formatProbability, formatUtcTime, escapeHtml } from "./format.js";
import { getTierColor, getTierStroke, PALETTE, FORECAST_PALETTE, COVERAGE_PALETTE } from "./legend.js";
import indiaBoundary from "./india_boundary.json";
import regionsGrid from "./regions_grid.json";
import { createParticleLayer } from "./particles.js";
import { createHeatmapLayer } from "./heatmap.js";

// Major Indian cities for geographic orientation
const CITIES = [
  { name: "New Delhi", coords: [28.61, 77.20], primary: true },
  { name: "Mumbai", coords: [19.07, 72.87], primary: true },
  { name: "Kolkata", coords: [22.57, 88.36], primary: true },
  { name: "Chennai", coords: [13.08, 80.27], primary: true },
  { name: "Bengaluru", coords: [12.97, 77.59], primary: true },
  { name: "Hyderabad", coords: [17.38, 78.48], primary: false },
  { name: "Ahmedabad", coords: [23.02, 72.57], primary: false },
  { name: "Nagpur", coords: [21.14, 79.08], primary: false },
  { name: "Bhopal", coords: [23.25, 77.41], primary: false },
  { name: "Guwahati", coords: [26.14, 91.73], primary: false },
  { name: "Srinagar", coords: [34.08, 74.79], primary: false },
  { name: "Kochi", coords: [9.93, 76.26], primary: false },
  { name: "Bhubaneswar", coords: [20.29, 85.82], primary: false },
];

// Maritime and synoptic watermarks
const MARITIME = [
  { name: "ARABIAN SEA", coords: [15.0, 68.0] },
  { name: "BAY OF BENGAL", coords: [14.0, 89.0] },
  { name: "INDIAN OCEAN", coords: [5.6, 78.0] },
  { name: "HIMALAYAN ARC", coords: [34.5, 80.5] },
];

export function createMap(containerElement, onSelectRegion, onModeChange) {
  // Initialize Leaflet map centered on India with NO external tile layers required
  const map = L.map(containerElement, {
    attributionControl: false,
    zoomControl: false,
    minZoom: 4,
    maxZoom: 10,
  }).setView([22.5, 82.0], 5);

  // 1. Add custom zoom control in top-right
  L.control.zoom({ position: "topright" }).addTo(map);

  // 2. Technical Scale Bar (Kilometers)
  L.control.scale({
    position: "bottomleft",
    metric: true,
    imperial: false,
    maxWidth: 140,
  }).addTo(map);

  // 3. Compass Rose (Tactical North Indicator)
  const compassEl = document.createElement("div");
  compassEl.className = "map-compass-rose";
  compassEl.setAttribute("aria-hidden", "true");
  compassEl.innerHTML = `
    <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
      <circle cx="16" cy="16" r="14" stroke="rgba(56, 189, 248, 0.35)" stroke-width="1" stroke-dasharray="2, 2" />
      <polygon points="16,3 19.5,15 16,13 12.5,15" fill="#38bdf8" />
      <polygon points="16,29 19.5,17 16,19 12.5,17" fill="#475569" />
      <line x1="16" y1="1" x2="16" y2="31" stroke="rgba(56, 189, 248, 0.25)" stroke-width="0.5" />
      <line x1="1" y1="16" x2="31" y2="16" stroke="rgba(56, 189, 248, 0.25)" stroke-width="0.5" />
      <text x="16" y="2" text-anchor="middle" font-size="6.5" font-weight="900" fill="#38bdf8" font-family="monospace">N</text>
    </svg>
  `;
  containerElement.appendChild(compassEl);

  // 4. Live Cursor Coordinates Telemetry Bar (Bottom Left)
  const coordsReadout = document.createElement("div");
  coordsReadout.className = "map-coords-readout";
  coordsReadout.setAttribute("role", "status");
  coordsReadout.setAttribute("aria-live", "off");
  coordsReadout.innerHTML = `
    <div class="coord-pill"><span class="coord-k">CRS</span> <span class="coord-v">EPSG:4326</span></div>
    <div class="coord-pill"><span class="coord-k">CURSOR</span> <span class="coord-v font-mono" id="dyn-lat">21.500°N</span>, <span class="coord-v font-mono" id="dyn-lon">78.500°E</span></div>
    <div class="coord-pill"><span class="coord-k">GRID</span> <span class="coord-v">2° REG / 0.25° IMD</span></div>
  `;
  containerElement.appendChild(coordsReadout);

  map.on("mousemove", (e) => {
    const latEl = coordsReadout.querySelector("#dyn-lat");
    const lonEl = coordsReadout.querySelector("#dyn-lon");
    if (latEl && lonEl) {
      latEl.textContent = `${e.latlng.lat.toFixed(3)}°N`;
      lonEl.textContent = `${e.latlng.lng.toFixed(3)}°E`;
    }
  });

  // Dedicated map panes for strict, robust z-indexing
  map.createPane("basemapPane");
  map.getPane("basemapPane").style.zIndex = "250";

  map.createPane("heatmapPane");
  map.getPane("heatmapPane").style.zIndex = "350";

  map.createPane("gridPane");
  map.getPane("gridPane").style.zIndex = "420";

  // 5. Atmospheric Coastline Glow + Offline India Landmass Basemap
  // Layer 5a: Outer diffuse atmospheric halo
  const outerHalo = L.geoJSON(indiaBoundary, {
    pane: "basemapPane",
    interactive: false,
    style: {
      fillColor: "transparent",
      color: "#0284c7",
      weight: 6,
      opacity: 0.22,
    },
  }).addTo(map);

  // Layer 6b: Mid-range atmospheric glow
  const midHalo = L.geoJSON(indiaBoundary, {
    pane: "basemapPane",
    interactive: false,
    style: {
      fillColor: "transparent",
      color: "#38bdf8",
      weight: 3,
      opacity: 0.45,
    },
  }).addTo(map);

  // Layer 6c: Solid landmass with sharp crisp coastline
  const baseLandLayer = L.geoJSON(indiaBoundary, {
    pane: "basemapPane",
    interactive: false,
    style: {
      fillColor: "#080e1c",
      fillOpacity: 0.88,
      color: "#38bdf8",
      weight: 1.25,
      opacity: 0.95,
    },
  }).addTo(map);

  // 7. Latitude/Longitude coordinate graticules and margin badges
  const graticules = L.layerGroup();
  for (let lat = 10; lat <= 35; lat += 5) {
    L.polyline([[lat, 66], [lat, 100]], {
      color: "rgba(56, 189, 248, 0.12)",
      weight: 0.75,
      dashArray: "3, 6",
      interactive: false,
    }).addTo(graticules);

    const latTag = L.divIcon({
      className: "map-coord-tag",
      html: `<span>${lat}°00'N</span>`,
      iconSize: [44, 14],
      iconAnchor: [46, 7],
    });
    L.marker([lat, 66.5], { icon: latTag, interactive: false }).addTo(graticules);
  }

  for (let lon = 70; lon <= 95; lon += 5) {
    L.polyline([[6.5, lon], [38.5, lon]], {
      color: "rgba(56, 189, 248, 0.12)",
      weight: 0.75,
      dashArray: "3, 6",
      interactive: false,
    }).addTo(graticules);

    const lonTag = L.divIcon({
      className: "map-coord-tag",
      html: `<span>${lon}°00'E</span>`,
      iconSize: [44, 14],
      iconAnchor: [22, -4],
    });
    L.marker([6.5, lon], { icon: lonTag, interactive: false }).addTo(graticules);
  }
  graticules.addTo(map);

  // 8. Maritime Watermark Labels
  const maritimeLayer = L.layerGroup();
  MARITIME.forEach((m) => {
    const icon = L.divIcon({
      className: "map-watermark-anchor",
      html: `<span>${m.name}</span>`,
      iconSize: [160, 20],
      iconAnchor: [80, 10],
    });
    L.marker(m.coords, { icon, interactive: false }).addTo(maritimeLayer);
  });
  maritimeLayer.addTo(map);

  // Continuous risk heatmap canvas layer (interpolated gradient)
  const heatmapEngine = createHeatmapLayer(map);

  // Animated streamline flow layer (decorative atmospheric flow)
  const particleEngine = createParticleLayer(map);

  // 11. Reference Cities Layer with radar target pings
  const citiesLayer = L.layerGroup();
  CITIES.forEach((c) => {
    const icon = L.divIcon({
      className: `map-city-anchor ${c.primary ? "primary-hub" : ""}`,
      html: `
        <span class="city-ping"><span class="city-core"></span></span>
        <span class="city-label">${escapeHtml(c.name)}</span>
      `,
      iconSize: [100, 18],
      iconAnchor: [6, 9],
    });
    L.marker(c.coords, { icon, interactive: false }).addTo(citiesLayer);
  });
  citiesLayer.addTo(map);

  // Reference 2° India-land analysis grid outline overlay
  let coverageGridLayer = null;

  // Fit bounds to India landmass on initialization
  if (baseLandLayer.getBounds().isValid()) {
    map.fitBounds(baseLandLayer.getBounds().pad(0.04));
  }

  let currentLayer = null;
  let selectedId = null;
  let hasUserMoved = false;
  let currentMode = "bust"; // 'bust' | 'forecast' | 'coverage'
  let cachedPayload = null;
  let cachedLead = 1;
  let isFlowEnabled = true;
  let isGlowEnabled = true;
  // 12. Floating HUD element inside map container (top-left)
  let hudElement = containerElement.querySelector(".map-hud");
  if (!hudElement) {
    hudElement = document.createElement("div");
    hudElement.className = "map-hud";
    hudElement.setAttribute("aria-live", "polite");
    hudElement.innerHTML = `
      <div class="hud-item hud-lead">
        <span class="hud-label">TARGET LEAD</span>
        <span class="hud-val" id="hud-lead-val">Lead Day 1</span>
      </div>
      <div class="hud-item hud-window">
        <span class="hud-label">VALID INTERVAL</span>
        <span class="hud-val font-mono" id="hud-window-val">Awaiting replay data</span>
      </div>
      <div class="hud-item hud-mode">
        <span class="hud-label">ACTIVE LAYER</span>
        <span class="hud-val" id="hud-mode-val">BUST RISK</span>
      </div>
      <div class="hud-item hud-summary">
        <span class="hud-label">REPLAY COVERAGE</span>
        <span class="hud-val font-mono" id="hud-summary-val">Awaiting replay data</span>
      </div>
    `;
    containerElement.appendChild(hudElement);
  }

  // 13. Floating Layer View Mode Switcher + Feature Toggles (top-right next to zoom)
  let switcherEl = containerElement.querySelector(".map-view-switcher");
  if (!switcherEl) {
    switcherEl = document.createElement("div");
    switcherEl.className = "map-view-switcher";
    switcherEl.setAttribute("role", "group");
    switcherEl.setAttribute("aria-label", "Map Layer View Selection");
    switcherEl.innerHTML = `
      <button type="button" class="btn-view-mode active" data-mode="bust" title="View probability-of-bust risk tiers">
        <span class="btn-icon">⚡</span> Bust Risk
      </button>
      <button type="button" class="btn-view-mode" data-mode="forecast" title="View forecast rainfall (unavailable in fixture mode)">
        <span class="btn-icon">🌧️</span> Forecast Rain
      </button>
      <button type="button" class="btn-view-mode" data-mode="coverage" title="View audited 112 India-land 2° grid coverage">
        <span class="btn-icon">🌐</span> 2° Land Grid
      </button>
      <span class="switcher-sep"></span>
      <button type="button" class="btn-tool-toggle active" id="btn-toggle-flow" title="Toggle decorative particle streamline animation (synthetic display effect; not decoded wind data)">
        <span class="btn-icon">💨</span> Flow (FX)
      </button>
      <button type="button" class="btn-tool-toggle active" id="btn-toggle-glow" title="Toggle visual gradient smoothing (display smoothing filter; not radar observations)">
        <span class="btn-icon">✨</span> Glow (FX)
      </button>
    `;
    containerElement.appendChild(switcherEl);

    // Bind Mode Buttons
    switcherEl.querySelectorAll(".btn-view-mode").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.getAttribute("data-mode");
        if (mode && mode !== currentMode) {
          setMode(mode);
        }
      });
    });

    // Bind Wind Flow Toggle
    const flowBtn = switcherEl.querySelector("#btn-toggle-flow");
    if (flowBtn) {
      flowBtn.addEventListener("click", () => {
        isFlowEnabled = particleEngine.toggle();
        flowBtn.classList.toggle("active", isFlowEnabled);
      });
    }

    // Bind Radar Glow Toggle
    const glowBtn = switcherEl.querySelector("#btn-toggle-glow");
    if (glowBtn) {
      glowBtn.addEventListener("click", () => {
        isGlowEnabled = heatmapEngine.toggle();
        glowBtn.classList.toggle("active", isGlowEnabled);
      });
    }
  }

  function setMode(mode) {
    currentMode = mode;

    // Update switcher buttons
    switcherEl.querySelectorAll(".btn-view-mode").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-mode") === mode);
    });

    // Update HUD
    const hudMode = containerElement.querySelector("#hud-mode-val");
    if (hudMode) {
      hudMode.textContent = mode === "bust" ? "BUST RISK" : mode === "forecast" ? "FORECAST RAIN" : "2° LAND GRID";
    }

    // Update heatmap
    if (cachedPayload?.features) {
      heatmapEngine.update(cachedPayload.features, mode);
    }

    // Notify legend to update
    if (onModeChange) {
        onModeChange(mode, cachedPayload?.data_mode || "fixture");
    }

    // Re-render layer
    if (cachedPayload) {
      render(cachedPayload, cachedLead, selectedId);
    }
  }

  function getFeatureStyle(feature, isSelected) {
    const p = feature.properties || {};

    if (currentMode === "coverage") {
      const cov = p.coverage_fraction ?? 0;
      let fill = COVERAGE_PALETTE.coastal.fill;
      let stroke = COVERAGE_PALETTE.coastal.stroke;

      if (cov >= 1.0) {
        fill = COVERAGE_PALETTE.full.fill;
        stroke = COVERAGE_PALETTE.full.stroke;
      } else if (cov >= 0.80) {
        fill = COVERAGE_PALETTE.supported.fill;
        stroke = COVERAGE_PALETTE.supported.stroke;
      }

      return {
        fillColor: fill,
        fillOpacity: isSelected ? 0.95 : 0.62,
        color: isSelected ? "#38bdf8" : stroke,
        weight: isSelected ? 3.5 : 1,
        opacity: 0.85,
        className: isSelected ? "map-selected-cell" : "",
      };
    }

    if (currentMode === "forecast") {
      // Forecast view uses only a real, explicitly provided forecast-total field if present.
      // In fixture mode, forecast totals are unavailable; render neutral no-data styling without inferred colours.
      const fRain = typeof p.f_control_mm === "number" ? p.f_control_mm : null;
      let fill = FORECAST_PALETTE.no_data.fill;
      let stroke = FORECAST_PALETTE.no_data.stroke;

      if (fRain !== null) {
        if (fRain >= 30.0) {
          fill = FORECAST_PALETTE.heavy.fill;
          stroke = FORECAST_PALETTE.heavy.stroke;
        } else if (fRain >= 15.0) {
          fill = FORECAST_PALETTE.moderate.fill;
          stroke = FORECAST_PALETTE.moderate.stroke;
        } else {
          fill = FORECAST_PALETTE.light.fill;
          stroke = FORECAST_PALETTE.light.stroke;
        }
      }

      return {
        fillColor: fill,
        fillOpacity: isSelected ? 0.95 : 0.35,
        color: isSelected ? "#38bdf8" : stroke,
        weight: isSelected ? 3.5 : 1.5,
        opacity: 0.85,
        className: isSelected ? "map-selected-cell" : "",
      };
    }

    // Default 'bust' mode
    const isDay10 = Number(cachedLead) === 10;
    const cov = p.coverage_fraction ?? (p.is_land_supported ? 1.0 : 0);
    const isPeripheral = p.is_land_supported === false || cov < 0.80;

    if (isDay10) {
      return {
        fillColor: "#334155",
        fillOpacity: isSelected ? 0.9 : 0.4,
        color: isSelected ? "#38bdf8" : "#475569",
        weight: isSelected ? 3 : 1,
        opacity: 0.7,
        className: isSelected ? "map-selected-cell" : "",
      };
    }

    if (isPeripheral) {
      return {
        fillColor: "#1e293b",
        fillOpacity: isSelected ? 0.85 : 0.35,
        color: isSelected ? "#38bdf8" : "#475569",
        weight: isSelected ? 3 : 1.25,
        dashArray: "3, 4",
        opacity: 0.75,
        className: isSelected ? "map-selected-cell" : "",
      };
    }

    const tier = p.tier || "no_data";
    const fillColor = getTierColor(tier);
    const strokeColor = getTierStroke(tier);

    if (isSelected) {
      return {
        fillColor,
        fillOpacity: 0.92,
        color: "#38bdf8",
        weight: 4,
        opacity: 1,
        className: "map-selected-cell",
      };
    }

    return {
      fillColor,
      fillOpacity: tier === "no_data" ? 0.4 : 0.68,
      color: strokeColor,
      weight: 1.5,
      opacity: 0.85,
      className: "",
    };
  }

  function render(geojsonPayload, activeLead, selectedRegionId = null) {
    cachedPayload = geojsonPayload;
    cachedLead = activeLead;
    selectedId = selectedRegionId;

    if (currentLayer) {
      currentLayer.remove();
      currentLayer = null;
    }
    if (coverageGridLayer) {
      coverageGridLayer.remove();
      coverageGridLayer = null;
    }

    // Update Floating HUD
    const features = geojsonPayload.features || [];
    const firstFeature = features[0]?.properties;
    const hudLead = containerElement.querySelector("#hud-lead-val");
    const hudWindow = containerElement.querySelector("#hud-window-val");
    const hudSummary = containerElement.querySelector("#hud-summary-val");

    if (hudLead) hudLead.textContent = `Lead Day ${activeLead}`;
    if (hudSummary) {
      const isDay10 = Number(activeLead) === 10;
      if (isDay10) {
        hudSummary.textContent = "112 regions · All Day 10 unavailable (+240–+243h not evidenced)";
      } else {
        const scored = features.filter(
          (feature) => typeof feature.properties?.p_bust === "number" && Number.isFinite(feature.properties.p_bust)
        ).length;
        const peripheral = features.filter(
          (feature) => feature.properties?.is_land_supported === false || (feature.properties?.coverage_fraction ?? 1) < 0.80
        ).length;
        const high = features.filter((feature) => feature.properties?.tier === "high").length;
        const watch = features.filter((feature) => feature.properties?.tier === "watch").length;
        const low = features.filter((feature) => feature.properties?.tier === "low").length;
        const riskCounts = [
          high ? `${high} high` : null,
          watch ? `${watch} watch` : null,
          low ? `${low} low` : null,
        ].filter(Boolean);
        hudSummary.textContent = `${riskCounts.join(" · ") || `${scored} scored`} · ${peripheral} no-data`;
      }
    }
    if (hudWindow) {
      if (Number(activeLead) === 10) {
        hudWindow.textContent = "Unavailable (+240–+243h not evidenced)";
      } else if (firstFeature?.valid_start_utc && firstFeature?.valid_end_utc) {
        hudWindow.textContent = `${formatUtcTime(firstFeature.valid_start_utc)} → ${formatUtcTime(
          firstFeature.valid_end_utc
        )}`;
      } else if (firstFeature?.window_quality === "approximate") {
        hudWindow.textContent = "Approximate window";
      } else if (firstFeature?.window_quality === "unavailable") {
        hudWindow.textContent = "Interval unavailable";
      } else {
        hudWindow.textContent = "Interval unavailable";
      }
    }

    // Update continuous heatmap layer
    heatmapEngine.update(features, currentMode);

    if (currentMode === "coverage") {
      // In coverage mode: render all 112 India-land cells
      coverageGridLayer = L.geoJSON(regionsGrid, {
        pane: "gridPane",
        style: (feature) => getFeatureStyle(feature, feature.properties.region_id === selectedId),
        onEachFeature: (feature, layer) => {
          const p = feature.properties;
          const covPct = Math.round((p.coverage_fraction || 0) * 100);
          const tooltipHtml = `
            <div class="map-tooltip-card">
              <div class="map-tooltip-header">
                <span class="map-tooltip-id">${escapeHtml(p.region_id)}</span>
                <span class="map-tooltip-badge tier-${p.is_land_supported ? "low" : "no_data"}">
                  ${p.is_land_supported ? "Supported" : "Peripheral"}
                </span>
              </div>
              <div class="map-tooltip-body">
                <div class="map-tooltip-stat">
                  <span class="stat-lbl">Land Coverage:</span>
                  <span class="stat-val font-mono">${p.land_points} / ${p.total_points} pts (${covPct}%)</span>
                </div>
                <div class="map-tooltip-stat">
                  <span class="stat-lbl">Bounds:</span>
                  <span class="stat-val font-mono">[${p.lat_min}°N–${p.lat_max}°N] × [${p.lon_min}°E–${p.lon_max}°E]</span>
                </div>
                <div class="map-tooltip-stat">
                  <span class="stat-lbl">Split Policy:</span>
                  <span class="stat-val font-mono">${p.is_land_supported ? "Eligible for future rows.parquet build" : "Explicit no-data"}</span>
                </div>
              </div>
            </div>
          `;
          layer.bindTooltip(tooltipHtml, {
            className: "custom-leaflet-tooltip",
            sticky: true,
            direction: "top",
            offset: [0, -10],
            opacity: 1,
          });

          layer.on("click", () => {
            setSelected(p.region_id);
            onSelectRegion(p.region_id, feature);
          });
        },
      }).addTo(map);

      return;
    }

    // Standard replay layer (risk or forecast total when supplied by a real artifact).
    // First render background subtle reference grid for unselected regions
    coverageGridLayer = L.geoJSON(regionsGrid, {
      pane: "gridPane",
      interactive: false,
      style: (feature) => ({
        fillColor: "transparent",
        color: feature.properties?.is_land_supported ? "#334155" : "#1e293b",
        weight: 0.75,
        opacity: feature.properties?.is_land_supported ? 0.35 : 0.15,
        dashArray: "2, 4",
      }),
    }).addTo(map);

    // Create GeoJSON layer for active replay polygons
    currentLayer = L.geoJSON(geojsonPayload, {
      pane: "gridPane",
      style: (feature) => getFeatureStyle(feature, feature.properties.region_id === selectedId),
      onEachFeature: (feature, layer) => {
        const p = feature.properties || {};
        const tier = p.tier || "no_data";
        const TIER_META = { high: { label: "High Risk" }, medium: { label: "Medium Risk" }, low: { label: "Low Risk" }, no_data: { label: "No Data" } };
        const tierMeta = TIER_META[tier] || TIER_META.no_data;
        const covPct = Math.round(((p.coverage_fraction ?? (p.is_land_supported ? 1.0 : 0)) * 100));
        const isPeripheral = p.is_land_supported === false || (p.coverage_fraction !== null && p.coverage_fraction !== undefined && Number(p.coverage_fraction) < 0.80);
        const isDay10 = Number(cachedLead) === 10;
        const probText = Number.isFinite(Number(p.p_bust)) ? `${(Number(p.p_bust) * 100).toFixed(2)}%` : "—";
        const thresholdText = Number.isFinite(Number(p.threshold_mm)) ? `${Number(p.threshold_mm).toFixed(1)} mm` : "—";

        const badgeHtml = isDay10
          ? '<span class="map-tooltip-badge tier-no_data">Day 10 Unavailable</span>'
          : isPeripheral
          ? '<span class="map-tooltip-badge tier-no_data">Peripheral (&lt;80%)</span>'
          : `<span class="map-tooltip-badge tier-${p.tier}">${escapeHtml(tierMeta.label)}</span>`;

        let reasonHtml = "";
        if (isDay10) {
          reasonHtml = '<div class="map-tooltip-reason">⚠️ Day 10 scoring unavailable: exact +240–+243h accumulation interval not evidenced in archive. No risk fabricated.</div>';
        } else if (isPeripheral) {
          reasonHtml = `<div class="map-tooltip-reason">🌐 Peripheral Grid Cell: IMD land coverage ${covPct}% (&lt; 80% threshold). Excluded from model scoring.</div>`;
        } else if (p.no_data_reason) {
          reasonHtml = `<div class="map-tooltip-reason">⚠️ ${escapeHtml(p.no_data_reason)}</div>`;
        }

        const probPct = Number.isFinite(Number(p.p_bust)) ? Number(p.p_bust) * 100 : 0;
        const meterHtml = !isDay10 && p.p_bust !== null
          ? `<div class="tooltip-meter-track">
               <div class="tooltip-meter-fill tier-${p.tier}" style="width: ${probPct}%;"></div>
             </div>`
          : "";

        const validWindow = isDay10
          ? "Unavailable (+240–+243h not evidenced)"
          : p.valid_start_utc && p.valid_end_utc
          ? `${formatUtcTime(p.valid_start_utc)} → ${formatUtcTime(p.valid_end_utc)}`
          : p.window_quality === "unavailable"
          ? "Unavailable"
          : "Interval unavailable";
        const forecastText = isDay10
          ? "Unavailable"
          : Number.isFinite(Number(p.f_control_mm))
          ? `${Number(p.f_control_mm).toFixed(1)} mm`
          : "Forecast total unavailable in fixture";
        const rainStat = isDay10
          ? `<div class="map-tooltip-stat">
               <span class="stat-lbl">Status:</span>
               <span class="stat-val font-mono text-warning">Unavailable</span>
             </div>`
          : currentMode === "forecast"
          ? `<div class="map-tooltip-stat">
               <span class="stat-lbl">Forecast Rain:</span>
               <span class="stat-val ${Number.isFinite(Number(p.f_control_mm)) ? "font-mono" : "text-muted"}">${escapeHtml(forecastText)}</span>
             </div>`
          : `<div class="map-tooltip-stat">
               <span class="stat-lbl">P(bust):</span>
               <span class="stat-val font-mono">${escapeHtml(probText)}</span>
             </div>
             ${meterHtml}
             <div class="map-tooltip-stat">
               <span class="stat-lbl">q90 Threshold:</span>
               <span class="stat-val font-mono">${escapeHtml(thresholdText)}</span>
             </div>`;

        const tooltipHtml = `
          <div class="map-tooltip-card">
            <div class="map-tooltip-header">
              <span class="map-tooltip-id">${escapeHtml(p.region_id)}</span>
              ${badgeHtml}
            </div>
            <div class="map-tooltip-body">
              ${rainStat}
              <div class="map-tooltip-stat">
                <span class="stat-lbl">Window:</span>
                <span class="stat-val font-mono">${escapeHtml(
                  isDay10
                    ? "Unavailable (+240–+243h)"
                    : p.window_quality === "exact"
                    ? `Exact · ${validWindow}`
                    : p.window_quality === "approximate"
                    ? "Approximate"
                    : p.window_quality === "unavailable"
                    ? "Unavailable"
                    : "Awaiting replay"
                )}</span>
              </div>
            </div>
            ${reasonHtml}
          </div>
        `;

        layer.bindTooltip(tooltipHtml, {
          className: "custom-leaflet-tooltip",
          sticky: true,
          direction: "top",
          offset: [0, -10],
          opacity: 1,
        });

        layer.on("mouseover", () => {
          if (feature.properties.region_id !== selectedId) {
            layer.setStyle({
              weight: 2.5,
              color: "#f8fafc",
              fillOpacity: 0.9,
            });
          }
        });

        layer.on("mouseout", () => {
          if (feature.properties.region_id !== selectedId) {
            layer.setStyle(getFeatureStyle(feature, false));
          }
        });

        layer.on("click", () => {
          setSelected(p.region_id);
          onSelectRegion(p.region_id, feature);
        });
      },
    }).addTo(map);

    if (!hasUserMoved && baseLandLayer.getBounds().isValid()) {
      map.fitBounds(baseLandLayer.getBounds().pad(0.04));
    }
  }

  function clear(lead) {
    if (currentLayer) {
      currentLayer.remove();
      currentLayer = null;
    }
    selectedId = null;

    const hudLead = containerElement.querySelector("#hud-lead-val");
    const hudWindow = containerElement.querySelector("#hud-window-val");

    if (hudLead && lead) hudLead.textContent = `Lead Day ${lead}`;
    if (hudWindow) hudWindow.textContent = "No replay asset for selected lead";

    heatmapEngine.update([], currentMode);
  }

  function setSelected(regionId) {
    selectedId = regionId;
    if (!currentLayer && !coverageGridLayer) return;

    const activeLayers = [currentLayer, coverageGridLayer].filter(Boolean);
    activeLayers.forEach((grp) => {
      grp.eachLayer((layer) => {
        const featId = layer.feature?.properties?.region_id;
        layer.setStyle(getFeatureStyle(layer.feature, featId === selectedId));
        if (featId === selectedId) {
          layer.bringToFront();
        }
      });
    });
  }

  return {
    render,
    clear,
    setSelected,
    setMode,
    invalidateSize: () => map.invalidateSize(),
  };
}
