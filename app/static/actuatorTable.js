(function () {
  "use strict";

const BASE_URL = window.APP_CONFIG?.BASE_URL || window.location.origin;

const lockButton = document.getElementById("lock-button");
const tableBody = document.getElementById("actuators-table");

let isLocked = false;
let actuatorCache = [];
const uiStates = {};

lockButton.onclick = () => {
  isLocked = !isLocked;
  lockButton.textContent = isLocked ? "Unlock Actuators" : "Lock Actuators";
};

function isServoType(type) {
  return type === "servo" || type === "servo3";
}

function isGpioType(type) {
  return type === "gpioDevice" || type === "poweredGpioDevice";
}

function needsEnableButton(actuator) {
  return isServoType(actuator.actuator_type);
}

function hasRelayPower(actuator) {
  return actuator.relayID !== null && actuator.relayID !== undefined;
}

function needsPowerButton(actuator) {
  const type = actuator.actuator_type;
  if (isServoType(type)) return hasRelayPower(actuator);
  return type === "poweredDevice" || type === "poweredGpioDevice";
}

function setButtonStyle(button, active, activeColor = "lightgrey",  activeBorder, inactiveColor = "gray", inactiveBorder = "black",) {
  button.style.border = "2px outset";
  button.style.borderColor = active ? activeBorder : inactiveBorder;
  button.style.backgroundColor = active ? activeColor : inactiveColor;
}

function supportsOpenClose(actuator) {
  const type = actuator.actuator_type;
  if (type === "solenoid") return true;
  if (!isServoType(type)) return false;

  const aliases = Array.isArray(actuator.position_aliases) ? actuator.position_aliases : [];
  if (aliases.length === 2) return true;

  const lower = aliases.map((a) => String(a).toLowerCase());
  return lower.includes("open") && lower.includes("closed");
}

function getOpenCloseStates(actuator) {
  const aliases = Array.isArray(actuator.position_aliases) ? actuator.position_aliases : [];
  if (aliases.length === 2) return { open: aliases[0], closed: aliases[1] };

  const lowerMap = new Map(aliases.map((a) => [String(a).toLowerCase(), a]));
  return {
    open: lowerMap.get("open") || "open",
    closed: lowerMap.get("closed") || "closed"
  };
}

function defaultUiState(actuator) {
  const aliases = Array.isArray(actuator.position_aliases) ? actuator.position_aliases : [];
  const openClose = getOpenCloseStates(actuator);

  return {
    openState: supportsOpenClose(actuator) ? openClose.closed : null,
    powerState: needsPowerButton(actuator) ? "off" : null,
    armingState: isGpioType(actuator.actuator_type) ? "disarmed" : null,
    positionState: aliases.length > 0 ? aliases[0] : null,
    enableState: needsEnableButton(actuator) ? "disabled" : null
  };
}

function ensureUiState(actuator) {
  if (!uiStates[actuator.name]) {
    uiStates[actuator.name] = defaultUiState(actuator);
  }
  return uiStates[actuator.name];
}

function syncUiStateWithConfig(actuators) {
  const known = new Set(actuators.map((a) => a.name));

  actuators.forEach((actuator) => {
    ensureUiState(actuator);
  });

  Object.keys(uiStates).forEach((name) => {
    if (!known.has(name)) {
      delete uiStates[name];
    }
  });
}

function applyStateUpdate(actuator, stateValue) {
  if (!actuator || !stateValue) return;

  if (typeof stateValue === "object") {
    const { power, enable, arming, position } = stateValue;
    if (power) applyStateUpdate(actuator, power);
    if (enable) applyStateUpdate(actuator, enable);
    if (arming) applyStateUpdate(actuator, arming);
    if (position) applyStateUpdate(actuator, position);
    return;
  }
  const state = String(stateValue);
  const lower = state.toLowerCase();
  const ui = ensureUiState(actuator);

  if (lower === "on" || lower === "off") {
    if (ui.powerState !== null) {
      ui.powerState = lower;
    }
    return;
  }

  if (lower === "enable" || lower === "enabled" || lower === "disable" || lower === "disabled") {
    if (ui.enableState !== null) {
      ui.enableState = lower === "enable" || lower === "enabled" ? "enabled" : "disabled";
    }
    return;
  }

  if (lower === "armed" || lower === "disarmed") {
    if (ui.armingState !== null) {
      ui.armingState = lower;
    }
    return;
  }

  if (supportsOpenClose(actuator)) {
    const { open, closed } = getOpenCloseStates(actuator);
    if (lower === String(open).toLowerCase()) {
      ui.openState = open;
      if (ui.positionState !== null) ui.positionState = open;
      return;
    }
    if (lower === String(closed).toLowerCase()) {
      ui.openState = closed;
      if (ui.positionState !== null) ui.positionState = closed;
      return;
    }
  }

  const aliases = Array.isArray(actuator.position_aliases) ? actuator.position_aliases : [];
  const alias = aliases.find((a) => String(a).toLowerCase() === lower);
  if (alias) {
    ui.positionState = alias;
  }
}

async function sendCommand(actuator, state) {
  const response = await fetch(`${BASE_URL}/api/commands`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: actuator.actuator_type,
      name: actuator.name,
      state
    })
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
}

async function onStateAction(actuator, nextState, options = {}) {
  const { requiresPower = false, requiresEnable = false } = options;
  if (isLocked) {
    alert("Actuators are locked. Please unlock to change states.");
    return;
  }

  const ui = ensureUiState(actuator);
  if (requiresPower && ui.powerState === "off") {
    alert("Device power is off. Please turn it on first.");
    return;
  }
  if (requiresEnable && ui.enableState === "disabled") {
    alert("Servo is disabled. Please enable PWM first.");
    return;
  }

  try {
    await sendCommand(actuator, nextState);
    applyStateUpdate(actuator, nextState);
    renderActuators(actuatorCache);
  } catch (err) {
    console.error("Command error:", err);
    alert(`Failed to send command for ${actuator.name}`);
  }
}

function renderActuators(actuators) {
  tableBody.innerHTML = "";

  if (!Array.isArray(actuators) || actuators.length === 0) {
    tableBody.innerHTML = "<tr><td colspan='2'>No actuators found in config.</td></tr>";
    return;
  }

  actuators.forEach((actuator) => {
    const ui = ensureUiState(actuator);
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    nameCell.style.width = "200px";
    nameCell.textContent = actuator.name;
    nameCell.title = `${actuator.name} (${actuator.actuator_type})`;

    const stateCell = document.createElement("td");
    stateCell.style.display = "flex";
    stateCell.style.gap = "6px";
    stateCell.style.flexWrap = "wrap";
    stateCell.style.textAlign = 'center';
    stateCell.style.justifyContent = 'center';

    if (supportsOpenClose(actuator)) {
      const { open, closed } = getOpenCloseStates(actuator);
      const isOpen = ui.openState === open;

      const openCloseBtn = document.createElement("button");
      openCloseBtn.textContent = isOpen ? open : closed;
      setButtonStyle(openCloseBtn, isOpen, "green", "darkgreen", "red", "darkred");
      openCloseBtn.onclick = () => onStateAction(actuator, isOpen ? closed : open, {
        requiresEnable: isServoType(actuator.actuator_type),
        requiresPower: isServoType(actuator.actuator_type) && needsPowerButton(actuator)
      });
      stateCell.appendChild(openCloseBtn);
    }

    if (isServoType(actuator.actuator_type)) {
      const aliases = Array.isArray(actuator.position_aliases) ? actuator.position_aliases : [];
      if (aliases.length > 2) {
        aliases.forEach((alias) => {
          const btn = document.createElement("button");
          const active = ui.positionState === alias;
          btn.textContent = alias;
          setButtonStyle(btn, active, "green", "darkgreen", "gray", "black");
          btn.onclick = () => onStateAction(actuator, alias, {
            requiresEnable: true,
            requiresPower: needsPowerButton(actuator)
          });
          stateCell.appendChild(btn);
        });
      }
      const enabled = ui.enableState === "enabled";
      const enableBtn = document.createElement("button");
      enableBtn.textContent = enabled ? "enabled" : "disabled";
      setButtonStyle(enableBtn, enabled, "#5d7cb8", "#1f3257", "gray", "black");
      enableBtn.onclick = () => onStateAction(actuator, enabled ? "disable" : "enable",{
        requiresPower: needsPowerButton(actuator)
      });
      stateCell.appendChild(enableBtn);
    }

    if (isGpioType(actuator.actuator_type)) {
      const armed = ui.armingState === "armed";
      const armBtn = document.createElement("button");
      armBtn.textContent = armed ? "armed" : "disarmed";
      setButtonStyle(armBtn, armed, "green", "darkgreen", "red", "darkred");
      armBtn.onclick = () => onStateAction(actuator, armed ? "disarmed" : "armed", {
        requiresPower: actuator.actuator_type === "poweredGpioDevice"
      });
      stateCell.appendChild(armBtn);
    }

    if (needsPowerButton(actuator)) {
      const enabled = ui.powerState === "on";
      const powerBtn = document.createElement("button");
      powerBtn.textContent = enabled ? "on" : "off";
      setButtonStyle(powerBtn, enabled, "green", "darkgreen", "red", "darkred");
      powerBtn.onclick = () => onStateAction(actuator, enabled ? "off" : "on");
      stateCell.appendChild(powerBtn);
    }

    row.appendChild(nameCell);
    row.appendChild(stateCell);
    tableBody.appendChild(row);
  });
}

async function fetchActuators() {
  try {
    const response = await fetch(`${BASE_URL}/api/actuators`, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    actuatorCache = await response.json();
    syncUiStateWithConfig(actuatorCache);
    renderActuators(actuatorCache);
  } catch (error) {
    console.error("Error fetching actuators:", error);
    tableBody.innerHTML = "<tr><td colspan='2'>Failed to load actuators.</td></tr>";
  }
}

window.addEventListener("nova:actuator_states", (event) => {
  const updates = event.detail || {};

  Object.entries(updates).forEach(([name, state]) => {
    const actuator = actuatorCache.find((a) => a.name === name);
    if (actuator) {
      applyStateUpdate(actuator, state);
    }
  });

  if (actuatorCache.length > 0) {
    renderActuators(actuatorCache);
  }
});

fetchActuators();

})();


