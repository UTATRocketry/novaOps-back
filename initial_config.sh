#!/bin/bash

# Update and upgrade the system
echo "Updating system..."
sudo apt update && sudo apt upgrade -y

# Install required packages
echo "Installing packages..."
sudo apt install ufw
sudo apt install -y dnsmasq docker-compose
sudo apt install mosquitto mosquitto-clients # Check with sudo systemctl status mosquitto
sudo apt-get install -y nodejs npm

# Backup existing dhcpcd.conf and create a new one
echo "Backing up Files..."
sudo cp /etc/dhcpcd.conf /etc/dhcpcd.conf.v1

# Install dhcpcd5 and desktop ui
echo "Installing packages..."
sudo apt install dhcpcd5 --fix-missing
sudo apt install --reinstall raspberrypi-ui-mods

#firewall set up
echo "Setting up firewall ..."
sudo ufw allow 8000/tcp
sudo ufw allow 8000/udp
sudo ufw disable
# sudo ufw status

# Backup existing dhcpcd.conf,dnsmasq.conf, and host files
echo "Backing up Files..."
sudo mv /etc/dhcpcd.conf /etc/dhcpcd.conf.v2
sudo cp /etc/dnsmasq.conf /etc/dnsmasq.conf.orig
sudo cp /etc/hosts /etc/hosts.orig


# Set a static IP address for Ethernet (eth0)
echo "Configuring static IP address for for the Raspberry Pi Ethernet..."
sudo bash -c 'cat << EOF >> /etc/dhcpcd.conf
# A sample configuration for dhcpcd.
# See dhcpcd.conf(5) for details.

# Allow users of this group to interact with dhcpcd via the control socket.
#controlgroup wheel

# Inform the DHCP server of our hostname for DDNS.
#hostname

# Use the hardware address of the interface for the Client ID.
#clientid
# or
# Use the same DUID + IAID as set in DHCPv6 for DHCPv4 ClientID as per RFC4361.
# Some non-RFC compliant DHCP servers do not reply with this set.
# In this case, comment out duid and enable clientid above.
duid

# Persist interface configuration when dhcpcd exits.
persistent

# vendorclassid is set to blank to avoid sending the default of
# dhcpcd-<version>:<os>:<machine>:<platform>
vendorclassid

# A list of options to request from the DHCP server.
option domain_name_servers, domain_name, domain_search
option classless_static_routes
# Respect the network MTU. This is applied to DHCP routes.
option interface_mtu

# Request a hostname from the network
option host_name

# Most distributions have NTP support.
#option ntp_servers

# Rapid commit support.
# Safe to enable by default because it requires the equivalent option set
# on the server to actually work.
option rapid_commit

# A ServerID is required by RFC2131.
require dhcp_server_identifier

# Generate SLAAC address using the Hardware Address of the interface
#slaac hwaddr
# OR generate Stable Private IPv6 Addresses based from the DUID
slaac private

interface eth0
static ip_address=192.168.0.1
static routers=192.168.0.1
static domain_name_servers=192.168.0.1 8.8.8.8
EOF'

# Configure dnsmasq for DHCP
echo "Setting up dnsmasq for DHCP..."
sudo bash -c 'cat << EOF > /etc/dnsmasq.conf
interface=eth0
bind-dynamic
domain-needed
bogus-priv
expand-hosts
domain=local
dhcp-range=192.168.0.2,192.168.0.150,255.255.255.0,12h
EOF'

#Add a local host to the hosts file
echo "Adding local host..."
sudo bash -c 'cat << EOF > /etc/hosts
127.0.0.1	localhost
::1		localhost ip6-localhost ip6-loopback
ff02::1		ip6-allnodes
ff02::2		ip6-allrouters

127.0.1.1	raspberrypi
192.168.0.1 novaOps
EOF'

# Add custom Mosquitto configuration file
echo "Customizing Mosquitto configuration..."
sudo touch /etc/mosquitto/conf.D/novamqtt.conf
sudo bash -c 'cat << EOF > /etc/mosquitto/conf.D/novamqtt.conf
listener 1883
allow_anonymous true
EOF'

echo "Restarting Mosquitto..."
sudo systemctl restart mosquitto

# Check if Docker is installed
echo "Installing Docker..."

if command -v docker &> /dev/null
then
    echo "Docker is already installed."
else
    echo "Docker is not installed. Installing Docker..."
    # Install Docker
    curl -sSL https://get.docker.com | sh
    # Add the current user to the Docker group
    sudo usermod -aG docker {$USER}
    echo "Docker installation complete. Please log out and log back in to apply group changes."
fi

#reboot
echo "Rebooting Raspberry Pi..."
sudo reboot

