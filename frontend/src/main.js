import "./style.css";

const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");
const API = `${API_BASE}/api`;
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024; // matches the API's GEO_MAX_UPLOAD_BYTES

const drop = document.querySelector("#drop");
const fileInput = document.querySelector("#file-input");
const newFileButton = document.querySelector("#new-file");
const statusSection = document.querySelector("#status");
const statusRail = document.querySelector("#status-rail");
const statusMessage = document.querySelector("#status-message");
const readout = document.querySelector("#readout");
const featuresSection = document.querySelector("#features");
const featureRows = document.querySelector("#feature-rows");

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

// --- upload -----------------------------------------------------------------

function pickFile() {
  fileInput.click();
}

drop.addEventListener("click", pickFile);
newFileButton.addEventListener("click", pickFile);
drop.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    pickFile();
  }
});

["dragenter", "dragover"].forEach((name) =>
  document.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.add("is-over");
  })
);

["dragleave", "drop"].forEach((name) =>
  document.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.remove("is-over");
  })
);

// Dropping works anywhere on the page, including after the drop target is gone.
document.addEventListener("drop", (event) => {
  const file = event.dataTransfer?.files?.[0];
  if (file) send(file);
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files?.[0];
  if (file) send(file);
  fileInput.value = "";
});

async function send(file) {
  const name = file.name.toLowerCase();
  if (!name.endsWith(".zip") && !name.endsWith(".kml")) {
    showStatus(
      { filename: file.name, status: "REJECTED" },
      `Unsupported type. Send a .zip containing a Shapefile, or a .kml — got "${file.name}".`,
      true
    );
    return;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    showStatus(
      { filename: file.name, status: "REJECTED" },
      `Too large: ${numberFormat.format(file.size)} B, limit is ${numberFormat.format(
        MAX_UPLOAD_BYTES
      )} B.`,
      true
    );
    return;
  }

  const body = new FormData();
  body.append("file", file, file.name);
  drop.hidden = true; // the results take over the right pane
  showStatus({ filename: file.name, status: "UPLOADING" }, "Uploading…");

  let created;
  try {
    const response = await fetch(`${API}/files/`, { method: "POST", body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(describeError(payload));
    }
    created = payload;
  } catch (error) {
    showStatus({ filename: file.name, status: "FAILED" }, error.message, true);
    return;
  }

  showStatus(created, "Queued for processing…", false, true);
  const record = await poll(created.id);
  if (!record) {
    showStatus(
      { ...created, status: "FAILED" },
      "Lost contact with the API while processing. Refresh and try again.",
      true
    );
    return;
  }
  if (record.status === "TIMED_OUT") {
    showStatus(
      { ...created, status: "FAILED" },
      "Still processing after 90 seconds. The file id is below; retry the measurements endpoint.",
      true
    );
    return;
  }

  if (record.status === "FAILED") {
    showStatus(record, record.error || "Processing failed.", true);
    return;
  }

  showStatus(record, "Complete.");
  try {
    const response = await fetch(`${API}/files/${record.id}/measurements/`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(describeError(payload));
    }
    renderMeasurements(payload);
  } catch (error) {
    showStatus(record, error.message, true);
  }
}

function describeError(payload) {
  if (typeof payload.detail === "string") return payload.detail;
  if (payload.detail) {
    const { message, error } = payload.detail;
    return [message, error].filter(Boolean).join(": ");
  }
  if (payload.message) return payload.message;
  // A non-JSON body means a proxy rejected the request before it reached the API.
  return "The request was refused before it reached the API. If it was an upload, it was probably over the size limit.";
}

async function poll(fileId, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await fetch(`${API}/files/${fileId}/`);
    if (!response.ok) return null;
    const record = await response.json();
    if (record.status === "COMPLETED" || record.status === "FAILED") return record;
    showStatus(record, "Reading features and measuring…", false, true);
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return { status: "TIMED_OUT" };
}

// --- rendering --------------------------------------------------------------

function showStatus(record, message, isError = false, working = false) {
  statusSection.hidden = false;
  const stateClass =
    record.status === "COMPLETED"
      ? "is-filled"
      : record.status === "FAILED" || record.status === "REJECTED"
        ? "is-failed"
        : "";

  statusRail.innerHTML = [
    railItem("file", record.filename || "—"),
    record.format ? railItem("format", record.format) : "",
    record.size_bytes != null ? railItem("size", `${numberFormat.format(record.size_bytes)} B`) : "",
    record.crs ? railItem("crs", `${record.crs}${record.crs_assumed ? " (assumed)" : ""}`) : "",
    record.feature_count != null ? railItem("features", record.feature_count) : "",
    `<span class="is-state"><span class="state-mark ${stateClass}"></span>${record.status}</span>`,
  ]
    .filter(Boolean)
    .join("");

  statusMessage.textContent = message || "";
  statusMessage.classList.toggle("is-error", isError);

  const existing = statusSection.querySelector(".status-bar");
  if (existing) existing.remove();
  if (working) {
    const bar = document.createElement("div");
    bar.className = "status-bar";
    statusSection.append(bar);
  }
}

function railItem(label, value) {
  return `<span>${label} <b>${escapeHtml(String(value))}</b></span>`;
}

function renderMeasurements(payload) {
  const summary = payload.summary;

  countUp(document.querySelector("#area-value"), summary.total_area_m2);
  countUp(document.querySelector("#length-value"), summary.total_length_m);
  document.querySelector("#area-sub").textContent = `${numberFormat.format(
    summary.total_area_km2
  )} km² total`;
  document.querySelector("#length-sub").textContent = `${numberFormat.format(
    summary.total_length_km
  )} km total`;

  document.querySelector("#calc-crs").textContent = payload.calculation_crs;
  const assumed = payload.crs_assumed ? "source CRS assumed · " : "";
  document.querySelector("#crs-note").textContent =
    `${assumed}${payload.source_crs} → ${payload.calculation_crs} (${strategyName(
      payload.projection_strategy
    )})`;

  document.querySelector("#features-count").textContent =
    `${summary.feature_count} features · ${summary.measured} measured · ` +
    `${summary.unsupported + summary.failed} not measurable`;

  featureRows.innerHTML = payload.features.map(featureRow).join("");

  readout.hidden = false;
  featuresSection.hidden = false;
}

function strategyName(strategy) {
  if (strategy === "utm") return "utm zone";
  if (strategy === "laea") return "equal-area, centred on extent";
  return "already projected";
}

function featureRow(feature) {
  const props = Object.entries(feature.properties || {})
    .map(([key, value]) => `<span><b>${escapeHtml(key)}</b> ${escapeHtml(String(value))}</span>`)
    .join("");

  const note = feature.error
    ? `<span class="note is-error">${escapeHtml(feature.error)}</span>`
    : feature.reason
      ? `<span class="note">${escapeHtml(feature.reason)}</span>`
      : (feature.warnings || [])
          .map((warning) => `<span class="note">${escapeHtml(warning)}</span>`)
          .join("");

  const value = feature.measurement
    ? `${numberFormat.format(feature.measurement.value)} ${unitLabel(feature.measurement.unit)}`
    : `<span class="value-muted">—</span>`;

  return `<tr>
    <td class="col-index">${feature.index + 1}</td>
    <td class="col-geom"><span class="geom-type">${escapeHtml(feature.geometry_type)}</span>${note}</td>
    <td><div class="props">${props || '<span class="value-muted">no attributes</span>'}</div></td>
    <td class="col-num">${value}</td>
  </tr>`;
}

function unitLabel(unit) {
  if (unit === "m2") return "m²";
  if (unit === "m") return "m";
  return unit || "";
}

function countUp(element, target) {
  if (reducedMotion) {
    element.textContent = numberFormat.format(target);
    return;
  }
  const started = performance.now();
  const duration = 700;
  const tick = (now) => {
    const progress = Math.min((now - started) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = numberFormat.format(target * eased);
    if (progress < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function escapeHtml(value) {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]
  );
}
