document.getElementById("update-button").onclick = updateConfig;
document.getElementById("save-button").onclick = toggleSaving;
document.getElementById("calibration-button").onclick = toggleCalibration;

function updateConfig() {
  fetch("/update_config")
    .then(res => res.json())
    .then(data => console.log("Updated config:", data))
    .catch(err => console.error("Update config error:", err));
}

let isSaving = false;
function toggleSaving() {
  const btn = document.getElementById('save-button');
  isSaving = !isSaving;
  btn.textContent = isSaving ? "Stop Saving Data" : "Start Saving Data";

  const endpoint = isSaving ? "/start_saving_data" : "/stop_saving_data";
  fetch(endpoint)
    .then(res => res.json())
    .then(data => console.log("Saving toggled:", data))
    .catch(err => console.error("Saving toggle error:", err));

  if (!isSaving) {
    fetch("/download_data_file")
      .then(response => {
        const disposition = response.headers.get("Content-Disposition");
        let filename = "data_file.csv";
        if (disposition) {
          const match = disposition.match(/filename="?(.+)"?/);
          if (match) filename = match[1];
        }
        return response.blob().then(blob => ({ blob, filename }));
      })
      .then(({ blob, filename }) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        URL.revokeObjectURL(url);
        a.remove();
      })
      .catch(err => console.error("Download error:", err));
  }
}

let calibrationFlag = true;
function toggleCalibration() {
  const btn = document.getElementById("calibration-button");
  calibrationFlag = !calibrationFlag;
  btn.textContent = calibrationFlag ? "Get Uncalibrated Values" : "Get Calibrated Values";

  fetch("/toggle_calibration", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ calibration: calibrationFlag }),
  })
    .then(res => res.json())
    .then(data => console.log("Calibration toggled:", data))
    .catch(err => console.error("Calibration toggle error:", err));
}


