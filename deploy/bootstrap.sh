#!/usr/bin/env bash
# One-time EC2 bootstrap for YADA.
# Usage: ./deploy/bootstrap.sh <public-ip> <letsencrypt-email>
set -euo pipefail

PUBLIC_IP="${1:?usage: bootstrap.sh <public-ip> <email>}"
EMAIL="${2:?usage: bootstrap.sh <public-ip> <email>}"
HOSTNAME="${PUBLIC_IP}.nip.io"

echo "==> installing docker, nginx, certbot, git"
sudo apt-get update -y
sudo apt-get install -y docker.io docker-compose-v2 docker-buildx nginx certbot python3-certbot-nginx git

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

if [ ! -d yada-scraper-api ]; then
  echo "==> cloning repository"
  git clone https://github.com/karnyadavdev/YADA-Scraper-API.git yada-scraper-api
fi
cd yada-scraper-api

if [ ! -f .env ]; then
  echo "==> generating YADA_API_KEY"
  echo "YADA_API_KEY=$(openssl rand -hex 32)" > .env
  chmod 600 .env
fi

echo "==> nginx site for ${HOSTNAME}"
sed "s|{{IP}}|${PUBLIC_IP}|g" deploy/nginx.conf | sudo tee /etc/nginx/sites-available/yada >/dev/null
sudo ln -sf /etc/nginx/sites-available/yada /etc/nginx/sites-enabled/yada
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

echo "==> building and starting YADA (slim image)"
sudo docker compose up -d --build

echo "==> requesting HTTPS certificate"
sudo certbot --nginx -d "$HOSTNAME" --non-interactive --agree-tos -m "$EMAIL" || true

echo
echo "DONE."
echo "  API:      http://${HOSTNAME}/"
echo "  Health:   http://${HOSTNAME}/health"
echo "  Your API key is in .env (YADA_API_KEY) - needed for /scrape calls."

