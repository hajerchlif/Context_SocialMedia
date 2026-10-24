#!/usr/bin/env bash
# Deploy Context_SocialMedia on an Ubuntu Server (Docker + NVIDIA GPU).
# Usage:  curl -fsSL <raw-url>/scripts/deploy_ubuntu.sh | bash
#    or:  bash scripts/deploy_ubuntu.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/hajerchlif/Context_SocialMedia.git}"
APP_DIR="${APP_DIR:-$HOME/Context_SocialMedia}"
WITH_GPU="${WITH_GPU:-1}"      # 1 = install NVIDIA Container Toolkit (for the optional LLM)
WITH_LLM="${WITH_LLM:-0}"      # 1 = also start Ollama and pull the Qwen model

log(){ printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

log "1/6 System packages"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg git openssl

log "2/6 Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER" || true
sudo systemctl enable --now docker

if [ "$WITH_GPU" = "1" ]; then
  log "3/6 NVIDIA Container Toolkit (GPU in Docker)"
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "!! nvidia-smi not found: install the NVIDIA driver first (sudo ubuntu-drivers install), reboot, then re-run."
  else
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    sudo apt-get update -y
    sudo apt-get install -y nvidia-container-toolkit
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
  fi
else
  log "3/6 GPU skipped (WITH_GPU=0)"
fi

log "4/6 Get the code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p data config/seeds

log "5/6 Configuration (.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
  sed -i "s/^HMAC_SECRET=.*/HMAC_SECRET=$(openssl rand -hex 32)/" .env
  chmod 600 .env
  echo "-> .env created with random secrets. Edit it now to add your API keys: nano $APP_DIR/.env"
fi

log "6/6 Start"
if [ ! -d collector ]; then
  echo "!! The collector code (folder 'collector/') is not in the repo yet."
  echo "   Generate it with Claude Code in $APP_DIR using PROMPT_Collecteur_Multiplateforme.md,"
  echo "   push it, then run:  cd $APP_DIR && docker compose up -d --build"
  sudo docker compose up -d db
else
  sudo docker compose up -d --build
fi

if [ "$WITH_LLM" = "1" ]; then
  MODEL=$(grep -E '^OLLAMA_MODEL=' .env | cut -d= -f2); MODEL=${MODEL:-qwen3:14b}
  sudo docker compose --profile llm up -d ollama
  sleep 5
  sudo docker compose exec ollama ollama pull "$MODEL"
fi

log "Done."
echo "Status:   cd $APP_DIR && docker compose ps"
echo "Logs:     docker compose logs -f collector"
echo "Export:   docker compose --profile tools run --rm export"
echo "GPU test: docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"
echo "(Log out and back in once so that 'docker' works without sudo.)"
