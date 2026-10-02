/**
 * Left Control Dock component for Synoptiq.
 * Manages issue-time selection, discrete Day 1-10 selector buttons,
 * risk legend, and provenance telemetry.
 */

import { escapeHtml } from "./format.js";
import { renderLegend } from "./legend.js";

const FEATURED_CASE = {
  init: "2019-12-31",
  lead: 6,
  regionId: "R28N-094E",
};

/**
 * Initializes and binds the control dock.
 * @param {HTMLElement} container
 * @param {object} options
 */
export function setupControls(
  container,
  { inits, selectedInit, selectedLead, onInitChange, onLeadChange, onOpenCorpusInfo, onOpenFeatureArch, onOpenFeaturedCase }
) {
  const featuredCaseAvailable = inits.includes(FEATURED_CASE.init);
  container.innerHTML = `
    <div class="dock-section">
      <div class="dock-title-row">
        <span class="dock-eyebrow">REPLAY PARAMETERS</span>
      </div>

      <div class="control-group">
        <label for="init-select" class="control-label">
          <span>ISSUE INITIALIZATION</span>
          <span class="control-tag">00 UTC</span>
        </label>
        <div class="select-wrapper">
          <select id="init-select" class="select-init" aria-label="Forecast issue date">
            ${inits
              .map(
                (d) => `<option value="${escapeHtml(d)}" ${d === selectedInit ? "selected" : ""}>${escapeHtml(d)}</option>`
              )
              .join("")}
          </select>
        </div>
        <div class="init-corpus-meta">
          <span class="corpus-tag">3 Benchmark Dates</span>
          <button type="button" class="btn-corpus-info font-mono" id="btn-open-corpus-info" title="View 10-Year historical corpus split architecture">10-Yr Corpus Info ℹ️</button>
        </div>
        ${featuredCaseAvailable ? `
          <button type="button" class="btn-featured-case" id="btn-open-featured-case" title="Open the documented held-out replay case R28N-094E on 2019-12-31, Day 6">
            <span class="featured-case-kicker">FEATURED HELD-OUT CASE</span>
            <span class="featured-case-value font-mono">2019-12-31 · R28N-094E · D6</span>
          </button>` : ""}
      </div>

      <div class="control-group">
        <div class="control-label-row">
          <label class="control-label" id="lead-label">FORECAST LEAD HORIZON</label>
          <span class="lead-active-pill" id="lead-active-display">Lead Day ${selectedLead}</span>
        </div>

        <div class="lead-button-grid" role="group" aria-labelledby="lead-label">
          ${[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            .map((day) => {
              const isSelected = day === selectedLead;
              const isDay10 = day === 10;
              const tooltip = isDay10
                ? "Day 10 is strictly unavailable because the exact +240–+243-hour accumulation is not evidenced. No risk probability is fabricated."
                : `Lead Day ${day}`;

              return `
                <button
                  type="button"
                  class="btn-lead ${isSelected ? "btn-lead-active" : ""} ${isDay10 ? "btn-lead-day10 btn-lead-gray" : ""}"
                  data-lead="${day}"
                  title="${escapeHtml(tooltip)}"
                  aria-pressed="${isSelected ? "true" : "false"}"
                >
                  <span class="lead-num">D${day}</span>
                  ${isDay10 ? '<span class="lead-badge-gray">UNAVAIL</span>' : ""}
                </button>
              `;
            })
            .join("")}
        </div>
        <div class="lead-policy-card">
          <span class="policy-icon">◌</span>
          <div class="policy-body">
            <strong>Day 10:</strong> unavailable until an exact +240–+243h accumulation interval is evidenced. Day 1–9 remain independently scored.
          </div>
        </div>
      </div>
    </div>

    <div class="dock-section dock-legend-section" id="dock-legend-container"></div>

    <div class="dock-section dock-summary-section">
      <div class="dock-title-row">
        <span class="dock-eyebrow">SOURCE ARCHITECTURE</span>
      </div>
      <dl class="provenance-specs">
        <div>
          <dt>Forecast Model</dt>
          <dd id="spec-model">GEFSv12 Reforecast (00 UTC)</dd>
        </div>
        <div>
          <dt>Truth Target</dt>
          <dd id="spec-truth">IMD 0.25&deg; Daily (03–03 UTC)</dd>
        </div>
        <div>
          <dt>Spatial Unit</dt>
          <dd id="spec-domain">Fixed 2° India-land grid (65 scored / 47 peripheral)</dd>
        </div>
        <div>
          <dt>Window Semantics</dt>
          <dd id="spec-window">Awaiting replay data</dd>
        </div>
        <div>
          <dt>Spread Baseline</dt>
          <dd class="text-warning">Unavailable (p01–p04 absent)</dd>
        </div>
      </dl>
    </div>

    <!-- Model feature scope sits at the end of the dock: it is useful context,
         but never competes with issue-time controls or current-map evidence. -->
    <div class="dock-section dock-model-section">
      <div class="dock-title-row">
        <span class="dock-eyebrow">MODEL FEATURE ARCHITECTURE</span>
        <button type="button" class="btn-link-action font-mono" id="btn-open-feature-arch">Roadmap &rarr;</button>
      </div>
      <div class="feature-status-summary">
        <div class="feat-status-block feat-status-active">
          <div class="feat-status-head">
            <span class="feat-badge-active">&check; 5 ACTIVE FEATURES (c00)</span>
          </div>
          <p class="feat-desc font-mono">f_control_mm &bull; region &bull; season &bull; lead_day &bull; bucket</p>
        </div>

        <div class="feat-status-block feat-status-deferred">
          <div class="feat-status-head">
            <span class="feat-badge-deferred">&hourglass; ROADMAP / DEFERRED</span>
          </div>
          <p class="feat-desc">Ensemble spread (p01–p04), q850 moisture, pressure, and wind vectors are deferred to Phase 4 multi-level ingestion.</p>
        </div>
      </div>
    </div>
  `;

  // Render the legend inside the dock
  const legendContainer = container.querySelector("#dock-legend-container");
  if (legendContainer) {
    renderLegend(legendContainer);
  }

  // Bind Init select change
  const selectInit = container.querySelector("#init-select");
  selectInit.addEventListener("change", (e) => {
    onInitChange(e.target.value);
  });

  // Bind Lead buttons
  const leadButtons = container.querySelectorAll(".btn-lead");
  leadButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const lead = Number(btn.getAttribute("data-lead"));
      onLeadChange(lead);
    });
  });

  // Bind info modals
  const corpusBtn = container.querySelector("#btn-open-corpus-info");
  if (corpusBtn && onOpenCorpusInfo) {
    corpusBtn.addEventListener("click", onOpenCorpusInfo);
  }

  const archBtn = container.querySelector("#btn-open-feature-arch");
  if (archBtn && onOpenFeatureArch) {
    archBtn.addEventListener("click", onOpenFeatureArch);
  }

  const featuredCaseBtn = container.querySelector("#btn-open-featured-case");
  if (featuredCaseBtn && onOpenFeaturedCase) {
    featuredCaseBtn.addEventListener("click", () => onOpenFeaturedCase(FEATURED_CASE));
  }

  return {
    updateSelectedLead(lead) {
      const display = container.querySelector("#lead-active-display");
      if (display) display.textContent = `Lead Day ${lead}`;

      const buttons = container.querySelectorAll(".btn-lead");
      buttons.forEach((btn) => {
        const bLead = Number(btn.getAttribute("data-lead"));
        const isActive = bLead === lead;
        btn.classList.toggle("btn-lead-active", isActive);
        btn.setAttribute("aria-pressed", isActive ? "true" : "false");
      });
    },
    updateAvailableInits(initsList, currentInit) {
      if (selectInit) {
        selectInit.innerHTML = initsList
          .map(
            (d) => `<option value="${escapeHtml(d)}" ${d === currentInit ? "selected" : ""}>${escapeHtml(d)}</option>`
          )
          .join("");
      }
    },
    updateSelectedInit(init) {
      if (selectInit) selectInit.value = init;
    },
    updateProvenance({ model, truth_source, window_quality, window_text, data_mode, lead }) {
      const specModel = container.querySelector("#spec-model");
      const specTruth = container.querySelector("#spec-truth");
      const specWindow = container.querySelector("#spec-window");
      const specDomain = container.querySelector("#spec-domain");
      if (specModel && model) specModel.textContent = model;
      if (specTruth && truth_source) specTruth.textContent = truth_source;
      if (specDomain) {
        specDomain.textContent =
          data_mode === "historical_replay"
            ? "Fixed 2° India-Land Grid"
            : "Fixed 2° grid; integration data only";
      }
      if (specWindow) {
        if (window_text) {
          specWindow.textContent = window_text;
        } else if (window_quality === "exact") {
          specWindow.textContent = "Exact (03:00–03:00 UTC)";
        } else if (window_quality === "approximate") {
          specWindow.textContent = "Approximate window";
        } else if (window_quality === "unavailable") {
          specWindow.textContent =
            Number(lead) === 10
              ? "Unavailable (exact +240–+243h not evidenced)"
              : "Unavailable";
        } else {
          specWindow.textContent = "Awaiting replay data";
        }
      }
    },
    updateLegend(mode, dataMode = "historical_replay", tierCounts = null) {
      const legendContainer = container.querySelector("#dock-legend-container");
      if (legendContainer) renderLegend(legendContainer, mode, dataMode, tierCounts);
    },
  };
}
