
const lockButton = document.getElementById('lock-button');
lockButton.onclick = toggleActuatorsLock;

let isLocked = false;
function toggleActuatorsLock() {
    isLocked = !isLocked;
    lockButton.textContent = isLocked ? 'Unlock Actuators' : 'Lock Actuators';
}
async function fetchActuators() {
    try {
        const response = await fetch('http://192.168.0.1:8000/get_actuators', {
            headers: { 'Accept': 'application/json' }
        });
        const actuators = await response.json();
        const tableBody = document.getElementById('actuators-table');
        tableBody.innerHTML = '';
        actuators.forEach(actuator => {
            // Ensure actuator has necessary properties
            actuator.openState = actuator.openState || "closed";
            actuator.powerState = actuator.powerState || 'off';
            actuator.armingState = actuator.armingState || 'disarmed'; 
            
            const row = document.createElement('tr');
            const nameCell = document.createElement('td');
            nameCell.style.width = '200px';
            nameCell.textContent = actuator.name;
            nameCell.title = actuator.name; // Show full name on hover
            row.appendChild(nameCell);

            const stateCell = document.createElement('td');
            stateCell.style.textAlign = 'center';
            //stateCell.style.display = 'flex';
            //stateCell.style.gap = '10px'; // Add some space between buttons
            stateCell.style.justifyContent = 'center';

            // devices that need a “open / closed” button
            if (['servo', 'solenoid'].includes(actuator.type)) {
                const openButton = document.createElement('button');
                openButton.style.border = '2px outset';
                openButton.style.borderColor = actuator.openState === 'open' ? 'darkgreen' : 'darkred';
                openButton.style.backgroundColor = actuator.openState === 'open' ? 'green' : 'red';
                openButton.textContent = actuator.openState === 'open' ? 'open' : 'closed';
                openButton.onclick = async () => {
                    if (isLocked) {
                        alert('Actuators are locked. Please unlock to change states.');
                    }
                    else if (actuator.type === 'servo' && actuator.powerState === 'off') {
                        alert('Servo is disabled. Please enable it first.');
                    }
                    else {
                        const newOpenState = actuator.openState === 'open' ? 'closed' : 'open';
                        // sendCommand(actuator.name, actuator.type, newState);
                        try {
                            const response = await fetch('http://192.168.0.1:8000/send_command', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ type: actuator.type, name: actuator.name, state: newOpenState })
                            });
                            if (!response.ok) {
                                alert('Failed to send command. Please try again.');
                            }
                            else if (response.status === 200) {
                                // Command was sent successfully
                                console.log(`Command sent successfully for ${actuator.name}`);
                                actuator.openState = newOpenState; // Update the state locally
                                openButton.style.borderColor = newOpenState === 'open' ? 'darkgreen' : 'darkred';
                                openButton.style.backgroundColor = newOpenState === 'open' ? 'green' : 'red';
                                openButton.textContent = newOpenState === 'open' ? 'open' : 'closed';
                            }
                            // fetchActuators(); // Refresh the table after sending the command
                        } catch (error) {
                            console.error('Error sending command:', error);
                        }
                        
                    }
                };
                stateCell.appendChild(openButton);
            }

            // devices that need an arming button
            if (['gpioDevice', 'poweredGpioDevice'].includes(actuator.type)) {
                const armingButton = document.createElement('button');
                armingButton.setAttribute("id", `${actuator.name}-armingButton`)
                armingButton.style.border = '2px outset';
                armingButton.style.borderColor = actuator.armingState === 'armed' ? 'darkgreen' : 'darkred';
                armingButton.style.backgroundColor = actuator.armingState === 'armed'  ? 'green' : 'red';
                armingButton.textContent = actuator.armingState === 'armed' ? 'armed' : 'disarmed';
                armingButton.onclick = async () => {
                    if (isLocked) {
                        alert('Actuators are locked. Please unlock to change states.');
                    }
                    else if (actuator.type === 'poweredGpioDevice' && actuator.powerState === 'off') {
                        alert('Device is disabled. Please enable it first.');
                    }
                    else {
                        const newArmingState = actuator.armingState === 'armed' ? 'disarmed' : 'armed';
                        // sendCommand(actuator.name, actuator.type, newState);
                        try {
                            const response = await fetch('http://192.168.0.1:8000/send_command', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ type: actuator.type, name: actuator.name, state: newArmingState })
                            });
                            if (!response.ok) {
                                alert('Failed to send command. Please try again.');
                            }
                            else if (response.status === 200) {
                                // Command was sent successfully
                                actuator.armingState = newArmingState; // Update the state locally
                                console.log(`Command sent successfully for ${actuator.name}`);
                                armingButton.style.borderColor = newArmingState === 'armed' ? 'darkgreen' : 'darkred';
                                armingButton.style.backgroundColor = newArmingState === 'armed' ? 'green' : 'red';
                                armingButton.textContent = newArmingState === 'armed' ? 'armed' : 'disarmed';
                            // fetchActuators(); // Refresh the table after sending the command
                            }
                        } catch (error) {
                            console.error('Error sending command:', error);
                        }
                        
                    }
                };
                stateCell.appendChild(armingButton);
            }

            /* ---------- 3-way servo valve ---------- */
            if (actuator.type === 'servo3') {
                // ensure backend sent a current state
                const positions = actuator.positions || ['1', '2', '3'];  // label each port
                actuator.valveState = actuator.valveState || positions[1]; // default to first position if not set
                const borderColours = { active: 'darkgreen', idle: "black" }; // border colours for each button
                const colours   = { active: 'green', idle: 'gray' }; // lightsteelblue steelblue cornflowerblue DodgerBlue
            
                positions.forEach(position => {
                const btn = document.createElement('button');
                btn.setAttribute("id", `${actuator.name}-btn-${position}`)
                btn.textContent = position;
                btn.style.margin = '0 1px';
                btn.style.border = '2px outset';
                btn.style.borderColor = 
                    (actuator.valveState === position ? borderColours.active : borderColours.idle);
                btn.style.backgroundColor =
                    (actuator.valveState === position ? colours.active : colours.idle);

                btn.onclick = async () => {
                    if (isLocked) {
                        alert('Actuators are locked. Please unlock to change states.'); return;
                    }
                    if (actuator.powerState === 'off') {
                        alert('Servo is disabled. Please enable it first.'); return;
                    }
            
                    try {
                        const response = await fetch('http://192.168.0.1:8000/send_command', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                            type: actuator.type,          // "servo3"
                            name: actuator.name,
                            state: position               // "1" | "2" | "3"
                            })
                        });
                        if (!response.ok) {
                            alert('Failed to send command. Please try again.');
                        }
                        else if (response.status === 200) {
                            console.log(`Command sent successfully for ${actuator.name} to position ${position}`);
                        
                            // update local shadow copy & button colours
                            actuator.valveState = position;
                            // repaint all three buttons
                            Array.from(stateCell.children).forEach(el => {
                                if (el.id !== `${actuator.name}-powerButton`) {
                                    el.style.backgroundColor =
                                        (el.textContent === position ? colours.active : colours.idle);
                                    el.style.borderColor =
                                        (el.textContent === position ? borderColours.active : borderColours.idle);
                                }
                            });
                        }
                    } catch (err) {
                    console.error('Error sending command:', err);
                    }
                };
            
                stateCell.appendChild(btn);
                });
            }
            // devices that need a power-enable button
            if (['servo', 'servo3', 'poweredDevice', 'poweredGpioDevice'].includes(actuator.type)) {
                const powerButton = document.createElement('button');
                powerButton.setAttribute("id", `${actuator.name}-powerButton`)
                powerButton.style.border = '2px outset';
                powerButton.style.borderColor = 'black';
                powerButton.style.backgroundColor = actuator.powerState === 'on' ? 'lightgrey' : 'gray';
                powerButton.textContent = actuator.powerState === 'on' ? 'enabled' : 'disabled';
                powerButton.onclick = async () => {
                    if (isLocked) {
                        alert('Actuators are locked. Please unlock to change states.');
                    }
                    else {
                        const newPowerState = actuator.powerState === 'on' ? 'off' : 'on';
                        // sendCommand(actuator.name, actuator.type, newState);
                        try {
                            const response = await fetch('http://192.168.0.1:8000/send_command', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ type: actuator.type, name: actuator.name, state: newPowerState })
                            });
                            if (!response.ok) {
                                alert('Failed to send command. Please try again.');
                            }
                            else if (response.status === 200) {
                                // Command was sent successfully
                                console.log(`Command sent successfully for ${actuator.name}`);
                                actuator.powerState = newPowerState; // Update the state locally
                                powerButton.style.backgroundColor = newPowerState === 'on' ? 'lightgrey' : 'gray';
                                powerButton.textContent = newPowerState === 'on' ? 'enabled' : 'disabled';
                            }
                            // fetchActuators(); // Refresh the table after sending the command
                        } catch (error) {
                            console.error('Error sending command:', error);
                        }
                        
                        
                    }
                };
                stateCell.appendChild(powerButton);
            }
            row.appendChild(stateCell);

            tableBody.appendChild(row);
        });
    } catch (error) {
        console.error('Error fetching actuators:', error);
    }
}
async function sendCommand(name, type, state) {
    try {
        const response = await fetch('http://192.168.0.1:8000/send_command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type, name, state })
        });
        if (!response.ok) {
            alert('Failed to send command. Please try again.');
            throw new Error('Network response was not ok');
        }
        // fetchActuators(); // Refresh the table after sending the command
    } catch (error) {
        console.error('Error sending command:', error);
    }
}

fetchActuators();