# NovaOps Backend

## Overview
TBD

## Directory Structure
```bash
/novaOps
│
├── app/
│   ├── 
│   └── static/
├── config/
├── Dockerfile                      # Docker instructions for building the FastAPI app image
└── README.md                       # This README file
```


## Features

- MQTT subscribe/publish integration
- WebSocket channel for parsed sensor data and actuator states
- Config-driven sensor and actuator parsing
- Linear interpolation calibration and rolling average
- REST API for config, flags, data files, sensors, and actuators
- Logging to console and rotating file logs
- Unit tests for parsing and command translation


## Important Notes


## API Summary

- `GET /api/actuators`
- `GET /api/sensors`
- `GET /api/config`
- `POST /api/config/upload`
- `PUT /api/config`
- `PATCH /api/config`
- `POST /api/config/reload`
- `POST /api/flags/calibration`
- `POST /api/flags/data-saving`
- `GET /api/data-files`
- `GET /api/data-files/{file_name}`
- `POST /api/commands`
- `WS /ws?role=operator|pad|viewer|dev`

## MQTT

Environment variables:

- `NOVA_MQTT_BROKER` (default `localhost`)
- `NOVA_MQTT_PORT` (default `1883`)

Topics:

- Subscribe: `nova/telemetry`
- Publish commands: `nova/command`
- Publish controls: `nova/control`

## Requirements
To run this project, you will need Docker and Docker Compose installed on your machine. Installation guides for Docker can be found [here](https://docs.docker.com/get-docker/) and for Docker Compose [here](https://docs.docker.com/compose/install/).

## Running the Application
1. **Clone the Repository:**
   ```bash
   git clone https://github.com/UTATRocketry/novaOps-back.git
   cd /path/to/novaOps-back
   git checkout fastapi_server
   ```

### Without Docker
Broker values:
- `localhost` (or `local`)
- `hivemq` (maps to `broker.hivemq.com`)
- any custom host string

2. **Run the dev scripts**
#### Linux

```bash
bash scripts/run_dev_linux.sh --broker localhost
bash scripts/run_dev_linux.sh --broker hivemq --with-dummy
```

#### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_dev_windows.ps1 -Broker localhost
powershell -ExecutionPolicy Bypass -File scripts/run_dev_windows.ps1 -Broker hivemq -WithDummy
```


### With Docker

2. **Build and Run the Docker Containers:**
   ```bash
   sudo docker-compose up --build
   ```

3. **Viewing the Application:**
   Once the container is running, you can access the application in your browser at: `http://localhost:8000`
    To see the stdin, stdout, stderr from the container run:
    ```
    docker attach CONTAINER
    ```
4. **Stopping the Application:**
   To stop the application, use CTRL+C in the original terminal where you started the app or run `sudo docker-compose stop` in a second terminal within the novaOps-back directory.


## To setup a Raspberry Pi with the Config Scripts
1. **Clone the Repository:**
   ```bash
   git clone https://github.com/UTATRocketry/novaOps-back.git
   cd /path/to/novaOps-back
   git checkout fastapi_server
   ```
2. **Update Log File Path:**
`# Save a log of all commands and their output`
Update line 4 `exec > /path/to/nova/session.log 2>&1` of the `initial_config.sh` file, so that the file path is accurate

3. **Make the scripts executable:**
```bash
chmod +x /path/to/nova/initial_config.sh
chmod +x /path/to/nova/post_reboot_config.sh
```
4. **Run the scripts:**
- Run `initial_config.sh` wait for it to reboot the Pi and then run `post_reboot_config.sh`. 
- This will start the server on `http://192.168.0.1:8000`. Use that if connected by ethernet or `http://raspberrypi.local:8000` to see the application in a browser. 
- Use `sudo docker-compose stop` to stop and `sudo docker-compose up` to run again. 
- The HTTP data endpoint is at `http://raspberrypi.local:8000/front` and the Websocket data endpoint is at `http://raspberrypi.local:8000/ws_basic`

## Troubleshooting

**Stopping the Application:**
To stop the application and remove the containers entirely, run:
   ``` bash
   sudo docker-compose down
   ``` 
**Can't connect to server via client:**
- Check if server is accessible from the pi at `http://0.0.0.0:8000`
- Check if the static IP address is configured correctly with `ip addr show eth0`
- Check if DHCP is enabled and running with `sudo systemctl status dhcpcd`
- Check if DNS is enabled and running with `sudo systemctl status dnsmasq`
- Check if the Docker container is running with `sudo docker ps`