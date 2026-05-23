#!/bin/bash
set -e

DEPLOY_DIR="/opt/agentic-ai/deployments/backend"
APP_DIR="/opt/agentic-ai/agentic_ai_backend"
ROOT_DIR="/opt/agentic-ai"

echo "Starting backend deployment..."

mkdir -p "$APP_DIR"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "ERROR: Production .env file is missing at $APP_DIR/.env"
  exit 1
fi

rsync -a --delete \
  --exclude ".git" \
  --exclude ".env" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude "data/cache" \
  "$DEPLOY_DIR/" "$APP_DIR/"

cd "$ROOT_DIR"

echo "Building backend Docker container..."
docker compose build backend

echo "Starting backend Docker container..."
docker compose up -d backend

echo "Restarting nginx..."
docker compose restart nginx

echo "Cleaning old Docker images..."
docker image prune -f

echo "Backend deployment completed."