#!/usr/bin/env bash
# roger-deploy — pull the latest published image and (re)deploy roger.
#
# Installed to /usr/local/bin/roger-deploy and run by roger-deploy.timer every few minutes
# (also runnable by hand). Pulls ghcr.io/r055le/roger:main; `docker compose up -d` only
# recreates the container when the image digest (or config) actually changed, so a run with
# no new image is a cheap no-op. Secrets are injected at runtime by `sops exec-env` and never
# written to disk in the clear.
set -euo pipefail

DEPLOY_DIR="${ROGER_DEPLOY_DIR:-/opt/roger}"
cd "$DEPLOY_DIR"

# Serialize with the timer so an overlapping tick can't race a redeploy.
exec 9>"${DEPLOY_DIR}/.deploy.lock"
flock -n 9 || { echo "roger-deploy: another run holds the lock, skipping"; exit 0; }

# compose interpolates the whole file (including required ${VAR:?} vars) on every subcommand,
# so even `pull` needs the env populated — run inside the sops-decrypted environment.
echo "roger-deploy: pulling"
sops exec-env roger.env 'docker compose pull --quiet'

# Supply-chain gate (backlog 2.1): only run an image this repo's release workflow signed. cosign
# resolves :main to its current digest and checks the keyless signature against the workflow's OIDC
# identity. set -e means a bad/absent signature (or a missing cosign) aborts before `up` — fail closed.
# cosign must be on PATH for the systemd service (install to /usr/local/bin); see deploy/README.md.
IMAGE="ghcr.io/r055le/roger:main"
COSIGN_IDENTITY="https://github.com/R055LE/roger/.github/workflows/release.yml@refs/heads/main"
COSIGN_ISSUER="https://token.actions.githubusercontent.com"
echo "roger-deploy: verifying image signature (cosign)"
cosign verify \
  --certificate-identity "$COSIGN_IDENTITY" \
  --certificate-oidc-issuer "$COSIGN_ISSUER" \
  "$IMAGE" >/dev/null

echo "roger-deploy: applying"
sops exec-env roger.env 'docker compose up -d'

echo "roger-deploy: pruning superseded images"
docker image prune -f >/dev/null

echo "roger-deploy: done"
