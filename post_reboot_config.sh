#!/bin/bash

# manual eth0 IP  setting
echo "Configuring IP address"
sudo ifconfig eth0 192.168.0.1 netmask 255.255.255.0 # Check with ip addr show eth0
# sudo ifconfig wlan0 172.20.10.1 netmask 255.255.255.0

# Ask if the user wants to enable the container to run at boot
read -p "Would you like Docker to run at boot? (y/n): " run_at_boot

if [ "$run_at_boot" == "y" ]; then
    echo "Setting up Docker to run at boot..."
    sudo systemctl enable docker
    sudo systemctl start docker
    echo "The container will now start automatically at boot."
else
    echo "Skipping auto-start configuration."
fi

# Build and run Docker container using docker-compose
echo "Building FastAPI container..."
sudo docker-compose build
sudo docker-compose up --no-start

# Start DHCP service
echo "Starting dhcpcd..."
sudo systemctl enable dhcpcd
sudo systemctl start dhcpcd

# Restart the necessary services
echo "Restarting services to apply changes..."
# Restart DHCP client daemon 
sudo systemctl restart dhcpcd  # Check with sudo systemctl status dhcpcd 
# Restart dnsmasq to apply changes
sudo service dnsmasq restart # Check with sudo systemctl status dnsmasq

# Start the Docker container using docker-compose
echo "Starting FastAPI container..."
sudo docker-compose up