/**
 * Synoptiq Web Dashboard — Main Orchestrator.
 * Coordinates offline Leaflet map, mission control dock, region inspector,
 * bottom trust strip, and truth-preserving error states.
 */

import { getAvailableInits, getEvaluation, getHealth, getRegion, getRegionLeadCurve, getReplay } from "./api.js";
import { setupControls } from "./controls.js";
import {
  clearMapEmptyState,
  renderInspectorPrompt,
  renderMapEmptyState,
  renderMapLoadingState,
} from "./empty_state.js";
import { createMap } from "./map.js";
import { setupModals } from "./modals.js";
import { renderRegion } from "./region.js";
import { normalizeLeadCurveItem, normalizeRegion, normalizeReplay } from "./replay_adapter.js";
import { renderTrust } from "./trust.js";
import "./styles.css";

// App state
const state = {
  init: "2018-08-01",
  lead: 1,
  selectedRegionId: null,
  currentReplay: null,
  availableInits: ["2018-08-01"],
  dataMode: "historical_replay",
  regionRequestId: 0,
};

let currentEvaluation = null;

// DOM references
const navInitTime = document.querySelector("#nav-init-time");
const navLeadHorizon = document.querySelector("#nav-lead-horizon");
const navWindowSemantics = document.querySelector("#nav-window-semantics");
const navModeBadge = document.querySelector("#nav-mode-badge");
const navModeText = document.querySelector("#nav-mode-text");
const controlDockEl = document.querySelector("#control-dock");
const mapEl = document.querySelector("#map");
const mapOverlayEl = document.querySelector("#map-overlay");
const regionPanelEl = document.querySelector("#region-panel");
const trustEl = document.querySelector("#trust");

let controlsHandle = null;

// Setup accessible modal system
const modals = setupModals({
  getEvaluationData: () => currentEvaluation,
});

// Wire top nav UI wrapper pills
const btnNavScope = document.querySelector("#btn-nav-scope");
if (btnNavScope) btnNavScope.addEventListener("click", () => modals.openOperationalScope());

const btnNavLive = document.querySelector("#btn-nav-live");
if (btnNavLive) btnNavLive.addEventListener("click", () => modals.openOperationalScope());

const btnNavModel = document.querySelector("#btn-nav-model");
if (btnNavModel) btnNavModel.addEventListener("click", () => modals.openFeatureArchitecture());

function updateModeChrome(dataMode) {
  state.dataMode = dataMode || state.dataMode;
  if (navModeBadge) navModeBadge.className = `mode-badge mode-${state.dataMode}`;
  if (navModeText) {
    navModeText.textContent = "HISTORICAL REPLAY";
  }
}

// Initialize Map
const map = createMap(
  mapEl,
  async (regionId, feature) => {
    state.selectedRegionId = regionId;
    await loadRegionDetails(regionId, feature?.properties);
  },
  (mode, dataMode) => {
    controlsHandle?.updateLegend(mode, dataMode);
  }
);

/**
 * Loads and displays regional inspector details.
 */
async function loadRegionDetails(regionId, fallbackProperties) {
  const requestId = ++state.regionRequestId;
  const init = state.init;
  const lead = state.lead;
  let regionData = null;
  let detailError = null;

  try {
    regionData = normalizeRegion(await getRegion(regionId, init, lead));
  } catch (error) {
    detailError = error;
  }

  // The curve is built only from independently supplied per-lead responses.
  // No intermediate probability or threshold is interpolated by the client.
  const curveResponses = await getRegionLeadCurve(regionId, init);
  if (requestId !== state.regionRequestId || init !== state.init || lead !== state.lead) return;

  const leadCurve = curveResponses.map(({ lead: curveLead, data, error }) =>
    normalizeLeadCurveItem(curveLead, data, error)
  );

  renderRegion(regionPanelEl, {
    regionData,
    fallbackProperties: fallbackProperties || { region_id: regionId, lead_day: lead },
    lead,
    leadCurve,
    detailError,
    onSelectLead: (selectedLead) => loadReplay(init, selectedLead),
  });
}

/**
 * Loads replay GeoJSON for the current init date and lead day.
 */
