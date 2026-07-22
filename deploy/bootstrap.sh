#!/usr/bin/env bash
# hermes-bootstrap.sh — one-time (re-runnable) host prep for running roger.
#
# Installs Docker + the compose plugin + age from apt (distro-maintained, security-patched)
# and sops from a pinned, checksum-verified release binary, puts the deploy user in the docker
# group, and enables Docker at boot. Touches no secrets and does not start roger.
#
#   ssh hermes 'sudo bash -s' < deploy/hermes-bootstrap.sh
#
# Idempotent: safe to run repeatedly.
set -euo pipefail

SOPS_VERSION="v3.13.2"
DEPLOY_USER="${SUDO_USER:-$(id -un)}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (use: sudo bash hermes-bootstrap.sh)." >&2
  exit 1
fi

log "Installing Docker engine, compose plugin, and age (apt)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 age ca-certificates curl

log "Enabling Docker at boot"
systemctl enable --now docker

if id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
  log "$DEPLOY_USER already in the docker group"
else
  log "Adding $DEPLOY_USER to the docker group (takes effect on next login/session)"
  usermod -aG docker "$DEPLOY_USER"
fi

# sops isn't packaged in apt — install a pinned, checksum-verified release binary.
if command -v sops >/dev/null 2>&1 && sops --version 2>/dev/null | grep -q "${SOPS_VERSION#v}"; then
  log "sops ${SOPS_VERSION} already installed"
else
  log "Installing sops ${SOPS_VERSION} (checksum-verified)"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  base="https://github.com/getsops/sops/releases/download/${SOPS_VERSION}"
  curl -fsSL "${base}/sops-${SOPS_VERSION}.linux.amd64" -o "${tmp}/sops"
  curl -fsSL "${base}/sops-${SOPS_VERSION}.checksums.txt" -o "${tmp}/checksums.txt"
  want="$(grep "sops-${SOPS_VERSION}.linux.amd64\$" "${tmp}/checksums.txt" | awk '{print $1}')"
  got="$(sha256sum "${tmp}/sops" | awk '{print $1}')"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    echo "sops checksum verification failed (want='$want' got='$got')" >&2
    exit 1
  fi
  install -m 0755 "${tmp}/sops" /usr/local/bin/sops
fi

log "Installed versions:"
docker --version
docker compose version | head -1
age --version | head -1
sops --version | head -1

cat <<'EOF'

==> Bootstrap complete. Next steps (see deploy/README.md):
    1. Generate the age key + provision /opt/roger and the encrypted roger.env.
    2. Install the deploy timer:  sudo bash deploy/install-systemd.sh
EOF
