#!/usr/bin/env bash
# GCE startup script: install Docker and prepare the app directory.
#
# Runs as root on every boot, so every step is idempotent. gcp-deploy.sh waits for
# /var/lib/news-fanout-startup-done before it copies files in.
set -euo pipefail

exec > >(tee -a /var/log/news-fanout-startup.log) 2>&1
echo "[startup] begin $(date -Is)"

if ! command -v docker >/dev/null 2>&1; then
    echo "[startup] installing docker"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates
    # Convenience script pulls in the compose v2 plugin as well.
    curl -fsSL https://get.docker.com | sh
else
    echo "[startup] docker already installed"
fi

systemctl enable --now docker

# Let the login user drive docker without sudo.
for candidate in $(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1}'); do
    usermod -aG docker "$candidate" 2>/dev/null || true
done

install -d -m 0755 /opt/news-fanout

# Cap total container log size as a second line of defence next to the per-service
# limits in docker-compose.prod.yml.
if [ ! -f /etc/docker/daemon.json ]; then
    cat >/etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
    systemctl restart docker
fi

touch /var/lib/news-fanout-startup-done
echo "[startup] done $(date -Is)"
