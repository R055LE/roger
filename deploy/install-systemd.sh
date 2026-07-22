#!/usr/bin/env bash
# install-systemd.sh — install the roger-deploy script + poll timer on the host. Idempotent.
#
# Run from a checkout of deploy/ (not piped over stdin — it reads its sibling files):
#   scp -r deploy <host>:/tmp/roger-src
#   ssh <host> 'sudo bash /tmp/roger-src/install-systemd.sh'
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

install -m 0755 "$SRC/roger-deploy.sh" /usr/local/bin/roger-deploy

# The deploy runs as the (non-root) invoking user so Docker isn't driven as root and the age
# key stays in that user's home. Fill the unit's placeholder with whoever ran sudo.
DEPLOY_USER="${SUDO_USER:?run via sudo so the deploy user is known}"
sed "s/__DEPLOY_USER__/${DEPLOY_USER}/" "$SRC/roger-deploy.service" \
  > /etc/systemd/system/roger-deploy.service
chmod 0644 /etc/systemd/system/roger-deploy.service
install -m 0644 "$SRC/roger-deploy.timer" /etc/systemd/system/roger-deploy.timer

systemctl daemon-reload
systemctl enable --now roger-deploy.timer

echo "Installed. Timer:"
systemctl list-timers roger-deploy.timer --no-pager || true
echo
echo "Trigger an immediate deploy with:  sudo systemctl start roger-deploy.service"
echo "Follow deploy logs with:           journalctl -u roger-deploy.service -f"
