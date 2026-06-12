(function () {
  "use strict";

  const BASE_URL = window.APP_CONFIG?.BASE_URL || window.location.origin;

  const updateBtn = document.getElementById("update-button");
  const saveBtn = document.getElementById("save-button");
  const downloadBtn = document.getElementById("download-button");
  const calibrationBtn = document.getElementById("calibration-button");
  const purgeBtn = document.getElementById("purge-button");
  const updateStatus = document.getElementById("update-status");
  const assignedRoleElem = document.getElementById("assigned-role");

  updateBtn.onclick = updateConfig;
  saveBtn.onclick = toggleSaving;
  downloadBtn.onclick = downloadData;
  calibrationBtn.onclick = toggleCalibration;

  function setAssignedRole(role) {
    if (!assignedRoleElem) return;
    assignedRoleElem.textContent = role || "Unknown";
  }

  window.addEventListener("nova:session", (event) => {
    const detail = event.detail || {};
    if (detail.role) {
      setAssignedRole(detail.role);
    }
  });

  let purgeState = "closed";
  if (purgeBtn) {
    purgeBtn.onclick = togglePurge;
    setPurgeButton();

    window.addEventListener("nova:actuator_states", (event) => {
      const states = event.detail || {};
      const rawState = states.BVOTP;
      if (!rawState) return;

      const state = typeof rawState === "object" ? (rawState.position || rawState.state) : rawState;
      if (!state) return;

      const normalized = String(state).toLowerCase();
      if (normalized === "open" || normalized === "closed") {
        purgeState = normalized;
        setPurgeButton();
      }
    });
  }

  function setPurgeButton() {
    if (!purgeBtn) return;
    const isOpen = purgeState === "open";
    purgeBtn.textContent = isOpen ? "CLOSE" : "PURGE";
    purgeBtn.classList.toggle("purge-open", isOpen);
  }

  async function togglePurge() {
    const nextState = purgeState === "open" ? "closed" : "open";

    try {
      const headers = { "Content-Type": "application/json" };
      if (window.NOVA_CLIENT_ID) headers["X-Client-Id"] = window.NOVA_CLIENT_ID;

      const res = await fetch(`${BASE_URL}/api/commands`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          type: "servo",
          name: "BVOTP",
          state: nextState
        })
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      purgeState = nextState;
      setPurgeButton();
    } catch (err) {
      console.error("Purge toggle error:", err);
      alert("Failed to send PURGE/CLOSE command for BVOTP.");
    }
  }

  async function updateConfig() {
    try {
      const res = await fetch(`${BASE_URL}/api/config/reload`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      updateStatus.textContent = "Config reloaded.";
    } catch (err) {
      console.error("Update config error:", err);
      updateStatus.textContent = "Failed to reload config.";
    }
  }

  let isSaving = false;
  async function toggleSaving() {
    isSaving = !isSaving;
    saveBtn.textContent = isSaving ? "Stop Saving Data" : "Start Saving Data";

    try {
      const res = await fetch(`${BASE_URL}/api/flags/data-saving`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: isSaving })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error("Saving toggle error:", err);
      isSaving = !isSaving;
      saveBtn.textContent = isSaving ? "Stop Saving Data" : "Start Saving Data";
    }
  }

  async function downloadData() {
    try {
      const listRes = await fetch(`${BASE_URL}/api/data-files`);
      if (!listRes.ok) throw new Error(`HTTP ${listRes.status}`);

      const files = await listRes.json();
      if (!Array.isArray(files) || files.length === 0) {
        alert("No CSV files available.");
        return;
      }

      const fileName = files[files.length - 1];
      const fileRes = await fetch(`${BASE_URL}/api/data-files/${encodeURIComponent(fileName)}`);
      if (!fileRes.ok) throw new Error(`HTTP ${fileRes.status}`);

      const blob = await fileRes.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Download error:", err);
      alert("Failed to download CSV.");
    }
  }

  let calibrationEnabled = true;
  async function toggleCalibration() {
    calibrationEnabled = !calibrationEnabled;
    calibrationBtn.textContent = calibrationEnabled ? "Get Uncalibrated Values" : "Get Calibrated Values";

    try {
      const res = await fetch(`${BASE_URL}/api/flags/calibration`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: calibrationEnabled })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error("Calibration toggle error:", err);
      calibrationEnabled = !calibrationEnabled;
      calibrationBtn.textContent = calibrationEnabled ? "Get Uncalibrated Values" : "Get Calibrated Values";
    }
  }
})();

