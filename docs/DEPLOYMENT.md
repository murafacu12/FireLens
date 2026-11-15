# Deploying FIRELENS: Oracle Cloud (API) + Vercel (website)

Cost: **US$0/month** within Oracle's Always Free tier and Vercel's free plan.

```
 browser ──► https://<project>.vercel.app          (Vercel: the website, static files on a CDN)
    │
    └──────► https://<API_DOMAIN>/api/...          (Oracle VM: Caddy HTTPS ─► FastAPI + SQLite)
                                                    hourly NASA refresh runs inside the API
```

The browser calls the API directly (not through Vercel), so map tiles don't use Vercel's free quota.
The API allows the website's address through CORS.

Files: `backend/Dockerfile`, `deploy/docker-compose.yml`, `deploy/Caddyfile`,
`deploy/.env.production.example`, `deploy/setup-vm.sh`, `deploy/upload-data.sh`,
`deploy/backup.sh`, `frontend/vercel.json`.

---

## 1. Oracle Cloud: create the server (you, in the browser)

1. **Account:** https://www.oracle.com/cloud/free/. Choose the **home region** carefully, it can't
   change: **Brazil East (São Paulo)** or **Chile Central (Santiago)**. Oracle asks for a card to
   verify identity; Always Free resources are not charged.
2. **VM:** *Compute → Instances → Create instance*
   - Image: **Canonical Ubuntu 24.04** (or 22.04).
   - Shape: *Change shape → Ampere → **VM.Standard.A1.Flex***, **2 OCPUs, 12 GB RAM** (Always
     Free allows up to 4 OCPUs / 24 GB in total).
   - Networking: create a new VCN with a **public subnet**, and **assign a public IPv4 address**.
   - SSH keys: *Generate a key pair for me* and **download the private key**, or paste your own
     public key.
   - Boot volume: **100 GB** (Always Free allows 200 GB in total).
   - If it says *Out of capacity for shape VM.Standard.A1.Flex*: retry later or pick another
     availability domain. This is common and temporary.
3. **Open ports 80 and 443:** *Networking → Virtual cloud networks → your VCN → Security Lists →
   Default Security List → Add Ingress Rules*: source `0.0.0.0/0`, TCP, destination port `80`;
   and the same for `443`.
4. Note the VM's **public IP**.

## 2. Set up the server

From this PC (Git Bash), with the key you downloaded:

```bash
ssh -i ~/Downloads/ssh-key.key ubuntu@<VM-IP>
```

On the VM:

```bash
BRANCH=main   # or the branch to deploy, e.g. analysis-engine
curl -fsSL https://raw.githubusercontent.com/murafacu12/FireLens/$BRANCH/deploy/setup-vm.sh -o setup-vm.sh
BRANCH=$BRANCH bash setup-vm.sh   # Docker, firewall, swap, code in ~/FireLens, daily backup
exit                       # log out and back in once, so 'docker' works without sudo
```

If the repository is private, clone it with a GitHub token or deploy key instead, then run
`bash ~/FireLens/deploy/setup-vm.sh`. If the setup should use another branch:
`git -C ~/FireLens checkout <branch>`.

## 3. Settings and secrets

On the VM:

```bash
cd ~/FireLens/deploy
cp .env.production.example .env
nano .env
```

- `API_DOMAIN`: without your own domain use the free **sslip.io** name of the IP:
  IP `129.146.12.34` → `129-146-12-34.sslip.io`. It needs no setup.
- `FIRELENS_ADMIN_TOKEN`: a long random key (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).
  It locks loading data, ingest and alert rules. Keep it like a password.
- `FIRELENS_CORS_ORIGINS`: the website's address; fill it in after step 5.
- `FIRELENS_FIRMS_MAP_KEY`, Copernicus client: copy from your local `.env` if you use them.

## 4. Data and start

**Option A, start with everything already loaded** (recommended). From this PC, repository root:

```bash
bash deploy/upload-data.sh ubuntu@<VM-IP> ~/Downloads/ssh-key.key
```

It sends a consistent snapshot of `firelens.db` (~400 MB), `data/baseline.db` (~470 MB),
`data/enrichment.db` and the map-context tile cache `data/geo` (~800 MB; `SKIP_GEO=1` skips it,
tiles then download on demand). Training data stays here.

**Option B, start empty:** the API loads the last 7 days of NASA data by itself on first start.

Then on the VM:

```bash
cd ~/FireLens/deploy
docker compose up -d --build       # first build ~5–10 min on the Arm VM
docker compose logs -f api         # Ctrl+C to stop following
```

Check: `https://<API_DOMAIN>/api/health` in a browser shows `"status": "ok"` and
`"admin_locked": true`. The first visit may take a minute while Caddy gets the certificate.

## 5. Vercel: the website (you, in the browser)

1. https://vercel.com → sign in with GitHub → **Add New → Project** → import the FireLens repository.
2. **Root Directory:** `frontend`. Framework is detected (Vite); `frontend/vercel.json` sets the rest.
3. **Environment Variables:**
   - `VITE_API_BASE` = `https://<API_DOMAIN>` (no trailing slash)
   - `VITE_CESIUM_ION_TOKEN` = optional, for 3D terrain
4. **Deploy.** Note the address, e.g. `https://firelens.vercel.app`.
5. Back on the VM, put that address in `FIRELENS_CORS_ORIGINS` in `deploy/.env`, then:
   ```bash
   docker compose up -d
   ```

Every push to the production branch redeploys the website. `VITE_API_BASE` is read at build time:
after changing it, redeploy.

## 6. Admin key in the website

Loading history, "refresh now" and creating or deleting alert rules ask for the admin key the first
time; it is kept in that browser only. A wrong key is forgotten. Everyone else can use everything
else (map, analysis, calendar, fire details) without a key.

## Operations

| Task | Command (on the VM, in `~/FireLens/deploy`) |
|---|---|
| Update to the latest code | `git -C ~/FireLens pull && docker compose up -d --build` |
| Logs | `docker compose logs -f api` / `docker compose logs -f caddy` |
| Restart | `docker compose restart api` |
| Status | `docker compose ps` and `https://<API_DOMAIN>/api/health` |
| Backup now | `bash backup.sh` (daily at 04:30 by cron; last 7 kept in `backups/`) |
| Restore a backup | `docker compose stop api && gunzip -c backups/firelens-YYYYMMDD.db.gz > data/firelens.db && sudo chown 10001 data/firelens.db && rm -f data/firelens.db-wal data/firelens.db-shm && docker compose start api` |
| Release that changes the database | back up, add `FIRELENS_ALLOW_SCHEMA_UPGRADE=true` to `.env` for one start, then remove it |

**Off-server backups (optional):** create an Object Storage bucket (20 GB free), install and set up
the OCI CLI on the VM (`bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"`,
then `oci setup config`), and set `OCI_BUCKET=<bucket>` in the crontab line.

## Rules that matter

- **Run exactly one API container.** The hourly NASA refresh and history loading run inside it;
  two copies would both write.
- **Never commit `deploy/.env`.** It holds the admin key and the provider keys.
- **Oracle reclaims idle Always Free VMs** (CPU below 20% for 7 days, among other conditions).
  The hourly refresh keeps the VM busy; check the console's notices once in a while.
- The database is SQLite on the VM's disk. Moving to PostgreSQL is a separate, planned step for
  when many users or several servers are needed.
