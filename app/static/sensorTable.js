(function () {
  "use strict";

const WS_URL = window.APP_CONFIG?.WS_URL || `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
const wsUrl = `${WS_URL}/ws?role=operator`;

let socket;
let reconnectAttempts = 0;
const maxReconnectAttempts = 10;
const reconnectInterval = 2000;

// Desired role for this control UI. Operator can issue any command; hazardous
// commands remain gated per-state by the physical lockout on the backend.
const DESIRED_ROLE = "operator";
let roleRequested = false;

const BASE_URL = window.APP_CONFIG?.BASE_URL || window.location.origin;

const statusElem = document.getElementById("status");
const reconnectStatusElem = document.getElementById("reconnect-status");
const sensorTableBody = document.getElementById("sensor-table-body");
const gpioTableBody = document.getElementById("gpio-table-body");

gpioTableBody.innerHTML = "<tr><td colspan='2'>GPIO stream is not provided by this backend.</td></tr>";

// Persistent rows keyed by sensor name. The table is built once from config so
// that GCS and FAS streams (which arrive in separate packets, at different
// rates) each update their own rows instead of replacing the whole table.
const sensorRows = new Map();

function createSensorRow(name) {
  const row = document.createElement("tr");
  const nameCell = document.createElement("td");
  const valueCell = document.createElement("td");
  const avgCell = document.createElement("td");

  nameCell.textContent = name || "Unnamed";
  valueCell.textContent = "N/A";
  avgCell.textContent = "N/A";

  row.appendChild(nameCell);
  row.appendChild(valueCell);
  row.appendChild(avgCell);
  sensorTableBody.appendChild(row);

  const entry = { row, valueCell, avgCell };
  sensorRows.set(name, entry);
  return entry;
}

function buildSensorTable(names) {
  sensorTableBody.innerHTML = "";
  sensorRows.clear();
  if (!Array.isArray(names) || names.length === 0) {
    sensorTableBody.innerHTML = "<tr><td colspan='4'>No sensor data available.</td></tr>";
    return;
  }
  names.forEach((name) => createSensorRow(name));
}

async function loadSensorTableFromConfig() {
  try {
    const res = await fetch(`${BASE_URL}/api/sensors`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const sensors = await res.json();
    const names = Array.isArray(sensors)
      ? sensors.map((s) => s.name).filter(Boolean)
      : [];
    buildSensorTable(names);
  } catch (err) {
    // Fall back to building rows lazily from incoming telemetry.
    console.error("Failed to load sensor config:", err);
  }
}

// Update only the sensors present in this packet; leave all other rows intact.
function updateSensorTable(sensors) {
  if (!Array.isArray(sensors)) return;

  sensors.forEach((sensor) => {
    const name = sensor.name || "Unnamed";
    const entry = sensorRows.get(name) || createSensorRow(name);
    if (sensor.value !== undefined && sensor.value !== null) {
      entry.valueCell.textContent = sensor.value;
    }
    if (sensor.avg !== undefined && sensor.avg !== null) {
      entry.avgCell.textContent = sensor.avg;
    }
  });
}

loadSensorTableFromConfig();

function connectWebSocket() {
  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    statusElem.textContent = "Connected";
    statusElem.style.color = "green";
    reconnectStatusElem.textContent = "";
    reconnectAttempts = 0;
    // Fresh connection starts as viewer; allow one re-elevation request.
    roleRequested = false;
  };

  socket.onmessage = (event) => {
    try {
      const message = JSON.parse(event.data);
      
      if (message.type === "session") {
        // Expose the client id so REST command POSTs can authorize via X-Client-Id.
        window.NOVA_CLIENT_ID = message.client_id || null;
        // All clients connect as "viewer". Self-elevate once so this control UI
        // can send actuator commands. The backend replies to a role_request with
        // a fresh "session" message, so guard against re-requesting in a loop.
        if (!roleRequested && message.role !== DESIRED_ROLE) {
          roleRequested = true;
          try {
            socket.send(JSON.stringify({ type: "role_request", role: DESIRED_ROLE }));
          } catch (err) {
            console.error(`Failed to request ${DESIRED_ROLE} role:`, err);
          }
        }
        window.dispatchEvent(new CustomEvent("nova:session", { detail: message }));
      }

      if (message.type === "parsed_data" && Array.isArray(message.sensors)) {
        updateSensorTable(message.sensors);
      }

      if (message.type === "actuator_states") {
        window.dispatchEvent(new CustomEvent("nova:actuator_states", { detail: message.actuator_states || {} }));
      }

      if (message.type === "snapshot") {
        window.dispatchEvent(new CustomEvent("nova:actuator_states", { detail: message.actuator_states || {} }));
      }
    } catch (err) {
      console.error("Error parsing websocket message:", err);
    }
  };

  socket.onerror = (error) => {
    statusElem.textContent = "Error";
    statusElem.style.color = "red";
    console.error("WebSocket error:", error);
  };

  socket.onclose = () => {
    statusElem.textContent = "Disconnected";
    statusElem.style.color = "orange";
    attemptReconnect();
  };
}

function attemptReconnect() {
  if (reconnectAttempts < maxReconnectAttempts) {
    reconnectAttempts += 1;
    reconnectStatusElem.textContent = `Reconnecting... (${reconnectAttempts})`;
    setTimeout(connectWebSocket, reconnectInterval);
  } else {
    reconnectStatusElem.textContent = "Failed to reconnect.";
    reconnectStatusElem.style.color = "red";
  }
}

connectWebSocket();

})();
