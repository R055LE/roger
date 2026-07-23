# Deploying roger

Pull-based continuous deployment, built for a host with **no inbound path from the internet**.
Nothing pushes *in* — the host only ever reaches *out*, so no ports are exposed and no deploy
credential lives off-box.

```
push to main ──▶ GitHub Actions ──▶ ghcr.io/r055le/roger:main
                                              │
                        deploy host: roger-deploy.timer (every 5 min)
                                              │
                     docker compose pull + up -d  (secrets via sops)
```

- **`release.yml`** builds the image on every push to `main` and publishes it to GHCR
  (public, no secrets baked in — SBOM + provenance attestations attached).
- **`roger-deploy.timer`** on the host polls that tag every 5 minutes and redeploys only when the
  image digest changed. Docker's `restart: unless-stopped` keeps roger running across reboots;
  the timer keeps it *current*.

## Pieces

| File | Role |
|---|---|
| `bootstrap.sh` | Install Docker + compose + age + sops on the host. Idempotent. |
| `roger-deploy.sh` | Pull + `up -d` + prune. Installed as `/usr/local/bin/roger-deploy`. |
| `roger-deploy.service` / `.timer` | systemd oneshot + 5-minute poll timer. |
| `install-systemd.sh` | Install the above and enable the timer (runs the deploy as the invoking user). |

## Inviting the bot

Roger needs a Discord application + bot user (Developer Portal → your app). Two things must be set
correctly, both in service of least privilege (ARCHITECTURE §2.4):

**Gateway intents — all privileged intents OFF.** In the portal's *Bot* tab, leave **Message
Content**, **Server Members**, and **Presence** disabled. Roger asserts this at startup and refuses
to run if any is on.

**Invite with only the permissions the tools use — never Administrator.** Use OAuth2 → URL Generator
with scopes `bot` and `applications.commands`, and tick exactly:

| Permission | Why |
|---|---|
| View Channels | read the structure it manages |
| Manage Channels | create and edit channels |
| Manage Roles | create (zero-perm) roles and set channel overwrites |
| Send Messages | post the digest, and `post_message` |
| Embed Links | the digest is posted as an embed |

That checklist is permission integer **`268454928`**. `Manage Roles` is the broad one — but Roger
only ever creates zero-permission roles and applies overwrites from a fixed allowlist, so the tool
surface is far narrower than the gateway grant (ARCHITECTURE §2.6, §2.7). Do **not** grant
Administrator; nothing Roger does needs it.

**Role hierarchy.** Discord only lets a bot manage roles/channels *below* its own top role, and only
grant permissions it holds. Drag Roger's role up so it sits above anything it needs to touch.

## First-time provision

Run from a checkout, against a host you can reach over SSH. Set `HOST` to that host.

```bash
HOST=your-deploy-host

# 1. Host prep: Docker, compose, age, sops.
ssh "$HOST" 'sudo bash -s' < deploy/bootstrap.sh

# 2. Generate the age key ON the host and print only its PUBLIC recipient.
ssh "$HOST" 'mkdir -p ~/.config/sops/age && \
  test -f ~/.config/sops/age/keys.txt || age-keygen -o ~/.config/sops/age/keys.txt; \
  chmod 600 ~/.config/sops/age/keys.txt; age-keygen -y ~/.config/sops/age/keys.txt'
#    -> put the age1... line into .sops.yaml (replace the placeholder), commit it.
#    -> keep an offline backup of the private key file. It is the only thing that can decrypt
#       roger.env, and it is never committed.

# 3. Provision the deploy dir and the encrypted env.
ssh "$HOST" 'sudo install -d -o "$USER" -g "$USER" /opt/roger && \
             sudo install -d /opt/roger/data && sudo chown 10001:10001 /opt/roger/data'
scp compose.yaml .sops.yaml "$HOST":/opt/roger/
scp roger.env "$HOST":/opt/roger/roger.env        # plaintext, transits SSH only
ssh "$HOST" 'cd /opt/roger && sops -e -i roger.env'  # encrypt in place; never committed

# 4. Install the deploy timer (must run via sudo so it picks up the deploy user).
scp -r deploy "$HOST":/tmp/roger-src
ssh "$HOST" 'sudo bash /tmp/roger-src/install-systemd.sh'

# 5. First deploy (the timer also fires within ~2 min of boot / install).
ssh "$HOST" 'sudo systemctl start roger-deploy.service'
ssh "$HOST" 'docker logs --tail 20 "$(docker ps -q --filter name=roger)"'
```

> **One-time GHCR step:** the first `release.yml` run publishes the package **private** by
> default. Make it public once in the package's settings → *Change visibility → Public*, so the
> host can pull without a token. After that, nothing else is manual.

The `/opt/roger/data` chown to `10001` matches the image's runtime uid (see `Dockerfile`), so the
read-only container can write the SQLite DB into the bind mount.

## Day-to-day

- **Ship a change:** push to `main`. CI gates it, the image builds, the host redeploys within
  ~5 minutes. Nothing else to do.
- **Deploy now:** `ssh "$HOST" 'sudo systemctl start roger-deploy.service'`.
- **Watch logs:** `journalctl -u roger-deploy.service -f` (deploys) or
  `docker logs -f "$(docker ps -q --filter name=roger)"` (the bot). Use plain `docker`, not
  `docker compose` — any compose subcommand re-interpolates the file and needs the decrypted
  env, so wrap those as `sops exec-env /opt/roger/roger.env 'docker compose … <cmd>'`.
- **Change config/secrets:** `cd /opt/roger && sops roger.env`, save, then trigger a deploy.
  `up -d` recreates the container with the new env.

## Notes

- **Pinning vs. tracking.** Third-party deps (base image, Python packages, sops) are pinned.
  roger's *own* image tracks the `:main` channel on purpose — that's what continuous deploy is.
  For release-gated prod, point `compose.yaml` at a `:sha-<...>` tag and bump it deliberately.
- **Why not watchtower.** It needs the Docker socket (root-equivalent) and nudges toward
  auto-`latest`. A plain `compose pull` on a timer is a smaller attack surface and keeps the
  version story honest.
- **Later, over a private overlay.** If the host joins a mesh VPN, a push-on-merge deploy
  (Actions → SSH over the overlay) becomes possible without exposing the host. The pull timer is
  a fine permanent fallback either way.
