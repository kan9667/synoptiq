/**
 * Bottom Trust & Evaluation Bar component for Synoptiq.
 * Truthfully exposes held-out evaluation status, operational caveats, and API docs.
 */

import { escapeHtml } from "./format.js";

const METRIC_LABELS = {
  brier_score: "Brier score",
  brier_skill_score: "Brier skill score",
  auc_roc: "ROC AUC",
  expected_calibration_error: "Calibration error",
  sample_count: "Test samples",
};

function renderMetrics(metrics) {
  if (!metrics || Object.keys(metrics).length === 0) {
    return `<span class="trust-metrics-none font-mono">Metrics pending validated held-out evaluation</span>`;
  }

  // The frozen real replay bundles candidate and baseline metrics separately.
  // Flatten only supplied numeric values; do not derive a skill score in the UI.
  if (metrics.candidate && metrics.climatology) {
    const comparison = [
      ["Candidate Brier", metrics.candidate.brier_score],
      ["Climatology Brier", metrics.climatology.brier_score],
      ["Candidate − climatology", metrics.brier_score_delta],
    ].filter(([, value]) => typeof value === "number" && Number.isFinite(value));

    return `<div class="metric-grid">
      ${comparison
        .map(
          ([label, value]) =>
            `<span class="metric-card"><small>${escapeHtml(label)}</small><strong class="font-mono">${escapeHtml(
              value.toFixed(3)
            )}</strong></span>`
        )
        .join("")}
      <span class="metric-card metric-card-deferred" title="Spread-only baseline comparison is unavailable: p01–p04 member spread is not in current corpus">
        <small>Spread baseline</small>
        <strong class="font-mono text-warning">UNAVAILABLE</strong>
      </span>
    </div>`;
  }

  return `<div class="metric-grid">${Object.entries(metrics)
    .slice(0, 4)
    .map(([key, value]) => {
      const label = METRIC_LABELS[key] || key.replace(/_/g, " ");
      const display = typeof value === "number" ? value.toFixed(3) : String(value);
      return `<span class="metric-card"><small>${escapeHtml(label)}</small><strong class="font-mono">${escapeHtml(display)}</strong></span>`;
    })
    .join("")}</div>`;
}

/**
 * Renders the persistent bottom trust strip.
 * @param {HTMLElement} element
 * @param {object} evaluation
 * @param {object} options
 */
export function renderTrust(element, evaluation, { onOpenReliability } = {}) {
  const status = evaluation?.status || "insufficient_test_data";
  const message =
    evaluation?.message ||
    "No held-out evaluation is available for this historical replay.";
  const metrics = evaluation?.metrics;
  const dataMode = evaluation?.data_mode || "historical_replay";

  const metricsHtml = renderMetrics(metrics);

  element.innerHTML = `
    <div class="trust-bar-inner">
      <div class="trust-col trust-eval">
        <div class="trust-tag-row">
          <span class="trust-status-badge status-${escapeHtml(status)}">
            EVAL: ${escapeHtml(status.toUpperCase().replace(/_/g, " "))}
          </span>
          ${metricsHtml}
          <button type="button" class="btn-trust-action font-mono" id="btn-open-reliability" title="View plotted held-out reliability diagram and calibration curve">
            <span class="btn-icon">📈</span> Reliability Diagram &rarr;
          </button>
        </div>
      </div>

      <div class="trust-col trust-disclaimer">
        <div class="disclaimer-badge">RESEARCH PROTOTYPE</div>
        <p class="disclaimer-text">
          GEFSv12 reforecast research prototype; not NCUM/NEPS operational validation. Not a live public warning service.
        </p>
      </div>

      <div class="trust-col trust-meta">
        <div class="trust-meta-row">
          <span class="trust-mode-pill mode-${escapeHtml(dataMode)}">
            ${escapeHtml(dataMode.toUpperCase())}
          </span>
          <a href="/docs" target="_blank" rel="noopener" class="trust-link font-mono" title="FastAPI OpenAPI Specification">
            API Specs /docs &rarr;
          </a>
        </div>
        <div class="trust-sub">
          Synoptiq &bull; SIH 26079
        </div>

      </div>
    </div>
  `;

  const relBtn = element.querySelector("#btn-open-reliability");
  if (relBtn && onOpenReliability) {
    relBtn.addEventListener("click", onOpenReliability);
  }
}