async function loadReplay(newInit, newLead, selectedRegionId = undefined) {
  // Invalidate an inspector request made for a previous date/lead before the
  // map changes, so late responses cannot overwrite the new selection.
  state.regionRequestId += 1;
  state.init = newInit ?? state.init;
  state.lead = Number(newLead ?? state.lead);
  if (selectedRegionId !== undefined) state.selectedRegionId = selectedRegionId;

  // Update navigation telemetry
  if (navInitTime) navInitTime.textContent = `${state.init} 00:00Z`;
  if (navLeadHorizon) navLeadHorizon.textContent = `Lead Day ${state.lead}`;

  // Update controls state
  controlsHandle?.updateSelectedLead(state.lead);
  controlsHandle?.updateSelectedInit(state.init);
  renderMapLoadingState(mapOverlayEl, { lead: state.lead });

  try {
    const replay = normalizeReplay(await getReplay(state.init, state.lead));
    state.currentReplay = replay;
    updateModeChrome(replay.data_mode);
    clearMapEmptyState(mapOverlayEl);

    // Render features on map
    map.render(replay, state.lead, state.selectedRegionId);

    // Compute tier counts for legend
    const features = replay.features || [];
    const isDay10Lead = Number(state.lead) === 10;
    const tierCounts = isDay10Lead ? null : {
      high: features.filter((f) => f.properties?.tier === "high").length,
      watch: features.filter((f) => f.properties?.tier === "watch").length,
      low: features.filter((f) => f.properties?.tier === "low").length,
      no_data: features.filter((f) => !f.properties?.tier || f.properties.tier === "no_data").length,
    };
    controlsHandle?.updateLegend("bust", replay.data_mode, tierCounts);

    // Determine window semantics to display in top bar
    const firstProps = replay.features?.[0]?.properties;
    const quality = firstProps?.window_quality;
    if (navWindowSemantics) {
      if (quality === "exact") {
        navWindowSemantics.textContent = "Exact (03:00–03:00 UTC)";
      } else if (quality === "approximate") {
        navWindowSemantics.textContent = "Approximate window";
      } else if (quality === "unavailable") {
        navWindowSemantics.textContent =
          Number(state.lead) === 10
            ? "Unavailable (+240–+243h not evidenced)"
            : "Unavailable";
      } else {
        navWindowSemantics.textContent = "Awaiting replay data";
      }
    }

    // Update controls specs
    controlsHandle?.updateProvenance({
      model: replay.model,
      truth_source: replay.truth_source,
      window_quality: quality,
      data_mode: replay.data_mode,
      lead: state.lead,
    });

    // Check if the previously selected region still exists in this lead
    const matchedFeature = replay.features?.find(
      (f) => f.properties?.region_id === state.selectedRegionId
    );

    if (matchedFeature) {
      await loadRegionDetails(state.selectedRegionId, matchedFeature.properties);
    } else {
      state.selectedRegionId = null;
      renderInspectorPrompt(regionPanelEl);
    }
  } catch (error) {
    // A failed request must never leave geometry from a prior selection visible.
    state.currentReplay = null;
    state.selectedRegionId = null;
    map.clear(state.lead);

    if (navWindowSemantics) {
      navWindowSemantics.textContent = "No replay asset for selected lead";
    }

    controlsHandle?.updateProvenance({
      model: "No replay asset",
      truth_source: "Not available",
      window_quality: null,
      window_text: "No replay asset for selected lead",
      data_mode: state.dataMode,
      lead: state.lead,
    });

    const isDayTen = Number(state.lead) === 10;
    renderMapEmptyState(mapOverlayEl, {
      lead: state.lead,
      title: isDayTen ? "Day 10 Is Unavailable" : `Replay Data Unavailable: Day ${state.lead}`,
      message: isDayTen
        ? "The exact Day 10 accumulation interval is not evidenced, so Synoptiq returns no risk score."
        : error.status === 404
        ? "No frozen replay artifact exists for this issue date and lead. Synoptiq does not substitute or fabricate a result."
        : `The replay could not be loaded: ${error.message}`,
      onReset: () => loadReplay(state.init, 1),
    });


    // Truthful inspector notice
    regionPanelEl.innerHTML = `
      <div class="inspector-prompt" role="status">
        <div class="prompt-icon">⚠️</div>
        <h3>${isDayTen ? "Day 10 Unavailable" : `Lead Day ${state.lead} Unavailable`}</h3>
        <p class="prompt-text">
          ${isDayTen ? "No Day 10 probability is shown without an evidenced exact accumulation interval." : "No frozen replay artifact exists for this selection."}
        </p>
        <div class="prompt-hint">
          <span>Choose another available lead or issue date.</span>
        </div>
      </div>
    `;
  }
}

/**
 * Boots the application and loads initial contracts.
 */
async function boot() {
  try {
    // 1. Health check & Mode determination
    const health = await getHealth();
    updateModeChrome(health.data_mode || "historical_replay");

    // 2. Discover available initialization dates from API
    const inits = await getAvailableInits();
    state.availableInits = inits;
    if (!inits.includes(state.init) && inits.length > 0) {
      state.init = inits[0];
    }

    // 3. Initialize Control Dock
    controlsHandle = setupControls(controlDockEl, {
      inits: state.availableInits,
      selectedInit: state.init,
      selectedLead: state.lead,
      onInitChange: (init) => loadReplay(init, state.lead),
      onLeadChange: (lead) => loadReplay(state.init, lead),
      onOpenCorpusInfo: () => modals.openCorpusDates(),
      onOpenFeatureArch: () => modals.openFeatureArchitecture(),
      onOpenFeaturedCase: ({ init, lead, regionId }) => loadReplay(init, lead, regionId),
    });

    // 4. Initial Inspector Prompt
    renderInspectorPrompt(regionPanelEl);

    // 5. Load Evaluation & Trust Strip
    currentEvaluation = await getEvaluation();
    renderTrust(trustEl, currentEvaluation, {
      onOpenReliability: () => modals.openReliability(),
    });

    // 6. Load Replay
    await loadReplay(state.init, state.lead);
  } catch (error) {
    console.error("Boot error:", error);
    if (regionPanelEl) {
      regionPanelEl.innerHTML = `
        <div class="inspector-prompt" role="alert">
          <div class="prompt-icon">❌</div>
          <h3>API Connection Error</h3>
          <p class="prompt-text">Unable to connect to local Synoptiq API: ${error.message}</p>
        </div>
      `;
    }
  }
}

// Window resize handling for Leaflet
window.addEventListener("resize", () => {
  map.invalidateSize();
});

// Boot the app
boot();
