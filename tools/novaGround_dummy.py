import json
import random
import time
from datetime import datetime
import math

import paho.mqtt.client as mqtt

BROKER = "broker.hivemq.com"
PORT = 1883
TOPIC = "nova/telemetry"

CLIENT_ID = "novaground"

NUM_HATS = 2
CHANNELS = list(range(8))

GPIO_INPUTS = [5, 6]


def generate_sensor_data():
    sensors = []

    timestamp = int(time.time() * 1000)

    for hat in range(NUM_HATS):
        for ch in CHANNELS:
            sensors.append({
                "hat_id": hat,
                "channel_id": ch,
                "value": 2.5 + 2.5 * math.sin(time.time()) + random.uniform(-0.1,0.1),
                "timestamp": timestamp
            })

    return sensors


def generate_gpio_data():
    gpios = []

    for pin in GPIO_INPUTS:
        gpios.append({
            "pin_id": pin,
            "state": random.randint(0, 1)
        })

    return gpios


def main():

    client = mqtt.Client(client_id=CLIENT_ID)

    client.connect(BROKER, PORT, 60)

    print("Connected to MQTT broker")

    try:
        while True:

            payload = {
                "sensors": generate_sensor_data(),
                #"gpios": generate_gpio_data()
            }

            payload_str = json.dumps(payload)

            client.publish(TOPIC, payload_str)

            print(payload_str)

            time.sleep(0.05)  # 20 Hz

    except KeyboardInterrupt:
        print("Stopping simulator")
        client.disconnect()


if __name__ == "__main__":
    main()