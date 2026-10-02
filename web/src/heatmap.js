/**
 * Visual Display Smoothing Filter for Synoptiq.
 * Renders client-side radial gradient smoothing across regional grid centroids on an HTML5 Canvas.
 * NOTE: This is a visual interpolation smoothing filter for UI presentation;
 * it is NOT radar observations, continuous gridded rainfall data, or physical fields.
 * 100% offline, client-side, zero external libraries.
 */

export function createHeatmapLayer(map) {
  let isVisible = true;
  let currentFeatures = [];
  let currentMode = "bust"; // 'bust' | 'forecast'

  const canvas = document.createElement("canvas");
  canvas.className = "leaflet-heatmap-canvas";
  canvas.style.position = "absolute";
  canvas.style.top = "0";
  canvas.style.left = "0";
  canvas.style.pointerEvents = "none";
  const pane = map.getPane("heatmapPane") || map.getPanes().overlayPane;
  pane.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  function resize() {
    const size = map.getSize();
    const pixelRatio = window.devicePixelRatio || 1;
    canvas.width = size.x * pixelRatio;
    canvas.height = size.y * pixelRatio;
    canvas.style.width = `${size.x}px`;
    canvas.style.height = `${size.y}px`;
    ctx.scale(pixelRatio, pixelRatio);
    reposition();
    draw();
  }

  function reposition() {
    const topLeft = map.containerPointToLayerPoint([0, 0]);
    L.DomUtil.setPosition(canvas, topLeft);
  }

  function draw() {
    const size = map.getSize();
    ctx.clearRect(0, 0, size.x, size.y);

    if (!isVisible || !currentFeatures || currentFeatures.length === 0) return;

    // Use global composite operation for smooth additive radar blending
    ctx.globalCompositeOperation = "screen";

    // Dynamic radius based on map zoom: ~1 degree latitude in screen pixels
    const p1 = map.latLngToContainerPoint([20, 78]);
    const p2 = map.latLngToContainerPoint([21, 78]);
    const degPixels = Math.abs(p2.y - p1.y);
    const radius = Math.max(35, degPixels * 1.8);

    currentFeatures.forEach((f) => {
      const p = f.properties || {};
      const coords = f.geometry?.coordinates?.[0];
      if (!coords || coords.length === 0) return;

      // Calculate centroid
      const cLon = (coords[0][0] + coords[2][0]) / 2;
      const cLat = (coords[0][1] + coords[2][1]) / 2;
      const pt = map.latLngToContainerPoint([cLat, cLon]);

      if (pt.x < -radius || pt.x > size.x + radius || pt.y < -radius || pt.y > size.y + radius) {
        return;
      }

      let colorCenter = "rgba(71, 85, 105, 0.4)";
      let colorMid = "rgba(71, 85, 105, 0.15)";

      if (currentMode === "forecast") {
        // Missing forecast totals render with no inferred rainfall glow.
        return;
      }

      // Bust risk mode
      if (p.tier === "high") {
        colorCenter = "rgba(239, 68, 68, 0.72)"; // high risk crimson red
        colorMid = "rgba(248, 113, 113, 0.4)";
      } else if (p.tier === "watch") {
        colorCenter = "rgba(245, 158, 11, 0.7)"; // watch amber
        colorMid = "rgba(251, 191, 36, 0.38)";
      } else if (p.tier === "low") {
        colorCenter = "rgba(13, 148, 136, 0.65)"; // low risk teal
        colorMid = "rgba(45, 212, 191, 0.35)";
      }

      if (p.tier === "no_data" || p.window_quality === "unavailable") {
        return; // No radiant glow for unavailable data
      }

      const grad = ctx.createRadialGradient(pt.x, pt.y, 4, pt.x, pt.y, radius);
      grad.addColorStop(0, colorCenter);
      grad.addColorStop(0.55, colorMid);
      grad.addColorStop(1, "rgba(0, 0, 0, 0)");

      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, radius, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.globalCompositeOperation = "source-over";
  }

  map.on("resize", resize);
  map.on("viewreset", () => {
    reposition();
    draw();
  });
  map.on("move", () => {
    reposition();
    draw();
  });

  resize();

  return {
    update(features, mode) {
      currentFeatures = features || [];
      currentMode = mode || "bust";
      draw();
    },
    toggle(visible) {
      if (visible === undefined) {
        isVisible = !isVisible;
      } else {
        isVisible = Boolean(visible);
      }
      canvas.style.display = isVisible ? "block" : "none";
      if (isVisible) draw();
      return isVisible;
    },
    destroy() {
      map.off("resize", resize);
      map.off("move", draw);
      if (canvas.parentNode) {
        canvas.parentNode.removeChild(canvas);
      }
    },
  };
}
