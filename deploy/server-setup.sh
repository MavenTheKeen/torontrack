#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server (tested on Oracle Cloud Ampere A1, arm64).
# Run as the default sudo user: bash deploy/server-setup.sh
set -euo pipefail

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install \
  ca-certificates curl git unattended-upgrades iptables-persistent

# Docker Engine and the Compose plugin from Docker's own repository.
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" |
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Rotate container logs so a chatty service cannot fill the disk.
echo '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}' |
  sudo tee /etc/docker/daemon.json >/dev/null
sudo systemctl restart docker
sudo usermod -aG docker "$USER"

# Oracle's Ubuntu images reject inbound traffic other than SSH in iptables, on top of
# the cloud security list. Allow HTTP and HTTPS (TCP, and UDP for HTTP/3) ahead of the
# final REJECT rule, and keep the rules across reboots.
reject=$(sudo iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" {print $1; exit}')
for rule in "tcp 80" "tcp 443" "udp 443"; do
  set -- $rule
  if ! sudo iptables -C INPUT -p "$1" --dport "$2" -j ACCEPT 2>/dev/null; then
    if [ -n "$reject" ]; then
      sudo iptables -I INPUT "$reject" -p "$1" --dport "$2" -j ACCEPT
    else
      sudo iptables -A INPUT -p "$1" --dport "$2" -j ACCEPT
    fi
  fi
done
sudo netfilter-persistent save

# Install security updates automatically.
echo 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null

echo "Setup complete. Log out and back in so the docker group applies."
