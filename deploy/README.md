# Deploying roger to hermes

Pull-based continuous deployment. hermes sits on an isolated VLAN with **no inbound path from
the internet** (only `Admin → :22`), so nothing pushes *in* — the box only ever reaches *out*.

```
push to main ──▶ GitHub Actions ──▶ ghcr.io/r055le/roger:main
                                              │
                        hermes: roger-deploy.timer (every 5 min)
                                              │
                     docker compose pull + up -d  (secrets via sops)
```

- **`release.yml`** builds the image on every push to `main` and publishes it to GHCR
  (public, no secrets baked in — SBOM + provenance attestations attached).
- **`roger-deploy.timer`** on hermes polls that tag every 5 minutes and redeploys only when the
  image digest changed. Docker's `restart: unless-stopped` keeps roger running across reboots;
  the timer keeps it *current*.

## Pieces

| File | Role |
|---|---|
| `hermes-bootstrap.sh` | Install Docker + compose + age + sops on the host. Idempotent. |
| `roger-deploy.sh` | Pull + `up -d` + prune. Installed as `/usr/local/bin/roger-deploy`. |
| `roger-deploy.service` / `.timer` | systemd oneshot + 5-minute poll timer. |
| `install-systemd.sh` | Install the above into place and enable the timer. |

## First-time provision

Run from a checkout on a box that can `ssh hermes`.

```bash
# 1. Host prep: Docker, compose, age, sops.
ssh hermes 'sudo bash -s' < deploy/hermes-bootstrap.sh

# 2. Generate the age key ON hermes and print its PUBLIC recipient.
ssh hermes 'mkdir -p ~/.config/sops/age && \
  test -f ~/.config/sops/age/keys.txt || age-keygen -o ~/.config/sops/age/keys.txt; \
  age-keygen -y ~/.config/sops/age/keys.txt'
#    -> copy the age1... line into .sops.yaml (replace the placeholder), commit it.
#    -> back up ~/.config/sops/age/keys.txt somewhere safe (NAS). It is the only key that
#       can decrypt roger.env; it is NOT in git.

# 3. Provision the deploy dir and the encrypted env.
ssh hermes 'sudo install -d -o $USER -g $USER /opt/roger && \
            sudo install -d -o 10001 -g 10001 /opt/roger/data'
scp compose.yaml .sops.yaml hermes:/opt/roger/
scp roger.env    hermes:/opt/roger/roger.env      # plaintext, transits SSH only
ssh hermes 'cd /opt/roger && sops -e -i roger.env' # encrypt in place; never committed

# 4. Install the deploy timer.
scp -r deploy hermes:/tmp/roger-src
ssh hermes 'sudo bash /tmp/roger-src/install-systemd.sh'

# 5. First deploy (the timer also fires within ~2 min of boot / install).
ssh hermes 'sudo systemctl start roger-deploy.service'
ssh hermes 'docker compose -f /opt/roger/compose.yaml logs --tail 20 roger'
```

> **One-time GHCR step:** the first `release.yml` run publishes the package **private** by
> default. Make it public once at
> `github.com/users/R055LE/packages/container/roger/settings` → *Change visibility → Public*,
> so hermes can pull without a token. (After that, nothing else is manual.)

## Day-to-day

- **Ship a change:** push to `main`. CI gates it, the image builds, hermes redeploys within
  ~5 minutes. Nothing else to do.
- **Deploy now:** `ssh hermes 'sudo systemctl start roger-deploy.service'`.
- **Watch logs:** `ssh hermes 'journalctl -u roger-deploy.service -f'` (deploys) or
  `ssh hermes 'docker compose -f /opt/roger/compose.yaml logs -f roger'` (the bot).
- **Change config/secrets:** `ssh hermes 'cd /opt/roger && sops roger.env'`, save, then
  trigger a deploy. `up -d` recreates the container with the new env.

## Notes

- **Pinning vs. tracking.** Third-party deps (base image, Python packages, sops) are pinned.
  roger's *own* image tracks the `:main` channel on purpose — that's what continuous deploy is.
  For release-gated prod, point `compose.yaml` at a `:sha-<...>` tag and bump it deliberately.
- **Why not watchtower.** It needs the Docker socket (root-equivalent) and nudges toward
  auto-`latest`. A plain `compose pull` on a timer is a smaller attack surface and keeps the
  version story honest.
- **Later, over Tailscale.** Once the tailnet is up, a push-on-merge deploy (Actions → SSH over
  the mesh) becomes possible without exposing hermes. The pull timer is a fine permanent
  fallback either way.
