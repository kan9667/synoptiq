/**
 * Empty state and placeholder components for Synoptiq.
 * Truthful, clear notices when data is unavailable or awaiting user selection.
 */

import { escapeHtml } from "./format.js";

/**
 * Renders an empty state overlay over the map container when replay data is unavailable.
 * @param {HTMLElement} container
 * @param {object} options
 */
export function renderMapEmptyState(container, { lead, title, message, onReset }) {
  container.innerHTML = `
    <div class="map-empty-overlay" role="status" aria-live="polite">
      <div class="empty-card">
        <div class="empty-icon" aria-hidden="true">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="8" x2="12" y2="12"></line>
            <line x1="12" y1="16" x2="12.01" y2="16"></line>
          </svg>
        </div>
        <h3 class="empty-title">${escapeHtml(title ?? `Replay Unavailable: Day ${lead}`)}</h3>
        <p class="empty-message">${escapeHtml(
          message ??
            `No historical replay is available for this lead. Missing results are never fabricated.`
        )}</p>
        <div class="empty-actions">
          <button type="button" class="btn-reset" id="empty-reset-day1">
            Return to Day 1 Replay
          </button>
        </div>
      </div>
    </div>
  `;

  const resetBtn = container.querySelector("#empty-reset-day1");
  if (resetBtn && typeof onReset === "function") {
    resetBtn.addEventListener("click", onReset);
  }
}

/**
 * Renders a short, non-blocking loading state while a replay request is in flight.
 * The map is deliberately covered so an earlier date/lead cannot be mistaken for
 * the newly selected replay.
 */
export function renderMapLoadingState(container, { lead }) {
  container.innerHTML = `
    <div class="map-loading-overlay" role="status" aria-live="polite">
      <span class="loading-orbit" aria-hidden="true"></span>
      <div>
        <strong>Loading Lead Day ${escapeHtml(lead)}</strong>
        <span>Retrieving the frozen replay artifact…</span>
      </div>
    </div>
  `;
}

/**
 * Clears the map empty state overlay.
 * @param {HTMLElement} container
 */
export function clearMapEmptyState(container) {
  container.querySelectorAll(".map-empty-overlay, .map-loading-overlay").forEach((overlay) => overlay.remove());
}

/**
 * Renders the default unselected state for the region inspector.
 * @param {HTMLElement} container
 */
export function renderInspectorPrompt(container) {
  container.innerHTML = `
    <div class="inspector-prompt" role="region" aria-label="Region Inspector Prompt">
      <div class="prompt-icon" aria-hidden="true">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
          <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
          <line x1="12" y1="8" x2="12" y2="16"></line>
          <line x1="8" y1="12" x2="16" y2="12"></line>
        </svg>
      </div>
      <h3>Regional Inspector</h3>
      <p class="prompt-text">
        Select a 2&deg; region to inspect its risk score, verified UTC window, source provenance, and any supplied score evidence.
      </p>
      <div class="prompt-hint">
        <span>Start with a colored region, then follow its evidence and provenance.</span>
      </div>
    </div>

  `;
}
