# FIRELENS on Railway

The whole app (website + API) runs as **one Docker container** on Railway, built from this
repository on every push. Cost on the Hobby plan: **about US$10–12/month** (US$5 plan including
US$5 of usage, plus ~US$5–7 of memory, CPU, volume and traffic). Railway needs a payment card.

Files: `Dockerfile` (repository root: builds the website, then the API with the website inside),
`railway.json` (Railway settings), `.dockerignore`.

```
browser ──► https://<name>.up.railway.app ──► container: FastAPI (/api/*) + website (/)
                                                 └── volume /data: firelens.db, map-context tiles,
                                                     report cache, seasonal archive (optional)
```

## 1. Create the service (in the browser)

1. Sign up at https://railway.com with GitHub (the account that owns the repository), choose the
   **Hobby** plan and add the card.
2. **New Project → Deploy from GitHub repo →** `murafacu12/FireLens`. If the repository is not
   listed, use *Configure GitHub App* and give Railway access to it.
3. Railway finds `railway.json` and builds the root `Dockerfile`. The first build takes ~5 minutes.
   Don't worry if the first start fails before the volume exists: it is created next.

## 2. Volume: where the database lives

In the service: **Settings → Volumes → New Volume** (or right-click the service on the canvas →
*Attach volume*):

- **Mount path:** `/data`
- Size: the Hobby default is enough to start (the live database grows by ~50–100 MB a week).

Without the volume every redeploy would start from an empty database.

## 3. Variables

**Service → Variables → New Variable**:

| Variable | Value | Needed |
|---|---|---|
| `FIRELENS_ADMIN_TOKEN` | a long random key: `python -c "import secrets; print(secrets.token_urlsafe(32))"` | **Yes**: locks history loading, refresh and alert rules |
| `FIRELENS_FIRMS_MAP_KEY` | your NASA FIRMS key | Recommended: load past dates |
| `FIRELENS_COPERNICUS_CLIENT_ID` / `FIRELENS_COPERNICUS_CLIENT_SECRET` | Copernicus Data Space client | Optional: Sentinel readings per fire |
| `VITE_CESIUM_ION_TOKEN` | Cesium ion token | Optional: 3D terrain (used at build time) |

Already set inside the image (don't add them): `FIRELENS_DATABASE_URL=sqlite:////data/firelens.db`,
`FIRELENS_DATA_DIR=/data`, `FIRELENS_WEB_DIR=/app/web`, `FIRELENS_SEED_WINDOW=24h`. Railway
provides `PORT`.

Saving variables redeploys the service.

## 4. Public address

**Settings → Networking → Generate Domain**. Railway gives `https://<name>.up.railway.app` with
HTTPS. A custom domain can be added in the same place.

Check: `https://<name>.up.railway.app/api/health` shows `"status": "ok"` and `"admin_locked": true`.

## 5. First start and data

- On first start with an empty volume, FIRELENS loads the **last 24 hours worldwide** from NASA's
  public files (~1–2 minutes; the badge says *Loading*). Then it refreshes every hour.
- **History:** in the app, *Advanced → Calendar → Fire history*: pick dates and a country or the
  current view, and press *Load history*. It asks for the admin key once. It uses the FIRMS key and
  NASA's archive, so nothing has to be uploaded. FIRMS allows 5,000 requests per 10 minutes; one
  month of a country is ~30 requests.
- The seasonal comparison needs `baseline.db` on the volume (optional; without it that section
  says it has no archive). It can be rebuilt with `python -m ml.download_baseline` (needs the
  FIRMS key, ~5 hours).

## Operations

| Task | Where |
|---|---|
| Deploy a new version | push to the branch Railway watches (Settings → Source); it rebuilds and redeploys |
| Logs | service → **Deployments** → a deployment → **View logs** |
| Restart | service → ⋮ → **Restart** |
| Roll back | **Deployments** → an earlier deployment → **Redeploy** |
| Usage and cost | project → **Usage** (set a usage limit under Workspace → Usage to cap the bill) |
| Backups | volume → **Backups** (manual or scheduled snapshots) |

## Rules that matter

- **Exactly one replica** (`numReplicas: 1`). The hourly refresh and history loading run inside
  the API; two copies would both write the same database.
- Keep the admin key and the provider keys only in Railway's Variables, never in the repository.
- A release that changes the database structure: take a volume backup, add
  `FIRELENS_ALLOW_SCHEMA_UPGRADE=true` for one deploy, then remove it.
